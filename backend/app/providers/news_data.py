from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
import random
from typing import Any, Dict, List, Optional
import httpx
from app.core.config import settings
from app.core.exceptions import ProviderError
from app.core.logging import logger


class BaseNewsDataProvider(ABC):
    """Abstract base class for financial news and corporate event providers."""

    @abstractmethod
    async def get_recent_news(self, symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch and normalize recent news/events for a ticker symbol."""
        pass


class MockNewsDataProvider(BaseNewsDataProvider):
    """Mock news provider supplying normalized market headlines and events."""

    MOCK_TEMPLATES = [
        ("EARNINGS", "{symbol} Reports Q{quarter} Revenue of ${rev}B, Beating Estimates", "POSITIVE", "Operating margins expanded by 180 basis points with robust enterprise software adoption."),
        ("EARNINGS", "{symbol} Misses Q{quarter} Profit Consensus Amid Supply Chain Headwinds", "NEGATIVE", "Company experienced margin compression due to elevated transport and raw material input costs."),
        ("PRODUCT", "{symbol} Unveils Next-Generation Cloud and Edge AI Acceleration Platform", "POSITIVE", "The new platform offers 2.5x energy efficiency and has already secured Tier-1 customer commitments."),
        ("LEADERSHIP", "{symbol} Announces Chief Operating Officer Succession Plan", "NEUTRAL", "Veteran executive appointed to oversee global supply chain expansion and capital allocation."),
        ("REGULATORY", "{symbol} Receives Key European Union Regulatory Clearance", "POSITIVE", "Antitrust authorities concluded review with standard commitments and no structural remedies required."),
        ("ANALYST_RATING", "Wall Street Brokerage Upgrades {symbol} to Buy with Raised Target", "POSITIVE", "Analyst highlighted durable recurring cash flows and accelerating enterprise cloud adoption."),
        ("DIVIDEND", "{symbol} Board of Directors Declares Quarterly Cash Dividend", "POSITIVE", "Dividend payout of $0.25 per share represents a 5% increase year-over-year."),
    ]

    async def get_recent_news(self, symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
        symbol_upper = symbol.upper()
        today = date.today()
        events: List[Dict[str, Any]] = []

        rng = random.Random(f"news_{symbol_upper}_{today.isoformat()}")

        for i in range(min(limit, len(self.MOCK_TEMPLATES))):
            event_type, title_template, default_impact, desc = self.MOCK_TEMPLATES[i]
            event_date = today - timedelta(days=i * 3 + rng.randint(0, 2))

            title = title_template.format(
                symbol=symbol_upper,
                quarter=(i % 4) + 1,
                rev=round(rng.uniform(15.0, 95.0), 1),
            )

            events.append({
                "event_type": event_type,
                "event_title": title,
                "event_description": desc,
                "event_date": event_date,
                "impact": default_impact,
                "source_raw_id": None,
            })

        logger.info(f"MockNewsDataProvider: generated {len(events)} events for {symbol_upper}")
        return events


class NewsAPIDataProvider(BaseNewsDataProvider):
    """NewsAPI provider implementation with timeout and error handling."""

    BASE_URL = "https://newsapi.org/v2/everything"

    def __init__(self, api_key: str, timeout: float = 10.0):
        self.api_key = api_key
        self.timeout = timeout

    async def get_recent_news(self, symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
        params = {
            "q": symbol.upper(),
            "sortBy": "publishedAt",
            "pageSize": limit,
            "apiKey": self.api_key,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(self.BASE_URL, params=params)
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                logger.error(f"NewsAPI error for {symbol}: {e}")
                raise ProviderError("NewsAPI", str(e))

        articles = data.get("articles", [])
        normalized: List[Dict[str, Any]] = []

        for item in articles:
            published_at_str = item.get("publishedAt")
            event_date = date.today()
            if published_at_str:
                try:
                    event_date = datetime.fromisoformat(published_at_str.replace("Z", "+00:00")).date()
                except Exception:
                    pass

            normalized.append({
                "event_type": "NEWS",
                "event_title": (item.get("title") or "")[:500],
                "event_description": item.get("description") or item.get("content"),
                "event_date": event_date,
                "impact": "NEUTRAL",
                "source_raw_id": None,
            })

        return normalized


def get_news_data_provider() -> BaseNewsDataProvider:
    """News provider factory function."""
    provider_name = settings.NEWS_DATA_PROVIDER.lower()
    if provider_name == "newsapi" and settings.NEWS_DATA_API_KEY:
        logger.info("Using NewsAPIDataProvider")
        return NewsAPIDataProvider(
            api_key=settings.NEWS_DATA_API_KEY,
            timeout=settings.NEWS_DATA_TIMEOUT_SECONDS,
        )
    logger.info("Using MockNewsDataProvider")
    return MockNewsDataProvider()
