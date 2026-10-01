from fastapi.testclient import TestClient


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
