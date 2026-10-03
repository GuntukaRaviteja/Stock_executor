"""Phase 14 pure-engine tests — allocation methods, constraints and eligibility.

Every expected value is derived from the formula under test: equal weight is ``100/N``,
risk-adjusted weight is ``sharpe_i / sum(sharpe) * 100``, and inverse-volatility weight is
``(1/vol_i) / sum(1/vol) * 100``. Weights are asserted to sum to exactly 100%.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.services.portfolio.optimization import (
    AllocationMethod,
    CorrelationMatrix,
    CorrelationStatus,
    OptimizationRequest,
    OptimizationResult,
    StrategyAssignment,
    StrategyEligibility,
    StrategyOptimizer,
    StrategyPerformance,
    compute_correlation_matrix,
    evaluate_eligibility,
    equal_weight_raw_weights,
    inverse_volatility_raw_weights,
    pearson_correlation,
    raw_weights_for,
    risk_adjusted_raw_weights,
    solve_constraints,
    ZERO,
)


def perf(
    strategy_id: str,
    *,
    total_return_pct="50",
    sharpe_ratio="1.5",
    max_drawdown_pct="10",
    annualized_volatility_pct="20",
    number_of_trades=10,
) -> StrategyPerformance:
    """Build a :class:`StrategyPerformance` with sensible defaults."""
    return StrategyPerformance(
        strategy_id=strategy_id,
        strategy_name=strategy_id,
        total_return_pct=Decimal(str(total_return_pct)),
        sharpe_ratio=Decimal(str(sharpe_ratio)) if sharpe_ratio else None,
        max_drawdown_pct=Decimal(str(max_drawdown_pct)),
        annualized_volatility_pct=Decimal(str(annualized_volatility_pct)) if annualized_volatility_pct else None,
        number_of_trades=number_of_trades,
    )


OPT = StrategyOptimizer()
CAPITAL = Decimal("1_000_000")


# ----------------------------------------------------------------------
# Equal-weight
# ----------------------------------------------------------------------


class TestEqualWeight:
    def test_n_strategies_get_equal_weight(self):
        strategies = [perf("a"), perf("b"), perf("c")]
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.EQUAL_WEIGHT,
                strategies=strategies,
            )
        )
        weights = [a.weight_pct for a in result.assignments if a.is_active]
        assert len(weights) == 3
        for w in weights:
            assert w == Decimal("33.3333")
        assert sum(weights) == pytest.approx(Decimal("100"))

    def test_two_strategies_get_50_50(self):
        strategies = [perf("a"), perf("b")]
        result = OPT.optimize(
            OptimizationRequest(total_capital=CAPITAL, method=AllocationMethod.EQUAL_WEIGHT, strategies=strategies)
        )
        weights = [a.weight_pct for a in result.assignments if a.is_active]
        assert all(w == Decimal("50.0000") for w in weights)

    def test_weights_sum_to_exactly_100(self):
        strategies = [perf("a"), perf("b"), perf("c"), perf("d"), perf("e")]
        result = OPT.optimize(
            OptimizationRequest(total_capital=CAPITAL, method=AllocationMethod.EQUAL_WEIGHT, strategies=strategies)
        )
        weights = [a.weight_pct for a in result.assignments if a.is_active]
        assert sum(weights) == Decimal("100.0000")

    def test_capital_is_weight_times_total(self):
        strategies = [perf("a"), perf("b")]
        result = OPT.optimize(
            OptimizationRequest(total_capital=CAPITAL, method=AllocationMethod.EQUAL_WEIGHT, strategies=strategies)
        )
        for assignment in result.assignments:
            if assignment.is_active:
                expected = assignment.weight_pct / Decimal("100") * CAPITAL
                assert assignment.capital == expected


# ----------------------------------------------------------------------
# Risk-adjusted (Sharpe-proportional)
# ----------------------------------------------------------------------


class TestRiskAdjusted:
    def test_sharpe_proportional(self):
        """Weight = sharpe_i / sum(sharpe) * 100."""
        s1 = perf("a", sharpe_ratio="1.5")
        s2 = perf("b", sharpe_ratio="0.5")
        result = OPT.optimize(
            OptimizationRequest(total_capital=CAPITAL, method=AllocationMethod.RISK_ADJUSTED, strategies=[s1, s2])
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        # sharpe ratio 1.5 / (1.5 + 0.5) = 75%
        assert by_id["a"].weight_pct == Decimal("75.0000")
        assert by_id["b"].weight_pct == Decimal("25.0000")

    def test_negative_sharpe_gets_zero(self):
        s1 = perf("a", sharpe_ratio="1.0")
        s2 = perf("b", sharpe_ratio=Decimal("-0.5"))
        result = OPT.optimize(
            OptimizationRequest(total_capital=CAPITAL, method=AllocationMethod.RISK_ADJUSTED, strategies=[s1, s2])
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        assert by_id["a"].weight_pct == Decimal("100.0000")
        assert by_id["b"].weight_pct == ZERO

    def test_all_zero_sharpe_degrades_to_equal(self):
        s1 = perf("a", sharpe_ratio="0")
        s2 = perf("b", sharpe_ratio=Decimal("0"))
        result = OPT.optimize(
            OptimizationRequest(total_capital=CAPITAL, method=AllocationMethod.RISK_ADJUSTED, strategies=[s1, s2])
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        assert by_id["a"].weight_pct == Decimal("50.0000")
        assert by_id["b"].weight_pct == Decimal("50.0000")


# ----------------------------------------------------------------------
# Inverse volatility
# ----------------------------------------------------------------------


class TestInverseVolatility:
    def test_lower_vol_gets_more_weight(self):
        """Weight = (1/vol_i) / sum(1/vol) * 100."""
        s1 = perf("a", annualized_volatility_pct="20")
        s2 = perf("b", annualized_volatility_pct="10")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL, method=AllocationMethod.INVERSE_VOLATILITY, strategies=[s1, s2]
            )
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        # inv_vol: a=1/20=0.05, b=1/10=0.1, total=0.15
        # weight_a = 0.05/0.15 * 100 = 33.3333
        # weight_b = 0.1/0.15 * 100 = 66.6667
        assert by_id["a"].weight_pct == Decimal("33.3333")
        assert by_id["b"].weight_pct == Decimal("66.6667")

    def test_weights_sum_to_100(self):
        s1 = perf("a", annualized_volatility_pct="15")
        s2 = perf("b", annualized_volatility_pct="25")
        s3 = perf("c", annualized_volatility_pct="30")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL, method=AllocationMethod.INVERSE_VOLATILITY, strategies=[s1, s2, s3]
            )
        )
        weights = [a.weight_pct for a in result.assignments if a.is_active]
        assert sum(weights) == Decimal("100.0000")


# ----------------------------------------------------------------------
# User-specified weights
# ----------------------------------------------------------------------


class TestUserWeights:
    def test_user_weights_are_applied_then_renormalised(self):
        s1 = perf("a")
        s2 = perf("b")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.USER_WEIGHTS,
                strategies=[s1, s2],
                user_weights={"a": Decimal("70"), "b": Decimal("30")},
            )
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        assert by_id["a"].weight_pct == Decimal("70.0000")
        assert by_id["b"].weight_pct == Decimal("30.0000")

    def test_user_weights_summing_to_120_get_clamped_to_100(self):
        s1 = perf("a")
        s2 = perf("b")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.USER_WEIGHTS,
                strategies=[s1, s2],
                user_weights={"a": Decimal("80"), "b": Decimal("40")},
            )
        )
        weights = [a.weight_pct for a in result.assignments if a.is_active]
        assert sum(weights) == Decimal("100.0000")

    def test_user_weights_not_listed_get_zero(self):
        s1 = perf("a")
        s2 = perf("b")
        s3 = perf("c")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.USER_WEIGHTS,
                strategies=[s1, s2, s3],
                user_weights={"a": Decimal("60"), "b": Decimal("40")},
            )
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        assert by_id["c"].weight_pct == ZERO
        assert by_id["c"].eligibility is StrategyEligibility.ELIGIBLE


# ----------------------------------------------------------------------
# Constraint solver
# ----------------------------------------------------------------------


class TestConstraintSolver:
    def test_raw_weights_pass_through_unchanged(self):
        raw = {"a": Decimal("50"), "b": Decimal("50")}
        final, clamped, redist = solve_constraints(raw, Decimal("0"), Decimal("100"))
        assert final == {"a": Decimal("50"), "b": Decimal("50")}
        assert clamped == []
        assert redist == []

    def test_high_weight_clamped_and_redistributed(self):
        """If one weight is 80% and max is 60%, it gets clamped and the surplus redistributed."""
        raw = {"a": Decimal("80"), "b": Decimal("20")}
        final, clamped, redist = solve_constraints(raw, Decimal("0"), Decimal("60"))
        # a clamped to 60, b absorbs the 20 -> b = 40
        assert final["a"] == Decimal("60")
        assert final["b"] == Decimal("40")
        assert len(clamped) == 1
        assert clamped[0][0] == "a"

    def test_low_weight_clamped_to_min(self):
        raw = {"a": Decimal("95"), "b": Decimal("5")}
        final, clamped, redist = solve_constraints(raw, Decimal("10"), Decimal("100"))
        # b clamped to 10, a absorbs the -5 -> a = 90
        assert final["b"] == Decimal("10")
        assert final["a"] == Decimal("90")

    def test_infeasible_max_raises(self):
        """3 strategies at max 30% → max total 90 < 100 → ValueError."""
        raw = {"a": Decimal("50"), "b": Decimal("25"), "c": Decimal("25")}
        with pytest.raises(ValueError, match="Cannot reach 100"):
            solve_constraints(raw, Decimal("0"), Decimal("30"))

    def test_infeasible_min_raises(self):
        """3 strategies at min 40% → min total 120 > 100 → ValueError."""
        raw = {"a": Decimal("33.33"), "b": Decimal("33.33"), "c": Decimal("33.34")}
        with pytest.raises(ValueError, match="Cannot drop to 100"):
            solve_constraints(raw, Decimal("40"), Decimal("100"))

    def test_weights_always_sum_to_100(self):
        """Regardless of raw weights, the output sums to exactly 100."""
        for raw in [
            {"a": Decimal("33.33"), "b": Decimal("33.33"), "c": Decimal("33.34")},
            {"a": Decimal("90"), "b": Decimal("5"), "c": Decimal("5")},
            {"a": Decimal("10"), "b": Decimal("80"), "c": Decimal("10")},
        ]:
            final, _, _ = solve_constraints(raw, Decimal("0"), Decimal("100"))
            assert sum(final.values()) == Decimal("100.0000"), raw

    def test_redistribution_is_deterministic(self):
        raw = {"a": Decimal("80"), "b": Decimal("20"), "c": Decimal("10")}
        final1, _, redist1 = solve_constraints(raw, Decimal("0"), Decimal("50"))
        final2, _, redist2 = solve_constraints(raw, Decimal("0"), Decimal("50"))
        assert final1 == final2
        assert redist1 == redist2


# ----------------------------------------------------------------------
# Eligibility
# ----------------------------------------------------------------------


class TestEligibility:
    def test_eligible_strategy(self):
        p = perf("a", number_of_trades=10)
        elig, reasons = evaluate_eligibility(p)
        assert elig is StrategyEligibility.ELIGIBLE
        assert reasons == []

    def test_insufficient_trades(self):
        p = perf("a", number_of_trades=1)
        elig, reasons = evaluate_eligibility(p, min_observations=2)
        assert elig is StrategyEligibility.INSUFFICIENT_DATA
        assert "1 trade" in reasons[0]

    def test_no_volatility_or_drawdown(self):
        p = StrategyPerformance(
            strategy_id="a",
            strategy_name="a",
            total_return_pct=Decimal("50"),
            sharpe_ratio=Decimal("1"),
            max_drawdown_pct=Decimal("0"),
            annualized_volatility_pct=None,
            number_of_trades=50,
        )
        elig, reasons = evaluate_eligibility(p)
        assert elig is StrategyEligibility.INSUFFICIENT_DATA

    def test_excluded_strategy(self):
        p = perf("a")
        elig, reasons = evaluate_eligibility(p, excluded=["a"])
        assert elig is StrategyEligibility.EXCLUDED


# ----------------------------------------------------------------------
# Correlation
# ----------------------------------------------------------------------


class TestPearsonCorrelation:
    def test_perfect_positive(self):
        x = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5")]
        y = [Decimal("2"), Decimal("4"), Decimal("6"), Decimal("8"), Decimal("10")]
        assert pearson_correlation(x, y) == Decimal("1.0000")

    def test_perfect_negative(self):
        x = [Decimal("1"), Decimal("2"), Decimal("3")]
        y = [Decimal("3"), Decimal("2"), Decimal("1")]
        corr = pearson_correlation(x, y)
        assert corr == Decimal("-1.0000")

    def test_flat_series_returns_none(self):
        x = [Decimal("1"), Decimal("1"), Decimal("1")]
        y = [Decimal("1"), Decimal("2"), Decimal("3")]
        assert pearson_correlation(x, y) is None

    def test_too_few_observations(self):
        assert pearson_correlation([Decimal("1")], [Decimal("1")]) is None


class TestCorrelationMatrix:
    def test_single_strategy(self):
        matrix = compute_correlation_matrix({"a": [Decimal("0.01"), Decimal("0.02")]})
        assert matrix.status is CorrelationStatus.SINGLE_STRATEGY
        assert matrix.get("a", "a") == Decimal("1")

    def test_sufficient_data(self):
        returns_a = [Decimal("0.01"), Decimal("-0.02"), Decimal("0.03"), Decimal("0.05")]
        returns_b = [Decimal("0.015"), Decimal("-0.01"), Decimal("0.025"), Decimal("0.04")]
        matrix = compute_correlation_matrix({"a": returns_a, "b": returns_b})
        assert matrix.status is CorrelationStatus.SUFFICIENT
        assert matrix.get("a", "a") == Decimal("1")
        assert matrix.get("b", "b") == Decimal("1")
        assert matrix.get("a", "b") is not None

    def test_insufficient_data(self):
        matrix = compute_correlation_matrix(
            {"a": [Decimal("0.01")], "b": [Decimal("0.01")]},
            min_observations=2,
        )
        assert matrix.status is CorrelationStatus.INSUFFICIENT_DATA
        assert ("a", "b") in matrix.insufficient_pairs

    def test_matrix_is_symmetric(self):
        returns_a = [Decimal("0.01"), Decimal("-0.02"), Decimal("0.03"), Decimal("0.05")]
        returns_b = [Decimal("0.015"), Decimal("-0.01"), Decimal("0.025"), Decimal("0.04")]
        matrix = compute_correlation_matrix({"a": returns_a, "b": returns_b})
        assert matrix.get("a", "b") == matrix.get("b", "a")

    def test_diagonal_is_one(self):
        returns_a = [Decimal("0.01"), Decimal("-0.02"), Decimal("0.03")]
        returns_b = [Decimal("0.02"), Decimal("0.01"), Decimal("0.03")]
        matrix = compute_correlation_matrix({"a": returns_a, "b": returns_b})
        assert matrix.get("a", "a") == Decimal("1")
        assert matrix.get("b", "b") == Decimal("1")


# ----------------------------------------------------------------------
# Result structure
# ----------------------------------------------------------------------


class TestResultStructure:
    def test_total_capital_reflects_allocation(self):
        s1 = perf("a")
        s2 = perf("b")
        result = OPT.optimize(
            OptimizationRequest(total_capital=Decimal("200000"), method=AllocationMethod.EQUAL_WEIGHT, strategies=[s1, s2])
        )
        assert result.total_capital == Decimal("200000")

    def test_ineligible_strategies_get_zero_capital(self):
        s1 = perf("a")
        s2 = perf("b", number_of_trades=0)
        result = OPT.optimize(
            OptimizationRequest(total_capital=CAPITAL, method=AllocationMethod.EQUAL_WEIGHT, strategies=[s1, s2])
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        assert by_id["b"].capital == ZERO
        assert by_id["b"].eligibility is StrategyEligibility.INSUFFICIENT_DATA

    def test_assignments_are_sorted_by_strategy_id(self):
        strategies = [perf("c"), perf("a"), perf("b")]
        result = OPT.optimize(
            OptimizationRequest(total_capital=CAPITAL, method=AllocationMethod.EQUAL_WEIGHT, strategies=strategies)
        )
        ids = [a.strategy_id for a in result.assignments]
        assert ids == sorted(ids)

    def test_method_is_recorded(self):
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL, method=AllocationMethod.RISK_ADJUSTED, strategies=[perf("a")]
            )
        )
        assert result.method is AllocationMethod.RISK_ADJUSTED


# ----------------------------------------------------------------------
# Correlation edge cases
# ----------------------------------------------------------------------


class TestCorrelationEdgeCases:
    def test_constant_series_returns_none(self):
        """A constant return series has zero variance → correlation is None."""
        a = [Decimal("0.01"), Decimal("0.01"), Decimal("0.01"), Decimal("0.01")]
        b = [Decimal("0.02"), Decimal("0.02"), Decimal("0.02"), Decimal("0.02")]
        corr = pearson_correlation(a, b)
        assert corr is None

    def test_perfect_positive_correlation(self):
        a = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("0.04")]
        b = [Decimal("0.02"), Decimal("0.04"), Decimal("0.06"), Decimal("0.08")]
        corr = pearson_correlation(a, b)
        assert corr is not None
        assert abs(corr - Decimal("1")) < Decimal("0.0001")

    def test_perfect_negative_correlation(self):
        a = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("0.04")]
        b = [Decimal("0.04"), Decimal("0.03"), Decimal("0.02"), Decimal("0.01")]
        corr = pearson_correlation(a, b)
        assert corr is not None
        assert abs(corr - Decimal("-1")) < Decimal("0.0001")

    def test_insufficient_overlap_in_matrix(self):
        """Pairs with < min_observations overlapping points are in insufficient_pairs."""
        strategy_returns = {
            "a": [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")],
            "b": [Decimal("0.01")],  # only 1 return
        }
        matrix = compute_correlation_matrix(strategy_returns, min_observations=2)
        assert matrix.status == CorrelationStatus.INSUFFICIENT_DATA
        assert ("a", "b") in matrix.insufficient_pairs

    def test_matrix_with_mixed_pairs(self):
        """Some pairs have enough overlap, some don't."""
        strategy_returns = {
            "a": [Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("0.04"), Decimal("0.05")],
            "b": [Decimal("0.02"), Decimal("0.03"), Decimal("0.04"), Decimal("0.05"), Decimal("0.06")],
            "c": [Decimal("0.03")],  # only 1 return
        }
        matrix = compute_correlation_matrix(strategy_returns, min_observations=2)
        assert ("a", "b") in matrix.matrix
        assert ("a", "c") in matrix.insufficient_pairs
        assert ("b", "c") in matrix.insufficient_pairs

    def test_single_strategy_status(self):
        matrix = compute_correlation_matrix({"only": [Decimal("0.01") * 2]})
        assert matrix.status == CorrelationStatus.SINGLE_STRATEGY
        assert matrix.matrix == {}

    def test_empty_returns_status(self):
        matrix = compute_correlation_matrix({"a": [], "b": []})
        # Both have 0 returns → overlap = 0 < 2 → insufficient
        assert matrix.status == CorrelationStatus.INSUFFICIENT_DATA


