"""Prints an end-of-day summary using this add-on's already-configured
options -- same config the live engine uses, no separate .env needed. The
live engine also emits one automatically at every UTC day rollover (to
logs/eod_reports.jsonl, the log, and the alert webhook); this entrypoint is
for re-printing a day on demand:

    docker exec -it addon_local_tradingbot python /app/addon_eod_entrypoint.py
    docker exec -it addon_local_tradingbot python /app/addon_eod_entrypoint.py 2026-07-20

(container name may differ -- check `docker ps`.) Reads only the persisted
journal files; never connects to IB, never places orders."""
from __future__ import annotations

from _addon_options import apply_options_as_env

if __name__ == "__main__":
    apply_options_as_env()

    from tradingbot.eod import main

    main()
