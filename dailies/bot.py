import datetime
import logging
import zoneinfo
from typing import override

import discord
from discord.ext import tasks

from dailies.chore import Chore, parse_chore_from_json, ChoreParseException, add_months, get_monthday
from dailies.command import parse_chore_from_line, parse_duration
from dailies.logger import LOGGER, ROTATING_FILE_HANDLER
from dailies.util import TIME_FORMAT, DATE_FORMAT, DATETIME_FORMAT, VERSION, SerializableFile


class DailiesConfig(SerializableFile):
    discord_token: str = ""
    discord_remind_channel = 0
    remind_time: datetime.time = datetime.time(hour=9)
    timezone: zoneinfo.ZoneInfo = zoneinfo.ZoneInfo("UTC")
    command_prefix: str = "."

    def __init__(self):
        super().__init__("config.json")

    @override
    def serialize(self) -> dict:
        return {
            "discord_token": self.discord_token,
            "discord_remind_channel": self.discord_remind_channel,
            "remind_time": self.remind_time.strftime(TIME_FORMAT),
            "timezone": self.timezone.key,
            "command_prefix": self.command_prefix
        }

    @override
    def deserialize(self, obj: dict):
        self.discord_token = obj["discord_token"]
        self.discord_remind_channel = obj["discord_remind_channel"]
        self.remind_time = datetime.datetime.strptime(obj["remind_time"], TIME_FORMAT).time()
        self.timezone = zoneinfo.ZoneInfo(obj["timezone"])
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
    next_remind_dt: datetime.datetime

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = DailiesConfig()
        self.config.load()
        LOGGER.info("Successfully loaded configuration file")
        self.state = DailiesState()
        self.state.load()
        if self.state.next_remind_date is None:
            rt = self.config.remind_time
            now = datetime.datetime.now().astimezone()
            rdt = now.replace(hour=rt.hour, minute=rt.minute, second=rt.second, tzinfo=self.config.timezone)
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
        if self.config.discord_remind_channel == 0:
            LOGGER.warning("Remind channel is not set. You can do this by either sending `.config set channel #<channel>` in Discord or by filling out the `discord_remind_channel` field in `config.json`.")
        self.remind_task.start()

    @tasks.loop(minutes=1)
    async def remind_task(self):
        if self.config.discord_remind_channel == 0:
            return

        now = datetime.datetime.now().astimezone()
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
        reminders = []
        if len(current_chores) == 0:
            msgtext = "No chores for today!"
        else:
            msgtext = "Here are your dailies:"
            for user, user_chores in current_chores.items():
                reminders.append("<@" + str(user) + "> " + ", ".join(map(lambda x: x.title, user_chores)))
        rt = self.config.remind_time
        self.next_remind_dt = datetime.datetime(now.year, now.month, now.day, rt.hour, rt.minute, rt.second, tzinfo=self.config.timezone) + datetime.timedelta(days=1)
        self.state.next_remind_date = self.next_remind_dt.date()
        self.state.save()
        LOGGER.info(f"Saved next reminder to occur on {self.state.next_remind_date}")
        await channel.send(msgtext)
        if len(reminders) > 0:
            for reminder in reminders:
                message = await channel.send(reminder)
                await message.add_reaction("✅")


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
                now = datetime.datetime.now().astimezone()
                for chore_id, remind_date in self.state.upcoming_chores.items():
                    chore = self.state.chores[chore_id]
                    remind_dt = datetime.datetime(remind_date.year, remind_date.month, remind_date.day, self.config.remind_time.hour, self.config.remind_time.minute, tzinfo=self.config.timezone)
                    diff = int((remind_dt - now).total_seconds())
                    reply += f"\n* [`{chore_id}`] "
                    if diff < 86400: # seconds in a day
                        hours = diff // 3600
                        reply += f"{hours} hour"
                        if hours != 1:
                            reply += "s"
                    else:
                        days = diff // 86400
                        reply += f"{days} day"
                        if days != 1:
                            reply += "s"
                    reply += f" until <@{chore.user}> needs to {chore.title}"
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
                f"* `{p}add`: Add and schedule a new chore\n"
                f"* `{p}list`: List all chores\n"
                f"* `{p}upcoming`: List all upcoming chores\n"
                f"* `{p}delay`: Delay an upcoming chore\n"
                f"* `{p}delete`: Delete a chore\n"
                f"* `{p}config`: Configure this bot\n"
                f"* `{p}ping`: Test if this bot is responding\n"
                f"* `{p}version`: Show version information\n"
                f"* `{p}help`: Show this message\n")
        elif args[0] == "config":
            p = self.config.command_prefix
            usage = ("Allows setting and viewing of configuration values for the bot.\n"
                     f"Usage #1: `{p}config set <field> <value>`\n"
                     f"Usage #2: `{p}config get`\n\n"
                     "Fields:\n"
                     "* `channel`: The Discord channel where the bot will send reminders. If this is not set, the bot will not send any reminders!\n"
                     "* `time`: The time in which to send reminders. Format should be in 24-hour time (default is `9:00`).\n"
                     "* `timezone`: The IANA time zone for this bot to use (default is `UTC`).\n"
                     "* `prefix`: The command prefix used to invoke commands from this bot (default is `.`).\n\n"
                     "Examples:\n"
                     f"* Set the remind channel: `{p}config set channel #reminders`\n"
                     f"* Set the reminder time to 1pm: `{p}config set time 13:00`\n"
                     f"* Set the timezone to be in Los Angeles: `{p}config set timezone America/Los_Angeles`\n"
                     f"* Set the command prefix: `{p}config set prefix !`\n"
                     f"* View all configuration values: `{p}config get`")
            if len(args) == 1:
                reply = usage
            else:
                if args[1] == "get":
                    reply = ("Configuration values:\n"
                             f"* Reminder channel: <#{self.config.discord_remind_channel}>\n"
                             f"* Reminder time: {self.config.remind_time.strftime('%H:%M')}\n"
                             f"* Timezone: {self.config.timezone.key}")
                elif args[1] == "set":
                    if len(args) < 4:
                        reply = usage
                    elif args[2] == "channel":
                        if len(args[3]) > 3 and args[3][:2] == "<#" and args[3][-1] == ">":
                            try:
                                channel_id = int(args[3][2:-1])
                                channel = self.get_channel(channel_id)
                                if channel is None:
                                    reply = f"Channel not found: {channel_id}"
                                elif not isinstance(channel, discord.abc.Messageable):
                                    reply = f"Not able to send messages in this channel: <#{channel_id}>"
                                else:
                                    self.config.discord_remind_channel = channel_id
                                    self.config.save()
                                    LOGGER.info(f"Updated configuration field `discord_remind_channel` to `{self.config.discord_remind_channel}`")
                                    reply = f"Reminder channel successfully set to <#{channel_id}>"
                            except ValueError:
                                reply = f"Invalid channel ID: {args[3]}"
                        else:
                            reply = f"Invalid channel: {args[3]}"
                    elif args[2] == "time":
                        try:
                            self.config.remind_time = datetime.datetime.strptime(args[3], '%H:%M').time()
                            self.next_remind_dt = self.next_remind_dt.replace(hour=self.config.remind_time.hour,
                                                                              minute=self.config.remind_time.minute)
                            self.config.save()
                            LOGGER.info(f"Updated configuration field `remind_time` to `{self.config.remind_time}`")
                            reply = f"Reminder time successfully set to {self.config.remind_time.strftime('%H:%M')}"
                        except ValueError:
                            reply = f"Invalid format. Must be `HH:MM` (i.e. `9:00`, `14:00`): {args[3]}"
                    elif args[2] == "timezone":
                        try:
                            self.config.timezone = zoneinfo.ZoneInfo(args[3])
                            self.next_remind_dt = self.next_remind_dt.replace(tzinfo=self.config.timezone)
                            self.config.save()
                            LOGGER.info(f"Updated configuration field `timezone` to {self.config.timezone.key}")
                            reply = f"Timezone successfully set to {self.config.timezone.key}"
                        except zoneinfo.ZoneInfoNotFoundError:
                            reply = f"Timezone not found. Should be an IANA identifier (i.e. `America/New_York` or `Europe/London`): {args[3]}"
                    elif args[2] == "prefix":
                        self.config.command_prefix = args[3]
                        self.config.save()
                        LOGGER.info(f"Updated configuration field `command_prefix` to `{self.config.command_prefix}`")
                        reply = f"Command prefix now set to `{self.config.command_prefix}`."
                    else:
                        reply = f"Invalid configuration field. Must be `channel`, `time`, or `prefix`: {args[3]}"
                else:
                    reply = usage
        elif args[0] == "version":
            reply = f"Running version `{VERSION}` of Dailies Bot"
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
    intents.reactions = True
    client = DailiesClient(intents=intents)
    if client.config.discord_token == "":
        LOGGER.error("Shutting down client, must specify Discord token in `config.json`")
    else:
        LOGGER.info("Running Dailies Bot Discord client...")
        client.run(client.config.discord_token, log_handler=ROTATING_FILE_HANDLER, log_level=logging.DEBUG)


if __name__ == "__main__":
    run_bot()
