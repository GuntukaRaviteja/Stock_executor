import pytest
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from sqlalchemy.orm import Session
from app.models.stock import Stock
from app.providers.market_data import MockMarketDataProvider
from app.services.market_data import market_data_service
from app.services.market_data.validator import MarketDataValidator
from app.services.market_data.normalizer import MarketDataNormalizer
from app.services.market_data.fetcher import MarketDataFetcher
from app.core.exceptions import ProviderError, StockNotFoundError


class TestMarketDataValidator:
    """Test market data validation."""

    def test_valid_record_passes(self):
        validator = MarketDataValidator()
        record = {
            "trading_date": date(2025, 1, 1),
            "open": Decimal("100.00"),
            "high": Decimal("105.00"),
            "low": Decimal("99.00"),
            "close": Decimal("103.00"),
            "adjusted_close": Decimal("103.00"),
            "volume": 1000000,
        }
        result = validator.validate_record(record, "TEST.NS")
        assert result["trading_date"] == date(2025, 1, 1)
        assert result["open"] == Decimal("100.00")

    def test_missing_trading_date_fails(self):
        validator = MarketDataValidator()
        record = {
            "open": Decimal("100.00"),
            "high": Decimal("105.00"),
            "low": Decimal("99.00"),
            "close": Decimal("103.00"),
            "volume": 1000000,
        }
        with pytest.raises(Exception) as exc_info:
            validator.validate_record(record, "TEST.NS")
        assert "Missing required field 'trading_date'" in str(exc_info.value)

    def test_high_less_than_low_fails(self):
        validator = MarketDataValidator()
        record = {
            "trading_date": date(2025, 1, 1),
            "open": Decimal("100.00"),
            "high": Decimal("100.00"),  # high < low
            "low": Decimal("105.00"),
            "close": Decimal("103.00"),
            "volume": 1000000,
        }
        with pytest.raises(Exception) as exc_info:
            validator.validate_record(record, "TEST.NS")
        assert "high" in str(exc_info.value).lower()

    def test_high_less_than_open_fails(self):
        validator = MarketDataValidator()
        record = {
            "trading_date": date(2025, 1, 1),
            "open": Decimal("105.00"),
            "high": Decimal("100.00"),  # high < open
            "low": Decimal("99.00"),
            "close": Decimal("103.00"),
            "volume": 1000000,
        }
        with pytest.raises(Exception) as exc_info:
            validator.validate_record(record, "TEST.NS")
        assert "high" in str(exc_info.value).lower()

    def test_negative_volume_fails(self):
        validator = MarketDataValidator()
        record = {
            "trading_date": date(2025, 1, 1),
            "open": Decimal("100.00"),
            "high": Decimal("105.00"),
            "low": Decimal("99.00"),
            "close": Decimal("103.00"),
            "volume": -1000,
        }
        with pytest.raises(Exception) as exc_info:
            validator.validate_record(record, "TEST.NS")
        assert "volume cannot be negative" in str(exc_info.value)

    def test_negative_price_fails(self):
        validator = MarketDataValidator()
        record = {
            "trading_date": date(2025, 1, 1),
            "open": Decimal("-100.00"),
            "high": Decimal("105.00"),
            "low": Decimal("99.00"),
            "close": Decimal("103.00"),
            "volume": 1000000,
        }
        with pytest.raises(Exception) as exc_info:
            validator.validate_record(record, "TEST.NS")
        assert "open cannot be negative" in str(exc_info.value)


class TestMarketDataNormalizer:
    """Test market data normalization."""

    def test_normalize_mock_record(self):
        normalizer = MarketDataNormalizer()
        records = [{
            "price_date": date(2025, 1, 1),
            "open_price": Decimal("100.00"),
            "high_price": Decimal("105.00"),
            "low_price": Decimal("99.00"),
            "close_price": Decimal("103.00"),
            "volume": 1000000,
        }]
        normalized = normalizer.normalize(records, "TEST.NS", "mock")
        assert len(normalized) == 1
        assert normalized[0]["trading_date"] == date(2025, 1, 1)
        assert normalized[0]["open"] == Decimal("100.00")
        assert normalized[0]["provider_symbol"] == "TEST.NS"

    def test_normalize_alphavantage_record(self):
        normalizer = MarketDataNormalizer()
        records = [{
            "price_date": date(2025, 1, 1),
            "open_price": Decimal("100.00"),
            "high_price": Decimal("105.00"),
            "low_price": Decimal("99.00"),
            "close_price": Decimal("103.00"),
            "volume": 1000000,
        }]
        normalized = normalizer.normalize(records, "TEST", "alphavantage")
        assert len(normalized) == 1
        assert normalized[0]["adjusted_close"] is None  # Alpha Vantage doesn't provide


