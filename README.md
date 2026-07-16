# TradingBot

Automated day-trading bot for Interactive Brokers, driven through TWS or IB
Gateway's API (via [`ib_async`](https://github.com/ib-api-reloaded/ib_async)).
Trades US, EU, and Asia stocks plus crypto to extend trading-hours coverage
across a single day.

**⚠️ Financial risk disclaimer:** this software places real orders that can
lose real money. It is provided for educational purposes, comes with no
warranty, and is not financial advice. Test extensively on a **paper trading**
account before ever pointing it at a live account. You are solely responsible
for any losses.

**Running this on Home Assistant OS?** See [`DEPLOY.md`](DEPLOY.md) — it
ships as a proper Home Assistant add-on (`ha-addons/tradingbot`), configured
through the HA UI instead of a `.env` file. This top-level README covers
running it directly (plain Python / your own server).

## What it does

- Connects to TWS/IB Gateway and streams live bars for a configurable list of
  symbols across **US, EU, and Asia stocks plus crypto** (see "Multi-market
  trading" below) — each market keeps its own session/hours independently.
- Runs an EMA-crossover momentum strategy (filtered by RSI and session VWAP)
  to generate long/short entry signals.
- Sizes every position from your account equity and an ATR-based stop
  distance, converted through a live FX rate for foreign-currency symbols,
  so every trade risks a fixed, configurable % of the account regardless of
  which market it's in.
- Every entry is a **bracket order**: a market entry with an attached
  stop-loss and take-profit — no naked/unprotected positions are ever opened.
- Enforces a hard **daily loss limit** (kill switch, across all markets):
  once breached, no new trades are opened and everything is flattened.
- Enforces a max number of concurrent positions (global, across all markets)
  and a max notional per position.
- **Never holds positions past a market's own session**: stops opening new
  trades a configurable number of minutes before that market's close, and
  force-flattens that market's positions shortly before it closes — this is
  a day-trading bot, not a swing-trading bot. Crypto (which never technically
  closes) still gets a daily synthetic flatten checkpoint for the same
  bounded-risk discipline.
- Defaults to Interactive Brokers' **paper trading** port and refuses to
  start against a live port unless you explicitly opt in.
- **Optional news sentiment shadow-trading** (`ENABLE_NEWS_MONITOR=true`):
  polls Finnhub for news per symbol, has Claude assess each new article,
  and — observation only, never real orders — simulates a hypothetical
  trade to log whether the call would have been right. See below.

## Architecture

```
src/tradingbot/
  config.py            typed settings loaded from .env
  market_hours.py       generic timezone-aware session helper (MarketSession)
  markets.py             built-in market presets: US/EU/ASIA/CRYPTO
  symbols.py              MARKETS config DSL parser -> per-symbol specs
  fx.py                   live currency conversion via IB forex quotes
  broker/connection.py  IB connect/reconnect + multi-asset contract qualification
  data/bars.py           live streaming historical bars per symbol
  data/indicators.py     EMA / RSI / ATR / session VWAP (pure pandas, no IB dependency)
  strategy/               pluggable Strategy interface + EmaRsiVwapMomentum
  risk/manager.py         position sizing + daily loss kill switch
  execution/order_manager.py  bracket orders + per-market/global flatten
  news/                   news sentiment shadow-trading (see below)
  engine.py               orchestrates the whole loop, per market
  main.py                 entry point
```

`data/indicators.py`, `strategy/`, `risk/manager.py`, `market_hours.py`,
`markets.py`, and `symbols.py` have no dependency on `ib_async` and are
fully unit tested (see `tests/`). Swap in a different `Strategy`
implementation (same interface) to change the trading logic without
touching connection, risk or execution code.

## Multi-market trading (US / EU / Asia / crypto)

Set `MARKETS` (instead of `SYMBOLS`) in `.env` to trade across more than
just US stocks:

```
MARKETS=US:AAPL,MSFT,NVDA;EU:SAP,ASML@AEB@EUR;ASIA:700;CRYPTO:BTC,ETH
```

Format: `MARKET:sym1,sym2,sym3@EXCHANGE@CURRENCY;MARKET2:sym4,...` — a bare
symbol uses that market's default exchange/currency; append
`@EXCHANGE@CURRENCY` to override (e.g. `VOD@LSE@GBP` for London within the
`EU` group). Built-in markets (fixed presets in `src/tradingbot/markets.py`
— edit that file for a different exchange or session):

| Market | Default exchange/currency | Session |
|---|---|---|
| `US` | SMART / USD | ~4:00–20:00 ET (includes pre/post-market) |
| `EU` | IBIS (Xetra) / EUR | 9:00–17:30 CET |
| `ASIA` | SEHK (Hong Kong) / HKD | 9:30–16:00 HKT |
| `CRYPTO` | PAXOS / USD | 24/7, daily 23:55 UTC flatten checkpoint |

**This is not true zero-downtime.** Every stock exchange still closes on
weekends, and there's a gap between the US close and Asia's next open — only
crypto here trades continuously. Combining all four markets gets you close
to round-the-clock weekday coverage with brief gaps, not literal 24/7.

**Costs and requirements:**
- Non-US market data (EU, Asia) typically needs a separate IB market data
  subscription beyond the default US entitlements.
- Crypto trading requires IB crypto trading permissions on your account.
  The default `PAXOS` venue is for US-regulated (IBKR LLC) accounts; EU
  accounts (IBKR Ireland/EEA "Europe - Crypto-Assets" permission) trade
  crypto via Zero Hash instead -- override per symbol, e.g.
  `CRYPTO:BTC@ZEROHASHE@USD` (confirmed against a real account under
  ZEROHASHE, not ZEROHASH; try `@EUR` if `@USD` doesn't qualify).
- Cross-currency position sizing needs live IB market data access to the
  relevant FX pair.
- US extended hours have materially thinner liquidity/wider spreads than the
  regular session.
- Hong Kong's midday trading halt (~12:00–13:00 HKT) isn't modeled; orders
  simply won't fill until the exchange resumes trading.

## Prerequisites

1. **Interactive Brokers account** with market data subscriptions for the
   symbols you want to trade (paper accounts inherit your live account's
   market data entitlements once linked).
2. **TWS** (Trader Workstation) or **IB Gateway** installed and running.
3. In TWS/Gateway: `File -> Global Configuration -> API -> Settings`:
   - Enable **"Enable ActiveX and Socket Clients"**.
   - Add `127.0.0.1` to **"Trusted IP Addresses"**.
   - Note the **Socket port** (defaults: TWS paper `7497`, TWS live `7496`,
     Gateway paper `4002`, Gateway live `4001`).
   - Untick "Read-Only API" (the bot needs to place orders).
4. Log in to TWS/Gateway with your **paper trading** account first.
5. Python 3.11+.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: symbols, risk parameters, etc.
```

`IB_PORT` defaults to `7497` (TWS paper trading). Leave it as-is until you've
validated the bot's behavior extensively.

## Running

Make sure TWS/IB Gateway is running and logged in, then:

```bash
source .venv/bin/activate
python -m tradingbot.main
```

The bot logs to both the console and `logs/tradingbot.log` (rotating). It
runs continuously; stop it with `Ctrl+C` — on shutdown it flattens any open
positions before disconnecting.

## Running the tests

```bash
pip install -r requirements.txt
pytest
```

Tests cover indicator math, strategy signal generation, risk manager
position sizing, the daily loss kill switch, and market-hours logic — none
of them require a live TWS/Gateway connection.

## Going live

Going live is a deliberate, explicit action:

1. Set `IB_PORT` to your live TWS (`7496`) or Gateway (`4001`) port.
2. Set `ALLOW_LIVE_TRADING=true` in `.env`. Without this, the bot refuses to
   start against a live port.
3. Start with small size: a low `RISK_PER_TRADE_PCT`, a tight
   `MAX_DAILY_LOSS_PCT`, and `MAX_CONCURRENT_POSITIONS=1` until you trust the
   bot's behavior in live conditions (fills, slippage, latency all differ
   from paper trading).

## News sentiment shadow-trading

Set `ENABLE_NEWS_MONITOR=true` plus `FINNHUB_API_KEY` and `ANTHROPIC_API_KEY`
in `.env` to turn this on. Every `NEWS_POLL_INTERVAL_SEC`, the bot fetches
recent news per symbol from [Finnhub](https://finnhub.io) (free tier works),
sends each new article to Claude for a structured LONG/SHORT/NONE +
confidence assessment (`src/tradingbot/news/sentiment.py`), and — above
`NEWS_CONFIDENCE_THRESHOLD` — opens a **simulated** trade sized with the same
ATR stop/target rules as the real strategy (`src/tradingbot/news/shadow_trade.py`).
It's tracked against the bot's own live bar data until the stop, the target,
or `NEWS_MAX_HOLD_MIN` is hit, then logged as a win/loss/timeout with an
R-multiple to `logs/shadow_trades.jsonl`.

**This never places a real order.** It's a way to evaluate whether the news
signal would have been profitable before ever considering wiring it to real
execution — which, if you get there, is a materially higher-risk feature
than the pure-technical strategy (headline speed races against firms with
structurally lower latency, LLM interpretation can misread nuance/sarcasm,
and it's much harder to backtest than price-based signals).

It also costs real money to run: one Finnhub call per symbol per poll
interval, one Anthropic API call per new article seen. Keep the poll
interval reasonable.

## Known limitations / possible next steps

- Market-hours logic doesn't account for exchange holidays or early closes —
  plug in [`pandas_market_calendars`](https://pypi.org/project/pandas-market-calendars/)
  for full holiday-awareness.
- Market presets (exchange/currency/session) are fixed in `markets.py`, not
  settings — covers one representative exchange per region (Xetra, Hong
  Kong); other exchanges need a per-symbol `@EXCHANGE@CURRENCY` override or
  editing the presets directly.
- Hong Kong's midday trading halt isn't modeled as a split session.
- The bundled strategy (EMA cross + RSI + VWAP) is a straightforward,
  well-known starting point tuned for stocks, not a proven money-maker —
  crypto's different volatility profile in particular hasn't been separately
  validated. Backtest and paper trade before trusting any of it with real
  capital.
- No built-in backtesting engine yet; `strategy/` is decoupled from IB
  specifically so historical OHLCV data can be replayed through the same
  `generate_signal()` logic.
- Single-process, single-account. No multi-account/portfolio allocation.
