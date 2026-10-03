"""Phase 12 API tests — snapshots, performance history and every portfolio endpoint.

Numbered coverage in this file:

* 52-55 snapshots: creation, historical retrieval, performance history, determinism
* 56-63 endpoints: create, retrieve, positions, trade, risk check, valuation, metrics,
  snapshots
* 64 error responses: structured payloads, never a stack trace or a raw database error

These run against the real FastAPI app with an isolated SQLite database, so the whole stack is
exercised: router -> service -> repositories -> models.

Money fields are serialised by the API as strings, so every amount is read through
:func:`dec` — a value never round-trips through a float.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.stock import Stock

BASE = "/api/v1/portfolios"


def dec(value) -> Decimal:
    """Read a money field out of a JSON body without passing it through a float."""
    return Decimal(str(value))


#: Keys that necessarily differ between two runs of the same script: the primary keys and the
#: wall-clock timestamps. Everything else — every money figure, every count, every percentage —
#: must be identical, and that is what :func:`_without_identity` leaves behind.
_IDENTITY_KEYS = {
    "portfolio_id",
    "position_id",
    "snapshot_id",
    "order_id",
    "trade_id",
    "transaction_id",
    "as_of",
    "created_at",
    "updated_at",
    "opened_at",
    "executed_at",
    "evaluated_at",
    "timestamp",
}


def _without_identity(payload):
    """Drop the keys listed in :data:`_IDENTITY_KEYS` at any depth."""
    if isinstance(payload, dict):
        return {
            key: _without_identity(value)
            for key, value in payload.items()
            if key not in _IDENTITY_KEYS
        }
    if isinstance(payload, list):
        return [_without_identity(item) for item in payload]
    return payload


@pytest.fixture
def stock(db_session: Session) -> Stock:
    record = Stock(symbol="APICO", company_name="Api Co", exchange="NSE", market="INDIA")
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


@pytest.fixture
def other_stock(db_session: Session) -> Stock:
    record = Stock(symbol="APICO2", company_name="Api Co Two", exchange="NSE", market="INDIA")
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


def no_fee_payload(name="API Portfolio", capital="100000", **overrides) -> dict:
    """A portfolio-creation body with fees and slippage switched off, so every asserted amount
    is exactly the stated arithmetic."""
    payload = {
        "name": name,
        "initial_capital": capital,
        "trading_costs": {"commission_rate": "0", "min_fee": "0", "slippage_rate": "0"},
    }
    payload.update(overrides)
    return payload


def create(client: TestClient, name="API Portfolio", capital="100000", **overrides) -> dict:
    response = client.post(BASE, json=no_fee_payload(name=name, capital=capital, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def buy(client: TestClient, portfolio_id: int, stock_id: int, quantity: int, price: str):
    return client.post(
        f"{BASE}/{portfolio_id}/orders",
        json={"stock_id": stock_id, "side": "BUY", "quantity": quantity, "requested_price": price},
    )


def sell(client: TestClient, portfolio_id: int, stock_id: int, quantity: int, price: str):
    return client.post(
        f"{BASE}/{portfolio_id}/orders",
        json={"stock_id": stock_id, "side": "SELL", "quantity": quantity, "requested_price": price},
    )


# ======================================================================
# 56-57  Portfolio creation and retrieval
# ======================================================================


def test_56_portfolio_creation_endpoint(client: TestClient):
    """56. POST /portfolios creates a flat portfolio and returns its summary."""
    response = client.post(
        BASE,
        json=no_fee_payload(
            name="API Portfolio",
            description="Created over HTTP",
            initial_capital="250000",
            currency="USD",
        ),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["portfolio_id"] > 0
    assert body["name"] == "API Portfolio"
    assert body["status"] == "ACTIVE"
    assert body["trading_mode"] == "PAPER"
    assert dec(body["initial_capital"]) == Decimal("250000")
    assert dec(body["current_cash"]) == Decimal("250000")
    assert dec(body["equity"]) == Decimal("250000")
    assert dec(body["total_pnl"]) == Decimal("0")
    assert dec(body["return_pct"]) == Decimal("0")
    assert body["position_count"] == 0
    assert body["currency"] == "USD"
    assert body["disclaimer"]


def test_56b_portfolio_defaults_to_the_configured_currency(client: TestClient):
    """56. A portfolio created without a currency gets the application default."""
    body = create(client)

    assert body["currency"] == "INR"


@pytest.mark.parametrize("capital", ["0", "-100", "-0.01"])
def test_56c_portfolio_creation_rejects_non_positive_capital(client: TestClient, capital):
    """56/2/3. Zero and negative capital never reach the service."""
    response = client.post(BASE, json={"name": "Bad", "initial_capital": capital})

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "initial_capital" in body["error"]["message"]


def test_56d_portfolio_creation_rejects_a_missing_name(client: TestClient):
    """56. The name is required and bounded."""
    assert client.post(BASE, json={"initial_capital": "1000"}).status_code == 422
    assert (
        client.post(BASE, json={"name": "", "initial_capital": "1000"}).status_code == 422
    )
    assert (
        client.post(BASE, json={"name": "x" * 200, "initial_capital": "1000"}).status_code
        == 422
    )


def test_56e_portfolio_creation_rejects_an_invalid_currency(client: TestClient):
    """56. The currency must be a three-letter code."""
    response = client.post(
        BASE, json={"name": "Bad currency", "initial_capital": "1000", "currency": "RUPEES"}
    )

    assert response.status_code == 422
    assert "currency" in response.json()["error"]["message"]


def test_56f_portfolio_creation_rejects_invalid_risk_configuration(client: TestClient):
    """56. A limit that cannot mean anything is refused at creation."""
    for limits in (
        {"max_position_allocation_pct": "0"},
        {"max_position_allocation_pct": "150"},
        {"max_daily_loss_pct": "-1"},
        {"min_trade_value": "0"},
        {"max_positions": 0},
        {"commission_rate": "-0.1"},
    ):
        payload = no_fee_payload(name="Bad limits", initial_capital="1000")
        if "commission_rate" in limits:
            payload["trading_costs"] = limits
        else:
            payload["risk_limits"] = limits
        response = client.post(BASE, json=payload)
        assert response.status_code == 422, limits


def test_57_portfolio_retrieval(client: TestClient):
    """57. GET /portfolios lists, and GET /{id} retrieves one portfolio."""
    first = create(client, name="First")
    second = create(client, name="Second", initial_capital="50000")

    listing = client.get(BASE)
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] == 2
    names = {row["name"] for row in body["data"]}
    assert names == {"First", "Second"}

    one = client.get(f"{BASE}/{first['portfolio_id']}")
    assert one.status_code == 200
    assert one.json()["name"] == "First"
    assert dec(one.json()["current_cash"]) == Decimal("100000")

    assert second["portfolio_id"] != first["portfolio_id"]


def test_57b_lifecycle_endpoints_pause_and_close(client: TestClient):
    """57. POST /pause and POST /close drive the documented lifecycle."""
    portfolio = create(client)

    paused = client.post(f"{BASE}/{portfolio['portfolio_id']}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "PAUSED"

    closed = client.post(f"{BASE}/{portfolio['portfolio_id']}/close")
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "CLOSED"

    # Re-closing is safe, and a closed portfolio cannot be paused.
    assert client.post(f"{BASE}/{portfolio['portfolio_id']}/close").status_code == 200
    refused = client.post(f"{BASE}/{portfolio['portfolio_id']}/pause")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "PORTFOLIO_INACTIVE"


def test_57c_update_endpoint(client: TestClient):
    """57. PATCH /{id} partially updates name, description and limits."""
    portfolio = create(client, name="Before")

    response = client.patch(
        f"{BASE}/{portfolio['portfolio_id']}",
        json={"description": "After", "risk_limits": {"max_positions": 2}},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Before"
    assert body["description"] == "After"
    assert body["risk_limits"]["max_positions"] == 2


# ======================================================================
# 58-59  Positions and trades
# ======================================================================


def test_58_position_endpoints(client: TestClient, stock):
    """58. GET /positions lists, and GET /positions/{stock_id} retrieves one."""
    portfolio = create(client)
    buy(client, portfolio["portfolio_id"], stock.id, 100, "50")

    listing = client.get(f"{BASE}/{portfolio['portfolio_id']}/positions")
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] == 1
    position = body["data"][0]
    assert position["stock_id"] == stock.id
    assert position["quantity"] == 100
    assert dec(position["average_entry_price"]) == Decimal("50")
    assert dec(position["market_value"]) == Decimal("5000")
    assert dec(position["unrealized_pnl"]) == Decimal("0")

    single = client.get(f"{BASE}/{portfolio['portfolio_id']}/positions/{stock.id}")
    assert single.status_code == 200
    assert single.json()["stock_id"] == stock.id
    assert single.json()["position_id"] == position["position_id"]
    assert dec(single.json()["market_value"]) == Decimal("5000")


def test_58b_position_endpoint_filters_by_stock(client: TestClient, stock, other_stock):
    """58. The list can be narrowed to one stock."""
    portfolio = create(client)
    buy(client, portfolio["portfolio_id"], stock.id, 10, "50")
    buy(client, portfolio["portfolio_id"], other_stock.id, 5, "20")

    filtered = client.get(
        f"{BASE}/{portfolio['portfolio_id']}/positions", params={"stock_id": other_stock.id}
    )

    assert filtered.json()["count"] == 1
    assert filtered.json()["data"][0]["stock_id"] == other_stock.id


def test_59_trade_endpoint(client: TestClient, stock):
    """59. An order fills, appears in /trades, and returns the derived figures."""
    portfolio = create(client, initial_capital="100000")

    placed = buy(client, portfolio["portfolio_id"], stock.id, 100, "50")

    assert placed.status_code == 201, placed.text
    body = placed.json()
    assert body["order"]["status"] == "FILLED"
    assert body["trade"]["side"] == "BUY"
    assert dec(body["trade"]["execution_price"]) == Decimal("50.0000")
    assert dec(body["trade"]["gross_value"]) == Decimal("5000.00")
    assert dec(body["trade"]["fees"]) == Decimal("0.00")

    history = client.get(f"{BASE}/{portfolio['portfolio_id']}/trades")
    assert history.status_code == 200
    assert history.json()["count"] == 1
    assert history.json()["data"][0]["symbol"] == stock.symbol


def test_59b_sell_endpoint_books_realized_pnl(client: TestClient, stock):
    """59. A sell returns cash and books realised P&L."""
    portfolio = create(client)
    buy(client, portfolio["portfolio_id"], stock.id, 100, "50")

    sold = sell(client, portfolio["portfolio_id"], stock.id, 40, "60")

    assert sold.status_code == 201, sold.text
    assert dec(sold.json()["trade"]["realized_pnl"]) == Decimal("400.00")

    summary = client.get(f"{BASE}/{portfolio['portfolio_id']}").json()
    assert dec(summary["realized_pnl"]) == Decimal("400.00")
    # 100000 - 5000 spent, then 40 * 60 = 2400 returned.
    assert dec(summary["current_cash"]) == Decimal("97400")


def test_59c_order_history_records_rejections_with_a_code(client: TestClient, stock):
    """59. A rejected order is still recorded, with its machine-readable code."""
    portfolio = create(
        client, initial_capital="100000", risk_limits={"max_position_allocation_pct": "10"}
    )

    refused = buy(client, portfolio["portfolio_id"], stock.id, 500, "50")

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "MAX_POSITION_ALLOCATION_EXCEEDED"

    orders = client.get(f"{BASE}/{portfolio['portfolio_id']}/orders").json()
    assert orders["count"] == 1
    assert orders["data"][0]["status"] == "REJECTED"
    assert orders["data"][0]["rejection_code"] == "MAX_POSITION_ALLOCATION_EXCEEDED"


# ======================================================================
# 60  Risk check
# ======================================================================


def test_60_risk_check_endpoint_approves(client: TestClient, stock):
    """60. A trade inside every limit is approved, with the numbers that decided it."""
    portfolio = create(
        client, risk_limits={"max_position_allocation_pct": "25", "max_exposure_pct": "50"}
    )

    response = client.post(
        f"{BASE}/{portfolio['portfolio_id']}/risk/check",
        json={"stock_id": stock.id, "side": "BUY", "quantity": 200, "requested_price": "100"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["rejection_code"] is None
    assert body["violations"] == []
    assert dec(body["equity"]) == Decimal("100000")
    assert dec(body["cash"]) == Decimal("100000")
    assert dec(body["exposure_pct"]) == Decimal("0.0000")
    assert dec(body["evaluation_price"]) == Decimal("100.0000")
    assert body["position_count"] == 0


def test_60b_risk_check_endpoint_rejects_with_every_breached_rule(client: TestClient, stock):
    """60. A refused trade reports all the rules it breaks, not only the first."""
    portfolio = create(
        client,
        initial_capital="100000",
        risk_limits={
            "max_position_allocation_pct": "10",
            "max_exposure_pct": "15",
            # Above the 400 * 100 order, so the undersized-order rule reports alongside the others.
            "min_trade_value": "50000",
        },
    )

    response = client.post(
        f"{BASE}/{portfolio['portfolio_id']}/risk/check",
        json={"stock_id": stock.id, "side": "BUY", "quantity": 400, "requested_price": "100"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is False
    # The response carries the first breach as the headline code and every breach as a violation.
    assert body["rejection_code"] == "MIN_TRADE_VALUE_BELOW"
    breached = {violation["rule"] for violation in body["violations"]}
    assert {"max_position_allocation_pct", "max_exposure_pct", "min_trade_value"} <= breached
    # Every violation explains itself with the numbers behind the breach.
    for violation in body["violations"]:
        assert violation["rule"] and violation["reason"]
        assert violation["details"]


def test_60c_risk_check_reports_the_daily_loss_rule(client: TestClient, stock, db_session):
    """60. The Phase 12 daily-loss rule appears in the pre-trade check."""
    portfolio = create(client, risk_limits={"max_daily_loss_pct": "5"})
    buy(client, portfolio["portfolio_id"], stock.id, 800, "100")
    # A snapshot dated yesterday sets the day's opening equity.
    client.post(
        f"{BASE}/{portfolio['portfolio_id']}/snapshots",
        json={"as_of": (datetime.utcnow() - timedelta(days=1)).isoformat()},
    )
    client.post(
        f"{BASE}/{portfolio['portfolio_id']}/valuation",
        json={"prices": [{"stock_id": stock.id, "price": "87.5"}]},
    )

    response = client.post(
        f"{BASE}/{portfolio['portfolio_id']}/risk/check",
        json={"stock_id": stock.id, "side": "BUY", "quantity": 1, "requested_price": "87.5"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is False
    daily = next(v for v in body["violations"] if v["rule"] == "max_daily_loss_pct")
    assert daily["details"]["day_open_equity"] == "100000.00"
    assert dec(daily["details"]["daily_loss_pct"]) == Decimal("10.0000")


def test_60d_risk_check_never_mutates(client: TestClient, stock):
    """60. A risk check changes nothing on the portfolio."""
    portfolio = create(client, risk_limits={"max_position_allocation_pct": "10"})
    buy(client, portfolio["portfolio_id"], stock.id, 100, "50")
    before = client.get(f"{BASE}/{portfolio['portfolio_id']}").json()

    client.post(
        f"{BASE}/{portfolio['portfolio_id']}/risk/check",
        json={"stock_id": stock.id, "side": "BUY", "quantity": 9999, "requested_price": "50"},
    )

    after = client.get(f"{BASE}/{portfolio['portfolio_id']}").json()
    assert dec(after["current_cash"]) == dec(before["current_cash"])
    assert after["trade_count"] == before["trade_count"]
    assert after["order_count"] == before["order_count"]


def test_60e_risk_check_requires_a_price(client: TestClient, stock):
    """60. A check must say at what price to evaluate the trade."""
    portfolio = create(client)

    response = client.post(
        f"{BASE}/{portfolio['portfolio_id']}/risk/check",
        json={"stock_id": stock.id, "side": "BUY", "quantity": 1},
    )

    assert response.status_code == 422


# ======================================================================
# 61-62  Valuation and metrics
# ======================================================================


def test_61_valuation_endpoints(client: TestClient, stock):
    """61. POST /valuation marks at supplied prices; GET /valuation reads the stored marks."""
    portfolio = create(client)
    buy(client, portfolio["portfolio_id"], stock.id, 100, "50")

    marked = client.post(
        f"{BASE}/{portfolio['portfolio_id']}/valuation",
        json={"prices": [{"stock_id": stock.id, "price": "60"}]},
    )

    assert marked.status_code == 200, marked.text
    body = marked.json()
    assert dec(body["current_cash"]) == Decimal("95000")
    assert dec(body["market_value"]) == Decimal("6000")
    assert dec(body["equity"]) == Decimal("101000")
    assert dec(body["unrealized_pnl"]) == Decimal("1000")
    assert dec(body["total_pnl"]) == Decimal("1000")
    assert dec(body["return_pct"]) == Decimal("1.0000")
    # The valuation response reports per-position weights rather than a portfolio exposure figure.
    assert dec(body["positions"][0]["allocation_pct"]) == Decimal("5.9406")

    summary = client.get(f"{BASE}/{portfolio['portfolio_id']}").json()
    assert dec(summary["exposure_pct"]) == Decimal("5.9406")

    read_back = client.get(f"{BASE}/{portfolio['portfolio_id']}/valuation")
    assert read_back.status_code == 200
    assert dec(read_back.json()["equity"]) == dec(body["equity"])


def test_62_metrics_endpoint(client: TestClient, stock):
    """62. GET /performance reports P&L, activity, exposure and the daily figures."""
    portfolio = create(client)
    buy(client, portfolio["portfolio_id"], stock.id, 100, "50")
    sell(client, portfolio["portfolio_id"], stock.id, 40, "60")

    response = client.get(f"{BASE}/{portfolio['portfolio_id']}/performance")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["portfolio_id"] == portfolio["portfolio_id"]
    # Cash 97400 plus the remaining 60 shares marked at the 60 sale price.
    assert dec(body["realized_pnl"]) == Decimal("400.00")
    assert dec(body["unrealized_pnl"]) == Decimal("600.00")
    assert dec(body["equity"]) == Decimal("101000")
    assert dec(body["total_pnl"]) == Decimal("1000")
    assert dec(body["return_pct"]) == Decimal("1.0000")
    assert body["order_count"] == 2
    assert body["trade_count"] == 2
    assert body["closed_trades"] == 1
    assert body["winning_trades"] == 1
    assert dec(body["average_winning_trade"]) == Decimal("400.00")
    assert dec(body["win_rate_pct"]) == Decimal("100.0000")
    assert body["currency"] == "INR"
    # The Phase 12 daily figures are present and consistent. Day-open equity is the opening
    # snapshot, 100000, and the day's P&L is the 1000 the round trip plus marking produced.
    assert dec(body["day_open_equity"]) == Decimal("100000")
    assert dec(body["daily_pnl"]) == Decimal("1000")
    assert dec(body["daily_return_pct"]) == Decimal("1.0000")
    # Metrics that need more history say so instead of reporting a zero.
    assert body["sharpe_ratio"] is None
    assert isinstance(body["insufficient_data"], list) and body["insufficient_data"]


def test_62b_metrics_report_null_rather_than_zero_without_history(client: TestClient):
    """62. No trades and no snapshots means no win rate, no Sharpe, no profit factor."""
    portfolio = create(client)

    body = client.get(f"{BASE}/{portfolio['portfolio_id']}/performance").json()

    assert body["trade_count"] == 0
    assert body["win_rate_pct"] is None
    assert body["profit_factor"] is None
    assert body["annualized_volatility_pct"] is None
    assert body["sharpe_ratio"] is None
    assert body["sortino_ratio"] is None
    assert body["cagr_pct"] is None
    assert body["insufficient_data"]


def test_63_snapshots_endpoint_lists_the_persisted_curve(client: TestClient, stock):
    """63. GET /snapshots returns the persisted equity curve, oldest first and paginated."""
    portfolio = create(client)
    portfolio_id = portfolio["portfolio_id"]
    buy(client, portfolio_id, stock.id, 100, "50")

    # Three points on the curve: flat, marked down, marked back up.
    client.post(f"{BASE}/{portfolio_id}/snapshots", json={})
    client.post(
        f"{BASE}/{portfolio_id}/valuation",
        json={"prices": [{"stock_id": stock.id, "price": "40"}]},
    )
    client.post(f"{BASE}/{portfolio_id}/snapshots", json={})
    client.post(
        f"{BASE}/{portfolio_id}/valuation",
        json={"prices": [{"stock_id": stock.id, "price": "50"}]},
    )
    client.post(f"{BASE}/{portfolio_id}/snapshots", json={})

    everything = client.get(f"{BASE}/{portfolio_id}/snapshots")
    assert everything.status_code == 200, everything.text
    body = everything.json()
    assert body["portfolio_id"] == portfolio_id
    assert body["success"] is True
    assert body["count"] == body["total"] == 3

    # Oldest first, so the curve reads forwards without the client sorting it.
    # Cash is 95000 throughout; the 100 shares are what move.
    equities = [dec(point["equity"]) for point in body["data"]]
    assert equities == [Decimal("100000.00"), Decimal("99000.00"), Decimal("100000.00")]
    market_values = [dec(point["market_value"]) for point in body["data"]]
    assert market_values == [Decimal("5000.00"), Decimal("4000.00"), Decimal("5000.00")]
    # Drawdown is signed and measured from the running peak, so only the middle point is in
    # one, and it reads as a negative percentage of the 100000 peak.
    assert [dec(point["drawdown_pct"]) for point in body["data"]] == [
        Decimal("0.0000"),
        Decimal("-1.0000"),
        Decimal("0.0000"),
    ]
    timestamps = [point["timestamp"] for point in body["data"]]
    assert timestamps == sorted(timestamps)

    # Every snapshot is a complete, self-contained record rather than a delta.
    for point in body["data"]:
        assert set(point) >= {"snapshot_id", "timestamp", "cash", "equity", "drawdown_pct"}
        assert dec(point["cash"]) == Decimal("95000.00")

    # `total` counts the whole curve while `count` counts the page, so a client can page.
    page = client.get(f"{BASE}/{portfolio_id}/snapshots?skip=1&limit=1")
    assert page.status_code == 200, page.text
    paged = page.json()
    assert paged["count"] == 1
    assert paged["total"] == 3
    assert dec(paged["data"][0]["equity"]) == Decimal("99000.00")

    # A snapshot survives a read: it is persisted history, not a recomputation.
    assert dec(client.get(f"{BASE}/{portfolio_id}/snapshots").json()["data"][0]["equity"]) == (
        Decimal("100000.00")
    )


def test_62c_drawdown_endpoint(client: TestClient, stock):
    """62. GET /drawdown reports the curve and its extremes."""
    portfolio = create(client)
    buy(client, portfolio["portfolio_id"], stock.id, 100, "50")
    client.post(f"{BASE}/{portfolio['portfolio_id']}/snapshots", json={})
    client.post(
        f"{BASE}/{portfolio['portfolio_id']}/valuation",
        json={"prices": [{"stock_id": stock.id, "price": "40"}]},
    )
    client.post(f"{BASE}/{portfolio['portfolio_id']}/snapshots", json={})

    body = client.get(f"{BASE}/{portfolio['portfolio_id']}/drawdown").json()

    assert body["peak_equity"] is not None
    assert dec(body["max_drawdown"]) == Decimal("1000.00")
    assert dec(body["max_drawdown_pct"]) == Decimal("1.0000")
    assert len(body["points"]) == 3  # the synthetic opening point plus two snapshots


# ======================================================================
# 52-55  Snapshots and performance history
# ======================================================================


def test_52_snapshot_creation(client: TestClient, stock):
    """52. POST /snapshots captures every figure the equity curve needs."""
    portfolio = create(client)
    buy(client, portfolio["portfolio_id"], stock.id, 100, "50")

    response = client.post(
        f"{BASE}/{portfolio['portfolio_id']}/snapshots",
        json={"prices": [{"stock_id": stock.id, "price": "60"}]},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert dec(body["cash"]) == Decimal("95000")
    assert dec(body["market_value"]) == Decimal("6000")
    assert dec(body["equity"]) == Decimal("101000")
    assert dec(body["realized_pnl"]) == Decimal("0")
    assert dec(body["unrealized_pnl"]) == Decimal("1000")
    assert dec(body["total_pnl"]) == Decimal("1000")
    assert dec(body["return_pct"]) == Decimal("1.0000")
    assert dec(body["exposure_pct"]) == Decimal("5.9406")
    assert body["timestamp"]


def test_53_snapshot_historical_retrieval(client: TestClient, stock):
    """53. GET /snapshots returns the curve oldest first, with a total count."""
    portfolio = create(client)
    buy(client, portfolio["portfolio_id"], stock.id, 100, "50")
    for day in range(1, 4):
        client.post(
            f"{BASE}/{portfolio['portfolio_id']}/snapshots",
            json={"as_of": (datetime.utcnow() - timedelta(days=4 - day)).isoformat()},
        )

    response = client.get(f"{BASE}/{portfolio['portfolio_id']}/snapshots")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert body["total"] == 3
    timestamps = [row["timestamp"] for row in body["data"]]
    assert timestamps == sorted(timestamps)


def test_54_performance_history(client: TestClient, stock):
    """54. Performance history is read from the recorded curve, oldest first."""
    portfolio = create(client)
    buy(client, portfolio["portfolio_id"], stock.id, 800, "50")
    client.post(f"{BASE}/{portfolio['portfolio_id']}/snapshots", json={})  # equity 100000
    client.post(
        f"{BASE}/{portfolio['portfolio_id']}/valuation",
        json={"prices": [{"stock_id": stock.id, "price": "40"}]},
    )
    # Cash 60000 + market value 800 * 40 = 32000, so equity is 92000.
    client.post(f"{BASE}/{portfolio['portfolio_id']}/snapshots", json={})

    curve = client.get(f"{BASE}/{portfolio['portfolio_id']}/equity-curve").json()

    assert curve["count"] == 2
    assert curve["total"] == 2
    equities = [dec(point["equity"]) for point in curve["data"]]
    assert equities == [Decimal("100000.00"), Decimal("92000.00")]


def test_55_snapshot_calculations_are_deterministic(client: TestClient, stock):
    """55. The same prices produce byte-identical snapshot figures every time.

    Two identical portfolios driven by the same requests must record the same money: that is
    what makes a backtest of this engine reproducible at all.
    """
    def run(name: str) -> list:
        portfolio = create(client, name="Determinism")
        buy(client, portfolio["portfolio_id"], stock.id, 137, "43.21")
        sell(client, portfolio["portfolio_id"], stock.id, 41, "55.55")
        client.post(
            f"{BASE}/{portfolio['portfolio_id']}/valuation",
            json={"prices": [{"stock_id": stock.id, "price": "51.11"}]},
        )
        snapshots = client.post(f"{BASE}/{portfolio['portfolio_id']}/snapshots", json={}).json()
        performance = client.get(f"{BASE}/{portfolio['portfolio_id']}/performance").json()
        positions = client.get(f"{BASE}/{portfolio['portfolio_id']}/positions").json()
        return {
            "snapshot": _without_identity(snapshots),
            "performance": _without_identity(performance),
            "positions": _without_identity(positions),
        }

    first, second = run("Determinism A"), run("Determinism B")

    assert first == second
    # Spot-check that the run was not trivial: it really did trade.
    assert first["snapshot"]["equity"] == first["performance"]["equity"]
    assert first["positions"]["data"][0]["quantity"] == 96


# ======================================================================
# 64  Error responses
# ======================================================================


def test_64_unknown_portfolio_is_a_structured_404(client: TestClient):
    """64. A missing portfolio is a structured 404, never a stack trace."""
    response = client.get(f"{BASE}/987654")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PORTFOLIO_NOT_FOUND"
    assert "987654" in body["error"]["message"]
    assert "Traceback" not in response.text


def test_64b_unknown_stock_is_a_structured_404(client: TestClient):
    """64. A missing stock is a structured 404 too."""
    portfolio = create(client)

    response = buy(client, portfolio["portfolio_id"], 987654, 1, "10")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "STOCK_NOT_FOUND"


def test_64c_insufficient_cash_is_a_structured_409(client: TestClient, stock):
    """64. An unfunded buy reports what it needed and what was available."""
    portfolio = create(client, initial_capital="1000")

    response = buy(client, portfolio["portfolio_id"], stock.id, 100, "50")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "INSUFFICIENT_CASH"
    assert body["error"]["details"]["required_cash"] == "5000.00"
    assert body["error"]["details"]["available_cash"] == "1000.00"


def test_64d_oversell_is_a_structured_409(client: TestClient, stock):
    """64. Selling more than is held reports both quantities."""
    portfolio = create(client)
    buy(client, portfolio["portfolio_id"], stock.id, 10, "50")

    response = sell(client, portfolio["portfolio_id"], stock.id, 11, "50")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "INSUFFICIENT_POSITION"
    assert body["error"]["details"]["requested_quantity"] == "11"
    assert body["error"]["details"]["owned_quantity"] == "10"


def test_64e_minimum_trade_value_is_a_structured_409(client: TestClient, stock):
    """64. The Phase 12 minimum trade value reports its own machine-readable code."""
    portfolio = create(client, risk_limits={"min_trade_value": "1000"})
    buy(client, portfolio["portfolio_id"], stock.id, 100, "50")

    response = sell(client, portfolio["portfolio_id"], stock.id, 1, "50")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "MIN_TRADE_VALUE_BELOW"
    assert body["error"]["details"]["rule"] == "min_trade_value"


def test_64f_daily_loss_is_a_structured_409(client: TestClient, stock):
    """64. The Phase 12 daily-loss rule reports its own code and its numbers."""
    portfolio = create(client, risk_limits={"max_daily_loss_pct": "5"})
    buy(client, portfolio["portfolio_id"], stock.id, 800, "50")
    client.post(
        f"{BASE}/{portfolio['portfolio_id']}/snapshots",
        json={"as_of": (datetime.utcnow() - timedelta(days=1)).isoformat()},
    )
    client.post(
        f"{BASE}/{portfolio['portfolio_id']}/valuation",
        json={"prices": [{"stock_id": stock.id, "price": "43.75"}]},
    )

    response = buy(client, portfolio["portfolio_id"], stock.id, 1, "43.75")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "MAX_DAILY_LOSS_EXCEEDED"
    assert body["error"]["details"]["day_open_equity"] == "100000.00"


def test_64g_invalid_quantity_is_a_structured_422(client: TestClient, stock):
    """64. A non-positive quantity is rejected by the schema before the engine sees it."""
    portfolio = create(client)

    response = client.post(
        f"{BASE}/{portfolio['portfolio_id']}/orders",
        json={"stock_id": stock.id, "side": "BUY", "quantity": 0, "requested_price": "50"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "quantity" in body["error"]["message"]


def test_64h_invalid_price_is_a_structured_422(client: TestClient, stock):
    """64. A non-positive price is rejected too."""
    portfolio = create(client)

    response = client.post(
        f"{BASE}/{portfolio['portfolio_id']}/orders",
        json={"stock_id": stock.id, "side": "BUY", "quantity": 1, "requested_price": "0"},
    )

    assert response.status_code == 422


def test_64i_trading_a_closed_portfolio_is_a_structured_409(client: TestClient, stock):
    """64. A closed portfolio refuses trades with the lifecycle code."""
    portfolio = create(client)
    client.post(f"{BASE}/{portfolio['portfolio_id']}/close")

    response = buy(client, portfolio["portfolio_id"], stock.id, 1, "50")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "PORTFOLIO_INACTIVE"
    assert body["error"]["details"]["status"] == "CLOSED"


def test_64j_no_response_ever_contains_a_traceback(client: TestClient, stock):
    """64. Not one error path leaks internals."""
    portfolio = create(client)
    responses = [
        client.get(f"{BASE}/987654"),
        client.get(f"{BASE}/{portfolio['portfolio_id']}/positions/987654"),
        client.get(f"{BASE}/{portfolio['portfolio_id']}/orders/987654"),
        buy(client, portfolio["portfolio_id"], 987654, 1, "10"),
        buy(client, portfolio["portfolio_id"], stock.id, 0, "10"),
        buy(client, portfolio["portfolio_id"], stock.id, 1, "-1"),
        client.post(BASE, json={"initial_capital": "1000"}),
    ]

    for response in responses:
        assert response.status_code >= 400
        assert response.json()["success"] is False
        assert "Traceback" not in response.text
        assert "sqlalchemy" not in response.text.lower()
        assert "SELECT " not in response.text
        assert response.json()["error"]["code"]