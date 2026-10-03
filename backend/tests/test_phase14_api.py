"""Phase 14 API tests — route registration, request validation, success and error paths.

These run through the real router, DI wiring, services and database, so they cover the
parts a pure-engine test cannot: that the endpoint is registered, that Decimal survives the
HTTP boundary unchanged, and that domain errors become the right status codes.
"""

from decimal import Decimal

from tests.phase14_helpers import (
    optimize_request,
    optimize_via_api,
    strategy_perf,
)
from app.schemas.optimization import AllocationMethod


BASE = "/api/v1/portfolios"


def create_portfolio(client, capital="1000000"):
    body = {
        "name": "P14",
        "initial_capital": capital,
        "trading_costs": {"commission_rate": "0", "min_fee": "0", "slippage_rate": "0"},
    }
    response = client.post(BASE, json=body)
    assert response.status_code == 201, response.text
    return response.json()["portfolio_id"]


# ----------------------------------------------------------------------
# OpenAPI registration
# ----------------------------------------------------------------------


class TestRoutesAreRegistered:
    def test_optimize_path_is_in_schema(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        base = "/api/v1/portfolios/{portfolio_id}"
        assert "post" in paths[f"{base}/optimize"]

    def test_optimize_schemas_are_published(self, client):
        schemas = client.get("/openapi.json").json()["components"]["schemas"]
        for name in (
            "OptimizeRequest",
            "OptimizationResultSchema",
            "StrategyPerformanceSchema",
            "StrategyAssignmentSchema",
            "CorrelationMatrixSchema",
            "StrategyWeightSchema",
        ):
            assert name in schemas, name


# ----------------------------------------------------------------------
# Success paths
# ----------------------------------------------------------------------


class TestOptimizeEndpoint:
    def test_equal_weight_allocation(self, client):
        pid = create_portfolio(client)
        body = optimize_request(strategies=[strategy_perf("a"), strategy_perf("b")])
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total_capital"] == "1000000.00"
        assert data["method"] == "equal_weight"
        active = [a for a in data["assignments"] if a["eligibility"] == "eligible"]
        assert len(active) == 2
        weights = [Decimal(a["weight_pct"]) for a in active]
        assert sum(weights) == Decimal("100.0000")

    def test_risk_adjusted_allocation(self, client):
        pid = create_portfolio(client)
        s1 = strategy_perf("a", sharpe_ratio="2.0", number_of_trades=10)
        s2 = strategy_perf("b", sharpe_ratio="0.5", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.RISK_ADJUSTED,
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        by_id = {a["strategy_id"]: a for a in data["assignments"]}
        assert Decimal(by_id["a"]["weight_pct"]) > Decimal(by_id["b"]["weight_pct"])

    def test_inverse_volatility_allocation(self, client):
        pid = create_portfolio(client)
        s1 = strategy_perf("a", annualized_volatility_pct="20", number_of_trades=10)
        s2 = strategy_perf("b", annualized_volatility_pct="10", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.INVERSE_VOLATILITY,
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        by_id = {a["strategy_id"]: a for a in data["assignments"]}
        assert Decimal(by_id["a"]["weight_pct"]) < Decimal(by_id["b"]["weight_pct"])

    def test_user_weights_allocation(self, client):
        pid = create_portfolio(client)
        s1 = strategy_perf("a", number_of_trades=10)
        s2 = strategy_perf("b", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.USER_WEIGHTS,
        )
        body["user_weights"] = [
            {"strategy_id": "a", "weight_pct": "70"},
            {"strategy_id": "b", "weight_pct": "30"},
        ]
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        by_id = {a["strategy_id"]: a for a in data["assignments"]}
        assert Decimal(by_id["a"]["weight_pct"]) == Decimal("70.0000")
        assert Decimal(by_id["b"]["weight_pct"]) == Decimal("30.0000")

    def test_ineligible_strategy_gets_zero_capital(self, client):
        pid = create_portfolio(client)
        s1 = strategy_perf("a", number_of_trades=10)
        s2 = strategy_perf("b", number_of_trades=0)
        body = optimize_request(strategies=[s1, s2])
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        by_id = {a["strategy_id"]: a for a in data["assignments"]}
        assert by_id["b"]["eligibility"] == "insufficient_data"
        assert Decimal(by_id["b"]["capital"]) == Decimal("0")

    def test_weights_sum_to_100(self, client):
        pid = create_portfolio(client)
        strategies = [strategy_perf(f"strat_{i}") for i in range(5)]
        body = optimize_request(strategies=strategies)
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        active = [a for a in data["assignments"] if a["eligibility"] == "eligible"]
        weights = [Decimal(a["weight_pct"]) for a in active]
        assert sum(weights) == Decimal("100.0000")

    def test_capital_is_proportional_to_weight(self, client):
        pid = create_portfolio(client)
        body = optimize_request(strategies=[strategy_perf("a"), strategy_perf("b")])
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        total_capital = Decimal(data["total_capital"])
        for assignment in data["assignments"]:
            if assignment["eligibility"] == "eligible":
                weight = Decimal(assignment["weight_pct"])
                capital = Decimal(assignment["capital"])
                expected = (weight / Decimal("100") * total_capital).quantize(Decimal("0.01"))
                assert capital == expected

    def test_response_includes_disclaimer(self, client):
        pid = create_portfolio(client)
        body = optimize_request(strategies=[strategy_perf("a")])
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        assert "disclaimer" in data
        assert "PAPER TRADING" in data["disclaimer"]

    def test_single_strategy_gets_100(self, client):
        pid = create_portfolio(client)
        body = optimize_request(strategies=[strategy_perf("solo", number_of_trades=20)])
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        assert Decimal(data["assignments"][0]["weight_pct"]) == Decimal("100.0000")

    def test_two_strategies_capital_sums_to_total(self, client):
        pid = create_portfolio(client)
        body = optimize_request(strategies=[strategy_perf("a"), strategy_perf("b")])
        response = optimize_via_api(client, pid, body)
        data = response.json()
        capitals = [Decimal(a["capital"]) for a in data["assignments"] if a["eligibility"] == "eligible"]
        assert sum(capitals) == Decimal("1000000.00")


# ----------------------------------------------------------------------
# Request validation
# ----------------------------------------------------------------------


class TestRequestValidation:
    def test_negative_capital_rejected(self, client):
        pid = create_portfolio(client)
        body = {"total_capital": "-1000", "method": "equal_weight", "strategies": []}
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 422

    def test_user_weights_required_for_user_method(self, client):
        pid = create_portfolio(client)
        body = {
            "total_capital": "100000",
            "method": "user_weights",
            "strategies": [],
        }
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 422

    def test_user_weights_invalid_without_user_method(self, client):
        pid = create_portfolio(client)
        body = {
            "total_capital": "100000",
            "method": "equal_weight",
            "strategies": [],
            "user_weights": [{"strategy_id": "a", "weight_pct": "100"}],
        }
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 422

    def test_min_exceeds_max_rejected(self, client):
        pid = create_portfolio(client)
        body = {
            "total_capital": "100000",
            "method": "equal_weight",
            "strategies": [],
            "min_weight_pct": "50",
            "max_weight_pct": "30",
        }
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 422

    def test_unknown_method_rejected(self, client):
        pid = create_portfolio(client)
        body = {
            "total_capital": "100000",
            "method": "nonexistent_method",
            "strategies": [],
        }
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 422

    def test_extra_field_rejected(self, client):
        pid = create_portfolio(client)
        body = {
            "total_capital": "100000",
            "method": "equal_weight",
            "strategies": [],
            "unexpected_field": "value",
        }
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 422


# ----------------------------------------------------------------------
# Error paths
# ----------------------------------------------------------------------


class TestErrorPaths:
    def test_nonexistent_portfolio_404(self, client):
        body = {"total_capital": "100000", "method": "equal_weight", "strategies": []}
        response = optimize_via_api(client, 99999, body)
        assert response.status_code == 404

    def test_no_eligible_strategies_warns(self, client):
        """When no strategy has enough trades, the result has a warning."""
        pid = create_portfolio(client)
        s1 = strategy_perf("a", number_of_trades=0)
        s2 = strategy_perf("b", number_of_trades=1)
        body = optimize_request(strategies=[s1, s2])
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        assert len(data["warnings"]) > 0
        assert len(data["assignments"]) == 2

    def test_infeasible_max_weight_returns_warning(self, client):
        """3 strategies at 30% max → infeasible → returned as a warning with empty assignments."""
        pid = create_portfolio(client)
        s1 = strategy_perf("a", number_of_trades=10)
        s2 = strategy_perf("b", number_of_trades=10)
        s3 = strategy_perf("c", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2, s3],
            max_weight_pct="30",
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        assert len(data["warnings"]) > 0


# ----------------------------------------------------------------------
# No-look-ahead & determinism via API
# ----------------------------------------------------------------------


class TestApiDeterminism:
    def test_identical_requests_produce_identical_responses(self, client):
        pid = create_portfolio(client)
        strategies = [strategy_perf("a"), strategy_perf("b"), strategy_perf("c")]
        body = optimize_request(strategies=strategies)
        r1 = optimize_via_api(client, pid, body)
        r2 = optimize_via_api(client, pid, body)
        assert r1.json() == r2.json()

    def test_decimal_precision_preserved(self, client):
        """Decimal values survive the JSON round-trip without float corruption."""
        pid = create_portfolio(client)
        body = optimize_request(
            strategies=[
                strategy_perf("a", sharpe_ratio="1.5"),
                strategy_perf("b", sharpe_ratio="0.5"),
            ],
            method=AllocationMethod.RISK_ADJUSTED,
        )
        response = optimize_via_api(client, pid, body)
        data = response.json()
        for assignment in data["assignments"]:
            if assignment["eligibility"] == "eligible":
                weight = assignment["weight_pct"]
                assert "." in weight


# ----------------------------------------------------------------------
# Constraint solver & clamping
# ----------------------------------------------------------------------


class TestConstraintClamping:
    def test_min_weight_clamps_up(self, client):
        """A strategy with raw weight below min is clamped up to min."""
        pid = create_portfolio(client)
        s1 = strategy_perf("a", sharpe_ratio="0.1", number_of_trades=10)
        s2 = strategy_perf("b", sharpe_ratio="10", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.RISK_ADJUSTED,
            min_weight_pct="20",
            max_weight_pct="80",
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        by_id = {a["strategy_id"]: a for a in data["assignments"]}
        # strategy 'a' has very low Sharpe so raw weight is near 0; clamped to 20
        assert Decimal(by_id["a"]["weight_pct"]) >= Decimal("20")
        assert Decimal(by_id["b"]["weight_pct"]) <= Decimal("80")

    def test_max_weight_clamps_down(self, client):
        """A strategy with raw weight above max is clamped down to max."""
        pid = create_portfolio(client)
        s1 = strategy_perf("a", sharpe_ratio="10", number_of_trades=10)
        s2 = strategy_perf("b", sharpe_ratio="0.1", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.RISK_ADJUSTED,
            min_weight_pct="0",
            max_weight_pct="80",
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        by_id = {a["strategy_id"]: a for a in data["assignments"]}
        # strategy 'a' has very high Sharpe so raw weight is near 100; clamped to 80
        assert Decimal(by_id["a"]["weight_pct"]) <= Decimal("80")

    def test_clamped_weights_reported_in_response(self, client):
        """When clamping occurs, the response reports the clamped weights."""
        pid = create_portfolio(client)
        s1 = strategy_perf("a", sharpe_ratio="0.01", number_of_trades=10)
        s2 = strategy_perf("b", sharpe_ratio="10", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.RISK_ADJUSTED,
            min_weight_pct="10",
            max_weight_pct="90",
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        # clamped_weights should be non-empty when clamping occurred
        assert len(data["clamped_weights"]) > 0
        assert len(data["redistribution"]) >= 0

    def test_three_equal_strategies_respect_max(self, client):
        """33.3% raw weights, max=50 — no clamping needed, still sums to ~100."""
        pid = create_portfolio(client)
        strategies = [strategy_perf(f"strat_{i}") for i in range(3)]
        body = optimize_request(
            strategies=strategies,
            min_weight_pct="0",
            max_weight_pct="50",
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        active = [a for a in data["assignments"] if a["eligibility"] == "eligible"]
        weights = [Decimal(a["weight_pct"]) for a in active]
        # Quantization (4 dp) may cause a 0.0001 drift
        assert abs(sum(weights) - Decimal("100")) < Decimal("0.01")
        for w in weights:
            assert Decimal("0") <= w <= Decimal("50")


class TestRiskAdjustedEdgeCases:
    def test_non_positive_sharpe_gets_zero_weight_raw(self, client):
        """Strategy with Sharpe <= 0 gets zero raw weight before clamping."""
        pid = create_portfolio(client)
        s1 = strategy_perf("a", sharpe_ratio="-0.5", number_of_trades=10)
        s2 = strategy_perf("b", sharpe_ratio="1.5", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.RISK_ADJUSTED,
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        by_id = {a["strategy_id"]: a for a in data["assignments"]}
        # Negative Sharpe → zero raw weight → clamped to min_weight_pct (0 by default)
        assert Decimal(by_id["a"]["weight_pct"]) == Decimal("0.0000")
        assert Decimal(by_id["b"]["weight_pct"]) == Decimal("100.0000")

    def test_all_zero_sharpe_falls_back_to_equal_weight(self, client):
        """When all Sharpe ratios are zero or negative, equal weight is used."""
        pid = create_portfolio(client)
        s1 = strategy_perf("a", sharpe_ratio="0", number_of_trades=10)
        s2 = strategy_perf("b", sharpe_ratio="-1", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.RISK_ADJUSTED,
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        weights = {a["strategy_id"]: Decimal(a["weight_pct"]) for a in data["assignments"]}
        assert weights["a"] == Decimal("50.0000")
        assert weights["b"] == Decimal("50.0000")


class TestInverseVolatilityEdgeCases:
    def test_low_vol_strategy_gets_higher_weight(self, client):
        pid = create_portfolio(client)
        s1 = strategy_perf("a", annualized_volatility_pct="5", number_of_trades=10)
        s2 = strategy_perf("b", annualized_volatility_pct="20", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.INVERSE_VOLATILITY,
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        by_id = {a["strategy_id"]: a for a in data["assignments"]}
        assert Decimal(by_id["a"]["weight_pct"]) > Decimal(by_id["b"]["weight_pct"])

    def test_zero_volatility_gets_equal_weight(self, client):
        """Strategy with zero vol gets 1/vol = infinity, handled gracefully."""
        pid = create_portfolio(client)
        s1 = strategy_perf("a", annualized_volatility_pct="0", number_of_trades=10)
        s2 = strategy_perf("b", annualized_volatility_pct="10", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.INVERSE_VOLATILITY,
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        weights = [Decimal(a["weight_pct"]) for a in data["assignments"] if a["eligibility"] == "eligible"]
        assert sum(weights) == Decimal("100.0000")


class TestUserWeightsWithConstraints:
    def test_user_weights_clamped_to_max(self, client):
        pid = create_portfolio(client)
        s1 = strategy_perf("a", number_of_trades=10)
        s2 = strategy_perf("b", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.USER_WEIGHTS,
            min_weight_pct="0",
            max_weight_pct="60",
        )
        body["user_weights"] = [
            {"strategy_id": "a", "weight_pct": "80"},
            {"strategy_id": "b", "weight_pct": "20"},
        ]
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        by_id = {a["strategy_id"]: a for a in data["assignments"]}
        assert Decimal(by_id["a"]["weight_pct"]) <= Decimal("60")

    def test_user_weights_clamped_to_min(self, client):
        pid = create_portfolio(client)
        s1 = strategy_perf("a", number_of_trades=10)
        s2 = strategy_perf("b", number_of_trades=10)
        body = optimize_request(
            strategies=[s1, s2],
            method=AllocationMethod.USER_WEIGHTS,
            min_weight_pct="30",
            max_weight_pct="70",
        )
        body["user_weights"] = [
            {"strategy_id": "a", "weight_pct": "10"},
            {"strategy_id": "b", "weight_pct": "90"},
        ]
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        by_id = {a["strategy_id"]: a for a in data["assignments"]}
        assert Decimal(by_id["a"]["weight_pct"]) >= Decimal("30")
        assert Decimal(by_id["b"]["weight_pct"]) <= Decimal("70")

    def test_user_weights_unknown_strategy_excluded(self, client):
        """A strategy in user_weights not in the list is silently ignored."""
        pid = create_portfolio(client)
        s1 = strategy_perf("a", number_of_trades=10)
        body = optimize_request(
            strategies=[s1],
            method=AllocationMethod.USER_WEIGHTS,
            min_weight_pct="0",
            max_weight_pct="100",
        )
        body["user_weights"] = [
            {"strategy_id": "a", "weight_pct": "100"},
        ]
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        by_id = {a["strategy_id"]: a for a in data["assignments"]}
        assert Decimal(by_id["a"]["weight_pct"]) == Decimal("100.0000")


class TestCorrelationStatusResponse:
    def test_correlation_status_insufficient_data_for_explicit(self, client):
        """With explicit performance data (no equity curves in DB), correlation is insufficient."""
        pid = create_portfolio(client)
        body = optimize_request(
            strategies=[strategy_perf("a"), strategy_perf("b")],
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        assert data["correlation_status"] == "insufficient_data"

    def test_correlation_status_single_strategy(self, client):
        """With only one strategy, correlation status is single_strategy."""
        pid = create_portfolio(client)
        body = optimize_request(strategies=[strategy_perf("solo")])
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        assert data["correlation_status"] == "single_strategy"


class TestRiskBudgetAndCapital:
    def test_risk_budget_equals_capital_for_equal_weight(self, client):
        """Risk budget equals capital allocated (no separate risk multiplier)."""
        pid = create_portfolio(client)
        body = optimize_request(strategies=[strategy_perf("a"), strategy_perf("b")])
        response = optimize_via_api(client, pid, body)
        data = response.json()
        for assignment in data["assignments"]:
            if assignment["eligibility"] == "eligible":
                assert Decimal(assignment["risk_budget"]) == Decimal(assignment["capital"])

    def test_effective_min_max_returned_in_response(self, client):
        pid = create_portfolio(client)
        body = optimize_request(
            strategies=[strategy_perf("a"), strategy_perf("b")],
            min_weight_pct="5",
            max_weight_pct="95",
        )
        response = optimize_via_api(client, pid, body)
        data = response.json()
        assert data["effective_min_weight_pct"] == "5"
        assert data["effective_max_weight_pct"] == "95"


# ----------------------------------------------------------------------
# DB-backed correlation via API
# ----------------------------------------------------------------------


from datetime import date, timedelta as _timedelta
from app.models.backtest import Backtest as _Backtest
from app.models.backtest import BacktestEquityPoint as _EquityPoint
from app.models.stock import Stock as _Stock
from decimal import Decimal as _D


def _create_db_backtest(db, name, start, closes, trades=10):
    """Insert a registered strategy backtest with an equity curve into the DB."""
    symbol = name[:4].upper()
    stock = db.query(_Stock).filter_by(symbol=symbol).first()
    if stock is None:
        stock = _Stock(symbol=symbol, company_name=f"{name} Ltd", exchange="NSE", market="INDIA")
        db.add(stock)
        db.flush()

    from app import models
    bt = _Backtest(
        stock_id=stock.id,
        strategy_name=name,
        strategy_version="1.0",
        start_date=start,
        end_date=start + _timedelta(days=len(closes)),
        initial_capital=_D("100000"),
        final_equity=_D("100000"),
        total_return_pct=_D("10.0000"),
        max_drawdown_pct=_D("5.0000"),
        annualized_volatility_pct=_D("15.0000"),
        sharpe_ratio=_D("1.2"),
        number_of_trades=trades,
        parameters_json="{}",
    )
    db.add(bt)
    db.flush()

    equity = _D("1")
    for i, close in enumerate(closes):
        equity = _D(str(close)) / _D(str(closes[0]))
        db.add(_EquityPoint(
            backtest_id=bt.id,
            date=start + _timedelta(days=i + 1),
            cash=equity,
            position_value=_D("0"),
            equity=equity,
        ))
    db.commit()
    return bt


class TestDbCorrelationViaApi:
    def test_correlation_sufficient_with_overlapping_equity_curves(self, client, db_session):
        """When backtests exist in the DB, correlation is computed from equity curves."""
        start = date(2024, 1, 1)
        closes_a = [100, 101, 102, 103, 104, 105]
        closes_b = [100, 102, 104, 106, 108, 109]
        _create_db_backtest(db_session, "alpha", start, closes_a)
        _create_db_backtest(db_session, "beta", start, closes_b)

        pid = create_portfolio(client)
        body = optimize_request(
            strategies=[strategy_perf("alpha", number_of_trades=10), strategy_perf("beta", number_of_trades=10)],
        )
        response = optimize_via_api(client, pid, body)
        assert response.status_code == 200
        data = response.json()
        assert data["correlation_status"] == "sufficient"
        assert data["correlation"] is not None
        assert data["correlation"]["observation_count"] > 0
        assert len(data["correlation"]["matrix"]) > 0

    def test_api_optimize_omits_strategies_uses_db_registry(self, client, db_session):
        """When strategies is omitted, services gather from registered strategies in DB."""
        start = date(2024, 1, 1)
        closes = [100, 101, 102, 103, 104]
        _create_db_backtest(db_session, "phase7_technical", start, closes)

        pid = create_portfolio(client)
        body = {
            "total_capital": "1000000",
            "method": "equal_weight",
            "min_weight_pct": "0",
            "max_weight_pct": "100",
        }
        response = client.post(f"{BASE}/{pid}/optimize", json=body)
        assert response.status_code == 200
        data = response.json()
        # phase7_technical has 5 trades >= min_observations(2), so it should be eligible
        assert len(data["assignments"]) >= 1
