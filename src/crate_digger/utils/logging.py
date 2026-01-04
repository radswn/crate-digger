import logging
import sys

from crate_digger.constants import LOGGING_DATEFMT, LOGGING_FMT


def get_logger(name: str = "crate_digger") -> logging.Logger:
    """Create or retrieve a configured logger instance.

    Args:
        name: Logger name (default: 'crate_digger')

    Returns:
        Configured logger instance with StreamHandler
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if not logger.hasHandlers():
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            fmt=LOGGING_FMT, datefmt=LOGGING_DATEFMT, style="{"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """Return singular/plural word based on count."""

    return singular if count == 1 else (plural or f"{singular}s")
