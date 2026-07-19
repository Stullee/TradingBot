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
| `strategy` | `vwap_mean_reversion` (default), `ema_rsi_momentum`, or `trendline_breakout` — see below |
| `bar_size` | IB bar size string, e.g. `5 mins` |
| `ema_fast` / `ema_slow` | EMA crossover periods (used by `ema_rsi_momentum` only) |
| `rsi_period` | RSI lookback period (used by `vwap_mean_reversion`/`ema_rsi_momentum`) |
| `rsi_long_min/max`, `rsi_short_min/max` | RSI entry filter bands (used by `ema_rsi_momentum` only) |
| `rsi_oversold` / `rsi_overbought` | RSI extremes required for an entry (used by `vwap_mean_reversion` only) |
| `vwap_dist_atr_mult` | How far price must have drifted from VWAP, in ATR multiples, to trigger a mean-reversion entry (used by `vwap_mean_reversion` only) |
| `trend_ema_period` | Trend filter EMA period (used by `vwap_mean_reversion` only) — blocks LONG entries in a downtrend and SHORT entries in an uptrend, so dip-buys/rip-sells only fire with the prevailing trend, not against it. Set to `0` to disable and get the old unconditional countertrend behavior |
| `trend_window` | Rolling regression window in bars (used by `trendline_breakout` only) |
| `min_r_squared` | Minimum goodness-of-fit (0-1) required to treat the regression as a real trend, not noise (used by `trendline_breakout` only) |
| `atr_period`, `stop_atr_mult`, `target_atr_mult` | ATR-based stop-loss/take-profit distances |
| `allow_shorting` | Enable short entries (disabled by default) |
| `risk_per_trade_pct` | % of account equity risked per trade (stop-distance based) |
| `max_daily_loss_pct` | Daily loss kill switch threshold. Baselines and tripped switches persist across restarts — a restart never grants a fresh loss budget |
| `max_weekly_loss_pct` | Weekly drawdown breaker: % loss since the ISO week's start that halts trading until next week (catches slow bleeds a daily limit never trips on). `0` disables |
| `max_concurrent_positions` | Max number of simultaneous open positions (in-flight entry orders count) |
| `max_position_pct` | Max % of equity allocated to a single position's notional |
| `max_open_risk_pct` | Portfolio heat cap: total entry-to-stop risk across all open positions as % of equity. `0` disables |
| `no_new_entries_before_close_min` | Stop opening new trades this many minutes before a market's close |
| `flatten_before_close_min` | Force-close a market's positions this many minutes before its close (early closes/holidays are calendar-aware) |
| `no_entries_after_open_min` | No entries during the first N minutes after a market's open (opening-auction volatility). `0` disables |
| `log_level` | `debug`, `info`, `warning`, or `error` |
| `alert_webhook_url` | Optional webhook POSTed on kill-switch trips, positions found without a stop, and critical order rejections — point it at a Home Assistant webhook trigger for phone notifications. Empty disables |
| `backtest_commission_per_share` / `backtest_slippage_bps` | Cost model for the backtest entrypoints (commission per share per side; adverse slippage per market fill) |
| `enable_ai_advisor` | Periodic Claude-based supervisory analysis of the real-trade journal/risk state (advisory only, never places orders; needs `anthropic_api_key`). Shown on the dashboard. Disabled by default |
| `advisor_model` / `advisor_interval_min` | Which Claude model the advisor uses, and how often (one API call per interval) |
| `enable_news_monitor` | Enable news sentiment shadow-trading (see below). Disabled by default |
| `finnhub_api_key` | API key from [finnhub.io](https://finnhub.io) (free tier available) |
| `anthropic_api_key` | Your Anthropic API key, used to have Claude assess each news article |
| `news_model` | Claude model id used for assessment (default: a fast/cheap Haiku model) |
| `news_confidence_threshold` | Minimum model confidence (0-1) required to open a shadow trade |
| `news_poll_interval_sec` | How often to actually poll for new articles |
| `news_max_hold_min` | Force-close a shadow trade after this long if neither stop nor target hit |
| `enable_dashboard` | Enable the live status dashboard (see below). Enabled by default |

## Live status dashboard

A read-only web dashboard runs alongside the trading engine (same process,
no extra setup), auto-refreshing every 5 seconds. It never places orders.
Shows:

- **Per-symbol signals** — every watched symbol, its current position (if
  any), the bar strategy's latest LONG/SHORT/FLAT reading and RSI, and the
  latest news-sentiment reading, all in one table — a "what's this stock
  doing right now" glance instead of digging through logs. When the active
  strategy is `trendline_breakout`, extra columns appear automatically:
  the fitted line's slope, its R-squared (fit quality), and the current
  price's distance from that line — the exact numbers `generate_signal` is
  acting on, as a live sanity check on what the strategy currently sees.
- **Open positions** with live unrealized P&L.
- **Today's realized P&L** per symbol.
- **Open shadow trades** — any news-driven shadow trade currently being
  tracked (direction, entry/stop/target, confidence, the headline that
  triggered it), not just ones that have already closed.
- **News shadow-trading track record** and **news analysis activity**
  (batches assessed, direction breakdown, average confidence) — the same
  persisted history `python -m tradingbot.report` reads.

It shows up as a **panel in the Home Assistant sidebar** (via Ingress) once
you rebuild to a version with this feature — look for "TradingBot (IB Day
Trading)" in the sidebar, not a separate URL. It's also reachable directly
at `http://<host>:8099/` if you'd rather open it that way. The port is
fixed at 8099 for the add-on specifically because Home Assistant's Ingress
routing is configured against that fixed port — if you need a different
port, that's a standalone/`.env` (`DASHBOARD_PORT`) thing, not something to
change via the add-on's options. Turn it off entirely with
`enable_dashboard: false` if you'd rather not run a web server at all.

## Strategy: VWAP mean-reversion vs EMA/RSI momentum vs trendline breakout

Three pluggable strategies are built in, selected via the `strategy` option:

- **`vwap_mean_reversion`** (default): enters when price has drifted
  meaningfully below/above the session VWAP (scaled by ATR) *and* RSI
  confirms an oversold/overbought extreme *and* the current bar shows the
  first sign of a reversal. Exits once price reverts back to VWAP (or the
  ATR stop/target bracket is hit first, whichever comes first). This fires
  many times a session — genuine "many small trades a day" day trading —
  because VWAP deviations happen constantly, not just at trend turns. Best
  suited to range-bound/choppy conditions, which is most of a session; it
  can lose money fighting a strongly trending day.
- **`ema_rsi_momentum`**: trend-following EMA(fast/slow) crossover, filtered
  by an RSI band and VWAP side. Only fires at genuine trend turns — a handful
  of times a session at most — but tends to catch bigger moves when a real
  trend develops. Exits on the opposite EMA cross.
- **`trendline_breakout`**: fits a least-squares regression line over the
  last `trend_window` bars and enters when price breaks above (or, if
  shorting, below) that line, requiring the fit to be tight
  (`min_r_squared`) so it's chasing a real, established rise rather than a
  noisy zigzag that happens to net upward. Chases momentum instead of
  fading it, unlike the other two — more aggressive and, unlike the other
  two, **unvalidated live**. Backtest it first (see Backtesting below)
  before trusting it with capital. Exits when price falls back through
  its own trendline.

If you're not seeing enough trades with the momentum strategy, or want a
higher-frequency, higher-win-rate/smaller-per-trade approach, use the
default `vwap_mean_reversion`. Switch to `ema_rsi_momentum` if you'd rather
trade fewer, larger trend moves, or `trendline_breakout` if you want to
chase a confirmed rise instead of waiting for either of those setups.

## Multi-market trading (US / EU / Asia / crypto)

Set `markets` (instead of `symbols`) to trade across more than just US stocks
and extend trading-hours coverage. Format:

```
MARKET:sym1,sym2,sym3@EXCHANGE@CURRENCY;MARKET2:sym4,...
```

Example: `US:AAPL,MSFT,NVDA;EU:SAP,ASML@AEB@EUR;ASIA:700;CRYPTO:BTC,ETH`

Built-in markets (fixed presets — see the repo's `src/tradingbot/markets.py`
to change exchange/currency/session defaults):

