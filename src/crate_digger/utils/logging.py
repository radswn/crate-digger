import logging
import sys

from crate_digger.constants import LOGGING_DATEFMT, LOGGING_FMT


def get_logger(name: str = "crate_digger") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if not logger.hasHandlers():
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            fmt=LOGGING_FMT,
            datefmt=LOGGING_DATEFMT,
            style="{"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger
