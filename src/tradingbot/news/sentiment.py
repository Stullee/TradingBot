"""Claude-based sentiment assessment for a stock's recent news. Assesses all
new articles about one stock from a single poll together as one combined
picture (rather than one isolated call per headline), so mixed signals net
out sensibly and repeated/corroborating coverage can raise confidence.
Uses forced tool-use so the model's answer always comes back as structured
data instead of free text that would need brittle parsing."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)

_TOOL = {
    "name": "assess_news_sentiment",
    "description": (
        "Report the trading-relevant sentiment assessment of one or more "
        "recent financial news articles about a single stock."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["LONG", "SHORT", "NONE"],
                "description": (
                    "LONG if the news is clearly bullish and likely to move the "
                    "price up this session, SHORT if clearly bearish, NONE if "
                    "not clearly market-moving or not specific to this company."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "How confident you are that this news moves the price in that direction, same session.",
            },
            "rationale": {
                "type": "string",
                "description": "One or two sentences explaining the assessment.",
            },
        },
        "required": ["direction", "confidence", "rationale"],
    },
}

_SYSTEM_PROMPT = (
    "You assess whether a stock's recent news is likely to move its price "
    "during the current trading session. You may be given more than one "
    "recent article about the same stock from the same short window -- "
    "weigh them together as one overall picture rather than in isolation "
    "(e.g. a mix of good and bad news may net out to NONE or a weaker "
    "signal; multiple articles corroborating the same story can raise "
    "confidence). Be conservative: most news is noise. Only assign LONG or "
    "SHORT when the combined news is specific to the company and plausibly "
    "market-moving (e.g. earnings surprises, guidance changes, M&A, major "
    "contract wins/losses, regulatory action, executive changes). Generic "
    "market commentary, analyst price-target tweaks, or old/rehashed news "
    "should get NONE with low confidence."
)


@dataclass
class NewsAssessment:
    direction: str  # "LONG" | "SHORT" | "NONE"
    confidence: float
    rationale: str


class NewsSentimentAnalyzer:
    def __init__(self, api_key: str, model: str):
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    async def assess(self, symbol: str, articles: list[dict]) -> NewsAssessment:
        items = []
        for i, article in enumerate(articles, 1):
            headline = article.get("headline", "")
            summary = article.get("summary") or "(no summary provided)"
            items.append(f"{i}. Headline: {headline}\n   Summary: {summary}")
        prompt = (
            f"Stock: {symbol}\n"
            f"Recent news item(s), oldest first:\n" + "\n".join(items)
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=300,
                system=_SYSTEM_PROMPT,
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "assess_news_sentiment"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - never let a bad API call kill the engine
            log.warning("News sentiment assessment failed for %s: %s", symbol, exc)
            return NewsAssessment("NONE", 0.0, f"assessment failed: {exc}")

        for block in response.content:
            if block.type == "tool_use":
                data = block.input
                return NewsAssessment(
                    direction=data["direction"],
                    confidence=float(data["confidence"]),
                    rationale=data["rationale"],
                )
        return NewsAssessment("NONE", 0.0, "model returned no assessment")

    async def aclose(self) -> None:
        await self._client.close()
