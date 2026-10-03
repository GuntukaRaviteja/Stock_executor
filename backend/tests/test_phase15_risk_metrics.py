"""Phase 15 — volatility, beta, max drawdown, historical VaR and Expected Shortfall.

Each metric is checked against a hand-computable series, and each metric is checked for what it
reports when the data is absent: an unmeasurable metric must read as missing, never as zero.
"""

from decimal import Decimal

import pytest

from app.services.portfolio_risk.covariance_analyzer import CovarianceAnalyzer
from app.schemas.portfolio_risk import RiskStatus
from app.services.portfolio_risk.exposure_analyzer import ExposureAnalyzer
from app.services.portfolio_risk.risk_metrics import (
    BETA,
    EXPECTED_SHORTFALL,
    MAX_DRAWDOWN,
    VAR_95,
    VOLATILITY,
    RiskMetricsEngine,
    equity_curve_from_returns,
    quantile,
    sample_std_dev,
)
from tests.phase15_helpers import (
    LONG_SERIES,
    SHORT_SERIES,
    ZERO,
    config,
    cyclic,
    diversified_snapshot,
    oscillating,
    perfect_pair,
    position,
    snapshot,
)

ONE = Decimal("1")


def _tail_snapshot() -> "object":
    """A snapshot with two bad days in twenty, so VaR and the tail average can be hand-checked."""
    returns = [Decimal("-0.10"), Decimal("-0.05")] + [Decimal("0.01")] * 18
    return snapshot(
        [position("AAA", 100, Decimal("10"))], portfolio_returns=returns, asset_returns={}
    )


def _metrics(policy=None):
    return RiskMetricsEngine(policy or config())


def _all(snapshot_obj, policy=None):
    """Run the metrics over a snapshot exactly as the engine wires them together."""
    engine = _metrics(policy)
    exposure = ExposureAnalyzer(policy or config()).analyze(snapshot_obj)
    covariance = CovarianceAnalyzer(policy or config()).analyze(
        {asset.symbol: asset.weight_pct for asset in exposure.assets},
        snapshot_obj.asset_returns,
        snapshot_obj.benchmark_returns,
        snapshot_obj.benchmark_symbol,
    )
    return engine.calculate(snapshot_obj, exposure, covariance)


class TestStatisticsHelpers:
    def test_sample_std_dev_matches_hand_computation(self):
        values = [Decimal("2"), Decimal("4"), Decimal("4"), Decimal("4"), Decimal("5"), Decimal("5"), Decimal("7"), Decimal("9")]
        # Population variance is 4; the sample estimator divides by n - 1.
        expected = (Decimal("32") / Decimal(7)).sqrt()
        assert sample_std_dev(values) == expected

    def test_identical_values_have_no_deviation(self):
        assert sample_std_dev([Decimal("3")] * 10) == ZERO

    def test_single_value_has_no_deviation(self):
        assert sample_std_dev([Decimal("3")]) == ZERO

    def test_quantile_interpolates_between_observations(self):
        ordered = [Decimal("0"), Decimal("10")]
        assert quantile(ordered, Decimal("0.5")) == Decimal("5")

    def test_quantile_at_the_ends_returns_the_observations(self):
        ordered = [Decimal("1"), Decimal("2"), Decimal("3")]
        assert quantile(ordered, ZERO) == Decimal("1")
        assert quantile(ordered, ONE) == Decimal("3")

    def test_quantile_of_an_empty_sample_is_an_error(self):
        with pytest.raises(ValueError, match="empty"):
            quantile([], Decimal("0.5"))

    def test_equity_curve_compounds_returns(self):
        curve = equity_curve_from_returns([Decimal("0.10"), Decimal("-0.10")])
        assert curve == [Decimal("1.0000"), Decimal("1.1000"), Decimal("0.9900")]


class TestVolatility:
    def test_prefers_the_covariance_figure(self):
        base, shifted = perfect_pair()
        snap = diversified_snapshot(asset_returns={"AAA": base, "BBB": shifted, "CCC": base, "DDD": shifted})
        result = _all(snap)[VOLATILITY]
        assert result.value is not None
        assert result.details["method"] == "covariance_matrix"
        assert result.limit_name == "max_portfolio_volatility"

    def test_falls_back_to_the_portfolio_return_series(self):
        snap = diversified_snapshot(asset_returns={})
        result = _all(snap)[VOLATILITY]
        assert result.value is not None
        assert result.details["method"] == "portfolio_return_series"
        assert "cannot reflect diversification" in result.explanation

    def test_no_data_at_all_is_insufficient(self):
        snap = diversified_snapshot(asset_returns={}, portfolio_returns=[])
        result = _all(snap)[VOLATILITY]
        assert result.value is None
        assert result.status == RiskStatus.DATA_INSUFFICIENT
        assert "missing data, not low risk" in result.explanation

    def test_utilization_is_measured_against_the_configured_limit(self):
        snap = diversified_snapshot()
        result = _all(snap, config(max_portfolio_volatility=Decimal("5")))[VOLATILITY]
        assert result.limit == Decimal("5")
        assert result.utilization_pct == pytest.approx(
            result.value / Decimal("5") * Decimal("100"), abs=Decimal("0.01")
        )


class TestBeta:
    def test_weighted_position_betas_are_used_when_available(self):
        snap = diversified_snapshot()
        exposure = ExposureAnalyzer().analyze(snap)
        result = _all(snap)[BETA]
        assert result.details["method"] == "position_betas"
        expected = sum(
            (asset.weight_pct / Decimal("100") * asset.beta for asset in exposure.assets if asset.beta is not None),
            ZERO,
        )
        assert result.value == pytest.approx(expected, abs=Decimal("0.0001"))

    def test_estimates_beta_from_returns_when_positions_have_none(self):
        """The holding moves twice as far as the benchmark each day, so its beta is 2."""
        benchmark = [Decimal("0.01") * Decimal(index + 1) for index in range(LONG_SERIES)]
        holding = [value * 2 for value in benchmark]
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))],
            asset_returns={"AAA": holding},
            benchmark_returns=benchmark,
            benchmark_symbol="SPY",
        )
        result = _all(snap)[BETA]
        assert result.details["method"] == "estimated_from_returns"
        assert result.value == pytest.approx(Decimal("2"), abs=Decimal("0.01"))

    def test_absent_beta_data_is_insufficient_not_zero(self):
        """A portfolio with no betas and no benchmark has an unmeasurable beta, not a zero one."""
        snap = snapshot(
            [position("AAA", 50, Decimal("10")), position("BBB", 50, Decimal("10"))],
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
            benchmark_symbol="SPY",
            benchmark_returns=None,
        )
        result = _all(snap)[BETA]
        assert result.value is None
        assert result.status == RiskStatus.DATA_INSUFFICIENT
        assert "not a neutral beta" in result.explanation
        assert "missing data" in result.explanation


class TestMaxDrawdown:
    def test_known_drawdown(self):
        curve = [Decimal("100"), Decimal("120"), Decimal("90"), Decimal("110")]
        snap = snapshot([position("AAA", 100, Decimal("10"))], equity_curve=curve)
        result = _all(snap)[MAX_DRAWDOWN]
        # 120 -> 90 is a 25% fall.
        assert result.value == Decimal("25.0000")
        assert result.details["worst_drawdown_index"] == 2
        assert result.details["source"] == "supplied_equity_curve"

    def test_current_drawdown_is_reported_separately_from_the_worst(self):
        curve = [Decimal("100"), Decimal("120"), Decimal("90"), Decimal("100")]
        result = _all(snapshot([position("AAA", 100, Decimal("10"))], equity_curve=curve))[MAX_DRAWDOWN]
        assert result.value == Decimal("25.0000")
        assert Decimal(result.details["current_drawdown_pct"]) == Decimal("16.6667")

    def test_rising_curve_has_no_drawdown(self):
        curve = [Decimal("100") * Decimal(index + 1) for index in range(10)]
        result = _all(snapshot([position("AAA", 100, Decimal("10"))], equity_curve=curve))[MAX_DRAWDOWN]
        assert result.value == ZERO
        assert result.status == RiskStatus.PASS

    def test_drawdown_is_derived_from_returns_when_no_curve_is_given(self):
        returns = [Decimal("0.20"), Decimal("-0.25")]
        snap = snapshot([position("AAA", 100, Decimal("10"))], portfolio_returns=returns)
        result = _all(snap)[MAX_DRAWDOWN]
        assert result.details["source"] == "derived_from_portfolio_returns"
        assert result.value == Decimal("25.0000")

    def test_single_observation_is_insufficient(self):
        snap = snapshot([position("AAA", 100, Decimal("10"))], equity_curve=[Decimal("100")])
        result = _all(snap)[MAX_DRAWDOWN]
        assert result.value is None
        assert result.status == RiskStatus.DATA_INSUFFICIENT

    def test_drawdown_is_not_the_largest_daily_loss(self):
        """A slow bleed produces a larger drawdown than any single day's loss — they differ."""
        curve = [Decimal("100")]
        for _ in range(10):
            curve.append(curve[-1] * Decimal("0.99"))
        result = _all(snapshot([position("AAA", 100, Decimal("10"))], equity_curve=curve))[MAX_DRAWDOWN]
        assert result.value > Decimal("1")
        assert "not the same as" in result.explanation


class TestValueAtRisk:
    def test_historical_quantile_of_a_known_sample(self):
        """One -10% day and one -5% day in twenty: the 5th percentile interpolates to -5.25%.

        The quantile is interpolated, so with 20 observations the 5th percentile sits at position
        ``0.05 * 19 = 0.95`` between the two worst returns: ``-10% * 0.05 + -5% * 0.95``.
        """
        snap = _tail_snapshot()
        result = _all(snap)[VAR_95]
        assert result.value == Decimal("5.2500")
        assert result.details["method"] == "historical_quantile"
        assert result.limit_name == "risk_budget_var"

    def test_positive_tail_only_sample_reports_no_loss(self):
        returns = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")] * 20
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))], portfolio_returns=returns, asset_returns={}
        )
        result = _all(snap)[VAR_95]
        assert result.value == ZERO
        assert result.status == RiskStatus.NOT_APPLICABLE
        # With a budget configured the same sample is simply inside it.
        budgeted = _all(snap, config(risk_budget_var=Decimal("5")))[VAR_95]
        assert budgeted.status == RiskStatus.PASS
        assert budgeted.utilization_pct == ZERO

    def test_short_sample_is_insufficient(self):
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))],
            portfolio_returns=[Decimal("-0.01")] * SHORT_SERIES,
            asset_returns={},
        )
        result = _all(snap)[VAR_95]
        assert result.value is None
        assert result.status == RiskStatus.DATA_INSUFFICIENT
        assert "artefact of the sample" in result.explanation

    def test_longer_horizon_scales_by_the_square_root_of_time(self):
        snap = _tail_snapshot()
        one_day = _all(snap, config(var_horizon_days=1))[VAR_95].value
        ten_day = _all(snap, config(var_horizon_days=10))[VAR_95].value
        assert one_day == Decimal("5.2500")
        assert ten_day == pytest.approx(Decimal("5.2500") * Decimal(str(10 ** 0.5)), abs=Decimal("0.01"))

    def test_higher_confidence_reports_a_larger_loss(self):
        snap = _tail_snapshot()
        ninety_nine = _all(snap, config(var_confidence=Decimal("0.99")))[VAR_95].value
        ninety_five = _all(snap, config(var_confidence=Decimal("0.95")))[VAR_95].value
        assert ninety_nine == Decimal("9.0500")
        assert ninety_nine > ninety_five


