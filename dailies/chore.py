import calendar
import datetime

from dailies.util import DATE_FORMAT


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