class TestConstraintSolverEdgeCases:
    def test_min_equals_max_forces_exact_split(self):
        """When min equals max, every strategy must receive exactly that weight."""
        s1 = perf("a")
        s2 = perf("b")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.EQUAL_WEIGHT,
                strategies=[s1, s2],
                min_weight_pct=Decimal("50"),
                max_weight_pct=Decimal("50"),
            )
        )
        for a in result.assignments:
            assert a.weight_pct == Decimal("50.0000")

    def test_infeasible_min_too_high(self):
        """2 strategies, min 60 — impossible (60*2=120 > 100)."""
        s1 = perf("a")
        s2 = perf("b")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.EQUAL_WEIGHT,
                strategies=[s1, s2],
                min_weight_pct=Decimal("60"),
                max_weight_pct=Decimal("100"),
            )
        )
        assert len(result.warnings) > 0
        assert result.assignments == ()

    def test_infeasible_max_too_low(self):
        """3 strategies, max 30 — impossible (30*3=90 < 100)."""
        strategies = [perf("a"), perf("b"), perf("c")]
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.EQUAL_WEIGHT,
                strategies=strategies,
                min_weight_pct=Decimal("0"),
                max_weight_pct=Decimal("30"),
            )
        )
        assert len(result.warnings) > 0

    def test_clamping_redistributes_residual(self):
        """When one strategy is clamped, residual weight is redistributed."""
        s1 = perf("a", sharpe_ratio="0.1")
        s2 = perf("b", sharpe_ratio="10")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.RISK_ADJUSTED,
                strategies=[s1, s2],
                min_weight_pct=Decimal("20"),
                max_weight_pct=Decimal("80"),
            )
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        assert by_id["a"].weight_pct >= Decimal("20")
        assert by_id["b"].weight_pct <= Decimal("80")
        total = by_id["a"].weight_pct + by_id["b"].weight_pct
        assert total == Decimal("100.0000")

    def test_clamped_weights_field_populated(self):
        s1 = perf("a", sharpe_ratio="0.1")
        s2 = perf("b", sharpe_ratio="10")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.RISK_ADJUSTED,
                strategies=[s1, s2],
                min_weight_pct=Decimal("20"),
                max_weight_pct=Decimal("80"),
            )
        )
        assert len(result.clamped_weights) == 2


