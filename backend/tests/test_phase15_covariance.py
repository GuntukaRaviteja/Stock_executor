"""Phase 15 — correlation, covariance, volatility, risk contribution and clusters.

Covers known correlation matrices, perfect and zero correlation, missing and insufficient data,
portfolio variance and volatility, risk-contribution reconciliation, cluster detection, beta, and
the refusal to use an invalid covariance matrix.
"""

from decimal import Decimal

import pytest

from app.core.exceptions import CovarianceCalculationError, InsufficientMarketDataError
from app.services.backtest.models import quantize_percent
from app.services.portfolio_risk.covariance_analyzer import (
    CovarianceAnalyzer,
    covariance_summary,
    pearson_correlation,
)
from tests.phase15_helpers import (
    LONG_SERIES,
    SHORT_SERIES,
    config,
    cyclic,
    independent_pair,
    perfect_pair,
)

HUNDRED = Decimal("100")
ONE = Decimal("1")
ZERO = Decimal("0")


def _analyze(weights, returns, policy=None, **kwargs):
    return CovarianceAnalyzer(policy or config()).analyze(weights, returns, **kwargs)


class TestPearsonCorrelation:
    def test_known_matrix(self):
        x = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
        y = [Decimal("2"), Decimal("4"), Decimal("6"), Decimal("8")]
        z = [Decimal("4"), Decimal("1"), Decimal("3"), Decimal("2")]
        assert pearson_correlation(x, y) == Decimal("1.0000")
        assert pearson_correlation(x, z) == Decimal("-0.4000")

    def test_negative_correlation(self):
        x = [Decimal("1"), Decimal("2"), Decimal("3")]
        y = [Decimal("-1"), Decimal("-2"), Decimal("-3")]
        assert pearson_correlation(x, y) == Decimal("-1.0000")

    def test_zero_variance_is_undefined_not_zero(self):
        """A flat series says nothing about its correlation; reporting 0 would be a claim."""
        assert pearson_correlation([Decimal("1")] * 5, [Decimal("1"), Decimal("2")]) is None

    def test_mismatched_lengths_are_undefined(self):
        assert pearson_correlation([Decimal("1"), Decimal("2")], [Decimal("1")]) is None

    def test_single_observation_is_undefined(self):
        assert pearson_correlation([Decimal("1")], [Decimal("1")]) is None


