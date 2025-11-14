import datetime

from dailies.chore import Chore, ChoreParseException
from dailies.logger import LOGGER


def parse_chore_from_line(args: list[str]) -> Chore:
    LOGGER.debug(f"Parsing chore from arguments: {' '.join(args)}")
    chore = Chore()
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
