"""Phase 15 — the seven stress scenarios.

Covers the catalogue itself, each scenario's arithmetic, deterministic target selection, and the
difference between a scenario that could not be evaluated (``DATA_INSUFFICIENT``) and one that had
nothing to act on (``NOT_APPLICABLE``).

``position_impacts`` holds each position's contribution to the *portfolio* return, so a loss shows
up as a negative number there and as a positive magnitude in ``worst_position_loss_pct``.
"""

from decimal import Decimal

import pytest

from app.schemas.portfolio_risk import RiskStatus
from app.services.portfolio_risk.covariance_analyzer import CovarianceAnalyzer
from app.services.portfolio_risk.exposure_analyzer import ExposureAnalyzer
from app.services.portfolio_risk.stress_tester import StressTester
from app.services.portfolio_risk.risk_metrics import sample_std_dev
from tests.phase15_helpers import (
    ZERO,
    config,
    cyclic,
    diversified_snapshot,
    independent_pair,
    perfect_pair,
    position,
    rescale_to_volatility,
    snapshot,
)

HUNDRED = Decimal("100")
SQRT_252 = Decimal(str(252 ** 0.5))
ONE_SIDED_Z_95 = Decimal("1.645")


def _exposure(snap, policy=None):
    return ExposureAnalyzer(policy or config()).analyze(snap)


def _run(snap, policy=None):
    """Run the catalogue the way the engine does, and index the outcomes by id."""
    policy = policy or config()
    exposure = _exposure(snap, policy)
    covariance = CovarianceAnalyzer(policy).analyze(
        {asset.symbol: asset.weight_pct for asset in exposure.assets},
        snap.asset_returns,
        snap.benchmark_returns,
        snap.benchmark_symbol,
    )
    outcomes = StressTester(policy).run(exposure, snap.positions, covariance, snap.portfolio_value)
    return {outcome.scenario_id: outcome for outcome in outcomes}, outcomes


def _loss(outcome):
    return outcome.estimated_portfolio_loss_pct


class TestCatalogue:
    def test_all_seven_scenarios_are_present_and_ordered(self):
        _, outcomes = _run(diversified_snapshot())
        assert [outcome.scenario_id for outcome in outcomes] == [
            "MARKET_CRASH_20",
            "MARKET_CRASH_30",
            "SECTOR_SHOCK",
            "HIGH_BETA_SHOCK",
            "CORRELATION_SPIKE",
            "LIQUIDITY_STRESS",
            "STRATEGY_DRAWDOWN",
        ]

    def test_every_scenario_states_its_assumptions(self):
        _, outcomes = _run(diversified_snapshot())
        for outcome in outcomes:
            assert outcome.assumptions
            assert outcome.description
            assert outcome.explanation

    def test_target_selection_prefers_the_configured_sector_when_it_is_held(self):
        exposure = _exposure(diversified_snapshot())
        sector = next(
            spec
            for spec in StressTester(config(stress_sector="Healthcare")).scenarios(exposure)
            if spec.scenario_id == "SECTOR_SHOCK"
        )
        assert set(sector.sector_shocks) == {"Healthcare"}

    def test_target_selection_falls_back_to_the_heaviest_held_sector(self):
        exposure = _exposure(diversified_snapshot())
        sector = next(
            spec
            for spec in StressTester(config(stress_sector=None)).scenarios(exposure)
            if spec.scenario_id == "SECTOR_SHOCK"
        )
        # MOM holds AAA and BBB, both larger than either Healthcare holding.
        assert set(sector.sector_shocks) == {"Technology"}

    def test_strategy_target_is_the_heaviest_strategy(self):
        exposure = _exposure(diversified_snapshot())
        strategy = next(
            spec
            for spec in StressTester().scenarios(exposure)
            if spec.scenario_id == "STRATEGY_DRAWDOWN"
        )
        assert set(strategy.strategy_shocks) == {"MOM"}


class TestMarketCrashes:
    def test_a_fully_invested_book_loses_exactly_the_shock(self):
        policy = config()
        snap = snapshot(
            [position("AAA", 100, Decimal("10")), position("BBB", 100, Decimal("10"))],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        outcomes, _ = _run(snap, policy)
        assert _loss(outcomes["MARKET_CRASH_20"]) == policy.market_crash_shock
        assert _loss(outcomes["MARKET_CRASH_30"]) == policy.severe_crash_shock

    def test_severe_crash_is_worse_than_the_market_crash(self):
        outcomes, _ = _run(diversified_snapshot())
        assert _loss(outcomes["MARKET_CRASH_30"]) > _loss(outcomes["MARKET_CRASH_20"])

    def test_cash_cushions_a_market_decline(self):
        """A portfolio holding cash must lose less of its equity than a fully invested one."""
        invested_only = snapshot(
            [position("AAA", 100, Decimal("10"))],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7)},
        )
        with_cash = snapshot(
            [position("AAA", 100, Decimal("10"))],
            cash="500",
            asset_returns={"AAA": cyclic("0.01", 7)},
        )
        assert _loss(_run(invested_only)[0]["MARKET_CRASH_20"]) == Decimal("20.0000")
        assert _loss(_run(with_cash)[0]["MARKET_CRASH_20"]) == Decimal("13.3333")

    def test_every_position_receives_the_same_shock(self):
        outcomes, _ = _run(diversified_snapshot())
        crash = outcomes["MARKET_CRASH_20"]
        assert set(crash.asset_shocks.values()) == {config().market_crash_shock}
        # Contributions differ only because the positions are differently sized.
        assert len({abs(impact) for impact in crash.position_impacts.values()}) > 1

    def test_worst_position_is_the_largest_contribution(self):
        outcomes, _ = _run(diversified_snapshot())
        crash = outcomes["MARKET_CRASH_30"]
        assert crash.worst_position_symbol == min(
            crash.position_impacts, key=lambda symbol: (crash.position_impacts[symbol], symbol)
        )
        assert crash.worst_position_loss_pct == -min(crash.position_impacts.values())


class TestSectorShock:
    def test_only_the_targeted_sector_falls(self):
        outcomes, _ = _run(diversified_snapshot())
        shock = outcomes["SECTOR_SHOCK"]
        moved = {symbol for symbol, impact in shock.position_impacts.items() if impact < ZERO}
        assert moved == {"AAA", "BBB"}
        assert all(shock.position_impacts[symbol] == ZERO for symbol in ("CCC", "DDD"))

    def test_missing_sector_data_is_data_insufficient(self):
        snap = snapshot(
            [
                position("AAA", 100, Decimal("10"), sector=None),
                position("BBB", 100, Decimal("10"), sector=None),
            ],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        shock = _run(snap)[0]["SECTOR_SHOCK"]
        assert shock.status == RiskStatus.DATA_INSUFFICIENT
        assert shock.estimated_portfolio_loss_pct is None
        assert "arbitrary group" in shock.explanation

    def test_a_configured_sector_nobody_holds_is_not_shocked(self):
        """Targeting an unheld sector would report a zero loss, which reads as safety."""
        snap = snapshot(
            [position("AAA", 100, Decimal("10"), sector="Energy")],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7)},
        )
        shock = _run(snap, config(stress_sector="Technology"))[0]["SECTOR_SHOCK"]
        assert shock.status == RiskStatus.DATA_INSUFFICIENT
        assert shock.position_impacts == {}


