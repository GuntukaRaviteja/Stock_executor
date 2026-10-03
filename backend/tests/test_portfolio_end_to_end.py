"""Phase 11 end-to-end walkthrough — the specification's manual scenario, executed.

The specification asks for a manual run through the whole portfolio lifecycle. A manual run proves
it once; this file proves it on every test run, and proves it *through the API* — router, schemas,
services, repositories and database — rather than by calling the engine directly.

The scenario is the one in ``docs/phase-11.md``: ₹100,000, five candidates, a 20% position cap, 1%
risk per trade, a 10% cash reserve, an 8% stop and no fees. Stock ids are placeholders for
arithmetic; nothing here is a recommendation.
"""

from tests.phase11_helpers import (
    BASE,
    candidate,
    create,
    create_stock,
    decisions,
    portfolio,
    prices,
    rebalance,
    risk_report,
)

# Five candidates, each targeting 20,000 — 20% of 100,000. Four divide evenly and one floors,
# and the fifth is partly starved of cash, which is the case worth testing.
#
# STK5's target weight is also 20%, but by the time it is sized the earlier four have committed
# 78,850 of the 90,000 investable (the other 10,000 is the reserve), leaving 11,150 — 35 shares at
# 310, not the 64 its target weight alone would allow. The engine sizes to what the portfolio can
# fund rather than refusing the name outright.
CANDIDATES = [
    ("STK1", "1000", 20),
    ("STK2", "250", 80),
    ("STK3", "80", 250),
    ("STK4", "1450", 13),
    ("STK5", "310", 35),
]


def _allocation() -> dict:
    return {
        "sizing_method": "EQUAL_WEIGHT",
        "max_position_weight_pct": "20",
        "cash_reserve_pct": "10",
        "risk_per_trade_pct": "1",
        "stop_loss_pct": "8",
    }


