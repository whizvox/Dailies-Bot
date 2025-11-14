import calendar
import datetime
import json
import logging
import pathlib
import random
import sys
from logging import Formatter
from logging.handlers import RotatingFileHandler
from typing import override

import discord
from discord.ext import tasks

DATE_FORMAT = "%Y/%m/%d"
TIME_FORMAT = "%H:%M:%S"
TIMEZONE_FORMAT = "%z"
DATETIME_FORMAT = "%Y/%m/%dT%H:%M:%S%z"
VERSION = "0.1.3-dev"
LOGGER = logging.Logger("dailies-bot", logging.DEBUG)

def add_months(year: int, month: int, delta_months: int) -> tuple[int, int]:
    new_month = (month - 1 + delta_months) % 12 + 1
    new_year = year + (month - 1 + delta_months) // 12
    return new_year, new_month


def get_monthday(year: int, month: int, delta: int) -> int:
    if delta > 0:
        return delta
    _, total_month_days = calendar.monthrange(year, month)
    return total_month_days + delta

class Chore:
    title: str = ""
    interval: int | None = None
    unit: str | None = None
    weekday: str | None = None
    monthdays: int = 0
    date: datetime.date | None = None
    user: int = 0

    def __repr__(self):
        return (f"Chore(title={self.title}, interval={self.interval}, unit={self.unit}, weekday={self.weekday}, "
                f"monthdays={self.monthdays}, date={self.date}, user={self.user})")

    def __str__(self):
        return self.__repr__()

    def to_json(self) -> dict:
        return {"title": self.title, "interval": self.interval, "unit": self.unit, "weekday": self.weekday,
                "monthdays": self.monthdays, "date": None if self.date is None else self.date.strftime(DATE_FORMAT), "user": self.user}

    def get_weekday_index(self):
        if self.weekday == "m":
            return 0
        elif self.weekday == "t":
            return 1
        elif self.weekday == "w":
            return 2
        elif self.weekday == "r":
            return 3
        elif self.weekday == "f":
            return 4
        elif self.weekday == "s":
            return 5
        elif self.weekday == "u":
            return 6
        return -1

    def format_interval(self) -> str | None:
        if self.interval is None or self.unit is None:
            return None
        if self.unit == "d":
            unit = "day"
        elif self.unit == "w":
            unit = "week"
        elif self.unit == "m":
            unit = "month"
        else:
            return None
        if self.interval == 1:
            return unit
        return f"{self.interval} {unit}s"

    def calculate_next_date(self, sched_date: datetime.date=None) -> datetime.date:
        if self.date is not None:
            return self.date
        now = datetime.datetime.now().date()
        if self.unit == "d":
            if sched_date is None:
                return now + datetime.timedelta(days=1)
            else:
                diff = now - sched_date
                return now + datetime.timedelta(days=max(self.interval - diff.days, 1))
        elif self.unit == "w":
            weekday = self.get_weekday_index()
            if weekday == -1:
                raise Exception("Invalid weekday set: " + self.weekday)
            curr_weekday = now.weekday()
            if sched_date is not None:
                # if previous scheduled date has a different weekday, go backwards until we find the correct weekday
                if sched_date.weekday() > weekday:
                    sched_date = sched_date - datetime.timedelta(days=sched_date.weekday() - weekday)
                elif sched_date.weekday() < weekday:
                    sched_date = sched_date - datetime.timedelta(days=7 - (weekday - sched_date.weekday()))
                result = sched_date + datetime.timedelta(days=self.interval * 7)
                if (result - now).days > 0:
                    return result
                # if the new date is today or in the past, fallback to scheduling on the next weekday
            if curr_weekday < weekday:
                return now + datetime.timedelta(days=weekday - curr_weekday)
            else:
                return now + datetime.timedelta(days=7 + (weekday - curr_weekday))
        elif self.unit == "m":
            day = get_monthday(now.year, now.month, self.monthdays)
            if sched_date is not None:
                year = sched_date.year
                month = sched_date.month
                if sched_date.day < day:
                    year, month = add_months(year, month, -1)
                year, month = add_months(year, month, self.interval)
                day = get_monthday(year, month, self.monthdays)
                result = datetime.date(year, month, day)
                if (result - now).days > 0:
                    return result
            if now.day < day:
                return datetime.date(now.year, now.month, day)
            else:
                year, month = add_months(now.year, now.month, 1)
                day = get_monthday(year, month, self.monthdays)
                return datetime.date(year, month, day)
        else:
            raise Exception(f"Invalid units: {self.unit}")


