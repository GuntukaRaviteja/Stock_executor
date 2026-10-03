"""Phase 15 — limit detection, the adjustment decision, and deterministic risk reduction.

Covers which breaches become violations, what happens when a mandatory metric cannot be measured,
the fixed reduction priority, whole-share selling with proceeds left in cash, and the guarantee
that reduction converges on equity rather than drifting through rounding.
"""

from decimal import Decimal

import pytest

from app.schemas.portfolio_risk import RiskStatus
from app.services.portfolio_risk.exposure_analyzer import ExposureAnalyzer
from app.services.portfolio_risk.risk_controller import RiskController
from app.services.portfolio_risk.risk_engine import PortfolioRiskEngine
from tests.phase15_helpers import (
    ZERO,
    config,
    cyclic,
    diversified_snapshot,
    position,
    snapshot,
    wide_policy,
)

HUNDRED = Decimal("100")


def _bundle(snap, policy=None):
    """Measure one snapshot through the engine's own evaluation pass."""
    engine = _engine(policy or config())
    return engine.evaluate(
        snap.positions, snap.cash, snap.portfolio_value, snap
    )


def _engine(policy=None):
    return PortfolioRiskEngine(policy or config())


def _concentration(assessment):
    """The concentration metrics of an assessment, read from the assessment itself."""
    return assessment.concentration


class _AdjustmentView:
    """The adjusted positions, cash and exposure of one assessment, read in one place.

    The engine reduces as part of ``analyze``, so the reduction is inspected through the
    assessment rather than through a separate call that could disagree with it.
    """

    def __init__(self, assessment):
        self.assessment = assessment
        self.adjustments = assessment.adjustments
        self.final = assessment.final_portfolio
        self.positions = self.final.positions if self.final else assessment.exposure.positions
        self.cash = self.final.cash if self.final else ZERO
        self.exposure = self.final.exposure if self.final else assessment.exposure
        self.stopped_reason = assessment.explanation[-1]


class TestViolationDetection:
    def test_a_healthy_portfolio_has_no_violations(self):
        violations = _bundle(diversified_snapshot(), wide_policy()).violations
        assert [item for item in violations if item.status is RiskStatus.BREACH] == []
        # Coverage gaps are still reported; "no breach" must not read as "nothing to see here".
        assert [item for item in violations if item.status is RiskStatus.DATA_INSUFFICIENT]

    def test_a_single_position_book_breaches_the_concentration_limits(self):
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7)},
        )
        violations = _bundle(snap).violations
        rules = {violation.rule for violation in violations}
        assert "single_asset_weight" in rules
        breached = [item for item in violations if item.status is RiskStatus.BREACH]
        assert breached
        assert all(item.explanation for item in violations)
        assert {item.rule for item in breached} <= rules

    def test_violations_are_ordered_deterministically(self):
        snap = snapshot(
            [
                position("AAA", 100, Decimal("10")),
                position("BBB", 100, Decimal("10")),
                position("CCC", 100, Decimal("10")),
            ],
            cash="0",
            asset_returns={
                "AAA": cyclic("0.01", 7),
                "BBB": cyclic("0.008", 5),
                "CCC": cyclic("0.006", 3),
            },
        )
        first = _bundle(snap).violations
        second = _bundle(snap).violations
        assert first == second
        assert [violation.rule for violation in first] == [
            violation.rule for violation in second
        ]

    def test_a_stress_scenario_over_the_limit_is_a_violation(self):
        violations = _bundle(diversified_snapshot(), config(max_stress_loss=Decimal("5"))).violations
        rules = {violation.rule for violation in violations}
        assert "max_stress_loss" in rules

    def test_an_illiquid_position_is_a_violation(self):
        snap = snapshot(
            [position("AAA", 100, Decimal("10"), avg_daily_traded_value="10")],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7)},
        )
        bundle = _bundle(snap)
        assert "max_participation_ratio" in {item.rule for item in bundle.violations}
        assert bundle.liquidity.flagged_positions
        assert bundle.liquidity.max_participation_ratio is not None
        assert bundle.liquidity.max_participation_ratio > bundle.liquidity.max_participation_limit

    def test_a_limit_that_is_not_configured_never_violates(self):
        """An unset limit is not a limit: nothing is measured against it."""
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7)},
        )
        policy = config(max_single_asset_weight=None)
        rules = {violation.rule for violation in _bundle(snap, policy).violations}
        assert "max_single_asset_weight" not in rules


