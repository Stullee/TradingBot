"""Runs the backtester using this add-on's already-configured options --
same SYMBOLS/MARKETS/STRATEGY/risk config the live engine uses, no separate
.env needed. Meant to be run one-off inside the running add-on container:

    docker exec -it addon_local_tradingbot python addon_backtest_entrypoint.py

(container name may differ -- check `docker ps`; on HAOS it's usually
`addon_<repo-slug>_tradingbot` or similar). This never places orders."""
from __future__ import annotations

from _addon_options import apply_options_as_env

if __name__ == "__main__":
    apply_options_as_env()

    from tradingbot.backtest.runner import main

    main()
