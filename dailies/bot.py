import datetime
import logging
from typing import override

import discord
from discord.ext import tasks

from dailies.chore import Chore, parse_chore_from_json, ChoreParseException
from dailies.command import parse_chore_from_line
from dailies.logger import LOGGER, ROTATING_FILE_HANDLER
from dailies.util import TIME_FORMAT, TIMEZONE_FORMAT, DATE_FORMAT, DATETIME_FORMAT, VERSION, SerializableFile


class DailiesConfig(SerializableFile):
    discord_token: str = ""
    discord_remind_channel = 0
    remind_time: datetime.time = datetime.time(hour=9)
    timezone: datetime.tzinfo | None = None
    command_prefix: str = "."

    def __init__(self):
        super().__init__("config.json")

    @override
    def serialize(self) -> dict:
        return {
            "discord_token": self.discord_token,
            "discord_remind_channel": self.discord_remind_channel,
            "remind_time": self.remind_time.strftime(TIME_FORMAT),
            "timezone": None if self.timezone is None else datetime.time(tzinfo=self.timezone).strftime(TIMEZONE_FORMAT),
            "command_prefix": self.command_prefix
        }

    @override
    def deserialize(self, obj: dict):
        self.discord_token = obj["discord_token"]
        self.discord_remind_channel = obj["discord_remind_channel"]
        self.remind_time = datetime.datetime.strptime(obj["remind_time"], TIME_FORMAT).time()
        if "timezone" in obj and obj["timezone"] is not None:
            self.timezone = datetime.datetime.strptime(obj["timezone"], TIMEZONE_FORMAT).astimezone().tzinfo
        else:
            self.timezone = None
        self.command_prefix = obj["command_prefix"]


class DailiesState(SerializableFile):
    chores: dict[int, Chore] = {}
    upcoming_chores: dict[int, datetime.date] = {}
    last_chore_id: int = 0
    next_remind_date: datetime.date | None = None

    def __init__(self):
        super().__init__("state.json")

    def add_new_chore(self, chore) -> int:
        while self.last_chore_id in self.chores:
            self.last_chore_id += 1
        chore_id = self.last_chore_id
        self.chores[chore_id] = chore
        if chore.date is not None:
            self.upcoming_chores[chore_id] = chore.date
        else:
            self.upcoming_chores[chore_id] = chore.calculate_next_date()
        self.save()
        return chore_id

    @override
    def serialize(self) -> dict:
        chores: list[dict] = []
        for chore_id, chore in self.chores.items():
            chores.append({"id": chore_id, "chore": chore.to_json()})
        upcoming: list[dict] = []
        for chore_id, date in self.upcoming_chores.items():
            upcoming.append({"id": chore_id, "date": date.strftime(DATE_FORMAT)})
        return {
            "chores": chores,
            "upcoming_chores": upcoming,
            "last_chore_id": self.last_chore_id,
            "next_remind_date": None if self.next_remind_date is None else self.next_remind_date.strftime(DATE_FORMAT)
        }

    @override
    def deserialize(self, obj: dict):
        self.chores.clear()
        self.upcoming_chores.clear()
        for entry in obj["chores"]:
            self.chores[entry["id"]] = parse_chore_from_json(entry["chore"])
        for entry in obj["upcoming_chores"]:
            self.upcoming_chores[entry["id"]] = datetime.datetime.strptime(entry["date"], DATE_FORMAT).date()
        self.last_chore_id = obj["last_chore_id"]
        if "next_remind_date" in obj and obj["next_remind_date"] is not None:
            self.next_remind_date = datetime.datetime.strptime(obj["next_remind_date"], DATE_FORMAT).date()
        else:
            self.next_remind_date = None


class DailiesClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = DailiesConfig()
        self.config.load()
        if self.config.timezone is None:
            def_tz = datetime.datetime.now().astimezone().tzinfo
            LOGGER.info(f"Timezone not detected in `timezone` configuration field. Setting it to the system {def_tz}")
            self.config.timezone = def_tz
            self.config.save()
        LOGGER.info("Successfully loaded configuration file")
        self.state = DailiesState()
        self.state.load()
        if self.state.next_remind_date is None:
            rt = self.config.remind_time
            now = datetime.datetime.now(tz=self.config.timezone)
            rdt = now.replace(hour=rt.hour, minute=rt.minute, second=rt.second)
            if (rdt - now).total_seconds() < 0:
                rdt = rdt + datetime.timedelta(days=1)
            self.next_remind_dt = rdt
            self.state.next_remind_date = self.next_remind_dt.date()
            self.state.save()
            LOGGER.info(f"Starting first remind timer task at {rdt.strftime(DATETIME_FORMAT)}")
        else:
            rt = self.config.remind_time
            rd = self.state.next_remind_date
            self.next_remind_dt = datetime.datetime(year=rd.year, month=rd.month, day=rd.day, hour=rt.hour, minute=rt.minute, second=rt.second, tzinfo=self.config.timezone)
        LOGGER.info(f"Successfully loaded state file and found {len(self.state.chores)} chore(s)")

    async def on_ready(self):
        LOGGER.info(f"Version {VERSION} of Dailies Bot has been loaded")
        LOGGER.info(f"Logged in as {self.user}")
        self.remind_task.start()

    @tasks.loop(minutes=1)
    async def remind_task(self):
        now = datetime.datetime.now(tz=self.config.timezone)
        if (now - self.next_remind_dt).total_seconds() < 0:
            return

        LOGGER.info("Initiating reminder task...")
        channel = self.get_channel(self.config.discord_remind_channel)
        assert isinstance(channel, discord.abc.Messageable)

        current_chores: dict[int, list[Chore]] = {}

        update: dict[int, datetime.date] = {}
        delete: list[int] = []
        for chore_id, date in self.state.upcoming_chores.items():
            if (date - now.date()).days <= 0:
                chore = self.state.chores[chore_id]
                if chore.user not in current_chores:
                    current_chores[chore.user] = list()
                current_chores[chore.user].append(chore)
                if chore.date is None:
                    update[chore_id] = chore.calculate_next_date(date)
                else:
                    delete.append(chore_id)
        for chore_id, new_date in update.items():
            self.state.upcoming_chores[chore_id] = new_date
        for chore_id in delete:
            del self.state.chores[chore_id]
            del self.state.upcoming_chores[chore_id]
        if len(current_chores) == 0:
            message = "No chores for today!"
        else:
            message = "Here are your dailies:"
            for user, user_chores in current_chores.items():
                message += "\n" + "<@" + str(user) + "> " + ", ".join(map(lambda x: x.title, user_chores))

        rt = self.config.remind_time
        self.next_remind_dt = now.replace(hour=rt.hour, minute=rt.minute, second=rt.second) + datetime.timedelta(days=1)
        self.state.next_remind_date = self.next_remind_dt.date()
        self.state.save()
        LOGGER.info(f"Saved next reminder to occur on {self.next_remind_dt.strftime(DATETIME_FORMAT)}")
        await channel.send(message)


    async def on_message(self, message: discord.Message):
        if message.author.id == self.user.id or not message.content.startswith(self.config.command_prefix):
            return
        LOGGER.debug(f"Attempting to parse message from {message.author}#{message.author.id}: {message.content}")
        args = message.content[len(self.config.command_prefix):].split()
        reply = ""
        if args[0] == "list":
            if len(self.state.chores) == 0:
                reply = "No chores have been added..."
            else:
                reply = "List of all chores:\n" + "\n".join(map(lambda x: str(x), self.state.chores.values()))
        elif args[0] == "upcoming":
            if len(self.state.upcoming_chores) == 0:
                reply = "No upcoming chores..."
            else:
                reply = "List of all upcoming chores:"
                for chore_id, days_until in self.state.upcoming_chores.items():
                    chore = self.state.chores[chore_id]
                    reply += f"\n{days_until} day(s) until {chore.title} (<@{chore.user}>)"
        elif args[0] == "add":
            if len(args) == 1:
                reply = (
                    "Every *N* days: `.add <title> <user> every <days>d` (ex. `.add \"Do the dishes\" @user every 2d`)\n"
                    "Every *N* weeks: `.add <title> <user> every <weeks>w <weekday>` (ex. `.add \"Clean bathroom\" @user every 3w sunday`)\n"
                    "Every *N* months: `.add <title> <user> every <months>m <monthdays>` (ex. `.add \"Pay rent\" @user every 1m -3`)\n"
                    "On a specific date: `.add <title> <user> on <yyyy/mm/dd>` (ex. `.add \"Sign up for classes\" @user on 2025/06/01`)")
            else:
                try:
                    chore = parse_chore_from_line(args[1])
                    chore_id = self.state.add_new_chore(chore)
                    reply = f"Successfully added new chore `{chore.title}` for <@{chore.user}> (id: {chore_id})"
                except ChoreParseException as e:
                    reply = e.message
        elif args[0] == "delete":
            chore_id = None
            try:
                chore_id = int(args[1])
            except ValueError:
                reply = f"Chore ID not found: `{args[1]}`"
            if chore_id is not None:
                if chore_id not in self.state.chores:
                    reply = f"Chore ID not found: {chore_id}"
                else:
                    del self.state.chores[chore_id]
                    if chore_id in self.state.upcoming_chores:
                        del self.state.upcoming_chores[chore_id]
                    self.state.save()
                    reply = "Chore has successfully been deleted."
        await message.reply(reply, allowed_mentions=discord.AllowedMentions(users=False))


def run_bot():
    intents = discord.Intents.default()
    intents.message_content = True
    client = DailiesClient(intents=intents)
    if client.config.discord_token == "" or client.config.discord_remind_channel == 0:
        LOGGER.error("Shutting down client, must specify Discord token and channel ID in `config.json`")
    else:
        LOGGER.info("Running Dailies Bot Discord client...")
        client.run(client.config.discord_token, log_handler=ROTATING_FILE_HANDLER, log_level=logging.DEBUG)


if __name__ == "__main__":
    run_bot()
