"""Position sizing and loss limits. Pure logic plus optional JSON state
persistence -- no IB dependency -> unit testable.

Two latched kill switches, each with its own baseline:
  - daily: loss since the UTC day's starting equity >= max_daily_loss_pct
  - weekly: loss since the ISO week's starting equity >= max_weekly_loss_pct
    (0 disables). Catches the "lose the daily limit every single day"
    failure mode a purely daily limit can't -- five straight -2% days is
    -10% with nothing tripping.

State (baselines + latched switches) is persisted to a JSON file when
enabled, so a mid-day restart can't silently hand the bot a fresh loss
budget or forget a tripped kill switch -- previously a crash-looping
process under a supervisor could re-trade the worst day of the year over
and over, resetting its baseline to the already-depleted equity each time.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def floor_to_increment(value: float, increment: float) -> float:
    """Largest multiple of `increment` <= value, cleaned of float dust
    (e.g. floor_to_increment(0.37, 0.1) -> 0.3, not 0.30000000000000004)."""
    if increment <= 0:
        return value
    steps = math.floor(value / increment + 1e-9)
    # Number of decimals the increment itself needs, so the result rounds
    # to an exact multiple instead of accumulating binary-float error.
    text = f"{increment:.10f}".rstrip("0")
    decimals = len(text.split(".")[1]) if "." in text else 0
    return round(steps * increment, decimals)


class RiskManager:
    def __init__(
        self,
        risk_per_trade_pct: float,
        max_daily_loss_pct: float,
        max_concurrent_positions: int,
        max_position_pct: float,
        max_weekly_loss_pct: float = 0.0,
        max_open_risk_pct: float = 0.0,
    ):
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_concurrent_positions = max_concurrent_positions
        self.max_position_pct = max_position_pct
        # 0 disables either limit.
        self.max_weekly_loss_pct = max_weekly_loss_pct
        self.max_open_risk_pct = max_open_risk_pct

        self._starting_equity: float | None = None
        self._kill_switch_active = False
        self._trading_date: date | None = None
        self._week_key: str | None = None
        self._week_start_equity: float | None = None
        self._weekly_kill_active = False
        self._state_path: Path | None = None

    # --- persistence -------------------------------------------------------
    def enable_persistence(self, path: Path) -> None:
        """Opt-in (the live engine only): baselines and latched kill switches
        get written to `path` and can be restored by start_or_restore_session.
        Short-lived tools (report, backtester) never call this, mirroring
        BrokerConnection.enable_realized_pnl_persistence's reasoning."""
        self._state_path = path

    def _write_state(self) -> None:
        if self._state_path is None or self._starting_equity is None:
            return
        state = {
            "date": str(self._trading_date) if self._trading_date else None,
            "starting_equity": self._starting_equity,
            "kill_switch_active": self._kill_switch_active,
            "week": self._week_key,
            "week_start_equity": self._week_start_equity,
            "weekly_kill_active": self._weekly_kill_active,
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state))
        except OSError:
            log.warning("Could not persist risk state to %s", self._state_path)

    def _read_state(self) -> dict | None:
        if self._state_path is None or not self._state_path.exists():
            return None
        try:
            return json.loads(self._state_path.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read %s, starting risk session fresh.", self._state_path)
            return None

    # --- daily/weekly lifecycle -------------------------------------------
    def start_or_restore_session(self, equity: float, today: date | None = None) -> None:
        """Like start_new_session, but if persisted state exists for the same
        UTC day (and/or ISO week), restores that baseline and any latched
        kill switch instead of re-latching from current (possibly already
        depleted) equity -- a restart must not grant a fresh loss budget."""
        today = today or datetime.now(timezone.utc).date()
        week = _week_key(today)
        state = self._read_state()

        if state and state.get("date") == str(today) and state.get("starting_equity"):
            self._trading_date = today
            self._starting_equity = float(state["starting_equity"])
            self._kill_switch_active = bool(state.get("kill_switch_active", False))
            log.info(
                "Risk manager: restored today's session (starting equity=%.2f, "
                "daily kill switch=%s) from a previous run.",
                self._starting_equity,
                self._kill_switch_active,
            )
        else:
            self._trading_date = today
            self._starting_equity = equity
            self._kill_switch_active = False
            log.info("Risk manager: new trading session started, starting equity=%.2f", equity)

        if state and state.get("week") == week and state.get("week_start_equity"):
            self._week_key = week
            self._week_start_equity = float(state["week_start_equity"])
            self._weekly_kill_active = bool(state.get("weekly_kill_active", False))
        else:
            self._week_key = week
            self._week_start_equity = equity
            self._weekly_kill_active = False
        self._write_state()

    def start_new_session(self, equity: float, today: date | None = None) -> None:
        """Rolls the daily baseline (clearing the daily kill switch), and the
        weekly baseline too if `today` starts a new ISO week."""
        today = today or datetime.now(timezone.utc).date()
        self._trading_date = today
        self._starting_equity = equity
        self._kill_switch_active = False

        week = _week_key(today)
        if week != self._week_key:
            self._week_key = week
            self._week_start_equity = equity
            self._weekly_kill_active = False
            log.info("Risk manager: new ISO week (%s), weekly baseline=%.2f", week, equity)
        elif self._week_start_equity is None:
            self._week_key = week
            self._week_start_equity = equity

        log.info("Risk manager: new trading session started, starting equity=%.2f", equity)
        self._write_state()

    @property
    def kill_switch_active(self) -> bool:
        """True when either the daily or the weekly limit is latched."""
        return self._kill_switch_active or self._weekly_kill_active

    @property
    def daily_kill_switch_active(self) -> bool:
        return self._kill_switch_active

    @property
    def weekly_kill_switch_active(self) -> bool:
        return self._weekly_kill_active

    def check_loss_limits(self, current_equity: float) -> bool:
        """Returns True (latching the corresponding kill switch) if the daily
        or weekly loss limit is breached."""
        if self._starting_equity is None:
            raise RuntimeError("start_new_session() must be called before risk checks")

        if self._starting_equity > 0:
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
                    self._write_state()

        if (
            self.max_weekly_loss_pct > 0
            and self._week_start_equity
            and self._week_start_equity > 0
        ):
            weekly_loss_pct = (
                (self._week_start_equity - current_equity) / self._week_start_equity * 100
            )
            if weekly_loss_pct >= self.max_weekly_loss_pct:
                if not self._weekly_kill_active:
                    log.error(
                        "WEEKLY LOSS LIMIT BREACHED: -%.2f%% since week start >= %.2f%% "
                        "limit. Weekly kill switch activated until the next ISO week: "
                        "no new entries, flattening positions.",
                        weekly_loss_pct,
                        self.max_weekly_loss_pct,
                    )
                    self._weekly_kill_active = True
                    self._write_state()

        return self.kill_switch_active

    # --- pre-trade checks --------------------------------------------------
    def can_open_new_position(
        self, open_position_count: int, open_risk_pct: float | None = None
    ) -> bool:
        """`open_risk_pct` is the summed entry-to-stop risk of all currently
        open (or in-flight) positions as a % of equity -- the portfolio
        "heat". When provided and max_open_risk_pct is set, a new position is
        only allowed if its own risk_per_trade_pct still fits under the cap:
        the concurrent-positions count alone lets 10 x 0.5% = 5% of equity be
        at risk at once, all of it typically correlated."""
        if self.kill_switch_active:
            return False
        if open_position_count >= self.max_concurrent_positions:
            return False
        if (
            self.max_open_risk_pct > 0
            and open_risk_pct is not None
            and open_risk_pct + self.risk_per_trade_pct > self.max_open_risk_pct + 1e-9
        ):
            return False
        return True

    def position_size(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
        min_size_increment: float = 1.0,
        min_quantity: float = 0.0,
    ) -> float:
        """Quantity such that a stop-out risks ~risk_per_trade_pct of equity,
        capped so the position's notional never exceeds max_position_pct of
        equity, floored to a multiple of `min_size_increment` (1.0 = whole
        shares; fractional increments enable crypto sizing, exchange lot
        sizes enable e.g. Hong Kong board lots). Returns 0 when the result
        would fall below `min_quantity` (venue minimum order size)."""
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0 or entry_price <= 0:
            return 0.0

        risk_budget = equity * (self.risk_per_trade_pct / 100)
        shares_by_risk = floor_to_increment(risk_budget / risk_per_share, min_size_increment)

        max_notional = equity * (self.max_position_pct / 100)
        shares_by_notional = floor_to_increment(max_notional / entry_price, min_size_increment)

        quantity = max(0.0, min(shares_by_risk, shares_by_notional))
        if min_quantity > 0 and quantity < min_quantity:
            return 0.0
        return quantity
