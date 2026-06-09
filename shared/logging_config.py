"""Structured logging setup for all bots."""

import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Server runs UTC. Operators and ERP are in Vietnam (Asia/Ho_Chi_Minh, UTC+7).
# Stamp logs in local time so timestamps match wall-clock when grepping during
# incidents. Use a fixed offset (not zoneinfo) — Vietnam has no DST.
LOG_TZ = timezone(timedelta(hours=7))


def _tz_converter(timestamp):
    """logging.Formatter.converter hook — returns time.struct_time in UTC+7."""
    return datetime.fromtimestamp(timestamp, tz=LOG_TZ).timetuple()


class _FlushHandler(logging.StreamHandler):
    """StreamHandler that flushes after every emit (for nohup/file redirect)."""
    def emit(self, record):
        super().emit(record)
        self.flush()


def setup_logger(name: str, log_file: str = None) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "[%(asctime)s][%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fmt.converter = _tz_converter

    # stdout handler — flushes immediately so nohup logs appear in real-time
    stream_handler = _FlushHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    # Optional file handler
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger
