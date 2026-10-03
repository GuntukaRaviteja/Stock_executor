"""Phase 15 — violation detection, deterministic adjustment and the final decision.

This is where the measurements produced by the other modules become a decision. Three things
happen here, in order:

**Detect.** Every limit is compared against its observation and any breach becomes a
:class:`~app.services.portfolio_risk.models.RiskViolation` carrying the numbers and a sentence
explaining them. A mandatory metric that could not be computed is *also* recorded — as a
data-insufficient violation — so an unmeasurable risk can never be mistaken for a satisfied one.

**Adjust.** Deterministic risk reduction, in a fixed priority order:

1. a position above the single-asset limit is trimmed to it;
2. a sector above its limit gives up the excess, largest position first;
3. a strategy above its limit gives up the excess, largest position first;
4. an asset class or top-N concentration above its limit gives up the excess;
5. a correlated cluster contributing too much risk gives up the excess, from its heaviest member;
6. the highest risk-contribution position is trimmed until volatility is within its limit;
7. the worst-stressed position gives up the excess until the stress loss is within its limit;
8. an illiquid position is trimmed until its participation ratio is acceptable.

Every reduction sells shares and **leaves the proceeds in cash**. Nothing is ever bought to
"refill" the freed capital, so an adjusted portfolio always holds the same equity and never
carries more leverage than the one it was handed. After each change the whole book is
re-evaluated; the loop stops as soon as it is compliant or when no legal reduction remains.

What cannot be adjusted
-----------------------
Some breaches describe history rather than current composition, and no current trade can change
them:

* **maximum drawdown** — a past peak-to-trough decline;
* **VaR and Expected Shortfall** — quantiles of the supplied historical sample;
* **minimum position count** — trimming to satisfy a concentration rule can conflict with it;
* **beta**, where no per-asset beta is available to steer with.

Those are reported as breaches and left standing. A portfolio whose drawdown is 30% against a 25%
limit is not repaired by selling: it is reported, and the decision says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.exceptions import RiskAdjustmentError
from app.schemas.portfolio_risk import PortfolioRiskDecision, RiskStatus
from app.services.backtest.models import quantize_money, quantize_percent
from app.services.portfolio_risk.config import ZERO, PortfolioRiskConfig
from app.services.portfolio_risk.evaluation import EPSILON, exceeds, excess_pct, pct, utilization_pct
from app.services.portfolio_risk.models import (
    ConcentrationMetric,
    CovarianceAnalysis,
    LiquidityAnalysis,
    PortfolioExposure,
    RiskAdjustment,
    RiskMetricResult,
    RiskPosition,
    RiskViolation,
    StressOutcome,
)

#: One hundred — the scale of every percentage in this package.
_HUNDRED = Decimal("100")
#: Smallest tradable quantity. Sub-share precision is not a position.
_TICK = Decimal("1")

#: Concentration rule -> the config limit it enforces.
_RULE_LIMITS = {
    "single_asset_weight": "max_single_asset_weight",
    "sector_weight": "max_sector_weight",
    "strategy_weight": "max_strategy_weight",
    "asset_class_weight": "max_asset_class_weight",
    "top_n_concentration": "max_top_n_concentration",
    "maximum_position_count": "maximum_position_count",
    "minimum_position_count": "minimum_position_count",
}

#: Risk metric key -> the config limit it enforces.
_METRIC_LIMITS = {
    "volatility": "max_portfolio_volatility",
    "beta": "max_portfolio_beta",
    "max_drawdown": "max_drawdown",
    "var_95": "risk_budget_var",
    "expected_shortfall": None,
}

#: Breaches that describe history or a structural conflict, and so cannot be adjusted away.
#: Keyed by the ``rule`` name a violation carries.
NON_ADJUSTABLE_RULES = frozenset(
    {
        "max_drawdown",
        "risk_budget_var",
        "max_portfolio_beta",
        "minimum_position_count",
    }
)

#: Why each non-adjustable rule cannot be fixed by trading.
NON_ADJUSTABLE_REASONS = {
    "max_drawdown": (
        "Maximum drawdown measures a decline that has already happened in the supplied equity "
        "curve, so no current reduction can change it."
    ),
    "risk_budget_var": (
        "Historical Value at Risk is a quantile of the supplied return sample, so no current "
        "reduction can change it."
    ),
    "max_portfolio_beta": (
        "Beta cannot be steered without per-asset betas, and none were supplied to steer with."
    ),
    "minimum_position_count": (
        "The minimum position count conflicts with the concentration rule this portfolio "
        "breaches; satisfying one would violate the other."
    ),
}


@dataclass(frozen=True)
class EvaluationBundle:
    """Everything one pass of the evaluation produces, for a given set of positions and cash."""

    exposure: PortfolioExposure
    concentration: Tuple[ConcentrationMetric, ...]
    metrics: Mapping[str, RiskMetricResult]
    covariance: CovarianceAnalysis
    stress_tests: Tuple[StressOutcome, ...]
    liquidity: LiquidityAnalysis
    violations: Tuple[RiskViolation, ...] = ()


@dataclass(frozen=True)
class AdjustmentOutcome:
    """The result of running the adjustment loop to completion."""

    positions: Tuple[RiskPosition, ...]
    cash: Decimal
    adjustments: Tuple[RiskAdjustment, ...]
    passes: int
    stopped_reason: str


class RiskController:
    """Turns measurements into violations and violations into a decision."""

    def __init__(self, config: Optional[PortfolioRiskConfig] = None):
        self.config = config or PortfolioRiskConfig().validate()

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect_violations(
        self,
        concentration: Sequence[ConcentrationMetric],
        metrics: Mapping[str, RiskMetricResult],
        stress_tests: Sequence[StressOutcome],
        liquidity: LiquidityAnalysis,
    ) -> Tuple[RiskViolation, ...]:
        """Every breach and every unevaluable mandatory metric, in a deterministic order."""
        violations: List[RiskViolation] = []

        for metric in sorted(concentration, key=lambda item: (-item.value_pct, item.metric, item.subject or "")):
            if metric.status not in (RiskStatus.BREACH, RiskStatus.DATA_INSUFFICIENT):
                continue
            limit_name = self._limit_name_for(metric.metric)
            if limit_name is None or self.config.get_limit(limit_name) is None:
                continue
            violations.append(
                self._concentration_violation(metric, limit_name)
            )

        for key in sorted(metrics):
            result = metrics[key]
            limit_name = result.limit_name or _METRIC_LIMITS.get(key)
            if limit_name is None or self.config.get_limit(limit_name) is None:
                continue
            if result.status not in (RiskStatus.BREACH, RiskStatus.DATA_INSUFFICIENT):
                continue
            violations.append(self._metric_violation(result, limit_name))

        # A scenario that could not run is a gap in coverage, not a breach. It is mandatory only
        # when *every* scenario was unevaluable, because only then was the stress limit never
        # actually tested against anything; one unrunnable scenario alongside scenarios that did
        # run is reported and named, but does not make the portfolio undecidable.
        all_unevaluable = all(
            outcome.status is RiskStatus.DATA_INSUFFICIENT for outcome in stress_tests
        )
        for outcome in stress_tests:
            if outcome.status is RiskStatus.BREACH:
                violations.append(self._stress_violation(outcome))
            elif outcome.status is RiskStatus.DATA_INSUFFICIENT and outcome.risk_limit_pct is not None:
                violations.append(
                    RiskViolation(
                        rule="max_stress_loss",
                        limit_name="max_stress_loss",
                        subject=outcome.scenario_id,
                        limit=outcome.risk_limit_pct,
                        observed=None,
                        utilization_pct=None,
                        excess_pct=None,
                        is_mandatory=all_unevaluable and self.config.is_mandatory("max_stress_loss"),
                        status=RiskStatus.DATA_INSUFFICIENT,
                        explanation=(
                            f"Stress scenario '{outcome.scenario_id}' could not be evaluated, so it "
                            "cannot be confirmed to be within the "
                            f"{pct(outcome.risk_limit_pct)} loss limit. {outcome.explanation}"
                        ),
                        details={"scenario": outcome.scenario_id},
                    )
                )

        violations.append(self._liquidity_violation(liquidity))
        return tuple(violation for violation in violations if violation is not None)

    def _limit_name_for(self, metric_name: str) -> Optional[str]:
        """Map a concentration metric name to its config limit.

        The top-N metric carries its own N in its name, so it is matched by prefix rather than
        compared exactly.
        """
        if metric_name in _RULE_LIMITS:
            return _RULE_LIMITS[metric_name]
        if metric_name.startswith("top_") and metric_name.endswith("_concentration"):
            return "max_top_n_concentration"
        return None

    def _concentration_violation(
        self,
        metric: ConcentrationMetric,
        limit_name: str,
    ) -> RiskViolation:
        """A concentration breach, explained in the units a reader thinks in."""
        limit = metric.limit_pct if metric.limit_pct is not None else self.config.get_limit(limit_name)
        is_mandatory = self.config.is_mandatory(limit_name)
        overage = excess_pct(metric.value_pct, limit)

        if metric.status is RiskStatus.DATA_INSUFFICIENT:
            explanation = (
                f"{_rule_label(metric.metric, metric.subject)} could not be evaluated against the "
                f"configured {pct(limit)} limit, so it cannot be confirmed to be within it."
            )
        else:
            explanation = (
                f"{_rule_label(metric.metric, metric.subject)} is {pct(metric.value_pct)}, exceeding "
                f"the configured {pct(limit)} limit by {pct(overage)}"
                + (f" (utilization {pct(metric.utilization_pct)} of the limit)." if metric.utilization_pct else ".")
            )
        return RiskViolation(
            rule=metric.metric,
            limit_name=limit_name,
            subject=metric.subject,
            limit=limit,
            observed=metric.value_pct,
            utilization_pct=metric.utilization_pct,
            excess_pct=overage,
            is_mandatory=is_mandatory,
            status=metric.status,
            explanation=explanation,
            details=dict(metric.details),
        )

    def _metric_violation(self, result: RiskMetricResult, limit_name: str) -> RiskViolation:
        """A risk-metric breach, or a mandatory metric that could not be evaluated."""
        is_mandatory = self.config.is_mandatory(limit_name)
        overage = excess_pct(result.value, result.limit)

        if result.status is RiskStatus.DATA_INSUFFICIENT:
            explanation = (
                f"{result.metric.replace('_', ' ')} could not be evaluated against the configured "
                f"{pct(result.limit)} limit, so it cannot be confirmed to be within it. "
                f"{result.explanation}"
            )
        else:
            explanation = (
                f"{result.metric.replace('_', ' ')} is {pct(result.value)}, exceeding the "
                f"configured {pct(result.limit)} limit by {pct(overage)}. {result.explanation}"
            )
        return RiskViolation(
            rule=result.limit_name or result.metric,
            limit_name=limit_name,
            subject=None,
            limit=result.limit,
            observed=result.value,
            utilization_pct=result.utilization_pct,
            excess_pct=overage,
            is_mandatory=is_mandatory,
            status=result.status,
            explanation=explanation,
            details=dict(result.details),
        )

    def _stress_violation(self, outcome: StressOutcome) -> RiskViolation:
        """A scenario whose simulated loss breaches the stress limit."""
        overage = excess_pct(outcome.estimated_portfolio_loss_pct, outcome.risk_limit_pct)
        return RiskViolation(
            rule="max_stress_loss",
            limit_name="max_stress_loss",
            subject=outcome.scenario_id,
            limit=outcome.risk_limit_pct,
            observed=outcome.estimated_portfolio_loss_pct,
            utilization_pct=outcome.utilization_pct,
            excess_pct=overage,
            is_mandatory=self.config.is_mandatory("max_stress_loss"),
            status=RiskStatus.BREACH,
            explanation=(
                f"Stress scenario '{outcome.scenario_id}' simulates a "
                f"{pct(outcome.estimated_portfolio_loss_pct)} loss, exceeding the configured "
                f"{pct(outcome.risk_limit_pct)} stress limit by {pct(overage)}. "
                "The scenario is a deterministic simulation of today's holdings, not a forecast."
            ),
            details={"scenario": outcome.scenario_id},
        )

    def _liquidity_violation(self, liquidity: LiquidityAnalysis) -> Optional[RiskViolation]:
        """Liquidity breaches and the explicit report of unavailable liquidity data."""
        flagged = list(liquidity.flagged_positions)
        if flagged:
            symbols = ", ".join(sorted(str(item.get("symbol")) for item in flagged))
            worst = flagged[0]
            return RiskViolation(
                rule="max_participation_ratio",
                limit_name="max_participation_ratio",
                subject=liquidity.worst_participation_symbol or symbols,
                limit=self.config.max_participation_ratio,
                observed=liquidity.max_participation_ratio,
                utilization_pct=utilization_pct(
                    liquidity.max_participation_ratio, self.config.max_participation_ratio
                ),
                excess_pct=excess_pct(
                    liquidity.max_participation_ratio, self.config.max_participation_ratio
                ),
                is_mandatory=self.config.is_mandatory("max_participation_ratio"),
                status=RiskStatus.BREACH,
                explanation=(
                    f"{len(flagged)} position(s) exceed the configured participation limit of "
                    f"{pct(self.config.max_participation_ratio)} of average daily traded value "
                    f"({symbols}); the largest is {worst.get('symbol')} at "
                    f"{pct(Decimal(str(worst.get('participation_ratio'))))}, roughly "
                    f"{worst.get('days_to_liquidate')} trading day(s) to unwind at that limit."
                ),
                details={"flagged": [dict(item) for item in flagged]},
            )

        if liquidity.status is RiskStatus.DATA_INSUFFICIENT:
            return RiskViolation(
                rule="liquidity_data_unavailable",
                limit_name=None,
                subject=None,
                limit=self.config.max_participation_ratio,
                observed=None,
                utilization_pct=None,
                excess_pct=None,
                is_mandatory=self.config.is_mandatory("max_participation_ratio"),
                status=RiskStatus.DATA_INSUFFICIENT,
                explanation=liquidity.explanation,
                details={"positions_analysed": liquidity.positions_analysed},
            )
        return None

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------

    def decide(
        self,
        violations: Sequence[RiskViolation],
        adjustments: Sequence[RiskAdjustment],
    ) -> Tuple[PortfolioRiskDecision, str, Tuple[Mapping[str, object], ...]]:
        """The final decision, its explanation and the per-reason detail behind it.

        ``DATA_INSUFFICIENT`` is checked first and on purpose: a mandatory metric that could not be
        evaluated cannot be traded around, so the honest answer is "I do not know", not
        "approved".
        """
        details: List[Mapping[str, object]] = []
        blocking = [
            violation
            for violation in violations
            if violation.is_mandatory
            and violation.status in (RiskStatus.BREACH, RiskStatus.DATA_INSUFFICIENT)
        ]
        unmeasurable = [item for item in blocking if item.status is RiskStatus.DATA_INSUFFICIENT]

        if unmeasurable:
            for violation in unmeasurable:
                details.append(
                    {
                        "code": "MANDATORY_METRIC_UNMEASURABLE",
                        "explanation": violation.explanation,
                        "details": {"rule": violation.rule, "subject": violation.subject},
                    }
                )
            decision = PortfolioRiskDecision.DATA_INSUFFICIENT
            explanation = (
                f"{len(unmeasurable)} mandatory risk metric(s) could not be evaluated from the data "
                f"supplied ({', '.join(sorted({str(item.rule) for item in unmeasurable}))}). A "
                "portfolio is not approved on the grounds that its risk could not be measured."
            )
            return decision, explanation, tuple(details)

        # A non-mandatory metric that could not be evaluated is a coverage gap, not a breach. It is
        # reported with its own detail code and named in the explanation, but it does not by itself
        # reject a portfolio whose evaluable limits are all satisfied.
        gaps = [item for item in violations if item.status is RiskStatus.DATA_INSUFFICIENT]
        breaches = [item for item in violations if item.status is RiskStatus.BREACH]

        for gap in gaps:
            details.append(
                {
                    "code": "EVALUATION_GAP",
                    "explanation": gap.explanation,
                    "details": {"rule": gap.rule, "subject": gap.subject},
                }
            )

        if not breaches:
            if adjustments:
                return (
                    PortfolioRiskDecision.ADJUSTED,
                    (
                        f"{len(adjustments)} deterministic risk reduction(s) were applied and the "
                        "resulting portfolio is within every configured limit that could be "
                        "evaluated. Reductions left their proceeds in cash, so total exposure was "
                        "not increased."
                        + (
                            f" {len(gaps)} metric(s) still could not be evaluated."
                            if gaps
                            else ""
                        )
                    ),
                    tuple(
                        [
                            {
                                "code": "ADJUSTED_TO_COMPLIANCE",
                                "explanation": adjustment.reason,
                                "details": {
                                    "symbol": adjustment.symbol,
                                    "quantity_sold": adjustment.quantity_sold,
                                    "violated_constraint": adjustment.violated_constraint,
                                },
                            }
                            for adjustment in adjustments
                        ]
                    ),
                )
            suffix = (
                f" {len(gaps)} metric(s) could not be evaluated and are listed as evaluation gaps "
                "rather than as passes."
                if gaps
                else ""
            )
            return (
                PortfolioRiskDecision.APPROVED,
                "Every configured portfolio risk limit that could be evaluated is satisfied by the "
                f"portfolio as supplied; no adjustment was needed.{suffix}",
                # The gap entries are kept alongside the summary: an approval whose detail list
                # names no unmeasured metric would hide the very caveat the explanation states.
                tuple(
                    list(details)
                    + [
                        {
                            "code": "ALL_LIMITS_SATISFIED",
                            "explanation": (
                                "No breach of any configured limit was found."
                                + (f" {len(gaps)} metric(s) could not be evaluated." if gaps else "")
                            ),
                            "details": {"evaluation_gaps": len(gaps)},
                        }
                    ]
                ),
            )

        # Any surviving breach means the portfolio is not compliant, whether the rule was mandatory
        # (which no policy can waive) or adjustable (which means no legal reduction existed).
        for violation in breaches:
            code = (
                "MANDATORY_BREACH_UNRESOLVED"
                if violation.is_mandatory
                else "NON_ADJUSTABLE_BREACH"
            )
            reason = NON_ADJUSTABLE_REASONS.get(violation.rule or "", "")
            details.append(
                {
                    "code": code,
                    "explanation": f"{violation.explanation} {reason}".strip(),
                    "details": {
                        "rule": violation.rule,
                        "subject": violation.subject,
                        "mandatory": violation.is_mandatory,
                        "adjustable": violation.rule not in NON_ADJUSTABLE_RULES,
                    },
                }
            )

        mandatory = [violation for violation in breaches if violation.is_mandatory]
        if mandatory:
            explanation = (
                f"{len(mandatory)} mandatory risk limit(s) remain breached after "
                f"{len(adjustments)} deterministic adjustment(s); the portfolio cannot be made "
                "compliant from the holdings and data supplied."
            )
        else:
            explanation = (
                f"{len(breaches)} risk limit(s) remain breached after {len(adjustments)} "
                "deterministic adjustment(s), and no legal reduction existed to correct them."
            )
        return PortfolioRiskDecision.REJECTED, explanation, tuple(details)


class RiskAdjustmentEngine:
    """Deterministic, explainable risk reduction.

    Pure: it mutates no external state, it sells no more than it holds, and every action it takes
    is reproducible from the input portfolio and the policy alone.
    """

    def __init__(self, config: Optional[PortfolioRiskConfig] = None):
        self.config = config or PortfolioRiskConfig().validate()

    def adjust(
        self,
        snapshot,
        evaluate: Callable[[Sequence[RiskPosition], Decimal], EvaluationBundle],
    ) -> AdjustmentOutcome:
        """Reduce the portfolio until it complies, or until no legal reduction remains.

        ``evaluate`` re-runs the whole measurement stack for a candidate position set, so each
        pass acts on current facts rather than on the original ones.
        """
        positions: List[RiskPosition] = list(snapshot.positions)
        cash = Decimal(str(snapshot.cash))
        portfolio_value = Decimal(str(snapshot.portfolio_value))
        adjustments: List[RiskAdjustment] = []
        reason = "No action was required: the portfolio was already within every configurable limit."

        for pass_index in range(1, self.config.max_adjustment_passes + 1):
            bundle = evaluate(positions, cash)
            action = self._next_action(bundle, portfolio_value)
            if action is None:
                reason = (
                    "The portfolio is within every limit this engine can correct by trading."
                    if not bundle.violations
                    else "No further deterministic reduction is legal; the remaining breaches are "
                    "historical or structural and are reported rather than traded around."
                )
                return AdjustmentOutcome(
                    positions=tuple(positions),
                    cash=cash,
                    adjustments=tuple(adjustments),
                    passes=pass_index - 1,
                    stopped_reason=reason,
                )

            positions, cash, adjustment = action(positions, cash, portfolio_value, pass_index)
            if adjustment is None:
                reason = (
                    "The next reduction in the priority order could not be executed at the current "
                    "prices, and no legal alternative reduction remained."
                )
                return AdjustmentOutcome(
                    positions=tuple(positions),
                    cash=cash,
                    adjustments=tuple(adjustments),
                    passes=pass_index,
                    stopped_reason=reason,
                )
            adjustments.append(adjustment)

        return AdjustmentOutcome(
            positions=tuple(positions),
            cash=cash,
            adjustments=tuple(adjustments),
            passes=self.config.max_adjustment_passes,
            stopped_reason=(
                f"Stopped after the configured maximum of {self.config.max_adjustment_passes} "
                "adjustment passes; the portfolio did not reach compliance within that budget."
            ),
        )

    # ------------------------------------------------------------------
    # Action selection
    # ------------------------------------------------------------------

    def _next_action(
        self,
        bundle: EvaluationBundle,
        portfolio_value: Decimal,
    ) -> Optional[Callable]:
        """The single next reduction to apply, chosen by the fixed priority order.

        Returns ``None`` when the portfolio is compliant or when every remaining breach is one no
        reduction can fix. The ordering is deliberate: a position is trimmed before a sector,
        because the position-level rule is the one that is always actionable, and a sector's
        excess is best removed from its largest member rather than spread across all of them.
        """
        actionable = [
            violation
            for violation in bundle.violations
            if violation.status is RiskStatus.BREACH
            and violation.rule not in NON_ADJUSTABLE_RULES
        ]
        if not actionable:
            return None

        # ``violations`` arrives sorted by descending severity, so the *first* occurrence of a rule
        # is its worst instance. Keeping the first (rather than letting a later, milder breach of
        # the same rule overwrite it) means the heaviest position is the one that gets trimmed.
        by_rule: Dict[str, RiskViolation] = {}
        for violation in actionable:
            by_rule.setdefault(violation.rule, violation)

        if "single_asset_weight" in by_rule:
            violation = by_rule["single_asset_weight"]
            return lambda positions, cash, value, priority: self._trim_symbol_to_limit(
                positions, cash, value, priority, str(violation.subject), violation
            )
        if "sector_weight" in by_rule:
            violation = by_rule["sector_weight"]
            return lambda positions, cash, value, priority: self._reduce_group_to_limit(
                positions, cash, value, priority, str(violation.subject), violation
            )
        if "strategy_weight" in by_rule:
            violation = by_rule["strategy_weight"]
            return lambda positions, cash, value, priority: self._reduce_group_to_limit(
                positions, cash, value, priority, str(violation.subject), violation
            )
        if "asset_class_weight" in by_rule:
            violation = by_rule["asset_class_weight"]
            return lambda positions, cash, value, priority: self._reduce_group_to_limit(
                positions, cash, value, priority, str(violation.subject), violation
            )
        if "top_n_concentration" in by_rule or any(
            rule.startswith("top_") for rule in by_rule
        ):
            violation = next(
                item for item in actionable if item.rule.startswith("top_")
            )
            return lambda positions, cash, value, priority: self._reduce_top_n(
                positions, cash, value, priority, bundle, violation
            )
        if "correlation_concentration" in by_rule:
            violation = by_rule["correlation_concentration"]
            return lambda positions, cash, value, priority: self._reduce_cluster(
                positions, cash, value, priority, bundle, violation
            )
        if "volatility" in by_rule:
            violation = by_rule["volatility"]
            return lambda positions, cash, value, priority: self._reduce_volatility(
                positions, cash, value, priority, bundle, violation
            )
        if "max_stress_loss" in by_rule:
            violation = by_rule["max_stress_loss"]
            return lambda positions, cash, value, priority: self._reduce_stress_loss(
                positions, cash, value, priority, bundle, violation
            )
        if "max_participation_ratio" in by_rule:
            violation = by_rule["max_participation_ratio"]
            return lambda positions, cash, value, priority: self._reduce_participation(
                positions, cash, value, priority, violation
            )
        return None

    # ------------------------------------------------------------------
    # Individual reductions
    # ------------------------------------------------------------------

    def _trim_symbol_to_limit(
        self,
        positions: List[RiskPosition],
        cash: Decimal,
        portfolio_value: Decimal,
        priority: int,
        symbol: str,
        violation: RiskViolation,
    ) -> Tuple[List[RiskPosition], Decimal, Optional[RiskAdjustment]]:
        """Trim one instrument exactly to the single-position limit."""
        limit = violation.limit
        if limit is None:
            return positions, cash, None
        lots = [position for position in positions if position.symbol == symbol]
        if not lots:
            return positions, cash, None

        target_value = limit / _HUNDRED * portfolio_value
        current_value = sum((lot.market_value for lot in lots), ZERO)
        to_sell_value = current_value - target_value
        if to_sell_value <= ZERO:
            return positions, cash, None

        return self._sell_value(
            positions,
            cash,
            portfolio_value,
            priority,
            candidates=lots,
            value_to_sell=to_sell_value,
            constraint=violation.rule,
            subject=symbol,
            limit=limit,
            improvement=(
                f"Brings {symbol} from {pct(violation.observed)} to the {pct(limit)} single-position "
                f"limit, removing {pct(violation.excess_pct)} of concentrated exposure."
            ),
        )

    def _reduce_group_to_limit(
        self,
        positions: List[RiskPosition],
        cash: Decimal,
        portfolio_value: Decimal,
        priority: int,
        group: str,
        violation: RiskViolation,
    ) -> Tuple[List[RiskPosition], Decimal, Optional[RiskAdjustment]]:
        """Remove a group's excess weight, selling its heaviest member first."""
        limit = violation.limit
        if limit is None:
            return positions, cash, None

        selector = {
            "sector_weight": lambda position: position.sector == group,
            "strategy_weight": lambda position: position.strategy_name == group,
            "asset_class_weight": lambda position: position.asset_class_name == group,
        }.get(violation.rule)
        if selector is None:
            return positions, cash, None

        members = [position for position in positions if selector(position)]
        if not members:
            return positions, cash, None
        to_sell_value = (violation.observed - limit) / _HUNDRED * portfolio_value
        if to_sell_value <= ZERO:
            return positions, cash, None

        return self._sell_value(
            positions,
            cash,
            portfolio_value,
            priority,
            candidates=members,
            value_to_sell=to_sell_value,
            constraint=violation.rule,
            subject=group,
            limit=limit,
            improvement=(
                f"Removes {pct(to_sell_value / portfolio_value * _HUNDRED)} of {group} weight, "
                f"taking the group from {pct(violation.observed)} back to the {pct(limit)} limit."
            ),
        )

    def _reduce_top_n(
        self,
        positions: List[RiskPosition],
        cash: Decimal,
        portfolio_value: Decimal,
        priority: int,
        bundle: EvaluationBundle,
        violation: RiskViolation,
    ) -> Tuple[List[RiskPosition], Decimal, Optional[RiskAdjustment]]:
        """Reduce the heaviest positions until the top-N block fits its limit.

        Selling from the largest positions is what actually moves a top-N measure: trimming small
        positions leaves the block that defines it untouched.
        """
        limit = violation.limit
        if limit is None:
            return positions, cash, None
        ordered = sorted(
            bundle.exposure.assets, key=lambda asset: (-asset.weight_pct, asset.symbol)
        )
        candidates = [position for position in positions if position.symbol in {a.symbol for a in ordered}]
        to_sell_value = (violation.observed - limit) / _HUNDRED * portfolio_value
        if to_sell_value <= ZERO or not candidates:
            return positions, cash, None
        candidates.sort(
            key=lambda position: (
                -_weight_of(bundle, position.symbol),
                position.symbol,
                position.strategy_name,
            )
        )
        return self._sell_value(
            positions,
            cash,
            portfolio_value,
            priority,
            candidates=candidates,
            value_to_sell=to_sell_value,
            constraint=violation.rule,
            subject=None,
            limit=limit,
            improvement=(
                f"Reduces the largest positions until their combined weight is within the "
                f"{pct(limit)} top-N limit, from {pct(violation.observed)}."
            ),
        )

    def _reduce_cluster(
        self,
        positions: List[RiskPosition],
        cash: Decimal,
        portfolio_value: Decimal,
        priority: int,
        bundle: EvaluationBundle,
        violation: RiskViolation,
    ) -> Tuple[List[RiskPosition], Decimal, Optional[RiskAdjustment]]:
        """Trim the heaviest member of the largest correlated cluster.

        Correlation concentration is quadratic in the weights, so the required reduction is solved
        exactly rather than guessed: for the vector ``w`` reduced along ``e_i`` to ``w - d*e_i``,
        ``(w - d e_i)' M (w - d e_i) = V - 2 d (M w)_i + d^2 M_ii``, and the smaller root of that
        quadratic is the reduction needed.
        """
        limit = violation.limit
        covariance = bundle.covariance
        clusters = covariance.clusters
        if limit is None or not clusters:
            return positions, cash, None
        target = (limit / _HUNDRED) ** 2
        members = clusters[0].symbols
        for symbol in members:
            weights = _weight_vector(bundle, covariance.symbols)
            matrix = covariance.correlation
            reduction = _quadratic_reduction(weights, matrix, covariance.symbols, symbol, target)
            if reduction is not None and reduction > ZERO:
                lots = [position for position in positions if position.symbol == symbol]
                if lots:
                    return self._sell_value(
                        positions,
                        cash,
                        portfolio_value,
                        priority,
                        candidates=lots,
                        value_to_sell=reduction * portfolio_value,
                        constraint=violation.rule,
                        subject=symbol,
                        limit=limit,
                        improvement=(
                            f"Reduces {symbol}, a member of the "
                            f"{len(members)}-position correlated cluster carrying "
                            f"{pct(clusters[0].combined_weight_pct)} of the portfolio, to bring "
                            f"correlation concentration within the {pct(limit)} limit."
                        ),
                    )
        return positions, cash, None

    def _reduce_volatility(
        self,
        positions: List[RiskPosition],
        cash: Decimal,
        portfolio_value: Decimal,
        priority: int,
        bundle: EvaluationBundle,
        violation: RiskViolation,
    ) -> Tuple[List[RiskPosition], Decimal, Optional[RiskAdjustment]]:
        """Trim the largest risk contributor until volatility is within its limit.

        Same exact quadratic solve as the cluster reduction, applied to the covariance matrix:
        portfolio variance falls as ``V - 2 d (Sigma w)_i + d^2 Sigma_ii``, and the smaller root of
        that equation is the weight that has to go. No step size is guessed, and the loop
        re-evaluates afterwards in case quantisation to whole shares left it marginally over.
        """
        limit = violation.limit
        covariance = bundle.covariance
        if limit is None or not covariance.symbols:
            return positions, cash, None
        target = (limit / _HUNDRED) ** 2
        weights = _weight_vector(bundle, covariance.symbols)
        ordered = sorted(
            covariance.risk_contributions, key=lambda item: (-item.contribution_pct, item.symbol)
        )
        for contribution in ordered:
            reduction = _quadratic_reduction(
                weights, covariance.covariance, covariance.symbols, contribution.symbol, target
            )
            if reduction is None or reduction <= ZERO:
                continue
            lots = [position for position in positions if position.symbol == contribution.symbol]
            if not lots:
                continue
            return self._sell_value(
                positions,
                cash,
                portfolio_value,
                priority,
                candidates=lots,
                value_to_sell=reduction * portfolio_value,
                constraint=violation.rule,
                subject=contribution.symbol,
                limit=limit,
                improvement=(
                    f"{contribution.symbol} contributes {pct(contribution.contribution_pct)} of "
                    f"portfolio volatility; reducing it by {pct(reduction * _HUNDRED)} of equity "
                    f"brings annualised volatility back within the {pct(limit)} limit."
                ),
            )
        return positions, cash, None

    def _reduce_stress_loss(
        self,
        positions: List[RiskPosition],
        cash: Decimal,
        portfolio_value: Decimal,
        priority: int,
        bundle: EvaluationBundle,
        violation: RiskViolation,
    ) -> Tuple[List[RiskPosition], Decimal, Optional[RiskAdjustment]]:
        """Sell out of the worst-hit position until the simulated loss fits its limit.

        Stress loss is linear in the weights, so the required sale is exact: removing value ``X``
        from a position shocked by ``s`` reduces the portfolio loss by ``X * s / equity``.
        """
        limit = violation.limit
        if limit is None:
            return positions, cash, None
        outcome = next(
            (
                item
                for item in bundle.stress_tests
                if item.scenario_id == str(violation.subject)
            ),
            None,
        )
        if outcome is None or not outcome.position_impacts:
            return positions, cash, None

        # Worst impact first, then heaviest weight, then symbol — fully deterministic.
        candidates = sorted(
            outcome.position_impacts.items(), key=lambda item: (item[1], item[0])
        )
        excess_pct_points = (outcome.estimated_portfolio_loss_pct or ZERO) - limit
        if excess_pct_points <= ZERO:
            return positions, cash, None
        weight_reduction = excess_pct_points / _HUNDRED

        for symbol, impact in candidates:
            shock = _shock_for(outcome, symbol)
            if shock is None or shock <= ZERO:
                continue
            value_to_sell = weight_reduction / shock * portfolio_value
            lots = [position for position in positions if position.symbol == symbol]
            if not lots:
                continue
            return self._sell_value(
                positions,
                cash,
                portfolio_value,
                priority,
                candidates=lots,
                value_to_sell=value_to_sell,
                constraint=violation.rule,
                subject=symbol,
                limit=limit,
                improvement=(
                    f"Scenario {outcome.scenario_id} loses {pct(excess_pct_points)} more than the "
                    f"{pct(limit)} limit allows; reducing {symbol}, which takes the full "
                    f"{pct(shock)} shock, recovers exactly that amount."
                ),
            )
        return positions, cash, None

    def _reduce_participation(
        self,
        positions: List[RiskPosition],
        cash: Decimal,
        portfolio_value: Decimal,
        priority: int,
        violation: RiskViolation,
    ) -> Tuple[List[RiskPosition], Decimal, Optional[RiskAdjustment]]:
        """Trim the worst position until its participation ratio fits the limit.

        One symbol per pass: the violation names the single worst offender, and reducing the
        others first would spread small sales across a book without fixing the position that
        actually cannot be exited.
        """
        limit = violation.limit
        symbol = violation.subject
        if limit is None or not symbol:
            return positions, cash, None
        lots = [position for position in positions if position.symbol == symbol]
        if not lots:
            return positions, cash, None

        # Reduce the position until value / ADTV == limit, i.e. value == limit * ADTV. Traded
        # value is a property of the instrument, so the first lot with one carries it.
        with_volume = [lot for lot in lots if lot.avg_daily_traded_value is not None]
        if not with_volume:
            return positions, cash, None
        traded_value = Decimal(str(with_volume[0].avg_daily_traded_value))
        to_sell_value = sum((lot.market_value for lot in lots), ZERO) - limit * traded_value
        if to_sell_value <= ZERO:
            return positions, cash, None

        return self._sell_value(
            positions,
            cash,
            portfolio_value,
            priority,
            candidates=lots,
            value_to_sell=to_sell_value,
            constraint=violation.rule,
            subject=str(symbol),
            limit=limit,
            improvement=(
                f"Reduces the position until it represents at most {pct(limit)} of average daily "
                "traded value, so it can be exited without moving the market against itself."
            ),
        )

    # ------------------------------------------------------------------
    # Selling mechanics
    # ------------------------------------------------------------------

    def _sell_value(
        self,
        positions: List[RiskPosition],
        cash: Decimal,
        portfolio_value: Decimal,
        priority: int,
        candidates: Sequence[RiskPosition],
        value_to_sell: Decimal,
        constraint: str,
        subject: Optional[str],
        limit: Optional[Decimal],
        improvement: str,
    ) -> Tuple[List[RiskPosition], Decimal, Optional[RiskAdjustment]]:
        """Sell whole shares worth ``value_to_sell``, largest holding first.

        The proceeds stay in cash — this engine never buys to replace what it sold, because doing
        so would replace one concentration with another while claiming to have reduced risk.
        Selling is floored to whole shares, and a symbol whose shares are spread across strategies
        is sold from the strategy holding the most of it, ties broken by strategy name.
        """
        ordered = sorted(
            candidates,
            key=lambda position: (-position.market_value, position.symbol, position.strategy_name),
        )
        remaining = Decimal(str(value_to_sell))
        updated: Dict[Tuple[str, str], RiskPosition] = {}
        sold_total = ZERO
        proceeds = ZERO
        first: Optional[Tuple[RiskPosition, int]] = None

        for lot in ordered:
            if remaining <= ZERO:
                break
            price = Decimal(str(lot.price))
            if price <= ZERO:
                continue
            # Round *up* to the next whole share rather than down. Flooring would leave a residue
            # that is still above the limit whenever one share is worth more than the excess — the
            # loop would then keep "correcting" the same position and never reach compliance.
            # Over-shooting by at most one share is the cost of that, and it is a real reduction
            # rather than a claim.
            wanted = (remaining / price).to_integral_value(rounding="ROUND_CEILING")
            quantity = min(lot.quantity, int(wanted))
            if quantity <= 0:
                continue
            updated[(lot.symbol, lot.strategy_name)] = RiskPosition(
                symbol=lot.symbol,
                quantity=lot.quantity - quantity,
                price=price,
                strategy=lot.strategy,
                sector=lot.sector,
                industry=lot.industry,
                asset_class=lot.asset_class,
                beta=lot.beta,
                avg_daily_traded_value=lot.avg_daily_traded_value,
                liquidity_score=lot.liquidity_score,
            )
            sold_total += quantity
            proceeds += quantize_money(Decimal(quantity) * price)
            remaining -= Decimal(quantity) * price
            if first is None:
                first = (lot, quantity)

        if not sold_total:
            return positions, cash, None

        result = [
            updated.get((position.symbol, position.strategy_name), position)
            for position in positions
        ]
        result = [position for position in result if position.quantity > 0]
        cash = quantize_money(cash + proceeds)

        source, quantity = first
        previous_weight = quantize_percent(
            sum(
                (
                    position.market_value
                    for position in positions
                    if position.symbol == source.symbol
                ),
                ZERO,
            )
            / portfolio_value
            * _HUNDRED
        )
        new_weight = quantize_percent(
            sum(
                (
                    position.market_value
                    for position in result
                    if position.symbol == source.symbol
                ),
                ZERO,
            )
            / portfolio_value
            * _HUNDRED
        )

        label = subject or source.symbol
        reason = (
            f"Reduced {source.symbol} from {previous_weight}% to {new_weight}% of portfolio equity "
            f"by selling {quantity} share(s) from strategy '{source.strategy_name}'. "
            f"{constraint.replace('_', ' ')} was breached against its configured "
            + (f"{pct(limit)} limit." if limit is not None else "limit.")
            + " Proceeds stay in cash, so portfolio equity and total leverage are unchanged."
        )
        remaining_for_symbol = sum(
            position.quantity for position in result if position.symbol == source.symbol
        )
        adjustment = RiskAdjustment(
            priority=priority,
            action="CLOSE" if remaining_for_symbol == 0 else "REDUCE",
            symbol=source.symbol,
            strategy=source.strategy_name,
            quantity_sold=int(sold_total),
            previous_quantity=source.quantity,
            new_quantity=source.quantity - int(sold_total),
            previous_weight_pct=previous_weight,
            new_weight_pct=new_weight,
            market_value_sold=quantize_money(proceeds),
            violated_constraint=constraint,
            expected_risk_improvement=improvement,
            reason=reason,
        )
        return result, cash, adjustment


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _rule_label(metric: str, subject: Optional[str]) -> str:
    """A human phrase for a concentration rule."""
    labels = {
        "single_asset_weight": f"Position '{subject}'",
        "sector_weight": f"Sector '{subject}'",
        "strategy_weight": f"Strategy '{subject}'",
        "asset_class_weight": f"Asset class '{subject}'",
        "top_n_concentration": "Top-N position concentration",
        "maximum_position_count": "Position count",
        "minimum_position_count": "Position count",
    }
    if metric in labels:
        return labels[metric]
    if metric.startswith("top_") and metric.endswith("_concentration"):
        return f"Top-{metric[len('top_'):-len('_concentration')]} position concentration"
    return metric.replace("_", " ").capitalize()


