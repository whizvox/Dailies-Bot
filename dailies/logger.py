import logging
import sys
from logging.handlers import RotatingFileHandler

FORMATTER = logging.Formatter("[{asctime}] [{levelname:<8}] {name}: {message}", datefmt="%Y-%m-%d %H:%M:%S", style="{")
LOGGER = logging.Logger("dailies-bot", logging.DEBUG)
ROTATING_FILE_HANDLER = RotatingFileHandler(filename="log.txt", maxBytes=1000000, backupCount=10, encoding="utf-8")
STREAM_HANDLER = logging.StreamHandler(sys.stdout)
STREAM_HANDLER.setFormatter(FORMATTER)
LOGGER.addHandler(ROTATING_FILE_HANDLER)
LOGGER.addHandler(STREAM_HANDLER)