| Market | Default exchange/currency | Session |
|---|---|---|
| `US` | SMART / USD | 9:30–16:00 ET (regular hours only) |
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
  account. The default `PAXOS` venue is for US-regulated (IBKR LLC)
  accounts; EU accounts (IBKR Ireland/EEA "Europe - Crypto-Assets"
  permission) trade crypto via Zero Hash instead -- override per symbol,
  e.g. `CRYPTO:BTC@ZEROHASHE@USD` (confirmed against a real account under
  ZEROHASHE, not ZEROHASH; try `@EUR` if `@USD` doesn't qualify).
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
the real strategy. It logs the outcome (win/loss/timeout, or flattened at
its market's close, with an R-multiple) to `/data/logs/shadow_trades.jsonl`.
Any still-open shadow trade is also mirrored to
`/data/logs/open_shadow_trades.json`, and any qualifying assessment that
couldn't open one yet to `/data/logs/pending_assessments.json` -- both
restored on startup, so a restart (add-on rebuild included) doesn't
silently drop a trade in flight or a pending opportunity.

**This never places real orders.** It exists purely to let you evaluate
whether the news-sentiment signal would have been profitable, before ever
considering wiring it to real execution.

**Cost note:** this makes real, billed API calls — one Finnhub request per
symbol per poll interval, and one Anthropic API call per symbol per poll
*only if* it has new (unseen) articles, batching all of that poll's new
articles about the same stock into a single call rather than one call per
headline. Every new article gets exactly one Claude call, ever, whether or
not a shadow trade can open right away — if it can't (a trade's already
open for that symbol, or its market is closed), a qualifying assessment is
kept as a pending candidate and retried on later ticks once that clears,
instead of being skipped and lost. Keep `news_poll_interval_sec` reasonable
(the default is 5 minutes) to avoid surprises on both providers' usage/rate
limits. Every article's assessment is persisted to
`/data/logs/news_analysis.jsonl`, so a restart doesn't reprocess (and
re-bill) the same day's news backlog again — before this was fixed, every
restart re-assessed everything Finnhub's 1-day lookback returned as "new,"
which is what actually drains API credits fast on a large watchlist, not
the poll interval itself. With a big watchlist (50+ symbols), also expect
the poll to take up to a minute or so to complete — requests to Finnhub are
deliberately paced to stay under its free-tier rate limit rather than
firing all at once. Each symbol's batch is also capped at 10 articles per
poll (the most recent 10, by publish time) — a fresh start or a very newsy
stock can otherwise hand Finnhub's full 1-day lookback to a single Claude
call at once (confirmed live: 250 articles in one call for a single
symbol), which is both expensive and a worse assessment than a focused
batch. Anything past the cap simply stays unseen and gets picked up (still
capped) on a later poll instead of being dropped. If the news monitor ever
looks like it's gone quiet, check for a `News poll complete: X/Y symbols
had new articles...` line — that's logged every poll regardless of
whether anything was found, so silence there (not just silence in
`[NEWS]` lines) is the real signal something's actually stuck.

## Status report

`python addon_report_entrypoint.py` (same `docker exec` pattern as the
backtester, see below) prints a snapshot: account equity, open positions
with unrealized P&L, today's realized P&L per symbol (also persisted to
`/data/logs/realized_pnl.json` so a restart doesn't lose P&L for a position
that already fully closed), today's fill count, and — reading straight
from the persisted files above — news
shadow-trading win rate/expectancy and news-analysis activity. Read-only,
places no orders.

## Backtesting

Before trusting a strategy, it's worth checking whether it has any
historical edge at all. The repo includes a standalone backtester that
replays the configured strategy bar-by-bar over historical IB data and
prints win rate, expectancy (in R-multiples), profit factor, and max
drawdown per symbol. It never places orders.

This add-on doesn't expose it as a UI option (it's a one-off analysis
tool, not a long-running service) — run it as a one-off command inside
the already-running add-on container instead, using its own SYMBOLS/
MARKETS/STRATEGY config automatically (no separate `.env` needed):

```bash
docker ps                 # find the container name, e.g. addon_local_tradingbot
docker exec -it <container-name> python addon_backtest_entrypoint.py
```

If your HA install doesn't give you host shell access (e.g. no SSH/Terminal
add-on), run it from a clone of the repo on any machine that can reach your
IB Gateway instead: `pip install -e .`, copy your `.env`, then
`python -m tradingbot.backtest.runner`. See the repo's `README.md` for
details.

### Trend-filter before/after comparison

A second one-off tool answers a narrower question specifically for
`vwap_mean_reversion`'s trend filter (`trend_ema_period`): does it actually
help, and does that hold up on data it wasn't looked at while deciding on
it? It fetches the same history once per symbol, then runs both the filter
on and the filter off over a chronological train/validation split (70/30,
validation = the more recent slice) and prints both configurations' stats
side by side for both slices. A filter that only wins on the train slice is
a sign it's tuned to that window rather than a real edge.

```bash
docker exec -it <container-name> python addon_compare_entrypoint.py
```

Or standalone: `python -m tradingbot.backtest.compare_runner`.

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