class TestRiskAdjustedWeighting:
    def test_negative_and_positive_sharpe_mixed(self):
        s1 = perf("a", sharpe_ratio="-1")
        s2 = perf("b", sharpe_ratio="2")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.RISK_ADJUSTED,
                strategies=[s1, s2],
            )
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        # Negative Sharpe → 0 raw weight
        assert by_id["a"].weight_pct == ZERO
        assert by_id["b"].weight_pct == Decimal("100.0000")

    def test_moderate_sharpe_dominates(self):
        s1 = perf("a", sharpe_ratio="1")
        s2 = perf("b", sharpe_ratio="3")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.RISK_ADJUSTED,
                strategies=[s1, s2],
            )
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        assert Decimal(by_id["a"].weight_pct) < Decimal(by_id["b"].weight_pct)
        # 1/(1+3) = 25%, 3/(1+3) = 75%
        assert by_id["a"].weight_pct == Decimal("25.0000")
        assert by_id["b"].weight_pct == Decimal("75.0000")

    def test_risk_adjusted_sums_to_100(self):
        strategies = [perf(f"s{i}", sharpe_ratio=str(Decimal(i))) for i in range(1, 5)]
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.RISK_ADJUSTED,
                strategies=strategies,
            )
        )
        total = sum(a.weight_pct for a in result.assignments)
        assert total == Decimal("100.0000")