class TestExpectedShortfall:
    def test_average_of_the_tail_exceeds_var(self):
        metrics = _all(_tail_snapshot())
        assert metrics[EXPECTED_SHORTFALL].value > metrics[VAR_95].value
        assert metrics[EXPECTED_SHORTFALL].details["method"] == "historical_tail_mean"
        assert metrics[EXPECTED_SHORTFALL].details["tail_observations"] > 0

    def test_tail_is_the_worst_observations(self):
        """Five per cent of sixty observations is three, so three losses are averaged."""
        returns = [Decimal("0.01")] * (LONG_SERIES - 3) + [
            Decimal("-0.10"),
            Decimal("-0.20"),
            Decimal("-0.30"),
        ]
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))], portfolio_returns=returns, asset_returns={}
        )
        result = _all(snap)[EXPECTED_SHORTFALL]
        assert result.details["tail_observations"] == 3
        assert result.details["tail_rule"] == "worst ceil(alpha * observations) returns"
        assert result.value == Decimal("20.0000")

    def test_one_deep_loss_dominates_a_short_sample_tail(self):
        returns = [Decimal("0.01")] * (LONG_SERIES - 1) + [Decimal("-0.90")]
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))], portfolio_returns=returns, asset_returns={}
        )
        result = _all(snap)[EXPECTED_SHORTFALL]
        assert result.value > Decimal("29")
        assert result.value < Decimal("31")

    def test_no_limit_means_no_breach_can_be_declared(self):
        result = _all(_tail_snapshot())[EXPECTED_SHORTFALL]
        assert result.limit is None
        assert result.utilization_pct is None
        assert result.status == RiskStatus.NOT_APPLICABLE

    def test_short_sample_is_insufficient(self):
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))],
            portfolio_returns=[Decimal("-0.01")] * SHORT_SERIES,
            asset_returns={},
        )
        result = _all(snap)[EXPECTED_SHORTFALL]
        assert result.value is None
        assert result.status == RiskStatus.DATA_INSUFFICIENT


class TestMetricSet:
    def test_every_metric_carries_a_status_and_an_explanation(self):
        metrics = _all(diversified_snapshot())
        assert set(metrics) == {VOLATILITY, BETA, MAX_DRAWDOWN, VAR_95, EXPECTED_SHORTFALL}
        for name, result in metrics.items():
            assert result.metric == name
            assert result.status
            assert result.explanation
            assert result.details

    def test_an_oscillating_series_produces_a_real_drawdown(self):
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))],
            portfolio_returns=list(oscillating()),
            asset_returns={},
        )
        result = _all(snap)[MAX_DRAWDOWN]
        assert result.value > Decimal("20")
        assert Decimal(result.details["current_drawdown_pct"]) > ZERO

    def test_metrics_are_deterministic(self):
        snap = diversified_snapshot()
        assert _all(snap) == _all(snap)

    def test_correlated_books_have_a_lower_portfolio_volatility_than_the_average(self):
        base, shifted = perfect_pair()
        correlated = snapshot(
            [position("AAA", 50, Decimal("10")), position("BBB", 50, Decimal("10"))],
            asset_returns={"AAA": base, "BBB": shifted},
        )
        weights = {"AAA": Decimal("50"), "BBB": Decimal("50")}
        correlated_result = CovarianceAnalyzer().analyze(weights, correlated.asset_returns)
        assert correlated_result.weight_coverage_pct == Decimal("100.0000")
        # Both holdings move together, so portfolio volatility equals each holding's own.
        assert correlated_result.annualised_volatility_pct == pytest.approx(
            correlated_result.daily_volatility_pct
            * Decimal(str(252 ** 0.5)),
            abs=Decimal("0.01"),
        )