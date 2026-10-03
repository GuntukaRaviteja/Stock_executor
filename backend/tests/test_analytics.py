"""Unit tests for Phase 6 analytics service."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import AppValidationError
from app.main import app
from app.schemas.analytics import (
    ConsolidatedAnalyticsResponse,
    TechnicalIndicatorPoint,
    ReturnsResponse,
    SMAResponse,
    EMAResponse,
    RSIResponse,
    MACDResponse,
    VolatilityResponse,
    SupportResistanceResponse,
    AnalyticsRequest
)


@pytest.fixture
def stock_id(client, db_session):
    """Create a test stock and return its ID. Cleanup after test."""
    import uuid
    symbol = f"ANATEST_{uuid.uuid4().hex[:8]}"
    res = client.post(
        "/api/v1/stocks",
        json={"symbol": symbol, "company_name": "Analytics Test Inc", "exchange": "NASDAQ"},
    )
    assert res.status_code == 201, f"Failed to create stock: {res.json()}"
    created_id = res.json()["id"]
    yield created_id
    # Cleanup: delete prices then the stock, using the shared test session.
    from app.models.stock import Stock
    from app.models.price import StockPrice

    db_session.query(StockPrice).filter(StockPrice.stock_id == created_id).delete()
    db_session.query(Stock).filter(Stock.id == created_id).delete()
    db_session.commit()


def _insert_prices(client, stock_id, num_days=30):
    """Insert deterministic price data."""
    base_price = Decimal("100.00")
    for i in range(num_days):
        close = base_price + Decimal(str(i * 0.1))
        client.post(
            f"/api/v1/stocks/{stock_id}/prices",
            json={
                "price_date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                "open_price": str(close + Decimal("1.0")),
                "high_price": str(close + Decimal("2.0")),
                "low_price": str(close - Decimal("2.0")),
                "close_price": str(close),
                "volume": 1000000,
            },
        )


# --- Returns Tests ---

def test_returns_calculation(stock_id):
    """Test daily and cumulative returns calculation."""
    client = TestClient(app)
    with client:
        _insert_prices(client, stock_id, num_days=30)

        # Test returns endpoint
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/returns")
        assert response.status_code == 200
        data = response.json()
        # Convert to float for comparison (JSON serializes Decimal to string)
        assert float(data["cumulative_return"]) > 0  # Price went up

        # Check daily returns structure
        assert "daily_returns" in data
        assert len(data["daily_returns"]) == 29  # n-1 daily returns for n prices
        # Check that values are numeric (could be Decimal or float after JSON serialization)
        for point in data["daily_returns"]:
            val = point["value"]
            # Handle string values from JSON serialization
            numeric_val = float(val) if isinstance(val, str) else val
            assert isinstance(numeric_val, (int, float, Decimal))


def test_returns_insufficient_data(stock_id):
    """Test returns with insufficient data."""
    # Insert only 1 price record via the API
    client = TestClient(app)
    with client:
        client.post(
            f"/api/v1/stocks/{stock_id}/prices",
            json={
                "price_date": "2026-01-01",
                "open_price": "100.00",
                "high_price": "102.00",
                "low_price": "98.00",
                "close_price": "101.00",
                "volume": 1000000,
            },
        )

        # Should fail with validation error
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/returns")
        assert response.status_code == 422


# --- SMA Tests ---

def test_sma_calculation(stock_id):
    """Test SMA calculation."""
    client = TestClient(app)
    with client:
        _insert_prices(client, stock_id, num_days=50)

        # Test SMA 20
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/sma?period=20")
        assert response.status_code == 200
        data = response.json()
        assert data["period"] == 20
        assert len(data["values"]) == 31  # 50 - 20 + 1

        # Test SMA 50 (needs 50 records, should work with exactly 50)
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/sma?period=50")
        assert response.status_code == 200
        data = response.json()
        assert data["period"] == 50
        assert len(data["values"]) == 1  # Only 1 value with exactly 50 records


def test_sma_insufficient_data(stock_id):
    """Test SMA with insufficient data."""
    client = TestClient(app)
    with client:
        # Only insert 5 price records
        _insert_prices(client, stock_id, num_days=5)

        # SMA 20 should fail with insufficient data
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/sma?period=20")
        assert response.status_code == 422


def test_sma_invalid_period(stock_id):
    """Test SMA with invalid period."""
    client = TestClient(app)
    with client:
        # Insert 5 records
        _insert_prices(client, stock_id, num_days=5)

        # Period <= 0 should fail
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/sma?period=0")
        assert response.status_code == 422

        # Negative period should fail
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/sma?period=-5")
        assert response.status_code == 422


# --- EMA Tests ---

def test_ema_calculation(stock_id):
    """Test EMA calculation."""
    client = TestClient(app)
    with client:
        _insert_prices(client, stock_id, num_days=30)

        # Test EMA 12
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/ema?period=12")
        assert response.status_code == 200
        data = response.json()
        assert data["period"] == 12
        assert len(data["values"]) == 19  # 30 - 12 + 1

        # Test EMA 26
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/ema?period=26")
        assert response.status_code == 200
        data = response.json()
        assert data["period"] == 26
        assert len(data["values"]) == 5  # 30 - 26 + 1


# --- RSI Tests ---

def test_rsi_calculation(stock_id):
    """Test RSI calculation."""
    client = TestClient(app)
    with client:
        _insert_prices(client, stock_id, num_days=40)

        # Test RSI 14
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/rsi?period=14")
        assert response.status_code == 200
        data = response.json()
        assert data["period"] == 14
        # RSI should be between 0 and 100
        for point in data["values"]:
            val = point["value"]
            # Handle both Decimal and float/int types
            numeric_val = float(val) if not isinstance(val, (int, float)) else val
            assert 0 <= numeric_val <= 100


# --- MACD Tests ---

def test_macd_calculation(stock_id):
    """Test MACD calculation."""
    client = TestClient(app)
    with client:
        _insert_prices(client, stock_id, num_days=60)

        # Test MACD
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/macd?fast=12&slow=26&signal=9")
        assert response.status_code == 200
        data = response.json()
        assert data["fast_period"] == 12
        assert data["slow_period"] == 26
        assert data["signal_period"] == 9
        # All series should have values - macd/signal/histogram are lists of TechnicalIndicatorPoint
        assert len(data["macd"]) > 0
        assert len(data["signal"]) > 0
        assert len(data["histogram"]) > 0


def test_macd_invalid_config(stock_id):
    """Test MACD with invalid configuration."""
    client = TestClient(app)
    with client:
        # fast >= slow should fail
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/macd?fast=26&slow=12&signal=9")
        assert response.status_code == 422


# --- Volatility Tests ---

def test_volatility_calculation(stock_id):
    """Test volatility calculation."""
    client = TestClient(app)
    with client:
        _insert_prices(client, stock_id, num_days=50)

        # Test volatility
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/volatility?period=20")
        assert response.status_code == 200
        data = response.json()
        assert data["period"] == 20
        # Volatility should be positive
        for point in data["values"]:
            val = point["value"]
            numeric_val = float(val) if not isinstance(val, (int, float)) else val
            assert numeric_val > 0


# --- Support/Resistance Tests ---

def test_support_resistance_calculation(stock_id):
    """Test support/resistance calculation."""
    client = TestClient(app)
    with client:
        _insert_prices(client, stock_id, num_days=50)

        # Test support/resistance
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/support-resistance")
        assert response.status_code == 200
        data = response.json()
        assert "support_levels" in data
        assert "resistance_levels" in data


# --- Consolidated Analytics Tests ---

def test_consolidated_analytics(stock_id):
    """Test consolidated analytics endpoint."""
    client = TestClient(app)
    with client:
        _insert_prices(client, stock_id, num_days=60)

        # Test consolidated endpoint
        response = client.get(
            f"/api/v1/stocks/{stock_id}/analytics?"
            f"sma_periods=20&50&ema_periods=12&26&rsi_period=14&"
            f"macd_fast=12&macd_slow=26&macd_signal=9&volatility_period=20"
        )
        assert response.status_code == 200
        data = response.json()

        # Verify structure
        assert data["stock_id"] == stock_id
        assert data["symbol"] is not None
        assert data["latest_price"] is not None
        assert data["analysis_start"] is not None
        assert data["analysis_end"] is not None
        assert "returns" in data
        assert "sma" in data
        assert "ema" in data
        assert "rsi" in data
        assert "macd" in data
        assert "volatility" in data
        assert "support_resistance" in data


def test_consolidated_insufficient_data(stock_id):
    """Test consolidated analytics with insufficient data."""
    client = TestClient(app)
    with client:
        # Insert only 5 records
        _insert_prices(client, stock_id, num_days=5)

        # Should fail with validation error
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics")
        assert response.status_code == 422


# --- Nonexistent Stock Tests ---

def test_nonexistent_stock():
    """Test analytics with nonexistent stock."""
    client = TestClient(app)
    with client:
        response = client.get("/api/v1/stocks/999999/analytics")
        assert response.status_code == 404


def test_nonexistent_stock_returns():
    """Test returns endpoint with nonexistent stock."""
    client = TestClient(app)
    with client:
        response = client.get("/api/v1/stocks/999999/analytics/returns")
        assert response.status_code == 404


def test_nonexistent_stock_sma():
    """Test SMA endpoint with nonexistent stock."""
    client = TestClient(app)
    with client:
        response = client.get("/api/v1/stocks/999999/analytics/sma?period=20")
        assert response.status_code == 404


# --- Bollinger Bands / Volume Tests (consumed by the Phase 7 signal engine) ---

def test_bollinger_bands_calculation(stock_id):
    """Bollinger bands are returned in ascending order around the middle band."""
    client = TestClient(app)
    with client:
        _insert_prices(client, stock_id, num_days=40)

        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/bollinger-bands?period=20")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["period"] == 20
        lower = float(data["lower_band"])
        middle = float(data["middle_band"])
        upper = float(data["upper_band"])
        assert lower < middle < upper


def test_bollinger_bands_insufficient_data(stock_id):
    """Bollinger bands require the full period of records."""
    client = TestClient(app)
    with client:
        _insert_prices(client, stock_id, num_days=5)
        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/bollinger-bands?period=20")
        assert response.status_code == 422


def test_volume_average_calculation(stock_id):
    """Volume average reports the latest volume and the ratio to the mean."""
    client = TestClient(app)
    with client:
        _insert_prices(client, stock_id, num_days=30)

        response = client.get(f"/api/v1/stocks/{stock_id}/analytics/volume?period=20")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["period"] == 20
        assert int(data["latest_volume"]) == 1000000
        assert float(data["average_volume"]) == 1000000.0
        assert float(data["ratio"]) == 1.0