class TestMarketDataFetcher:
    """Test market data fetcher."""

    @pytest.mark.asyncio
    async def test_fetch_historical_mock(self):
        provider = MockMarketDataProvider()
        fetcher = MarketDataFetcher(provider)
        records = await fetcher.fetch_historical("AAPL", date(2025, 1, 1), date(2025, 1, 10))
        assert len(records) > 0
        assert all("price_date" in r for r in records)

    @pytest.mark.asyncio
    async def test_provider_failure_raises(self):
        failing_provider = AsyncMock()
        failing_provider.get_historical_data.side_effect = ProviderError("TestProvider", "API down")
        failing_provider.__class__.__name__ = "TestProvider"

        fetcher = MarketDataFetcher(failing_provider)
        with pytest.raises(ProviderError) as exc_info:
            await fetcher.fetch_historical("AAPL", date(2025, 1, 1), date(2025, 1, 10))
        assert "TestProvider" in str(exc_info.value)


class TestMarketDataService:
    """Test market data service integration."""

    @pytest.mark.asyncio
    async def test_ingest_stock_success(self, db_session: Session):
        """Test successful ingestion of stock data."""
        # Create test stock
        stock = Stock(
            symbol="TESTSTOCK",
            company_name="Test Stock Inc",
            exchange="NSE",
            market="INDIA",
            provider_symbol="TESTSTOCK.NS",
        )
        db_session.add(stock)
        db_session.commit()
        db_session.refresh(stock)

        # Use mock provider
        result = await market_data_service.ingest_stock(
            db_session, stock, start_date=date(2025, 1, 1), end_date=date(2025, 1, 10)
        )

        assert result.status == "success"
        assert result.fetched > 0
        assert result.inserted > 0
        assert result.updated == 0

    @pytest.mark.asyncio
    async def test_ingest_stock_idempotent(self, db_session: Session):
        """Test that re-ingestion updates instead of duplicating."""
        stock = Stock(
            symbol="IDEMPOTENT",
            company_name="Idempotent Test",
            exchange="NSE",
            market="INDIA",
            provider_symbol="IDEMPOTENT.NS",
        )
        db_session.add(stock)
        db_session.commit()
        db_session.refresh(stock)

        # First ingestion
        result1 = await market_data_service.ingest_stock(
            db_session, stock, start_date=date(2025, 1, 1), end_date=date(2025, 1, 10)
        )
        first_inserted = result1.inserted

        # Second ingestion (same date range)
        result2 = await market_data_service.ingest_stock(
            db_session, stock, start_date=date(2025, 1, 1), end_date=date(2025, 1, 10)
        )

        assert result2.inserted == 0
        assert result2.updated == first_inserted
        assert result2.skipped == 0

        # Verify database has no duplicates
        from app.repositories.price_repository import price_repository
        dates = price_repository.get_existing_dates_for_stock(
            db_session, stock.id, date(2025, 1, 1), date(2025, 1, 10)
        )
        assert len(dates) == first_inserted

    @pytest.mark.asyncio
    async def test_ingest_multiple_stocks_partial_failure(self, db_session: Session):
        """Test ingestion handles partial failures correctly (not found)."""
        # Create one valid stock
        stock1 = Stock(
            symbol="GOODSTOCK",
            company_name="Good Stock Inc",
            exchange="NSE",
            market="INDIA",
            provider_symbol="GOODSTOCK.NS",
        )
        db_session.add(stock1)
        db_session.commit()

        # Use default service (mock provider works)
        result = await market_data_service.ingest_multiple_stocks(
            db_session,
            symbols=["GOODSTOCK", "BADSTOCK"],  # BADSTOCK doesn't exist
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 10),
        )

        assert result.status == "partial"
        assert result.requested == 2
        assert result.successful == 1
        assert result.failed == 0
        assert result.not_found == 1

    @pytest.mark.asyncio
    async def test_get_stock_history(self, db_session: Session):
        """Test retrieving stock history."""
        # Create stock and add some prices
        stock = Stock(
            symbol="HISTORYTEST",
            company_name="History Test Inc",
            exchange="NSE",
            market="INDIA",
        )
        db_session.add(stock)
        db_session.commit()
        db_session.refresh(stock)

        # Ingest some data first
        await market_data_service.ingest_stock(
            db_session, stock, start_date=date(2025, 1, 1), end_date=date(2025, 1, 10)
        )

        # Retrieve history
        response = market_data_service.get_stock_history(
            db_session, "HISTORYTEST", start_date=date(2025, 1, 1), end_date=date(2025, 1, 10)
        )

        assert response.symbol == "HISTORYTEST"
        assert response.count > 0
        assert len(response.data) > 0
        assert all(isinstance(d.open, Decimal) for d in response.data)

    @pytest.mark.asyncio
    async def test_get_stock_history_not_found(self, db_session: Session):
        """Test 404 for non-existent stock."""
        with pytest.raises(StockNotFoundError):
            market_data_service.get_stock_history(db_session, "NONEXISTENT")

    @pytest.mark.asyncio
    async def test_ingest_stock_not_found(self, db_session: Session):
        """Test ingestion fails gracefully for non-existent stock."""
        # This would be caught at the API level, but service should handle it
        result = await market_data_service.ingest_multiple_stocks(
            db_session,
            symbols=["NONEXISTENT"],
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 10),
        )
        assert result.not_found == 1
        assert result.successful == 0