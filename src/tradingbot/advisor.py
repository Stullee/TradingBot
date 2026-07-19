"""AI trading advisor: a Claude-based supervisory analyst wired into the
live engine.

Every ADVISOR_INTERVAL_MIN it receives a structured snapshot of everything
the system knows about itself -- the real-trade journal (win rate, avg R,
net P&L after commissions, per-symbol R), shadow-trading results, risk
state (kill switches, equity vs. daily/weekly baselines), open positions,
and the active strategy/config -- and returns a structured assessment:
overall health, what's working, what's losing money, and concrete
recommendations.

**Advisory only, by design.** It has no order authority and cannot change
configuration: the deterministic layers (bracket orders, kill switches,
position sizing) stay pure code, because a stochastic component must never
be able to bypass hard risk guarantees. What an LLM *is* good at here is
the tireless-analyst role: noticing that one symbol has bled -4R this week,
that all wins come from one market, that the sample is still too small to
conclude anything -- and saying so plainly. Reports are appended to
logs/advisor_reports.jsonl, shown on the dashboard/CLI report, and a
CRITICAL health call fires the alert webhook.

Cost note: one Claude call per interval (default hourly), with a compact
JSON snapshot -- a few cents/day at default settings, not per-bar/symbol.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)

_TOOL = {
    "name": "report_trading_assessment",
    "description": "Report a structured supervisory assessment of the trading system's current performance and risk posture.",
    "input_schema": {
        "type": "object",
        "properties": {
            "health": {
                "type": "string",
                "enum": ["OK", "WARNING", "CRITICAL"],
                "description": (
                    "CRITICAL only for situations needing prompt human attention "
                    "(risk discipline failing, rapid/systematic losses, evidence of "
                    "a malfunction). WARNING for negative-but-contained trends."
                ),
            },
            "assessment": {
                "type": "string",
                "description": "3-6 sentence plain-language summary: is the system making or losing money, what is driving it, and how confident can we be given the sample size.",
            },
            "observations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific, evidence-backed observations (cite the numbers), e.g. per-symbol or per-strategy patterns, cost drag, risk events.",
            },
            "recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "detail": {"type": "string", "description": "What to change and why, concretely."},
                        "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["title", "detail", "priority"],
                },
                "description": "Concrete, bounded suggestions a human could apply (disable a symbol, adjust a parameter, gather more data before concluding). Never 'place this trade'.",
            },
        },
        "required": ["health", "assessment", "observations", "recommendations"],
    },
}

_SYSTEM_PROMPT = (
    "You are the supervisory analyst for an automated intraday trading system "
    "(Interactive Brokers; ATR-sized bracket orders; daily/weekly loss kill "
    "switches; strategies: VWAP mean reversion, EMA momentum, or trendline "
    "breakout). You receive a JSON snapshot of its live performance and state. "
    "You have NO order authority and cannot change settings -- you analyze and "
    "advise the human operator.\n"
    "Principles: be blunt and quantitative; the goal is real profitability "
    "after commissions, not activity. Always consider sample size before "
    "concluding anything (say explicitly when there are too few trades to "
    "judge). Watch for: negative expectancy (avg R <= 0), one symbol or market "
    "bleeding consistently, wins too small relative to losses, risk events "
    "(kill switches, missing stops, order rejections), and divergence between "
    "shadow/news signals and real results. Do not invent data not present in "
    "the snapshot. Recommend pausing or shrinking risk when evidence points "
    "down; recommend nothing when the honest answer is 'keep collecting data'."
)


class TradingAdvisor:
    def __init__(
        self,
        api_key: str,
        model: str,
        interval_min: int,
        log_dir: Path,
    ):
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._interval_sec = max(interval_min, 5) * 60
        self._report_path = Path(log_dir) / "advisor_reports.jsonl"
        self._last_run_monotonic: float | None = None
        self.latest_report: dict | None = self._load_latest_report()

    def _load_latest_report(self) -> dict | None:
        if not self._report_path.exists():
            return None
        latest = None
        try:
            with self._report_path.open() as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            latest = json.loads(line)
                        except json.JSONDecodeError:
                            continue
        except OSError:
            return None
        return latest

    @property
    def due(self) -> bool:
        if self._last_run_monotonic is None:
            return True
        return time.monotonic() - self._last_run_monotonic >= self._interval_sec

    async def maybe_run(self, snapshot: dict) -> dict | None:
        """Runs an assessment if the interval has elapsed. Returns the new
        report, or None if not due / the call failed. Never raises."""
        if not self.due:
            return None
        self._last_run_monotonic = time.monotonic()
        try:
            return await self._run(snapshot)
        except Exception:  # noqa: BLE001 - the advisor must never break trading
            log.exception("AI advisor run failed; will retry next interval.")
            return None

    async def _run(self, snapshot: dict) -> dict | None:
        prompt = (
            "Current system snapshot (JSON):\n"
            + json.dumps(snapshot, default=str)
            + "\n\nProduce your supervisory assessment via the tool."
        )
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=1500,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "report_trading_assessment"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if block.type == "tool_use":
                report = dict(block.input)
                report["generated_at"] = datetime.now(timezone.utc).isoformat()
                report["model"] = self._model
                self.latest_report = report
                self._append_report(report)
                log.info(
                    "[ADVISOR] health=%s: %s",
                    report.get("health"),
                    report.get("assessment", "")[:300],
                )
                return report
        log.warning("AI advisor returned no structured assessment.")
        return None

    def _append_report(self, report: dict) -> None:
        try:
            self._report_path.parent.mkdir(parents=True, exist_ok=True)
            with self._report_path.open("a") as f:
                f.write(json.dumps(report) + "\n")
        except OSError:
            log.warning("Could not persist advisor report to %s", self._report_path)

    async def aclose(self) -> None:
        await self._client.close()
