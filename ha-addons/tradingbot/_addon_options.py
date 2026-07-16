"""Shared helper: translates the add-on's /data/options.json (filled in via
the HA UI configuration form) into the environment variables
tradingbot.config.Settings reads. Used by both the live entrypoint and the
backtest entrypoint so a one-off `docker exec` into the running add-on
container picks up the exact same SYMBOLS/MARKETS/STRATEGY/risk config as
the live engine, without needing a separate .env file."""
from __future__ import annotations

import json
import os
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")


def apply_options_as_env() -> None:
    if not OPTIONS_PATH.exists():
        return
    options = json.loads(OPTIONS_PATH.read_text())
    for key, value in options.items():
        if value is None:
            continue
        os.environ[key.upper()] = str(value)
