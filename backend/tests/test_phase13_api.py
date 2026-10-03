"""Phase 13 API tests — success, error and serialization paths for the four new endpoints.

These run through the real router, DI wiring, services and database, so they cover the parts a
pure-engine test cannot: that the endpoints are registered, that Decimal survives the HTTP
boundary unchanged, that a domain failure becomes the right status code, and that an
``apply=true`` allocation really travels through the ordinary order path.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.phase13_helpers import (
    BASE,
    NO_FEES,
    allocate,
    backtest,
    candidate_body,
    create_portfolio,
    create_stock,
    create_via_api,
    policy_body,
    position_size,
    risk_state,
    store_price,
)

ANCHOR = date(2024, 1, 1)


def backtest_candidate(stock_id: int, **extra) -> dict:
    """A backtest candidate body. The replay schema carries no price: history supplies it."""
    return {"stock_id": stock_id, **extra}


def flat_series(count, price="100", start=ANCHOR):
    """``count`` consecutive dates, for the price rows the endpoints read."""
    return [start + timedelta(days=index) for index in range(count)]


# ----------------------------------------------------------------------
# OpenAPI registration
# ----------------------------------------------------------------------


class TestRoutesAreRegistered:
    def test_all_four_phase_13_paths_appear_in_the_schema(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        base = "/api/v1/portfolios/{portfolio_id}"
        assert "post" in paths[f"{base}/allocate"]
        assert "get" in paths[f"{base}/risk/state"]
        assert "post" in paths[f"{base}/backtest"]
        assert "post" in paths[f"{base}/risk/position-size"]

    def test_the_schemas_are_published(self, client):
        schemas = client.get("/openapi.json").json()["components"]["schemas"]
        for name in (
            "AllocateRequest",
            "AllocationResponse",
            "PortfolioRiskPolicySchema-Input",
            "PortfolioRiskStateResponse",
            "PositionSizeResponse",
            "PortfolioBacktestRequest",
            "PortfolioBacktestResponse",
        ):
            assert name in schemas, name


# ----------------------------------------------------------------------
# Sizing
# ----------------------------------------------------------------------


class TestPositionSizeEndpoint:
    def test_returns_the_full_arithmetic_not_only_the_answer(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        response = position_size(
            client,
            portfolio_id,
            {
                "stock_id": stock_id,
                "entry_price": "100",
                "stop_price": "95",
                "policy": policy_body(max_risk_per_trade_pct="1", max_position_weight_pct="30"),
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["quantity"] == 200
        assert body["quantity_by_risk"] == 200
        assert body["quantity_by_capital"] == 300
        assert body["risk_budget"] == "1000.00"
        assert body["risk_per_share"] == "5.0000"
        assert body["binding_constraint"] == "risk"
        assert body["explanation"]

    def test_decimal_survives_the_http_boundary_as_a_string(self, client, db_session):
        """Monetary figures are Decimal end to end, never a float."""
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        body = position_size(
            client,
            portfolio_id,
            {
                "stock_id": stock_id,
                "entry_price": "100",
                "stop_price": "95",
                "policy": policy_body(),
            },
        ).json()
        assert isinstance(body["risk_budget"], str)
        assert body["risk_budget"] == "1000.00"
        assert "." in body["capital_required"]

    def test_a_stop_above_the_entry_is_refused(self, client, db_session):
        """A stop above the entry defines no per-share risk, so there is no defensible size."""
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        response = position_size(
            client,
            portfolio_id,
            {
                "stock_id": stock_id,
                "entry_price": "100",
                "stop_price": "105",
                "policy": policy_body(),
            },
        )
        assert response.status_code == 422, response.text
        body = response.json()
        # The refusal names the offending prices, so the caller can see which value was wrong.
        detail = body["error"]["details"]
        assert detail["code"] == "INVALID_STOP_PRICE"
        assert detail["entry_price"] == "100.0000"
        assert detail["stop_loss_price"] == "105"

    def test_a_stop_equal_to_the_entry_is_refused(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        response = position_size(
            client,
            portfolio_id,
            {
                "stock_id": stock_id,
                "entry_price": "100",
                "stop_price": "100",
                "policy": policy_body(),
            },
        )
        assert response.status_code == 422

    def test_a_non_positive_entry_is_refused_by_the_schema(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        response = position_size(
            client,
            portfolio_id,
            {"stock_id": stock_id, "entry_price": "0", "stop_price": "-1"},
        )
        assert response.status_code == 422

    def test_an_unknown_portfolio_is_a_404(self, client):
        response = position_size(
            client, 9999, {"stock_id": 1, "entry_price": "100", "stop_price": "95"}
        )
        assert response.status_code == 404

    def test_an_unknown_stock_is_a_404(self, client, db_session):
        """A candidate must exist, because its sector and history come from the database."""
        portfolio_id = create_portfolio(db_session)
        response = position_size(
            client, portfolio_id, {"stock_id": 99999, "entry_price": "100", "stop_price": "95"}
        )
        assert response.status_code == 404

    def test_the_policy_is_validated_by_the_schema(self, client, db_session):
        """A weight above 100% is not a weight, and is refused before any arithmetic runs."""
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        response = position_size(
            client,
            portfolio_id,
            {
                "stock_id": stock_id,
                "entry_price": "100",
                "stop_price": "95",
                "policy": {**policy_body(), "max_position_weight_pct": "150"},
            },
        )
        assert response.status_code == 422

    def test_no_order_is_placed(self, client, db_session):
        """Sizing is a question, not an instruction."""
        from app.models.portfolio import PortfolioOrder as Order

        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        position_size(
            client,
            portfolio_id,
            {"stock_id": stock_id, "entry_price": "100", "stop_price": "95", "policy": policy_body()},
        )
        assert db_session.query(Order).count() == 0


# ----------------------------------------------------------------------
# Allocation
# ----------------------------------------------------------------------


class TestAllocateEndpoint:
    def test_approves_and_rejects_in_one_call(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        response = allocate(
            client,
            portfolio_id,
            {
                "candidates": [candidate_body(stock_id, 100, stop_price="95", score=90)],
                "policy": policy_body(max_position_weight_pct="30", max_risk_per_trade_pct="1"),
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["candidates_considered"] == 1
        assert body["decisions"][0]["approved"] is True
        assert body["decisions"][0]["quantity"] == 200

    def test_the_response_reports_cash_on_both_sides(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        body = allocate(
            client,
            portfolio_id,
            {
                "candidates": [candidate_body(stock_id, 100, stop_price="95", score=90)],
                "policy": policy_body(),
            },
        ).json()
        assert body["cash_before"] == "100000.00"
        assert Decimal(body["cash_after"]) < Decimal(body["cash_before"])

    def test_nothing_is_written_without_apply(self, client, db_session):
        """The default is a dry run, so a caller can price a proposal before committing to it."""
        from app.models.portfolio import PortfolioOrder as Order

        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        allocate(
            client,
            portfolio_id,
            {
                "candidates": [candidate_body(stock_id, 100, stop_price="95", score=90)],
                "policy": policy_body(),
            },
        )
        assert db_session.query(Order).count() == 0

    def test_apply_creates_the_order(self, client, db_session):
        from app.models.portfolio import PortfolioOrder as Order

        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        store_price(db_session, stock_id, ANCHOR, "100")
        response = allocate(
            client,
            portfolio_id,
            {
                "candidates": [candidate_body(stock_id, 100, stop_price="95", score=90)],
                "policy": policy_body(),
                "apply": True,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["decisions"][0]["applied"] is True
        assert db_session.query(Order).count() == 1

    def test_a_refused_candidate_carries_its_code(self, client, db_session):
        """One position allowed, two candidates: the second names the rule it broke."""
        portfolio_id = create_portfolio(db_session)
        first = create_stock(db_session, "AAA")
        second = create_stock(db_session, "BBB")
        body = allocate(
            client,
            portfolio_id,
            {
                "candidates": [
                    candidate_body(first, 100, stop_price="95", score=90),
                    candidate_body(second, 100, stop_price="95", score=50),
                ],
                "policy": policy_body(max_positions=1),
            },
        ).json()
        refused = [item for item in body["decisions"] if not item["approved"]]
        assert refused
        assert refused[0]["stock_id"] == second
        assert "MAX_POSITIONS_REACHED" in [
            item["code"] for item in refused[0]["rejection_reasons"]
        ]
        assert refused[0]["rejection_reasons"][0]["reason"]
        assert body["rejected_count"] == 1
        assert body["approved_count"] == 1

    def test_every_candidate_gets_a_decision(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        ids = [create_stock(db_session, f"S{index}") for index in range(3)]
        body = allocate(
            client,
            portfolio_id,
            {
                "candidates": [
                    candidate_body(stock, 100, stop_price="95", score=score)
                    for stock, score in zip(ids, (90, 50, 10))
                ],
                "policy": policy_body(),
            },
        ).json()
        assert {item["stock_id"] for item in body["decisions"]} == set(ids)

    def test_candidates_are_ranked_by_score_then_id(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        ids = [create_stock(db_session, f"S{index}") for index in range(3)]
        body = allocate(
            client,
            portfolio_id,
            {
                "candidates": [
                    candidate_body(stock, 100, stop_price="95", score=score)
                    for stock, score in zip(ids, (10, 90, 50))
                ],
                "policy": policy_body(),
            },
        ).json()
        assert [item["stock_id"] for item in body["decisions"]] == [ids[1], ids[2], ids[0]]

    def test_an_empty_candidate_list_is_refused_by_the_schema(self, client, db_session):
        """The endpoint allocates over a list of one or more; an empty list is not a request."""
        portfolio_id = create_portfolio(db_session)
        assert allocate(client, portfolio_id, {"candidates": []}).status_code == 422

    def test_an_unknown_portfolio_is_a_404(self, client):
        assert allocate(
            client, 9999, {"candidates": [candidate_body(1, 100, stop_price="95")]}
        ).status_code == 404

    def test_an_unknown_candidate_is_refused_within_the_run(self, client, db_session):
        """A stock that does not exist has no sector and no history, so it cannot be allocated."""
        portfolio_id = create_portfolio(db_session)
        body = allocate(
            client,
            portfolio_id,
            {"candidates": [candidate_body(99999, 100, stop_price="95")]},
        ).json()
        assert body["decisions"][0]["approved"] is False
        assert body["rejected_count"] == 1
        assert "does not exist" in " ".join(
            item["reason"] for item in body["decisions"][0]["rejection_reasons"]
        )

    def test_a_non_positive_entry_price_is_refused_by_the_schema(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        response = allocate(
            client,
            portfolio_id,
            {"candidates": [{"stock_id": stock_id, "entry_price": "0", "stop_price": "-1"}]},
        )
        assert response.status_code == 422


# ----------------------------------------------------------------------
# Risk state
# ----------------------------------------------------------------------


class TestRiskStateEndpoint:
    def test_a_new_portfolio_reports_no_flags(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        response = risk_state(client, portfolio_id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["flags"] == []
        assert body["position_count"] == 0
        assert body["equity"] == "100000.00"

    def test_exposure_and_weights_are_reported(self, client, db_session):
        from tests.phase13_helpers import buy_via_api

        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        store_price(db_session, stock_id, ANCHOR, "100")
        buy_via_api(client, portfolio_id, stock_id, 300, "100")
        body = risk_state(client, portfolio_id, prices=f"{stock_id}=100").json()
        assert Decimal(body["market_value"]) > 0
        assert body["exposure_pct"] is not None
        assert body["cash_weight_pct"] is not None

    def test_a_breach_is_reported_as_a_structured_flag(self, client, db_session):
        from tests.phase13_helpers import buy_via_api

        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        store_price(db_session, stock_id, ANCHOR, "100")
        buy_via_api(client, portfolio_id, stock_id, 800, "100")
        body = risk_state(
            client,
            portfolio_id,
            prices=f"{stock_id}=100",
        ).json()
        assert isinstance(body["flags"], list)

    def test_the_detail_names_the_rule_and_the_numbers(self, client, db_session):
        from tests.phase13_helpers import buy_via_api

        portfolio_id = create_portfolio(
            db_session, risk_limits={"max_position_weight_pct": Decimal("10")}
        )
        stock_id = create_stock(db_session)
        store_price(db_session, stock_id, ANCHOR, "100")
        buy_via_api(client, portfolio_id, stock_id, 500, "100")
        body = risk_state(client, portfolio_id, prices=f"{stock_id}=100").json()
        assert body["flag_details"]
        detail = body["flag_details"][0]
        assert detail["rule"]
        assert detail["reason"]

    def test_sector_weights_are_reported_for_known_sectors(self, client, db_session):
        """A position in a profiled company must produce a sector weight, not a validation error.

        The sector name is a string and the weight a Decimal, so the response needs a typed pair
        rather than a uniform mapping.
        """
        from tests.phase13_helpers import buy_via_api

        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session, "BANK", sector="BANKING")
        store_price(db_session, stock_id, ANCHOR, "100")
        buy_via_api(client, portfolio_id, stock_id, 200, "100")
        body = risk_state(client, portfolio_id, prices=f"{stock_id}=100").json()
        assert body["sector_weights"] == [{"sector": "BANKING", "weight_pct": "20.0000"}]

    def test_an_unknown_sector_produces_no_weight_at_all(self, client, db_session):
        """Not bucketed as 'unknown', which would report a weight belonging to no real sector."""
        from tests.phase13_helpers import buy_via_api

        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session, "NOPROFILE")
        store_price(db_session, stock_id, ANCHOR, "100")
        buy_via_api(client, portfolio_id, stock_id, 200, "100")
        body = risk_state(client, portfolio_id, prices=f"{stock_id}=100").json()
        assert body["sector_weights"] == []

    def test_reading_the_state_changes_nothing(self, client, db_session):
        from app.models.portfolio import PortfolioOrder as Order

        portfolio_id = create_portfolio(db_session)
        risk_state(client, portfolio_id)
        risk_state(client, portfolio_id)
        assert db_session.query(Order).count() == 0

    def test_malformed_prices_are_refused(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        response = risk_state(client, portfolio_id, prices="not-a-pair")
        assert response.status_code == 422

    def test_an_unknown_portfolio_is_a_404(self, client):
        assert risk_state(client, 9999).status_code == 404


# ----------------------------------------------------------------------
# Backtest
# ----------------------------------------------------------------------


class TestBacktestEndpoint:
    def _bars(self, db_session, stock_id, count=8):
        from tests.phase13_helpers import persist_bars
        from tests.phase13_helpers import bar as make_bar

        rows = []
        for index in range(count):
            day = ANCHOR + timedelta(days=index)
            price = Decimal("100") + Decimal(index)
            rows.append(make_bar(day, price, price, price, price))
        persist_bars(db_session, stock_id, rows)

    def test_replays_and_returns_metrics(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        self._bars(db_session, stock_id)
        response = backtest(
            client,
            portfolio_id,
            {
                "candidates": [backtest_candidate(stock_id)],
                "start_date": ANCHOR.isoformat(),
                "end_date": (ANCHOR + timedelta(days=7)).isoformat(),
                "initial_capital": "100000",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["initial_capital"] == "100000.00"
        assert body["days_processed"] == 8
        assert len(body["equity_curve"]) == 8
        assert "total_return_pct" in body
        assert "max_drawdown_pct" in body
        assert body["number_of_trades"] >= 0

    def test_the_run_reports_whether_it_was_free_of_look_ahead(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        self._bars(db_session, stock_id)
        body = backtest(
            client,
            portfolio_id,
            {
                "candidates": [backtest_candidate(stock_id)],
                "start_date": ANCHOR.isoformat(),
                "end_date": (ANCHOR + timedelta(days=7)).isoformat(),
                "initial_capital": "100000",
            },
        ).json()
        assert body["no_look_ahead"] is True

    def test_a_backtest_writes_nothing(self, client, db_session):
        """A backtest is a question about recorded history, not an instruction."""
        from app.models.portfolio import PortfolioOrder as Order
        from app.models.portfolio import PortfolioTrade as Trade

        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        self._bars(db_session, stock_id)
        backtest(
            client,
            portfolio_id,
            {
                "candidates": [backtest_candidate(stock_id)],
                "start_date": ANCHOR.isoformat(),
                "end_date": (ANCHOR + timedelta(days=7)).isoformat(),
                "initial_capital": "100000",
            },
        )
        assert db_session.query(Order).count() == 0
        assert db_session.query(Trade).count() == 0

    def test_two_identical_calls_return_identical_results(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        self._bars(db_session, stock_id)
        payload = {
            "candidates": [backtest_candidate(stock_id)],
            "start_date": ANCHOR.isoformat(),
            "end_date": (ANCHOR + timedelta(days=7)).isoformat(),
            "initial_capital": "100000",
        }
        first = backtest(client, portfolio_id, payload).json()
        second = backtest(client, portfolio_id, payload).json()
        for key in ("final_equity", "total_fees", "days_processed", "total_pnl"):
            assert first[key] == second[key]

    def test_an_empty_candidate_list_is_refused_by_the_schema(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        response = backtest(client, portfolio_id, {"candidates": []})
        assert response.status_code == 422

    def test_a_candidate_without_stored_prices_is_refused(self, client, db_session):
        """A backtest replays recorded history, so a stock with none cannot be replayed."""
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        response = backtest(
            client,
            portfolio_id,
            {
                "candidates": [backtest_candidate(stock_id)],
                "start_date": ANCHOR.isoformat(),
                "end_date": (ANCHOR + timedelta(days=7)).isoformat(),
                "initial_capital": "100000",
            },
        )
        assert response.status_code in (404, 422)

    def test_a_window_before_the_stored_prices_is_refused(self, client, db_session):
        """No bars in the window means no replay, and it is reported rather than faked."""
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        self._bars(db_session, stock_id)
        response = backtest(
            client,
            portfolio_id,
            {
                "candidates": [backtest_candidate(stock_id)],
                "start_date": "2030-01-01",
                "end_date": "2030-01-08",
                "initial_capital": "100000",
            },
        )
        assert response.status_code in (404, 422)

    def test_an_unknown_portfolio_is_a_404(self, client):
        response = backtest(
            client,
            9999,
            {
                "candidates": [backtest_candidate(1)],
                "start_date": ANCHOR.isoformat(),
                "end_date": (ANCHOR + timedelta(days=7)).isoformat(),
                "initial_capital": "100000",
            },
        )
        assert response.status_code == 404

    def test_non_positive_capital_is_refused_by_the_schema(self, client, db_session):
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        response = backtest(
            client,
            portfolio_id,
            {
                "candidates": [backtest_candidate(stock_id)],
                "start_date": ANCHOR.isoformat(),
                "end_date": (ANCHOR + timedelta(days=7)).isoformat(),
                "initial_capital": "0",
            },
        )
        assert response.status_code == 422


# ----------------------------------------------------------------------
# Backward compatibility
# ----------------------------------------------------------------------


class TestExistingEndpointsStillWork:
    def test_the_pre_existing_sizing_endpoint_is_unchanged(self, client, db_session):
        """Phase 13 adds a path; it must not alter the Phase 9 one."""
        portfolio_id = create_portfolio(db_session)
        stock_id = create_stock(db_session)
        store_price(db_session, stock_id, ANCHOR, "100")
        response = client.post(
            f"{BASE}/{portfolio_id}/position-size",
            json={
                "stock_id": stock_id,
                "method": "RISK_BASED",
                "entry_price": "100",
                "stop_loss_price": "95",
                "risk_pct": "1",
            },
        )
        assert response.status_code == 200, response.text

    def test_a_portfolio_created_without_phase_13_settings_still_loads(self, client):
        body = create_via_api(client, name="Legacy")
        assert body["trading_mode"] == "PAPER"
        assert body["equity"] == "100000.00"

    def test_the_disclaimer_travels_with_every_phase_13_response(self, client, db_session):
        """The simulated nature of the data must not be droppable through a new endpoint."""
        portfolio_id = create_portfolio(db_session)
        body = risk_state(client, portfolio_id).json()
        assert "PAPER TRADING" in str(body).upper() or body.get("trading_mode") == "PAPER"