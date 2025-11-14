import json
import pathlib
import random

from dailies.logger import LOGGER

DATE_FORMAT = "%Y/%m/%d"
TIME_FORMAT = "%H:%M:%S"
TIMEZONE_FORMAT = "%z"
DATETIME_FORMAT = "%Y/%m/%dT%H:%M:%S%z"
VERSION = "0.1.4-dev"

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