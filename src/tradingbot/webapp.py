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
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from tradingbot.broker.connection import BrokerConnection
from tradingbot.config import Settings
from tradingbot.status import (
    gather_account_status,
    gather_news_analysis_status,
    gather_shadow_trading_status,
)

log = logging.getLogger(__name__)

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
  html += "</div>";

  html += "<h2>Open Positions</h2>" + renderPositions(data.positions);
  html += "<h2>Today's Realized P&amp;L</h2>" + renderRealized(data.realized_pnl_by_symbol);

  html += "<h2>News Shadow-Trading</h2>";
  if (!data.shadow.closed) {
    html += '<div class="empty">No shadow trades closed yet.</div>';
  } else {
    html += "<table><tr><th>Closed</th><th>Win</th><th>Loss</th><th>Timeout</th><th>Win rate</th><th>Avg R</th></tr><tr><td>" +
      data.shadow.closed + "</td><td>" + data.shadow.wins + "</td><td>" + data.shadow.losses +
      "</td><td>" + data.shadow.timeouts + "</td><td>" + Math.round(data.shadow.win_rate * 100) +
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

function refresh() {
  fetch("api/status").then(function(r) { return r.json(); }).then(function(data) {
    if (data.error) {
      document.getElementById("app").innerHTML = '<div class="err">' + data.error + "</div>";
      return;
    }
    render(data);
  }).catch(function(err) {
    document.getElementById("app").innerHTML = '<div class="err">Failed to load status: ' + err + "</div>";
  });
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


async def _status_payload(broker: BrokerConnection, settings: Settings) -> dict:
    account = await gather_account_status(broker, include_fills=False)
    log_dir = Path(settings.log_dir)
    shadow = gather_shadow_trading_status(log_dir / "shadow_trades.jsonl")
    news = gather_news_analysis_status(log_dir / "news_analysis.jsonl")
    return {
        "equity": account.equity,
        "base_currency": account.base_currency,
        "positions": [asdict(p) for p in account.positions],
        "realized_pnl_by_symbol": account.realized_pnl_by_symbol,
        "shadow": asdict(shadow),
        "news": asdict(news),
    }


def create_app(broker: BrokerConnection, settings: Settings) -> web.Application:
    async def handle_index(request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def handle_status(request: web.Request) -> web.Response:
        try:
            data = await _status_payload(broker, settings)
        except Exception as exc:  # noqa: BLE001 - never let a bad query 500-loop the page
            log.exception("Dashboard status query failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response(data)

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    return app


async def run_dashboard(broker: BrokerConnection, settings: Settings) -> None:
    """Runs until cancelled. The caller wraps this in a background task with
    broad exception handling -- a dashboard failure (e.g. the port already
    being in use) must never take down the trading engine it's reporting
    on."""
    app = create_app(broker, settings)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.dashboard_port)
    await site.start()
    log.info("Dashboard listening on 0.0.0.0:%d", settings.dashboard_port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
