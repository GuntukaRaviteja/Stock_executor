from unittest.mock import patch
from fastapi.testclient import TestClient


def test_health_endpoint_success(client: TestClient):
    """Test 1: Health endpoint returns 200 and database connected."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["database"] == "connected"


def test_health_endpoint_db_failure(client: TestClient):
    """Test health endpoint returns 503 when database is unreachable."""
    with patch("app.main.check_database_health", return_value=(False, "Connection refused")):
        response = client.get("/health")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "error"
        assert data["database"] == "disconnected"
        assert "Connection refused" in data["detail"]
