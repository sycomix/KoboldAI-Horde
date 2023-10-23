import json, os
from uuid import uuid4
from datetime import datetime
import threading, time
from logger import logger

class WaitingPrompt:
    # Every 10 secs we store usage data to disk
    def __init__(self, db, wps, pgs, prompt, user, models, params, **kwargs):
        self._db = db
        self._waiting_prompts = wps
        self._processing_generations = pgs
        self.prompt = prompt
        self.user = user
        self.models = models
        self.params = params
        self.n = params.get('n', 1)
        # We assume more than 20 is not needed. But I'll re-evalute if anyone asks.
        if self.n > 20:
            logger.warning(f"User {self.user.get_unique_alias()} requested {self.n} gens per action. Reducing to 20...")
            self.n = 20
        self.max_length = params.get("max_length", 80)
        self.max_content_length = params.get("max_content_length", 1024)
        self.total_usage = 0
        self.id = str(uuid4())
        # This is what we send to KoboldAI to the /generate/ API
        self.gen_payload = params
        self.gen_payload["prompt"] = prompt
        # We always send only 1 iteration to KoboldAI
        self.gen_payload["n"] = 1
        # The generations that have been created already
        self.processing_gens = []
        self.last_process_time = datetime.now()
        self.servers = kwargs.get("servers", [])
        self.softprompts = kwargs.get("softprompts", [''])
        # Prompt requests are removed after 10 mins of inactivity, to prevent memory usage
        self.stale_time = 600


    def activate(self):
        # We separate the activation from __init__ as often we want to check if there's a valid server for it
        # Before we add it to the queue
        self._waiting_prompts.add_item(self)
        logger.info(f"New prompt request by user: {self.user.get_unique_alias()}")
        thread = threading.Thread(target=self.check_for_stale, args=())
        thread.daemon = True
        thread.start()

    def needs_gen(self):
        return self.n > 0

    def start_generation(self, server, matching_softprompt):
        if self.n <= 0:
            return
        new_gen = ProcessingGeneration(self, self._processing_generations, server)
        self.processing_gens.append(new_gen)
        self.n -= 1
        self.refresh()
        return {
            "payload": self.gen_payload,
            "softprompt": matching_softprompt,
            "id": new_gen.id,
        }

    def is_completed(self):
        if self.needs_gen():
            return(False)
        return all(procgen.is_completed() for procgen in self.processing_gens)

    def count_processing_gens(self):
        ret_dict = {
            "finished": 0,
            "processing": 0,
        }
        for procgen in self.processing_gens:
            if procgen.is_completed():
                ret_dict["finished"] += 1
            else:
                ret_dict["processing"] += 1
        return(ret_dict)

    def get_status(self):
        ret_dict = self.count_processing_gens()
        ret_dict["waiting"] = self.n
        ret_dict["done"] = self.is_completed()
        ret_dict["generations"] = []
        for procgen in self.processing_gens:
            if procgen.is_completed():
                gen_dict = {
                    "text": procgen.generation,
                    "server_id": procgen.server.id,
                    "server_name": procgen.server.name,
                }
                ret_dict["generations"].append(gen_dict)
        return(ret_dict)

    def record_usage(self, chars, kudos):
        self.total_usage += chars
        self.user.record_usage(chars, kudos)
        self.refresh()

    def check_for_stale(self):
        while True:
            if self.is_stale():
                self.delete()
                break
            time.sleep(10)

    def delete(self):
        for gen in self.processing_gens:
            gen.delete()
        self._waiting_prompts.del_item(self)
        del self

    def refresh(self):
        self.last_process_time = datetime.now()

    def is_stale(self):
        return (datetime.now() - self.last_process_time).seconds > self.stale_time


class ProcessingGeneration:
    def __init__(self, owner, pgs, server):
        self._processing_generations = pgs
        self.id = str(uuid4())
        self.owner = owner
        self.server = server
        # We store the model explicitly, in case the server changed models between generations
        self.model = server.model
        self.generation = None
        self.start_time = datetime.now()
        self._processing_generations.add_item(self)

    def set_generation(self, generation):
        if self.is_completed():
            return(0)
        self.generation = generation
        chars = len(generation)
        kudos = self.owner._db.convert_chars_to_kudos(chars, self.model)
        self.server.record_contribution(chars, kudos, (datetime.now() - self.start_time).seconds)
        self.owner.record_usage(chars, kudos)
        logger.info(f"New Generation worth {kudos} kudos, delivered by server: {self.server.name}")
        return(chars)

    def is_completed(self):
        return bool(self.generation)

    def delete(self):
        self._processing_generations.del_item(self)
        del self


