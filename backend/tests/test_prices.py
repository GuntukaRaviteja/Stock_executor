from datetime import date
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch
from app.core.exceptions import ProviderError
from app.models.stock import Stock


def create_test_stock(client: TestClient, symbol: str = "AAPL") -> int:
    res = client.post(
        "/api/v1/stocks",
        json={"symbol": symbol, "company_name": f"{symbol} Inc", "exchange": "NASDAQ"},
    )
    return res.json()["id"]


def test_price_insertion(client: TestClient):
    """Test 7: Insert valid price record."""
    stock_id = create_test_stock(client, "AAPL")

    payload = {
        "price_date": "2026-09-15",
        "open_price": "180.00",
        "high_price": "185.00",
        "low_price": "179.50",
        "close_price": "184.20",
        "volume": 25000000,
    }
    response = client.post(f"/api/v1/stocks/{stock_id}/prices", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["stock_id"] == stock_id
    assert float(data["close_price"]) == 184.20
    assert data["price_date"] == "2026-09-15"


def test_duplicate_price_handling(client: TestClient):
    """Test 8: Inserting price with same stock_id and date returns 409."""
    stock_id = create_test_stock(client, "MSFT")

    payload = {
        "price_date": "2026-09-16",
        "open_price": "410.00",
        "high_price": "415.00",
        "low_price": "408.00",
        "close_price": "412.50",
        "volume": 12000000,
    }
    # First insert
    res1 = client.post(f"/api/v1/stocks/{stock_id}/prices", json=payload)
    assert res1.status_code == 201

    # Second insert with same date
    res2 = client.post(f"/api/v1/stocks/{stock_id}/prices", json=payload)
    assert res2.status_code == 409
    data = res2.json()
    assert data["success"] is False
    assert data["error"]["code"] == "DUPLICATE_PRICE"


def test_price_history_retrieval(client: TestClient):
    """Test 9: Price history retrieval with date filters."""
    stock_id = create_test_stock(client, "NVDA")

    # Add 3 days of prices
    for day in range(1, 4):
        client.post(
            f"/api/v1/stocks/{stock_id}/prices",
            json={
                "price_date": f"2026-09-0{day}",
                "open_price": "120.00",
                "high_price": "125.00",
                "low_price": "119.00",
                "close_price": "124.00",
                "volume": 10000000,
            },
        )

    # Fetch all
    response = client.get(f"/api/v1/stocks/{stock_id}/prices")
    assert response.status_code == 200
    assert len(response.json()) == 3

    # Fetch filtered
    filtered_res = client.get(
        f"/api/v1/stocks/{stock_id}/prices?start_date=2026-09-02&end_date=2026-09-03"
    )
    assert filtered_res.status_code == 200
    assert len(filtered_res.json()) == 2


def test_latest_price_retrieval(client: TestClient):
    """Test getting most recent price bar."""
    stock_id = create_test_stock(client, "AMZN")

    client.post(
        f"/api/v1/stocks/{stock_id}/prices",
        json={
            "price_date": "2026-09-01",
            "open_price": "180.00",
            "high_price": "182.00",
            "low_price": "179.00",
            "close_price": "181.00",
            "volume": 5000000,
        },
    )
    client.post(
        f"/api/v1/stocks/{stock_id}/prices",
        json={
            "price_date": "2026-09-10",
            "open_price": "185.00",
            "high_price": "190.00",
            "low_price": "184.00",
            "close_price": "189.50",
            "volume": 8000000,
        },
    )

    latest_res = client.get(f"/api/v1/stocks/{stock_id}/prices/latest")
    assert latest_res.status_code == 200
    assert latest_res.json()["price_date"] == "2026-09-10"


def test_invalid_price_data(client: TestClient):
    """Test 12: Validate OHLC bounds (high < max(open, close))."""
    stock_id = create_test_stock(client, "GOOGL")

    # High lower than open price
    invalid_payload = {
        "price_date": "2026-09-20",
        "open_price": "180.00",
        "high_price": "170.00",  # Invalid! high < open
        "low_price": "165.00",
        "close_price": "175.00",
        "volume": 1000000,
    }
    response = client.post(f"/api/v1/stocks/{stock_id}/prices", json=invalid_payload)
    assert response.status_code == 422
    data = response.json()
    assert data["success"] is False
    assert data["error"]["code"] == "VALIDATION_ERROR"


def test_ingest_current_price(client: TestClient, db_session):
    """Test 13: Ingest current price from market data provider."""
    stock_id = create_test_stock(client, "CURRPRICE")

    # Use mock provider - it will return deterministic data for today
    response = client.post(f"/api/v1/stocks/{stock_id}/prices/ingest")
    assert response.status_code == 200
    data = response.json()
    assert data["stock_id"] == stock_id
    assert data["symbol"] == "CURRPRICE"
    assert data["status"] == "success"
    assert data["records_received"] == 1
    assert data["records_inserted"] == 1
    assert data["records_updated"] == 0
    assert data["records_skipped"] == 0

    # Verify price was stored
    latest_res = client.get(f"/api/v1/stocks/{stock_id}/prices/latest")
    assert latest_res.status_code == 200
    assert latest_res.json()["price_date"] == str(date.today())


def test_ingest_current_price_idempotent(client: TestClient, db_session):
    """Test 14: Current price ingestion is idempotent."""
    stock_id = create_test_stock(client, "IDEMPOTENT")

    # First ingestion
    res1 = client.post(f"/api/v1/stocks/{stock_id}/prices/ingest")
    assert res1.status_code == 200
    assert res1.json()["records_inserted"] == 1
    assert res1.json()["records_updated"] == 0

    # Second ingestion (same day) - should update, not insert duplicate
    res2 = client.post(f"/api/v1/stocks/{stock_id}/prices/ingest")
    assert res2.status_code == 200
    assert res2.json()["records_inserted"] == 0
    assert res2.json()["records_updated"] == 1

    # Verify only one record exists
    history_res = client.get(f"/api/v1/stocks/{stock_id}/prices")
    assert history_res.status_code == 200
    assert len(history_res.json()) == 1


def test_ingest_current_price_provider_failure(client: TestClient, db_session):
    """Test 15: Current price ingestion handles provider failure gracefully."""
    from app.services.market_data.service import market_data_service
    from app.services.market_data.fetcher import MarketDataFetcher
    from app.providers.market_data import BaseMarketDataProvider

    stock_id = create_test_stock(client, "FAILPRICE")

    # Create a failing provider
    failing_provider = AsyncMock()
    failing_provider.get_historical_data.side_effect = ProviderError("FailingAPI", "API down")
    failing_provider.get_quote.side_effect = ProviderError("FailingAPI", "API down")
    failing_provider.__class__.__name__ = "FailingProvider"

    failing_fetcher = MarketDataFetcher(failing_provider)
    failing_service = type(market_data_service)(fetcher=failing_fetcher)

    # Override the dependency
    from app.main import app
    from app.api.dependencies import get_market_data_service

    app.dependency_overrides[get_market_data_service] = lambda: failing_service

    try:
        response = client.post(f"/api/v1/stocks/{stock_id}/prices/ingest")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert "API down" in data["error"]
        assert data["records_inserted"] == 0
    finally:
        app.dependency_overrides.clear()


def test_ingest_historical_prices(client: TestClient, db_session):
    """Test 16: Ingest historical prices from market data provider."""
    stock_id = create_test_stock(client, "HISTPRICE")

    payload = {
        "start_date": "2025-01-01",
        "end_date": "2025-01-10",
    }
    response = client.post(f"/api/v1/stocks/{stock_id}/prices/ingest-historical", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["stock_id"] == stock_id
    assert data["symbol"] == "HISTPRICE"
    assert data["status"] == "success"
    assert data["records_received"] > 0
    assert data["records_inserted"] > 0
    assert data["records_updated"] == 0
    assert data["records_skipped"] >= 0

    # Verify history was stored
    history_res = client.get(f"/api/v1/stocks/{stock_id}/prices?start_date=2025-01-01&end_date=2025-01-10")
    assert history_res.status_code == 200
    assert len(history_res.json()) > 0


def test_ingest_historical_prices_idempotent(client: TestClient, db_session):
    """Test 17: Historical price ingestion is idempotent."""
    stock_id = create_test_stock(client, "HISTIDEMP")

    payload = {
        "start_date": "2025-01-01",
        "end_date": "2025-01-10",
    }
    # First ingestion
    res1 = client.post(f"/api/v1/stocks/{stock_id}/prices/ingest-historical", json=payload)
    assert res1.status_code == 200
    first_inserted = res1.json()["records_inserted"]
    assert first_inserted > 0

    # Second ingestion (same date range) - should update, not insert duplicates
    res2 = client.post(f"/api/v1/stocks/{stock_id}/prices/ingest-historical", json=payload)
    assert res2.status_code == 200
    assert res2.json()["records_inserted"] == 0
    assert res2.json()["records_updated"] == first_inserted

    # Verify no duplicates
    history_res = client.get(f"/api/v1/stocks/{stock_id}/prices?start_date=2025-01-01&end_date=2025-01-10")
    assert history_res.status_code == 200
    assert len(history_res.json()) == first_inserted


def test_ingest_historical_prices_invalid_date_range(client: TestClient):
    """Test 18: Historical ingestion rejects invalid date range."""
    stock_id = create_test_stock(client, "BADDATES")

    payload = {
        "start_date": "2025-01-10",
        "end_date": "2025-01-01",  # end before start
    }
    response = client.post(f"/api/v1/stocks/{stock_id}/prices/ingest-historical", json=payload)
    assert response.status_code == 422
    data = response.json()
    assert data["success"] is False
    assert data["error"]["code"] == "VALIDATION_ERROR"


def test_ingest_historical_prices_not_found(client: TestClient):
    """Test 19: Historical ingestion returns 404 for non-existent stock."""
    payload = {
        "start_date": "2025-01-01",
        "end_date": "2025-01-10",
    }
    response = client.post("/api/v1/stocks/999999/prices/ingest-historical", json=payload)
    assert response.status_code == 404
    data = response.json()
    assert data["success"] is False
    assert data["error"]["code"] == "STOCK_NOT_FOUND"


def test_ingest_current_price_not_found(client: TestClient):
    """Test 20: Current price ingestion returns 404 for non-existent stock."""
    response = client.post("/api/v1/stocks/999999/prices/ingest")
    assert response.status_code == 404
    data = response.json()
    assert data["success"] is False
    assert data["error"]["code"] == "STOCK_NOT_FOUND"
