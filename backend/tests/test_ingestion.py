import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy.orm import Session
from app.core.exceptions import ProviderError
from app.models.stock import Stock
from app.providers.market_data import AlphaVantageMarketDataProvider, MockMarketDataProvider
from app.services.ingestion_service import IngestionService


@pytest.mark.asyncio
async def test_mock_market_provider():
    """Verify mock market provider generates valid OHLC data."""
    provider = MockMarketDataProvider()
    quote = await provider.get_quote("AAPL")
    assert quote["symbol"] == "AAPL"
    assert quote["high_price"] >= max(quote["open_price"], quote["close_price"])
    assert quote["low_price"] <= min(quote["open_price"], quote["close_price"])

    history = await provider.get_historical_data("AAPL")
    assert len(history) > 0
    for bar in history:
        assert bar["high_price"] >= max(bar["open_price"], bar["close_price"])
        assert bar["low_price"] <= min(bar["open_price"], bar["close_price"])


@pytest.mark.asyncio
async def test_provider_failure_handling(db_session: Session):
    """Test 14: Provider failures are caught gracefully and reported."""
    stock = Stock(symbol="FAIL", company_name="Failing Co", exchange="NYSE")
    db_session.add(stock)
    db_session.commit()
    db_session.refresh(stock)

    failing_provider = AsyncMock()
    failing_provider.get_historical_data.side_effect = ProviderError("FailingAPI", "Upstream API down")

    svc = IngestionService(market_provider=failing_provider)
    result = await svc.ingest_prices_for_stock(db_session, stock)

    assert result["status"] == "failed"
    assert "Upstream API down" in result["error"]
    assert result["inserted"] == 0


@pytest.mark.asyncio
async def test_ingestion_pipeline_run(db_session: Session):
    """Test 15: Ingestion pipeline execution, insertion and skipping on rerun."""
    stock = Stock(symbol="INGEST", company_name="Ingestion Test Inc", exchange="NASDAQ")
    db_session.add(stock)
    db_session.commit()
    db_session.refresh(stock)

    svc = IngestionService(market_provider=MockMarketDataProvider())

    # First run: should insert records
    res1 = await svc.run_pipeline(db_session, symbols=["INGEST"], days=10, include_events=False)
    assert "INGEST" in res1
    assert res1["INGEST"]["inserted"] > 0
    first_inserted = res1["INGEST"]["inserted"]

    # Second run: should skip existing records
    res2 = await svc.run_pipeline(db_session, symbols=["INGEST"], days=10, include_events=False)
    assert res2["INGEST"]["inserted"] == 0
    assert res2["INGEST"]["skipped"] == first_inserted
