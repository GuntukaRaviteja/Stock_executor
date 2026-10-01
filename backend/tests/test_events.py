from fastapi.testclient import TestClient


def create_test_stock(client: TestClient, symbol: str = "AAPL") -> int:
    res = client.post(
        "/api/v1/stocks",
        json={"symbol": symbol, "company_name": f"{symbol} Inc", "exchange": "NASDAQ"},
    )
    return res.json()["id"]


def test_event_creation(client: TestClient):
    """Test 10: Event creation."""
    stock_id = create_test_stock(client, "AAPL")

    payload = {
        "event_type": "earnings",
        "event_title": "AAPL Q4 Earnings Beat",
        "event_description": "Apple beats Q4 revenue and EPS expectations.",
        "event_date": "2026-09-20",
        "impact": "positive",
    }
    response = client.post(f"/api/v1/stocks/{stock_id}/events", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["stock_id"] == stock_id
    assert data["event_type"] == "EARNINGS"  # Normalized to uppercase
    assert data["event_title"] == "AAPL Q4 Earnings Beat"
    assert data["impact"] == "POSITIVE"


def test_event_retrieval(client: TestClient):
    """Test 11: Event retrieval for stock and recent events across all stocks."""
    stock_id = create_test_stock(client, "MSFT")

    # Add 2 events
    client.post(
        f"/api/v1/stocks/{stock_id}/events",
        json={
            "event_type": "PRODUCT",
            "event_title": "Microsoft Copilot 2.0 Released",
            "event_date": "2026-09-10",
        },
    )
    client.post(
        f"/api/v1/stocks/{stock_id}/events",
        json={
            "event_type": "DIVIDEND",
            "event_title": "Quarterly Dividend Declared",
            "event_date": "2026-09-15",
        },
    )

    # Test retrieval by stock
    res = client.get(f"/api/v1/stocks/{stock_id}/events")
    assert res.status_code == 200
    assert len(res.json()) == 2

    # Test recent events endpoint
    recent_res = client.get("/api/v1/events/recent")
    assert recent_res.status_code == 200
    events = recent_res.json()
    assert len(events) >= 2
    assert any(e["event_title"] == "Microsoft Copilot 2.0 Released" for e in events)


def test_duplicate_event_rejection(client: TestClient):
    """Test duplicate event handling returns 409 Conflict."""
    stock_id = create_test_stock(client, "TSLA")

    payload = {
        "event_type": "REGULATORY",
        "event_title": "EPA Approval Granted",
        "event_date": "2026-09-22",
    }
    res1 = client.post(f"/api/v1/stocks/{stock_id}/events", json=payload)
    assert res1.status_code == 201

    res2 = client.post(f"/api/v1/stocks/{stock_id}/events", json=payload)
    assert res2.status_code == 409
    data = res2.json()
    assert data["success"] is False
    assert data["error"]["code"] == "DUPLICATE_EVENT"