def test_the_documented_walkthrough(client, db_session):
    stock_ids = {
        symbol: create_stock(db_session, symbol, sector="Technology" if symbol != "STK5" else "Energy")
        for symbol, _, _ in CANDIDATES
    }

    # --- 1-2. create and configure ------------------------------------------
    pid = create(
        client,
        capital="100000",
        fees={"commission_rate": "0", "slippage_rate": "0", "min_fee": "0"},
        allocation_config=_allocation(),
    )["portfolio_id"]

    # --- 3-5. candidates in, target weights out -----------------------------
    body = decisions(
        client,
        pid,
        {"candidates": [candidate(stock_ids[s], p) for s, p, _ in CANDIDATES], "apply": False},
    )
    assert body["equity"] == "100000.00"
    assert body["cash_reserve_required"] == "10000.00"
    # Five candidates split to exactly 20% each, which is the cap, so nothing is truncated.
    assert {item["target_weight_pct"] for item in body["decisions"]} == {"20.0000"}
    # Every name is targeted; only the last one is smaller than its weight implies, because that
    # is all the cash left after the reserve and the four earlier buys.
    assert {item["action"] for item in body["decisions"]} == {"BUY"}
    for symbol, _, quantity in CANDIDATES:
        decision = next(i for i in body["decisions"] if i["stock_id"] == stock_ids[symbol])
        assert decision["action"] == "BUY", symbol
        assert decision["target_quantity"] == quantity, (symbol, decision)
        # Every decision explains itself, one sentence per reason code.
        assert len(decision["reasons"]) == len(decision["explanation"]), symbol
    # A proposal writes nothing.
    assert portfolio(client, pid)["position_count"] == 0

    # --- 6-7. apply, then check cash, positions and equity ------------------
    applied = decisions(
        client,
        pid,
        {"candidates": [candidate(stock_ids[s], p) for s, p, _ in CANDIDATES], "apply": True},
    )
    assert all(item["applied"] for item in applied["decisions"])
    summary = portfolio(client, pid)
    assert summary["position_count"] == 5
    # 89,700 invested, leaving 10,300 of cash — the 10,000 reserve plus 300 spare.
    assert summary["current_cash"] == "10300.00"
    assert summary["market_value"] == "89700.00"
    assert summary["equity"] == "100000.00"

    # --- 8-9. rebalance: one trim, four holds   towards new targets -------------------------------
    # Drop STK1's target to 10% and leave the other four at 20%. Run against the original
    # marks, where STK2-STK4 sit at exactly 20% and drift to zero. STK5 does not: it was only
    # funded to 11% back in step 4, so it is bought up here — which is what makes this plan a
    # genuine reallocation rather than a one-way trim.
    plan = rebalance(
        client,
        pid,
        {
            "target_weights": {
                str(stock_ids["STK1"]): "10",
                str(stock_ids["STK2"]): "20",
                str(stock_ids["STK3"]): "20",
                str(stock_ids["STK4"]): "20",
                str(stock_ids["STK5"]): "20",
            },
            "apply": False,
        },
    )
    # One item per target, and the three that did not move are reported as holds rather than
    # being dropped from the plan: "checked and correct" is a result.
    assert len(plan["items"]) == 5
    holds = [item for item in plan["items"] if item["action"] == "HOLD"]
    assert len(holds) == 3, [(i["stock_id"], i["action"], i["below_threshold"]) for i in plan["items"]]
    assert all(item["below_threshold"] for item in holds)
    assert all(item["side"] is None for item in holds)
    assert {item["stock_id"] for item in holds} == {
        stock_ids["STK2"],
        stock_ids["STK3"],
        stock_ids["STK4"],
    }
    # 10% of 100,000 is 10,000, which is 10 shares at 1000; 20 are held, so 10 come out.
    trimmed = next(i for i in plan["items"] if i["stock_id"] == stock_ids["STK1"])
    assert trimmed["side"] == "SELL"
    assert trimmed["required_quantity"] == -10
    # 20% of 100,000 is 20,000, which is 64 shares at 310; 35 are held, so 29 are bought.
    topped_up = next(i for i in plan["items"] if i["stock_id"] == stock_ids["STK5"])
    assert topped_up["side"] == "BUY"
    assert topped_up["required_quantity"] == 29
    # Both are executable: the 10,000 the sell releases is what funds the 8,990 the buy needs,
    # which is why sells are proposed and prefunded before buys.
    assert plan["trade_count"] == 2
    assert all(item["blocked"] is False for item in plan["items"])
    # A proposal writes nothing.
    assert portfolio(client, pid)["position_count"] == 5


    # --- 10-11. re-mark and read unrealized P&L -----------------------------
    marked = client.post(
        f"{BASE}/{pid}/valuation",
        json={"prices": prices((stock_ids["STK1"], "1120"), (stock_ids["STK4"], "1380"))},
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["unrealized_pnl"] == "1490.00"
    summary = portfolio(client, pid)
    # +2,400 on STK1 and −910 on STK4; the other three keep their entry mark.
    assert summary["unrealized_pnl"] == "1490.00"
    assert summary["equity"] == "101490.00"

    # --- 12. sell part of a position; realised P&L follows -------------------
    sold = client.post(
        f"{BASE}/{pid}/orders",
        json={
            "stock_id": stock_ids["STK1"],
            "side": "SELL",
            "quantity": 5,
            "requested_price": "1120",
        },
    )
    assert sold.status_code == 201, sold.text
    trade = sold.json()["trade"]
    # 5 * 1120 proceeds, against 5 * 1000 of cost basis, with no fees.
    assert trade["realized_pnl"] == "600.00"
    summary = portfolio(client, pid)
    assert summary["realized_pnl"] == "600.00"
    positions = client.get(f"{BASE}/{pid}/positions").json()
    stk1 = next(p for p in positions["data"] if p["stock_id"] == stock_ids["STK1"])
    assert stk1["quantity"] == 15

    # --- 13-14. breach the stop and confirm the risk engine sees it --------
    # The stop is 8% below the 1000 entry, so 920 is the trigger and 910 is through it. A
    # crossed stop is a recommendation rather than a violation: it is a level to act at, not a
    # limit the portfolio is outside of.
    breached = risk_report(client, pid, prices=f"{stock_ids['STK1']}=910")
    assert "stop_loss_pct" not in [item["rule"] for item in breached["violations"]]
    recommendation = next(
        item for item in breached["recommendations"] if item["stock_id"] == stock_ids["STK1"]
    )
    assert recommendation["rule"] == "stop_loss_pct"
    assert recommendation["action"] == "SELL"
    assert recommendation["quantity"] == 15
    # A recommendation is not an execution: the position is still open.
    assert portfolio(client, pid)["position_count"] == 5
    forced = decisions(client, pid, {"prices": prices((stock_ids["STK1"], "910"))})
    exit_decision = next(i for i in forced["decisions"] if i["stock_id"] == stock_ids["STK1"])
    assert exit_decision["action"] == "SELL"
    assert "stop_loss_pct" in exit_decision["reasons"]

    # --- 17. snapshot -------------------------------------------------------
    snapshot = client.post(f"{BASE}/{pid}/snapshots", json={})
    assert snapshot.status_code == 201, snapshot.text
    curve = client.get(f"{BASE}/{pid}/equity-curve").json()
    assert len(curve["data"]) == 1

    # --- 18. performance ----------------------------------------------------
    performance = client.get(f"{BASE}/{pid}/performance").json()
    assert performance["equity"] == portfolio(client, pid)["equity"]
    # One closed trade, and it was a winner.
    assert performance["winning_trades"] == 1
    assert performance["win_rate_pct"] == "100.0000"
    assert performance["average_gain"] == "600.00"

    # The ledger agrees with the cash balance: one opening balance, five buys, one sell.
    ledger = client.get(f"{BASE}/{pid}/transactions").json()
    assert ledger["total"] == 6
    summary = client.get(f"{BASE}/{pid}/transactions/summary").json()
    assert summary["counts"] == {"BUY": 5, "SELL": 1}


def test_a_second_identical_run_produces_identical_decisions(client, db_session):
    """Determinism, end to end: the same inputs twice, over HTTP, must agree exactly."""
    stock_ids = {symbol: create_stock(db_session, symbol) for symbol, _, _ in CANDIDATES}

    def run():
        pid = create(
            client,
            name="Repeat",
            capital="100000",
            fees={"commission_rate": "0", "slippage_rate": "0", "min_fee": "0"},
            allocation_config=_allocation(),
        )["portfolio_id"]
        return decisions(
            client,
            pid,
            {"candidates": [candidate(stock_ids[s], p) for s, p, _ in CANDIDATES], "apply": False},
        )

    first, second = run(), run()
    assert [item["stock_id"] for item in first["decisions"]] == [
        item["stock_id"] for item in second["decisions"]
    ]
    assert first["decisions"] == second["decisions"]