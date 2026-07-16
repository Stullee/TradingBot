"""Entry point: `python -m tradingbot.main`"""
from __future__ import annotations

import asyncio
import logging
import os

from tradingbot.config import load_settings
from tradingbot.engine import TradingEngine
from tradingbot.logging_setup import setup_logging

log = logging.getLogger(__name__)


async def _amain() -> None:
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_dir)

    # Set by the add-on Dockerfile at build time (TRADINGBOT_VERSION mirrors
    # ha-addons/tradingbot/config.yaml's version; TRADINGBOT_COMMIT is the
    # exact pinned commit). Falls back to "dev"/"unknown" for a plain repo
    # checkout run outside the add-on image. Logged first and loudest so a
    # pasted log snippet is unambiguous about which build produced it --
    # rebuilds happen often enough during development that "which version
    # was this" is otherwise a real question every time.
    version = os.environ.get("TRADINGBOT_VERSION", "dev")
    commit = os.environ.get("TRADINGBOT_COMMIT", "unknown")
    log.info("=== TradingBot version=%s commit=%s ===", version, commit)

    log.info(
        "Starting TradingBot | mode=%s | symbols=%s",
        "PAPER" if settings.is_paper else "LIVE",
        settings.symbol_list,
    )
    if not settings.is_paper:
        log.warning(
            "*** LIVE TRADING MODE - REAL MONEY WILL BE AT RISK. "
            "Make sure you have thoroughly validated this bot on paper trading first. ***"
        )

    engine = TradingEngine(settings)
    try:
        await engine.run_forever()
    except KeyboardInterrupt:
        log.info("Shutdown requested by user.")


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
