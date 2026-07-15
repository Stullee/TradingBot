# TradingBot (IB Day Trading)

Runs the [TradingBot](https://github.com/Stullee/TradingBot) automated
intraday day-trading engine as a Home Assistant add-on.

**This add-on only contains the trading logic.** It needs a separately
running **IB Gateway** (or TWS) that it can reach over the network — this
add-on does not include or manage IB Gateway itself. See the repository's
`DEPLOY.md` for how to run IB Gateway either on another always-on device on
your LAN (recommended) or directly on this Home Assistant host.

## Before you start

- Log in to the target TWS/IB Gateway with a **paper trading** account first.
  Do not point this at a live account until you've watched it trade
  successfully on paper for a while.
- `ib_host` / `ib_port` must point at that running IB Gateway/TWS instance.
  If IB Gateway runs on the same host as Home Assistant OS via Docker with
  its default port publishing, `127.0.0.1:4002` (paper) / `127.0.0.1:4001`
  (live) is typical. If it runs on another device, use that device's LAN IP.
- `allow_live_trading` must be explicitly set to `true` to connect to a live
  port (`7496`/`4001`) — the bot refuses to start against a live port
  otherwise, as a safety net against accidental real-money trading.

## Configuration options

| Option | Description |
|---|---|
| `ib_host` / `ib_port` | Address of the running IB Gateway/TWS instance |
| `ib_client_id` | TWS API client id (must be unique per connected client) |
| `ib_account_id` | Optional; leave empty to use whichever account is active |
| `allow_live_trading` | Must be `true` to connect to a live-trading port |
| `symbols` | Simple case: comma-separated US tickers, e.g. `AAPL,MSFT,NVDA` |
| `markets` | Multi-market DSL, takes priority over `symbols` when set — see below |
| `bar_size` | IB bar size string, e.g. `5 mins` |
| `ema_fast` / `ema_slow` | EMA crossover periods |
| `rsi_period`, `rsi_long_min/max`, `rsi_short_min/max` | RSI entry filter bands |
| `atr_period`, `stop_atr_mult`, `target_atr_mult` | ATR-based stop-loss/take-profit distances |
| `allow_shorting` | Enable short entries (disabled by default) |
| `risk_per_trade_pct` | % of account equity risked per trade (stop-distance based) |
| `max_daily_loss_pct` | Daily loss kill switch threshold |
| `max_concurrent_positions` | Max number of simultaneous open positions |
| `max_position_pct` | Max % of equity allocated to a single position's notional |
| `no_new_entries_before_close_min` | Stop opening new trades this many minutes before a market's close |
| `flatten_before_close_min` | Force-close a market's positions this many minutes before its close |
| `log_level` | `debug`, `info`, `warning`, or `error` |
| `enable_news_monitor` | Enable news sentiment shadow-trading (see below). Disabled by default |
| `finnhub_api_key` | API key from [finnhub.io](https://finnhub.io) (free tier available) |
| `anthropic_api_key` | Your Anthropic API key, used to have Claude assess each news article |
| `news_model` | Claude model id used for assessment (default: a fast/cheap Haiku model) |
| `news_confidence_threshold` | Minimum model confidence (0-1) required to open a shadow trade |
| `news_poll_interval_sec` | How often to actually poll for new articles |
| `news_max_hold_min` | Force-close a shadow trade after this long if neither stop nor target hit |

## Multi-market trading (US / EU / Asia / crypto)

Set `markets` (instead of `symbols`) to trade across more than just US stocks
and extend trading-hours coverage. Format:

```
MARKET:sym1,sym2,sym3@EXCHANGE@CURRENCY;MARKET2:sym4,...
```

Example: `US:AAPL,MSFT,NVDA;EU:SAP,ASML@AEB@EUR;ASIA:0700.HK;CRYPTO:BTC,ETH`

Built-in markets (fixed presets — see the repo's `src/tradingbot/markets.py`
to change exchange/currency/session defaults):

| Market | Default exchange/currency | Session |
|---|---|---|
| `US` | SMART / USD | ~4:00–20:00 ET (includes pre/post-market) |
| `EU` | IBIS (Xetra) / EUR | 9:00–17:30 CET |
| `ASIA` | SEHK (Hong Kong) / HKD | 9:30–16:00 HKT |
| `CRYPTO` | PAXOS / USD | 24/7, with a daily 23:55 UTC flatten checkpoint |

Override a symbol's exchange/currency with `SYMBOL@EXCHANGE@CURRENCY`, e.g.
`VOD@LSE@GBP` for a London-listed stock inside the `EU` market group.

**No true zero-downtime:** even using all four, every stock exchange still
closes on weekends and there's a gap between the US close and Asia's next
open. Only crypto trades continuously.

**Costs and requirements:**
- Non-US market data (EU, Asia) typically needs a separate IB market data
  subscription (billed monthly by IB) beyond the default US entitlements.
- Crypto trading requires IB crypto trading permissions enabled on your
  account.
- Position sizing across currencies uses a live FX rate fetched from IB to
  convert your account's base-currency risk budget into each symbol's local
  currency — this needs IB market data access to the relevant FX pair.
- US extended-hours trading has materially thinner liquidity/wider spreads
  than the regular session — expect worse fills.
- Hong Kong's midday trading halt (~12:00–13:00 HKT) isn't modeled; the bot
  will attempt to trade through it, though orders simply won't fill until
  the exchange resumes.

## News sentiment shadow-trading

When `enable_news_monitor` is on, the add-on additionally polls Finnhub for
news on your configured symbols, has Claude assess whether each new article
is likely to move the price, and — above `news_confidence_threshold` —
**simulates** a hypothetical trade using the same ATR stop/target sizing as
the real strategy. It logs the outcome (win/loss/timeout, R-multiple) to
`/data/logs/shadow_trades.jsonl`.

**This never places real orders.** It exists purely to let you evaluate
whether the news-sentiment signal would have been profitable, before ever
considering wiring it to real execution.

**Cost note:** this makes real, billed API calls — one Finnhub request per
symbol per poll interval, and one Anthropic API call per new article seen.
Keep `news_poll_interval_sec` reasonable (the default is 5 minutes) to avoid
surprises on both providers' usage/rate limits.

## Logs

Live logs are visible in this add-on's **Log** tab. They're also written to
`/data/logs/tradingbot.log` (rotating), which persists across add-on
restarts/updates.

## Updating

This add-on installs the bot's Python package directly from the GitHub
repository at build time. After pushing new commits, use the add-on's
**Rebuild** button in the Home Assistant UI to pick them up.

## Risk disclaimer

This software places real orders that can lose real money. It comes with no
warranty and is not financial advice. Test extensively on paper trading
before ever enabling `allow_live_trading`. You are solely responsible for
any losses.
