"""End-to-end tests for the Phase 9 paper-trading API.

These run against the real FastAPI app with an isolated SQLite database, so they exercise the
full stack: router -> service -> repositories -> models. They cover the six required
end-to-end scenarios plus persistence, idempotency, portfolio states, rollback and the
Phase 7/8 strategy integration.

Money fields are serialised by the API as strings, so every amount is compared through
:func:`dec` — a test never rounds a value through a float.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.portfolio import PortfolioOrder, PortfolioPosition, PortfolioTrade
from app.models.price import StockPrice
from app.models.stock import Stock
from tests.backtest_fixtures import bars_from_closes, decline_then_rally, rally_then_decline

BASE = "/api/v1/portfolios"
CAPITAL = "100000"
RATE = Decimal("0.001")


def dec(value) -> Decimal:
    """Read a money field out of a JSON body without passing it through a float."""
    return Decimal(str(value))


# ======================================================================
# Helpers
# ======================================================================


@pytest.fixture
def stock(db_session: Session) -> Stock:
    """A single stored stock the orders can reference."""
    record = Stock(
        symbol="TESTCO",
        company_name="Test Company Ltd",
        exchange="NSE",
        market="INDIA",
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


@pytest.fixture
def second_stock(db_session: Session) -> Stock:
    record = Stock(
        symbol="TESTCO2",
        company_name="Second Test Company Ltd",
        exchange="NSE",
        market="INDIA",
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


def seed_tape(db_session: Session, stock: Stock, bars) -> None:
    """Persist a deterministic OHLCV tape for ``stock``.

    The bar shapes come from the Phase 8 fixtures, so the series used here are the same ones
    the Phase 8 regression suite proves produce an entry or an exit.
    """
    for bar in bars:
        db_session.add(
            StockPrice(
                stock_id=stock.id,
                price_date=bar.date,
                open_price=bar.open,
                high_price=bar.high,
                low_price=bar.low,
                close_price=bar.close,
                volume=bar.volume,
            )
        )
    db_session.commit()


def create_portfolio(client: TestClient, name="Paper Portfolio", **overrides) -> dict:
    payload = {"name": name, "initial_capital": CAPITAL}
    payload.update(overrides)
    response = client.post(BASE, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def order(client: TestClient, portfolio_id: int, side: str, stock_id: int, quantity: int, price: str, **extra):
    payload = {
        "stock_id": stock_id,
        "side": side,
        "quantity": quantity,
        "requested_price": price,
        **extra,
    }
    return client.post(f"{BASE}/{portfolio_id}/orders", json=payload)


def buy(client: TestClient, portfolio_id: int, stock_id: int, quantity: int, price: str, **extra):
    return order(client, portfolio_id, "BUY", stock_id, quantity, price, **extra)


def sell(client: TestClient, portfolio_id: int, stock_id: int, quantity: int, price: str, **extra):
    return order(client, portfolio_id, "SELL", stock_id, quantity, price, **extra)


def fee_for(quantity: int, price: str) -> Decimal:
    """The fee the engine must charge, stated independently of the engine."""
    return (Decimal(quantity) * Decimal(price) * RATE).quantize(Decimal("0.01"))


def quant(value: Decimal) -> Decimal:
    """Round a money amount the way the engine does."""
    return Decimal(value).quantize(Decimal("0.01"))


# ======================================================================
# Scenario 1 — buy, mark up, sell, close out
# ======================================================================


class TestScenarioBuyValueSell:
    def test_full_lifecycle(self, client: TestClient, stock: Stock):
        """Create -> buy -> mark to market -> sell -> final cash, equity and P&L."""
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        # --- create
        assert dec(portfolio["current_cash"]) == Decimal(CAPITAL)
        assert dec(portfolio["equity"]) == Decimal(CAPITAL)
        assert dec(portfolio["initial_capital"]) == Decimal(CAPITAL)
        assert dec(portfolio["realized_pnl"]) == Decimal("0")
        assert dec(portfolio["unrealized_pnl"]) == Decimal("0")
        assert dec(portfolio["total_pnl"]) == Decimal("0")
        assert portfolio["status"] == "ACTIVE"
        assert portfolio["trading_mode"] == "PAPER"
        assert portfolio["position_count"] == 0

        # --- buy 10 shares at 1000
        buy_fee = fee_for(10, "1000")
        response = buy(client, portfolio_id, stock.id, 10, "1000")
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["order"]["status"] == "FILLED"
        assert dec(body["order"]["execution_price"]) == Decimal("1000")
        assert dec(body["order"]["fees"]) == buy_fee
        assert dec(body["order"]["net_cash_impact"]) == -(Decimal("10000") + buy_fee)
        assert body["trade"]["trade_id"] > 0
        assert body["trade"]["quantity"] == 10
        assert dec(body["portfolio"]["current_cash"]) == quant(
            Decimal(CAPITAL) - Decimal("10000") - buy_fee
        )

        # --- mark the position to 1100
        valuation = client.post(
            f"{BASE}/{portfolio_id}/valuation",
            json={"prices": [{"stock_id": stock.id, "price": "1100"}]},
        )
        assert valuation.status_code == 200, valuation.text
        valued = valuation.json()
        assert dec(valued["market_value"]) == Decimal("11000.00")
        assert dec(valued["unrealized_pnl"]) == Decimal("1000.00")  # 10 * (1100 - 1000)
        assert dec(valued["equity"]) == quant(Decimal(CAPITAL) + Decimal("1000") - buy_fee)

        # --- sell everything at 1100
        sell_fee = fee_for(10, "1100")
        response = sell(client, portfolio_id, stock.id, 10, "1100")
        assert response.status_code == 201, response.text
        closed = response.json()

        assert closed["order"]["status"] == "FILLED"
        assert dec(closed["trade"]["realized_pnl"]) == quant(
            Decimal("1000") - sell_fee - buy_fee
        )
        assert dec(closed["portfolio"]["current_cash"]) == quant(
            Decimal(CAPITAL) + Decimal("1000") - buy_fee - sell_fee
        )
        assert closed["portfolio"]["position_count"] == 0
        assert dec(closed["portfolio"]["market_value"]) == Decimal("0.00")

        # --- final performance
        performance = client.get(f"{BASE}/{portfolio_id}/performance").json()
        assert performance["position_count"] == 0
        assert performance["trade_count"] == 2
        assert dec(performance["realized_pnl"]) == quant(
            Decimal("1000") - sell_fee - buy_fee
        )
        assert dec(performance["unrealized_pnl"]) == Decimal("0.00")
        assert dec(performance["total_fees"]) == buy_fee + sell_fee
        assert dec(performance["equity"]) == dec(performance["current_cash"])
        assert dec(performance["total_pnl"]) == dec(performance["realized_pnl"])
        assert dec(performance["return_pct"]) > 0

        # --- order and trade history
        orders = client.get(f"{BASE}/{portfolio_id}/orders").json()
        assert orders["total"] == 2
        assert {item["status"] for item in orders["data"]} == {"FILLED"}

        trades = client.get(f"{BASE}/{portfolio_id}/trades").json()
        assert trades["total"] == 2
        assert {item["side"] for item in trades["data"]} == {"BUY", "SELL"}

        # --- snapshot captures the final equity
        snapshot = client.post(f"{BASE}/{portfolio_id}/snapshots", json={})
        assert snapshot.status_code == 201, snapshot.text
        assert dec(snapshot.json()["equity"]) == dec(performance["equity"])

    def test_valuation_never_creates_a_trade_or_moves_cash(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "1000")

        before_cash = client.get(f"{BASE}/{portfolio_id}").json()["current_cash"]
        client.post(
            f"{BASE}/{portfolio_id}/valuation",
            json={"prices": [{"stock_id": stock.id, "price": "1500"}]},
        )

        after = client.get(f"{BASE}/{portfolio_id}").json()
        assert dec(after["current_cash"]) == dec(before_cash)
        assert after["trade_count"] == 1
        assert after["order_count"] == 1


# ======================================================================
# Scenario 2 — several stocks, allocation and portfolio equity
# ======================================================================


class TestScenarioMultiplePositions:
    def test_allocation_and_portfolio_equity(
        self, client: TestClient, stock: Stock, second_stock: Stock
    ):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        assert buy(client, portfolio_id, stock.id, 10, "1000").status_code == 201
        assert buy(client, portfolio_id, second_stock.id, 20, "500").status_code == 201

        valuation = client.post(
            f"{BASE}/{portfolio_id}/valuation",
            json={
                "prices": [
                    {"stock_id": stock.id, "price": "1200"},
                    {"stock_id": second_stock.id, "price": "400"},
                ]
            },
        ).json()

        assert dec(valuation["market_value"]) == Decimal("20000.00")  # 12,000 + 8,000
        positions = {item["stock_id"]: item for item in valuation["positions"]}
        assert dec(positions[stock.id]["market_value"]) == Decimal("12000.00")
        assert dec(positions[stock.id]["unrealized_pnl"]) == Decimal("2000.00")
        assert dec(positions[second_stock.id]["unrealized_pnl"]) == Decimal("-2000.00")
        assert dec(valuation["unrealized_pnl"]) == Decimal("0.00")

        # Allocation is measured against total equity (cash plus both positions).
        expected_equity = quant(Decimal(CAPITAL) - dec(valuation["total_fees"]))
        assert dec(valuation["equity"]) == expected_equity
        allocation = sum(dec(item["allocation_pct"]) for item in valuation["positions"])
        expected_allocation = quant(Decimal("20000") / expected_equity * Decimal("100"))
        assert abs(allocation - expected_allocation) < Decimal("0.01")

    def test_positions_endpoint_reports_open_positions(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "1000")

        response = client.get(f"{BASE}/{portfolio_id}/positions")
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        position = body["data"][0]
        assert position["symbol"] == stock.symbol
        assert position["quantity"] == 10
        assert dec(position["average_entry_price"]) == Decimal("1000.0000")
        assert dec(position["market_value"]) == Decimal("10000.00")


# ======================================================================
# Scenario 3 and 4 — signal engine -> strategy -> paper order
# ======================================================================


class TestStrategyIntegration:
    def test_signal_engine_buy_produces_a_filled_paper_order(
        self, client: TestClient, db_session: Session, stock: Stock
    ):
        """Scenario 3: Phase 7 BUY signal -> Phase 8 rules -> risk -> execution -> position."""
        seed_tape(db_session, stock, bars_from_closes(decline_then_rally(70, 25)))
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        response = client.post(
            f"{BASE}/{portfolio_id}/strategy-orders", json={"stock_id": stock.id}
        )
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["action"] == "enter"
        assert body["strategy"] == "phase7_technical"
        assert body["order"] is not None
        assert body["order"]["status"] == "FILLED"
        assert body["order"]["side"] == "BUY"
        assert body["order"]["source"] == "STRATEGY"
        assert body["trade"] is not None
        assert body["portfolio"]["position_count"] == 1
        assert dec(body["portfolio"]["current_cash"]) < Decimal(CAPITAL)

        positions = client.get(f"{BASE}/{portfolio_id}/positions").json()
        assert positions["data"][0]["quantity"] == body["order"]["quantity"]
        assert positions["data"][0]["quantity"] > 0

    def test_same_strategy_event_is_never_executed_twice(
        self, client: TestClient, db_session: Session, stock: Stock
    ):
        """Reusing a strategy event key replays the original order instead of executing again."""
        seed_tape(db_session, stock, bars_from_closes(decline_then_rally(70, 25)))
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        request = {"stock_id": stock.id, "idempotency_key": "signal-bar-1"}
        first = client.post(f"{BASE}/{portfolio_id}/strategy-orders", json=request).json()
        cash_after_first = dec(first["portfolio"]["current_cash"])

        # A fresh tape makes the rules produce a *new* decision; the reused key must still
        # suppress the second execution and return the original order.
        db_session.query(StockPrice).filter(StockPrice.stock_id == stock.id).delete()
        db_session.commit()
        seed_tape(db_session, stock, bars_from_closes(rally_then_decline(up=60, down=25)))

        second = client.post(f"{BASE}/{portfolio_id}/strategy-orders", json=request).json()
        assert second["idempotent_replay"] is True
        assert second["order"]["order_id"] == first["order"]["order_id"]
        assert second["order"]["side"] == "BUY"
        assert second["trade"]["trade_id"] == first["trade"]["trade_id"]
        assert dec(second["portfolio"]["current_cash"]) == cash_after_first
        assert second["portfolio"]["trade_count"] == 1
        assert second["portfolio"]["order_count"] == 1
        assert dec(first["order"]["net_cash_impact"]) == dec(second["order"]["net_cash_impact"])

    def test_a_fresh_strategy_event_is_a_new_order(
        self, client: TestClient, db_session: Session, stock: Stock
    ):
        """Distinct signal bars are independent decisions and are each executed."""
        seed_tape(db_session, stock, bars_from_closes(decline_then_rally(70, 25)))
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        first = client.post(
            f"{BASE}/{portfolio_id}/strategy-orders", json={"stock_id": stock.id}
        ).json()
        db_session.query(StockPrice).filter(StockPrice.stock_id == stock.id).delete()
        db_session.commit()
        seed_tape(db_session, stock, bars_from_closes(rally_then_decline(up=60, down=25)))

        second = client.post(
            f"{BASE}/{portfolio_id}/strategy-orders", json={"stock_id": stock.id}
        ).json()
        assert second["action"] == "exit"
        assert second["idempotent_replay"] is False
        assert second["order"]["order_id"] != first["order"]["order_id"]
        assert second["portfolio"]["trade_count"] == 2
        assert second["portfolio"]["position_count"] == 0

    def test_exit_signal_closes_an_existing_position(
        self, client: TestClient, db_session: Session, stock: Stock
    ):
        """Scenario 4: an open position is closed when the strategy produces an exit."""
        seed_tape(db_session, stock, bars_from_closes(decline_then_rally(70, 25)))
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        entered = client.post(
            f"{BASE}/{portfolio_id}/strategy-orders", json={"stock_id": stock.id}
        ).json()
        assert entered["action"] == "enter"
        opened_quantity = entered["order"]["quantity"]

        # Replace the tape with one that ends in a bearish crossover, then re-run the rules.
        db_session.query(StockPrice).filter(StockPrice.stock_id == stock.id).delete()
        db_session.commit()
        seed_tape(db_session, stock, bars_from_closes(rally_then_decline(up=60, down=25)))

        response = client.post(
            f"{BASE}/{portfolio_id}/strategy-orders", json={"stock_id": stock.id}
        )
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["action"] == "exit"
        assert body["order"]["side"] == "SELL"
        assert body["order"]["quantity"] == opened_quantity
        assert body["trade"]["realized_pnl"] is not None
        assert body["portfolio"]["position_count"] == 0
        assert client.get(f"{BASE}/{portfolio_id}/positions").json()["count"] == 0

    def test_strategy_hold_places_no_order(
        self, client: TestClient, db_session: Session, stock: Stock
    ):
        """A flat tape satisfies no entry gate, so the rules place no order at all."""
        seed_tape(db_session, stock, bars_from_closes([Decimal("100")] * 120))
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        body = client.post(
            f"{BASE}/{portfolio_id}/strategy-orders", json={"stock_id": stock.id}
        ).json()
        assert body["action"] == "hold"
        assert body["order"] is None
        assert body["trade"] is None
        assert body["portfolio"]["order_count"] == 0
        assert -100 <= body["composite_score"] <= 100

    def test_unknown_strategy_is_rejected(self, client: TestClient, db_session: Session, stock: Stock):
        seed_tape(db_session, stock, bars_from_closes(decline_then_rally(70, 25)))
        portfolio = create_portfolio(client)
        response = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/strategy-orders",
            json={"stock_id": stock.id, "strategy": "does_not_exist"},
        )
        assert response.status_code == 422


# ======================================================================
# Scenario 5 — insufficient cash
# ======================================================================


class TestScenarioInsufficientCash:
    def test_buy_beyond_available_cash_is_rejected_and_changes_nothing(
        self, client: TestClient, stock: Stock
    ):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        response = buy(client, portfolio_id, stock.id, 1000, "1000")
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "INSUFFICIENT_CASH"
        assert "Insufficient cash" in error["message"]

        after = client.get(f"{BASE}/{portfolio_id}").json()
        assert dec(after["current_cash"]) == dec(portfolio["current_cash"])
        assert dec(after["equity"]) == dec(portfolio["equity"])
        assert after["position_count"] == 0
        assert dec(after["total_pnl"]) == Decimal("0.00")
        assert after["trade_count"] == 0
        # The rejection is still recorded so order history explains itself.
        assert after["order_count"] == 1

    def test_rejection_is_recorded_with_a_reason(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        buy(client, portfolio["portfolio_id"], stock.id, 1000, "1000")

        orders = client.get(
            f"{BASE}/{portfolio['portfolio_id']}/orders", params={"status": "REJECTED"}
        ).json()
        assert orders["count"] == 1
        rejected = orders["data"][0]
        assert rejected["rejection_code"] == "INSUFFICIENT_CASH"
        assert rejected["rejection_reason"]
        assert rejected["execution_price"] is None


# ======================================================================
# Scenario 6 — risk limit exceeded
# ======================================================================


class TestScenarioRiskLimit:
    def test_risk_limit_rejects_the_order_and_preserves_state(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(
            client, name="Capped", risk_limits={"max_position_allocation_pct": "20"}
        )
        portfolio_id = portfolio["portfolio_id"]

        response = buy(client, portfolio_id, stock.id, 90, "1000")
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "MAX_POSITION_ALLOCATION_EXCEEDED"
        assert error["details"]["rule"] == "max_position_allocation_pct"

        after = client.get(f"{BASE}/{portfolio_id}").json()
        assert dec(after["current_cash"]) == dec(portfolio["current_cash"])
        assert after["position_count"] == 0
        assert after["trade_count"] == 0

    def test_order_inside_the_limit_is_accepted(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(
            client, name="Capped", risk_limits={"max_position_allocation_pct": "20"}
        )
        assert buy(client, portfolio["portfolio_id"], stock.id, 10, "1000").status_code == 201

    def test_max_position_quantity_is_enforced(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(
            client, name="Qty capped", risk_limits={"max_position_quantity": 5}
        )
        portfolio_id = portfolio["portfolio_id"]

        assert buy(client, portfolio_id, stock.id, 5, "100").status_code == 201
        response = buy(client, portfolio_id, stock.id, 1, "100")
        assert response.status_code == 409
        assert response.json()["error"]["details"]["rule"] == "max_position_quantity"

        positions = client.get(f"{BASE}/{portfolio_id}/positions").json()
        assert positions["data"][0]["quantity"] == 5

    def test_max_position_value_is_enforced(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(
            client, name="Value capped", risk_limits={"max_position_value": "1000"}
        )
        portfolio_id = portfolio["portfolio_id"]

        assert buy(client, portfolio_id, stock.id, 10, "100").status_code == 201
        response = buy(client, portfolio_id, stock.id, 10, "100")
        assert response.status_code == 409
        assert response.json()["error"]["details"]["rule"] == "max_position_value"

    def test_drawdown_protection_blocks_new_buys_but_allows_sells(
        self, client: TestClient, stock: Stock
    ):
        """After an 11% loss the 10% drawdown rule blocks new buys but not exits."""
        portfolio = create_portfolio(
            client, name="Drawdown capped", risk_limits={"max_drawdown_pct": "10"}
        )
        portfolio_id = portfolio["portfolio_id"]

        buy(client, portfolio_id, stock.id, 900, "100")  # 90% invested at 100
        client.post(
            f"{BASE}/{portfolio_id}/valuation",
            json={"prices": [{"stock_id": stock.id, "price": "88"}]},
        )
        client.post(f"{BASE}/{portfolio_id}/snapshots", json={})

        blocked = buy(client, portfolio_id, stock.id, 1, "88")
        assert blocked.status_code == 409
        assert blocked.json()["error"]["details"]["rule"] == "max_drawdown_pct"

        # Selling out is always allowed so the portfolio can de-risk.
        assert sell(client, portfolio_id, stock.id, 900, "88").status_code == 201


# ======================================================================
# Portfolio states
# ======================================================================


class TestPortfolioStates:
    def test_active_portfolio_accepts_orders(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        assert buy(client, portfolio["portfolio_id"], stock.id, 1, "100").status_code == 201

    def test_paused_portfolio_rejects_orders(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        response = client.patch(f"{BASE}/{portfolio_id}/status", json={"status": "PAUSED"})
        assert response.status_code == 200
        assert response.json()["status"] == "PAUSED"

        rejected = buy(client, portfolio_id, stock.id, 1, "100")
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "PORTFOLIO_INACTIVE"

        after = client.get(f"{BASE}/{portfolio_id}").json()
        assert dec(after["current_cash"]) == Decimal(CAPITAL)
        assert after["trade_count"] == 0

    def test_closed_portfolio_rejects_orders(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        client.patch(f"{BASE}/{portfolio_id}/status", json={"status": "CLOSED"})

        rejected = buy(client, portfolio_id, stock.id, 1, "100")
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "PORTFOLIO_INACTIVE"

    def test_closed_portfolio_cannot_be_reopened(self, client: TestClient):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        client.patch(f"{BASE}/{portfolio_id}/status", json={"status": "CLOSED"})

        response = client.patch(f"{BASE}/{portfolio_id}/status", json={"status": "ACTIVE"})
        assert response.status_code == 422

    def test_paused_portfolio_can_be_reactivated(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        client.patch(f"{BASE}/{portfolio_id}/status", json={"status": "PAUSED"})
        client.patch(f"{BASE}/{portfolio_id}/status", json={"status": "ACTIVE"})
        assert buy(client, portfolio_id, stock.id, 1, "100").status_code == 201


# ======================================================================
# Sell protection and position mechanics
# ======================================================================


class TestSellProtection:
    def test_selling_without_a_position_is_rejected(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        response = sell(client, portfolio_id, stock.id, 1, "100")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "INSUFFICIENT_POSITION"

        after = client.get(f"{BASE}/{portfolio_id}").json()
        assert dec(after["current_cash"]) == Decimal(CAPITAL)
        assert after["trade_count"] == 0

    def test_selling_more_than_owned_is_rejected(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 5, "100")
        before = client.get(f"{BASE}/{portfolio_id}").json()

        response = sell(client, portfolio_id, stock.id, 6, "100")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "INSUFFICIENT_POSITION"

        after = client.get(f"{BASE}/{portfolio_id}").json()
        assert dec(after["current_cash"]) == dec(before["current_cash"])
        assert after["position_count"] == 1
        assert client.get(f"{BASE}/{portfolio_id}/positions").json()["data"][0]["quantity"] == 5

    def test_partial_and_full_exits(self, client: TestClient, stock: Stock):
        """A partial exit keeps the average price; a full exit closes the position."""
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "100")
        buy(client, portfolio_id, stock.id, 10, "120")

        partial = sell(client, portfolio_id, stock.id, 5, "130").json()
        assert partial["portfolio"]["position_count"] == 1
        remaining = client.get(f"{BASE}/{portfolio_id}/positions").json()["data"][0]
        assert remaining["quantity"] == 15
        assert dec(remaining["average_entry_price"]) == Decimal("110.0000")
        # Realised P&L covers only the 5 shares sold: 5 * (130 - 110) before fees.
        assert dec(partial["trade"]["realized_pnl"]) < Decimal("100.00")

        full = sell(client, portfolio_id, stock.id, 15, "130").json()
        assert full["portfolio"]["position_count"] == 0
        assert client.get(f"{BASE}/{portfolio_id}/positions").json()["count"] == 0


# ======================================================================
# Fees, precision and P&L
# ======================================================================


class TestFeesAndPrecision:
    def test_fees_are_charged_on_both_sides_and_reduce_cash(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        bought = buy(client, portfolio_id, stock.id, 10, "1000").json()
        buy_fee = fee_for(10, "1000")
        assert dec(bought["trade"]["fees"]) == buy_fee
        assert dec(bought["trade"]["net_cash_impact"]) == -(Decimal("10000") + buy_fee)

        sold = sell(client, portfolio_id, stock.id, 10, "1100").json()
        sell_fee = fee_for(10, "1100")
        assert dec(sold["trade"]["fees"]) == sell_fee
        assert dec(sold["trade"]["net_cash_impact"]) == Decimal("11000") - sell_fee
        assert dec(sold["trade"]["realized_pnl"]) == quant(
            Decimal("1000") - sell_fee - buy_fee
        )

    def test_pnl_identity_reconciles_across_a_round_trip(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "1000")
        sell(client, portfolio_id, stock.id, 10, "1100")

        summary = client.get(f"{BASE}/{portfolio_id}").json()
        assert (
            dec(summary["realized_pnl"])
            + dec(summary["unrealized_pnl"])
            - dec(summary["open_entry_fees"])
            == dec(summary["total_pnl"])
        )
        assert dec(summary["equity"]) == Decimal(CAPITAL) + dec(summary["total_pnl"])

    def test_decimal_precision_is_preserved_over_http(self, client: TestClient, stock: Stock):
        """A price of 33.33 must not come back as a float artefact."""
        portfolio = create_portfolio(client)
        body = buy(client, portfolio["portfolio_id"], stock.id, 3, "33.33").json()

        assert dec(body["order"]["gross_value"]) == Decimal("99.99")
        assert dec(body["order"]["fees"]) == Decimal("0.10")
        assert dec(body["portfolio"]["current_cash"]) == quant(
            Decimal("100000") - Decimal("99.99") - Decimal("0.10")
        )

    def test_minimum_fee_configuration_is_honoured(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(
            client,
            name="Min fee",
            trading_costs={"commission_rate": "0.001", "min_fee": "5", "slippage_rate": "0"},
        )
        body = buy(client, portfolio["portfolio_id"], stock.id, 1, "100").json()
        assert dec(body["order"]["fees"]) == Decimal("5.00")

    def test_slippage_fills_adversely(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(
            client,
            name="Slippage",
            trading_costs={"commission_rate": "0", "min_fee": "0", "slippage_rate": "0.01"},
        )
        portfolio_id = portfolio["portfolio_id"]
        bought = buy(client, portfolio_id, stock.id, 1, "100").json()
        assert dec(bought["order"]["execution_price"]) == Decimal("101.0000")

        sold = sell(client, portfolio_id, stock.id, 1, "100").json()
        assert dec(sold["order"]["execution_price"]) == Decimal("99.0000")


# ======================================================================
# Idempotency and duplicate protection
# ======================================================================


class TestIdempotency:
    def test_repeating_an_idempotency_key_does_not_execute_twice(
        self, client: TestClient, stock: Stock
    ):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        payload = {
            "stock_id": stock.id,
            "side": "BUY",
            "quantity": 10,
            "requested_price": "100",
            "idempotency_key": "order-abc",
        }

        first = client.post(f"{BASE}/{portfolio_id}/orders", json=payload).json()
        assert first["idempotent_replay"] is False
        cash_after_first = dec(first["portfolio"]["current_cash"])

        second = client.post(f"{BASE}/{portfolio_id}/orders", json=payload).json()
        assert second["idempotent_replay"] is True
        assert second["order"]["order_id"] == first["order"]["order_id"]
        assert second["trade"]["trade_id"] == first["trade"]["trade_id"]
        assert dec(second["portfolio"]["current_cash"]) == cash_after_first
        assert second["portfolio"]["trade_count"] == 1
        assert second["portfolio"]["order_count"] == 1

    def test_idempotent_replay_reflects_the_current_price(
        self, client: TestClient, stock: Stock
    ):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        payload = {
            "stock_id": stock.id,
            "side": "BUY",
            "quantity": 10,
            "requested_price": "100",
            "idempotency_key": "stable",
        }
        first = client.post(f"{BASE}/{portfolio_id}/orders", json=payload).json()
        client.post(
            f"{BASE}/{portfolio_id}/valuation",
            json={"prices": [{"stock_id": stock.id, "price": "500"}]},
        )
        second = client.post(f"{BASE}/{portfolio_id}/orders", json=payload).json()

        # No second execution happened, so only the revaluation changed equity.
        assert dec(second["portfolio"]["equity"]) == dec(first["portfolio"]["equity"]) + Decimal(
            "4000.00"
        )
        assert second["portfolio"]["trade_count"] == 1


# ======================================================================
# Snapshots, performance and transaction safety
# ======================================================================


class TestSnapshotsAndPerformance:
    def test_snapshots_build_a_consistent_equity_curve(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        first_snapshot = client.post(f"{BASE}/{portfolio_id}/snapshots", json={}).json()
        assert dec(first_snapshot["equity"]) == Decimal(CAPITAL)
        assert dec(first_snapshot["total_pnl"]) == Decimal("0.00")

        buy(client, portfolio_id, stock.id, 10, "1000")
        client.post(
            f"{BASE}/{portfolio_id}/valuation",
            json={"prices": [{"stock_id": stock.id, "price": "1100"}]},
        )
        second_snapshot = client.post(f"{BASE}/{portfolio_id}/snapshots", json={}).json()
        assert dec(second_snapshot["unrealized_pnl"]) == Decimal("1000.00")
        assert dec(second_snapshot["equity"]) > dec(first_snapshot["equity"])

        curve = client.get(f"{BASE}/{portfolio_id}/snapshots").json()
        assert curve["total"] == 2
        assert dec(curve["data"][0]["equity"]) == dec(first_snapshot["equity"])
        assert dec(curve["data"][1]["equity"]) == dec(second_snapshot["equity"])

    def test_snapshot_can_apply_prices_and_still_reconcile(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "1000")

        snapshot = client.post(
            f"{BASE}/{portfolio_id}/snapshots",
            json={"prices": [{"stock_id": stock.id, "price": "1500"}]},
        ).json()

        assert dec(snapshot["market_value"]) == Decimal("15000.00")
        assert dec(snapshot["unrealized_pnl"]) == Decimal("5000.00")
        assert dec(snapshot["equity"]) == dec(snapshot["cash"]) + dec(snapshot["market_value"])

    def test_performance_reports_drawdown(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 900, "100")
        client.post(
            f"{BASE}/{portfolio_id}/valuation",
            json={"prices": [{"stock_id": stock.id, "price": "80"}]},
        )
        client.post(f"{BASE}/{portfolio_id}/snapshots", json={})

        performance = client.get(f"{BASE}/{portfolio_id}/performance").json()
        # The peak is the initial capital: the portfolio was never worth more than that.
        assert dec(performance["peak_equity"]) == Decimal(CAPITAL)
        expected_drawdown = quant(
            (Decimal(CAPITAL) - dec(performance["equity"])) / Decimal(CAPITAL) * Decimal("100")
        )
        assert dec(performance["current_drawdown_pct"]) == expected_drawdown
        assert dec(performance["current_drawdown_pct"]) > 18
        assert dec(performance["max_drawdown_pct"]) > 0
        assert dec(performance["max_drawdown"]) == Decimal(CAPITAL) - dec(performance["equity"])

    def test_performance_counts_orders_and_rejections(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 1, "100")
        sell(client, portfolio_id, stock.id, 5, "100")

        performance = client.get(f"{BASE}/{portfolio_id}/performance").json()
        assert performance["order_count"] == 2
        assert performance["filled_order_count"] == 1
        assert performance["rejected_order_count"] == 1
        assert performance["trade_count"] == 1


class TestTransactionSafety:
    def test_a_failure_mid_execution_rolls_everything_back(
        self, client: TestClient, db_session: Session, stock: Stock, monkeypatch
    ):
        """A write failure after the order is staged must leave no trace at all."""
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]

        from app.services.portfolio import service as service_module

        def explode(*args, **kwargs):
            raise RuntimeError("simulated persistence failure")

        monkeypatch.setattr(service_module.paper_trading_service.trade_repo, "add", explode)
        with pytest.raises(RuntimeError):
            buy(client, portfolio_id, stock.id, 10, "1000")

        monkeypatch.undo()

        after = client.get(f"{BASE}/{portfolio_id}").json()
        assert dec(after["current_cash"]) == Decimal(CAPITAL)
        assert dec(after["equity"]) == Decimal(CAPITAL)
        assert after["position_count"] == 0
        assert after["order_count"] == 0
        assert after["trade_count"] == 0
        assert db_session.query(PortfolioOrder).count() == 0
        assert db_session.query(PortfolioTrade).count() == 0
        assert db_session.query(PortfolioPosition).count() == 0

    def test_a_rejected_order_writes_no_trade_and_no_position(
        self, client: TestClient, db_session: Session, stock: Stock
    ):
        portfolio = create_portfolio(client)
        buy(client, portfolio["portfolio_id"], stock.id, 10_000, "1000")

        assert db_session.query(PortfolioTrade).count() == 0
        assert db_session.query(PortfolioPosition).count() == 0
        assert (
            db_session.query(PortfolioOrder)
            .filter(PortfolioOrder.status == "REJECTED")
            .count()
            == 1
        )


# ======================================================================
# Validation, errors and documentation
# ======================================================================


class TestValidationAndErrors:
    def test_unknown_portfolio_returns_404(self, client: TestClient):
        response = client.get(f"{BASE}/999999")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PORTFOLIO_NOT_FOUND"

    def test_unknown_stock_returns_404(self, client: TestClient):
        portfolio = create_portfolio(client)
        response = buy(client, portfolio["portfolio_id"], 999999, 1, "100")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "STOCK_NOT_FOUND"

    def test_non_positive_quantity_is_rejected_by_validation(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        response = buy(client, portfolio["portfolio_id"], stock.id, 0, "100")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_non_positive_price_is_rejected_by_validation(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        response = buy(client, portfolio["portfolio_id"], stock.id, 1, "0")
        assert response.status_code == 422

    def test_invalid_order_side_is_rejected(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client)
        response = client.post(
            f"{BASE}/{portfolio['portfolio_id']}/orders",
            json={"stock_id": stock.id, "side": "SHORT", "quantity": 1, "requested_price": "10"},
        )
        assert response.status_code == 422

    def test_non_positive_initial_capital_is_rejected(self, client: TestClient):
        response = client.post(BASE, json={"name": "bad", "initial_capital": "0"})
        assert response.status_code == 422

    def test_out_of_range_allocation_limit_is_rejected(self, client: TestClient):
        response = client.post(
            BASE,
            json={
                "name": "bad",
                "initial_capital": CAPITAL,
                "risk_limits": {"max_position_allocation_pct": "150"},
            },
        )
        assert response.status_code == 422

    def test_list_and_summary_endpoints(self, client: TestClient, stock: Stock):
        portfolio = create_portfolio(client, name="Listed")
        buy(client, portfolio["portfolio_id"], stock.id, 1, "100")

        listing = client.get(BASE)
        assert listing.status_code == 200
        assert listing.json()["count"] == 1
        assert listing.json()["data"][0]["name"] == "Listed"
        assert listing.json()["data"][0]["order_count"] == 1

    def test_order_history_filters_and_orders_newest_first(
        self, client: TestClient, stock: Stock
    ):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 1, "100", idempotency_key="a")
        buy(client, portfolio_id, stock.id, 1, "200", idempotency_key="b")

        history = client.get(f"{BASE}/{portfolio_id}/orders").json()
        assert [item["idempotency_key"] for item in history["data"]] == ["b", "a"]

        filtered = client.get(
            f"{BASE}/{portfolio_id}/orders", params={"stock_id": stock.id}
        ).json()
        assert filtered["count"] == 2

    def test_trades_are_filterable_by_stock(
        self, client: TestClient, stock: Stock, second_stock: Stock
    ):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 1, "100")
        buy(client, portfolio_id, second_stock.id, 1, "100")

        trades = client.get(
            f"{BASE}/{portfolio_id}/trades", params={"stock_id": second_stock.id}
        ).json()
        assert trades["count"] == 1
        assert trades["data"][0]["stock_id"] == second_stock.id

    def test_openapi_documents_portfolios(self, client: TestClient):
        """Swagger/OpenAPI must describe every portfolio endpoint and schema."""
        spec = client.get("/openapi.json")
        assert spec.status_code == 200
        paths = spec.json()["paths"]

        assert "post" in paths[BASE]
        assert "get" in paths[f"{BASE}/{{portfolio_id}}"]
        assert "get" in paths[f"{BASE}/{{portfolio_id}}/positions"]
        assert "post" in paths[f"{BASE}/{{portfolio_id}}/orders"]
        assert "get" in paths[f"{BASE}/{{portfolio_id}}/orders"]
        assert "get" in paths[f"{BASE}/{{portfolio_id}}/trades"]
        assert "post" in paths[f"{BASE}/{{portfolio_id}}/valuation"]
        assert "get" in paths[f"{BASE}/{{portfolio_id}}/performance"]
        assert "post" in paths[f"{BASE}/{{portfolio_id}}/snapshots"]
        assert "post" in paths[f"{BASE}/{{portfolio_id}}/strategy-orders"]

        components = spec.json()["components"]["schemas"]
        for schema in (
            "OrderCreate",
            "OrderResponse",
            "PortfolioCreate",
            "PortfolioPerformanceResponse",
            "PortfolioResponse",
            "PortfolioSnapshotResponse",
            "PortfolioValuationRequest",
            "PortfolioValuationResponse",
            "PositionResponse",
            "RiskLimitSettings",
            "TradeResponse",
            "TradingCostSettings",
        ):
            # A model used in both a request and a response is documented as two variants
            # (``-Input`` accepts a number or a string, ``-Output`` is a string) because
            # Decimal serialises losslessly as a string.
            assert any(
                name == schema or name.startswith(f"{schema}-") for name in components
            ), schema

        # Enums are documented so a consumer can discover the valid values.
        assert "OrderStatus" in components
        assert "PortfolioStatus" in components

    def test_swagger_docs_are_served(self, client: TestClient):
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200


class TestPersistenceAndPaperOnly:
    def test_state_is_persisted_in_the_database(
        self, client: TestClient, db_session: Session, stock: Stock
    ):
        portfolio = create_portfolio(client)
        portfolio_id = portfolio["portfolio_id"]
        buy(client, portfolio_id, stock.id, 10, "1000")

        row = (
            db_session.query(PortfolioOrder)
            .filter(PortfolioOrder.portfolio_id == portfolio_id)
            .one()
        )
        assert row.status == "FILLED"
        assert row.trading_mode == "PAPER"
        assert row.execution_price == Decimal("1000")

        position = (
            db_session.query(PortfolioPosition)
            .filter(PortfolioPosition.portfolio_id == portfolio_id)
            .one()
        )
        assert position.quantity == 10
        assert position.average_entry_price == Decimal("1000.0000")

        assert (
            db_session.query(PortfolioTrade)
            .filter(PortfolioTrade.portfolio_id == portfolio_id)
            .count()
            == 1
        )

    def test_trades_are_immutable_after_execution(self):
        """The repository exposes no way to change or delete a recorded execution."""
        from app.repositories import trade_repository

        assert not hasattr(trade_repository, "update")
        assert not hasattr(trade_repository, "delete")

    def test_paper_portfolio_is_labelled_as_simulated(self, client: TestClient):
        portfolio = create_portfolio(client)
        assert portfolio["trading_mode"] == "PAPER"
        assert "PAPER TRADING SIMULATION" in portfolio["disclaimer"]

    def test_no_brokerage_integration_exists(self):
        """Phase 9 must contain no brokerage or live-order integration."""
        from pathlib import Path

        package = Path(__file__).resolve().parents[1] / "app"
        forbidden = ("zerodha", "upstox", "alpaca", "interactive_brokers", "oanda", "ccxt")
        for path in (package / "services" / "portfolio").rglob("*.py"):
            content = path.read_text().lower()
            for broker in forbidden:
                assert broker not in content, f"{path} references {broker}"

    def test_no_brokerage_credentials_are_configurable(self):
        """No broker API key may exist in the application settings."""
        from app.core.config import Settings

        fields = set(Settings.model_fields)
        broker_fields = {field for field in fields if "BROKER" in field or "ZERODHA" in field}
        assert broker_fields == set()