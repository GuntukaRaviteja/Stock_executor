from fastapi.testclient import TestClient


def create_test_stock(client: TestClient, symbol: str = "COMP") -> int:
    res = client.post(
        "/api/v1/stocks",
        json={"symbol": symbol, "company_name": f"{symbol} Technologies", "exchange": "NASDAQ"},
    )
    return res.json()["id"]


def test_company_profile_crud(client: TestClient):
    """Test retrieving and updating company background information."""
    stock_id = create_test_stock(client, "CDEV")

    # Fetch initial company profile
    res1 = client.get(f"/api/v1/companies/{stock_id}")
    assert res1.status_code == 200
    assert res1.json()["stock_id"] == stock_id
    assert res1.json()["symbol"] == "CDEV"

    # Update company details
    update_payload = {
        "sector": "Information Technology",
        "industry": "Enterprise Software",
        "ceo": "Jane Doe",
        "employees": 5000,
        "market_cap": 25000000000.00,
    }
    update_res = client.put(f"/api/v1/companies/{stock_id}", json=update_payload)
    assert update_res.status_code == 200
    data = update_res.json()
    assert data["sector"] == "Information Technology"
    assert data["ceo"] == "Jane Doe"
    assert data["employees"] == 5000

    # Fetch by symbol
    sym_res = client.get("/api/v1/companies/symbol/cdev")
    assert sym_res.status_code == 200
    assert sym_res.json()["ceo"] == "Jane Doe"