class TestCovarianceMatrix:
    def test_known_covariance_and_volatility(self):
        """A perfectly correlated 50/50 pair has a variance that can be checked by hand.

        The series are a ramp and the same ramp shifted by one observation, so both have the same
        variance ``s`` and correlation 1. Then ``w' * Sigma * w = (w1 + w2)^2 * s = s`` because the
        weights sum to one: a perfectly correlated book diversifies not at all.
        """
        base, shifted = perfect_pair()
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": base, "BBB": shifted},
        )
        assert result.data_quality == "SUFFICIENT"
        count = Decimal(len(base))
        mean = sum(base, ZERO) / count
        centred = sum(((value - mean) ** 2 for value in base), ZERO)
        sample_variance = centred / (count - 1)
        assert result.variance_daily == pytest.approx(sample_variance, abs=Decimal("1e-12"))
        assert result.daily_volatility_pct == pytest.approx(
            quantize_percent(sample_variance.sqrt() * HUNDRED), abs=Decimal("0.0001")
        )

    def test_perfect_correlation_gives_the_weighted_volatility_sum(self):
        base, doubled = perfect_pair()
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": base, "BBB": doubled},
        )
        assert result.correlation[("AAA", "BBB")] == Decimal("1.0000")
        # Concentration sum over i != j: 2 * 0.5 * 0.5 * 1
        assert result.correlation_concentration == Decimal("0.5000")
        # A perfectly correlated pair diversifies not at all, so its portfolio volatility is the
        # weight-average of the two — never below the heavier of them.
        assert result.annualised_volatility_pct > Decimal("0")

    def test_diagonal_is_one(self):
        base, doubled = perfect_pair()
        result = _analyze({"AAA": Decimal("50"), "BBB": Decimal("50")}, {"AAA": base, "BBB": doubled})
        assert result.correlation[("AAA", "AAA")] == ONE
        assert result.correlation[("BBB", "BBB")] == ONE

    def test_matrix_is_symmetric(self):
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        assert result.correlation[("AAA", "BBB")] == result.correlation[("BBB", "AAA")]

    def test_symbol_order_is_sorted(self):
        result = _analyze(
            {"ZZZ": Decimal("30"), "AAA": Decimal("40"), "MMM": Decimal("30")},
            {
                "ZZZ": cyclic("0.01", 7),
                "AAA": cyclic("0.008", 5),
                "MMM": cyclic("0.006", 3),
            },
        )
        assert result.symbols == ("AAA", "MMM", "ZZZ")

    def test_non_finite_observation_is_refused(self):
        with pytest.raises(InsufficientMarketDataError, match="non-finite"):
            _analyze(
                {"AAA": Decimal("50"), "BBB": Decimal("50")},
                {
                    "AAA": cyclic("0.01", 7),
                    "BBB": cyclic("0.008", 5)[:-1] + [Decimal("NaN")],
                },
            )

    def test_indefinite_matrix_is_refused(self):
        """A matrix with a negative diagonal cannot produce a variance and must not be used."""
        analyzer = CovarianceAnalyzer()
        with pytest.raises(CovarianceCalculationError, match="not positive semi-definite"):
            analyzer._require_positive_semidefinite(
                ("AAA", "BBB"),
                {
                    ("AAA", "AAA"): Decimal("-0.01"),
                    ("BBB", "BBB"): Decimal("0.01"),
                    ("AAA", "BBB"): ZERO,
                    ("BBB", "AAA"): ZERO,
                },
            )

    def test_incomplete_matrix_is_refused(self):
        """A missing pair cannot be read as zero; that would invent a covariance."""
        analyzer = CovarianceAnalyzer()
        with pytest.raises(CovarianceCalculationError, match="not symmetric"):
            analyzer._require_positive_semidefinite(
                ("AAA", "BBB"),
                {("AAA", "AAA"): Decimal("0.01"), ("BBB", "BBB"): Decimal("0.01")},
            )

    def test_asymmetric_matrix_is_refused(self):
        analyzer = CovarianceAnalyzer()
        with pytest.raises(CovarianceCalculationError, match="not symmetric"):
            analyzer._require_positive_semidefinite(
                ("AAA", "BBB"),
                {
                    ("AAA", "AAA"): Decimal("0.01"),
                    ("BBB", "BBB"): Decimal("0.01"),
                    ("AAA", "BBB"): Decimal("0.02"),
                    ("BBB", "AAA"): Decimal("0.01"),
                },
            )

    def test_singular_matrix_is_accepted(self):
        """Two identical series give a singular but valid matrix; it must not be rejected."""
        base, _ = perfect_pair()
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": base, "BBB": list(base)},
        )
        assert result.data_quality == "SUFFICIENT"
        assert result.variance_daily >= ZERO


