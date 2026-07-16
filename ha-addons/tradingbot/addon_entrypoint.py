"""Home Assistant add-on entrypoint. Translates the add-on's /data/options.json
(filled in via the HA UI configuration form) into the environment variables
tradingbot.config.Settings reads, points logging at the add-on's persistent
/data directory, then runs the exact same bot as everywhere else."""
from __future__ import annotations

import os

from _addon_options import apply_options_as_env

if __name__ == "__main__":
    apply_options_as_env()
    os.environ.setdefault("LOG_DIR", "/data/logs")

    from tradingbot.main import main

    main()