class KAIServer:
    def __init__(self, db):
        self._db = db
        self.kudos_details = {
            "generated": 0,
            "uptime": 0,
        }
        self.last_reward_uptime = 0
        # Every how many seconds does this server get a kudos reward
        self.uptime_reward_threshold = 600

    def create(self, user, name, softprompts):
        self.user = user
        self.name = name
        self.softprompts = softprompts
        self.id = str(uuid4())
        self.contributions = 0
        self.fulfilments = 0
        self.kudos = 0
        self.performances = []
        self.uptime = 0
        self._db.register_new_server(self)

    def check_in(self, model, max_length, max_content_length, softprompts):
        if not self.is_stale():
            self.uptime += (datetime.now() - self.last_check_in).seconds
            # Every 10 minutes of uptime gets kudos rewarded
            if self.uptime - self.last_reward_uptime > self.uptime_reward_threshold:
                # Bigger model uptime gets more kudos
                kudos = round(self._db.calculate_model_multiplier(model) / 2.75, 2)
                self.modify_kudos(kudos,'uptime')
                self.user.record_uptime(kudos)
                logger.debug(f"server '{self.name}' received {kudos} kudos for uptime of {self.uptime_reward_threshold} seconds.")
                self.last_reward_uptime = self.uptime
        else:
            # If the server comes back from being stale, we just reset their last_reward_uptime
            # So that they have to stay up at least 10 mins to get uptime kudos
            self.last_reward_uptime = self.uptime
        self.last_check_in = datetime.now()
        self.model = model
        self.max_content_length = max_content_length
        self.max_length = max_length
        self.softprompts = softprompts

    def get_human_readable_uptime(self):
        if self.uptime < 60:
            return(f"{self.uptime} seconds")
        elif self.uptime < 60*60:
            return(f"{round(self.uptime/60,2)} minutes")
        elif self.uptime < 60*60*24:
            return(f"{round(self.uptime/60/60,2)} hours")
        else:
            return(f"{round(self.uptime/60/60/24,2)} days")

    def can_generate(self, waiting_prompt):
        # takes as an argument a WaitingPrompt class and checks if this server is valid for generating it
        is_matching = True
        skipped_reason = None
        if len(waiting_prompt.servers) >= 1 and self.id not in waiting_prompt.servers:
            is_matching = False
            skipped_reason = 'server_id'
        if len(waiting_prompt.models) >= 1 and self.model not in waiting_prompt.models:
            is_matching = False
            skipped_reason = 'models'
        if self.max_content_length < waiting_prompt.max_content_length:
            is_matching = False
            skipped_reason = 'max_content_length'
        if self.max_length < waiting_prompt.max_length:
            is_matching = False
            skipped_reason = 'max_length'
        matching_softprompt = False
        for sp in waiting_prompt.softprompts:
            # If a None softprompts has been provided, we always match, since we can always remove the softprompt
            if sp == '':
                matching_softprompt = True
                break
            for sp_name in self.softprompts:
                if sp in sp_name:
                    matching_softprompt = True
                    break
        if not matching_softprompt:
            is_matching = False
            skipped_reason = 'matching_softprompt'
        return([is_matching,skipped_reason])

    def record_contribution(self, chars, kudos, seconds_taken):
        perf = round(chars / seconds_taken,1)
        self.user.record_contributions(chars, kudos)
        self.modify_kudos(kudos,'generated')
        self._db.record_fulfilment(perf)
        self.contributions += chars
        self.fulfilments += 1
        self.performances.append(perf)
        if len(self.performances) > 20:
            del self.performances[0]

    def modify_kudos(self, kudos, action = 'generated'):
        self.kudos = round(self.kudos + kudos, 2)
        self.kudos_details[action] = round(self.kudos_details.get(action,0) + abs(kudos), 2) 

    def get_performance(self):
        return (
            f'{round(sum(self.performances) / len(self.performances), 1)} chars per second'
            if len(self.performances)
            else 'No requests fulfilled yet'
        )

    def is_stale(self):
        try:
            if (datetime.now() - self.last_check_in).seconds > 300:
                return(True)
        # If the last_check_in isn't set, it's a new server, so it's stale by default
        except AttributeError:
            return(True)
        return(False)

    def serialize(self):
        return {
            "oauth_id": self.user.oauth_id,
            "name": self.name,
            "model": self.model,
            "max_length": self.max_length,
            "max_content_length": self.max_content_length,
            "contributions": self.contributions,
            "fulfilments": self.fulfilments,
            "kudos": self.kudos,
            "kudos_details": self.kudos_details,
            "performances": self.performances,
            "last_check_in": self.last_check_in.strftime("%Y-%m-%d %H:%M:%S"),
            "id": self.id,
            "softprompts": self.softprompts,
            "uptime": self.uptime,
        }

    def deserialize(self, saved_dict):
        self.user = self._db.find_user_by_oauth_id(saved_dict["oauth_id"])
        self.name = saved_dict["name"]
        self.model = saved_dict["model"]
        self.max_length = saved_dict["max_length"]
        self.max_content_length = saved_dict["max_content_length"]
        self.contributions = saved_dict["contributions"]
        self.fulfilments = saved_dict["fulfilments"]
        self.kudos = saved_dict.get("kudos",0)
        self.kudos_details = saved_dict.get("kudos_details",self.kudos_details)
        self.performances = saved_dict.get("performances",[])
        self.last_check_in = datetime.strptime(saved_dict["last_check_in"],"%Y-%m-%d %H:%M:%S")
        self.id = saved_dict["id"]
        self.softprompts = saved_dict.get("softprompts",[])
        self.uptime = saved_dict.get("uptime",0)
        self._db.servers[self.name] = self