class TestUserWeightsRedistribution:
    def test_user_weights_renormalized_to_100(self):
        s1 = perf("a")
        s2 = perf("b")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.USER_WEIGHTS,
                strategies=[s1, s2],
                user_weights={"a": Decimal("60"), "b": Decimal("40")},
            )
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        assert by_id["a"].weight_pct == Decimal("60.0000")
        assert by_id["b"].weight_pct == Decimal("40.0000")

    def test_user_weights_unknown_strategy_ignored(self):
        s1 = perf("a")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.USER_WEIGHTS,
                strategies=[s1],
                user_weights={"a": Decimal("100"), "unknown": Decimal("0")},
            )
        )
        assert len(result.assignments) == 1
        assert result.assignments[0].weight_pct == Decimal("100.0000")

    def test_user_weights_partial_coverage_clamped(self):
        """user_weights covering only some strategies gets others at zero."""
        s1 = perf("a")
        s2 = perf("b")
        s3 = perf("c")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.USER_WEIGHTS,
                strategies=[s1, s2, s3],
                user_weights={"a": Decimal("70"), "b": Decimal("30")},
            )
        )
        by_id = {a.strategy_id: a for a in result.assignments}
        assert by_id["a"].weight_pct == Decimal("70.0000")
        assert by_id["b"].weight_pct == Decimal("30.0000")


class TestCapitalAssignment:
    def test_capital_proportional_to_weight(self):
        s1 = perf("a")
        s2 = perf("b")
        total = Decimal("500000")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=total,
                method=AllocationMethod.EQUAL_WEIGHT,
                strategies=[s1, s2],
            )
        )
        for a in result.assignments:
            expected = (a.weight_pct / Decimal("100") * total).quantize(Decimal("0.01"))
            assert a.capital == expected

    def test_risk_budget_equals_capital(self):
        s1 = perf("a")
        result = OPT.optimize(
            OptimizationRequest(
                total_capital=CAPITAL,
                method=AllocationMethod.EQUAL_WEIGHT,
                strategies=[s1],
            )
        )
        assert result.assignments[0].risk_budget == result.assignments[0].capital
        assert result.assignments[0].capital == CAPITAL


# Constants used in tests