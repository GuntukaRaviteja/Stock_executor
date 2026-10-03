"""Phase 15 — deterministic stress testing.

Seven fixed scenarios are applied to the portfolio **as it stands**. Each one returns a shocked
book and the arithmetic consequence of that shock: what the portfolio would have returned, how
much of equity it would have lost, which position hurt most, and whether that loss breaches the
configured stress limit.

===========================  ==========================================================
``MARKET_CRASH_20``          every position falls by the configured broad-crash magnitude
``MARKET_CRASH_30``          every position falls by the configured severe-crash magnitude
``SECTOR_SHOCK``             one sector falls by the configured magnitude; others are untouched
``HIGH_BETA_SHOCK``          the base shock, steepened per unit of beta above the market's
``CORRELATION_SPIKE``        correlations blended toward a fully-correlated book, volatilities up
``LIQUIDITY_STRESS``         an extra haircut on positions whose volume does not support them
``STRATEGY_DRAWDOWN``        the largest strategy's capital is shocked by the configured magnitude
===========================  ==========================================================

These are **simulations, not forecasts**
---------------------------------------
Each scenario is a deterministic arithmetic transformation of today's holdings. No scenario
claims to predict what will happen, no probability is attached to any of them, and none of them
learns anything — they exist so a reviewer can ask "what would -20% have cost this book?" and get
the same answer every time. Real markets do not move the way these scenarios say they will, and a
portfolio that survives all seven is not thereby safe.

Data sufficiency is a first-class outcome
-----------------------------------------
A scenario that cannot be evaluated from the data supplied reports ``DATA_INSUFFICIENT`` and a
``None`` loss. The high-beta scenario without betas, the sector scenario without sector
classification and the liquidity scenario without traded volume all take this path: an unknown
answer is reported as unknown, never as "no loss".
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from app.schemas.portfolio_risk import RiskStatus, StressScenarioType
from app.services.backtest.models import quantize_percent
from app.services.portfolio_risk.config import ZERO, PortfolioRiskConfig
from app.services.portfolio_risk.covariance_analyzer import CovarianceAnalyzer
from app.services.portfolio_risk.evaluation import pct, status_for, utilization_pct
from app.services.portfolio_risk.models import (
    CovarianceAnalysis,
    PortfolioExposure,
    RiskPosition,
    StressOutcome,
    StressScenarioSpec,
)

#: One hundred — the scale of every percentage in this package.
_HUNDRED = Decimal("100")
#: Beta above which the high-beta scenario escalates a position's shock.
_BETA_REFERENCE = Decimal("1")
#: One-sided normal quantile used to turn a stressed volatility into a stressed loss.
_ONE_SIDED_Z_95 = Decimal("1.645")


class StressTester:
    """Deterministic stress simulation engine.

    Pure: the same positions, the same policy and the same covariance analysis always produce the
    same scenario results, in the same order.
    """

    def __init__(self, config: Optional[PortfolioRiskConfig] = None):
        self.config = config or PortfolioRiskConfig().validate()
        self.covariance_analyzer = CovarianceAnalyzer(self.config)

    # ------------------------------------------------------------------
    # Scenario catalogue
    # ------------------------------------------------------------------

    def scenarios(self, exposure: PortfolioExposure) -> List[StressScenarioSpec]:
        """The full catalogue, resolved against this portfolio.

        Resolution is deterministic: the sector scenario targets the configured sector when one is
        named, otherwise the heaviest sector (ties broken alphabetically), and the strategy
        scenario targets the heaviest strategy the same way.
        """
        sector, sector_targeted = self._sector_target(exposure)
        strategy, strategy_targeted = self._strategy_target(exposure)

        specs = [
            StressScenarioSpec(
                scenario_id="MARKET_CRASH_20",
                name=f"Broad market -{self.config.market_crash_shock.normalize()}%",
                description=(
                    "A broad, undifferentiated market decline in which every holding falls by the "
                    "same magnitude. It isolates market risk with no sector or stock-specific "
                    "story behind it."
                ),
                scenario_type=StressScenarioType.MARKET_CRASH_20,
                sector_shocks={sector: self.config.sector_shock} if sector_targeted else {},
                assumptions=(
                    f"Every position falls {self.config.market_crash_shock}%.",
                    "Correlations are unchanged; the decline is uniform.",
                    "No position is added or removed.",
                ),
            ),
            StressScenarioSpec(
                scenario_id="MARKET_CRASH_30",
                name=f"Severe market -{self.config.severe_crash_shock.normalize()}%",
                description=(
                    "A severe market decline of the configured magnitude applied to every "
                    "position, representing the tail of a broad sell-off rather than its centre."
                ),
                scenario_type=StressScenarioType.MARKET_CRASH_30,
                assumptions=(
                    f"Every position falls {self.config.severe_crash_shock}%.",
                    "Correlations are unchanged; the decline is uniform.",
                ),
            ),
            StressScenarioSpec(
                scenario_id="SECTOR_SHOCK",
                name=f"Sector shock -"
                f"{self.config.sector_shock.normalize()}% ({sector or 'no sector data'})",
                description=(
                    f"The {sector or 'unclassified'} sector falls by the configured magnitude while "
                    "every other holding is left untouched, isolating the portfolio's exposure to "
                    "one industry."
                )
                if sector_targeted
                else (
                    "No sector classification was supplied for any position, so the sector shock "
                    "cannot be targeted. It is reported as data-insufficient rather than applied "
                    "to an arbitrary group."
                ),
                scenario_type=StressScenarioType.SECTOR_SHOCK,
                sector_shocks={sector: self.config.sector_shock} if sector_targeted else {},
                assumptions=(
                    f"Only {sector} falls {self.config.sector_shock}%; other sectors are flat."
                    if sector_targeted
                    else "No position carries a sector classification.",
                ),
            ),
            StressScenarioSpec(
                scenario_id="HIGH_BETA_SHOCK",
                name=f"High-beta shock -{self.config.high_beta_shock.normalize()}% base",
                description=(
                    "A base market decline in which each position's loss is steepened by "
                    f"{self.config.high_beta_tilt.normalize()} percentage points per unit of beta "
                    "above the market's, so high-beta holdings fall disproportionately."
                ),
                scenario_type=StressScenarioType.HIGH_BETA_SHOCK,
                assumptions=(
                    f"Base shock {self.config.high_beta_shock}%, plus "
                    f"{self.config.high_beta_tilt}% per unit of beta above 1.0.",
                    "Positions with no beta take the base shock only, and are reported as such.",
                ),
            ),
            StressScenarioSpec(
                scenario_id="CORRELATION_SPIKE",
                name=f"Correlation spike +{self.config.correlation_spike_multiplier.normalize()}",
                description=(
                    "Diversification disappears: each pair's correlation is blended toward a "
                    "fully-correlated book and every position's volatility is scaled by "
                    f"{self.config.correlation_spike_vol_multiplier.normalize()}x. The loss is the "
                    "one-sided 95% move implied by the stressed volatility."
                ),
                scenario_type=StressScenarioType.CORRELATION_SPIKE,
                correlation_multiplier=self.config.correlation_spike_multiplier,
                correlation_vol_multiplier=self.config.correlation_spike_vol_multiplier,
                assumptions=(
                    "rho' = (1 - m) * rho + m, with "
                    f"m = {self.config.correlation_spike_multiplier}, which keeps the implied "
                    "covariance positive semi-definite by construction.",
                    f"Volatility is scaled by {self.config.correlation_spike_vol_multiplier}.",
                    "The implied loss is 1.645 x the stressed daily volatility, the one-sided "
                    "95% normal move — a parametric figure, not a simulated path.",
                ),
            ),
            StressScenarioSpec(
                scenario_id="LIQUIDITY_STRESS",
                name=f"Liquidity stress +{self.config.liquidity_haircut.normalize()}% haircut",
                description=(
                    "Positions whose size is large relative to normal traded volume carry an extra "
                    f"{self.config.liquidity_haircut.normalize()}% haircut, representing the market "
                    "impact of having to sell into a thin market."
                ),
                scenario_type=StressScenarioType.LIQUIDITY_STRESS,
                liquidity_haircut=self.config.liquidity_haircut,
                assumptions=(
                    f"Positions above the {self.config.max_participation_ratio} participation limit "
                    f"take an extra {self.config.liquidity_haircut}% haircut.",
                    "Positions with no traded-value data take no haircut and are reported as "
                    "unassessed rather than assumed liquid.",
                ),
            ),
            StressScenarioSpec(
                scenario_id="STRATEGY_DRAWDOWN",
                name=f"Strategy drawdown -{self.config.strategy_shock.normalize()}% "
                f"({strategy or 'no strategies'})",
                description=(
                    f"The capital allocated to the largest strategy, {strategy or 'none'}, falls by "
                    f"the configured {self.config.strategy_shock.normalize()}%, modelling a strategy "
                    "whose own signal stops working while the rest of the book is unaffected."
                )
                if strategy_targeted
                else "No strategy allocation exists, so there is nothing to shock.",
                scenario_type=StressScenarioType.STRATEGY_DRAWDOWN,
                strategy_shocks={strategy: self.config.strategy_shock} if strategy_targeted else {},
                assumptions=(
                    f"Every position of {strategy} falls {self.config.strategy_shock}%; other "
                    "strategies are flat."
                    if strategy_targeted
                    else "The portfolio holds no positions.",
                ),
            ),
        ]
        return specs

    def run(
        self,
        exposure: PortfolioExposure,
        positions: Sequence[RiskPosition],
        covariance: CovarianceAnalysis,
        portfolio_value: Decimal,
    ) -> List[StressOutcome]:
        """Apply every catalogue scenario and return the outcomes in catalogue order.

        ``portfolio_value`` is portfolio equity, supplied explicitly so the caller and the stress
        tester can never measure the same book against two different denominators.
        """
        return [
            self._run_scenario(spec, exposure, positions, covariance, portfolio_value)
            for spec in self.scenarios(exposure)
        ]

    # ------------------------------------------------------------------
    # Scenario evaluation
    # ------------------------------------------------------------------

    def _run_scenario(
        self,
        spec: StressScenarioSpec,
        exposure: PortfolioExposure,
        positions: Sequence[RiskPosition],
        covariance: CovarianceAnalysis,
        portfolio_value: Decimal,
    ) -> StressOutcome:
        """Evaluate one scenario, dispatching on its type.

        Every branch ends with either a computed loss or an explicit data-sufficiency status —
        there is no path that returns a zero loss because the data was missing.
        """
        handler = {
            StressScenarioType.MARKET_CRASH_20: self._uniform_shock,
            StressScenarioType.MARKET_CRASH_30: self._uniform_shock,
            StressScenarioType.SECTOR_SHOCK: self._sector_shock,
            StressScenarioType.HIGH_BETA_SHOCK: self._high_beta_shock,
            StressScenarioType.CORRELATION_SPIKE: self._correlation_spike,
            StressScenarioType.LIQUIDITY_STRESS: self._liquidity_stress,
            StressScenarioType.STRATEGY_DRAWDOWN: self._strategy_drawdown,
        }[spec.scenario_type]
        return handler(spec, exposure, positions, covariance, portfolio_value)

    def _uniform_shock(
        self,
        spec: StressScenarioSpec,
        exposure: PortfolioExposure,
        positions: Sequence[RiskPosition],
        covariance: CovarianceAnalysis,
        portfolio_value: Decimal,
    ) -> StressOutcome:
        """Apply one magnitude to every position."""
        magnitude = (
            self.config.market_crash_shock
            if spec.scenario_type is StressScenarioType.MARKET_CRASH_20
            else self.config.severe_crash_shock
        )
        shocks = {symbol: magnitude for symbol in sorted(exposure.symbols)}
        return self._from_position_shocks(
            spec, exposure, positions, portfolio_value, shocks, {}, {}, data_quality={"beta_coverage": "not_required"}
        )

    def _sector_shock(
        self,
        spec: StressScenarioSpec,
        exposure: PortfolioExposure,
        positions: Sequence[RiskPosition],
        covariance: CovarianceAnalysis,
        portfolio_value: Decimal,
    ) -> StressOutcome:
        """Apply the magnitude to one sector's symbols only."""
        if not spec.sector_shocks:
            return self._not_evaluated(
                spec,
                exposure,
                reason=(
                    "No position carries a sector classification, so no sector can be shocked. "
                    "The scenario is reported as data-insufficient rather than applied to an "
                    "arbitrary group of holdings."
                ),
                data_quality={"classified_positions": 0, "positions": len(positions)},
            )
        sector = next(iter(spec.sector_shocks))
        magnitude = spec.sector_shocks[sector]
        shocks = {
            asset.symbol: magnitude
            for asset in exposure.assets
            if asset.sector == sector
        }
        unclassified = sum(
            (asset.weight_pct for asset in exposure.assets if asset.sector is None), ZERO
        )
        return self._from_position_shocks(
            spec,
            exposure,
            positions,
            portfolio_value,
            shocks,
            {sector: magnitude},
            {},
            data_quality={
                "targeted_sector": sector,
                "unclassified_weight_pct": str(quantize_percent(unclassified)),
            },
        )

    def _high_beta_shock(
        self,
        spec: StressScenarioSpec,
        exposure: PortfolioExposure,
        positions: Sequence[RiskPosition],
        covariance: CovarianceAnalysis,
        portfolio_value: Decimal,
    ) -> StressOutcome:
        """Steepen the base shock per unit of beta above the market's."""
        with_beta = [asset for asset in exposure.assets if asset.beta is not None]
        if not with_beta:
            return self._not_evaluated(
                spec,
                exposure,
                reason=(
                    "No position carries a beta and no benchmark return series was supplied, so "
                    "the high-beta scenario cannot be targeted at anything. Reporting a loss here "
                    "would mean inventing the very input the scenario is about."
                ),
                data_quality={"beta_coverage": "0", "positions": len(positions)},
            )
        covered = sum((asset.weight_pct for asset in with_beta), ZERO)
        shocks: Dict[str, Decimal] = {}
        for asset in exposure.assets:
            if asset.beta is None:
                shocks[asset.symbol] = self.config.high_beta_shock
                continue
            excess = max(ZERO, Decimal(str(asset.beta)) - _BETA_REFERENCE)
            shocks[asset.symbol] = quantize_percent(
                self.config.high_beta_shock + self.config.high_beta_tilt * excess
            )
        uncovered = sorted(
            asset.symbol for asset in exposure.assets if asset.beta is None
        )
        note = None
        if uncovered:
            # The base shock stands in for these positions. That is an assumption, not a
            # measurement, so the outcome says which weight it rests on.
            note = (
                f"Only {pct(quantize_percent(covered))} of equity carries a beta; "
                f"{', '.join(uncovered)} took the base shock alone, which is an assumption rather "
                "than a measured beta."
            )
        return self._from_position_shocks(
            spec,
            exposure,
            positions,
            portfolio_value,
            shocks,
            {},
            {},
            data_quality={
                "beta_coverage_pct": str(quantize_percent(covered)),
                "positions_without_beta": uncovered,
            },
            extra_note=note,
        )

    def _correlation_spike(
        self,
        spec: StressScenarioSpec,
        exposure: PortfolioExposure,
        positions: Sequence[RiskPosition],
        covariance: CovarianceAnalysis,
        portfolio_value: Decimal,
    ) -> StressOutcome:
        """Re-price the book's volatility with diversification removed."""
        stressed = self.covariance_analyzer.spiked_volatility(
            covariance,
            spec.correlation_multiplier or ZERO,
            spec.correlation_vol_multiplier or Decimal("1"),
        )
        if stressed is None:
            return self._not_evaluated(
                spec,
                exposure,
                reason=(
                    "No covariance matrix could be built for the held positions, so correlations "
                    "cannot be stressed. The unstressed loss is not substituted for it: a "
                    "scenario that could not run is reported as such."
                ),
                data_quality={"covariance": covariance.data_quality},
            )

        factor = Decimal(str(math.sqrt(self.config.annualization_periods)))
        daily = stressed / factor
        loss = quantize_percent(daily * _ONE_SIDED_Z_95)
        limit = self.config.max_stress_loss
        utilization = utilization_pct(loss, limit)
        explanation = (
            f"With correlations blended toward a fully-correlated book and volatilities scaled by "
            f"{spec.correlation_vol_multiplier}, annualised portfolio volatility rises to "
            f"{pct(stressed)} from {pct(covariance.annualised_volatility_pct)}. The implied loss "
            f"is {pct(loss)} of equity, being 1.645 times the stressed daily volatility — a "
            "parametric one-sided move, not a replay of any historical path."
        )
        return StressOutcome(
            scenario_id=spec.scenario_id,
            name=spec.name,
            description=spec.description,
            scenario_type=spec.scenario_type,
            status=status_for(loss, limit, self.config.warning_utilization_pct),
            estimated_portfolio_return_pct=quantize_percent(-loss),
            estimated_portfolio_loss_pct=loss,
            worst_position_loss_pct=None,
            worst_position_symbol=None,
            risk_limit_pct=limit,
            utilization_pct=utilization,
            position_impacts={},
            data_quality={
                "covariance": covariance.data_quality,
                "observation_count": covariance.observation_count,
                "base_annualised_volatility_pct": str(covariance.annualised_volatility_pct),
                "stressed_annualised_volatility_pct": str(stressed),
            },
            explanation=explanation,
            assumptions=spec.assumptions,
        )

    def _liquidity_stress(
        self,
        spec: StressScenarioSpec,
        exposure: PortfolioExposure,
        positions: Sequence[RiskPosition],
        covariance: CovarianceAnalysis,
        portfolio_value: Decimal,
    ) -> StressOutcome:
        """Add a haircut to positions whose participation ratio exceeds the limit."""
        limit_ratio = self.config.max_participation_ratio
        if not any(asset.avg_daily_traded_value is not None for asset in exposure.assets):
            return self._not_evaluated(
                spec,
                exposure,
                reason=(
                    "LIQUIDITY_DATA_UNAVAILABLE: no average daily traded value was supplied, so no "
                    "position can be identified as illiquid. Assuming the book is liquid would "
                    "assume away the risk this scenario exists to measure."
                ),
                data_quality={"positions_with_volume": 0, "positions": len(positions)},
            )

        shocks: Dict[str, Decimal] = {}
        illiquid: List[str] = []
        unassessed: List[str] = []
        for asset in exposure.assets:
            if asset.avg_daily_traded_value is None or asset.participation_ratio is None:
                unassessed.append(asset.symbol)
                continue
            ratio = asset.participation_ratio
            if limit_ratio is not None and ratio > limit_ratio:
                shocks[asset.symbol] = self.config.liquidity_haircut
                illiquid.append(asset.symbol)
            else:
                shocks[asset.symbol] = ZERO

        if not illiquid:
            return self._not_evaluated(
                spec,
                exposure,
                reason=(
                    "Traded-value data covers the book and no position exceeds the "
                    f"{pct(limit_ratio)} participation limit, so this scenario has nothing to "
                    "shock. It is reported as not applicable to this portfolio rather than as a "
                    "passing loss of zero."
                ),
                status=RiskStatus.NOT_APPLICABLE,
                data_quality={
                    "positions_with_volume": len(exposure.assets) - len(unassessed),
                    "positions_without_volume": sorted(unassessed),
                    "illiquid_positions": [],
                },
            )

        return self._from_position_shocks(
            spec,
            exposure,
            positions,
            portfolio_value,
            shocks,
            {},
            {},
            data_quality={
                "illiquid_positions": sorted(illiquid),
                "positions_without_volume": sorted(unassessed),
                "participation_limit": str(limit_ratio),
            },
        )

    def _strategy_drawdown(
        self,
        spec: StressScenarioSpec,
        exposure: PortfolioExposure,
        positions: Sequence[RiskPosition],
        covariance: CovarianceAnalysis,
        portfolio_value: Decimal,
    ) -> StressOutcome:
        """Apply the magnitude to every position of one strategy."""
        if not spec.strategy_shocks:
            return self._not_evaluated(
                spec,
                exposure,
                reason=(
                    "The portfolio holds no positions, so there is no strategy allocation to "
                    "shock."
                ),
                data_quality={"strategies": 0},
                status=RiskStatus.NOT_APPLICABLE,
            )
        strategy = next(iter(spec.strategy_shocks))
        magnitude = spec.strategy_shocks[strategy]
        shocked = {
            position.symbol
            for position in positions
            if position.strategy_name == strategy
        }
        shocks = {
            symbol: (magnitude if symbol in shocked else ZERO)
            for symbol in sorted(exposure.symbols)
        }
        return self._from_position_shocks(
            spec,
            exposure,
            positions,
            portfolio_value,
            shocks,
            {},
            {strategy: magnitude},
            data_quality={"targeted_strategy": strategy, "shocked_symbols": sorted(shocked)},
        )

    # ------------------------------------------------------------------
    # Shared machinery
    # ------------------------------------------------------------------

    def _from_position_shocks(
        self,
        spec: StressScenarioSpec,
        exposure: PortfolioExposure,
        positions: Sequence[RiskPosition],
        portfolio_value: Decimal,
        shocks: Mapping[str, Decimal],
        sector_shocks: Mapping[str, Decimal],
        strategy_shocks: Mapping[str, Decimal],
        data_quality: Mapping[str, object],
        extra_note: Optional[str] = None,
    ) -> StressOutcome:
        """Turn per-symbol shocks into a portfolio loss and a status.

        The portfolio return is the equity-weighted sum of the individual position returns, which
        is the exact first-order P&L of holding today's book through the shock. Each weight is
        taken from the position's own market value rather than from the rounded reported weight, so
        a fully invested book shocked by 20% loses exactly 20% instead of 19.9999% of quantisation
        drift.
        """
        impacts: Dict[str, Decimal] = {}
        for asset in exposure.assets:
            shock = shocks.get(asset.symbol, ZERO)
            weight = asset.market_value / portfolio_value
            impacts[asset.symbol] = quantize_percent(-weight * shock)

        portfolio_return = quantize_percent(sum(impacts.values(), ZERO))
        loss = quantize_percent(max(ZERO, -portfolio_return))
        limit = self.config.max_stress_loss
        utilization = utilization_pct(loss, limit)

        worst_symbol = None
        worst_loss: Optional[Decimal] = None
        if impacts:
            worst_symbol = min(impacts, key=lambda symbol: (impacts[symbol], symbol))
            worst_loss = quantize_percent(max(ZERO, -impacts[worst_symbol]))

        explanation = (
            f"Applying the scenario to the {len(exposure.assets)} positions as they stand gives a "
            f"portfolio return of {pct(portfolio_return)} of equity, a loss of {pct(loss)}."
            + (
                f" The worst position is {worst_symbol} at {pct(worst_loss)} of equity."
                if worst_symbol
                else ""
            )
            + (
                f" That is {pct(utilization)} of the configured {pct(limit)} stress limit."
                if utilization is not None
                else " No stress limit is configured for this scenario."
            )
            + (f" {extra_note}" if extra_note else "")
        )

        return StressOutcome(
            scenario_id=spec.scenario_id,
            name=spec.name,
            description=spec.description,
            scenario_type=spec.scenario_type,
            status=status_for(loss, limit, self.config.warning_utilization_pct),
            estimated_portfolio_return_pct=portfolio_return,
            estimated_portfolio_loss_pct=loss,
            worst_position_loss_pct=worst_loss,
            worst_position_symbol=worst_symbol,
            risk_limit_pct=limit,
            utilization_pct=utilization,
            position_impacts=impacts,
            data_quality=dict(data_quality),
            explanation=explanation,
            assumptions=spec.assumptions,
            asset_shocks={symbol: shocks[symbol] for symbol in sorted(shocks)},
            sector_shocks=dict(sector_shocks),
            strategy_shocks=dict(strategy_shocks),
        )

    def _not_evaluated(
        self,
        spec: StressScenarioSpec,
        exposure: PortfolioExposure,
        reason: str,
        data_quality: Mapping[str, object],
        status: RiskStatus = RiskStatus.DATA_INSUFFICIENT,
    ) -> StressOutcome:
        """A scenario that could not be evaluated, reported as ``None`` rather than as zero.

        ``DATA_INSUFFICIENT`` says the inputs were missing; ``NOT_APPLICABLE`` says the inputs
        were present and the scenario simply has nothing to act on — a portfolio where no
        position is illiquid is not a portfolio whose liquidity stress test failed.
        """
        return StressOutcome(
            scenario_id=spec.scenario_id,
            name=spec.name,
            description=spec.description,
            scenario_type=spec.scenario_type,
            status=status,
            estimated_portfolio_return_pct=None,
            estimated_portfolio_loss_pct=None,
            worst_position_loss_pct=None,
            worst_position_symbol=None,
            risk_limit_pct=self.config.max_stress_loss,
            utilization_pct=None,
            position_impacts={},
            data_quality=dict(data_quality),
            explanation=reason,
            assumptions=spec.assumptions,
            asset_shocks={},
            sector_shocks=dict(spec.sector_shocks),
            strategy_shocks=dict(spec.strategy_shocks),
        )

    # ------------------------------------------------------------------
    # Target selection
    # ------------------------------------------------------------------

    def _sector_target(self, exposure: PortfolioExposure) -> Tuple[Optional[str], bool]:
        """The sector the shock targets: the configured one, else the heaviest classified sector."""
        configured = self.config.stress_sector
        classified = [group for group in exposure.sectors if group.limit_applied]
        if configured:
            matching = [group.name for group in classified if group.name == configured]
            return (configured, True) if matching else (configured, False)
        if not classified:
            return None, False
        heaviest = sorted(classified, key=lambda group: (-group.weight_pct, group.name))[0]
        return heaviest.name, True

    def _strategy_target(self, exposure: PortfolioExposure) -> Tuple[Optional[str], bool]:
        """The strategy the drawdown targets: the one holding the most equity."""
        if not exposure.strategies:
            return None, False
        heaviest = sorted(exposure.strategies, key=lambda group: (-group.weight_pct, group.name))[0]
        return heaviest.name, True


__all__ = ["StressTester"]