class Index:
    def __init__(self):
        self._index = {}

    def add_item(self, item):
        self._index[item.id] = item

    def get_item(self, uuid):
        return(self._index.get(uuid))

    def del_item(self, item):
        del self._index[item.id]

    def get_all(self):
        return(self._index.values())


class PromptsIndex(Index):

    def count_waiting_requests(self, user):
        return sum(
            1
            for wp in self._index.values()
            if wp.user == user and not wp.is_completed()
        )

    def count_total_waiting_generations(self):
        return sum(wp.n for wp in self._index.values())

    def get_waiting_wp_by_kudos(self):
        sorted_wp_list = sorted(self._index.values(), key=lambda x: x.user.kudos, reverse=True)
        return [wp for wp in sorted_wp_list if wp.needs_gen()]



class GenerationsIndex(Index):
    pass

class User:
    def __init__(self, db):
        self._db = db
        self.kudos = 0
        self.kudos_details = {
            "accumulated": 0,
            "gifted": 0,
            "received": 0,
        }

    def create_anon(self):
        self.username = 'Anonymous'
        self.oauth_id = 'anon'
        self.api_key = '0000000000'
        self.invite_id = ''
        self.creation_date = datetime.now()
        self.last_active = datetime.now()
        self.id = 0
        self.contributions = {
            "chars": 0,
            "fulfillments": 0
        }
        self.usage = {
            "chars": 0,
            "requests": 0
        }

    def create(self, username, oauth_id, api_key, invite_id):
        self.username = username
        self.oauth_id = oauth_id
        self.api_key = api_key
        self.invite_id = invite_id
        self.creation_date = datetime.now()
        self.last_active = datetime.now()
        self.id = self._db.register_new_user(self)
        self.contributions = {
            "chars": 0,
            "fulfillments": 0
        }
        self.usage = {
            "chars": 0,
            "requests": 0
        }

    # Checks that this user matches the specified API key
    def check_key(api_key):
        return bool(self.api_key and self.api_key == api_key)

    def get_unique_alias(self):
        return(f"{self.username}#{self.id}")

    def record_usage(self, chars, kudos):
        self.usage["chars"] += chars
        self.usage["requests"] += 1
        self.modify_kudos(-kudos,"accumulated")

    def record_contributions(self, chars, kudos):
        self.contributions["chars"] += chars
        self.contributions["fulfillments"] += 1
        self.modify_kudos(kudos,"accumulated")

    def record_uptime(self, kudos):
        self.modify_kudos(kudos,"accumulated")

    def modify_kudos(self, kudos, action = 'accumulated'):
        self.kudos = round(self.kudos + kudos, 2)
        self.kudos_details[action] = round(self.kudos_details.get(action,0) + kudos, 2)


    def serialize(self):
        return {
            "username": self.username,
            "oauth_id": self.oauth_id,
            "api_key": self.api_key,
            "kudos": self.kudos,
            "kudos_details": self.kudos_details,
            "id": self.id,
            "invite_id": self.invite_id,
            "contributions": self.contributions,
            "usage": self.usage,
            "creation_date": self.creation_date.strftime("%Y-%m-%d %H:%M:%S"),
            "last_active": self.last_active.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def deserialize(self, saved_dict):
        self.username = saved_dict["username"]
        self.oauth_id = saved_dict["oauth_id"]
        self.api_key = saved_dict["api_key"]
        self.kudos = saved_dict["kudos"]
        self.kudos_details = saved_dict.get("kudos_details", self.kudos_details)
        self.id = saved_dict["id"]
        self.invite_id = saved_dict["invite_id"]
        self.contributions = saved_dict["contributions"]
        # if "tokens" in self.contributions:
        #     self.contributions["chars"] = self.contributions["tokens"] * 4
        #     self.record_contributions(self.contributions["chars"], self.contributions["chars"] / 100)
        #     del self.contributions["tokens"]
        self.usage = saved_dict["usage"]
        # if "tokens" in self.usage:
        #     self.usage["chars"] = self.usage["tokens"] * 4
        #     self.record_contributions(self.usage["chars"], -self.usage["chars"] / 100)
        #     del self.usage["tokens"]
        self.creation_date = datetime.strptime(saved_dict["creation_date"],"%Y-%m-%d %H:%M:%S")
        self.last_active = datetime.strptime(saved_dict["last_active"],"%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, interval = 3):
        self.interval = interval
        self.ALLOW_ANONYMOUS = True
        # This is used for synchronous generations
        self.SERVERS_FILE = "db/servers.json"
        self.servers = {}
        # Other miscellaneous statistics
        self.STATS_FILE = "db/stats.json"
        self.stats = {
            "fulfilment_times": [],
            "model_mulitpliers": {},
        }
        self.USERS_FILE = "db/users.json"
        self.users = {}
        # Increments any time a new user is added
        # Is appended to usernames, to ensure usernames never conflict
        self.last_user_id = 0
        if os.path.isfile(self.USERS_FILE):
            with open(self.USERS_FILE) as db:
                serialized_users = json.load(db)
                for user_dict in serialized_users:
                    new_user = User(self)
                    new_user.deserialize(user_dict)
                    self.users[new_user.oauth_id] = new_user
                    if new_user.id > self.last_user_id:
                        self.last_user_id = new_user.id
        self.anon = self.find_user_by_oauth_id('anon')
        if not self.anon:
            self.anon = User(self)
            self.anon.create_anon()
            self.users[self.anon.oauth_id] = self.anon
        if os.path.isfile(self.SERVERS_FILE):
            with open(self.SERVERS_FILE) as db:
                serialized_servers = json.load(db)
                for server_dict in serialized_servers:
                    new_server = KAIServer(self)
                    new_server.deserialize(server_dict)
                    self.servers[new_server.name] = new_server
        if os.path.isfile(self.STATS_FILE):
            with open(self.STATS_FILE) as db:
                self.stats = json.load(db)

        thread = threading.Thread(target=self.write_files, args=())
        thread.daemon = True
        thread.start()

    def write_files(self):
        while True:
            self.write_files_to_disk()
            time.sleep(self.interval)

    def write_files_to_disk(self):
        if not os.path.exists('db'):
            os.mkdir('db')
        server_serialized_list = [
            server.serialize()
            for server in self.servers.values()
            if server.user != self.anon
        ]
        with open(self.SERVERS_FILE, 'w') as db:
            json.dump(server_serialized_list,db)
        with open(self.STATS_FILE, 'w') as db:
            json.dump(self.stats,db)
        user_serialized_list = [user.serialize() for user in self.users.values()]
        with open(self.USERS_FILE, 'w') as db:
            json.dump(user_serialized_list,db)

    def get_top_contributor(self):
        top_contribution = 0
        top_contributor = None
        user = None
        for user in self.users.values():
            if user.contributions['chars'] > top_contribution and user != self.anon:
                top_contributor = user
                top_contribution = user.contributions['chars']
        return(top_contributor)

    def get_top_server(self):
        top_server = None
        top_server_contribution = 0
        for server in self.servers:
            if self.servers[server].contributions > top_server_contribution:
                top_server = self.servers[server]
                top_server_contribution = self.servers[server].contributions
        return(top_server)

    def get_available_models(self):
        models_ret = {}
        for server in self.servers.values():
            if server.is_stale():
                continue
            models_ret[server.model] = models_ret.get(server.model,0) + 1
        return(models_ret)

    def count_active_servers(self):
        return sum(1 for server in self.servers.values() if not server.is_stale())

    def get_total_usage(self):
        totals = {
            "chars": 0,
            "fulfilments": 0,
        }
        for server in self.servers.values():
            totals["chars"] += server.contributions
            totals["fulfilments"] += server.fulfilments
        return(totals)

    def record_fulfilment(self, token_per_sec):
        if len(self.stats["fulfilment_times"]) >= 10:
            del self.stats["fulfilment_times"][0]
        self.stats["fulfilment_times"].append(token_per_sec)

    def get_request_avg(self):
        if len(self.stats["fulfilment_times"]) == 0:
            return(0)
        avg = sum(self.stats["fulfilment_times"]) / len(self.stats["fulfilment_times"])
        return(round(avg,1))

    def register_new_user(self, user):
        self.last_user_id += 1
        self.users[user.oauth_id] = user
        logger.info(f'New user created: {user.username}#{self.last_user_id}')
        return(self.last_user_id)

    def register_new_server(self, server):
        self.servers[server.name] = server
        logger.info(f'New server checked-in: {server.name} by {server.user.get_unique_alias()}')

    def find_user_by_oauth_id(self,oauth_id):
        if oauth_id == 'anon' and not self.ALLOW_ANONYMOUS:
            return(None)
        return(self.users.get(oauth_id))

    def find_user_by_username(self, username):
        for user in self.users.values():
            uniq_username = username.split('#')
            if user.username == uniq_username[0] and user.id == int(uniq_username[1]):
                return None if user == self.anon and not self.ALLOW_ANONYMOUS else user
        return(None)

    def find_user_by_api_key(self,api_key):
        return next(
            (
                None if user == self.anon and not self.ALLOW_ANONYMOUS else user
                for user in self.users.values()
                if user.api_key == api_key
            ),
            None,
        )

    def find_server_by_name(self,server_name):
        return(self.servers.get(server_name))

    def transfer_kudos(self, source_user, dest_user, amount):
        if amount > source_user.kudos:
            return([0,'Not enough kudos.'])
        source_user.modify_kudos(-amount, 'gifted')
        dest_user.modify_kudos(amount, 'received')
        return([amount,'OK'])

    def transfer_kudos_to_username(self, source_user, dest_username, amount):
        dest_user = self.find_user_by_username(dest_username)
        if not dest_user:
            return([0,'Invalid target username.'])
        if dest_user == self.anon:
            return([0,'Tried to burn kudos via sending to Anonymous. Assuming PEBKAC and aborting.'])
        if dest_user == source_user:
            return([0,'Cannot send kudos to yourself, ya monkey!'])
        return self.transfer_kudos(source_user,dest_user, amount)

    def transfer_kudos_from_apikey_to_username(self, source_api_key, dest_username, amount):
        if source_user := self.find_user_by_api_key(source_api_key):
            return (
                ([0, 'You cannot transfer Kudos from Anonymous, smart-ass.'])
                if source_user == self.anon
                else self.transfer_kudos_to_username(
                    source_user, dest_username, amount
                )
            )
        else:
            return([0,'Invalid API Key.'])

    def calculate_model_multiplier(self, model_name):
        # To avoid doing this calculations all the time
        multiplier = self.stats["model_mulitpliers"].get(model_name)
        if multiplier:
            return(multiplier)
        try:
            import transformers, accelerate
            config = transformers.AutoConfig.from_pretrained(model_name)
            with accelerate.init_empty_weights():
                model = transformers.AutoModelForCausalLM.from_config(config)
            params_sum = sum(v.numel() for v in model.state_dict().values())
            logger.info(params_sum)
            multiplier = params_sum / 1000000000
        except OSError:
            logger.error(f"Model '{model_name}' not found in hugging face. Defaulting to multiplier of 1.")
            multiplier = 1
        self.stats["model_mulitpliers"][model_name] = multiplier
        return(multiplier)

    def convert_chars_to_kudos(self, chars, model_name):
        multiplier = self.calculate_model_multiplier(model_name)
        return round(chars * multiplier / 100,2)

