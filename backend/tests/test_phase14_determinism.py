"""Phase 14 determinism tests.

The optimizer must be deterministic: identical inputs always produce identical outputs,
with no dependence on insertion order, dict iteration order, or call sequence.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.services.portfolio.optimization import (
    AllocationMethod,
    OptimizationRequest,
    StrategyOptimizer,
    StrategyPerformance,
)
from tests.phase14_helpers import CAPITAL


def perf_full(strategy_id: str, sharpe="1.5", vol="20", **kw) -> StrategyPerformance:
    return StrategyPerformance(
        strategy_id=strategy_id,
        strategy_name=strategy_id,
        total_return_pct=Decimal(kw.get("total_return_pct", "50")),
        sharpe_ratio=Decimal(sharpe) if sharpe else None,
        max_drawdown_pct=Decimal(kw.get("max_drawdown_pct", "10")),
        annualized_volatility_pct=Decimal(vol) if vol else None,
        number_of_trades=kw.get("number_of_trades", 10),
    )


OPT = StrategyOptimizer()


class TestDeterminism:
    def test_same_input_same_output(self):
        strategies = [perf_full("a"), perf_full("b"), perf_full("c")]
        req = OptimizationRequest(
            total_capital=CAPITAL,
            method=AllocationMethod.RISK_ADJUSTED,
            strategies=strategies,
        )
        r1 = OPT.optimize(req)
        r2 = OPT.optimize(req)
        assert r1.assignments == r2.assignments

    def test_input_order_does_not_matter(self):
        """Strategies sent in different orders produce the same assignment set."""
        a = perf_full("a", sharpe="2")
        b = perf_full("b", sharpe="1")
        c = perf_full("c", sharpe="0.5")

        sorted_req = OptimizationRequest(
            total_capital=CAPITAL,
            method=AllocationMethod.RISK_ADJUSTED,
            strategies=[a, b, c],
        )
        reversed_req = OptimizationRequest(
            total_capital=CAPITAL,
            method=AllocationMethod.RISK_ADJUSTED,
            strategies=[c, b, a],
        )
        sorted_result = OPT.optimize(sorted_req)
        reversed_result = OPT.optimize(reversed_req)

        assert sorted_result.assignments == reversed_result.assignments

    def test_equal_weight_is_order_independent(self):
        strategies = [perf_full("a"), perf_full("b"), perf_full("c")]
        r1 = OPT.optimize(
            OptimizationRequest(total_capital=CAPITAL, method=AllocationMethod.EQUAL_WEIGHT, strategies=strategies)
        )
        r2 = OPT.optimize(
            OptimizationRequest(total_capital=CAPITAL, method=AllocationMethod.EQUAL_WEIGHT, strategies=list(reversed(strategies)))
        )
        assert r1.assignments == r2.assignments

    def test_user_weights_are_order_independent(self):
        a = perf_full("a")
        b = perf_full("b")
        r1 = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.USER_WEIGHTS,
                strategies=[a, b],
                user_weights={"a": Decimal("70"), "b": Decimal("30")},
            )
        )
        r2 = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.USER_WEIGHTS,
                strategies=[b, a],
                user_weights={"b": Decimal("30"), "a": Decimal("70")},
            )
        )
        assert r1.assignments == r2.assignments

    def test_constraint_solver_is_deterministic(self):
        from app.services.portfolio.optimization import solve_constraints

        raw = {"a": Decimal("80"), "b": Decimal("20"), "c": Decimal("10")}
        r1 = solve_constraints(raw, Decimal("0"), Decimal("40"))
        r2 = solve_constraints(raw, Decimal("0"), Decimal("40"))
        assert r1 == r2

    def test_correlation_matrix_is_deterministic(self):
        from app.services.portfolio.optimization import compute_correlation_matrix

        returns = {
            "a": [Decimal("0.01"), Decimal("-0.02"), Decimal("0.03"), Decimal("0.05")],
            "b": [Decimal("0.015"), Decimal("-0.01"), Decimal("0.025"), Decimal("0.04")],
        }
        m1 = compute_correlation_matrix(returns)
        m2 = compute_correlation_matrix(returns)
        assert m1 == m2

    def test_identical_output_on_repeated_runs(self):
        """Run the same optimization 10 times; every result must be identical."""
        strategies = [perf_full("a", sharpe="1.5"), perf_full("b", sharpe="2.0"), perf_full("c", sharpe="0.8")]
        req = OptimizationRequest(
            total_capital=CAPITAL,
            method=AllocationMethod.INVERSE_VOLATILITY,
            strategies=strategies,
        )
        first = OPT.optimize(req)
        for _ in range(9):
            assert OPT.optimize(req).assignments == first.assignments

    def test_no_random_module_used(self):
        """The optimization module must not import or use the random module."""
        import app.services.portfolio.optimization as opt_mod

        source = open(opt_mod.__file__).read()
        assert "import random" not in source
        assert "random." not in source
