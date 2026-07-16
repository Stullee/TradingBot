"""Fetches longer-duration historical bars from IB for backtesting -- unlike
data/bars.py's live 1-day duration used for real trading."""
from __future__ import annotations

import pandas as pd
from ib_async import IB, Contract

from tradingbot.data.bars import bars_to_dataframe


async def fetch_history(
    ib: IB,
    contract: Contract,
    bar_size: str,
    duration: str,
    use_rth: bool,
    what_to_show: str,
) -> pd.DataFrame:
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow=what_to_show,
        useRTH=use_rth,
        formatDate=2,
        keepUpToDate=False,
    )
    return bars_to_dataframe(bars)