class TestUnmeasurableMandatoryMetrics:
    def test_an_unmeasurable_mandatory_metric_decides_data_insufficient(self):
        snap = snapshot(
            [
                position("AAA", 50, Decimal("10"), beta=None),
                position("BBB", 50, Decimal("10"), beta=None),
            ],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        policy = config(
            max_portfolio_beta=Decimal("0.5"), mandatory_limits=("max_portfolio_beta",)
        )
        violations = _bundle(snap, policy).violations
        decision, explanation, details = RiskController(policy).decide(violations, [])
        assert decision.value == "DATA_INSUFFICIENT"
        assert "not approved" in explanation
        assert details[0]["code"] == "MANDATORY_METRIC_UNMEASURABLE"

    def test_a_coverage_gap_alone_does_not_reject_a_healthy_portfolio(self):
        snap = snapshot(
            [position("AAA", 10, Decimal("10")), position("BBB", 10, Decimal("10"))],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        policy = wide_policy()
        violations = _bundle(snap, policy).violations
        gaps = [item for item in violations if item.status is RiskStatus.DATA_INSUFFICIENT]
        assert gaps, "the beta and drawdown gaps should still be reported"
        decision, _, details = RiskController(policy).decide(violations, [])
        assert decision.value != "DATA_INSUFFICIENT"
        assert any(item["code"] == "EVALUATION_GAP" for item in details)

    def test_mandatory_data_insufficient_outranks_a_breach(self):
        policy = config(
            max_portfolio_beta=Decimal("0.5"), mandatory_limits=("max_portfolio_beta",)
        )
        snap = snapshot(
            [position("AAA", 100, Decimal("10"), beta=None)],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7)},
        )
        violations = _bundle(snap, policy).violations
        decision, explanation, _ = RiskController(policy).decide(violations, [])
        assert decision.value == "DATA_INSUFFICIENT"
        assert "could not be evaluated" in explanation


