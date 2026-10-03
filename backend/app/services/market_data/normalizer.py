from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional
from app.core.logging import logger


class MarketDataNormalizer:
    """Normalizes provider-specific market data into a common internal format."""

    def normalize_yfinance_record(
        self, record: Dict[str, Any], provider_symbol: str
    ) -> Dict[str, Any]:
        """Normalize a single yfinance record."""
        return {
            "provider_symbol": provider_symbol,
            "trading_date": record["price_date"],
            "open": record.get("open_price"),
            "high": record.get("high_price"),
            "low": record.get("low_price"),
            "close": record.get("close_price"),
            "adjusted_close": record.get("adjusted_close"),
            "volume": record.get("volume", 0),
        }

    def normalize_alphavantage_record(
        self, record: Dict[str, Any], provider_symbol: str
    ) -> Dict[str, Any]:
        """Normalize a single Alpha Vantage record."""
        return {
            "provider_symbol": provider_symbol,
            "trading_date": record["price_date"],
            "open": record.get("open_price"),
            "high": record.get("high_price"),
            "low": record.get("low_price"),
            "close": record.get("close_price"),
            "adjusted_close": None,  # Alpha Vantage daily doesn't provide adjusted close
            "volume": record.get("volume", 0),
        }

    def normalize_mock_record(
        self, record: Dict[str, Any], provider_symbol: str
    ) -> Dict[str, Any]:
        """Normalize a single mock provider record."""
        return {
            "provider_symbol": provider_symbol,
            "trading_date": record["price_date"],
            "open": record.get("open_price"),
            "high": record.get("high_price"),
            "low": record.get("low_price"),
            "close": record.get("close_price"),
            "adjusted_close": None,
            "volume": record.get("volume", 0),
        }

    def normalize(
        self,
        records: List[Dict[str, Any]],
        provider_symbol: str,
        provider_name: str,
    ) -> List[Dict[str, Any]]:
        """
        Normalize records based on provider type.

        Args:
            records: Raw records from provider
            provider_symbol: Symbol used for fetching
            provider_name: Name of provider (yfinance, alphavantage, mock)

        Returns:
            List of normalized records
        """
        normalized = []

        for record in records:
            try:
                if provider_name.lower() == "yfinance":
                    norm = self.normalize_yfinance_record(record, provider_symbol)
                elif provider_name.lower() == "alphavantage":
                    norm = self.normalize_alphavantage_record(record, provider_symbol)
                else:
                    norm = self.normalize_mock_record(record, provider_symbol)

                # Ensure all required fields present with defaults
                norm.setdefault("volume", 0)
                norm.setdefault("adjusted_close", None)

                normalized.append(norm)
            except Exception as e:
                logger.warning(
                    f"Failed to normalize record for {provider_symbol}: {e}"
                )

        logger.debug(f"Normalized {len(normalized)}/{len(records)} records for {provider_symbol}")
        return normalized


market_data_normalizer = MarketDataNormalizer()