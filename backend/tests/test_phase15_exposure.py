"""Phase 15 — exposure and concentration analysis.

Covers asset exposure (including the aggregation of one symbol across strategies), strategy
exposure, sector exposure, asset-class exposure, the top-N ladder, and the validation that refuses
an input the exposure maths could not honestly describe.
"""

from decimal import Decimal

import pytest

from app.core.exceptions import InvalidPortfolioStateError
from app.schemas.portfolio_risk import RiskStatus
from app.services.portfolio_risk.exposure_analyzer import (
    ExposureAnalyzer,
    validate_equity,
    validate_positions,
)
from tests.phase15_helpers import ZERO, config, position, snapshot, wide_policy

HUNDRED = Decimal("100")


def _weights(exposure, symbol):
    return next(item.weight_pct for item in exposure.assets if item.symbol == symbol)


class TestAssetExposure:
    def test_weight_is_market_value_over_equity(self):
        book = snapshot(
            [
                position("AAA", 10, 100),
                position("BBB", 10, 300),
            ],
            cash="0",
        )
        exposure = ExposureAnalyzer().analyze(book)

        assert _weights(exposure, "AAA") == Decimal("25.0000")
        assert _weights(exposure, "BBB") == Decimal("75.0000")
        assert exposure.summary.invested_pct == HUNDRED
        assert exposure.summary.cash_pct == ZERO

    def test_cash_counts_towards_equity(self):
        book = snapshot([position("AAA", 10, 100)], cash="100")
        exposure = ExposureAnalyzer().analyze(book)

        # Equity is cash plus market value, so a 1000 position against 100 of cash is 90.9%.
        assert _weights(exposure, "AAA") == Decimal("90.9091")
        assert exposure.summary.cash_pct == Decimal("9.0909")

    def test_same_symbol_in_two_strategies_is_one_exposure(self):
        """Phase 14 hands one instrument to several strategies; the book must read it as one risk."""
        book = snapshot(
            [
                position("AAA", 10, 100, strategy="MOM"),
                position("AAA", 10, 100, strategy="VAL"),
                position("BBB", 20, 100, strategy="VAL"),
            ]
        )
        exposure = ExposureAnalyzer().analyze(book)

        assert exposure.summary.total_positions == 2
        assert exposure.summary.total_lots == 3
        asset = next(item for item in exposure.assets if item.symbol == "AAA")
        assert asset.quantity == 20
        assert asset.market_value == Decimal("2000.00")
        assert asset.weight_pct == Decimal("50.0000")
        assert asset.strategies == ("MOM", "VAL")

    def test_assets_are_sorted_by_symbol(self):
        book = snapshot(
            [position("ZZZ", 1, 100), position("AAA", 1, 100), position("MMM", 1, 100)]
        )
        exposure = ExposureAnalyzer().analyze(book)
        assert [item.symbol for item in exposure.assets] == ["AAA", "MMM", "ZZZ"]


class TestStrategyExposure:
    def test_exposure_aggregates_per_strategy(self):
        book = snapshot(
            [
                position("AAA", 10, 100, strategy="MOM"),
                position("BBB", 10, 100, strategy="VAL"),
                position("CCC", 10, 100, strategy="VAL"),
            ]
        )
        exposure = ExposureAnalyzer().analyze(book)
        weights = {group.name: group.weight_pct for group in exposure.strategies}

        assert weights == {"MOM": Decimal("33.3333"), "VAL": Decimal("66.6667")}

    def test_breach_reports_utilization_against_the_limit(self):
        book = snapshot(
            [
                position("AAA", 10, 100, strategy="MOM"),
                position("BBB", 10, 100, strategy="MOM"),
            ]
        )
        exposure = ExposureAnalyzer(config(max_strategy_weight=Decimal("40"))).analyze(book)
        group = exposure.strategies[0]

        assert group.status is RiskStatus.BREACH
        assert group.limit_pct == Decimal("40")
        assert group.utilization_pct == Decimal("250.0000")

    def test_missing_strategy_is_measured_not_exempt(self):
        book = snapshot([position("AAA", 10, 100, strategy=None)])
        exposure = ExposureAnalyzer(config(max_strategy_weight=Decimal("50"))).analyze(book)
        group = exposure.strategies[0]

        assert group.name == "UNSPECIFIED"
        assert group.status is RiskStatus.BREACH

    def test_warning_at_the_configured_utilization(self):
        book = snapshot(
            [
                position("AAA", 10, 100, strategy="MOM"),
                position("BBB", 10, 100, strategy="VAL"),
            ]
        )
        exposure = ExposureAnalyzer(
            config(max_strategy_weight=Decimal("55"), warning_utilization_pct=Decimal("90"))
        ).analyze(book)
        statuses = {group.name: group.status for group in exposure.strategies}

        # 50% of a 55% limit is 90.9% utilization, which is at the warning threshold.
        assert statuses["MOM"] is RiskStatus.WARNING
        assert statuses["VAL"] is RiskStatus.WARNING


