import datetime
import logging
from typing import override

import discord
from discord.ext import tasks

from dailies.chore import Chore, parse_chore_from_json, ChoreParseException, add_months, get_monthday
from dailies.command import parse_chore_from_line, parse_duration
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
        LOGGER.debug(f"Attempting to parse message from {message.author}: {message.content}")
        args = message.content[len(self.config.command_prefix):].split()
        reply = ""
        if args[0] == "list":
            if len(self.state.chores) == 0:
                reply = "No chores have been added..."
            else:
                reply = "List of all chores:"
                for chore_id, chore in self.state.chores.items():
                    reply += f"\n* [`{chore_id}`] {chore.format_message()}"
        elif args[0] == "upcoming":
            if len(self.state.upcoming_chores) == 0:
                reply = "No upcoming chores..."
            else:
                reply = "List of all upcoming chores:"
                for chore_id, remind_date in self.state.upcoming_chores.items():
                    chore = self.state.chores[chore_id]
                    diff = remind_date - datetime.datetime.now().date()
                    reply += f"\n* [`{chore_id}`] {diff.days} day(s) until <@{chore.user}> needs to {chore.title}"
        elif args[0] == "add":
            if len(args) == 1:
                p = self.config.command_prefix
                reply = (
                    "Schedules a new chore.\n"
                    f"Usage #1: `{p}add <title> <user> every <days>d`\n"
                    f"Usage #2: `{p}add <title> <user> every <weeks>w <weekday>`\n"
                    f"Usage #3: `{p}add <title> <user> every <months>m <monthdays>`\n"
                    f"Usage #4: `{p}add <title> <user> on <yyyy/mm/dd>`\n\n"
                    "Examples:\n"
                    f"* Schedule a chore every 2 days: `{p}add \"Do the dishes\" @user every 2d`\n"
                    f"* Schedule a chore every 3 weeks on Sundays: `{p}add \"Clean bathroom\" @user every 3w sunday`\n"
                    f"* Schedule a chore every month on the 4th-to-last day: `{p}add \"Pay rent\" @user every 1m -3`\n"
                    f"* Schedule a chore on a specific date: `{p}add \"Sign up for classes\" @user on 2025/06/01`")
            else:
                try:
                    chore = parse_chore_from_line(args[1:])
                    chore_id = self.state.add_new_chore(chore)
                    LOGGER.info(f"Added new chore: [{chore_id}] {chore}")
                    reply = f"Successfully added new chore `{chore.title}` for <@{chore.user}> (id: {chore_id})"
                except ChoreParseException as e:
                    reply = e.message
        elif args[0] == "delete":
            if len(args) == 1:
                p = self.config.command_prefix
                reply = (
                    "Deletes a chore from both the overall chore list and the upcoming chores list. You can use "
                    f"`{p}list` to see all chores and their corresponding IDs.\n"
                    f"Usage: `{p}delete <chore ID>`\n\n"
                    "Example:\n"
                    f"* Delete chore with chore ID 7: `{p}delete 17`")
            chore_id = None
            try:
                chore_id = int(args[1])
            except ValueError:
                reply = f"Chore ID not found: {args[1]}"
            if chore_id is not None:
                if chore_id not in self.state.chores:
                    reply = f"Chore ID not found: {chore_id}"
                else:
                    chore = self.state.chores[chore_id]
                    del self.state.chores[chore_id]
                    if chore_id in self.state.upcoming_chores:
                        del self.state.upcoming_chores[chore_id]
                    self.state.save()
                    LOGGER.info(f"Deleted chore [{chore_id}] {chore}")
                    reply = "Chore has successfully been deleted."
        elif args[0] == "delay":
            if len(args) < 3:
                p = self.config.command_prefix
                reply = (
                    f"Delays an upcoming chore by a specified amount of time. You can use `{p}upcoming` to see all "
                    "upcoming chores and their corresponding IDs.\n"
                    f"Usage: `{p}delay <chore ID> <duration>`\n\n"
                    "Examples:\n"
                    f"* Delay chore ID 16 by 2 weeks: `{p}delay 16 2w`\n"
                    f"* Delay chore ID 23 by 5 days: `{p}delay 23 5d`\n"
                    f"* Delay chore ID 30 by 1 month: `{p}delay 30 1m`")
            else:
                chore_id = None
                try:
                    chore_id = int(args[1])
                except ValueError:
                    reply = f"Chore ID not found: {args[1]}"
                if chore_id is not None:
                    if chore_id not in self.state.upcoming_chores:
                        reply = f"Chore ID not found: {chore_id}"
                    else:
                        remind_date = self.state.upcoming_chores[chore_id]
                        chore = self.state.chores[chore_id]
                        n, unit = parse_duration(args[2])
                        new_remind_date = None
                        if unit == "d":
                            new_remind_date = remind_date + datetime.timedelta(days=n)
                        elif unit == "w":
                            new_remind_date = remind_date + datetime.timedelta(days=n * 7)
                        elif unit == "m":
                            next_year, next_month = add_months(remind_date.year, remind_date.month, n)
                            day = get_monthday(next_year, next_month, chore.monthdays)
                            new_remind_date = datetime.date(next_year, next_month, day)
                        else:
                            reply = f"Invalid duration: {args[2]}"
                        if new_remind_date is not None:
                            self.state.upcoming_chores[chore_id] = new_remind_date
                            self.state.save()
                            LOGGER.info(f"Updated reminder for upcoming chore to occur at {new_remind_date}: [{chore_id}] {chore}")
                            reply = f"Reminder for chore will now occur at {new_remind_date}: {chore.format_message()}"
        elif args[0] == "ping":
            reply = "Pong!"
        elif args[0] == "help" or args[0] == "cmds" or args[0] == "commands" or args[0] == "?":
            p = self.config.command_prefix
            reply = (
                "Commands:\n"
                f"* `{p}help`: Show this message\n"
                f"* `{p}ping`: Test if this bot is responding\n"
                f"* `{p}add`: Add and schedule a new chore\n"
                f"* `{p}list`: List all chores\n"
                f"* `{p}upcoming`: List all upcoming chores\n"
                f"* `{p}delay`: Delay an upcoming chore\n"
                f"* `{p}delete`: Delete a chore")
        else:
            # unknown command, ignore.
            return
        if reply == "":
            LOGGER.warning(f"Tried replying with an empty message to {message.author}: {message.content}")
        else:
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