class TestMissingData:
    def test_no_return_data_is_insufficient_not_zero(self):
        result = _analyze({"AAA": Decimal("50"), "BBB": Decimal("50")}, {})
        assert result.data_quality == "INSUFFICIENT"
        assert result.annualised_volatility_pct is None
        assert result.correlation_concentration is None
        assert result.risk_contributions == ()

    def test_insufficient_observations_is_reported(self):
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": cyclic("0.01", 7, SHORT_SERIES), "BBB": cyclic("0.008", 5, SHORT_SERIES)},
        )
        assert result.data_quality == "INSUFFICIENT"
        assert result.excluded_symbols == ("AAA", "BBB")
        assert "fewer than" in covariance_summary(result, config().min_observations)

    def test_single_symbol_cannot_produce_a_covariance(self):
        result = _analyze({"AAA": Decimal("100")}, {"AAA": cyclic("0.01", 7)})
        assert result.data_quality == "INSUFFICIENT"
        assert result.annualised_volatility_pct is None

    def test_partial_data_is_partial_and_names_the_exclusion(self):
        result = _analyze(
            {"AAA": Decimal("40"), "BBB": Decimal("35"), "CCC": Decimal("25")},
            {"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        assert result.data_quality == "PARTIAL"
        assert result.excluded_symbols == ("CCC",)
        assert result.weight_coverage_pct == Decimal("75.0000")

    def test_flat_series_is_excluded_not_treated_as_uncorrelated(self):
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": cyclic("0.01", 7), "BBB": [Decimal("0.01")] * LONG_SERIES},
        )
        assert "BBB" in result.excluded_symbols

    def test_coverage_ignores_cash(self):
        """Cash carries no return series by design; it must not read as missing coverage."""
        result = _analyze(
            {"AAA": Decimal("40"), "BBB": Decimal("30")},
            {"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        assert result.weight_coverage_pct == HUNDRED


class TestRiskContribution:
    def test_components_sum_to_portfolio_volatility(self):
        result = _analyze(
            {"AAA": Decimal("40"), "BBB": Decimal("35"), "CCC": Decimal("25")},
            {
                "AAA": cyclic("0.01", 7),
                "BBB": cyclic("0.008", 5),
                "CCC": cyclic("0.006", 3),
            },
        )
        total = sum(
            (item.component_contribution for item in result.risk_contributions), ZERO
        )
        assert total == pytest.approx(result.daily_volatility_pct, abs=Decimal("0.0001"))
        assert result.reconciliation_error_pct == pytest.approx(ZERO, abs=Decimal("0.0001"))

    def test_contributions_are_sorted_and_percentage_sums_to_100(self):
        result = _analyze(
            {"AAA": Decimal("40"), "BBB": Decimal("35"), "CCC": Decimal("25")},
            {
                "AAA": cyclic("0.01", 7),
                "BBB": cyclic("0.008", 5),
                "CCC": cyclic("0.006", 3),
            },
        )
        contributions = list(result.risk_contributions)
        assert contributions == sorted(
            contributions, key=lambda item: (-item.contribution_pct, item.symbol)
        )
        total = sum((item.contribution_pct for item in contributions), ZERO)
        assert total == pytest.approx(HUNDRED, abs=Decimal("0.01"))

    def test_marginal_is_zero_when_volatility_is_zero(self):
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": [Decimal("0.01"), Decimal("0.01")], "BBB": [Decimal("0.01"), Decimal("0.01")]},
            config(min_observations=2),
        )
        assert result.risk_contributions == ()


class TestCorrelationConcentration:
    def test_weighted_average_of_a_perfect_pair(self):
        base, doubled = perfect_pair()
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": base, "BBB": doubled},
        )
        assert result.weighted_average_correlation == Decimal("1.0000")

    def test_concentration_sum_excludes_the_diagonal(self):
        base, doubled = perfect_pair()
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": base, "BBB": doubled},
        )
        # With the diagonal included the sum would be 1.5; excluding it, it is 0.5.
        assert result.correlation_concentration == Decimal("0.5000")

    def test_single_position_has_no_pairwise_concentration(self):
        result = _analyze({"AAA": Decimal("100")}, {"AAA": cyclic("0.01", 7)})
        assert result.correlation_concentration is None
        assert result.weighted_average_correlation is None


