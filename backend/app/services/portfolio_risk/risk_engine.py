"""Phase 15 — the portfolio risk engine.

The one object that runs the whole Phase 15 stack and produces the canonical report. It composes
the measuring modules in a fixed order and then asks the controller what to do about what they
found:

    snapshot
      -> ExposureAnalyzer            asset / sector / strategy / class exposure + concentration
      -> CovarianceAnalyzer         correlation, covariance, volatility, risk contribution
      -> RiskMetricsEngine           volatility, beta, drawdown, VaR, Expected Shortfall
      -> LiquidityAnalyzer           participation ratios, liquidity score
      -> StressTester                the seven deterministic scenarios
      -> RiskController              violations, then the decision
      -> RiskAdjustmentEngine        deterministic reductions, re-evaluated each pass
      -> PortfolioRiskAssessment     one report

Pure and deterministic
----------------------
No clock, no database, no randomness, no iteration-order dependence. The same snapshot and policy
always produce an equal report: every list is sorted, every comparison carries a tolerance, and
adjustments are chosen by explicit priority rather than by a search. That is what makes the output
auditable — a reviewer can re-run the same inputs and diff the report.

Adjust then re-measure
----------------------
Adjustment runs only when the policy enables it and something is actually breached. After the
last reduction the whole stack runs **again** on the adjusted book, so the report's metrics,
violations and stress results describe the portfolio that would actually be traded — not the one
that was rejected. ``violations`` records what was found in the input; ``remaining_violations``
records what is still true afterwards.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.logging import logger
from app.schemas.portfolio_risk import PortfolioRiskDecision, RiskStatus
from app.services.backtest.models import quantize_percent
from app.services.portfolio_risk.config import ZERO, PortfolioRiskConfig
from app.services.portfolio_risk.covariance_analyzer import CovarianceAnalyzer
from app.services.portfolio_risk.evaluation import pct, status_for, utilization_pct
from app.services.portfolio_risk.exposure_analyzer import ExposureAnalyzer
from app.services.portfolio_risk.liquidity_analyzer import LiquidityAnalyzer
from app.services.portfolio_risk.models import (
    UNCLASSIFIED_SECTOR,
    AdjustedPortfolio,
    AssetExposure,
    ConcentrationMetric,
    CovarianceAnalysis,
    LiquidityAnalysis,
    PortfolioExposure,
    PortfolioRiskAssessment,
    RiskAdjustment,
    RiskBudgetResult,
    RiskMetricResult,
    RiskPosition,
    RiskSnapshot,
    RiskViolation,
    StressOutcome,
)
from app.services.portfolio_risk.risk_controller import (
    EvaluationBundle,
    RiskAdjustmentEngine,
    RiskController,
)
from app.services.portfolio_risk.risk_metrics import RiskMetricsEngine
from app.services.portfolio_risk.stress_tester import StressTester

#: One hundred — the scale of every percentage in this package.
_HUNDRED = Decimal("100")

#: Risk budget name -> (config field, label).
_BUDGET_FIELDS = {
    "risk_budget_asset_concentration": ("ASSET_CONCENTRATION", "Asset concentration"),
    "risk_budget_sector_concentration": ("SECTOR_CONCENTRATION", "Sector concentration"),
    "risk_budget_strategy_concentration": ("STRATEGY_CONCENTRATION", "Strategy concentration"),
    "risk_budget_volatility": ("VOLATILITY", "Volatility"),
    "risk_budget_beta": ("BETA", "Beta"),
    "risk_budget_drawdown": ("DRAWDOWN", "Drawdown"),
    "risk_budget_var": ("VAR", "Value at Risk"),
    "risk_budget_stress": ("STRESS", "Stress loss"),
}


class PortfolioRiskEngine:
    """Portfolio-level risk evaluation and control."""

    def __init__(self, config: Optional[PortfolioRiskConfig] = None):
        self.config = (config or PortfolioRiskConfig()).validate()
        self.exposure_analyzer = ExposureAnalyzer(self.config)
        self.covariance_analyzer = CovarianceAnalyzer(self.config)
        self.metrics_engine = RiskMetricsEngine(self.config)
        self.liquidity_analyzer = LiquidityAnalyzer(self.config)
        self.stress_tester = StressTester(self.config)
        self.controller = RiskController(self.config)
        self.adjuster = RiskAdjustmentEngine(self.config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, snapshot: RiskSnapshot) -> PortfolioRiskAssessment:
        """Evaluate the portfolio and return the canonical risk report."""
        logger.info(
            "PortfolioRiskEngine: starting portfolio risk analysis for portfolio_id=%s "
            "positions=%d",
            snapshot.portfolio_id,
            len(snapshot.positions),
        )

        evaluation = self.evaluate(snapshot.positions, snapshot.cash, snapshot.portfolio_value, snapshot)

        adjustments: Tuple[RiskAdjustment, ...] = ()
        final: Optional[AdjustedPortfolio] = None
        remaining: Tuple[RiskViolation, ...] = evaluation.violations
        stopped_reason = "Adjustment was not required."

        needs_adjustment = any(
            violation.status is RiskStatus.BREACH for violation in evaluation.violations
        )
        if self.config.enable_adjustment and needs_adjustment:
            outcome = self.adjuster.adjust(snapshot, self._evaluator(snapshot))
            adjustments = outcome.adjustments
            stopped_reason = outcome.stopped_reason
            if adjustments:
                final_exposure = self.exposure_analyzer.analyze(
                    RiskSnapshot(
                        portfolio_id=snapshot.portfolio_id,
                        portfolio_value=snapshot.portfolio_value,
                        cash=outcome.cash,
                        positions=outcome.positions,
                    )
                )
                remaining = self._violations_for(outcome.positions, outcome.cash, snapshot)
                final = AdjustedPortfolio(
                    portfolio_id=snapshot.portfolio_id,
                    portfolio_value=Decimal(str(snapshot.portfolio_value)),
                    cash=outcome.cash,
                    positions=outcome.positions,
                    exposure=final_exposure,
                )

        decision, decision_explanation, decision_details = self.controller.decide(
            remaining, adjustments
        )

        data_quality = self._data_quality(snapshot, evaluation)
        explanation = self._explanation(
            snapshot, evaluation, remaining, adjustments, decision, stopped_reason
        )

        logger.info(
            "PortfolioRiskEngine: portfolio_id=%s decision=%s violations=%d remaining=%d "
            "adjustments=%d data_quality=%s",
            snapshot.portfolio_id,
            decision.value,
            len(evaluation.violations),
            len(remaining),
            len(adjustments),
            evaluation.covariance.data_quality,
        )

        return PortfolioRiskAssessment(
            portfolio_id=snapshot.portfolio_id,
            portfolio_value=Decimal(str(snapshot.portfolio_value)),
            decision=decision,
            decision_explanation=decision_explanation,
            exposure=evaluation.exposure,
            concentration=evaluation.concentration,
            risk_metrics=evaluation.metrics,
            risk_budgets=self.risk_budgets(evaluation),
            risk_contributions=evaluation.covariance.risk_contributions,
            correlation_clusters=evaluation.covariance.clusters,
            covariance=evaluation.covariance,
            stress_tests=evaluation.stress_tests,
            liquidity=evaluation.liquidity,
            violations=evaluation.violations,
            remaining_violations=remaining,
            adjustments=adjustments,
            final_portfolio=final,
            data_quality=data_quality,
            explanation=explanation,
            decision_details=decision_details,
            config_applied=self.policy_summary(),
        )

    def evaluate(
        self,
        positions: Sequence[RiskPosition],
        cash: Decimal,
        portfolio_value: Decimal,
        snapshot: Optional[RiskSnapshot] = None,
    ) -> EvaluationBundle:
        """Run every measurement for one candidate position set.

        The adjustment loop calls this repeatedly with reduced holdings, which is why it takes the
        position set and cash as arguments rather than reading a snapshot.
        """
        candidate = RiskSnapshot(
            portfolio_id=snapshot.portfolio_id if snapshot else None,
            portfolio_value=Decimal(str(portfolio_value)),
            cash=Decimal(str(cash)),
            positions=tuple(positions),
            asset_returns=snapshot.asset_returns if snapshot else {},
            portfolio_returns=snapshot.portfolio_returns if snapshot else (),
            equity_curve=snapshot.equity_curve if snapshot else (),
            benchmark_symbol=snapshot.benchmark_symbol if snapshot else None,
            benchmark_returns=snapshot.benchmark_returns if snapshot else (),
        )
        return self._evaluate_snapshot(candidate)

    # ------------------------------------------------------------------
    # Measurement
    # ------------------------------------------------------------------

    def _evaluate_snapshot(self, snapshot: RiskSnapshot) -> EvaluationBundle:
        """Measure one portfolio: exposure, covariance, metrics, liquidity, stress, violations."""
        exposure = self.exposure_analyzer.analyze(snapshot)
        covariance = self.covariance_analyzer.analyze(
            {asset.symbol: asset.weight_pct for asset in exposure.assets},
            snapshot.asset_returns,
            snapshot.benchmark_returns,
            snapshot.benchmark_symbol or self.config.benchmark_symbol,
        )
        metrics = self.metrics_engine.calculate(snapshot, exposure, covariance)
        liquidity = self.liquidity_analyzer.analyze(exposure)
        stress_tests = (
            ()
            if self.config.disable_stress_tests
            else tuple(
                self.stress_tester.run(
                    exposure, snapshot.positions, covariance, snapshot.portfolio_value
                )
            )
        )
        concentration = self._concentration(exposure, covariance, metrics, stress_tests, liquidity)
        violations = self.controller.detect_violations(
            concentration, metrics, stress_tests, liquidity
        )
        return EvaluationBundle(
            exposure=exposure,
            concentration=concentration,
            metrics=metrics,
            covariance=covariance,
            stress_tests=stress_tests,
            liquidity=liquidity,
            violations=violations,
        )

    def _evaluator(self, snapshot: RiskSnapshot):
        """A closure the adjuster can call with any candidate position set."""

        def evaluate(positions: Sequence[RiskPosition], cash: Decimal) -> EvaluationBundle:
            return self.evaluate(positions, cash, snapshot.portfolio_value, snapshot)

        return evaluate

    def _violations_for(
        self,
        positions: Sequence[RiskPosition],
        cash: Decimal,
        snapshot: RiskSnapshot,
    ) -> Tuple[RiskViolation, ...]:
        """Violations that survive for a candidate (adjusted) position set."""
        return self.evaluate(positions, cash, snapshot.portfolio_value, snapshot).violations

    def _concentration(
        self,
        exposure: PortfolioExposure,
        covariance: CovarianceAnalysis,
        metrics: Mapping[str, RiskMetricResult],
        stress_tests: Sequence[StressOutcome],
        liquidity: LiquidityAnalysis,
    ) -> Tuple[ConcentrationMetric, ...]:
        """Exposure concentration, plus the correlation-concentration and count rules.

        Correlation concentration is measured as the **weighted average pairwise correlation**,
        because that is the quantity bounded by 100% and therefore comparable to a configured
        threshold. The raw concentration sum ``sum over i != j of w_i w_j rho_ij`` is published
        in the details instead, because it is the quantity the weighted average is derived from.
        """
        metrics_list = list(self.exposure_analyzer.concentration_metrics(exposure))

        average = covariance.weighted_average_correlation
        if average is None:
            metrics_list.append(
                ConcentrationMetric(
                    metric="correlation_concentration",
                    subject=None,
                    value_pct=ZERO,
                    limit_pct=self.config.max_correlation_concentration,
                    utilization_pct=None,
                    status=RiskStatus.DATA_INSUFFICIENT,
                    details={"covariance": covariance.data_quality},
                )
            )
        else:
            value = quantize_percent(average * _HUNDRED)
            metrics_list.append(
                ConcentrationMetric(
                    metric="correlation_concentration",
                    subject=None,
                    value_pct=value,
                    limit_pct=self.config.max_correlation_concentration,
                    utilization_pct=utilization_pct(value, self.config.max_correlation_concentration),
                    status=status_for(
                        value,
                        self.config.max_correlation_concentration,
                        self.config.warning_utilization_pct,
                    ),
                    details={
                        "weighted_average_correlation": str(average),
                        "concentration_sum": str(covariance.correlation_concentration),
                        "observation_count": covariance.observation_count,
                        "unavailable_pairs": len(covariance.unavailable_pairs),
                    },
                )
            )

        count_metric = self.exposure_analyzer.position_count_status(exposure)
        if count_metric is not None:
            metrics_list.append(count_metric)
        return tuple(metrics_list)

    # ------------------------------------------------------------------
    # Risk budgets
    # ------------------------------------------------------------------

    def risk_budgets(self, evaluation: EvaluationBundle) -> Tuple[RiskBudgetResult, ...]:
        """Consumption of each configured risk budget.

        A budget is a *target* the portfolio is measured against, which is why an overrun shows up
        as a budget breach in its own right. It is reported rather than acted on: budgets express
        intent, while limits express constraints, and this engine only trades against constraints.
        """
        budgets: List[RiskBudgetResult] = []
        observed = self._budget_observations(evaluation)

        for field_name, (label, metric_name) in _BUDGET_FIELDS.items():
            limit = getattr(self.config, field_name)
            if limit is None:
                continue
            value = observed.get(metric_name)
            status = status_for(value, limit, self.config.warning_utilization_pct)
            remaining = None if value is None else quantize_percent(limit - value)
            explanation = (
                f"{label} budget is {pct(value)} against a target of {pct(limit)}"
                + (
                    f", using {pct(utilization_pct(value, limit))} of it."
                    if utilization_pct(value, limit) is not None
                    else "."
                )
                if value is not None
                else (
                    f"{label} budget is {pct(limit)}, but the underlying metric could not be "
                    "computed from the data supplied, so no consumption is reported."
                )
            )
            budgets.append(
                RiskBudgetResult(
                    budget=label,
                    metric=metric_name,
                    value=value,
                    limit=limit,
                    utilization_pct=utilization_pct(value, limit),
                    remaining=remaining,
                    status=status,
                    explanation=explanation,
                )
            )
        return tuple(budgets)

    def _budget_observations(self, evaluation: EvaluationBundle) -> Dict[str, Optional[Decimal]]:
        """The value each budget is measured against."""
        exposure = evaluation.exposure
        metrics = evaluation.metrics
        observations: Dict[str, Optional[Decimal]] = {
            "ASSET_CONCENTRATION": exposure.summary.largest_position_pct,
            "SECTOR_CONCENTRATION": _max_group(exposure.sectors, applied_only=True),
            "STRATEGY_CONCENTRATION": _max_group(exposure.strategies),
            "VOLATILITY": _metric_value(metrics, "volatility"),
            "BETA": _metric_value(metrics, "beta"),
            "DRAWDOWN": _metric_value(metrics, "max_drawdown"),
            "VAR": _metric_value(metrics, "var_95"),
            "STRESS": _max_stress_loss(evaluation.stress_tests),
        }
        return observations

    # ------------------------------------------------------------------
    # Data quality and explanation
    # ------------------------------------------------------------------

    def _data_quality(
        self,
        snapshot: RiskSnapshot,
        evaluation: EvaluationBundle,
    ) -> Mapping[str, object]:
        """Explicit record of what could and could not be measured.

        Published in the report so a reader never has to infer it from a missing number. Every
        unmeasured item appears here as ``False`` or a count — never as an assumed zero.
        """
        exposure = evaluation.exposure
        covariance = evaluation.covariance
        with_sector = sum(1 for asset in exposure.assets if asset.sector)
        with_beta = sum(1 for asset in exposure.assets if asset.beta is not None)
        with_volume = sum(
            1 for asset in exposure.assets if asset.avg_daily_traded_value is not None
        )
        return {
            "covariance": covariance.data_quality,
            "covariance_observations": covariance.observation_count,
            "covariance_weight_coverage_pct": str(covariance.weight_coverage_pct),
            "covariance_excluded_symbols": list(covariance.excluded_symbols),
            "covariance_unavailable_pairs": len(covariance.unavailable_pairs),
            "covariance_reconciliation_error_pct": str(covariance.reconciliation_error_pct),
            "min_observations_required": self.config.min_observations,
            "positions_total": exposure.summary.total_positions,
            "positions_with_sector": with_sector,
            "positions_without_sector": exposure.summary.total_positions - with_sector,
            "unclassified_sector_weight_pct": str(exposure.summary.unclassified_sector_pct),
            "positions_with_beta": with_beta,
            "positions_with_volume": with_volume,
            "portfolio_returns_supplied": len(snapshot.portfolio_returns),
            "equity_curve_supplied": len(snapshot.equity_curve),
            "benchmark_supplied": bool(snapshot.benchmark_returns),
            "var_observations_required": self.config.min_var_observations,
            "stress_scenarios_run": len(evaluation.stress_tests),
            "liquidity_status": evaluation.liquidity.status.value,
        }

    def _explanation(
        self,
        snapshot: RiskSnapshot,
        evaluation: EvaluationBundle,
        remaining: Sequence[RiskViolation],
        adjustments: Sequence[RiskAdjustment],
        decision: PortfolioRiskDecision,
        stopped_reason: str,
    ) -> Tuple[str, ...]:
        """Plain-language narrative of the run, in reading order.

        Every line states a number. This is the part a reviewer reads first, so it is written to
        be correct on its own rather than to be a summary of the machine-readable fields.
        """
        exposure = evaluation.exposure
        summary = exposure.summary
        lines: List[str] = [
            (
                f"Portfolio holds {summary.total_positions} distinct position(s) across "
                f"{summary.total_lots} strategy lot(s), with {pct(summary.invested_pct)} of "
                f"{snapshot.portfolio_value} equity invested and {pct(summary.cash_pct)} in cash."
            )
        ]

        if summary.largest_position_symbol:
            lines.append(
                f"The largest position is {summary.largest_position_symbol} at "
                f"{pct(summary.largest_position_pct)}, and the top {summary.top_n_count} "
                f"position(s) together make up {pct(summary.top_n_concentration_pct)}."
            )

        over_sectors = [group for group in exposure.sectors if group.status is RiskStatus.BREACH]
        if over_sectors:
            lines.append(
                "Sectors above their configured limit: "
                + "; ".join(
                    f"{group.name} at {pct(group.weight_pct)} against {pct(group.limit_pct)}"
                    for group in sorted(over_sectors, key=lambda item: item.name)
                )
                + "."
            )
        over_strategies = [group for group in exposure.strategies if group.status is RiskStatus.BREACH]
        if over_strategies:
            lines.append(
                "Strategies above their configured limit: "
                + "; ".join(
                    f"{group.name} at {pct(group.weight_pct)} against {pct(group.limit_pct)}"
                    for group in sorted(over_strategies, key=lambda item: item.name)
                )
                + "."
            )

        if summary.unclassified_sector_pct > ZERO:
            lines.append(
                f"{pct(summary.unclassified_sector_pct)} of the portfolio carries no sector "
                "classification; it is reported in full but the sector limit is not applied to it, "
                "because the engine cannot know which sector it belongs to."
            )

        volatility = evaluation.metrics.get("volatility")
        if volatility is not None and volatility.value is not None:
            lines.append(volatility.explanation)
        covariance = evaluation.covariance
        if covariance.clusters:
            lines.append(
                "Highly correlated groups were detected: "
                + "; ".join(
                    f"{', '.join(cluster.symbols)} at average correlation "
                    f"{quantize_percent(cluster.average_correlation * _HUNDRED)} carrying "
                    f"{pct(cluster.combined_weight_pct)} of the portfolio"
                    for cluster in covariance.clusters
                )
                + ". Correlation here means shared exposure, not a causal relationship."
            )

        if evaluation.stress_tests:
            worst = _worst_stress(evaluation.stress_tests)
            if worst is not None and worst.estimated_portfolio_loss_pct is not None:
                lines.append(
                    f"Under the worst simulated scenario ({worst.scenario_id}) the portfolio would "
                    f"lose {pct(worst.estimated_portfolio_loss_pct)} of equity. Stress scenarios "
                    "are deterministic simulations of today's holdings, not forecasts."
                )
            unrun = [
                outcome
                for outcome in evaluation.stress_tests
                if outcome.status is RiskStatus.DATA_INSUFFICIENT
            ]
            for outcome in unrun:
                lines.append(f"Scenario {outcome.scenario_id} could not be run. {outcome.explanation}")

        lines.append(evaluation.liquidity.explanation)

        # Breaches and gaps are counted separately. A limit that could not be evaluated is not a
        # limit that was broken, and a report that calls a coverage gap a breach overstates what
        # is known about the portfolio in the direction of sounding alarming about missing data.
        breached = [
            violation
            for violation in evaluation.violations
            if violation.status is RiskStatus.BREACH
        ]
        gaps = [
            violation
            for violation in evaluation.violations
            if violation.status is RiskStatus.DATA_INSUFFICIENT
        ]
        if breached:
            lines.append(
                f"{len(breached)} risk limit(s) are breached in the portfolio as supplied."
            )
        if gaps:
            lines.append(
                f"{len(gaps)} risk limit(s) could not be evaluated at all, so compliance with them "
                "is unknown rather than confirmed."
            )
        if adjustments:
            lines.append(
                f"{len(adjustments)} deterministic reduction(s) were applied to correct them: "
                + "; ".join(adjustment.reason for adjustment in adjustments)
            )
        lines.append(stopped_reason)

        still_breached = [
            violation for violation in remaining if violation.status is RiskStatus.BREACH
        ]
        still_missing = [
            violation for violation in remaining if violation.status is RiskStatus.DATA_INSUFFICIENT
        ]
        if still_breached:
            lines.append(
                f"{len(still_breached)} breach(es) remain: "
                + " ".join(violation.explanation for violation in still_breached)
            )
        if still_missing:
            lines.append(
                f"{len(still_missing)} limit(s) remain unevaluated: "
                + " ".join(violation.explanation for violation in still_missing)
            )

        lines.append(
            "All risk figures are estimates from the historical sample supplied. Missing data is "
            "reported as missing, never as zero risk, and the limits applied are configurable "
            f"policy parameters. Decision: {decision.value}."
        )
        return tuple(lines)

    def policy_summary(self) -> Mapping[str, object]:
        """The policy actually applied, so a report can be reproduced from it."""
        return {
            "max_single_asset_weight": _limit_summary(self.config, 'max_single_asset_weight'),
            "max_sector_weight": _limit_summary(self.config, 'max_sector_weight'),
            "max_strategy_weight": _limit_summary(self.config, 'max_strategy_weight'),
            "max_asset_class_weight": _limit_summary(self.config, 'max_asset_class_weight'),
            "max_top_n_concentration": _limit_summary(self.config, 'max_top_n_concentration'),
            "top_n_count": self.config.top_n_count,
            "max_portfolio_volatility": _limit_summary(self.config, 'max_portfolio_volatility'),
            "max_portfolio_beta": _limit_summary(self.config, 'max_portfolio_beta'),
            "max_drawdown": _limit_summary(self.config, 'max_drawdown'),
            "max_correlation_concentration": _limit_summary(self.config, 'max_correlation_concentration'),
            "max_stress_loss": _limit_summary(self.config, 'max_stress_loss'),
            "max_participation_ratio": _limit_summary(self.config, 'max_participation_ratio'),
            "minimum_liquidity_score": _limit_summary(self.config, 'minimum_liquidity_score'),
            "minimum_position_count": self.config.minimum_position_count,
            "maximum_position_count": self.config.maximum_position_count,
            "correlation_cluster_threshold": _limit_summary(self.config, 'correlation_cluster_threshold'),
            "var_confidence": _limit_summary(self.config, 'var_confidence'),
            "var_horizon_days": self.config.var_horizon_days,
            "warning_utilization_pct": _limit_summary(self.config, 'warning_utilization_pct'),
            "annualization_periods": self.config.annualization_periods,
            "min_observations": self.config.min_observations,
            "min_var_observations": self.config.min_var_observations,
            "market_crash_shock": _limit_summary(self.config, 'market_crash_shock'),
            "severe_crash_shock": _limit_summary(self.config, 'severe_crash_shock'),
            "sector_shock": _limit_summary(self.config, 'sector_shock'),
            "high_beta_shock": _limit_summary(self.config, 'high_beta_shock'),
            "high_beta_tilt": _limit_summary(self.config, 'high_beta_tilt'),
            "strategy_shock": _limit_summary(self.config, 'strategy_shock'),
            "liquidity_haircut": _limit_summary(self.config, 'liquidity_haircut'),
            "correlation_spike_multiplier": _limit_summary(self.config, 'correlation_spike_multiplier'),
            "correlation_spike_vol_multiplier": str(
                self.config.correlation_spike_vol_multiplier
            ),
            "stress_sector": self.config.stress_sector,
            "mandatory_limits": list(self.config.mandatory_limits),
            "enable_adjustment": self.config.enable_adjustment,
            "disable_stress_tests": self.config.disable_stress_tests,
        }


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _limit_summary(config, name: str) -> Optional[str]:
    """One applied limit as a decimal string, or ``None`` when the rule is disabled.

    ``str(None)`` would report a disabled rule as the literal text ``"None"``, which a consumer
    reading the report would take for a limit of no value rather than for no limit at all.
    """
    value = getattr(config, name, None)
    return None if value is None else str(value)


def _metric_value(metrics: Mapping[str, RiskMetricResult], name: str) -> Optional[Decimal]:
    """The value of one metric, or ``None`` when it could not be computed."""
    result = metrics.get(name)
    return None if result is None else result.value


def _max_group(groups: Sequence, applied_only: bool = False) -> Optional[Decimal]:
    """Heaviest group weight, optionally ignoring groups whose limit is not applicable."""
    candidates = [group for group in groups if not applied_only or group.limit_applied]
    if not candidates:
        return None
    return max(group.weight_pct for group in candidates)


def _max_stress_loss(outcomes: Sequence[StressOutcome]) -> Optional[Decimal]:
    """Largest simulated loss across the scenarios that actually ran."""
    losses = [
        outcome.estimated_portfolio_loss_pct
        for outcome in outcomes
        if outcome.estimated_portfolio_loss_pct is not None
    ]
    return max(losses) if losses else None


def _worst_stress(outcomes: Sequence[StressOutcome]) -> Optional[StressOutcome]:
    """The scenario with the largest simulated loss, ties broken by scenario id."""
    evaluated = [
        outcome for outcome in outcomes if outcome.estimated_portfolio_loss_pct is not None
    ]
    if not evaluated:
        return None
    return sorted(
        evaluated, key=lambda item: (-(item.estimated_portfolio_loss_pct or ZERO), item.scenario_id)
    )[0]


#: Module-level engine bound to the default policy, matching the singleton pattern used by every
#: other service in this project. Callers with their own mandate construct their own engine.
portfolio_risk_engine = PortfolioRiskEngine()


__all__ = ["PortfolioRiskEngine", "portfolio_risk_engine"]