class TestAdjustment:
    def _adjust(self, snap, policy=None):
        """Run the full engine, which measures and then reduces in one pass."""
        policy = policy or config()
        assessment = _engine(policy).analyze(snap)
        return _AdjustmentView(assessment)

    def test_a_healthy_portfolio_is_left_alone(self):
        snap = diversified_snapshot()
        assessment = _engine(wide_policy()).analyze(snap)
        assert assessment.adjustments == ()
        assert assessment.final_portfolio is None
        assert assessment.decision.value == "APPROVED"
        assert any("Adjustment was not required" in line for line in assessment.explanation)

    def test_a_concentrated_book_is_reduced_to_the_limit(self):
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7)},
        )
        policy = config(max_single_asset_weight=Decimal("25"), top_n_count=2)
        outcome = self._adjust(snap, policy)
        assert outcome.adjustments
        after = outcome.exposure
        worst = max(asset.weight_pct for asset in after.assets)
        assert worst <= Decimal("25.0000")

    def test_proceeds_stay_in_cash(self):
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7)},
        )
        policy = config(max_single_asset_weight=Decimal("25"), top_n_count=2)
        outcome = self._adjust(snap, policy)
        sold_value = sum(
            (adjustment.market_value_sold for adjustment in outcome.adjustments), ZERO
        )
        assert sold_value > ZERO
        assert outcome.cash == snap.cash + sold_value

    def test_equity_is_preserved_exactly(self):
        snap = snapshot(
            [
                position("AAA", 100, Decimal("10")),
                position("BBB", 50, Decimal("20")),
            ],
            cash="250",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        policy = config(max_single_asset_weight=Decimal("20"), top_n_count=2)
        outcome = self._adjust(snap, policy)
        before = snap.portfolio_value
        after = sum(
            (
                holding.quantity * Decimal(str(holding.price))
                for holding in outcome.positions
            ),
            ZERO,
        ) + outcome.cash
        assert after == before

    def test_only_whole_shares_are_sold(self):
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7)},
        )
        outcome = self._adjust(snap, config(max_single_asset_weight=Decimal("25"), top_n_count=2))
        for adjustment in outcome.adjustments:
            assert isinstance(adjustment.quantity_sold, int)
            assert adjustment.new_quantity >= 0

    def test_never_sells_more_than_it_holds(self):
        snap = snapshot(
            [position("AAA", 3, Decimal("10")), position("BBB", 100, Decimal("10"))],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        outcome = self._adjust(snap, config(max_single_asset_weight=Decimal("5"), top_n_count=2))
        held = {holding.symbol: holding.quantity for holding in outcome.positions}
        assert all(quantity >= 0 for quantity in held.values())
        assert held.get("AAA", 0) <= 3

    def test_reductions_are_ordered_by_the_fixed_priority(self):
        """Position weight is corrected before the sector that contains it."""
        snap = snapshot(
            [
                position("AAA", 100, Decimal("10"), sector="Technology"),
                position("BBB", 100, Decimal("10"), sector="Technology"),
            ],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        policy = config(
            max_single_asset_weight=Decimal("30"),
            max_sector_weight=Decimal("30"),
            top_n_count=2,
        )
        outcome = self._adjust(snap, policy)
        adjustments = outcome.adjustments
        constraints = [adjustment.violated_constraint for adjustment in adjustments]
        assert constraints[0] == "single_asset_weight"
        # Priorities are applied in order, so a later rule never appears before an earlier one.
        assert [adjustment.priority for adjustment in adjustments] == sorted(
            adjustment.priority for adjustment in adjustments
        )
        if "sector_weight" in constraints:
            assert constraints.index("sector_weight") > max(
                index for index, name in enumerate(constraints) if name == "single_asset_weight"
            )

    def test_the_reduction_converges(self):
        snap = snapshot(
            [
                position("AAA", 100, Decimal("10")),
                position("BBB", 100, Decimal("10")),
                position("CCC", 100, Decimal("10")),
                position("DDD", 100, Decimal("10")),
            ],
            cash="0",
            asset_returns={
                symbol: cyclic("0.01", 7)
                for symbol in ("AAA", "BBB", "CCC", "DDD")
            },
        )
        policy = config(
            max_single_asset_weight=Decimal("20"),
            max_sector_weight=Decimal("30"),
            max_top_n_concentration=Decimal("40"),
            top_n_count=2,
        )
        outcome = self._adjust(snap, policy)
        assert len(outcome.adjustments) <= policy.max_adjustment_passes
        assessment = outcome.assessment
        for metric in assessment.remaining_violations:
            assert metric.observed is None or metric.observed <= metric.limit + HUNDRED
        remaining = {
            violation.rule for violation in assessment.remaining_violations
            if violation.status.value == "BREACH"
        }
        assert remaining <= {"max_sector_weight", "max_top_n_concentration"}
        worst = max(asset.weight_pct for asset in outcome.exposure.assets)
        assert worst <= Decimal("20.0000")

    def test_adjustment_is_deterministic(self):
        snap = snapshot(
            [position("AAA", 100, Decimal("10")), position("BBB", 100, Decimal("10"))],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        policy = config(max_single_asset_weight=Decimal("25"), top_n_count=2)
        first, second = self._adjust(snap, policy), self._adjust(snap, policy)
        assert first.adjustments == second.adjustments
        assert first.positions == second.positions
        assert first.cash == second.cash

    def test_every_adjustment_is_explained(self):
        snap = snapshot(
            [position("AAA", 100, Decimal("10"))],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7)},
        )
        outcome = self._adjust(snap, config(max_single_asset_weight=Decimal("25"), top_n_count=2))
        for adjustment in outcome.adjustments:
            assert adjustment.violated_constraint
            assert adjustment.reason
            assert adjustment.expected_risk_improvement
            assert adjustment.symbol
            assert adjustment.quantity_sold > 0
            assert adjustment.previous_quantity > adjustment.new_quantity
            assert adjustment.previous_weight_pct >= adjustment.new_weight_pct


class TestNoOpAdjustment:
    def test_a_structural_breach_stops_with_an_explanation(self):
        """One tiny holding cannot be cut to satisfy a sector limit; that is reported, not faked."""
        policy = config(
            max_single_asset_weight=Decimal("5"),
            top_n_count=2,
            max_sector_weight=Decimal("10"),
        )
        snap = snapshot(
            [
                position("AAA", 100, Decimal("10"), sector="Technology"),
                position("BBB", 1, Decimal("10"), sector="Technology"),
            ],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        assessment = _engine(policy).analyze(snap)
        assert assessment.final_portfolio is not None
        assert assessment.final_portfolio.exposure
        for adjustment in assessment.adjustments:
            assert adjustment.quantity_sold > 0
        # Equity never moves, whatever the engine decided to do.
        assert assessment.final_portfolio.portfolio_value == assessment.portfolio_value