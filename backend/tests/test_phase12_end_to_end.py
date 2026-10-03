"""Phase 12 end-to-end walkthrough — the whole portfolio lifecycle, executed over HTTP.

A manual run proves the flow once. This file proves it on every test run, and proves it *through
the API* — router, schemas, services, repositories and database — rather than by calling the
engine directly.

One walkthrough, in the order an operator would actually do it:

1. create a portfolio in a stated currency, with fees and slippage off so every expected amount
   is the stated arithmetic, and with the two new Phase 12 limits configured
2. read the summary back and confirm the currency and cash are as created
3. size a position and buy it
4. reject an order below the minimum trade value, and confirm the reason names that rule
5. mark the position down, snapshot, and watch the daily-loss rule close the day to new BUYs
6. confirm a SELL is still allowed while BUYs are refused — the loss limit must not trap a
   position
7. value the portfolio at supplied prices and read the marks back
8. record the equity curve and read performance, including the daily figures
9. read the risk report and the rejection codes
10. deposit capital, pause, confirm trading is locked out while capital still moves, then close
11. confirm the whole recorded history survives the closure and is still readable
12. run the identical script a second time and assert the two runs agree on every money figure

Stock ids are placeholders for arithmetic. Nothing here is a recommendation.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.stock import Stock

BASE = "/api/v1/portfolios"


def dec(value) -> Decimal:
    """Read a money field out of a JSON body without passing it through a float."""
    return Decimal(str(value))


@pytest.fixture
def e2e_stock(db_session: Session) -> Stock:
    record = Stock(symbol="E2ECO", company_name="End To End Co", exchange="NSE", market="INDIA")
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


def _run_the_lifecycle(client: TestClient, stock_id: int, label: str) -> dict:
    """The whole lifecycle, returning every figure worth comparing between runs.

    Fees and slippage are zero so that each asserted amount is exactly the arithmetic written
    here, with no rounding to explain.
    """
    collected: dict = {}

    # --- 1. create -----------------------------------------------------------
    created = client.post(
        BASE,
        json={
            "name": f"End To End {label}",
            "initial_capital": "100000",
            "currency": "INR",
            "trading_costs": {"commission_rate": "0", "min_fee": "0", "slippage_rate": "0"},
            "risk_limits": {
                "max_position_allocation_pct": "50",
                "max_daily_loss_pct": "5",
                "min_trade_value": "1000",
            },
        },
    )
    assert created.status_code == 201, created.text
    portfolio = created.json()
    pid = portfolio["portfolio_id"]
    collected["currency"] = portfolio["currency"]
    collected["initial_capital"] = dec(portfolio["initial_capital"])

    # --- 2. read the summary back -------------------------------------------
    summary = client.get(f"{BASE}/{pid}").json()
    assert summary["name"] == f"End To End {label}"
    assert summary["currency"] == "INR"
    assert dec(summary["current_cash"]) == Decimal("100000")
    assert dec(summary["equity"]) == Decimal("100000")
    assert summary["status"] == "ACTIVE"

    # --- 3. size, then buy ---------------------------------------------------
    sized = client.post(
        f"{BASE}/{pid}/position-size",
        json={
            "stock_id": stock_id,
            "equity": "100000",
            "available_cash": "100000",
            "method": "FIXED_CAPITAL",
            "capital": "20000",
            "price": "100",
        },
    )
    assert sized.status_code == 200, sized.text
    collected["sized_quantity"] = sized.json()["quantity"]

    bought = client.post(
        f"{BASE}/{pid}/orders",
        json={
            "stock_id": stock_id,
            "side": "BUY",
            "quantity": 200,
            "requested_price": "100",
        },
    )
    assert bought.status_code == 201, bought.text
    submitted = bought.json()
    # A submission returns the order, the trade it produced and the resulting portfolio.
    order, trade = submitted["order"], submitted["trade"]
    assert order["status"] == "FILLED"
    assert dec(order["execution_price"]) == Decimal("100")
    assert dec(order["gross_value"]) == Decimal("20000")
    assert dec(order["fees"]) == Decimal("0")
    assert trade is not None and trade["quantity"] == 200
    assert dec(submitted["portfolio"]["current_cash"]) == Decimal("80000")
    collected["filled_quantity"] = trade["quantity"]

    position = client.get(f"{BASE}/{pid}/positions/{stock_id}").json()
    assert position["quantity"] == 200
    assert dec(position["average_entry_price"]) == Decimal("100")
    assert dec(position["cost_basis"]) == Decimal("20000")
    assert dec(position["market_value"]) == Decimal("20000")

    after_buy = client.get(f"{BASE}/{pid}").json()
    assert dec(after_buy["current_cash"]) == Decimal("80000")
    assert dec(after_buy["equity"]) == Decimal("100000")
    assert dec(after_buy["exposure_pct"]) == Decimal("20.0000")
    collected["equity_after_buy"] = dec(after_buy["equity"])

    # --- 4. an order below the minimum is refused, naming that rule ----------
    dust = client.post(
        f"{BASE}/{pid}/orders",
        json={
            "stock_id": stock_id,
            "side": "BUY",
            "quantity": 1,
            "requested_price": "100",
        },
    )
    assert dust.status_code == 409, dust.text
    refusal = dust.json()
    assert refusal["success"] is False
    # The refusal carries the code of the rule that actually fired, not one blanket code.
    assert refusal["error"]["code"] == "MIN_TRADE_VALUE_BELOW"
    # The message explains the rule in words and quotes both figures.
    assert "below the configured minimum trade value of 1000" in refusal["error"]["message"]
    assert "commits 100.00" in refusal["error"]["message"]

    # --- 5. mark down, snapshot, and eventually the day closes to new BUYs ---
    client.post(f"{BASE}/{pid}/snapshots", json={})  # opening point: equity 100000
    marked = client.post(
        f"{BASE}/{pid}/valuation",
        json={"prices": [{"stock_id": stock_id, "price": "88"}]},
    )
    assert marked.status_code == 200, marked.text
    # Cash 80000 + 200 * 88 = 17600, so equity 97600: a 2.4% loss on the day.
    collected["equity_marked_down"] = dec(marked.json()["equity"])
    assert collected["equity_marked_down"] == Decimal("97600")

    client.post(
        f"{BASE}/{pid}/valuation",
        json={"prices": [{"stock_id": stock_id, "price": "85"}]},
    )
    # Cash 80000 + 200 * 85 = 17000, so equity 97000: a 3% loss, still inside the 5% limit.
    loss_day = client.post(
        f"{BASE}/{pid}/valuation",
        json={"prices": [{"stock_id": stock_id, "price": "82"}]},
    )
    # Cash 80000 + 200 * 82 = 16400, so equity 96400: a 3.6% loss.
    assert dec(loss_day.json()["equity"]) == Decimal("96400")

    # 20 * 82 = 1640, which clears the 1000 minimum, so only the daily-loss rule can refuse it.
    inside = client.post(
        f"{BASE}/{pid}/risk/check",
        json={
            "stock_id": stock_id,
            "side": "BUY",
            "quantity": 20,
            "requested_price": "82",
        },
    )
    assert inside.status_code == 200, inside.text
    assert inside.json()["accepted"] is True, "a 3.6% day is inside the 5% daily-loss limit"

    # Cross the limit: 100000 * 0.95 = 95000, so a mark below 75 takes equity under it.
    breached = client.post(
        f"{BASE}/{pid}/valuation",
        json={"prices": [{"stock_id": stock_id, "price": "74"}]},
    )
    # Cash 80000 + 200 * 74 = 14800, so equity 94800: a 5.2% loss.
    assert dec(breached.json()["equity"]) == Decimal("94800")

    refused = client.post(
        f"{BASE}/{pid}/orders",
        json={
            "stock_id": stock_id,
            "side": "BUY",
            "quantity": 20,
            "requested_price": "74",
        },
    )
    assert refused.status_code == 409, refused.text
    daily_loss = refused.json()
    assert daily_loss["error"]["code"] == "MAX_DAILY_LOSS_EXCEEDED"
    # The message states the loss, the day-open basis and the limit, and says SELLs stay open.
    assert "5.2000% below the 100000.00" in daily_loss["error"]["message"]
    assert "SELL orders remain allowed" in daily_loss["error"]["message"]

    # --- 6. a SELL is still allowed: the limit must not trap the position ----
    sold = client.post(
        f"{BASE}/{pid}/orders",
        json={
            "stock_id": stock_id,
            "side": "SELL",
            "quantity": 50,
            "requested_price": "74",
        },
    )
    assert sold.status_code == 201, sold.text
    assert sold.json()["order"]["status"] == "FILLED"
    # 100000 basis + 50 * (74 - 100) = a 1300 realised loss.
    collected["realized_pnl"] = dec(client.get(f"{BASE}/{pid}").json()["realized_pnl"])
    assert collected["realized_pnl"] == Decimal("-1300.00")

    # --- 7. valuation reads back what was marked -----------------------------
    # Cash 80000 + 50 * 74 = 83700, and the remaining 150 shares are marked at 74 = 11100.
    valuation = client.get(f"{BASE}/{pid}/valuation").json()
    assert dec(valuation["current_cash"]) == Decimal("83700")
    assert dec(valuation["equity"]) == Decimal("94800")
    assert dec(valuation["realized_pnl"]) == Decimal("-1300.00")
    assert dec(valuation["unrealized_pnl"]) == Decimal("-3900.00")
    assert dec(valuation["total_pnl"]) == Decimal("-5200")
    assert valuation["positions"][0]["quantity"] == 150

    # --- 8. the curve, and performance ---------------------------------------
    for _ in range(2):
        client.post(f"{BASE}/{pid}/snapshots", json={})

    curve = client.get(f"{BASE}/{pid}/equity-curve").json()
    assert curve["count"] >= 3
    curve_equities = [dec(point["equity"]) for point in curve["data"]]
    assert curve_equities[0] == Decimal("100000.00")
    assert curve_equities[-1] == Decimal("94800.00"), "the curve ends at the marked equity"

    # The drawdown report reads the same persisted curve rather than re-deriving it.
    drawdown = client.get(f"{BASE}/{pid}/drawdown").json()
    assert dec(drawdown["peak_equity"]) == Decimal("100000.00")
    assert dec(drawdown["current_equity"]) == Decimal("94800.00")
    assert dec(drawdown["max_drawdown"]) == Decimal("5200.00")
    assert dec(drawdown["max_drawdown_pct"]) == Decimal("5.2000")
    # The drawdown curve prepends a synthetic opening point at the initial capital, so it has
    # one more point than the snapshot curve rather than silently starting mid-history.
    assert len(drawdown["points"]) == curve["count"] + 1
    assert dec(drawdown["points"][0]["equity"]) == Decimal("100000.00")

    performance = client.get(f"{BASE}/{pid}/performance").json()
    collected["performance"] = {
        "realized_pnl": dec(performance["realized_pnl"]),
        "unrealized_pnl": dec(performance["unrealized_pnl"]),
        "equity": dec(performance["equity"]),
        "order_count": performance["order_count"],
        "trade_count": performance["trade_count"],
        "win_rate_pct": performance["win_rate_pct"],
        "currency": performance["currency"],
        "day_open_equity": dec(performance["day_open_equity"]),
        "daily_pnl": dec(performance["daily_pnl"]),
    }
    assert collected["performance"]["currency"] == "INR"
    # Four orders were submitted: the buy, the two refused buys and the sell. A refusal is
    # recorded as a REJECTED order, so the audit trail shows the attempt, not just the fills.
    assert collected["performance"]["order_count"] == 4
    assert collected["performance"]["trade_count"] == 2

    # --- 9. the risk report agrees ------------------------------------------
    risk = client.get(f"{BASE}/{pid}/risk").json()
    assert risk["portfolio_id"] == pid
    assert dec(risk["equity"]) == dec(collected["performance"]["equity"])
    assert dec(risk["cash"]) == dec(client.get(f"{BASE}/{pid}").json()["current_cash"])

    # --- 10. deposit, pause, close ------------------------------------------
    deposited = client.post(
        f"{BASE}/{pid}/deposit", json={"amount": "5000", "reason": "top up"}
    )
    assert deposited.status_code == 200, deposited.text
    # A deposit is not a gain: it raises equity and the basis together, so realised P&L is
    # untouched and only the cash balance moves.
    after_deposit = deposited.json()["portfolio"]
    assert dec(after_deposit["current_cash"]) == Decimal("88700")
    assert dec(after_deposit["equity"]) == Decimal("99800")
    assert dec(after_deposit["realized_pnl"]) == Decimal("-1300.00")
    assert deposited.json()["transaction"]["transaction_type"] == "DEPOSIT"

    paused = client.post(f"{BASE}/{pid}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "PAUSED"

    locked = client.post(
        f"{BASE}/{pid}/orders",
        json={
            "stock_id": stock_id,
            "side": "BUY",
            "quantity": 10,
            "requested_price": "80",
        },
    )
    assert locked.status_code == 409, locked.text
    assert "PAUSED" in locked.json()["error"]["message"]

    # Capital can still move while paused: the strategy stopped, not the account.
    assert client.post(
        f"{BASE}/{pid}/withdraw", json={"amount": "2500"}
    ).status_code == 200

    closed = client.post(f"{BASE}/{pid}/close")
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "CLOSED"

    # Closing is terminal but idempotent, and capital is frozen from here on.
    assert client.post(f"{BASE}/{pid}/close").status_code == 200
    assert client.post(f"{BASE}/{pid}/deposit", json={"amount": "100"}).status_code == 409

    final_order = client.post(
        f"{BASE}/{pid}/orders",
        json={
            "stock_id": stock_id,
            "side": "BUY",
            "quantity": 10,
            "requested_price": "80",
        },
    )
    assert final_order.status_code == 409, final_order.text
    assert "CLOSED" in final_order.json()["error"]["message"]

    # --- 11. the recorded history survives the closure ------------------------
    final = client.get(f"{BASE}/{pid}").json()
    assert final["status"] == "CLOSED"
    assert final["currency"] == "INR"
    # 83700 after the sell, + 5000 deposited, - 2500 withdrawn while paused.
    assert dec(final["current_cash"]) == Decimal("86200")
    assert dec(final["equity"]) == Decimal("97300")
    assert final["position_count"] == 1
    assert client.get(f"{BASE}/{pid}/trades").json()["count"] == 2
    assert client.get(f"{BASE}/{pid}/snapshots").json()["total"] >= 3
    assert client.get(f"{BASE}/{pid}/performance").status_code == 200
    assert client.get(f"{BASE}/{pid}/valuation").status_code == 200

    collected["final_cash"] = dec(final["current_cash"])
    return collected


def test_phase_12_full_lifecycle_end_to_end(client: TestClient, e2e_stock):
    """The whole Phase 12 flow, over HTTP, with every figure checked as it is produced."""
    first = _run_the_lifecycle(client, e2e_stock.id, "Run A")

    # The assertions inside the walkthrough carry the meaning; these restate the spine of it so
    # a failure names the number rather than the step.
    assert first["initial_capital"] == Decimal("100000")
    assert first["sized_quantity"] == 200
    assert first["equity_after_buy"] == Decimal("100000")
    assert first["equity_marked_down"] == Decimal("97600")
    assert first["realized_pnl"] == Decimal("-1300.00")
    assert first["final_cash"] == Decimal("86200")


def test_phase_12_full_lifecycle_is_deterministic(client: TestClient, e2e_stock):
    """Two identical runs produce identical money, counts and percentages.

    The two portfolios differ only in their name and their primary key, so the run proves the
    engine reads nothing it was not given — no clock, no network, no ambient state.
    """
    first = _run_the_lifecycle(client, e2e_stock.id, "Run A")
    second = _run_the_lifecycle(client, e2e_stock.id, "Run B")

    assert first == second


def test_phase_12_end_to_end_never_leaks_a_traceback(client: TestClient, e2e_stock):
    """Not one response anywhere in the walkthrough carries a stack trace."""
    portfolio = client.post(
        BASE,
        json={
            "name": "Leak Check",
            "initial_capital": "100000",
            "trading_costs": {"commission_rate": "0", "min_fee": "0", "slippage_rate": "0"},
        },
    ).json()
    pid = portfolio["portfolio_id"]

    bodies = [
        client.get(f"{BASE}/{pid}").text,
        client.get(f"{BASE}/{pid}/positions").text,
        client.get(f"{BASE}/{pid}/valuation").text,
        client.get(f"{BASE}/{pid}/risk").text,
        client.get(f"{BASE}/424242").text,
        client.post(f"{BASE}/{pid}/orders", json={"stock_id": e2e_stock.id}).text,
        client.post(
            f"{BASE}/{pid}/orders",
            json={
                "stock_id": e2e_stock.id,
                "side": "BUY",
                "quantity": -5,
                "requested_price": "10",
            },
        ).text,
        client.post(f"{BASE}/{pid}/snapshots", json={"as_of": "not-a-date"}).text,
    ]

    for body in bodies:
        assert "Traceback" not in body
        assert "sqlalchemy" not in body.lower()