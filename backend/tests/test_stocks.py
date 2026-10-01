from fastapi.testclient import TestClient


def test_create_stock(client: TestClient):
    """Test 3: Create stock successfully."""
    payload = {
        "symbol": "aapl",
        "company_name": "Apple Inc.",
        "exchange": "nasdaq",
    }
    response = client.post("/api/v1/stocks", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["symbol"] == "AAPL"  # Normalized to uppercase
    assert data["company_name"] == "Apple Inc."
    assert data["exchange"] == "NASDAQ"
    assert "id" in data


def test_get_stock_by_id(client: TestClient):
    """Test 4: Get stock by ID."""
    # Create stock first
    res = client.post(
        "/api/v1/stocks",
        json={"symbol": "MSFT", "company_name": "Microsoft Corp", "exchange": "NASDAQ"},
    )
    stock_id = res.json()["id"]

    response = client.get(f"/api/v1/stocks/{stock_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == stock_id
    assert data["symbol"] == "MSFT"


def test_get_stock_by_symbol(client: TestClient):
    """Test 5: Get stock by ticker symbol."""
    client.post(
        "/api/v1/stocks",
        json={"symbol": "GOOGL", "company_name": "Alphabet Inc.", "exchange": "NASDAQ"},
    )

    response = client.get("/api/v1/stocks/symbol/googl")
    assert response.status_code == 200
    assert response.json()["symbol"] == "GOOGL"


def test_duplicate_stock_handling(client: TestClient):
    """Test 6: Duplicate stock insertion returns 409 Conflict."""
    client.post(
        "/api/v1/stocks",
        json={"symbol": "NVDA", "company_name": "NVIDIA", "exchange": "NASDAQ"},
    )

    # Attempt duplicate create
    response = client.post(
        "/api/v1/stocks",
        json={"symbol": "nvda", "company_name": "NVIDIA Duplicate", "exchange": "NASDAQ"},
    )
    assert response.status_code == 409
    data = response.json()
    assert data["success"] is False
    assert data["error"]["code"] == "DUPLICATE_STOCK"


def test_stock_not_found(client: TestClient):
    """Test 13: Non-existent stock returns 404."""
    response = client.get("/api/v1/stocks/999999")
    assert response.status_code == 404
    data = response.json()
    assert data["success"] is False
    assert data["error"]["code"] == "STOCK_NOT_FOUND"


def test_invalid_input_create_stock(client: TestClient):
    """Test 12: Invalid input validation returns 422 Unprocessable Entity."""
    # Missing required company_name and empty symbol
    response = client.post("/api/v1/stocks", json={"symbol": ""})
    assert response.status_code == 422
    data = response.json()
    assert data["success"] is False
    assert data["error"]["code"] == "VALIDATION_ERROR"


def test_update_stock(client: TestClient):
    """Test updating stock company name."""
    res = client.post(
        "/api/v1/stocks",
        json={"symbol": "TSLA", "company_name": "Tesla Motors", "exchange": "NASDAQ"},
    )
    stock_id = res.json()["id"]

    update_res = client.put(
        f"/api/v1/stocks/{stock_id}",
        json={"company_name": "Tesla, Inc."},
    )
    assert update_res.status_code == 200
    assert update_res.json()["company_name"] == "Tesla, Inc."


def test_delete_stock(client: TestClient):
    """Test deleting stock returns 204."""
    res = client.post(
        "/api/v1/stocks",
        json={"symbol": "TEMP", "company_name": "Temporary Stock", "exchange": "NYSE"},
    )
    stock_id = res.json()["id"]

    del_res = client.delete(f"/api/v1/stocks/{stock_id}")
    assert del_res.status_code == 204

    # Verify not found
    get_res = client.get(f"/api/v1/stocks/{stock_id}")
    assert get_res.status_code == 404
