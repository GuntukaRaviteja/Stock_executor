"""Phase 10 API tests.

One test per new endpoint, plus the deferred-order lifecycle and the two properties that are
only observable over HTTP: that a refusal changes nothing, and that a metric which cannot be
computed is ``null`` rather than a misleading zero.

Every response is asserted as a raw string, because the API contract is decimal strings — an
assertion that passed through ``float()`` would hide a precision regression.
"""

import math
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.stock import Stock
from tests.phase10_helpers import NO_FEES, buy, insert_prices, sell

BASE = "/api/v1/portfolios"


@pytest.fixture
def stock(db_session) -> Stock:
    record = Stock(
        symbol="P10CO", company_name="Phase Ten Co Ltd", exchange="NSE", market="INDIA"
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


def create(client, name="P10", capital="100000", **extra) -> dict:
    """Create a portfolio with zero fees, so exact cash arithmetic is assertable."""
    body = {
        "name": name,
        "initial_capital": capital,
        "trading_costs": {key: str(value) for key, value in NO_FEES.items()},
        **extra,
    }
    response = client.post(BASE, json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ----------------------------------------------------------------------
# PATCH /{portfolio_id}
# ----------------------------------------------------------------------


class TestUpdatePortfolio:
    def test_name_and_description_can_be_updated(self, client):
        portfolio = create(client, name="Original")
        response = client.patch(
            f"{BASE}/{portfolio['portfolio_id']}",
            json={"name": "Renamed", "description": "Now with a description"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "Renamed"
        assert body["description"] == "Now with a description"

    def test_the_update_is_persisted(self, client, db_session):
        portfolio = create(client)
        client.patch(f"{BASE}/{portfolio['portfolio_id']}", json={"name": "Persisted"})
        again = client.get(f"{BASE}/{portfolio['portfolio_id']}").json()
        assert again["name"] == "Persisted"

    def test_cash_and_capital_are_not_client_writable(self, client):
        """A client must not be able to mint money by editing the summary."""
        portfolio = create(client)
        response = client.patch(
            f"{BASE}/{portfolio['portfolio_id']}",
            json={"name": "Cheat", "current_cash": "999999", "equity": "999999"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["current_cash"] == portfolio["current_cash"]
        assert body["equity"] == portfolio["equity"]

    def test_risk_limits_can_be_updated_and_take_effect(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        response = client.patch(
            f"{BASE}/{portfolio_id}", json={"risk_limits": {"max_exposure_pct": "5"}}
        )
        assert response.status_code == 200, response.text
        assert response.json()["risk_limits"]["max_exposure_pct"] == "5"

        # The new limit is enforced by the very next order: 100 shares at 100 is 10% of equity.
        refused = client.post(
            f"{BASE}/{portfolio_id}/orders",
            json={
                "stock_id": stock.id,
                "side": "BUY",
                "quantity": 100,
                "requested_price": "100",
            },
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "MAX_EXPOSURE_EXCEEDED"

    def test_a_limit_at_the_boundary_is_allowed(self, client, stock):
        """A limit is breached strictly above it, so exactly 5% is still accepted."""
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        client.patch(
            f"{BASE}/{portfolio_id}", json={"risk_limits": {"max_exposure_pct": "5"}}
        )
        accepted = client.post(
            f"{BASE}/{portfolio_id}/orders",
            json={
                "stock_id": stock.id,
                "side": "BUY",
                "quantity": 50,
                "requested_price": "100",
            },
        )
        assert accepted.status_code == 201, accepted.text

    def test_a_new_limit_survives_a_portfolio_reload(self, client, stock):
        """The stored config must round-trip: this is the regression for dropped limit fields."""
        portfolio = create(client)
        client.patch(
            f"{BASE}/{portfolio['portfolio_id']}",
            json={
                "risk_limits": {
                    "max_exposure_pct": "25",
                    "min_cash_reserve_pct": "5",
                    "max_positions": 3,
                }
            },
        )
        limits = client.get(f"{BASE}/{portfolio['portfolio_id']}/risk").json()["risk_limits"]
        assert limits["max_exposure_pct"] == "25"
        assert limits["min_cash_reserve_pct"] == "5"
        assert limits["max_positions"] == 3

    def test_an_unknown_portfolio_is_404(self, client):
        assert client.patch(f"{BASE}/999999", json={"name": "Ghost"}).status_code == 404

    def test_an_empty_patch_is_a_documented_no_op(self, client):
        """Every field is optional, so an empty body changes nothing and reports the state."""
        portfolio = create(client)
        response = client.patch(f"{BASE}/{portfolio['portfolio_id']}", json={})
        assert response.status_code == 200
        assert response.json()["name"] == portfolio["name"]


# ----------------------------------------------------------------------
# GET /{portfolio_id}/positions/{stock_id}
# ----------------------------------------------------------------------


class TestPositionDetail:
    def test_a_single_position_is_returned(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 100, "50")

        response = client.get(f"{BASE}/{portfolio_id}/positions/{stock.id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stock_id"] == stock.id
        assert body["quantity"] == 100
        assert body["average_entry_price"] == "50.0000"
        assert body["current_price"] == "50.0000"
        assert body["market_value"] == "5000.00"
        assert body["unrealized_pnl"] == "0.00"
        assert body["allocation_pct"] == "5.0000"

    def test_a_closed_position_is_no_longer_held(self, client, stock):
        """Closing the position removes it, so there is nothing to report for that stock."""
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "50")
        sell(client, portfolio_id, stock.id, 10, "60")

        assert client.get(f"{BASE}/{portfolio_id}/positions").json()["data"] == []
        assert client.get(f"{BASE}/{portfolio_id}/positions/{stock.id}").status_code == 404

    def test_an_unheld_stock_is_404(self, client, stock):
        portfolio = create(client)
        response = client.get(
            f"{BASE}/{portfolio['portfolio_id']}/positions/{stock.id}"
        )
        assert response.status_code == 404

    def test_an_unknown_stock_is_404(self, client):
        portfolio = create(client)
        response = client.get(f"{BASE}/{portfolio['portfolio_id']}/positions/999999")
        assert response.status_code == 404


# ----------------------------------------------------------------------
# Deferred orders: GET /orders/{order_id} and POST /orders/{order_id}/execute
# ----------------------------------------------------------------------


class TestDeferredOrders:
    def _defer(self, client, portfolio_id, stock, **extra):
        response = client.post(
            f"{BASE}/{portfolio_id}/orders",
            json={
                "stock_id": stock.id,
                "side": "BUY",
                "quantity": 10,
                "requested_price": "100",
                "defer_execution": True,
                **extra,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def test_a_deferred_order_is_recorded_as_pending(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        body = self._defer(client, portfolio_id, stock)

        assert body["order"]["status"] == "PENDING"
        assert body["trade"] is None
        # Recording an order must not move cash.
        assert body["portfolio"]["current_cash"] == portfolio["current_cash"]

        order_id = body["order"]["order_id"]
        fetched = client.get(f"{BASE}/{portfolio_id}/orders/{order_id}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "PENDING"
        assert fetched.json()["rejection_code"] is None

    def test_execute_endpoint_fills_the_pending_order(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        order_id = self._defer(client, portfolio_id, stock)["order"]["order_id"]

        response = client.post(f"{BASE}/{portfolio_id}/orders/{order_id}/execute")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["order"]["status"] == "FILLED"
        assert body["trade"]["quantity"] == 10
        assert body["portfolio"]["current_cash"] == "99000.00"

    def test_a_pending_order_cannot_be_executed_twice(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        order_id = self._defer(client, portfolio_id, stock)["order"]["order_id"]

        assert client.post(f"{BASE}/{portfolio_id}/orders/{order_id}/execute").status_code == 200
        second = client.post(f"{BASE}/{portfolio_id}/orders/{order_id}/execute")
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "ORDER_NOT_EXECUTABLE"

    def test_execute_revalidates_against_current_state(self, client, stock):
        """Cash spent between recording and executing makes the deferred order unfundable."""
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        order_id = self._defer(client, portfolio_id, stock)["order"]["order_id"]

        # Drain the cash so the deferred 1,000 order can no longer be funded.
        buy(client, portfolio_id, stock.id, 995, "100")

        response = client.post(f"{BASE}/{portfolio_id}/orders/{order_id}/execute")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "INSUFFICIENT_CASH"

        summary = client.get(f"{BASE}/{portfolio_id}").json()
        assert summary["current_cash"] == "500.00"

    def test_a_pending_order_in_a_closed_portfolio_is_refused(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        order_id = self._defer(client, portfolio_id, stock)["order"]["order_id"]

        client.patch(f"{BASE}/{portfolio_id}/status", json={"status": "CLOSED"})
        response = client.post(f"{BASE}/{portfolio_id}/orders/{order_id}/execute")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PORTFOLIO_INACTIVE"

    def test_an_unknown_order_is_404(self, client):
        portfolio = create(client)
        response = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/orders/999999/execute"
        )
        assert response.status_code == 404

    def test_an_order_from_another_portfolio_is_404(self, client, stock):
        first = create(client, name="First")
        second = create(client, name="Second")
        order_id = self._defer(client, first["portfolio_id"], stock)["order"]["order_id"]

        response = client.post(f"{BASE}/{second['portfolio_id']}/orders/{order_id}/execute")
        assert response.status_code == 404

    def test_an_immediate_order_is_not_executable_again(self, client, stock):
        """The default path still fills immediately, and that order is already settled."""
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        body = buy(client, portfolio_id, stock.id, 5, "100")
        assert body["order"]["status"] == "FILLED"

        response = client.post(
            f"{BASE}/{portfolio_id}/orders/{body['order']['order_id']}/execute"
        )
        assert response.status_code == 409


# ----------------------------------------------------------------------
# POST /{portfolio_id}/risk/check
# ----------------------------------------------------------------------


class TestRiskCheckEndpoint:
    def _check(self, client, portfolio_id, stock, **overrides):
        payload = {
            "stock_id": stock.id,
            "side": "BUY",
            "quantity": 10,
            "requested_price": "100",
            **overrides,
        }
        response = client.post(f"{BASE}/{portfolio_id}/risk/check", json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    def test_an_affordable_trade_is_accepted(self, client, stock):
        portfolio = create(client)
        body = self._check(client, portfolio["portfolio_id"], stock)
        assert body["accepted"] is True
        assert body["rejection_code"] is None
        assert body["violations"] == []
        assert body["equity"] == "100000.00"

    def test_a_refusal_records_the_rejection_and_moves_no_money(self, client, stock):
        """A refusal is auditable: the order row exists, but no cash, position or P&L moves."""
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "100")

        before = client.get(f"{BASE}/{portfolio_id}").json()
        orders_before = client.get(f"{BASE}/{portfolio_id}/orders").json()["total"]

        refused = client.post(
            f"{BASE}/{portfolio_id}/orders",
            json={
                "stock_id": stock.id,
                "side": "BUY",
                "quantity": 100000,
                "requested_price": "100",
            },
        )
        assert refused.status_code == 409, refused.text

        after = client.get(f"{BASE}/{portfolio_id}").json()
        for field in (
            "current_cash",
            "equity",
            "market_value",
            "invested_capital",
            "exposure_pct",
            "position_count",
            "trade_count",
            "realized_pnl",
            "unrealized_pnl",
            "total_pnl",
        ):
            assert after[field] == before[field], f"{field} changed on a refused order"

        # The only permitted change is one more recorded, rejected order.
        assert client.get(f"{BASE}/{portfolio_id}/orders").json()["total"] == orders_before + 1
        rejected = client.get(f"{BASE}/{portfolio_id}/orders", params={"status": "REJECTED"}).json()
        assert rejected["total"] == 1
        assert rejected["data"][0]["rejection_code"] == "INSUFFICIENT_CASH"
        assert rejected["data"][0]["rejection_reason"]

    def test_the_check_never_changes_the_portfolio(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        before = client.get(f"{BASE}/{portfolio_id}").json()
        self._check(client, portfolio_id, stock, quantity=100, requested_price="100")
        after = client.get(f"{BASE}/{portfolio_id}").json()
        assert after == before

    def test_insufficient_cash_is_reported_with_its_own_code(self, client, stock):
        portfolio = create(client)
        body = self._check(client, portfolio["portfolio_id"], stock, quantity=2000)
        assert body["accepted"] is False
        assert body["rejection_code"] == "INSUFFICIENT_CASH"

    def test_a_limit_breach_is_reported_with_the_rule_specific_code(self, client, stock):
        portfolio = create(client)
        client.patch(
            f"{BASE}/{portfolio['portfolio_id']}",
            json={"risk_limits": {"max_position_value": "500"}},
        )
        body = self._check(client, portfolio["portfolio_id"], stock)
        assert body["rejection_code"] == "MAX_POSITION_SIZE_EXCEEDED"
        assert body["violations"][0]["rule"] == "max_position_value"
        assert body["violations"][0]["limit"] == "500"

    def test_every_breached_rule_is_reported_not_only_the_first(self, client, stock):
        portfolio = create(client)
        client.patch(
            f"{BASE}/{portfolio['portfolio_id']}",
            json={
                "risk_limits": {
                    "max_position_value": "500",
                    "max_exposure_pct": "0.5",
                }
            },
        )
        body = self._check(client, portfolio["portfolio_id"], stock)
        rules = {violation["rule"] for violation in body["violations"]}
        assert {"max_position_value", "max_exposure_pct"} <= rules
        assert body["rejection_code"] == "MAX_POSITION_SIZE_EXCEEDED"

    def test_overselling_without_a_position_is_reported(self, client, stock):
        portfolio = create(client)
        body = self._check(client, portfolio["portfolio_id"], stock, side="SELL", quantity=5)
        assert body["rejection_code"] == "INSUFFICIENT_POSITION"

    def test_overselling_more_than_held_is_reported(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 5, "100")
        body = self._check(client, portfolio_id, stock, side="SELL", quantity=10)
        assert body["rejection_code"] == "INSUFFICIENT_POSITION"
        assert body["violations"][0]["details"]["owned_quantity"] == "5"

    def test_a_closed_portfolio_is_reported(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        client.patch(f"{BASE}/{portfolio_id}/status", json={"status": "PAUSED"})
        body = self._check(client, portfolio_id, stock)
        assert body["rejection_code"] == "PORTFOLIO_INACTIVE"

    def test_a_non_positive_price_is_refused_by_validation(self, client, stock):
        """The price is rejected before any risk rule runs, so no portfolio state is read."""
        portfolio = create(client)
        response = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/risk/check",
            json={
                "stock_id": stock.id,
                "side": "BUY",
                "quantity": 1,
                "requested_price": "0",
            },
        )
        assert response.status_code == 422

    def test_the_post_exposure_is_reported(self, client, stock):
        portfolio = create(client)
        body = self._check(client, portfolio["portfolio_id"], stock, quantity=100)
        assert body["exposure_pct"] == "0.0000"
        assert body["position_count"] == 0

    def test_the_projected_exposure_appears_in_the_violation(self, client, stock):
        portfolio = create(client)
        client.patch(
            f"{BASE}/{portfolio['portfolio_id']}",
            json={"risk_limits": {"max_exposure_pct": "5"}},
        )
        body = self._check(client, portfolio["portfolio_id"], stock, quantity=100)
        assert body["rejection_code"] == "MAX_EXPOSURE_EXCEEDED"
        assert body["violations"][0]["observed"] == "10.0000"


# ----------------------------------------------------------------------
# GET /{portfolio_id}/risk
# ----------------------------------------------------------------------


class TestEffectiveRisk:
    def test_defaults_are_reported_when_nothing_is_configured(self, client):
        portfolio = create(client)
        response = client.get(f"{BASE}/{portfolio['portfolio_id']}/risk")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ACTIVE"
        assert body["equity"] == "100000.00"
        assert body["cash"] == "100000.00"
        assert body["invested_capital"] == "0.00"
        assert body["exposure_pct"] == "0.0000"
        assert body["violations"] == []
        assert body["cash_reserve_required"] == "0.00"

    def test_configured_limits_are_reported(self, client):
        portfolio = create(client)
        client.patch(
            f"{BASE}/{portfolio['portfolio_id']}",
            json={"risk_limits": {"max_exposure_pct": "40", "max_drawdown_pct": "15"}},
        )
        limits = client.get(f"{BASE}/{portfolio['portfolio_id']}/risk").json()["risk_limits"]
        assert limits["max_exposure_pct"] == "40"
        assert limits["max_drawdown_pct"] == "15"

    def test_an_already_breached_limit_is_reported(self, client, stock):
        """A limit the portfolio is already past is a fact about now, not a refused trade."""
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        # Take the exposure first, then tighten the limit below what is already held.
        buy(client, portfolio_id, stock.id, 200, "100")
        client.patch(
            f"{BASE}/{portfolio_id}",
            json={"risk_limits": {"max_exposure_pct": "5"}},
        )

        body = client.get(f"{BASE}/{portfolio_id}/risk").json()
        assert body["exposure_pct"] == "20.0000"
        assert [breach["rule"] for breach in body["violations"]] == ["max_exposure_pct"]

    def test_an_unknown_portfolio_is_404(self, client):
        assert client.get(f"{BASE}/999999/risk").status_code == 404


# ----------------------------------------------------------------------
# POST /{portfolio_id}/position-size
# ----------------------------------------------------------------------


class TestPositionSizeEndpoint:
    def _size(self, client, portfolio_id, **payload):
        response = client.post(f"{BASE}/{portfolio_id}/position-size", json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    def test_fixed_capital(self, client):
        portfolio = create(client)
        body = self._size(
            client,
            portfolio["portfolio_id"],
            method="FIXED_CAPITAL",
            price="250",
            capital="10000",
        )
        assert body["quantity"] == 40
        assert body["capital"] == "10000.00"

    def test_percentage_uses_portfolio_equity(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 100, "50")  # equity unchanged, cash reduced

        body = self._size(
            client,
            portfolio_id,
            method="PERCENTAGE",
            price="250",
            allocation_pct="5",
        )
        assert body["quantity"] == 20
        assert body["budget"] == "5000.00"
        assert body["equity"] == "100000.00"

    def test_risk_based(self, client):
        portfolio = create(client)
        body = self._size(
            client,
            portfolio["portfolio_id"],
            method="RISK_BASED",
            price="100",
            risk_pct="1",
            entry_price="100",
            stop_loss_price="95",
        )
        assert body["quantity"] == 200
        assert body["risk_per_share"] == "5"
        assert body["allowed_risk"] == "1000.00"

    def test_the_endpoint_places_no_order(self, client):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        before = client.get(f"{BASE}/{portfolio_id}/orders").json()["total"]
        self._size(
            client,
            portfolio_id,
            method="FIXED_CAPITAL",
            price="250",
            capital="10000",
        )
        after = client.get(f"{BASE}/{portfolio_id}/orders").json()["total"]
        assert before == after

    def test_an_incomplete_request_is_422(self, client):
        portfolio = create(client)
        response = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/position-size",
            json={"method": "RISK_BASED", "price": "100", "risk_pct": "1"},
        )
        assert response.status_code == 422

    def test_an_unknown_portfolio_is_404(self, client):
        response = client.post(
            f"{BASE}/999999/position-size",
            json={"method": "FIXED_CAPITAL", "price": "100", "capital": "1000"},
        )
        assert response.status_code == 404


# ----------------------------------------------------------------------
# GET /{portfolio_id}/equity-curve and /drawdown
# ----------------------------------------------------------------------


class TestEquityCurveAndDrawdown:
    def test_an_empty_portfolio_has_an_empty_curve(self, client):
        portfolio = create(client)
        response = client.get(f"{BASE}/{portfolio['portfolio_id']}/equity-curve")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["data"] == []
        assert body["total"] == 0

    def test_the_curve_grows_with_each_snapshot(self, client):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        start = datetime(2026, 1, 1, 9, 30)
        for day in range(3):
            response = client.post(
                f"{BASE}/{portfolio_id}/snapshots",
                json={"as_of": (start + timedelta(days=day)).isoformat()},
            )
            assert response.status_code == 201, response.text

        body = client.get(f"{BASE}/{portfolio_id}/equity-curve").json()
        assert body["total"] == 3
        assert len(body["data"]) == 3
        assert all(point["drawdown_pct"] == "0.0000" for point in body["data"])
        assert all(point["exposure_pct"] == "0.0000" for point in body["data"])

    def test_drawdown_of_a_flat_portfolio_is_zero(self, client):
        """With nothing recorded the newest point is the current state, so the curve is short."""
        portfolio = create(client)
        body = client.get(f"{BASE}/{portfolio['portfolio_id']}/drawdown").json()
        assert body["max_drawdown"] == "0.00"
        assert body["max_drawdown_pct"] == "0.0000"
        assert body["peak_equity"] == "100000.00"
        assert body["current_equity"] == "100000.00"
        assert len(body["points"]) == 1
        assert body["points"][0]["equity"] == "100000.00"

    def test_drawdown_is_computed_from_the_stored_curve(self, client, stock):
        """100,000 -> 110,000 -> 90,000 is an 18.1818% decline from the peak.

        The curve is produced the way it is in practice: 1,000 shares bought at 100, then
        re-valued at 110 and at 90, with a snapshot recorded at each point.
        """
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 1000, "100")

        for day, price in ((2, "110"), (3, "90")):
            revalue = client.post(
                f"{BASE}/{portfolio_id}/valuation",
                json={
                    "as_of": f"2026-01-0{day}T09:30:00",
                    "prices": [{"stock_id": stock.id, "price": price}],
                },
            )
            assert revalue.status_code == 200, revalue.text
            client.post(
                f"{BASE}/{portfolio_id}/snapshots",
                json={"as_of": f"2026-01-0{day}T09:30:00"},
            )

        body = client.get(f"{BASE}/{portfolio_id}/drawdown").json()
        assert body["peak_equity"] == "110000.00"
        assert body["max_drawdown"] == "20000.00"
        assert body["max_drawdown_pct"] == "18.1818"
        assert len(body["points"]) == 3
        trough = min(body["points"], key=lambda point: Decimal(point["equity"]))
        assert trough["equity"] == "90000.00"
        assert trough["drawdown_pct"] == "-18.1818"

    def test_the_curve_can_be_bounded_by_date(self, client):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        for day in (1, 2, 3):
            client.post(
                f"{BASE}/{portfolio_id}/snapshots",
                json={"as_of": f"2026-01-0{day}T09:30:00"},
            )

        body = client.get(
            f"{BASE}/{portfolio_id}/equity-curve",
            params={"start_date": "2026-01-02"},
        ).json()
        assert body["total"] == 2


# ----------------------------------------------------------------------
# POST /{portfolio_id}/simulate
# ----------------------------------------------------------------------


class TestSimulateEndpoint:
    @pytest.fixture
    def priced_stock(self, db_session) -> int:
        """A stock with enough history for the indicators, and swings the strategy reacts to.

        A straight line never produces an entry signal, so the series oscillates; that is what
        makes "the run traded" a meaningful assertion rather than a vacuous one.
        """
        record = Stock(
            symbol="SIMCO", company_name="Simulator Co Ltd", exchange="NSE", market="INDIA"
        )
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)
        start = date(2025, 1, 1)
        bars = []
        for index in range(260):
            price = 100 + 12 * math.sin(index / 6.0) + index * 0.15
            bars.append(
                (start + timedelta(days=index), price, price + 1, price - 1, price)
            )
        insert_prices(db_session, record.id, bars)
        return record.id

    def test_a_short_but_valid_series_reports_no_orders_rather_than_failing(self, client, db_session):
        """Too few bars to warm the indicators is not corrupt data, so the run simply trades nothing.

        The response still says how many bars it looked at, so "no orders" is explained rather
        than silent.
        """
        record = Stock(
            symbol="SHORTCO", company_name="Short Co", exchange="NSE", market="INDIA"
        )
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)
        insert_prices(
            db_session,
            record.id,
            [(date(2026, 1, 1) + timedelta(days=i), 100, 101, 99, 100) for i in range(5)],
        )

        portfolio = create(client)
        response = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/simulate", json={"stock_id": record.id}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["bars_processed"] == 5
        assert body["orders"] == []

    def test_a_single_bar_is_refused_as_invalid_data(self, client, db_session):
        """Below the Phase 8 minimum bar count the series cannot be validated at all."""
        record = Stock(
            symbol="ONECO", company_name="One Bar Co", exchange="NSE", market="INDIA"
        )
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)
        insert_prices(db_session, record.id, [(date(2026, 1, 1), 100, 101, 99, 100)])

        portfolio = create(client)
        response = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/simulate", json={"stock_id": record.id}
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"]["reason"] == "insufficient_bars"

    def test_a_run_reports_orders_bars_and_the_performance_effect(self, client, priced_stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        response = client.post(
            f"{BASE}/{portfolio_id}/simulate", json={"stock_id": priced_stock}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stock_id"] == priced_stock
        assert body["bars_processed"] == 260
        assert body["execution_price_policy"] == "OPEN"
        assert body["performance"] is not None
        assert "positions" in body["performance"]
        for order in body["orders"]:
            assert order["signal_date"] is not None
            assert order["executed_at"] is not None
            # Look-ahead: the decision always precedes its fill.
            assert order["signal_date"] < order["executed_at"]

    def test_the_run_is_persisted_in_the_order_history(self, client, priced_stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        run = client.post(
            f"{BASE}/{portfolio_id}/simulate", json={"stock_id": priced_stock}
        ).json()

        history = client.get(f"{BASE}/{portfolio_id}/orders", params={"limit": 500}).json()
        assert history["total"] == len(run["orders"])

    def test_the_execution_policy_can_be_chosen(self, client, priced_stock):
        portfolio = create(client)
        body = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/simulate",
            json={"stock_id": priced_stock, "execution_price_policy": "CLOSE"},
        ).json()
        assert body["execution_price_policy"] == "CLOSE"

    def test_position_sizing_can_be_requested(self, client, priced_stock):
        """A fixed 5,000 budget caps every entry the strategy asks for."""
        portfolio = create(client)
        body = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/simulate",
            json={
                "stock_id": priced_stock,
                "position_sizing": {
                    "method": "FIXED_CAPITAL",
                    "capital": "5000",
                },
            },
        ).json()
        buys = [order for order in body["orders"] if order["side"] == "BUY"]
        assert buys, "the oscillating series must produce at least one entry"
        for order in buys:
            assert order["quantity"] <= 50, "5,000 of capital cannot buy more than 50 shares"

    def test_the_run_trades_without_a_sizing_request(self, client, priced_stock):
        portfolio = create(client)
        body = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/simulate", json={"stock_id": priced_stock}
        ).json()
        sides = {order["side"] for order in body["orders"]}
        assert sides == {"BUY", "SELL"}, "a full run both enters and exits"

    def test_an_unknown_stock_is_404(self, client):
        portfolio = create(client)
        response = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/simulate", json={"stock_id": 999999}
        )
        assert response.status_code == 404

    def test_an_unknown_strategy_is_rejected(self, client, priced_stock):
        portfolio = create(client)
        response = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/simulate",
            json={"stock_id": priced_stock, "strategy": "not_a_strategy"},
        )
        assert response.status_code == 422


# ----------------------------------------------------------------------
# Performance endpoint: null for unmeasurable metrics
# ----------------------------------------------------------------------


class TestPerformanceReporting:
    def test_metrics_are_null_before_there_is_history(self, client):
        """A metric that needs observations reports null, not 0."""
        portfolio = create(client)
        body = client.get(
            f"{BASE}/{portfolio['portfolio_id']}/performance"
        ).json()
        assert body["win_rate_pct"] is None
        assert body["profit_factor"] is None
        assert body["average_realized_pnl"] is None
        assert body["annualized_volatility_pct"] is None
        assert body["sharpe_ratio"] is None
        assert body["trade_count"] == 0
        assert body["closed_trades"] == 0

    def test_a_closed_round_trip_populates_the_trade_metrics(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "100")
        sell(client, portfolio_id, stock.id, 10, "120")

        body = client.get(f"{BASE}/{portfolio_id}/performance").json()
        assert body["closed_trades"] == 1
        assert body["win_rate_pct"] == "100.0000"
        assert body["realized_pnl"] == "200.00"
        assert body["average_realized_pnl"] == "200.00"

    def test_a_losing_round_trip_gives_a_zero_win_rate_not_null(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "100")
        sell(client, portfolio_id, stock.id, 10, "80")

        body = client.get(f"{BASE}/{portfolio_id}/performance").json()
        assert body["win_rate_pct"] == "0.0000"
        assert body["realized_pnl"] == "-200.00"

    def test_exposure_and_invested_capital_are_reported(self, client, stock):
        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 100, "50")

        body = client.get(f"{BASE}/{portfolio_id}/performance").json()
        assert body["exposure_pct"] == "5.0000"
        assert body["invested_capital"] == "5000.00"


class TestListFiltersAreHonoured:
    """``data`` and ``total`` must describe the same set, or pagination lies."""

    def test_order_status_filter_narrows_data_and_total(self, client, db_session, stock):
        second = Stock(
            symbol="P10B", company_name="Second Co Ltd", exchange="NSE", market="INDIA"
        )
        db_session.add(second)
        db_session.commit()
        db_session.refresh(second)

        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "100")
        buy(client, portfolio_id, second.id, 10, "100")
        # A refused order, so the history contains all three statuses.
        refused = client.post(
            f"{BASE}/{portfolio_id}/orders",
            json={
                "stock_id": stock.id,
                "side": "BUY",
                "quantity": 100000,
                "requested_price": "100",
            },
        )
        assert refused.status_code == 409

        unfiltered = client.get(f"{BASE}/{portfolio_id}/orders").json()
        assert unfiltered["total"] == 3

        filtered = client.get(f"{BASE}/{portfolio_id}/orders", params={"status": "FILLED"}).json()
        assert filtered["total"] == 2
        assert len(filtered["data"]) == 2
        assert {order["status"] for order in filtered["data"]} == {"FILLED"}

        rejected = client.get(
            f"{BASE}/{portfolio_id}/orders", params={"status": "REJECTED"}
        ).json()
        assert rejected["total"] == 1
        assert len(rejected["data"]) == 1

    def test_stock_filter_narrows_orders_and_trades(self, client, db_session, stock):
        second = Stock(
            symbol="P10C", company_name="Third Co Ltd", exchange="NSE", market="INDIA"
        )
        db_session.add(second)
        db_session.commit()
        db_session.refresh(second)

        portfolio = create(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "100")
        buy(client, portfolio_id, second.id, 20, "50")
        sell(client, portfolio_id, second.id, 5, "60")

        orders = client.get(
            f"{BASE}/{portfolio_id}/orders", params={"stock_id": second.id}
        ).json()
        assert orders["total"] == 2
        assert {order["stock_id"] for order in orders["data"]} == {second.id}

        trades = client.get(
            f"{BASE}/{portfolio_id}/trades", params={"stock_id": second.id}
        ).json()
        assert trades["total"] == 2

        all_trades = client.get(f"{BASE}/{portfolio_id}/trades").json()
        assert all_trades["total"] == 3


# ----------------------------------------------------------------------
# OpenAPI surface
# ----------------------------------------------------------------------


class TestOpenAPISurface:
    def test_every_phase_10_endpoint_is_documented(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        for path in (
            "/api/v1/portfolios/{portfolio_id}",
            "/api/v1/portfolios/{portfolio_id}/positions/{stock_id}",
            "/api/v1/portfolios/{portfolio_id}/orders/{order_id}",
            "/api/v1/portfolios/{portfolio_id}/orders/{order_id}/execute",
            "/api/v1/portfolios/{portfolio_id}/risk/check",
            "/api/v1/portfolios/{portfolio_id}/risk",
            "/api/v1/portfolios/{portfolio_id}/position-size",
            "/api/v1/portfolios/{portfolio_id}/equity-curve",
            "/api/v1/portfolios/{portfolio_id}/drawdown",
            "/api/v1/portfolios/{portfolio_id}/simulate",
        ):
            assert path in paths, f"{path} is missing from the OpenAPI document"

    def test_the_health_endpoint_still_works(self, client):
        response = client.get("/health")
        assert response.status_code == 200
