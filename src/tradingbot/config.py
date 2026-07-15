"""Typed application configuration loaded from environment / .env file."""
from __future__ import annotations

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LIVE_TRADING_PORTS = {7496, 4001}
PAPER_TRADING_PORTS = {7497, 4002}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Connection
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497
    ib_client_id: int = 17
    ib_account_id: str = ""
    allow_live_trading: bool = False

    # Universe
    symbols: str = "AAPL,MSFT,NVDA"

    # Strategy
    bar_size: str = "5 mins"
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_long_min: float = 50
    rsi_long_max: float = 70
    rsi_short_min: float = 30
    rsi_short_max: float = 50
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    target_atr_mult: float = 2.5
    allow_shorting: bool = False

    # Risk
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_concurrent_positions: int = 3
    max_position_pct: float = 20.0

    # Trading hours (US/Eastern, "HH:MM")
    market_open: str = "09:30"
    market_close: str = "16:00"
    no_new_entries_before_close_min: int = 15
    flatten_before_close_min: int = 5

    log_level: str = "INFO"

    @field_validator("symbols")
    @classmethod
    def _symbols_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("SYMBOLS must contain at least one ticker")
        return v

    @model_validator(mode="after")
    def _guard_live_trading(self) -> "Settings":
        if self.ib_port in LIVE_TRADING_PORTS and not self.allow_live_trading:
            raise ValueError(
                f"IB_PORT={self.ib_port} looks like a LIVE trading port. "
                "Set ALLOW_LIVE_TRADING=true explicitly if this is intentional. "
                "Refusing to start to avoid accidentally trading real money."
            )
        return self

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]

    @property
    def is_paper(self) -> bool:
        return self.ib_port in PAPER_TRADING_PORTS


def load_settings() -> Settings:
    return Settings()