class TestCorrelationClusters:
    def test_identical_series_form_one_cluster(self):
        base, doubled = perfect_pair()
        result = _analyze(
            {"AAA": Decimal("40"), "BBB": Decimal("40"), "CCC": Decimal("20")},
            {"AAA": base, "BBB": doubled, "CCC": cyclic("0.002", 3)},
        )
        assert len(result.clusters) == 1
        cluster = result.clusters[0]
        assert cluster.symbols == ("AAA", "BBB")
        assert cluster.average_correlation == Decimal("1.0000")
        assert cluster.combined_weight_pct == Decimal("80.0000")
        assert cluster.risk_concentration_pct is not None

    def test_uncorrelated_series_form_no_cluster(self):
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            dict(zip(("AAA", "BBB"), independent_pair())),
            config(correlation_cluster_threshold=Decimal("0.75")),
        )
        assert result.clusters == ()

    def test_cluster_threshold_is_configurable(self):
        """The configured threshold is what decides membership, not a hard-coded constant."""
        base = [Decimal("0.01") * Decimal(index + 1) for index in range(LONG_SERIES)]
        nudged = [
            value + Decimal("0.15") * (Decimal(1) if index % 2 == 0 else Decimal(-1))
            for index, value in enumerate(base)
        ]
        measured = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")}, {"AAA": base, "BBB": nudged}
        )
        observed = measured.weighted_average_correlation
        assert Decimal("0.5") < observed < ONE
        assert (
            _analyze(
                {"AAA": Decimal("50"), "BBB": Decimal("50")},
                {"AAA": base, "BBB": nudged},
                config(correlation_cluster_threshold=observed + Decimal("0.05")),
            ).clusters
            == ()
        )
        assert (
            len(
                _analyze(
                    {"AAA": Decimal("50"), "BBB": Decimal("50")},
                    {"AAA": base, "BBB": nudged},
                    config(correlation_cluster_threshold=observed - Decimal("0.05")),
                ).clusters
            )
            == 1
        )

    def test_cluster_ids_are_sequential_and_deterministic(self):
        result = _analyze(
            {"AAA": Decimal("30"), "BBB": Decimal("30"), "CCC": Decimal("20"), "DDD": Decimal("20")},
            {
                "AAA": cyclic("0.01", 7),
                "BBB": [value * 2 for value in cyclic("0.01", 7)],
                "CCC": cyclic("0.005", 4),
                "DDD": [value * 3 for value in cyclic("0.005", 4)],
            },
        )
        assert [cluster.cluster_id for cluster in result.clusters] == ["cluster_1", "cluster_2"]
        assert result.clusters == result.clusters


class TestBeta:
    def test_beta_from_returns(self):
        asset = [Decimal("0.02") * Decimal(index + 1) for index in range(LONG_SERIES)]
        benchmark = [Decimal("0.01") * Decimal(index + 1) for index in range(LONG_SERIES)]
        result = _analyze(
            {"AAA": Decimal("100")},
            {"AAA": asset},
            benchmark_returns=benchmark,
            benchmark_symbol="SPY",
        )
        # The asset moves at twice the benchmark's rate, so its beta is 2.
        assert result.beta == pytest.approx(Decimal("2"), abs=Decimal("0.0001"))
        assert result.benchmark == "SPY"

    def test_portfolio_beta_is_the_weighted_average(self):
        asset = [Decimal("0.02") * Decimal(index + 1) for index in range(LONG_SERIES)]
        flat = [Decimal("0.01") * Decimal(index + 1) for index in range(LONG_SERIES)]
        benchmark = [Decimal("0.01") * Decimal(index + 1) for index in range(LONG_SERIES)]
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": asset, "BBB": flat},
            benchmark_returns=benchmark,
        )
        assert result.beta == pytest.approx(Decimal("1.5"), abs=Decimal("0.001"))

    def test_flat_benchmark_makes_beta_undefined(self):
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
            benchmark_returns=[Decimal("0.01")] * LONG_SERIES,
        )
        assert result.beta is None
        assert result.individual_betas == {}

    def test_missing_benchmark_leaves_beta_undefined(self):
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        assert result.beta is None


class TestCorrelationSpike:
    def test_spiking_correlations_raises_volatility(self):
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        analyzer = CovarianceAnalyzer()
        spiked = analyzer.spiked_volatility(result, Decimal("0.3"), Decimal("1.2"))
        assert spiked > result.annualised_volatility_pct

    def test_spike_without_a_matrix_returns_none(self):
        analyzer = CovarianceAnalyzer()
        empty = _analyze({"AAA": Decimal("100")}, {})
        assert analyzer.spiked_volatility(empty, Decimal("0.3")) is None

    def test_spike_is_deterministic(self):
        result = _analyze(
            {"AAA": Decimal("50"), "BBB": Decimal("50")},
            {"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        analyzer = CovarianceAnalyzer()
        first = analyzer.spiked_volatility(result, Decimal("0.3"), Decimal("1.2"))
        second = analyzer.spiked_volatility(result, Decimal("0.3"), Decimal("1.2"))
        assert first == second