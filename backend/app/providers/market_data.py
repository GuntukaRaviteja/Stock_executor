from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from decimal import Decimal
import random
from typing import Any, Dict, List, Optional
import httpx
from app.core.config import settings
from app.core.exceptions import ProviderError
from app.core.logging import logger


class BaseMarketDataProvider(ABC):
    """Abstract base class for market data providers."""

    @abstractmethod
    async def get_quote(self, symbol: str) -> Dict[str, Any]:
        """Fetch latest quote for a symbol."""
        pass

    @abstractmethod
    async def get_historical_data(
        self,
        symbol: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch historical daily OHLCV prices for a symbol."""
        pass


class MockMarketDataProvider(BaseMarketDataProvider):
    """Deterministic mock market data provider for offline/testing use."""

    BASE_PRICES = {
        "AAPL": Decimal("185.50"),
        "MSFT": Decimal("420.25"),
        "GOOGL": Decimal("175.80"),
        "AMZN": Decimal("180.10"),
        "NVDA": Decimal("125.40"),
        "TSLA": Decimal("245.60"),
    }

    async def get_quote(self, symbol: str) -> Dict[str, Any]:
        symbol_upper = symbol.upper()
        base = self.BASE_PRICES.get(symbol_upper, Decimal("100.00"))
        today = date.today()

        # Seed random with symbol + today for deterministic values
        rng = random.Random(f"{symbol_upper}_{today.isoformat()}")
        change = Decimal(str(round(rng.uniform(-2.5, 2.5), 2)))
        close_price = max(Decimal("1.00"), base + change)
        open_price = max(Decimal("1.00"), base + Decimal(str(round(rng.uniform(-1.0, 1.0), 2))))
        high_price = max(open_price, close_price) + Decimal(str(round(rng.uniform(0.5, 3.0), 2)))
        low_price = min(open_price, close_price) - Decimal(str(round(rng.uniform(0.5, 2.0), 2)))
        low_price = max(Decimal("0.50"), low_price)
        volume = rng.randint(1_000_000, 50_000_000)

        logger.info(f"MockMarketDataProvider: generated quote for {symbol_upper}")
        return {
            "symbol": symbol_upper,
            "price_date": today,
            "open_price": open_price,
            "high_price": high_price,
            "low_price": low_price,
            "close_price": close_price,
            "volume": volume,
        }

    async def get_historical_data(
        self,
        symbol: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        symbol_upper = symbol.upper()
        end = end_date or date.today()
        start = start_date or (end - timedelta(days=30))

        if start > end:
            start, end = end, start

        records: List[Dict[str, Any]] = []
        current = start
        base = self.BASE_PRICES.get(symbol_upper, Decimal("100.00"))

        while current <= end:
            # Skip weekends for realistic stock calendar
            if current.weekday() < 5:
                rng = random.Random(f"{symbol_upper}_{current.isoformat()}")
                drift = Decimal(str(round(rng.uniform(-3.0, 3.0), 2)))
                open_val = max(Decimal("1.00"), base + drift)
                close_val = max(Decimal("1.00"), open_val + Decimal(str(round(rng.uniform(-2.0, 2.0), 2))))
                high_val = max(open_val, close_val) + Decimal(str(round(rng.uniform(0.5, 3.5), 2)))
                low_val = min(open_val, close_val) - Decimal(str(round(rng.uniform(0.5, 2.5), 2)))
                low_val = max(Decimal("0.50"), low_val)
                volume = rng.randint(2_000_000, 45_000_000)

                records.append({
                    "price_date": current,
                    "open_price": open_val,
                    "high_price": high_val,
                    "low_price": low_val,
                    "close_price": close_val,
                    "volume": volume,
                })
                base = close_val
            current += timedelta(days=1)

        logger.info(f"MockMarketDataProvider: generated {len(records)} historical records for {symbol_upper}")
        return records


class AlphaVantageMarketDataProvider(BaseMarketDataProvider):
    """Alpha Vantage market data provider with timeout and retry handling."""

    BASE_URL = "https://www.alphavantage.co/query"

    def __init__(self, api_key: str, timeout: float = 10.0):
        self.api_key = api_key
        self.timeout = timeout

    async def get_quote(self, symbol: str) -> Dict[str, Any]:
        params = {
            "function": "GLOBAL_QUOTE",
            "symbol": symbol.upper(),
            "apikey": self.api_key,
        }
        data = await self._make_request(params)
        quote = data.get("Global Quote", {})
        if not quote:
            raise ProviderError("AlphaVantage", f"No quote found for symbol {symbol}")

        try:
            return {
                "symbol": symbol.upper(),
                "price_date": date.fromisoformat(quote.get("07. latest trading day", str(date.today()))),
                "open_price": Decimal(quote["02. open"]),
                "high_price": Decimal(quote["03. high"]),
                "low_price": Decimal(quote["04. low"]),
                "close_price": Decimal(quote["05. price"]),
                "volume": int(quote.get("06. volume", 0)),
            }
        except (KeyError, ValueError) as e:
            raise ProviderError("AlphaVantage", f"Failed to parse quote response: {e}")

    async def get_historical_data(
        self,
        symbol: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        params = {
            "function": "TIME_SERIES_DAILY",
            "symbol": symbol.upper(),
            "outputsize": "compact",
            "apikey": self.api_key,
        }
        data = await self._make_request(params)
        time_series = data.get("Time Series (Daily)", {})
        if not time_series:
            raise ProviderError("AlphaVantage", f"No historical daily data returned for {symbol}")

        results: List[Dict[str, Any]] = []
        for date_str, values in time_series.items():
            record_date = date.fromisoformat(date_str)
            if start_date and record_date < start_date:
                continue
            if end_date and record_date > end_date:
                continue

            results.append({
                "price_date": record_date,
                "open_price": Decimal(values["1. open"]),
                "high_price": Decimal(values["2. high"]),
                "low_price": Decimal(values["3. low"]),
                "close_price": Decimal(values["4. close"]),
                "volume": int(values["5. volume"]),
            })

        return sorted(results, key=lambda x: x["price_date"])

    async def _make_request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        retries = 2
        last_error = None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(retries + 1):
                try:
                    logger.debug(f"AlphaVantage request attempt {attempt + 1}: params={params.get('function')}")
                    response = await client.get(self.BASE_URL, params=params)
                    response.raise_for_status()
                    payload = response.json()

                    if "Error Message" in payload:
                        raise ProviderError("AlphaVantage", payload["Error Message"])
                    if "Note" in payload:
                        logger.warning(f"AlphaVantage API call limit notice: {payload['Note']}")

                    return payload
                except httpx.TimeoutException as e:
                    last_error = e
                    logger.warning(f"AlphaVantage timeout on attempt {attempt + 1}: {e}")
                except httpx.HTTPStatusError as e:
                    last_error = e
                    logger.warning(f"AlphaVantage HTTP error {e.response.status_code} on attempt {attempt + 1}")
                except Exception as e:
                    last_error = e
                    logger.error(f"AlphaVantage unexpected error: {e}")
                    break

        raise ProviderError("AlphaVantage", f"Request failed after retries: {last_error}")


class YahooFinanceMarketDataProvider(BaseMarketDataProvider):
    """Yahoo Finance market data provider using yfinance library."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    async def get_quote(self, symbol: str) -> Dict[str, Any]:
        import yfinance as yf

        try:
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            hist = ticker.history(period="1d", interval="1d")
            if hist.empty:
                raise ProviderError("YahooFinance", f"No quote data for symbol {symbol}")

            latest = hist.iloc[-1]
            return {
                "symbol": symbol.upper(),
                "price_date": latest.name.date() if hasattr(latest.name, 'date') else date.today(),
                "open_price": Decimal(str(latest["Open"])),
                "high_price": Decimal(str(latest["High"])),
                "low_price": Decimal(str(latest["Low"])),
                "close_price": Decimal(str(latest["Close"])),
                "adjusted_close": Decimal(str(latest.get("Adj Close", latest["Close"]))),
                "volume": int(latest["Volume"]) if latest["Volume"] else 0,
            }
        except Exception as e:
            logger.error(f"YahooFinance get_quote error for {symbol}: {e}")
            raise ProviderError("YahooFinance", f"Failed to fetch quote: {e}")

    async def get_historical_data(
        self,
        symbol: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        import yfinance as yf

        try:
            ticker = yf.Ticker(symbol)
            start = start_date or (date.today() - timedelta(days=365))
            end = end_date or date.today()

            # yfinance end date is exclusive, add 1 day
            end_plus_one = end + timedelta(days=1)

            hist = ticker.history(start=start, end=end_plus_one, interval="1d", auto_adjust=False)
            if hist.empty:
                logger.info(f"YahooFinance: No historical data for {symbol} from {start} to {end}")
                return []

            results: List[Dict[str, Any]] = []
            for idx, row in hist.iterrows():
                trading_date = idx.date() if hasattr(idx, 'date') else idx
                results.append({
                    "price_date": trading_date,
                    "open_price": Decimal(str(row["Open"])) if row["Open"] else None,
                    "high_price": Decimal(str(row["High"])) if row["High"] else None,
                    "low_price": Decimal(str(row["Low"])) if row["Low"] else None,
                    "close_price": Decimal(str(row["Close"])) if row["Close"] else None,
                    "adjusted_close": Decimal(str(row.get("Adj Close", row["Close"]))) if row.get("Adj Close", row["Close"]) else None,
                    "volume": int(row["Volume"]) if row["Volume"] else 0,
                })

            logger.info(f"YahooFinance: fetched {len(results)} historical records for {symbol}")
            return results
        except Exception as e:
            logger.error(f"YahooFinance get_historical_data error for {symbol}: {e}")
            raise ProviderError("YahooFinance", f"Failed to fetch historical data: {e}")


def get_market_data_provider() -> BaseMarketDataProvider:
    """Provider factory based on application settings."""
    provider_name = settings.MARKET_DATA_PROVIDER.lower()
    if provider_name == "alphavantage" and settings.MARKET_DATA_API_KEY:
        logger.info("Using AlphaVantageMarketDataProvider")
        return AlphaVantageMarketDataProvider(
            api_key=settings.MARKET_DATA_API_KEY,
            timeout=settings.MARKET_DATA_TIMEOUT_SECONDS,
        )
    elif provider_name == "yfinance":
        logger.info("Using YahooFinanceMarketDataProvider")
        return YahooFinanceMarketDataProvider(
            timeout=settings.MARKET_DATA_TIMEOUT_SECONDS,
        )
    logger.info("Using MockMarketDataProvider")
    return MockMarketDataProvider()
