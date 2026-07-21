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
        # Default is 60s. A 60-day/5-min pull is a heavy query for IB's
        # history farm -- during active US market hours it routinely takes
        # longer than that and the client cancels it (Error 162: "API
        # historical data query cancelled") even though IB would have
        # delivered the data. 0 disables the client-side timeout entirely
        # (IB's own farm-side limits still apply), which is what backtests
        # -- unlike live trading -- can afford to wait on.
        timeout=0,
    )
    return bars_to_dataframe(bars)
