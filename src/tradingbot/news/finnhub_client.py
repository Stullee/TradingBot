"""Minimal async client for Finnhub's free /company-news endpoint."""
from __future__ import annotations

from datetime import date, timedelta

import httpx


class FinnhubNewsClient:
    def __init__(self, api_key: str, timeout: float = 10.0):
        self._client = httpx.AsyncClient(
            base_url="https://finnhub.io/api/v1", timeout=timeout
        )
        self._api_key = api_key

    async def company_news(self, symbol: str, lookback_days: int = 1) -> list[dict]:
        """Returns raw article dicts with (at least) id, headline, summary, url,
        datetime (unix seconds), source, related."""
        today = date.today()
        params = {
            "symbol": symbol,
            "from": (today - timedelta(days=lookback_days)).isoformat(),
            "to": today.isoformat(),
            "token": self._api_key,
        }
        response = await self._client.get("/company-news", params=params)
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
