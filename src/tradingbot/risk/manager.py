"""Position sizing and daily risk limits. Pure logic, no IB dependency -> unit testable."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(
        self,
        risk_per_trade_pct: float,
        max_daily_loss_pct: float,
        max_concurrent_positions: int,
        max_position_pct: float,
    ):
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_concurrent_positions = max_concurrent_positions
        self.max_position_pct = max_position_pct

        self._starting_equity: float | None = None
        self._kill_switch_active = False

    # --- daily lifecycle -------------------------------------------------
    def start_new_session(self, equity: float) -> None:
        self._starting_equity = equity
        self._kill_switch_active = False
        log.info("Risk manager: new trading session started, starting equity=%.2f", equity)

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    def check_daily_loss_limit(self, current_equity: float) -> bool:
        """Returns True (and latches the kill switch) if the daily loss limit is breached."""
        if self._starting_equity is None:
            raise RuntimeError("start_new_session() must be called before risk checks")
        if self._starting_equity <= 0:
            return self._kill_switch_active

        loss_pct = (self._starting_equity - current_equity) / self._starting_equity * 100
        if loss_pct >= self.max_daily_loss_pct:
            if not self._kill_switch_active:
                log.error(
                    "DAILY LOSS LIMIT BREACHED: -%.2f%% >= %.2f%% limit. "
                    "Kill switch activated: no new entries, flattening positions.",
                    loss_pct,
                    self.max_daily_loss_pct,
                )
            self._kill_switch_active = True
        return self._kill_switch_active

    # --- pre-trade checks --------------------------------------------------
    def can_open_new_position(self, open_position_count: int) -> bool:
        if self._kill_switch_active:
            return False
        return open_position_count < self.max_concurrent_positions

    def position_size(self, equity: float, entry_price: float, stop_price: float) -> int:
        """Number of shares such that a stop-out risks ~risk_per_trade_pct of equity,
        capped so the position's notional never exceeds max_position_pct of equity."""
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0 or entry_price <= 0:
            return 0

        risk_budget = equity * (self.risk_per_trade_pct / 100)
        shares_by_risk = int(risk_budget // risk_per_share)

        max_notional = equity * (self.max_position_pct / 100)
        shares_by_notional = int(max_notional // entry_price)

        return max(0, min(shares_by_risk, shares_by_notional))
