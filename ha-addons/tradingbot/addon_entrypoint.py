"""Home Assistant add-on entrypoint. Translates the add-on's /data/options.json
(filled in via the HA UI configuration form) into the environment variables
tradingbot.config.Settings reads (including LOG_DIR, pointed at the mapped
/config share), then runs the exact same bot as everywhere else."""
from __future__ import annotations

from _addon_options import apply_options_as_env

if __name__ == "__main__":
    apply_options_as_env()

    from tradingbot.main import main

    main()