def parse_chore_from_json(obj: dict) -> Chore:
    chore = Chore()
    chore.title = obj["title"]
    chore.interval = obj["interval"]
    chore.unit = obj["unit"]
    chore.weekday = obj["weekday"]
    chore.monthdays = obj["monthdays"]
    if "date" in obj and obj["date"] is not None:
        chore.date = datetime.datetime.strptime(obj["date"], DATE_FORMAT).date()
    chore.user = obj["user"]
    return chore


class ChoreParseException(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def parse_chore_from_line(line: str) -> Chore:
    LOGGER.debug(f"Parsing chore from line: {line}")
    chore = Chore()
    args = line.split(" ")
    chore.title = args[0]
    n = 1
    if len(args[0]) > 0 and args[0][0] == '"':
        chore.title = args[0][1:]
        end_str = False
        for word in args[1:]:
            n += 1
            if len(word) > 0:
                if word[-1] == '"':
                    chore.title += " " + word[:-1]
                    end_str = True
                    break
                else:
                    chore.title += " " + word
        if not end_str:
            raise ChoreParseException("Invalid chore title, must close string with another double-quote (\")")
    if len(args) < n + 3:
        raise ChoreParseException("Missing arguments, must specify title, user, and time")
    user_str = args[n]
    if len(user_str) < 4 or user_str[:2] != "<@" or user_str[-1] != ">":
        raise ChoreParseException(f"Invalid user: {user_str}")
    try:
        chore.user = int(user_str[2:-1])
    except:
        raise ChoreParseException(f"Invalid user ID: {user_str[2:-1]}")
    if args[n + 1] == "every":
        duration_str = args[n + 2]
        if duration_str[-1] in ["d", "w", "m"]:
            chore.unit = duration_str[-1]
        else:
            raise ChoreParseException(f"Invalid duration, must end in `d`, `w`, or `m`: {duration_str}")
        try:
            chore.interval = int(duration_str[:-1])
        except:
            raise ChoreParseException(f"Invalid duration, must begin with an integer: {duration_str}")
        if chore.unit != "d" and len(args) < n + 4:
            if chore.unit == "w":
                raise ChoreParseException("Must specify weekday (i.e. `monday`, `friday`)")
            else:
                raise ChoreParseException("Must specify number of days into month (i.e. `1`, `-3`)")
        if chore.unit == "w":
            if args[n + 3].lower() not in ["sunday", "u", "monday", "m", "tuesday", "t", "wednesday", "w", "thursday", "r", "friday", "f", "saturday", "s"]:
                raise ChoreParseException(f"Invalid weekday: {args[n + 3]}")
            chore.weekday = args[n + 3].lower()
            if chore.weekday == "sunday":
                chore.weekday = "u"
            elif chore.weekday == "thursday":
                chore.weekday = "r"
            elif len(chore.weekday) > 1:
                chore.weekday = chore.weekday[0]
        elif chore.unit == "m":
            try:
                chore.monthdays = int(args[n + 3])
                if abs(chore.monthdays) > 20:
                    raise ChoreParseException(f"Invalid month days, must be within [-20, 20]: {chore.monthdays}")
            except:
                raise ChoreParseException(f"Invalid month day, must be an integer: {args[n + 3]}")
    elif args[n + 1] == "on":
        valid_formats = ["%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"]
        for date_format in valid_formats:
            try:
                chore.date = datetime.datetime.strptime(args[n + 2], date_format).date()
                diff = chore.date - datetime.datetime.now().date()
                if diff.days <= 0:
                    raise ChoreParseException(f"Invalid date, must occur after today: {args[n + 2]}")
                break
            except ValueError:
                pass
        if chore.date is None:
            raise ChoreParseException(f"Invalid date, must be in `yyyy/mm/dd` or `mm/dd/yyyy` format: {args[n + 2]}")
    else:
        raise ChoreParseException("Invalid argument, must be 'every' or 'on': " + args[n])
    return chore


def random_sequence(length: int=6) -> str:
    result = ""
    for i in range(length):
        result += str(random.randint(0, 9))
    return result


class SerializableFile:
    def __init__(self, default_file_name: str):
        self._default_file_name = default_file_name

    def serialize(self) -> dict:
        raise NotImplementedError()

    def deserialize(self, obj: dict):
        raise NotImplementedError()

    def save(self, file_name: str | None=None):
        if file_name is None:
            file_name = self._default_file_name
        with open(file_name, "w", encoding="utf-8") as file:
            obj = self.serialize()
            json.dump(obj, file, indent=4)

    def load(self, file_name: str | None=None):
        if file_name is None:
            file_name = self._default_file_name
        path = pathlib.Path(file_name)
        if path.exists():
            try:
                with open(file_name, "r", encoding="utf-8") as file:
                    obj = json.load(file)
                    self.deserialize(obj)
            except Exception as e:
                if "." in file_name:
                    ext_index = file_name.rindex(".")
                    target = file_name[0:ext_index] + "_" + random_sequence() + file_name[ext_index:]
                else:
                    target = file_name + "_" + random_sequence()
                path.rename(target)
                self.save(file_name)
                LOGGER.warning(f"ERROR: Could not load `{file_name}`. Saved malformed file to `{target}` and using default values.")
                LOGGER.warning(e)
        else:
            self.save(file_name)


class DailiesConfig(SerializableFile):
    discord_token: str = ""
    discord_remind_channel = 0
    remind_time: datetime.time = datetime.time(hour=9)
    timezone: datetime.tzinfo | None = None

    def __init__(self):
        super().__init__("config.json")

    @override
    def serialize(self) -> dict:
        return {
            "discord_token": self.discord_token,
            "discord_remind_channel": self.discord_remind_channel,
            "remind_time": self.remind_time.strftime(TIME_FORMAT),
            "timezone": None if self.timezone is None else datetime.time(tzinfo=self.timezone).strftime(TIMEZONE_FORMAT)
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
        if message.author.id == self.user.id or not message.content.startswith("."):
            return
        LOGGER.debug(f"Attempting to parse message from {message.author}: {message.content}")
        args = message.content[1:].split(maxsplit=1)
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
                reply = ("Every *N* days: `.add <title> <user> every <days>d` (ex. `.add \"Do the dishes\" @user every 2d`)\n"
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
    formatter = Formatter("[{asctime}] [{levelname:<8}] {name}: {message}", datefmt="%Y-%m-%d %H:%M:%S", style="{")
    rot_file_handler = RotatingFileHandler(filename="log.txt", maxBytes=1000000, backupCount=10, encoding="utf-8")
    rot_file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(rot_file_handler)
    LOGGER.addHandler(stream_handler)
    intents = discord.Intents.default()
    intents.message_content = True
    client = DailiesClient(intents=intents)
    if client.config.discord_token == "" or client.config.discord_remind_channel == 0:
        LOGGER.error("Shutting down client, must specify Discord token and channel ID in `config.json`")
        print("Must specify Discord token and channel ID in `config.json`")
    else:
        LOGGER.info("Running Dailies Bot Discord client...")
        client.run(client.config.discord_token, log_handler=rot_file_handler, log_level=logging.DEBUG)


if __name__ == "__main__":
    run_bot()
