"""Typed application configuration loaded from environment / .env file."""
from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tradingbot.symbols import SymbolSpec, parse_markets_dsl, symbols_to_markets_dsl

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
    # Multi-market DSL, e.g. "US:AAPL,MSFT;EU:SAP.DE;ASIA:0700.HK;CRYPTO:BTC,ETH".
    # Takes priority over `symbols` when set; see tradingbot.symbols for the
    # exact format. Leave empty to keep using `symbols` (all assigned to US).
    markets: str = ""

    # Strategy
    # "vwap_mean_reversion" (default): fades price back toward VWAP, fires
    #   often -- many small trades a day, suited to range-bound conditions.
    # "ema_rsi_momentum": trend-following EMA crossover, fires rarely (only
    #   at genuine trend turns) -- see strategy/*.py docstrings for details.
    # "trendline_breakout": chases a confirmed rise/fall (regression-fit
    #   trendline break) instead of fading it -- unvalidated live, shadow-test
    #   before trusting it with capital.
    strategy: str = "vwap_mean_reversion"
    bar_size: str = "5 mins"
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_long_min: float = 50
    rsi_long_max: float = 70
    rsi_short_min: float = 30
    rsi_short_max: float = 50
    rsi_oversold: float = 30
    rsi_overbought: float = 70
    vwap_dist_atr_mult: float = 0.5
    # Trend filter for vwap_mean_reversion (ignored by ema_rsi_momentum):
    # blocks LONG entries while price is below this EMA (a downtrend) and
    # SHORT entries while price is above it (an uptrend) -- keeps the
    # "buy dips / sell rips" mean-reversion timing but only with the
    # prevailing trend, not against it. Set to 0 to disable (old
    # unconditional countertrend behavior).
    trend_ema_period: int = 50
    # trendline_breakout only: rolling window (in bars) the regression
    # trendline is fit over, and the minimum R-squared of that fit required
    # to treat it as a genuine trend (vs. a noisy zigzag that nets upward
    # over the window by chance).
    trend_window: int = 20
    min_r_squared: float = 0.7
    # trendline_breakout only: on the first bar of a session with a valid
    # fit, allow an entry without a fresh cross if price is beyond the line
    # by at most this many ATRs -- a trend established during the session
    # warmup would otherwise never be entered (its crossing bar happened
    # while signals were still disabled). 0 = pure crossing entries only.
    trend_entry_max_dist_atr: float = 1.0
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    target_atr_mult: float = 2.5
    allow_shorting: bool = False

    # Risk
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    # Weekly drawdown breaker: loss since the ISO week's starting equity that
    # latches a kill switch until the next week. Catches "lose the daily
    # limit every single day" -- a purely daily limit never trips on five
    # straight -2% days. 0 disables.
    max_weekly_loss_pct: float = 5.0
    max_concurrent_positions: int = 10
    max_position_pct: float = 20.0
    # Portfolio heat cap: total entry-to-stop risk of all open positions as a
    # % of equity. The concurrent-positions count alone allows e.g.
    # 10 x 0.5% = 5% of equity at risk at once, usually correlated; this
    # bounds the sum directly. 0 disables.
    max_open_risk_pct: float = 3.0

    # Trading hours: each market's own session times come from the built-in
    # presets in tradingbot.markets (not configurable here, to keep the
    # option surface manageable -- edit that file for a different exchange).
    # These buffer minutes apply the same way to every market's own close.
    no_new_entries_before_close_min: int = 15
    flatten_before_close_min: int = 5
    # No entries during the first N minutes after a market's open: the
    # opening auction and first bars have the widest spreads and an
    # ATR/VWAP that hasn't settled into the day's regime. 0 disables.
    no_entries_after_open_min: int = 15

    log_level: str = "INFO"
    log_dir: str = "logs"

    # Optional webhook POSTed on events needing prompt human attention (kill
    # switch trips, a position found without its stop, critical order
    # rejections). Works with a Home Assistant webhook trigger, ntfy, etc.
    # Empty disables. See tradingbot.alerts.
    alert_webhook_url: str = ""

    # Backtesting (see tradingbot.backtest.runner). IB duration string, e.g.
    # "30 D", "6 M", "1 Y" -- how far back to pull historical bars.
    backtest_duration: str = "60 D"
    # Cost model applied per simulated trade so backtest expectancy isn't
    # fiction: commission per share/unit per side (IB fixed US pricing is
    # $0.005/share, min $1 -- the minimum isn't modeled), and slippage in
    # basis points of price applied adversely to each market-order fill
    # (entries, stop exits, signal exits -- not the limit take-profit).
    backtest_commission_per_share: float = 0.005
    backtest_slippage_bps: float = 1.0

    # News sentiment shadow-trading (observation only, never places real orders)
    enable_news_monitor: bool = False
    finnhub_api_key: str = ""
    anthropic_api_key: str = ""
    news_model: str = "claude-haiku-4-5-20251001"
    news_confidence_threshold: float = 0.6
    news_poll_interval_sec: int = 300
    news_max_hold_min: int = 240

    # Live status dashboard (tradingbot.webapp) -- read-only, runs alongside
    # the trading engine in the same process. Reachable directly at
    # dashboard_port, and/or as a Home Assistant add-on Ingress tab.
    enable_dashboard: bool = True
    dashboard_port: int = 8099

    # AI advisor (tradingbot.advisor): periodic Claude-based supervisory
    # analysis of the trade journal / shadow results / risk state. Advisory
    # only -- it has no order authority and cannot change settings. Requires
    # ANTHROPIC_API_KEY. One API call per interval (cost scales with that,
    # not with symbols/bars).
    enable_ai_advisor: bool = False
    advisor_model: str = "claude-sonnet-5"
    advisor_interval_min: int = 60

    @model_validator(mode="after")
    def _guard_strategy_name(self) -> "Settings":
        valid = {"vwap_mean_reversion", "ema_rsi_momentum", "trendline_breakout"}
        if self.strategy not in valid:
            raise ValueError(f"STRATEGY must be one of {sorted(valid)}, got {self.strategy!r}")
        return self

    @model_validator(mode="after")
    def _guard_universe_not_empty(self) -> "Settings":
        if not self.markets.strip() and not self.symbols.strip():
            raise ValueError("Either SYMBOLS or MARKETS must contain at least one ticker")
        return self

    @model_validator(mode="after")
    def _guard_markets_dsl(self) -> "Settings":
        # Parses eagerly so a malformed MARKETS/SYMBOLS value fails fast at
        # startup instead of deep inside the engine.
        self.symbol_specs  # noqa: B018 - property access is the validation
        return self

    @model_validator(mode="after")
    def _guard_live_trading(self) -> "Settings":
        if self.ib_port in LIVE_TRADING_PORTS and not self.allow_live_trading:
            raise ValueError(
                f"IB_PORT={self.ib_port} looks like a LIVE trading port. "
                "Set ALLOW_LIVE_TRADING=true explicitly if this is intentional. "
                "Refusing to start to avoid accidentally trading real money."
            )
        return self

    @model_validator(mode="after")
    def _guard_news_monitor(self) -> "Settings":
        if self.enable_news_monitor and not (self.finnhub_api_key and self.anthropic_api_key):
            raise ValueError(
                "ENABLE_NEWS_MONITOR=true requires both FINNHUB_API_KEY and "
                "ANTHROPIC_API_KEY to be set."
            )
        return self

    @model_validator(mode="after")
    def _guard_ai_advisor(self) -> "Settings":
        if self.enable_ai_advisor and not self.anthropic_api_key:
            raise ValueError("ENABLE_AI_ADVISOR=true requires ANTHROPIC_API_KEY to be set.")
        return self

    @property
    def symbol_specs(self) -> list[SymbolSpec]:
        dsl = self.markets.strip() or symbols_to_markets_dsl(self.symbols)
        return parse_markets_dsl(dsl)

    @property
    def symbol_list(self) -> list[str]:
        return [spec.symbol for spec in self.symbol_specs]

    @property
    def is_paper(self) -> bool:
        return self.ib_port in PAPER_TRADING_PORTS


def load_settings() -> Settings:
    return Settings()