class TestHighBetaShock:
    def test_higher_beta_positions_fall_further(self):
        snap = diversified_snapshot()
        outcomes, _ = _run(snap)
        shock = outcomes["HIGH_BETA_SHOCK"]
        assets = {asset.symbol: asset for asset in _exposure(snap).assets}
        assert assets["AAA"].beta > assets["CCC"].beta
        assert shock.position_impacts["AAA"] < shock.position_impacts["CCC"]

    def test_positions_without_beta_cannot_be_steepened(self):
        snap = snapshot(
            [
                position("AAA", 100, Decimal("10"), beta=None),
                position("BBB", 100, Decimal("10"), beta=None),
            ],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        shock = _run(snap)[0]["HIGH_BETA_SHOCK"]
        assert shock.status == RiskStatus.DATA_INSUFFICIENT
        assert shock.estimated_portfolio_loss_pct is None
        assert "beta" in shock.explanation.lower()

    def test_partial_beta_coverage_is_named_in_the_outcome(self):
        """A beta-less position takes the base shock, and the report says that is an assumption."""
        snap = snapshot(
            [
                position("AAA", 50, Decimal("10"), beta="1.5"),
                position("BBB", 50, Decimal("10"), beta=None),
            ],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        shock = _run(snap)[0]["HIGH_BETA_SHOCK"]
        assert shock.data_quality["beta_coverage_pct"] == "50.0000"
        assert shock.data_quality["positions_without_beta"] == ["BBB"]
        assert "assumption rather" in shock.explanation
        # The steepened position falls further than the flat one.
        assert shock.position_impacts["AAA"] < shock.position_impacts["BBB"]


class TestCorrelationSpike:
    def test_loss_is_the_one_sided_move_of_the_stressed_volatility(self):
        policy = config()
        snap = diversified_snapshot()
        outcomes, _ = _run(snap, policy)
        spike = outcomes["CORRELATION_SPIKE"]

        exposure = _exposure(snap, policy)
        analysis = CovarianceAnalyzer(policy).analyze(
            {asset.symbol: asset.weight_pct for asset in exposure.assets},
            snap.asset_returns,
        )
        stressed = CovarianceAnalyzer(policy).spiked_volatility(
            analysis,
            policy.correlation_spike_multiplier,
            policy.correlation_spike_vol_multiplier,
        )
        assert stressed > analysis.annualised_volatility_pct
        assert _loss(spike) == pytest.approx(
            stressed / SQRT_252 * ONE_SIDED_Z_95, abs=Decimal("0.0001")
        )
        assert spike.data_quality["base_annualised_volatility_pct"] == str(
            analysis.annualised_volatility_pct
        )

    def test_a_larger_multiplier_hurts_more(self):
        snap = diversified_snapshot()
        base, _ = _run(snap, config(correlation_spike_multiplier=Decimal("0.3")))
        severe, _ = _run(snap, config(correlation_spike_multiplier=Decimal("0.9")))
        assert _loss(severe["CORRELATION_SPIKE"]) > _loss(base["CORRELATION_SPIKE"])

    def test_a_correlated_book_stresses_worse_than_a_diversified_one(self):
        """Diversification that is really there absorbs the spike.

        Both books hold the same two positions with the same individual volatility, so the only
        difference between them is the correlation: rescaling the independent series to the
        correlated series' standard deviation keeps the comparison honest.
        """
        book = [position("AAA", 50, Decimal("10")), position("BBB", 50, Decimal("10"))]
        base, shifted = perfect_pair(scale="0.005")
        target = sample_std_dev(base)
        first, second = independent_pair(scale="0.01")
        correlated = snapshot(
            book, cash="0", asset_returns={"AAA": base, "BBB": shifted}
        )
        diversified = snapshot(
            book,
            cash="0",
            asset_returns={
                "AAA": rescale_to_volatility(first, target),
                "BBB": rescale_to_volatility(second, target),
            },
        )
        assert sample_std_dev(diversified.asset_returns["AAA"]) == pytest.approx(
            sample_std_dev(correlated.asset_returns["AAA"]), abs=Decimal("0.0001")
        )
        assert _loss(_run(correlated)[0]["CORRELATION_SPIKE"]) > _loss(
            _run(diversified)[0]["CORRELATION_SPIKE"]
        )

    def test_spike_needs_a_covariance_matrix(self):
        spike = _run(diversified_snapshot(asset_returns={}))[0]["CORRELATION_SPIKE"]
        assert spike.status == RiskStatus.DATA_INSUFFICIENT
        assert spike.estimated_portfolio_loss_pct is None
        assert "not substituted" in spike.explanation


class TestLiquidityStress:
    def _book(self, volume):
        return snapshot(
            [
                position("AAA", 10, Decimal("10"), avg_daily_traded_value=volume),
                position("BBB", 10, Decimal("10"), avg_daily_traded_value=volume),
            ],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )

    def test_a_thin_book_carries_a_real_haircut(self):
        thin = _run(self._book("100"))[0]["LIQUIDITY_STRESS"]
        assert thin.status != RiskStatus.NOT_APPLICABLE
        assert _loss(thin) > ZERO

    def test_a_thicker_book_carries_a_smaller_one(self):
        """Both books sit above the participation limit, so both are haircut, less or more."""
        thinner = _run(self._book("150"))[0]["LIQUIDITY_STRESS"]
        thicker = _run(self._book("300"))[0]["LIQUIDITY_STRESS"]
        assert _loss(thicker) > ZERO
        assert _loss(thinner) >= _loss(thicker)

    def test_missing_volume_data_is_not_assumed_liquid(self):
        stress = _run(diversified_snapshot())[0]["LIQUIDITY_STRESS"]
        assert stress.status == RiskStatus.DATA_INSUFFICIENT
        assert stress.position_impacts == {}
        assert "Assuming the book is liquid" in stress.explanation

    def test_a_fully_liquid_book_is_not_applicable_rather_than_failed(self):
        stress = _run(self._book("1000000"))[0]["LIQUIDITY_STRESS"]
        assert stress.status == RiskStatus.NOT_APPLICABLE
        assert stress.estimated_portfolio_loss_pct is None

    def test_only_illiquid_positions_take_the_haircut(self):
        mixed = snapshot(
            [
                position("AAA", 10, Decimal("10"), avg_daily_traded_value="1000000"),
                position("BBB", 10, Decimal("10"), avg_daily_traded_value="100"),
            ],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7), "BBB": cyclic("0.008", 5)},
        )
        stress = _run(mixed)[0]["LIQUIDITY_STRESS"]
        assert stress.position_impacts["AAA"] == ZERO
        assert stress.position_impacts["BBB"] < ZERO