class TestSectorExposure:
    def test_positions_in_one_sector_aggregate(self):
        book = snapshot(
            [
                position("AAA", 10, 100, sector="Technology"),
                position("BBB", 10, 100, sector="Technology"),
                position("CCC", 10, 100, sector="Healthcare"),
            ]
        )
        exposure = ExposureAnalyzer().analyze(book)
        weights = {group.name: group.weight_pct for group in exposure.sectors}

        assert weights["Technology"] == Decimal("66.6667")
        assert weights["Healthcare"] == Decimal("33.3333")

    def test_sector_over_limit_is_flagged(self):
        book = snapshot(
            [
                position("AAA", 10, 100, sector="Technology"),
                position("BBB", 10, 100, sector="Technology"),
                position("CCC", 10, 100, sector="Healthcare"),
            ]
        )
        exposure = ExposureAnalyzer(config(max_sector_weight=Decimal("35"))).analyze(book)
        technology = next(g for g in exposure.sectors if g.name == "Technology")

        assert technology.status is RiskStatus.BREACH
        assert technology.limit_pct == Decimal("35")

    def test_unclassified_sector_is_reported_but_not_policed(self):
        """The engine cannot know the sector of an unclassified lot, so it must not cap it."""
        book = snapshot([position("AAA", 10, 100, sector=None)])
        exposure = ExposureAnalyzer(config(max_sector_weight=Decimal("1"))).analyze(book)
        group = exposure.sectors[0]

        assert group.name == "UNCLASSIFIED"
        assert group.limit_applied is False
        assert group.limit_pct is None
        assert group.status is RiskStatus.NOT_APPLICABLE
        assert exposure.summary.unclassified_sector_pct == HUNDRED

    def test_same_symbol_across_strategies_counts_once_in_its_sector(self):
        book = snapshot(
            [
                position("AAA", 10, 100, strategy="MOM", sector="Technology"),
                position("AAA", 10, 100, strategy="VAL", sector="Technology"),
            ]
        )
        exposure = ExposureAnalyzer().analyze(book)
        technology = next(g for g in exposure.sectors if g.name == "Technology")

        assert technology.weight_pct == HUNDRED
        assert technology.symbols == ("AAA",)


class TestAssetClassExposure:
    def test_asset_classes_are_aggregated(self):
        book = snapshot(
            [
                position("AAA", 10, 100, asset_class="EQUITY"),
                position("BBB", 10, 100, asset_class="BOND"),
            ]
        )
        exposure = ExposureAnalyzer(
            config(max_asset_class_weight=Decimal("60"), warning_utilization_pct=Decimal("100"))
        ).analyze(book)
        weights = {group.name: group.weight_pct for group in exposure.asset_classes}

        assert weights == {"BOND": Decimal("50.0000"), "EQUITY": Decimal("50.0000")}
        assert all(group.status is RiskStatus.PASS for group in exposure.asset_classes)

    def test_asset_class_limit_breach(self):
        book = snapshot(
            [
                position("AAA", 10, 100, asset_class="EQUITY"),
                position("BBB", 10, 100, asset_class="EQUITY"),
            ]
        )
        exposure = ExposureAnalyzer(config(max_asset_class_weight=Decimal("40"))).analyze(book)
        equity = next(g for g in exposure.asset_classes if g.name == "EQUITY")
        assert equity.status is RiskStatus.BREACH


