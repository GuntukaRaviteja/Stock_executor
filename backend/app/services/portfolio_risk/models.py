"""Phase 15 — engine-facing dataclasses for portfolio-level risk control.

These are the structures the Phase 15 engines read and return. They are frozen dataclasses
rather than pydantic models for the same reason Phase 13 and Phase 14 use dataclasses: the risk
arithmetic has to be unit-testable with no FastAPI, no request object and no fixture, and a
frozen record cannot be edited halfway through an analysis.

Every figure follows the project's percentage convention: ``*_pct`` is **percent**
(``20`` means 20%), and a value ending in ``ratio`` or named ``correlation``/``confidence`` is a
fraction. Nothing here is measured from a clock or a random source — a report built from the
same inputs is byte-for-byte reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Mapping, Optional, Tuple

from app.schemas.portfolio_risk import PortfolioRiskDecision, RiskStatus, StressScenarioType

#: Label used when a caller submits a lot with no strategy.
UNSPECIFIED_STRATEGY = "UNSPECIFIED"
#: Label used when a caller submits a lot with no sector classification.
UNCLASSIFIED_SECTOR = "UNCLASSIFIED"
#: Label used when a caller submits a lot with no asset class.
UNCLASSIFIED_ASSET_CLASS = "UNSPECIFIED"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskPosition:
    """One holding submitted for portfolio-level risk analysis.

    A symbol may appear once per strategy — Phase 14 hands the same instrument to several
    strategies — and the engine aggregates those lots into a single portfolio-level exposure.
    """

    symbol: str
    quantity: int
    price: Decimal
    strategy: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    asset_class: Optional[str] = None
    beta: Optional[Decimal] = None
    avg_daily_traded_value: Optional[Decimal] = None
    liquidity_score: Optional[Decimal] = None

    @property
    def strategy_name(self) -> str:
        """The strategy label, defaulting to ``UNSPECIFIED`` rather than to nothing."""
        return self.strategy or UNSPECIFIED_STRATEGY

    @property
    def sector_name(self) -> str:
        """The sector label, defaulting to ``UNCLASSIFIED``."""
        return self.sector or UNCLASSIFIED_SECTOR

    @property
    def asset_class_name(self) -> str:
        """The asset-class label, defaulting to ``UNSPECIFIED``."""
        return self.asset_class or UNCLASSIFIED_ASSET_CLASS

    @property
    def market_value(self) -> Decimal:
        """Quantity times price, unrounded."""
        return Decimal(self.quantity) * Decimal(str(self.price))


@dataclass(frozen=True)
class RiskSnapshot:
    """Everything one Phase 15 run reads.

    ``portfolio_value`` is portfolio equity — cash plus market value — and every weight in the
    report is a share of it. The three return/equity series are optional by design: what is
    absent is reported as ``DATA_INSUFFICIENT`` on the metrics that need it, never as zero.
    """

    portfolio_id: Optional[int]
    portfolio_value: Decimal
    cash: Decimal
    positions: Tuple[RiskPosition, ...] = ()
    asset_returns: Mapping[str, Tuple[Decimal, ...]] = field(default_factory=dict)
    portfolio_returns: Tuple[Decimal, ...] = ()
    equity_curve: Tuple[Decimal, ...] = ()
    benchmark_symbol: Optional[str] = None
    benchmark_returns: Tuple[Decimal, ...] = ()

    @property
    def symbols(self) -> Tuple[str, ...]:
        """Distinct symbols in the snapshot, sorted — the canonical ordering used everywhere."""
        return tuple(sorted({position.symbol for position in self.positions}))


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetExposure:
    """Portfolio-level exposure of one instrument, aggregated across strategies."""

    symbol: str
    quantity: int
    market_value: Decimal
    weight_pct: Decimal
    limit_pct: Optional[Decimal]
    utilization_pct: Optional[Decimal]
    status: RiskStatus
    strategies: Tuple[str, ...]
    sector: Optional[str]
    industry: Optional[str]
    asset_class: Optional[str]
    beta: Optional[Decimal]
    avg_daily_traded_value: Optional[Decimal]
    participation_ratio: Optional[Decimal]
    liquidity_score: Optional[Decimal]


@dataclass(frozen=True)
class GroupExposure:
    """Exposure aggregated by one dimension: strategy, sector or asset class."""

    name: str
    weight_pct: Decimal
    limit_pct: Optional[Decimal]
    utilization_pct: Optional[Decimal]
    status: RiskStatus
    market_value: Decimal
    position_count: int
    symbols: Tuple[str, ...]
    limit_applied: bool = True


@dataclass(frozen=True)
class ExposureSummary:
    """Headline concentration figures for the whole portfolio."""

    total_positions: int
    total_lots: int
    total_market_value: Decimal
    cash: Decimal
    cash_pct: Decimal
    invested_pct: Decimal
    largest_position_pct: Optional[Decimal]
    largest_position_symbol: Optional[str]
    top_3_concentration_pct: Decimal
    top_5_concentration_pct: Decimal
    top_10_concentration_pct: Decimal
    top_n_concentration_pct: Decimal
    top_n_count: int
    unclassified_sector_pct: Decimal


@dataclass(frozen=True)
class PortfolioExposure:
    """Complete exposure breakdown of one portfolio."""

    assets: Tuple[AssetExposure, ...]
    strategies: Tuple[GroupExposure, ...]
    sectors: Tuple[GroupExposure, ...]
    asset_classes: Tuple[GroupExposure, ...]
    summary: ExposureSummary

    @property
    def symbols(self) -> Tuple[str, ...]:
        """Every aggregated symbol, sorted — the canonical order for stress and clustering."""
        return tuple(asset.symbol for asset in self.assets)


@dataclass(frozen=True)
class ConcentrationMetric:
    """One concentration measurement against its limit."""

    metric: str
    value_pct: Decimal
    limit_pct: Optional[Decimal]
    utilization_pct: Optional[Decimal]
    status: RiskStatus
    subject: Optional[str] = None
    details: Mapping[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Risk metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskMetricResult:
    """One portfolio risk metric with its limit, utilization, status and explanation.

    ``value`` is ``None`` when the metric could not be computed. That is reported as
    ``DATA_INSUFFICIENT``, never as ``0`` — an unmeasurable metric is not a risk-free one.
    """

    metric: str
    value: Optional[Decimal]
    limit: Optional[Decimal]
    utilization_pct: Optional[Decimal]
    status: RiskStatus
    explanation: str
    limit_name: Optional[str] = None
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskBudgetResult:
    """Consumption of one portfolio risk budget."""

    budget: str
    metric: str
    value: Optional[Decimal]
    limit: Decimal
    utilization_pct: Optional[Decimal]
    remaining: Optional[Decimal]
    status: RiskStatus
    explanation: str


@dataclass(frozen=True)
class RiskContribution:
    """Marginal and component contribution of one position to portfolio volatility.

    ``marginal_contribution`` is ``d(sigma_p)/d(w_i)``; ``component_contribution`` is
    ``w_i * marginal_contribution``. The component contributions sum to portfolio volatility,
    which the engine verifies and reports as a reconciliation error rather than assuming.
    """

    symbol: str
    weight_pct: Decimal
    marginal_contribution: Decimal
    component_contribution: Decimal
    contribution_pct: Decimal


@dataclass(frozen=True)
class CorrelationCluster:
    """A group of positions that move together above the configured threshold.

    An exposure-concentration signal, not a causal claim.
    """

    cluster_id: str
    symbols: Tuple[str, ...]
    average_correlation: Decimal
    min_pairwise_correlation: Decimal
    combined_weight_pct: Decimal
    risk_concentration_pct: Optional[Decimal] = None


@dataclass(frozen=True)
class CovarianceAnalysis:
    """Correlation and covariance analysis of the positions that have usable return data.

    ``symbols`` is the set that *could* be analysed, sorted. Positions excluded from it are
    listed in ``excluded_symbols`` with the reason, so a partial result is never mistaken for a
    complete one.
    """

    symbols: Tuple[str, ...] = ()
    observation_count: int = 0
    correlation: Mapping[Tuple[str, str], Decimal] = field(default_factory=dict)
    covariance: Mapping[Tuple[str, str], Decimal] = field(default_factory=dict)
    daily_volatility_pct: Optional[Decimal] = None
    annualised_volatility_pct: Optional[Decimal] = None
    variance_daily: Optional[Decimal] = None
    risk_contributions: Tuple[RiskContribution, ...] = ()
    #: Sum over i != j of ``w_i * w_j * rho_ij`` — a fraction, not a percent.
    correlation_concentration: Optional[Decimal] = None
    #: Weighted mean of the off-diagonal correlations — a fraction.
    weighted_average_correlation: Optional[Decimal] = None
    #: Share of portfolio weight covered by the analysed symbols, as a percent.
    weight_coverage_pct: Optional[Decimal] = None
    clusters: Tuple[CorrelationCluster, ...] = ()
    beta: Optional[Decimal] = None
    individual_betas: Mapping[str, Decimal] = field(default_factory=dict)
    benchmark: Optional[str] = None
    reconciliation_error_pct: Optional[Decimal] = None
    data_quality: str = "INSUFFICIENT"
    excluded_symbols: Tuple[str, ...] = ()
    unavailable_pairs: Tuple[Tuple[str, str], ...] = ()

    @property
    def available(self) -> bool:
        """True when a covariance matrix was actually built."""
        return self.data_quality in ("SUFFICIENT", "PARTIAL") and bool(self.symbols)


@dataclass(frozen=True)
class LiquidityAnalysis:
    """Liquidity assessment from traded-value data, or an explicit unavailability status."""

    status: RiskStatus
    positions_with_data: int
    positions_analysed: int
    overall_score: Optional[Decimal]
    limit_pct: Optional[Decimal]
    utilization_pct: Optional[Decimal]
    max_participation_ratio: Optional[Decimal]
    max_participation_limit: Optional[Decimal]
    worst_participation_symbol: Optional[str]
    estimated_liquidation_days: Optional[Decimal]
    flagged_positions: Tuple[Mapping[str, object], ...]
    explanation: str


# ---------------------------------------------------------------------------
# Stress testing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StressScenarioSpec:
    """A deterministic stress scenario definition.

    The shocks are **magnitudes**, expressed as positive percentages of a position's value, and
    they describe a simulation on today's book. Nothing here forecasts an outcome.
    """

    scenario_id: str
    name: str
    description: str
    scenario_type: StressScenarioType
    asset_shocks: Mapping[str, Decimal] = field(default_factory=dict)
    sector_shocks: Mapping[str, Decimal] = field(default_factory=dict)
    strategy_shocks: Mapping[str, Decimal] = field(default_factory=dict)
    correlation_multiplier: Optional[Decimal] = None
    correlation_vol_multiplier: Optional[Decimal] = None
    liquidity_haircut: Optional[Decimal] = None
    assumptions: Tuple[str, ...] = ()
    risk_limit_pct: Optional[Decimal] = None


@dataclass(frozen=True)
class StressOutcome:
    """The arithmetic result of applying one scenario to the portfolio as it stands."""

    scenario_id: str
    name: str
    description: str
    scenario_type: StressScenarioType
    status: RiskStatus
    estimated_portfolio_return_pct: Optional[Decimal]
    estimated_portfolio_loss_pct: Optional[Decimal]
    worst_position_loss_pct: Optional[Decimal]
    worst_position_symbol: Optional[str]
    risk_limit_pct: Optional[Decimal]
    utilization_pct: Optional[Decimal]
    position_impacts: Mapping[str, Decimal]
    data_quality: Mapping[str, object]
    explanation: str
    assumptions: Tuple[str, ...] = ()
    asset_shocks: Mapping[str, Decimal] = field(default_factory=dict)
    sector_shocks: Mapping[str, Decimal] = field(default_factory=dict)
    strategy_shocks: Mapping[str, Decimal] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskViolation:
    """One breached limit, or one mandatory metric that could not be evaluated.

    Every violation carries an ``explanation`` written in sentences with the actual numbers in
    it, because a risk control nobody can read is not auditable.
    """

    rule: str
    limit_name: Optional[str]
    subject: Optional[str]
    limit: Optional[Decimal]
    observed: Optional[Decimal]
    utilization_pct: Optional[Decimal]
    excess_pct: Optional[Decimal]
    is_mandatory: bool
    status: RiskStatus
    explanation: str
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskAdjustment:
    """One deterministic risk-reduction action.

    A reduction sells shares and leaves the proceeds in cash. This engine never adds exposure
    elsewhere to compensate, so an adjusted portfolio always holds the same equity and never
    carries more leverage than the one it was handed.
    """

    priority: int
    action: str
    symbol: str
    strategy: str
    quantity_sold: int
    previous_quantity: int
    new_quantity: int
    previous_weight_pct: Decimal
    new_weight_pct: Decimal
    market_value_sold: Decimal
    violated_constraint: str
    expected_risk_improvement: str
    reason: str


@dataclass(frozen=True)
class AdjustedPortfolio:
    """The portfolio handed downstream once the risk controls have had their say."""

    portfolio_id: Optional[int]
    portfolio_value: Decimal
    cash: Decimal
    positions: Tuple[RiskPosition, ...]
    exposure: PortfolioExposure


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioRiskAssessment:
    """The complete result of one Phase 15 analysis."""

    portfolio_id: Optional[int]
    portfolio_value: Decimal
    decision: PortfolioRiskDecision
    decision_explanation: str
    exposure: PortfolioExposure
    concentration: Tuple[ConcentrationMetric, ...]
    risk_metrics: Mapping[str, RiskMetricResult]
    risk_budgets: Tuple[RiskBudgetResult, ...]
    risk_contributions: Tuple[RiskContribution, ...]
    correlation_clusters: Tuple[CorrelationCluster, ...]
    covariance: CovarianceAnalysis
    stress_tests: Tuple[StressOutcome, ...]
    liquidity: LiquidityAnalysis
    violations: Tuple[RiskViolation, ...]
    remaining_violations: Tuple[RiskViolation, ...]
    adjustments: Tuple[RiskAdjustment, ...]
    final_portfolio: Optional[AdjustedPortfolio]
    data_quality: Mapping[str, object]
    explanation: Tuple[str, ...]
    decision_details: Tuple[Mapping[str, object], ...]
    config_applied: Mapping[str, object]


__all__ = [
    "UNSPECIFIED_STRATEGY",
    "UNCLASSIFIED_SECTOR",
    "UNCLASSIFIED_ASSET_CLASS",
    "PortfolioRiskDecision",
    "RiskStatus",
    "StressScenarioType",
    "RiskPosition",
    "RiskSnapshot",
    "AssetExposure",
    "GroupExposure",
    "ExposureSummary",
    "PortfolioExposure",
    "ConcentrationMetric",
    "RiskMetricResult",
    "RiskBudgetResult",
    "RiskContribution",
    "CorrelationCluster",
    "CovarianceAnalysis",
    "LiquidityAnalysis",
    "StressScenarioSpec",
    "StressOutcome",
    "RiskViolation",
    "RiskAdjustment",
    "AdjustedPortfolio",
    "PortfolioRiskAssessment",
]