"""Phase 15 — portfolio risk metrics.

Computes the five headline numbers of a portfolio risk report, each measured against its own
configured limit and each carrying a plain-language explanation of where it came from:

``volatility``            annualised ``sqrt(w' * Sigma * w)`` from the covariance matrix, or the
                          annualised standard deviation of the portfolio's own return series
``beta``                  ``sum(w_i * beta_i)`` from the betas supplied on the positions, or
                          estimated against the benchmark from the return series
``max_drawdown``          worst peak-to-trough decline of the equity curve
``var_95``                historical one-period Value at Risk at the configured confidence
``expected_shortfall``    mean of the losses beyond that VaR threshold

These are **estimates from a sample**, not measurements of future risk. A metric that could not
be computed is reported ``DATA_INSUFFICIENT`` with a ``None`` value; it is never reported as
zero, because "not measured" and "no risk" are different answers and only one of them is true.
Every figure therefore also states the observation count it rests on.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from app.services.backtest.models import quantize_percent
from app.services.portfolio_risk.config import ZERO, PortfolioRiskConfig
from app.services.portfolio_risk.covariance_analyzer import covariance_summary
from app.services.portfolio_risk.evaluation import pct, status_for, utilization_pct
from app.services.portfolio_risk.models import (
    CovarianceAnalysis,
    PortfolioExposure,
    RiskMetricResult,
    RiskSnapshot,
)

#: One hundred — the scale of every percentage in this package.
_HUNDRED = Decimal("100")

#: Metric keys, also used as the report's ``risk_metrics`` mapping keys.
VOLATILITY = "volatility"
BETA = "beta"
MAX_DRAWDOWN = "max_drawdown"
VAR_95 = "var_95"
EXPECTED_SHORTFALL = "expected_shortfall"


class RiskMetricsEngine:
    """Deterministic portfolio risk metrics.

    Pure: every input is supplied, nothing is read from a clock, and no figure depends on
    dictionary iteration order.
    """

    def __init__(self, config: Optional[PortfolioRiskConfig] = None):
        self.config = config or PortfolioRiskConfig().validate()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate(
        self,
        snapshot: RiskSnapshot,
        exposure: PortfolioExposure,
        covariance: CovarianceAnalysis,
    ) -> Dict[str, RiskMetricResult]:
        """Every risk metric, keyed by name, with its limit, status and explanation."""
        return {
            VOLATILITY: self.volatility(snapshot, covariance),
            BETA: self.beta(snapshot, exposure, covariance),
            MAX_DRAWDOWN: self.max_drawdown(snapshot),
            VAR_95: self.value_at_risk(snapshot),
            EXPECTED_SHORTFALL: self.expected_shortfall(snapshot),
        }

    # ------------------------------------------------------------------
    # Volatility
    # ------------------------------------------------------------------

    def volatility(
        self,
        snapshot: RiskSnapshot,
        covariance: CovarianceAnalysis,
    ) -> RiskMetricResult:
        """Annualised portfolio volatility.

        The covariance-based figure is preferred because it is the portfolio's *own* risk
        including diversification. The standard deviation of the portfolio's return series is the
        fallback when no per-asset history was supplied. Neither is ever approximated as zero.
        """
        limit = self.config.max_portfolio_volatility
        warning = self.config.warning_utilization_pct

        if covariance.annualised_volatility_pct is not None:
            value = covariance.annualised_volatility_pct
            details = {
                "method": "covariance_matrix",
                "daily_volatility_pct": str(covariance.daily_volatility_pct),
                "annualisation_periods": self.config.annualization_periods,
                "observation_count": covariance.observation_count,
                "weight_coverage_pct": str(covariance.weight_coverage_pct),
                "reconciliation_error_pct": str(covariance.reconciliation_error_pct),
                "data_quality": covariance.data_quality,
                "source": covariance_summary(covariance, self.config.min_observations),
            }
            explanation = (
                f"Annualised portfolio volatility is {pct(value)}. "
                f"{covariance_summary(covariance, self.config.min_observations)} The estimate "
                "assumes the observed covariance between positions persists."
            )
            return self._metric(VOLATILITY, value, limit, warning, explanation, details, "max_portfolio_volatility")

        returns = [Decimal(str(value)) for value in snapshot.portfolio_returns]
        if len(returns) < max(2, self.config.min_var_observations):
            return RiskMetricResult(
                metric=VOLATILITY,
                value=None,
                limit=limit,
                utilization_pct=None,
                status=status_for(None, limit, warning),
                explanation=(
                    "Volatility cannot be computed: no per-asset return series were supplied for "
                    f"the held positions, and the portfolio's own return series has "
                    f"{len(returns)} observation(s) against a minimum of "
                    f"{self.config.min_var_observations}. This is missing data, not low risk."
                ),
                limit_name="max_portfolio_volatility",
                details={"observation_count": len(returns), "data_quality": "INSUFFICIENT"},
            )

        deviation = sample_std_dev(returns)
        factor = Decimal(str(math.sqrt(self.config.annualization_periods)))
        value = quantize_percent(deviation * factor * _HUNDRED)
        details = {
            "method": "portfolio_return_series",
            "daily_volatility_pct": str(quantize_percent(deviation * _HUNDRED)),
            "annualisation_periods": self.config.annualization_periods,
            "observation_count": len(returns),
            "source": (
                "Standard deviation of the supplied portfolio return series, annualised by the "
                "square root of time. It cannot capture diversification, because no per-asset "
                "history was supplied."
            ),
        }
        explanation = (
            f"Annualised portfolio volatility is {pct(value)}, from the standard deviation of "
            f"{len(returns)} supplied portfolio daily returns scaled by the square root of "
            f"{self.config.annualization_periods}. No per-asset history was supplied, so this "
            "figure cannot reflect diversification across positions."
        )
        return self._metric(VOLATILITY, value, limit, warning, explanation, details, "max_portfolio_volatility")

    # ------------------------------------------------------------------
    # Beta
    # ------------------------------------------------------------------

    def beta(
        self,
        snapshot: RiskSnapshot,
        exposure: PortfolioExposure,
        covariance: CovarianceAnalysis,
    ) -> RiskMetricResult:
        """Portfolio beta ``= sum(w_i * beta_i)``.

        The betas carried on the positions are used when they cover the portfolio, because they
        are what the caller already knows. Otherwise beta is estimated from the return series
        against the benchmark. If neither is available the metric is ``DATA_INSUFFICIENT`` —
        never ``0``, which would claim the portfolio moves independently of its benchmark.
        """
        limit = self.config.max_portfolio_beta
        warning = self.config.warning_utilization_pct
        benchmark = snapshot.benchmark_symbol or self.config.benchmark_symbol

        weighted: Dict[str, Decimal] = {}
        covered = ZERO
        for asset in exposure.assets:
            if asset.beta is not None:
                weighted[asset.symbol] = asset.beta
                covered += asset.weight_pct

        if weighted and covered > ZERO:
            value = quantize_percent(
                sum(
                    (
                        asset.weight_pct / _HUNDRED * asset.beta
                        for asset in exposure.assets
                        if asset.beta is not None
                    ),
                    ZERO,
                )
                * _HUNDRED
            ) / _HUNDRED
            contributions = {
                asset.symbol: str(
                    quantize_percent(asset.weight_pct / _HUNDRED * asset.beta * _HUNDRED) / _HUNDRED
                )
                for asset in exposure.assets
                if asset.beta is not None
            }
            details = {
                "method": "position_betas",
                "benchmark": benchmark,
                "individual_betas": {a.symbol: str(a.beta) for a in exposure.assets if a.beta is not None},
                "weighted_contributions": contributions,
                "weight_coverage_pct": str(quantize_percent(covered)),
            }
            explanation = (
                f"Portfolio beta is {quantize_percent(value * _HUNDRED) / _HUNDRED}, the weighted "
                f"average of the betas supplied on the positions"
                + (f" against {benchmark}" if benchmark else "")
                + f". It covers {pct(quantize_percent(covered))} of portfolio weight; positions "
                "without a beta are excluded from the figure rather than assumed to be "
                "market-neutral."
            )
            return self._metric(BETA, value, limit, warning, explanation, details, "max_portfolio_beta")

        if covariance.beta is not None:
            value = covariance.beta
            details = {
                "method": "estimated_from_returns",
                "benchmark": benchmark,
                "observation_count": covariance.observation_count,
                "individual_betas": {k: str(v) for k, v in sorted(covariance.individual_betas.items())},
                "weighted_contributions": {
                    k: str(quantize_percent(v * _HUNDRED) / _HUNDRED)
                    for k, v in sorted(covariance.individual_betas.items())
                },
                "weight_coverage_pct": str(covariance.weight_coverage_pct),
                "source": (
                    "Estimated as Cov(R_portfolio_component, R_benchmark) / Var(R_benchmark) over "
                    "the supplied return series."
                ),
            }
            explanation = (
                f"Portfolio beta is {quantize_percent(value * _HUNDRED) / _HUNDRED}, estimated from "
                f"{covariance.observation_count} overlapping daily return observations"
                + (f" against {benchmark}" if benchmark else " against the supplied benchmark series")
                + ". It is a historical sensitivity estimate, not a forward-looking one."
            )
            return self._metric(BETA, value, limit, warning, details=details, explanation=explanation,
                                limit_name="max_portfolio_beta")

        reason = (
            "no per-asset beta was supplied on the positions"
            if not weighted
            else f"per-asset betas cover only {pct(quantize_percent(covered))} of portfolio weight"
        )
        return RiskMetricResult(
            metric=BETA,
            value=None,
            limit=limit,
            utilization_pct=None,
            status=status_for(None, limit, warning),
            explanation=(
                "Beta cannot be computed: " + reason + ", and no benchmark return series was "
                "supplied to estimate it from. This is missing data, not a neutral beta."
            ),
            limit_name="max_portfolio_beta",
            details={
                "benchmark": benchmark,
                "weight_coverage_pct": str(quantize_percent(covered)) if weighted else None,
                "data_quality": "INSUFFICIENT",
            },
        )

    # ------------------------------------------------------------------
    # Drawdown
    # ------------------------------------------------------------------

    def max_drawdown(self, snapshot: RiskSnapshot) -> RiskMetricResult:
        """Worst peak-to-trough decline of the equity curve, plus the current drawdown.

        ``drawdown_t = (equity_t / running_peak_t) - 1``. This is the historical *maximum*
        drawdown: it is not the same quantity as a one-day loss, and the report keeps them
        distinct so a reader cannot confuse a bad day with a broken drawdown.
        """
        limit = self.config.max_drawdown
        warning = self.config.warning_utilization_pct

        curve = [Decimal(str(value)) for value in snapshot.equity_curve]
        source = "supplied_equity_curve"
        if len(curve) < 2 and len(snapshot.portfolio_returns) >= 2:
            curve = equity_curve_from_returns(
                [Decimal(str(value)) for value in snapshot.portfolio_returns]
            )
            source = "derived_from_portfolio_returns"

        if len(curve) < 2:
            return RiskMetricResult(
                metric=MAX_DRAWDOWN,
                value=None,
                limit=limit,
                utilization_pct=None,
                status=status_for(None, limit, warning),
                explanation=(
                    "Drawdown cannot be computed: fewer than two equity observations were "
                    "supplied and no portfolio return series was supplied to derive one from. "
                    "This is missing data, not a portfolio without drawdown."
                ),
                limit_name="max_drawdown",
                details={"observation_count": len(curve), "data_quality": "INSUFFICIENT"},
            )

        peak = curve[0]
        worst = ZERO
        current = ZERO
        worst_index = 0
        for index, equity in enumerate(curve):
            if equity > peak:
                peak = equity
            if peak > ZERO:
                drawdown = quantize_percent((peak - equity) / peak * _HUNDRED)
                if drawdown > worst:
                    worst = drawdown
                    worst_index = index
                current = drawdown

        details = {
            "observation_count": len(curve),
            "current_drawdown_pct": str(current),
            "peak_equity": str(peak),
            "final_equity": str(curve[-1]),
            "worst_drawdown_index": worst_index,
            "cumulative_return_pct": str(
                quantize_percent((curve[-1] / curve[0] - Decimal("1")) * _HUNDRED)
                if curve[0] > ZERO
                else ZERO
            ),
            "source": source,
            "method": "drawdown_t = (equity_t / running_peak_t) - 1, reported as a positive magnitude",
        }
        explanation = (
            f"Maximum historical drawdown is {pct(worst)} and the portfolio is currently "
            f"{pct(current)} below its running peak, over {len(curve)} equity observations. "
            "This is the worst peak-to-trough decline in the sample, which is not the same as "
            "the largest single-day loss."
        )
        return self._metric(MAX_DRAWDOWN, worst, limit, warning, explanation, details, "max_drawdown")

    # ------------------------------------------------------------------
    # Value at Risk and Expected Shortfall
    # ------------------------------------------------------------------

    def value_at_risk(self, snapshot: RiskSnapshot) -> RiskMetricResult:
        """Historical Value at Risk at the configured confidence and horizon.

        ``VaR_c = -quantile(returns, 1 - c)``, reported as a positive loss percentage. Beyond one
        day the figure is scaled by the square root of the horizon, the standard approximation —
        it assumes returns are independent, which is exactly what a correlated book violates, so
        the report says so rather than presenting the number as precise.
        """
        returns = [Decimal(str(value)) for value in snapshot.portfolio_returns]
        limit = self.config.risk_budget_var
        warning = self.config.warning_utilization_pct

        if len(returns) < self.config.min_var_observations:
            return RiskMetricResult(
                metric=VAR_95,
                value=None,
                limit=limit,
                utilization_pct=None,
                status=status_for(None, limit, warning),
                explanation=(
                    f"Historical VaR cannot be computed: {len(returns)} portfolio return "
                    f"observation(s) were supplied against a minimum of "
                    f"{self.config.min_var_observations}. A quantile from a sample this small "
                    "would be an artefact of the sample, so none is reported."
                ),
                limit_name="risk_budget_var",
                details={"observation_count": len(returns), "data_quality": "INSUFFICIENT"},
            )

        confidence = self.config.var_confidence
        alpha = Decimal("1") - confidence
        threshold = quantile(sorted(returns), alpha)
        loss = max(ZERO, -threshold * _HUNDRED)
        horizon = self.config.var_horizon_days
        if horizon > 1:
            loss = loss * Decimal(str(math.sqrt(horizon)))

        value = quantize_percent(loss)
        details = {
            "method": "historical_quantile",
            "confidence": str(confidence),
            "tail_probability": str(alpha),
            "horizon_days": horizon,
            "observation_count": len(returns),
            "quantile_return_pct": str(quantize_percent(threshold * _HUNDRED)),
            "scaling": (
                "square_root_of_time"
                if horizon > 1
                else "single_period"
            ),
            "source": (
                "Historical simulation: the empirical return quantile of the supplied portfolio "
                "return series. It describes this sample, not the distribution of future losses."
            ),
        }
        explanation = (
            f"Historical {pct(quantize_percent(confidence * _HUNDRED), 0)} "
            f"{horizon}-day VaR is {pct(value)}, taken from {len(returns)} portfolio return "
            "observations. It is an empirical estimate from this sample: it says nothing about "
            "losses outside the sample period."
        )
        return self._metric(VAR_95, value, limit, warning, explanation, details, "risk_budget_var")

    def expected_shortfall(self, snapshot: RiskSnapshot) -> RiskMetricResult:
        """Mean loss beyond the VaR threshold — the average of the tail, not the threshold.

        Reported alongside VaR because the tail average is the more informative of the two for a
        book this engine has just found concentrated, and because VaR alone understates what a
        breach of it actually costs.
        """
        returns = [Decimal(str(value)) for value in snapshot.portfolio_returns]
        warning = self.config.warning_utilization_pct

        if len(returns) < self.config.min_var_observations:
            return RiskMetricResult(
                metric=EXPECTED_SHORTFALL,
                value=None,
                limit=None,
                utilization_pct=None,
                status=status_for(None, None, warning),
                explanation=(
                    f"Expected Shortfall cannot be computed: {len(returns)} portfolio return "
                    f"observation(s) were supplied against a minimum of "
                    f"{self.config.min_var_observations}. A tail average over a sample this "
                    "small would describe the sample, not the risk."
                ),
                details={"observation_count": len(returns), "data_quality": "INSUFFICIENT"},
            )

        confidence = self.config.var_confidence
        alpha = Decimal("1") - confidence
        ordered = sorted(returns)
        threshold = quantile(ordered, alpha)
        # The tail is the worst ceil(alpha * n) observations rather than "everything at or below the
        # interpolated threshold". VaR is interpolated, so the threshold usually falls between two
        # observations; including the one above it would average in a loss that is less bad than
        # the level VaR names, and would pull Expected Shortfall back toward VaR.
        count = max(1, int((alpha * Decimal(len(ordered))).to_integral_value(rounding="ROUND_CEILING")))
        tail = ordered[:count]

        mean_loss = -sum(tail, ZERO) / Decimal(len(tail)) * _HUNDRED
        horizon = self.config.var_horizon_days
        if horizon > 1:
            mean_loss = mean_loss * Decimal(str(math.sqrt(horizon)))
        value = quantize_percent(max(ZERO, mean_loss))

        details = {
            "method": "historical_tail_mean",
            "confidence": str(confidence),
            "horizon_days": horizon,
            "observation_count": len(returns),
            "tail_observations": len(tail),
            "tail_probability": str(alpha),
            "threshold_return_pct": str(quantize_percent(threshold * _HUNDRED)),
            "tail_mean_return_pct": str(quantize_percent(mean_loss)),
            "tail_rule": "worst ceil(alpha * observations) returns",
            "source": (
                "Mean of the worst portfolio return observations beyond the Value at Risk tail "
                "probability."
            ),
        }
        explanation = (
            f"Expected Shortfall at {pct(quantize_percent(confidence * _HUNDRED), 0)} is "
            f"{pct(value)}, the average loss across the {len(tail)} worst observations in a "
            f"sample of {len(returns)}. It is an average of realised tail outcomes, not a bound on "
            "future losses."
        )
        return self._metric(EXPECTED_SHORTFALL, value, None, warning, explanation, details)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _metric(
        self,
        name: str,
        value: Optional[Decimal],
        limit: Optional[Decimal],
        warning: Decimal,
        explanation: str,
        details: Mapping[str, object],
        limit_name: Optional[str] = None,
    ) -> RiskMetricResult:
        """Assemble one metric with its utilization and status."""
        return RiskMetricResult(
            metric=name,
            value=value,
            limit=limit,
            utilization_pct=utilization_pct(value, limit),
            status=status_for(value, limit, warning),
            explanation=explanation,
            limit_name=limit_name,
            details=dict(details),
        )


# ---------------------------------------------------------------------------
# Pure statistics helpers
# ---------------------------------------------------------------------------


def sample_std_dev(values: Sequence[Decimal]) -> Decimal:
    """Sample standard deviation (``n - 1`` denominator), exact in :class:`Decimal`."""
    count = len(values)
    if count < 2:
        return ZERO
    mean = sum(values, ZERO) / Decimal(count)
    variance = sum(((value - mean) ** 2 for value in values), ZERO) / Decimal(count - 1)
    return variance.sqrt() if variance > ZERO else ZERO


def quantile(ordered_values: Sequence[Decimal], probability: Decimal) -> Decimal:
    """Linear-interpolated quantile of an **already sorted** sequence.

    Interpolation is used rather than picking the nearest rank, so the result moves smoothly with
    the sample instead of jumping between observations — which matters because the same engine
    reports VaR and Expected Shortfall and the two must agree on the threshold.
    """
    count = len(ordered_values)
    if count == 0:
        raise ValueError("quantile of an empty sequence is undefined")
    if count == 1:
        return ordered_values[0]

    position = Decimal(str(probability)) * Decimal(count - 1)
    lower = int(position)
    upper = min(lower + 1, count - 1)
    weight = position - Decimal(lower)
    if lower == upper:
        return ordered_values[lower]
    return ordered_values[lower] * (Decimal("1") - weight) + ordered_values[upper] * weight


def equity_curve_from_returns(returns: Sequence[Decimal], start: Decimal = Decimal("1")) -> List[Decimal]:
    """Cumulative equity implied by a return series, seeded at ``start``.

    Used only when the caller supplied returns but no equity curve: the drawdown derived this way
    is the same drawdown, computed from the same sample, and the report records which of the two
    sources was used.
    """
    equity = Decimal(str(start))
    curve = [quantize_percent(equity * _HUNDRED) / _HUNDRED]
    for value in returns:
        equity = equity * (Decimal("1") + Decimal(str(value)))
        curve.append(quantize_percent(equity * _HUNDRED) / _HUNDRED)
    return curve


__all__ = [
    "RiskMetricsEngine",
    "sample_std_dev",
    "quantile",
    "equity_curve_from_returns",
    "VOLATILITY",
    "BETA",
    "MAX_DRAWDOWN",
    "VAR_95",
    "EXPECTED_SHORTFALL",
]