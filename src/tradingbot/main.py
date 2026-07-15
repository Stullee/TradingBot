"""Entry point: `python -m tradingbot.main`"""
from __future__ import annotations

import asyncio
import logging

from tradingbot.config import load_settings
from tradingbot.engine import TradingEngine
from tradingbot.logging_setup import setup_logging

log = logging.getLogger(__name__)


async def _amain() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)

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
