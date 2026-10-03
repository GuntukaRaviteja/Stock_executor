from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from app.core.config import settings
from app.core.exceptions import ProviderError
from app.core.logging import logger
from app.providers.market_data import BaseMarketDataProvider, get_market_data_provider


class MarketDataFetcher:
    """Fetches historical market data from configured provider."""

    def __init__(self, provider: Optional[BaseMarketDataProvider] = None):
        self.provider = provider or get_market_data_provider()
        self.provider_name = self.provider.__class__.__name__.replace("MarketDataProvider", "")

    async def fetch_historical(
        self,
        provider_symbol: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch historical OHLCV data for a symbol.

        Args:
            provider_symbol: Provider-specific symbol (e.g., RELIANCE.NS)
            start_date: Start date (inclusive)
            end_date: End date (inclusive)

        Returns:
            List of raw provider records

        Raises:
            ProviderError: If provider fails
        """
        # Apply max history limit
        if start_date:
            max_start = date.today() - timedelta(days=settings.MAX_HISTORY_DAYS)
            if start_date < max_start:
                logger.warning(
                    f"Clamping start_date from {start_date} to {max_start} (max {settings.MAX_HISTORY_DAYS} days)"
                )
                start_date = max_start

        logger.info(
            f"Fetching historical data for {provider_symbol} from {start_date} to {end_date} via {self.provider_name}"
        )

        try:
            records = await self.provider.get_historical_data(
                symbol=provider_symbol,
                start_date=start_date,
                end_date=end_date,
            )
            logger.info(f"Fetched {len(records)} records for {provider_symbol}")
            return records
        except Exception as e:
            logger.error(f"Provider {self.provider_name} failed for {provider_symbol}: {e}")
            raise ProviderError(self.provider_name, str(e))

    async def fetch_latest_quote(self, provider_symbol: str) -> Dict[str, Any]:
        """Fetch latest quote for a symbol."""
        logger.info(f"Fetching latest quote for {provider_symbol} via {self.provider_name}")
        try:
            return await self.provider.get_quote(provider_symbol)
        except Exception as e:
            logger.error(f"Provider {self.provider_name} quote failed for {provider_symbol}: {e}")
            raise ProviderError(self.provider_name, str(e))


def get_market_data_fetcher(provider: Optional[BaseMarketDataProvider] = None) -> MarketDataFetcher:
    """Factory function to create a MarketDataFetcher."""
    return MarketDataFetcher(provider)