class TestStrategyDrawdown:
    def test_only_the_targeted_strategy_falls(self):
        outcomes, _ = _run(diversified_snapshot())
        shock = outcomes["STRATEGY_DRAWDOWN"]
        moved = {symbol for symbol, impact in shock.position_impacts.items() if impact < ZERO}
        assert moved == {"AAA", "BBB"}

    def test_an_empty_portfolio_is_not_applicable(self):
        shock = _run(snapshot([], cash="1000"))[0]["STRATEGY_DRAWDOWN"]
        assert shock.status == RiskStatus.NOT_APPLICABLE
        assert shock.estimated_portfolio_loss_pct is None
        assert "nothing to shock" in shock.description or "no strategy allocation" in shock.explanation


class TestScenarioAccounting:
    def test_position_impacts_sum_to_the_portfolio_return(self):
        _, outcomes = _run(diversified_snapshot())
        for outcome in outcomes:
            if not outcome.position_impacts:
                continue
            assert sum(outcome.position_impacts.values(), ZERO) == pytest.approx(
                outcome.estimated_portfolio_return_pct, abs=Decimal("0.0001")
            )

    def test_a_loss_is_the_negation_of_a_return(self):
        _, outcomes = _run(diversified_snapshot())
        for outcome in outcomes:
            if outcome.estimated_portfolio_return_pct is None:
                continue
            assert _loss(outcome) == -outcome.estimated_portfolio_return_pct

    def test_stress_is_deterministic(self):
        snap = diversified_snapshot()
        assert _run(snap)[1] == _run(snap)[1]

    def test_stress_magnitudes_follow_the_configuration(self):
        snap = diversified_snapshot()
        default, _ = _run(snap)
        harsher, _ = _run(snap, config(market_crash_shock=Decimal("40")))
        assert _loss(harsher["MARKET_CRASH_20"]) > _loss(default["MARKET_CRASH_20"])

    def test_a_breached_stress_limit_is_reported_as_such(self):
        outcomes, _ = _run(diversified_snapshot(), config(max_stress_loss=Decimal("5")))
        crash = outcomes["MARKET_CRASH_30"]
        assert crash.status == RiskStatus.BREACH
        assert crash.utilization_pct > HUNDRED