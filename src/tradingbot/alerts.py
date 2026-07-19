"""Webhook alerting for events a human should hear about promptly: kill
switches tripping, a position found without its protective stop, critical
order rejections, an AI-advisor CRITICAL health call.

Posts a small JSON payload ({"title", "message", "key", "timestamp"}) to
ALERT_WEBHOOK_URL -- works out of the box with a Home Assistant webhook
trigger (-> phone notification), ntfy.sh, Slack/Discord webhooks behind a
tiny adapter, etc. Deliberately fire-and-forget: an alert must never block
or crash trading, and repeated identical alerts are rate-limited per key so
a flapping condition can't spam hundreds of notifications."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)


class AlertSender:
    def __init__(self, webhook_url: str, min_interval_sec: float = 900.0):
        self.webhook_url = webhook_url.strip()
        self.min_interval_sec = min_interval_sec
        self._last_sent: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send_soon(self, key: str, title: str, message: str) -> None:
        """Schedules the alert on the running event loop without awaiting it
        -- safe to call from synchronous code paths (event handlers)."""
        if not self.enabled:
            return
        try:
            asyncio.get_running_loop().create_task(self.send(key, title, message))
        except RuntimeError:  # no running loop (tests, shutdown)
            log.debug("No event loop for alert %r, dropping.", key)

    async def send(self, key: str, title: str, message: str) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        last = self._last_sent.get(key)
        if last is not None and now - last < self.min_interval_sec:
            return
        self._last_sent[key] = now
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        payload = {
            "title": title,
            "message": message,
            "key": key,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            response = await self._client.post(self.webhook_url, json=payload)
            response.raise_for_status()
            log.info("Alert sent (%s): %s", key, title)
        except Exception as exc:  # noqa: BLE001 - alerting must never break trading
            log.warning("Failed to send alert %r: %s", key, exc)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
