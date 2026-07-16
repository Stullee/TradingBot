"""Console + rotating file logging setup."""
from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s UTC | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # logging.Formatter defaults to the container's local system time for
    # %(asctime)s, not UTC -- but every timestamp elsewhere in these logs
    # (bar dates, market-hours checks) is UTC or an explicit timezone. Force
    # UTC here too so log lines are never silently comparing two different
    # timezones against each other (this caused a real ~2h "crypto data is
    # delayed" misdiagnosis when the container's local tz wasn't UTC).
    fmt.converter = time.gmtime

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        Path(log_dir) / "tradingbot.log", maxBytes=5_000_000, backupCount=5
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
