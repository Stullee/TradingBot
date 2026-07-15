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
| `symbols` | Comma-separated tickers to trade, e.g. `AAPL,MSFT,NVDA` |
| `bar_size` | IB bar size string, e.g. `5 mins` |
| `ema_fast` / `ema_slow` | EMA crossover periods |
| `rsi_period`, `rsi_long_min/max`, `rsi_short_min/max` | RSI entry filter bands |
| `atr_period`, `stop_atr_mult`, `target_atr_mult` | ATR-based stop-loss/take-profit distances |
| `allow_shorting` | Enable short entries (disabled by default) |
| `risk_per_trade_pct` | % of account equity risked per trade (stop-distance based) |
| `max_daily_loss_pct` | Daily loss kill switch threshold |
| `max_concurrent_positions` | Max number of simultaneous open positions |
| `max_position_pct` | Max % of equity allocated to a single position's notional |
| `market_open` / `market_close` | Regular trading hours, US/Eastern, `HH:MM` |
| `no_new_entries_before_close_min` | Stop opening new trades this many minutes before close |
| `flatten_before_close_min` | Force-close all positions this many minutes before close |
| `log_level` | `debug`, `info`, `warning`, or `error` |

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
