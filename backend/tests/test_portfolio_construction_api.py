"""Phase 11 API tests — cash ledger, portfolio construction, rebalancing and the risk report.

One test per behaviour a caller can depend on, plus the two properties that are only observable
over HTTP: that a refusal changes nothing, and that a figure which cannot be computed is
``null`` rather than a misleading zero.

Responses are compared as raw strings where the value is money or a percentage, because the API
contract is decimal strings — an assertion that passed through ``float()`` would hide a
precision regression.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tests.phase11_helpers import (
    BASE,
    allocation,
    buy,
    candidate,
    create,
    create_stock,
    decisions,
    portfolio,
    prices,
    rebalance,
    risk_report,
    sell,
    transactions,
)

D = Decimal


@pytest.fixture
def stocks(db_session):
    """Three stocks in two sectors, so sector concentration can be exercised."""
    return {
        "tech_a": create_stock(db_session, "TECA", sector="Technology"),
        "tech_b": create_stock(db_session, "TECB", sector="Technology"),
        "energy": create_stock(db_session, "ENRG", sector="Energy"),
    }


# ----------------------------------------------------------------------
# Deposits and withdrawals
# ----------------------------------------------------------------------


class TestDeposits:
    def test_a_deposit_raises_cash_and_equity_by_the_amount(self, client):
        pid = create(client, capital="10000")["portfolio_id"]
        response = client.post(f"{BASE}/{pid}/deposit", json={"amount": "2500"})
        assert response.status_code == 200, response.text
        body = response.json()["portfolio"]
        assert body["current_cash"] == "12500.00"
        assert body["equity"] == "12500.00"

    def test_a_deposit_creates_no_position_and_no_trade(self, client):
        pid = create(client, capital="10000")["portfolio_id"]
        client.post(f"{BASE}/{pid}/deposit", json={"amount": "2500"})
        summary = portfolio(client, pid)
        assert summary["position_count"] == 0
        assert summary["trade_count"] == 0
        assert summary["realized_pnl"] == "0.00"

    def test_a_deposit_is_recorded_in_the_ledger(self, client):
        pid = create(client, capital="10000")["portfolio_id"]
        body = client.post(
            f"{BASE}/{pid}/deposit", json={"amount": "2500", "reason": "monthly top up"}
        ).json()
        entry = body["transaction"]
        assert entry["transaction_type"] == "DEPOSIT"
        assert entry["net_cash_impact"] == "2500.00"
        assert entry["cash_after"] == "12500.00"
        assert entry["reason"] == "monthly top up"
        assert entry["stock_id"] is None
        assert entry["quantity"] is None

    def test_a_deposit_creates_no_profit_at_all(self, client):
        # Equity rises without invested capital rising, so this is new money, not performance.
        # The P&L basis is the opening capital plus external flows, which is what keeps a
        # deposit from being reported as a gain.
        pid = create(client, capital="10000")["portfolio_id"]
        client.post(f"{BASE}/{pid}/deposit", json={"amount": "5000"})
        summary = portfolio(client, pid)
        assert summary["equity"] == "15000.00"
        assert summary["total_pnl"] == "0.00"
        assert summary["return_pct"] == "0.0000"

    def test_a_withdrawal_creates_no_loss_either(self, client):
        pid = create(client, capital="10000")["portfolio_id"]
        client.post(f"{BASE}/{pid}/withdraw", json={"amount": "2000"})
        summary = portfolio(client, pid)
        assert summary["equity"] == "8000.00"
        assert summary["total_pnl"] == "0.00"

    def test_a_deposit_does_not_distort_a_real_return(self, client, stocks):
        # A 10% gain, then a deposit that doubles the capital: the return is still 10%, because
        # the deposit joined the basis rather than the profit.
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 1000, "100")
        client.post(f"{BASE}/{pid}/valuation", json={"prices": prices((stocks["tech_a"], "110"))})
        assert portfolio(client, pid)["return_pct"] == "10.0000"
        client.post(f"{BASE}/{pid}/deposit", json={"amount": "100000"})
        summary = portfolio(client, pid)
        assert summary["total_pnl"] == "10000.00"
        assert summary["return_pct"] == "5.0000"

    def test_a_deposit_of_zero_is_refused(self, client):
        pid = create(client, capital="10000")["portfolio_id"]
        response = client.post(f"{BASE}/{pid}/deposit", json={"amount": "0"})
        assert response.status_code == 422

    def test_a_negative_deposit_is_refused(self, client):
        pid = create(client, capital="10000")["portfolio_id"]
        response = client.post(f"{BASE}/{pid}/deposit", json={"amount": "-100"})
        assert response.status_code == 422

    def test_a_deposit_to_a_missing_portfolio_is_404(self, client):
        assert client.post(f"{BASE}/999999/deposit", json={"amount": "10"}).status_code == 404

    def test_a_paused_portfolio_still_accepts_a_deposit(self, client):
        # Pausing stops trading, not accounting: a deposit books cash, not an order.
        pid = create(client, capital="10000")["portfolio_id"]
        client.patch(f"{BASE}/{pid}/status", json={"status": "PAUSED"})
        response = client.post(f"{BASE}/{pid}/deposit", json={"amount": "500"})
        assert response.status_code == 200, response.text
        assert response.json()["portfolio"]["current_cash"] == "10500.00"

    def test_a_closed_portfolio_accepts_nothing(self, client):
        pid = create(client, capital="10000")["portfolio_id"]
        client.patch(f"{BASE}/{pid}/status", json={"status": "CLOSED"})
        assert client.post(f"{BASE}/{pid}/deposit", json={"amount": "500"}).status_code == 409


class TestWithdrawals:
    def test_a_withdrawal_lowers_cash_and_equity_by_the_amount(self, client):
        pid = create(client, capital="10000")["portfolio_id"]
        response = client.post(f"{BASE}/{pid}/withdraw", json={"amount": "2500"})
        assert response.status_code == 200, response.text
        body = response.json()["portfolio"]
        assert body["current_cash"] == "7500.00"
        assert body["equity"] == "7500.00"

    def test_a_withdrawal_is_recorded_with_a_negative_impact(self, client):
        pid = create(client, capital="10000")["portfolio_id"]
        entry = client.post(f"{BASE}/{pid}/withdraw", json={"amount": "2500"}).json()[
            "transaction"
        ]
        assert entry["transaction_type"] == "WITHDRAWAL"
        assert entry["net_cash_impact"] == "-2500.00"
        assert entry["cash_after"] == "7500.00"

    def test_a_withdrawal_beyond_the_cash_is_refused_and_changes_nothing(self, client, stocks):
        pid = create(client, capital="10000")["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 80, "100")
        assert portfolio(client, pid)["current_cash"] == "2000.00"
        response = client.post(f"{BASE}/{pid}/withdraw", json={"amount": "2500"})
        assert response.status_code == 409, response.text
        summary = portfolio(client, pid)
        assert summary["current_cash"] == "2000.00"
        assert summary["equity"] == "10000.00"

    def test_a_withdrawal_does_not_liquidate_positions(self, client, stocks):
        pid = create(client, capital="10000")["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 80, "100")
        client.post(f"{BASE}/{pid}/withdraw", json={"amount": "1900"})
        summary = portfolio(client, pid)
        assert summary["current_cash"] == "100.00"
        assert summary["position_count"] == 1

    def test_a_withdrawal_can_push_cash_under_the_reserve(self, client, stocks):
        # Nothing forbids it, and the risk report then says so rather than hiding it.
        pid = create(
            client,
            capital="10000",
            risk_limits={"min_cash_reserve_pct": "20"},
        )["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 80, "100")
        client.post(f"{BASE}/{pid}/withdraw", json={"amount": "1900"})
        assert risk_report(client, pid)["violations"][0]["rule"] == "min_cash_reserve_pct"


# ----------------------------------------------------------------------
# The transaction ledger
# ----------------------------------------------------------------------


class TestTransactionLedger:
    def test_an_empty_ledger_is_empty_not_missing(self, client):
        pid = create(client)["portfolio_id"]
        body = transactions(client, pid)
        assert body["data"] == []
        assert body["total"] == 0

    def test_every_fill_is_recorded(self, client, stocks):
        pid = create(client)["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 10, "100")
        sell(client, pid, stocks["tech_a"], 4, "110")
        types = [entry["transaction_type"] for entry in transactions(client, pid)["data"]]
        assert types == ["SELL", "BUY"]

    def test_a_ledger_entry_carries_its_order_and_trade(self, client, stocks):
        pid = create(client)["portfolio_id"]
        order = buy(client, pid, stocks["tech_a"], 10, "100")
        entry = transactions(client, pid)["data"][0]
        assert entry["order_id"] == order["order"]["order_id"]
        assert entry["trade_id"] == order["trade"]["trade_id"]
        assert entry["symbol"] == "TECA"

    def test_the_ledger_replays_to_the_current_balance(self, client, stocks):
        # The invariant that makes the ledger worth keeping: initial capital plus every entry's
        # signed impact must equal the cash the portfolio reports.
        pid = create(client, capital="50000")["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        client.post(f"{BASE}/{pid}/deposit", json={"amount": "7000"})
        sell(client, pid, stocks["tech_a"], 20, "120")
        client.post(f"{BASE}/{pid}/withdraw", json={"amount": "3000"})
        body = transactions(client, pid)
        assert body["balance"] == portfolio(client, pid)["current_cash"]

    def test_the_ledger_is_newest_first(self, client, stocks):
        pid = create(client)["portfolio_id"]
        client.post(f"{BASE}/{pid}/deposit", json={"amount": "100"})
        client.post(f"{BASE}/{pid}/deposit", json={"amount": "200"})
        amounts = [entry["net_cash_impact"] for entry in transactions(client, pid)["data"]]
        assert amounts == ["200.00", "100.00"]

    def test_the_ledger_can_be_filtered_by_type(self, client, stocks):
        pid = create(client)["portfolio_id"]
        client.post(f"{BASE}/{pid}/deposit", json={"amount": "100"})
        buy(client, pid, stocks["tech_a"], 10, "100")
        body = transactions(client, pid, transaction_type="DEPOSIT")
        assert [entry["transaction_type"] for entry in body["data"]] == ["DEPOSIT"]
        assert body["total"] == 1

    def test_cash_movements_and_trades_stay_independently_auditable(self, client, stocks):
        pid = create(client)["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 10, "100")
        client.post(f"{BASE}/{pid}/deposit", json={"amount": "250"})
        assert transactions(client, pid, transaction_type="DEPOSIT")["total"] == 1
        assert transactions(client, pid, transaction_type="BUY")["total"] == 1

    def test_the_ledger_can_be_filtered_by_stock(self, client, stocks):
        pid = create(client)["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 10, "100")
        buy(client, pid, stocks["energy"], 10, "100")
        body = transactions(client, pid, stock_id=stocks["energy"])
        assert body["total"] == 1
        assert body["data"][0]["symbol"] == "ENRG"

    def test_a_bare_end_date_covers_the_whole_day(self, client):
        pid = create(client)["portfolio_id"]
        stamp = datetime(2026, 1, 15, 18, 30, tzinfo=timezone.utc).replace(tzinfo=None)
        response = client.post(
            f"{BASE}/{pid}/deposit", json={"amount": "100", "as_of": stamp.isoformat()}
        )
        assert response.status_code == 200, response.text
        same_day = transactions(client, pid, end_date="2026-01-15")
        assert same_day["total"] == 1
        assert transactions(client, pid, end_date="2026-01-14")["total"] == 0

    def test_the_ledger_can_be_paged(self, client):
        pid = create(client)["portfolio_id"]
        for amount in ("100", "200", "300"):
            client.post(f"{BASE}/{pid}/deposit", json={"amount": amount})
        page = transactions(client, pid, skip=1, limit=1)
        assert page["count"] == 1
        assert page["total"] == 3
        assert page["data"][0]["net_cash_impact"] == "200.00"

    def test_the_ledger_counts_itself_by_type(self, client, stocks):
        pid = create(client)["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 10, "100")
        client.post(f"{BASE}/{pid}/deposit", json={"amount": "100"})
        client.post(f"{BASE}/{pid}/withdraw", json={"amount": "50"})
        response = client.get(f"{BASE}/{pid}/transactions/summary")
        assert response.status_code == 200, response.text
        assert response.json()["counts"] == {"BUY": 1, "DEPOSIT": 1, "WITHDRAWAL": 1}

    def test_the_ledger_of_a_missing_portfolio_is_404(self, client):
        assert client.get(f"{BASE}/999999/transactions").status_code == 404

    def test_a_rejected_order_still_explains_itself_in_the_ledger(self, client, stocks):
        pid = create(client, capital="1000")["portfolio_id"]
        response = client.post(
            f"{BASE}/{pid}/orders",
            json={
                "stock_id": stocks["tech_a"],
                "side": "BUY",
                "quantity": 100,
                "requested_price": "100",
            },
        )
        assert response.status_code == 409
        # A refused order moves no cash, so it must not appear in the cash ledger.
        assert transactions(client, pid)["total"] == 0


# ----------------------------------------------------------------------
# Allocation configuration
# ----------------------------------------------------------------------


class TestAllocationConfig:
    def test_the_configuration_is_persisted_and_echoed(self, client):
        body = create(client, allocation_config=allocation())["allocation_config"]
        assert body["sizing_method"] == "EQUAL_WEIGHT"
        assert body["max_position_weight_pct"] == "40.0000"
        assert body["stop_loss_pct"] == "10.0000"
        assert body["cash_reserve_pct"] == "10.0000"

    def test_the_configuration_survives_a_reload(self, client):
        pid = create(client, allocation_config=allocation())["portfolio_id"]
        assert portfolio(client, pid)["allocation_config"]["stop_loss_pct"] == "10.0000"

    def test_a_portfolio_without_one_reports_the_defaults(self, client):
        body = create(client)["allocation_config"]
        assert body["sizing_method"] == "EQUAL_WEIGHT"
        assert body["cash_reserve_pct"] is not None

    def test_the_configuration_can_be_replaced(self, client):
        pid = create(client, allocation_config=allocation())["portfolio_id"]
        response = client.patch(
            f"{BASE}/{pid}", json={"allocation_config": allocation(stop_loss_pct="5")}
        )
        assert response.status_code == 200, response.text
        assert response.json()["allocation_config"]["stop_loss_pct"] == "5.0000"

    def test_risk_based_sizing_without_a_stop_is_refused(self, client):
        # Without a stop there is no per-share risk, so no defensible position size exists.
        response = client.post(
            BASE,
            json={
                "name": "No stop",
                "initial_capital": "10000",
                "allocation_config": {"sizing_method": "RISK_BASED"},
            },
        )
        assert response.status_code == 422

    def test_a_cap_above_one_hundred_is_refused(self, client):
        response = client.post(
            BASE,
            json={
                "name": "Over",
                "initial_capital": "10000",
                "allocation_config": {"max_position_weight_pct": "150"},
            },
        )
        assert response.status_code == 422


# ----------------------------------------------------------------------
# Construction decisions
# ----------------------------------------------------------------------


class TestDecisions:
    def test_a_decision_is_a_recommendation_and_writes_nothing(self, client, stocks):
        pid = create(client, allocation_config=allocation())["portfolio_id"]
        body = decisions(client, pid, {"candidates": [candidate(stocks["tech_a"], "100")]})
        assert body["applied"] is False
        assert body["decisions"][0]["applied"] is False
        assert portfolio(client, pid)["position_count"] == 0

    def test_an_untouched_portfolio_is_proposed_an_equal_weight_buy(self, client, stocks):
        pid = create(client, allocation_config=allocation())["portfolio_id"]
        body = decisions(
            client,
            pid,
            {"candidates": [candidate(stocks["tech_a"], "100"), candidate(stocks["energy"], "100")]},
        )
        assert body["candidate_count"] == 2
        assert body["eligible_count"] == 2
        # 100/2 = 50%, truncated to the 40% cap.
        assert body["decisions"][0]["target_weight_pct"] == "40.0000"

    def test_the_target_quantity_follows_from_the_weight_and_the_price(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        body = decisions(client, pid, {"candidates": [candidate(stocks["tech_a"], "100")]})
        # 40% of 100000 = 40000, at 100 a share.
        assert body["decisions"][0]["target_quantity"] == 400

    def test_the_cash_reserve_is_held_back(self, client, stocks):
        pid = create(
            client,
            capital="10000",
            allocation_config=allocation(max_position_weight_pct="80", cash_reserve_pct="10"),
        )["portfolio_id"]
        body = decisions(client, pid, {"candidates": [candidate(stocks["tech_a"], "100")]})
        assert body["cash_reserve_required"] == "1000.00"

    def test_the_reserve_actually_limits_the_buy(self, client, stocks):
        pid = create(
            client,
            capital="10000",
            allocation_config=allocation(max_position_weight_pct="80", cash_reserve_pct="50"),
        )["portfolio_id"]
        body = decisions(client, pid, {"candidates": [candidate(stocks["tech_a"], "100")]})
        # 80% of 10000 = 8000 is the weight target, but only 5000 is free after the 5000
        # reserve, and the engine must never spend the reserve.
        assert body["decisions"][0]["target_quantity"] == 50

    def test_every_decision_explains_itself(self, client, stocks):
        pid = create(client, allocation_config=allocation())["portfolio_id"]
        body = decisions(client, pid, {"candidates": [candidate(stocks["tech_a"], "100")]})
        decision = body["decisions"][0]
        assert decision["reasons"]
        assert len(decision["reasons"]) == len(decision["explanation"])

    def test_a_candidate_with_no_price_anywhere_is_skipped_with_a_reason(self, client, stocks):
        pid = create(client, allocation_config=allocation())["portfolio_id"]
        body = decisions(
            client, pid, {"candidates": [{"stock_id": stocks["tech_a"]}]}
        )
        assert body["decisions"] == []
        assert body["skipped"][0]["eligible"] is False
        assert body["skipped"][0]["reasons"]

    def test_a_fully_funded_portfolio_holds_and_buys_nothing(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        body = decisions(
            client, pid, {"candidates": [candidate(stocks["tech_a"], "100")], "apply": False}
        )
        action = body["decisions"][0]["action"]
        assert action in ("HOLD", "REDUCE")
        assert body["decisions"][0]["target_quantity"] == 400

    def test_a_reached_stop_loss_is_a_sell(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        # The stop is 10% below the 100 entry, so 90 is exactly the trigger.
        body = decisions(
            client,
            pid,
            {"prices": prices((stocks["tech_a"], "90")), "sizing_method": "EQUAL_WEIGHT"},
        )
        held = [item for item in body["decisions"] if item["stock_id"] == stocks["tech_a"]]
        assert held[0]["action"] == "SELL"
        assert held[0]["target_quantity"] == 0
        assert "stop_loss_pct" in held[0]["reasons"]

    def test_a_reached_take_profit_is_a_reduce(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        body = decisions(
            client,
            pid,
            {"prices": prices((stocks["tech_a"], "125")), "sizing_method": "EQUAL_WEIGHT"},
        )
        held = [item for item in body["decisions"] if item["stock_id"] == stocks["tech_a"]]
        # The target weight has not moved, so the position is trimmed rather than closed.
        assert held[0]["action"] == "REDUCE"
        assert "take_profit_pct" in held[0]["reasons"]

    def test_with_neither_candidates_nor_a_screener_only_held_positions_are_examined(
        self, client, stocks
    ):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        body = decisions(client, pid, {"prices": prices((stocks["tech_a"], "100"))})
        assert body["candidate_count"] == 1
        assert [item["stock_id"] for item in body["decisions"]] == [stocks["tech_a"]]

    def test_candidates_and_a_screener_are_mutually_exclusive(self, client, stocks):
        pid = create(client)["portfolio_id"]
        response = client.post(
            f"{BASE}/{pid}/decisions",
            json={
                "candidates": [candidate(stocks["tech_a"], "100")],
                "screener": {"min_momentum_score": 50},
            },
        )
        assert response.status_code == 422

    def test_applying_a_decision_places_a_simulated_order(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        body = decisions(
            client,
            pid,
            {"candidates": [candidate(stocks["tech_a"], "100")], "apply": True},
        )
        decision = body["decisions"][0]
        assert decision["applied"] is True
        assert decision["order_id"] is not None
        summary = portfolio(client, pid)
        assert summary["position_count"] == 1
        assert summary["trade_count"] == 1

    def test_a_blocked_decision_is_returned_but_not_applied(self, client, stocks):
        pid = create(
            client,
            capital="10000",
            allocation_config=allocation(),
            risk_limits={"max_trade_allocation_pct": "5"},
        )["portfolio_id"]
        body = decisions(
            client, pid, {"candidates": [candidate(stocks["tech_a"], "100")], "apply": True}
        )
        decision = body["decisions"][0]
        assert decision["blocked"] is True
        assert decision["applied"] is False
        assert decision["order_id"] is None
        assert portfolio(client, pid)["position_count"] == 0

    def test_a_blocked_decision_names_the_rule(self, client, stocks):
        pid = create(
            client,
            capital="10000",
            allocation_config=allocation(),
            risk_limits={"max_trade_allocation_pct": "5"},
        )["portfolio_id"]
        body = decisions(client, pid, {"candidates": [candidate(stocks["tech_a"], "100")]})
        assert "max_trade_allocation_pct" in body["decisions"][0]["reasons"]

    def test_the_effective_configuration_is_reported(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        body = decisions(client, pid, {"candidates": [candidate(stocks["tech_a"], "100")]})
        assert body["configuration"]["max_position_weight_pct"] == "40.0000"

    def test_a_per_request_override_does_not_change_the_stored_configuration(
        self, client, stocks
    ):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        body = decisions(
            client,
            pid,
            {
                "candidates": [candidate(stocks["tech_a"], "100")],
                "max_position_weight_pct": "10",
            },
        )
        assert body["decisions"][0]["target_weight_pct"] == "10.0000"
        assert portfolio(client, pid)["allocation_config"]["max_position_weight_pct"] == "40.0000"

    def test_decisions_are_ordered_by_score_then_id(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        body = decisions(
            client,
            pid,
            {
                "candidates": [
                    candidate(stocks["tech_a"], "100", composite_score=50),
                    candidate(stocks["energy"], "100", composite_score=50),
                    candidate(stocks["tech_b"], "100", composite_score=90),
                ]
            },
        )
        order = [item["stock_id"] for item in body["decisions"]]
        assert order == [stocks["tech_b"], stocks["tech_a"], stocks["energy"]]

    def test_the_same_request_produces_the_same_decisions(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        request = {
            "candidates": [candidate(stocks["tech_a"], "100")],
            "as_of": "2026-01-15T00:00:00",
        }
        first = decisions(client, pid, request)
        second = decisions(client, pid, request)
        assert first["decisions"][0] == second["decisions"][0]
        assert first["as_of"] == second["as_of"]

    def test_the_run_reports_the_risk_rules_already_breached(self, client, stocks):
        # The buy has to clear the limit to get in, so the breach is created by the price rising
        # afterwards rather than by setting an unreachable limit up front.
        pid = create(
            client,
            capital="100000",
            allocation_config=allocation(),
            risk_limits={"max_exposure_pct": "45"},
        )["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        # At 150 the position is 60000 against 120000 of equity: 50% invested, above the 45%
        # exposure limit, and 50% of equity in one name, above the 40% position cap. Both rules
        # are reported, because a report that named only one of them would hide the other.
        body = decisions(client, pid, {"prices": prices((stocks["tech_a"], "150"))})
        assert "max_exposure_pct" in body["risk_violations"]
        assert "max_position_weight_pct" in body["risk_violations"]

    def test_decisions_on_a_missing_portfolio_are_404(self, client):
        response = client.post(f"{BASE}/999999/decisions", json={"candidates": []})
        assert response.status_code == 404


# ----------------------------------------------------------------------
# Rebalancing
# ----------------------------------------------------------------------


class TestRebalance:
    def test_a_rebalance_is_a_proposal_and_writes_nothing(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        body = rebalance(
            client, pid, {"target_weights": {str(stocks["tech_a"]): "50"}}
        )
        assert body["applied"] is False
        assert portfolio(client, pid)["position_count"] == 1

    def test_an_overweight_position_is_trimmed_to_the_target(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        body = rebalance(
            client, pid, {"target_weights": {str(stocks["tech_a"]): "20"}}
        )
        # 20% of 100000 is 20000, at 100 a share: sell 200 of the 400 held.
        assert body["items"][0]["required_quantity"] == -200
        assert body["items"][0]["side"] == "SELL"
        assert body["items"][0]["target_quantity"] == 200
        assert body["trade_count"] == 1

    def test_an_underweight_position_is_bought_up_to_the_target(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        body = rebalance(
            client, pid, {"target_weights": {str(stocks["tech_a"]): "50"}}
        )
        # 50% of 100000 is 50000, at 100 a share: buy 100 of the 400 held.
        assert body["items"][0]["required_quantity"] == 100
        assert body["items"][0]["side"] == "BUY"
        assert body["items"][0]["target_quantity"] == 500
        assert body["trade_count"] == 1

    def test_drift_inside_the_threshold_proposes_no_trade(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        # 40% held against a 40.25% target is a 0.25% drift, inside the 0.5% threshold.
        body = rebalance(client, pid, {"target_weights": {str(stocks["tech_a"]): "40.25"}})
        item = body["items"][0]
        assert item["below_threshold"] is True
        assert item["required_quantity"] == 0
        assert item["side"] is None
        assert item["action"] == "HOLD"
        assert body["trade_count"] == 0

    def test_a_hold_is_still_reported(self, client, stocks):
        # "Checked and correct" is a result; an empty list would be indistinguishable from
        # "not looked at".
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        body = rebalance(client, pid, {"target_weights": {str(stocks["tech_a"]): "40"}})
        assert body["items"][0]["action"] == "HOLD"
        assert body["items"][0]["below_threshold"] is True

    def test_an_unlisted_holding_is_left_alone_by_default(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        body = rebalance(client, pid, {"target_weights": {str(stocks["energy"]): "20"}})
        assert [item["stock_id"] for item in body["items"]] == [stocks["energy"]]

    def test_an_unlisted_holding_can_be_closed(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        body = rebalance(
            client,
            pid,
            {"target_weights": {str(stocks["energy"]): "20"}, "drop_unlisted": True},
        )
        closing = [item for item in body["items"] if item["stock_id"] == stocks["tech_a"]][0]
        assert closing["required_quantity"] == -100
        assert closing["side"] == "SELL"
        assert "position_removal" in closing["reasons"]

    def test_targets_can_be_derived_from_candidates(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        body = rebalance(
            client,
            pid,
            {"candidates": [candidate(stocks["tech_a"], "100"), candidate(stocks["energy"], "100")]},
        )
        assert body["target_weights"] == {str(stocks["tech_a"]): "40.0000", str(stocks["energy"]): "40.0000"}

    def test_sells_are_funded_before_buys(self, client, stocks):
        # The plan's own sells have to pay for its buys, or every rebalance that moves weight
        # between names would report itself unfundable.
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        body = rebalance(
            client,
            pid,
            {
                "target_weights": {str(stocks["tech_a"]): "20", str(stocks["energy"]): "20"},
                "prices": prices((stocks["energy"], "100")),
            },
        )
        assert body["trade_count"] == 2
        assert all(item["blocked"] is False for item in body["items"])
        assert {item["side"] for item in body["items"]} == {"BUY", "SELL"}

    def test_applying_a_rebalance_moves_the_positions(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        body = rebalance(
            client, pid, {"target_weights": {str(stocks["tech_a"]): "50"}, "apply": True}
        )
        item = body["items"][0]
        assert item["applied"] is True
        assert item["order_id"] is not None
        summary = portfolio(client, pid)
        assert summary["position_count"] == 1

    def test_applying_reports_what_it_actually_achieved(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        body = rebalance(
            client,
            pid,
            {
                "target_weights": {str(stocks["tech_a"]): "20", str(stocks["energy"]): "20"},
                "drop_unlisted": False,
                "prices": prices((stocks["energy"], "100")),
                "apply": True,
            },
        )
        assert body["applied"] is True

    def test_targets_over_one_hundred_percent_are_refused(self, client, stocks):
        pid = create(client)["portfolio_id"]
        response = client.post(
            f"{BASE}/{pid}/rebalance",
            json={"target_weights": {str(stocks["tech_a"]): "70", str(stocks["energy"]): "70"}},
        )
        assert response.status_code == 422

    def test_a_rebalance_with_no_targets_is_refused(self, client):
        pid = create(client)["portfolio_id"]
        assert client.post(f"{BASE}/{pid}/rebalance", json={}).status_code == 422

    def test_a_negative_target_is_refused_with_a_pointer_to_the_alternative(self, client, stocks):
        pid = create(client)["portfolio_id"]
        response = client.post(
            f"{BASE}/{pid}/rebalance",
            json={"target_weights": {str(stocks["tech_a"]): "-10"}},
        )
        assert response.status_code == 422
        assert "drop_unlisted" in response.json()["error"]["message"]

    def test_items_are_ordered_by_stock_id(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        body = rebalance(
            client,
            pid,
            {
                "target_weights": {
                    str(stocks["tech_b"]): "20",
                    str(stocks["tech_a"]): "20",
                    str(stocks["energy"]): "20",
                }
            },
        )
        ids = [item["stock_id"] for item in body["items"]]
        assert ids == sorted(ids)

    def test_rebalance_on_a_missing_portfolio_is_404(self, client):
        response = client.post(
            f"{BASE}/999999/rebalance", json={"target_weights": {"1": "10"}}
        )
        assert response.status_code == 404


# ----------------------------------------------------------------------
# The portfolio risk report
# ----------------------------------------------------------------------


class TestRiskReport:
    def test_an_empty_portfolio_reports_zero_weights_not_nulls(self, client):
        pid = create(client)["portfolio_id"]
        body = risk_report(client, pid)
        assert body["equity"] == "100000.00"
        assert body["exposure_pct"] == "0.0000"
        assert body["position_count"] == 0
        assert body["sector_weights"] == {}

    def test_exposure_and_cash_split_add_up(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        body = risk_report(client, pid)
        assert body["exposure_pct"] == "40.0000"
        assert body["cash_pct"] == "60.0000"

    def test_each_position_reports_its_own_stop_and_target(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        risk = risk_report(client, pid)["position_risks"][0]
        assert risk["stop_price"] == "90.0000"
        assert risk["take_profit_price"] == "120.0000"
        assert risk["stop_loss_breached"] is False
        assert risk["take_profit_triggered"] is False

    def test_the_risk_of_a_position_is_its_loss_at_the_stop(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        risk = risk_report(client, pid)["position_risks"][0]
        assert risk["risk_amount"] == "1000.00"
        assert risk["risk_contribution_pct"] == "1.0000"

    def test_a_reached_stop_is_reported_and_recommended(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        body = risk_report(client, pid, prices="1=90")
        risk = body["position_risks"][0]
        assert risk["stop_loss_breached"] is True
        recommendation = [item for item in body["recommendations"] if item["stock_id"] == stocks["tech_a"]]
        assert recommendation[0]["action"] == "SELL"
        assert recommendation[0]["quantity"] == 100

    def test_a_recommendation_carries_its_numbers(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        body = risk_report(client, pid, prices="1=90")
        rule = body["recommendations"][0]["rule"]
        assert rule
        assert body["recommendations"][0]["reason"]

    def test_a_clean_portfolio_recommends_nothing(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        assert risk_report(client, pid)["recommendations"] == []

    def test_sector_concentration_is_reported(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 200, "100")
        buy(client, pid, stocks["tech_b"], 200, "100")
        buy(client, pid, stocks["energy"], 100, "100")
        weights = risk_report(client, pid)["sector_weights"]
        assert weights == {"Technology": "40.0000", "Energy": "10.0000"}

    def test_a_stock_with_no_profile_is_not_credited_to_a_sector(self, client, db_session):
        # An invented "unknown" bucket would report a weight belonging to no real sector.
        unknown = create_stock(db_session, "NOSEC")
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, unknown, 100, "100")
        assert risk_report(client, pid)["sector_weights"] == {}

    def test_concentration_is_reported_against_the_sector_cap(self, client, stocks):
        pid = create(
            client,
            capital="100000",
            allocation_config=allocation(max_sector_weight_pct="30"),
        )["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        body = risk_report(client, pid)
        assert "max_sector_weight_pct" in [item["rule"] for item in body["violations"]]

    def test_the_largest_position_is_reported(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 200, "100")
        buy(client, pid, stocks["energy"], 100, "100")
        body = risk_report(client, pid)
        assert body["top_position_weight_pct"] == "20.0000"
        assert body["top_positions_weight_pct"] == "30.0000"

    def test_positions_are_ordered_heaviest_first(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["energy"], 100, "100")
        buy(client, pid, stocks["tech_a"], 300, "100")
        weights = [item["weight_pct"] for item in risk_report(client, pid)["position_risks"]]
        assert weights == sorted(weights, reverse=True)

    def test_the_report_agrees_with_the_pretrade_risk_endpoint(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 400, "100")
        report = risk_report(client, pid)
        pretrade = client.get(f"{BASE}/{pid}/risk").json()
        assert report["equity"] == pretrade["equity"]
        assert report["cash"] == pretrade["cash"]
        assert report["exposure_pct"] == pretrade["exposure_pct"]
        assert [item["rule"] for item in report["violations"]] == [
            item["rule"] for item in pretrade["violations"]
        ]

    def test_supplied_prices_mark_the_portfolio(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        assert risk_report(client, pid)["market_value"] == "10000.00"
        assert risk_report(client, pid, prices="1=120")["market_value"] == "12000.00"

    def test_a_malformed_price_is_refused_rather_than_ignored(self, client):
        # Silently dropping a price would mark to the stored close while the caller believed
        # they had marked to theirs.
        pid = create(client)["portfolio_id"]
        assert client.get(f"{BASE}/{pid}/risk/report", params={"prices": "1"}).status_code == 422

    def test_the_total_risk_is_the_sum_of_the_positions(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        buy(client, pid, stocks["energy"], 200, "100")
        body = risk_report(client, pid)
        assert body["total_risk_amount"] == "3000.00"

    def test_the_report_changes_nothing(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        before = portfolio(client, pid)
        risk_report(client, pid, prices="1=1")
        assert portfolio(client, pid) == before

    def test_the_report_carries_the_policy_it_used(self, client):
        pid = create(client, allocation_config=allocation())["portfolio_id"]
        assert risk_report(client, pid)["allocation_config"]["max_position_weight_pct"] == "40.0000"

    def test_the_report_of_a_missing_portfolio_is_404(self, client):
        assert client.get(f"{BASE}/999999/risk/report").status_code == 404

    def test_every_response_is_disclaimed_as_a_simulation(self, client, stocks):
        pid = create(client, allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        for body in (
            risk_report(client, pid),
            decisions(client, pid, {"candidates": []}),
            rebalance(client, pid, {"target_weights": {str(stocks["tech_a"]): "50"}}),
            transactions(client, pid),
            client.post(f"{BASE}/{pid}/deposit", json={"amount": "1"}).json(),
        ):
            assert "no real money" in body["disclaimer"]


# ----------------------------------------------------------------------
# Properties that span the phase
# ----------------------------------------------------------------------


class TestPhaseProperties:
    def test_no_phase_eleven_endpoint_creates_a_pending_order(self, client, stocks):
        # Everything here either trades at once or not at all; a deferred order would leave the
        # ledger and the positions disagreeing with the response that reported it.
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        decisions(client, pid, {"candidates": [candidate(stocks["tech_a"], "100")], "apply": True})
        assert client.get(f"{BASE}/{pid}/orders").json()["data"][0]["status"] == "FILLED"

    def test_the_ledger_and_the_portfolio_never_disagree(self, client, stocks):
        pid = create(client, capital="100000", allocation_config=allocation())["portfolio_id"]
        buy(client, pid, stocks["tech_a"], 100, "100")
        client.post(f"{BASE}/{pid}/deposit", json={"amount": "1234.56"})
        decisions(client, pid, {"candidates": [candidate(stocks["energy"], "50")], "apply": True})
        assert transactions(client, pid)["balance"] == portfolio(client, pid)["current_cash"]

    def test_the_position_cap_is_never_exceeded_by_its_own_target(self, client, stocks):
        pid = create(
            client, capital="100000", allocation_config=allocation(max_position_weight_pct="25")
        )["portfolio_id"]
        body = decisions(
            client,
            pid,
            {"candidates": [candidate(stocks["tech_a"], "100")], "apply": True},
        )
        assert float(body["decisions"][0]["target_weight_pct"]) <= 25.0
        assert float(portfolio(client, pid)["exposure_pct"]) <= 25.0

    def test_a_decision_run_never_leaves_cash_negative(self, client, stocks):
        pid = create(
            client,
            capital="10000",
            allocation_config=allocation(max_position_weight_pct="90", cash_reserve_pct="10"),
        )["portfolio_id"]
        decisions(
            client,
            pid,
            {
                "candidates": [
                    candidate(stocks["tech_a"], "100"),
                    candidate(stocks["tech_b"], "100"),
                    candidate(stocks["energy"], "100"),
                ],
                "apply": True,
            },
        )
        assert float(portfolio(client, pid)["current_cash"]) >= 0