class TestTopNConcentration:
    def test_top_n_is_the_sum_of_the_n_largest_weights(self):
        book = snapshot(
            [
                position("A", 10, 400),
                position("B", 10, 300),
                position("C", 10, 200),
                position("D", 10, 100),
            ]
        )
        exposure = ExposureAnalyzer().analyze(book)
        summary = exposure.summary

        assert summary.total_positions == 4
        assert summary.largest_position_symbol == "A"
        assert summary.largest_position_pct == Decimal("40.0000")
        assert summary.top_3_concentration_pct == Decimal("90.0000")
        assert summary.top_5_concentration_pct == HUNDRED
        assert summary.top_10_concentration_pct == HUNDRED

    def test_top_n_honours_the_configured_count(self):
        book = snapshot(
            [
                position("A", 10, 400),
                position("B", 10, 300),
                position("C", 10, 200),
                position("D", 10, 100),
            ]
        )
        exposure = ExposureAnalyzer(config(top_n_count=2)).analyze(book)
        assert exposure.summary.top_n_count == 2
        assert exposure.summary.top_n_concentration_pct == Decimal("70.0000")

    def test_top_n_fewer_positions_than_n_sums_to_invested(self):
        book = snapshot([position("A", 10, 100), position("B", 10, 100)], cash="300")
        exposure = ExposureAnalyzer().analyze(book)
        # Both positions: 2000 of 2300 equity.
        assert exposure.summary.top_n_concentration_pct == Decimal("86.9566")


class TestConcentrationMetrics:
    def test_metrics_cover_every_rule(self):
        book = snapshot(
            [
                position("AAA", 10, 100, sector="Technology", strategy="MOM", asset_class="EQUITY"),
                position("BBB", 10, 100, sector="Healthcare", strategy="VAL", asset_class="BOND"),
            ]
        )
        analyzer = ExposureAnalyzer()
        metrics = analyzer.concentration_metrics(analyzer.analyze(book))
        names = {metric.metric for metric in metrics}

        assert "single_asset_weight" in names
        assert "sector_weight" in names
        assert "strategy_weight" in names
        assert "asset_class_weight" in names
        assert any(name.startswith("top_") for name in names)

    def test_metrics_are_deterministically_ordered(self):
        book = snapshot(
            [
                position("ZZZ", 10, 100),
                position("AAA", 10, 300),
                position("MMM", 10, 200),
            ]
        )
        analyzer = ExposureAnalyzer()
        first = analyzer.concentration_metrics(analyzer.analyze(book))
        second = analyzer.concentration_metrics(analyzer.analyze(book))

        assert first == second
        assert [m.subject for m in first if m.metric == "single_asset_weight"] == [
            "AAA",
            "MMM",
            "ZZZ",
        ]

    def test_position_count_metric_absent_without_a_count_rule(self):
        book = snapshot([position("AAA", 10, 100)])
        analyzer = ExposureAnalyzer()
        assert analyzer.position_count_status(analyzer.analyze(book)) is None

    def test_position_count_metric_absent_when_compliant(self):
        book = snapshot([position("AAA", 10, 100), position("BBB", 10, 100)])
        analyzer = ExposureAnalyzer(config(maximum_position_count=5))
        assert analyzer.position_count_status(analyzer.analyze(book)) is None

    def test_position_count_metric_present_when_configured(self):
        book = snapshot([position("AAA", 10, 100), position("BBB", 10, 100)])
        analyzer = ExposureAnalyzer(config(maximum_position_count=1))
        metric = analyzer.position_count_status(analyzer.analyze(book))
        assert metric.metric == "maximum_position_count"
        assert metric.status is RiskStatus.BREACH