def _weight_of(bundle: EvaluationBundle, symbol: str) -> Decimal:
    """Portfolio weight of one symbol in the current evaluation."""
    for asset in bundle.exposure.assets:
        if asset.symbol == symbol:
            return asset.weight_pct
    return ZERO


def _weight_vector(
    bundle: EvaluationBundle,
    symbols: Sequence[str],
) -> Dict[str, Decimal]:
    """Weight (as a fraction) per symbol, for the quadratic solves."""
    return {symbol: _weight_of(bundle, symbol) / _HUNDRED for symbol in symbols}


def _shock_for(outcome: StressOutcome, symbol: str) -> Optional[Decimal]:
    """The shock magnitude a scenario applied to one symbol."""
    for shocks in (outcome.asset_shocks,):
        if symbol in shocks:
            return shocks[symbol]
    return None


def _quadratic_reduction(
    weights: Mapping[str, Decimal],
    matrix: Mapping[Tuple[str, str], Decimal],
    symbols: Sequence[str],
    target_symbol: str,
    target_value: Decimal,
) -> Optional[Decimal]:
    """How much weight to remove from ``target_symbol`` to bring a quadratic form to ``target``.

    For ``w' M w`` reduced along one symbol to ``w - d * e_i``:

        ``(w - d e_i)' M (w - d e_i) = V - 2 d (M w)_i + d^2 M_ii``

    which is a quadratic in ``d``. The smaller root is the reduction that lands exactly on the
    target; ``None`` means no such reduction exists (the symbol carries no exposure to the form).
    """
    if target_symbol not in weights or weights[target_symbol] <= ZERO:
        return None
    current = sum(
        (
            weights[left] * weights[right] * matrix.get((left, right), ZERO)
            for left in symbols
            for right in symbols
        ),
        ZERO,
    )
    if current <= target_value:
        return ZERO
    cross = sum(
        (matrix.get((target_symbol, other), ZERO) * weights[other] for other in symbols), ZERO
    )
    curvature = matrix.get((target_symbol, target_symbol), ZERO)
    if curvature <= ZERO:
        return None
    discriminant = cross * cross - curvature * (current - target_value)
    if discriminant < ZERO:
        return None
    root = (cross - discriminant.sqrt()) / curvature
    if root <= ZERO:
        return None
    return min(root, weights[target_symbol])


__all__ = [
    "RiskController",
    "RiskAdjustmentEngine",
    "EvaluationBundle",
    "AdjustmentOutcome",
    "NON_ADJUSTABLE_RULES",
    "NON_ADJUSTABLE_REASONS",
]