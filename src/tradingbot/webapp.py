"""Live status dashboard: a small read-only web server run alongside the
trading engine, showing account equity, open positions, today's P&L,
shadow-trading track record, and news-analysis activity.

Runs in the same asyncio event loop/process as the engine, reading its
already-live IB connection state directly -- positions()/portfolio() are
local reads of data kept fresh by the connection's own streaming account
subscription, not new IB requests per page view. Never places orders; the
HTTP handlers are strictly read-only.

Reachable both directly (http://<host>:dashboard_port/) and, for the Home
Assistant add-on, as an Ingress tab embedded in the HA sidebar -- see
ha-addons/tradingbot/config.yaml. The page uses only relative URLs with no
external assets so it works unmodified behind Ingress's subpath proxying."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from tradingbot.broker.connection import BrokerConnection
from tradingbot.config import Settings
from tradingbot.news.monitor import NewsMonitor
from tradingbot.status import (
    gather_account_status,
    gather_latest_news_by_symbol,
    gather_news_analysis_status,
    gather_shadow_trading_status,
)

log = logging.getLogger(__name__)

# The dashboard polls every 5s (see _INDEX_HTML's setInterval below) -- a
# query that hasn't completed by the time the next poll would fire is
# already indistinguishable from "broken" to whoever's looking at the page,
# so this fails fast into a visible error instead of leaving the request
# (and the page) hanging indefinitely.
_STATUS_TIMEOUT_SEC = 8.0

_INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>TradingBot Status</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 1.5rem;
         background: #0f1115; color: #e6e6e6; }
  @media (prefers-color-scheme: light) { body { background: #f7f7f8; color: #1a1a1a; } }
  h1 { font-size: 1.3rem; margin: 0 0 1rem; }
  h2 { font-size: 1rem; margin: 1.5rem 0 0.5rem; opacity: 0.85; }
  .cards { display: flex; flex-wrap: wrap; gap: 0.75rem; }
  .card { background: rgba(128,128,128,0.12); border-radius: 8px; padding: 0.9rem 1.2rem;
          min-width: 140px; }
  .card .label { font-size: 0.75rem; opacity: 0.65; text-transform: uppercase; letter-spacing: 0.03em; }
  .card .value { font-size: 1.4rem; font-weight: 600; margin-top: 0.2rem; }
  .pos { color: #3ecf6e; } .neg { color: #ef5350; }
  table { border-collapse: collapse; width: 100%; margin-top: 0.4rem; font-size: 0.9rem; }
  th, td { text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid rgba(128,128,128,0.2); }
  th { opacity: 0.65; font-weight: 500; font-size: 0.8rem; }
  .empty { opacity: 0.5; padding: 0.4rem 0.6rem; font-size: 0.9rem; }
  .updated { opacity: 0.5; font-size: 0.75rem; margin-top: 1.5rem; }
  .err { color: #ef5350; }
</style>
</head>
<body>
<h1>TradingBot Status</h1>
<div id="app">Loading&hellip;</div>
<div class="updated" id="updated"></div>
<script>
function fmt(n, d) { d = d === undefined ? 2 : d; return (n === null || n === undefined) ? "-" : n.toFixed(d); }
function cls(n) { return n > 0 ? "pos" : (n < 0 ? "neg" : ""); }

function badge(dir) {
  if (dir === "LONG") return '<span class="pos">&#9650; LONG</span>';
  if (dir === "SHORT") return '<span class="neg">&#9660; SHORT</span>';
  if (dir === "FLAT" || dir === "NONE") return '<span style="opacity:0.5">FLAT</span>';
  return '<span style="opacity:0.35">-</span>';
}

function renderSymbols(symbols) {
  if (!symbols.length) return '<div class="empty">No symbols configured.</div>';
  var rows = symbols.map(function(s) {
    var posText = s.position_qty ? fmt(s.position_qty, 0) +
      (s.unrealized_pnl !== null ? " (" + fmt(s.unrealized_pnl) + ")" : "") : "-";
    return "<tr><td>" + s.symbol + "</td><td>" + posText + "</td><td>" + badge(s.bar_signal) +
      "</td><td>" + (s.rsi !== null && s.rsi !== undefined ? fmt(s.rsi, 0) : "-") + "</td><td>" +
      badge(s.news_direction) + "</td><td>" +
      (s.news_confidence !== null && s.news_confidence !== undefined ? fmt(s.news_confidence, 2) : "-") +
      "</td></tr>";
  }).join("");
  return "<table><tr><th>Symbol</th><th>Position (unreal. P&amp;L)</th><th>Bar Signal</th>" +
    "<th>RSI</th><th>News</th><th>Confidence</th></tr>" + rows + "</table>";
}

function renderPositions(positions) {
  if (!positions.length) return '<div class="empty">No open positions.</div>';
  var rows = positions.map(function(p) {
    return "<tr><td>" + p.symbol + "</td><td>" + fmt(p.quantity, 0) + "</td><td>" +
      fmt(p.avg_cost, 4) + "</td><td class=\\"" + cls(p.unrealized_pnl) + "\\">" +
      fmt(p.unrealized_pnl) + "</td></tr>";
  }).join("");
  return "<table><tr><th>Symbol</th><th>Qty</th><th>Avg Cost</th><th>Unrealized P&amp;L</th></tr>" +
    rows + "</table>";
}

function renderRealized(byBymbol) {
  var symbols = Object.keys(byBymbol);
  if (!symbols.length) return '<div class="empty">No realized P&amp;L yet today.</div>';
  var total = 0;
  var rows = symbols.sort(function(a, b) { return byBymbol[b] - byBymbol[a]; }).map(function(s) {
    total += byBymbol[s];
    return "<tr><td>" + s + "</td><td class=\\"" + cls(byBymbol[s]) + "\\">" + fmt(byBymbol[s]) + "</td></tr>";
  }).join("");
  return "<table><tr><th>Symbol</th><th>Realized P&amp;L</th></tr>" + rows +
    "<tr><td><b>Total</b></td><td class=\\"" + cls(total) + "\\"><b>" + fmt(total) + "</b></td></tr></table>";
}

function renderOpenShadowTrades(trades) {
  if (!trades.length) return '<div class="empty">No shadow trades currently open.</div>';
  var rows = trades.map(function(t) {
    var r = t.unrealized_r;
    var rText = (r === null || r === undefined) ? "-" : (r >= 0 ? "+" : "") + fmt(r, 2) + "R";
    return "<tr><td>" + t.symbol + "</td><td>" + badge(t.direction) + "</td><td>" +
      fmt(t.entry_price, 2) + "</td><td>" + (t.last_price !== null && t.last_price !== undefined ? fmt(t.last_price, 2) : "-") +
      "</td><td class=\\"" + cls(r) + "\\">" + rText + "</td><td>" +
      fmt(t.stop_price, 2) + "</td><td>" + fmt(t.target_price, 2) + "</td><td>" +
      fmt(t.confidence, 2) + "</td><td>" +
      (t.opened_at || "").slice(11, 19) + "</td><td>" + (t.headline || "") + "</td></tr>";
  }).join("");
  return "<table><tr><th>Symbol</th><th>Dir</th><th>Entry</th><th>Current</th><th>Unrealized</th>" +
    "<th>Stop</th><th>Target</th><th>Confidence</th><th>Opened</th><th>Headline</th></tr>" + rows + "</table>";
}

function render(data) {
  var html = "";
  html += '<div class="cards">';
  html += '<div class="card"><div class="label">Equity</div><div class="value">' +
    fmt(data.equity) + " " + data.base_currency + "</div></div>";
  html += '<div class="card"><div class="label">Shadow win rate</div><div class="value">' +
    (data.shadow.closed ? Math.round(data.shadow.win_rate * 100) + "%" : "-") + "</div></div>";
  html += '<div class="card"><div class="label">Shadow avg R</div><div class="value ' +
    cls(data.shadow.avg_r) + '">' + (data.shadow.closed ? fmt(data.shadow.avg_r, 2) : "-") + "</div></div>";
  html += '<div class="card"><div class="label">News assessed</div><div class="value">' +
    data.news.assessed + "</div></div>";
  html += '<div class="card"><div class="label">Shadow trades open</div><div class="value">' +
    data.open_shadow_trades.length + "</div></div>";
  html += "</div>";

  html += "<h2>Per-Symbol Signals</h2>" + renderSymbols(data.symbols);
  html += "<h2>Open Positions</h2>" + renderPositions(data.positions);
  html += "<h2>Today's Realized P&amp;L</h2>" + renderRealized(data.realized_pnl_by_symbol);

  html += "<h2>Open Shadow Trades</h2>" + renderOpenShadowTrades(data.open_shadow_trades);

  html += "<h2>News Shadow-Trading</h2>";
  if (!data.shadow.closed) {
    html += '<div class="empty">No shadow trades closed yet.</div>';
  } else {
    html += "<table><tr><th>Closed</th><th>Win</th><th>Loss</th><th>Timeout</th><th>Flattened</th>" +
      "<th>Win rate</th><th>Avg R</th></tr><tr><td>" +
      data.shadow.closed + "</td><td>" + data.shadow.wins + "</td><td>" + data.shadow.losses +
      "</td><td>" + data.shadow.timeouts + "</td><td>" + data.shadow.flattened + "</td><td>" +
      Math.round(data.shadow.win_rate * 100) +
      "%</td><td class=\\"" + cls(data.shadow.avg_r) + "\\">" + fmt(data.shadow.avg_r, 2) + "</td></tr></table>";
  }

  html += "<h2>News Analysis Activity</h2>";
  var dirs = Object.keys(data.news.direction_counts);
  if (!data.news.assessed && !data.news.skipped) {
    html += '<div class="empty">No articles assessed yet.</div>';
  } else {
    html += "<table><tr><th>Assessed batches</th><th>Skipped (trade open)</th><th>Avg confidence</th><th>Directions</th></tr><tr><td>" +
      data.news.assessed + "</td><td>" + data.news.skipped + "</td><td>" + fmt(data.news.avg_confidence) +
      "</td><td>" + (dirs.length ? dirs.map(function(d) { return d + ": " + data.news.direction_counts[d]; }).join(", ") : "-") +
      "</td></tr></table>";
  }

  document.getElementById("app").innerHTML = html;
  document.getElementById("updated").textContent = "Updated " + new Date().toLocaleTimeString();
}

// Under HA Ingress this page is served at a per-install path like
// .../api/hassio_ingress/<token> -- if that URL happens to reach the browser
// *without* a trailing slash (confirmed live: aiohttp's own access log shows
// exactly one request all session, "GET /", meaning the Supervisor stripped
// the ingress prefix down to nothing but never forwarded anything else --
// consistent with the iframe having navigated to the bare token with no
// trailing slash), a bare relative fetch("api/status") resolves per the
// standard last-path-segment-replacement rule and clobbers the token itself
// -- e.g. ".../hassio_ingress/<token>" + "api/status" => ".../hassio_ingress/
// api/status", which is a dead path the Supervisor can't route anywhere,
// silently swallowed before it ever reaches this process (nothing in
// aiohttp's access log to show for it, matching what was observed live).
// Building the request path by hand from location.pathname with an explicit
// trailing slash sidesteps that whole class of resolution surprise instead
// of depending on the browser's exact URL bar contents.
var API_STATUS_URL = location.pathname.replace(/\\/?$/, "/") + "api/status";

// A bare fetch() with no timeout just hangs forever on the failure above,
// leaving the page stuck on "Loading..." with no visible error. Bounding it
// with AbortController means the page always resolves to *something* --
// either real data or a visible error -- instead of an indefinite spinner.
var REFRESH_TIMEOUT_MS = 12000;

function refresh() {
  var controller = new AbortController();
  var timedOut = false;
  var timer = setTimeout(function() { timedOut = true; controller.abort(); }, REFRESH_TIMEOUT_MS);
  fetch(API_STATUS_URL, { signal: controller.signal }).then(function(r) {
    clearTimeout(timer);
    return r.json();
  }).then(function(data) {
    if (data.error) {
      document.getElementById("app").innerHTML = '<div class="err">' + data.error + "</div>";
      return;
    }
    render(data);
  }).catch(function(err) {
    clearTimeout(timer);
    var msg = timedOut
      ? "No response from the bot after " + (REFRESH_TIMEOUT_MS / 1000) + "s (request may not be " +
        "reaching it -- check Ingress/network, or try the add-on's direct http://<host>:8099/ URL)."
      : "Failed to load status: " + err;
    document.getElementById("app").innerHTML = '<div class="err">' + msg + "</div>";
  });
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


async def _status_payload(
    broker: BrokerConnection,
    settings: Settings,
    latest_signals: dict[str, dict],
    news_monitor: NewsMonitor | None = None,
) -> dict:
    account = await gather_account_status(broker, include_fills=False)
    log_dir = Path(settings.log_dir)
    shadow = gather_shadow_trading_status(log_dir / "shadow_trades.jsonl")
    news = gather_news_analysis_status(log_dir / "news_analysis.jsonl")
    latest_news = gather_latest_news_by_symbol(log_dir / "news_analysis.jsonl")
    positions_by_symbol = {p.symbol: p for p in account.positions}

    symbols = []
    for symbol in settings.symbol_list:
        pos = positions_by_symbol.get(symbol)
        sig = latest_signals.get(symbol)
        n = latest_news.get(symbol)
        symbols.append(
            {
                "symbol": symbol,
                "position_qty": pos.quantity if pos else 0,
                "unrealized_pnl": pos.unrealized_pnl if pos else None,
                "bar_signal": sig["signal"] if sig else None,
                "bar_signal_updated_at": sig["updated_at"] if sig else None,
                "rsi": sig["rsi"] if sig else None,
                "news_direction": n["direction"] if n else None,
                "news_confidence": n["confidence"] if n else None,
                "news_updated_at": n["assessed_at"] if n else None,
            }
        )

    open_shadow_trades = []
    if news_monitor is not None:
        for t in news_monitor.shadow.open_trades.values():
            row = asdict(t)
            row["unrealized_r"] = t.unrealized_r_multiple()
            open_shadow_trades.append(row)

    return {
        "equity": account.equity,
        "base_currency": account.base_currency,
        "positions": [asdict(p) for p in account.positions],
        "realized_pnl_by_symbol": account.realized_pnl_by_symbol,
        "shadow": asdict(shadow),
        "news": asdict(news),
        "symbols": symbols,
        "open_shadow_trades": open_shadow_trades,
    }


def create_app(
    broker: BrokerConnection,
    settings: Settings,
    latest_signals: dict[str, dict] | None = None,
    news_monitor: NewsMonitor | None = None,
) -> web.Application:
    latest_signals = {} if latest_signals is None else latest_signals

    async def handle_index(request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def handle_status(request: web.Request) -> web.Response:
        # Logged unconditionally (not just on failure) so a request that
        # never completes is distinguishable in the logs from one that was
        # never received at all -- confirmed live: the page loads fine but
        # /api/status never once shows up in aiohttp's access log, which
        # only ever logs after a response is sent. A hard timeout also
        # means a hang shows up as a visible error on the page instead of
        # an indefinite "Loading..." spinner.
        log.info("Dashboard: /api/status request received")
        start = time.monotonic()
        try:
            data = await asyncio.wait_for(
                _status_payload(broker, settings, latest_signals, news_monitor),
                timeout=_STATUS_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            log.warning("Dashboard: /api/status timed out after %.1fs", _STATUS_TIMEOUT_SEC)
            return web.json_response(
                {"error": f"Status query timed out after {_STATUS_TIMEOUT_SEC:.0f}s"}, status=504
            )
        except Exception as exc:  # noqa: BLE001 - never let a bad query 500-loop the page
            log.exception("Dashboard status query failed")
            return web.json_response({"error": str(exc)}, status=500)
        log.info("Dashboard: /api/status responded in %.2fs", time.monotonic() - start)
        return web.json_response(data)

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    return app


async def run_dashboard(
    broker: BrokerConnection,
    settings: Settings,
    latest_signals: dict[str, dict],
    news_monitor: NewsMonitor | None = None,
) -> None:
    """Runs until cancelled. The caller wraps this in a background task with
    broad exception handling -- a dashboard failure (e.g. the port already
    being in use) must never take down the trading engine it's reporting
    on. `latest_signals` is the engine's own live dict (see engine.py),
    shared by reference, not copied -- reads always see current values."""
    app = create_app(broker, settings, latest_signals, news_monitor)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.dashboard_port)
    await site.start()
    log.info("Dashboard listening on 0.0.0.0:%d", settings.dashboard_port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
