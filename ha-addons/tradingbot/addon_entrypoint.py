"""Home Assistant add-on entrypoint. Translates the add-on's /data/options.json
(filled in via the HA UI configuration form) into the environment variables
tradingbot.config.Settings reads, points logging at the add-on's persistent
/data directory, then runs the exact same bot as everywhere else."""
from __future__ import annotations

import json
import os
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")


def _apply_options_as_env() -> None:
    if not OPTIONS_PATH.exists():
        return
    options = json.loads(OPTIONS_PATH.read_text())
    for key, value in options.items():
        if value is None:
            continue
        os.environ[key.upper()] = str(value)


if __name__ == "__main__":
    _apply_options_as_env()
    os.environ.setdefault("LOG_DIR", "/data/logs")

    from tradingbot.main import main

    main()