class TestValidation:
    def test_negative_quantity_is_refused(self):
        with pytest.raises(InvalidPortfolioStateError, match="negative quantity"):
            validate_positions([position("AAA", -1, 100)], Decimal("100"), ZERO)

    def test_non_positive_price_is_refused(self):
        with pytest.raises(InvalidPortfolioStateError, match="priced at or below zero"):
            validate_positions([position("AAA", 1, 0)], Decimal("100"), ZERO)

    def test_negative_cash_is_refused(self):
        with pytest.raises(InvalidPortfolioStateError, match="negative"):
            validate_positions([position("AAA", 1, 100)], Decimal("100"), Decimal("-1"))

    def test_duplicate_symbol_for_one_strategy_is_refused(self):
        with pytest.raises(InvalidPortfolioStateError, match="Duplicate position"):
            validate_positions(
                [position("AAA", 1, 100, strategy="MOM"), position("AAA", 1, 100, strategy="MOM")],
                Decimal("200"),
                ZERO,
            )

    def test_same_symbol_in_two_strategies_is_allowed(self):
        validate_positions(
            [position("AAA", 1, 100, strategy="MOM"), position("AAA", 1, 100, strategy="VAL")],
            Decimal("200"),
            ZERO,
        )

    def test_conflicting_sectors_for_one_symbol_are_refused(self):
        with pytest.raises(InvalidPortfolioStateError, match="conflicting sectors"):
            validate_positions(
                [
                    position("AAA", 1, 100, strategy="MOM", sector="Technology"),
                    position("AAA", 1, 100, strategy="VAL", sector="Healthcare"),
                ],
                Decimal("200"),
                ZERO,
            )

    def test_unreconciled_equity_is_refused(self):
        with pytest.raises(InvalidPortfolioStateError, match="does not reconcile"):
            validate_equity([position("AAA", 10, 100)], ZERO, Decimal("5000"))

    def test_reconciliation_tolerates_quantisation(self):
        # Cash plus market value is 1000.01 against a stated 1000: within the relative tolerance.
        validate_equity([position("AAA", 10, 100)], Decimal("0.01"), Decimal("1000"))

    def test_reconciliation_tolerance_is_relative(self):
        # A 0.05 discrepancy is immaterial against 10000 and material against 100.
        with pytest.raises(InvalidPortfolioStateError, match="does not reconcile"):
            validate_equity([position("AAA", 1, 100)], Decimal("0.05"), Decimal("100"))

    def test_equity_must_be_positive(self):
        with pytest.raises(InvalidPortfolioStateError, match="must be positive"):
            validate_equity([position("AAA", 10, 100)], ZERO, ZERO)


class TestStatusBoundaries:
    def _equal_book(self, count: int = 4):
        """``count`` equal positions, each carrying exactly ``100/count`` percent."""
        return snapshot([position(f"S{index}", 1, 100) for index in range(count)])

    def test_exactly_at_the_limit_is_not_a_breach(self):
        """A value equal to its limit is compliant: at the limit is not over the limit."""
        exposure = ExposureAnalyzer(config(max_single_asset_weight=Decimal("25"))).analyze(
            self._equal_book()
        )
        assert all(asset.weight_pct == Decimal("25.0000") for asset in exposure.assets)
        assert all(asset.utilization_pct == HUNDRED for asset in exposure.assets)
        assert all(asset.status is not RiskStatus.BREACH for asset in exposure.assets)

    def test_at_full_utilization_the_metric_warns(self):
        exposure = ExposureAnalyzer(config(max_single_asset_weight=Decimal("25"))).analyze(
            self._equal_book()
        )
        assert exposure.assets[0].status is RiskStatus.WARNING

    def test_just_below_the_limit_still_warns_when_utilization_is_high(self):
        exposure = ExposureAnalyzer(
            config(max_single_asset_weight=Decimal("25.01"))
        ).analyze(self._equal_book())
        assert exposure.assets[0].status is RiskStatus.WARNING

    def test_well_below_the_limit_passes(self):
        exposure = ExposureAnalyzer(config(max_single_asset_weight=Decimal("50"))).analyze(
            self._equal_book()
        )
        assert all(asset.status is RiskStatus.PASS for asset in exposure.assets)

    def test_just_above_the_limit_breaches(self):
        exposure = ExposureAnalyzer(
            config(max_single_asset_weight=Decimal("24.99"), warning_utilization_pct=HUNDRED)
        ).analyze(self._equal_book())
        assert exposure.assets[0].status is RiskStatus.BREACH

    def test_no_limit_configured_is_not_a_pass(self):
        book = snapshot([position("AAA", 10, 100)])
        policy = wide_policy().replace(max_asset_class_weight=None)
        exposure = ExposureAnalyzer(policy).analyze(book)
        assert exposure.asset_classes[0].status is RiskStatus.NOT_APPLICABLE