"""Pydantic schemas for Phase 15 portfolio-level risk, stress testing and exposure control.

**Paper trading only.** Like every other schema in this package, nothing here carries a
brokerage identifier or credential, and nothing here can place a real order.

These are the HTTP-boundary models for the engine in
:mod:`app.services.portfolio_risk`. The engine's own dataclasses live there and are converted
here, mirroring the split used by Phase 14: the money and decision rules stay unit-testable
without FastAPI, while the field constraints below produce the OpenAPI contract and the 422s.

Unit convention
---------------
Percentages are **percent**, never fractions: ``20`` means 20%, and ``0.75`` appears only where
the quantity is genuinely a ratio (a correlation, a VaR confidence level, a participation
ratio expressed as a fraction). Every percentage field says so in its description.

Estimation, not measurement
---------------------------
Every derived figure in these models — volatility, beta, drawdown, VaR, Expected Shortfall and
every stress outcome — is an estimate computed from the historical sample the caller supplied.
Missing data is reported as ``DATA_INSUFFICIENT``; it is never turned into a zero, because a
missing risk number is not a small risk number.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.portfolio import PAPER_TRADING_DISCLAIMER


class RiskStatus(str, Enum):
    """Status of one risk metric against its configured limit.

    PASS               — within the configured limit
    WARNING            — within the limit but at/above the configured warning utilization
    BREACH             — the configured limit is exceeded
    DATA_INSUFFICIENT  — the metric cannot be computed from the data supplied
    NOT_APPLICABLE     — the metric does not apply to this portfolio
    """

    PASS = "PASS"
    WARNING = "WARNING"
    BREACH = "BREACH"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PortfolioRiskDecision(str, Enum):
    """Final portfolio-level risk decision.

    APPROVED           — every mandatory constraint passes as supplied
    ADJUSTED           — a constraint was breached and corrected deterministically
    REJECTED           — a constraint still breaches after adjustment, or no adjustment exists
    DATA_INSUFFICIENT  — a mandatory metric could not be evaluated at all
    """

    APPROVED = "APPROVED"
    ADJUSTED = "ADJUSTED"
    REJECTED = "REJECTED"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


class StressScenarioType(str, Enum):
    """Predefined deterministic stress scenario types."""

    MARKET_CRASH_20 = "MARKET_CRASH_20"
    MARKET_CRASH_30 = "MARKET_CRASH_30"
    SECTOR_SHOCK = "SECTOR_SHOCK"
    HIGH_BETA_SHOCK = "HIGH_BETA_SHOCK"
    CORRELATION_SPIKE = "CORRELATION_SPIKE"
    LIQUIDITY_STRESS = "LIQUIDITY_STRESS"
    STRATEGY_DRAWDOWN = "STRATEGY_DRAWDOWN"


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


class PortfolioRiskPositionSchema(BaseModel):
    """One holding submitted for portfolio-level risk analysis.

    A symbol may appear once per strategy: Phase 14 hands the same instrument to several
    strategies, and the engine aggregates those lots into a single portfolio-level exposure
    rather than pretending they are independent positions.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=32, description="Instrument symbol")
    quantity: int = Field(..., ge=0, description="Shares or units held by this strategy")
    price: Decimal = Field(..., gt=0, description="Current price per unit")
    strategy: Optional[str] = Field(
        None, max_length=100, description="Strategy holding this lot; defaults to UNSPECIFIED"
    )
    sector: Optional[str] = Field(
        None, max_length=100, description="Sector classification; omitted positions are unclassified"
    )
    industry: Optional[str] = Field(None, max_length=100, description="Industry classification")
    asset_class: Optional[str] = Field(
        None, max_length=50, description="Asset class (EQUITY, ETF, CASH_LIKE, …)"
    )
    beta: Optional[Decimal] = Field(
        None, ge=Decimal("-10"), le=Decimal("10"), description="Asset beta versus the benchmark"
    )
    avg_daily_traded_value: Optional[Decimal] = Field(
        None,
        gt=0,
        description="Average daily traded value, used for participation and liquidity",
    )
    liquidity_score: Optional[Decimal] = Field(
        None, ge=0, le=100, description="Liquidity score, 0 (untradeable) to 100"
    )


class PortfolioRiskConfigSchema(BaseModel):
    """Per-request override of the portfolio risk policy.

    Every field is optional; an omitted field keeps the application default from
    :mod:`app.core.config`, and an explicit ``null`` disables that rule. These values are
    **policy parameters**, not investment advice: they are the shipped defaults of this engine,
    intended to be replaced with the deployment's own mandate.
    """

    model_config = ConfigDict(extra="forbid")

    max_single_asset_weight: Optional[Decimal] = Field(None, gt=0, le=100)
    max_sector_weight: Optional[Decimal] = Field(None, gt=0, le=100)
    max_strategy_weight: Optional[Decimal] = Field(None, gt=0, le=100)
    max_asset_class_weight: Optional[Decimal] = Field(None, gt=0, le=100)
    max_top_n_concentration: Optional[Decimal] = Field(None, gt=0, le=100)
    top_n_count: Optional[int] = Field(None, ge=1, le=1000)
    max_portfolio_volatility: Optional[Decimal] = Field(None, gt=0, le=100)
    max_portfolio_beta: Optional[Decimal] = Field(None, gt=0, le=100)
    max_drawdown: Optional[Decimal] = Field(None, gt=0, le=100)
    max_correlation_concentration: Optional[Decimal] = Field(None, gt=0, le=100)
    max_stress_loss: Optional[Decimal] = Field(None, gt=0, le=100)
    minimum_liquidity_score: Optional[Decimal] = Field(None, ge=0, le=100)
    max_participation_ratio: Optional[Decimal] = Field(None, gt=0, le=100)
    minimum_position_count: Optional[int] = Field(None, ge=1, le=1000)
    maximum_position_count: Optional[int] = Field(None, ge=1, le=1000)
    correlation_cluster_threshold: Optional[Decimal] = Field(None, ge=0, le=1)
    var_confidence: Optional[Decimal] = Field(None, gt=0, lt=1)
    var_horizon_days: Optional[int] = Field(None, ge=1, le=250)
    benchmark_symbol: Optional[str] = Field(None, max_length=32)
    warning_utilization_pct: Optional[Decimal] = Field(None, gt=0, le=100)
    min_observations: Optional[int] = Field(None, ge=2, le=10000)
    min_var_observations: Optional[int] = Field(None, ge=2, le=10000)
    market_crash_shock: Optional[Decimal] = Field(None, gt=0, le=100)
    severe_crash_shock: Optional[Decimal] = Field(None, gt=0, le=100)
    sector_shock: Optional[Decimal] = Field(None, gt=0, le=100)
    high_beta_shock: Optional[Decimal] = Field(None, gt=0, le=100)
    high_beta_tilt: Optional[Decimal] = Field(None, ge=0, le=100)
    strategy_shock: Optional[Decimal] = Field(None, gt=0, le=100)
    liquidity_haircut: Optional[Decimal] = Field(None, ge=0, le=100)
    correlation_spike_multiplier: Optional[Decimal] = Field(None, ge=0, le=1)
    correlation_spike_vol_multiplier: Optional[Decimal] = Field(None, gt=0, le=5)
    stress_sector: Optional[str] = Field(None, max_length=100)
    risk_budget_asset_concentration: Optional[Decimal] = Field(None, gt=0, le=100)
    risk_budget_sector_concentration: Optional[Decimal] = Field(None, gt=0, le=100)
    risk_budget_strategy_concentration: Optional[Decimal] = Field(None, gt=0, le=100)
    risk_budget_volatility: Optional[Decimal] = Field(None, gt=0, le=100)
    risk_budget_beta: Optional[Decimal] = Field(None, gt=0, le=100)
    risk_budget_drawdown: Optional[Decimal] = Field(None, gt=0, le=100)
    risk_budget_var: Optional[Decimal] = Field(None, gt=0, le=100)
    risk_budget_stress: Optional[Decimal] = Field(None, gt=0, le=100)
    mandatory_limits: Optional[List[str]] = Field(
        None, description="Limit names that reject rather than adjust when breached"
    )
    disable_stress_tests: Optional[bool] = None
    enable_adjustment: Optional[bool] = None


class PortfolioRiskOverridesSchema(BaseModel):
    """Per-request policy overrides for a portfolio that already exists.

    The stored route reads positions, cash, prices and sectors from the database, so its body
    carries only what the database cannot know: the policy to apply. Accepting the full request
    schema there would force a caller to resend data the engine is about to read from storage
    anyway, and would let a caller pass a different ``portfolio_value`` than the one it measures.
    """

    model_config = ConfigDict(extra="forbid")

    risk_config: Optional[PortfolioRiskConfigSchema] = Field(
        None,
        description="Limits to override for this call only; omitted limits keep their defaults",
    )


class PortfolioRiskRequestSchema(BaseModel):
    """A portfolio submitted for portfolio-level risk analysis.

    Typically the output of the Phase 14 optimizer — each strategy's positions after Phase 13
    has sized them — expressed as holdings, plus whatever market data the caller can supply.
    ``portfolio_value`` is portfolio equity (cash plus market value) and every weight in the
    report is measured against it.

    All series are **daily returns as fractions** (``0.012`` is +1.2%), oldest first. Every one
    of them is optional: what is missing is reported as ``DATA_INSUFFICIENT`` on the metrics
    that depend on it, and never as a risk of zero.
    """

    model_config = ConfigDict(extra="forbid")

    portfolio_id: Optional[int] = Field(None, ge=1, description="Persisted portfolio, when known")
    portfolio_value: Decimal = Field(..., gt=0, description="Total portfolio equity")
    cash: Decimal = Field(Decimal("0"), ge=0, description="Cash held alongside the positions")
    positions: List[PortfolioRiskPositionSchema] = Field(
        default_factory=list, description="Holdings across every strategy"
    )
    asset_returns: Optional[Dict[str, List[Decimal]]] = Field(
        None, description="Daily return fractions per symbol, for covariance and beta"
    )
    portfolio_returns: Optional[List[Decimal]] = Field(
        None, description="Daily portfolio return fractions, for VaR and Expected Shortfall"
    )
    equity_curve: Optional[List[Decimal]] = Field(
        None, description="Historical portfolio equity values, oldest first, for drawdown"
    )
    benchmark_symbol: Optional[str] = Field(None, max_length=32)
    benchmark_returns: Optional[List[Decimal]] = Field(
        None, description="Daily benchmark return fractions"
    )
    risk_config: Optional[PortfolioRiskConfigSchema] = Field(
        None, description="Per-request limit overrides merged over the application defaults"
    )

    @model_validator(mode="after")
    def _validate(self) -> "PortfolioRiskRequestSchema":
        if len(self.positions) > 200:
            raise ValueError("A portfolio of more than 200 lots is outside this engine's scope")

        seen: Dict[tuple, int] = {}
        for index, position in enumerate(self.positions):
            key = (position.symbol, position.strategy or "UNSPECIFIED")
            if key in seen:
                raise ValueError(
                    f"Duplicate position: '{position.symbol}' appears twice for strategy "
                    f"'{position.strategy or 'UNSPECIFIED'}'. Each symbol may appear at most "
                    "once per strategy."
                )
            seen[key] = index

        if not self.positions and self.cash >= self.portfolio_value:
            raise ValueError(
                "A portfolio with no positions must hold its equity in cash; received "
                "cash >= portfolio_value with nothing invested"
            )
        return self


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


class AssetExposureSchema(BaseModel):
    """Portfolio-level exposure of one instrument, aggregated across strategies."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    quantity: int = Field(..., ge=0, description="Total units held across all strategies")
    market_value: Decimal = Field(..., ge=0)
    weight_pct: Decimal = Field(..., ge=0, description="Share of portfolio equity, as a percent")
    limit_pct: Optional[Decimal] = None
    utilization_pct: Optional[Decimal] = Field(None, description="Weight / limit, as a percent")
    status: RiskStatus = RiskStatus.PASS
    strategies: List[str] = Field(default_factory=list, description="Strategies holding this symbol")
    sector: Optional[str] = None
    industry: Optional[str] = None
    asset_class: Optional[str] = None
    beta: Optional[Decimal] = None
    avg_daily_traded_value: Optional[Decimal] = None
    participation_ratio: Optional[Decimal] = Field(
        None, description="Position value / average daily traded value, as a fraction"
    )
    liquidity_score: Optional[Decimal] = None


class StrategyExposureSchema(BaseModel):
    """Exposure aggregated by strategy."""

    model_config = ConfigDict(extra="forbid")

    strategy: str
    weight_pct: Decimal = Field(..., ge=0)
    limit_pct: Optional[Decimal] = None
    utilization_pct: Optional[Decimal] = None
    status: RiskStatus = RiskStatus.PASS
    market_value: Decimal = Field(..., ge=0)
    position_count: int = Field(..., ge=0)
    symbols: List[str] = Field(default_factory=list)


class SectorExposureSchema(BaseModel):
    """Exposure aggregated by sector.

    Positions submitted without a sector are aggregated under ``UNCLASSIFIED``. That group is
    reported but never measured against the sector limit: the engine cannot know which sector
    an unclassified lot belongs to, and applying a sector cap to a bucket of unknown membership
    would be a claim it has no data for.
    """

    model_config = ConfigDict(extra="forbid")

    sector: str
    weight_pct: Decimal = Field(..., ge=0)
    limit_pct: Optional[Decimal] = None
    utilization_pct: Optional[Decimal] = None
    status: RiskStatus = RiskStatus.PASS
    market_value: Decimal = Field(..., ge=0)
    position_count: int = Field(..., ge=0)
    symbols: List[str] = Field(default_factory=list)
    limit_applied: bool = Field(True, description="False for UNCLASSIFIED, whose sector is unknown")


class AssetClassExposureSchema(BaseModel):
    """Exposure aggregated by asset class."""

    model_config = ConfigDict(extra="forbid")

    asset_class: str
    weight_pct: Decimal = Field(..., ge=0)
    limit_pct: Optional[Decimal] = None
    utilization_pct: Optional[Decimal] = None
    status: RiskStatus = RiskStatus.PASS
    market_value: Decimal = Field(..., ge=0)
    symbols: List[str] = Field(default_factory=list)


class ExposureSummarySchema(BaseModel):
    """Headline concentration figures for the whole portfolio."""

    model_config = ConfigDict(extra="forbid")

    total_positions: int = Field(..., ge=0)
    total_lots: int = Field(..., ge=0, description="Position entries before symbol aggregation")
    total_market_value: Decimal = Field(..., ge=0)
    cash: Decimal = Field(..., ge=0)
    cash_pct: Decimal = Field(..., ge=0)
    invested_pct: Decimal = Field(..., ge=0)
    largest_position_pct: Optional[Decimal] = None
    largest_position_symbol: Optional[str] = None
    top_3_concentration_pct: Decimal = Field(..., ge=0)
    top_5_concentration_pct: Decimal = Field(..., ge=0)
    top_10_concentration_pct: Decimal = Field(..., ge=0)
    top_n_concentration_pct: Decimal = Field(..., ge=0)
    top_n_count: int = Field(..., ge=1)
    unclassified_sector_pct: Decimal = Field(Decimal("0"), ge=0)


class PortfolioExposureSchema(BaseModel):
    """Complete exposure breakdown of one portfolio."""

    model_config = ConfigDict(extra="forbid")

    assets: List[AssetExposureSchema] = Field(default_factory=list)
    strategies: List[StrategyExposureSchema] = Field(default_factory=list)
    sectors: List[SectorExposureSchema] = Field(default_factory=list)
    asset_classes: List[AssetClassExposureSchema] = Field(default_factory=list)
    summary: ExposureSummarySchema


class ConcentrationMetricSchema(BaseModel):
    """One concentration measurement against its limit."""

    model_config = ConfigDict(extra="forbid")

    metric: str = Field(..., description="Machine-readable metric name")
    value_pct: Decimal = Field(..., ge=0)
    limit_pct: Optional[Decimal] = None
    utilization_pct: Optional[Decimal] = None
    status: RiskStatus
    subject: Optional[str] = Field(None, description="Symbol, sector, strategy or class")
    details: Dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Risk metrics
# ---------------------------------------------------------------------------


class RiskMetricSchema(BaseModel):
    """One portfolio risk metric with its limit, utilization and status."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    value: Optional[Decimal] = Field(
        None, description="None only when the metric could not be computed"
    )
    limit: Optional[Decimal] = None
    utilization_pct: Optional[Decimal] = None
    status: RiskStatus
    limit_name: Optional[str] = Field(None, description="Config limit this metric is measured against")
    details: Dict = Field(default_factory=dict)
    explanation: str


class RiskBudgetSchema(BaseModel):
    """Consumption of one portfolio risk budget.

    A budget is a *target* the portfolio aims at, which is why exceeding it is reported as a
    breach of the budget rather than of a hard limit: the budget is still a configured ceiling,
    not an observation.
    """

    model_config = ConfigDict(extra="forbid")

    budget: str
    metric: str
    value: Optional[Decimal] = None
    limit: Decimal = Field(..., gt=0)
    utilization_pct: Optional[Decimal] = None
    remaining: Optional[Decimal] = None
    status: RiskStatus
    explanation: str


class RiskContributionSchema(BaseModel):
    """Marginal and component contribution of one position to portfolio volatility."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    weight_pct: Decimal = Field(..., ge=0)
    marginal_contribution: Decimal = Field(
        ..., description="d(portfolio volatility)/d(weight), in volatility units"
    )
    component_contribution: Decimal = Field(..., description="Weight x marginal contribution")
    contribution_pct: Decimal = Field(
        ..., description="Component contribution as a percent of portfolio volatility"
    )


class CorrelationEntrySchema(BaseModel):
    """One entry of the pairwise correlation matrix."""

    model_config = ConfigDict(extra="forbid")

    symbol_a: str
    symbol_b: str
    correlation: Decimal = Field(..., ge=Decimal("-1"), le=Decimal("1"))


class CorrelationClusterSchema(BaseModel):
    """A group of positions that move together above the configured correlation threshold.

    This is an exposure-concentration signal, not a causal claim: a cluster says these holdings
    carry the same market risk, not that one causes the other's move.
    """

    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    symbols: List[str]
    average_correlation: Decimal = Field(..., ge=Decimal("-1"), le=Decimal("1"))
    min_pairwise_correlation: Decimal = Field(..., ge=Decimal("-1"), le=Decimal("1"))
    combined_weight_pct: Decimal = Field(..., ge=0)
    risk_concentration_pct: Optional[Decimal] = Field(
        None, description="Share of portfolio risk contributed by the cluster"
    )


class LiquidityAnalysisSchema(BaseModel):
    """Portfolio liquidity assessment based on traded-value data.

    Nothing here is invented: without average daily traded value the analysis reports
    ``LIQUIDITY_DATA_UNAVAILABLE`` and the portfolio's overall liquidity score is ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    status: RiskStatus
    positions_with_data: int = Field(..., ge=0)
    positions_analysed: int = Field(..., ge=0)
    overall_score: Optional[Decimal] = Field(None, ge=0, le=100)
    limit_pct: Optional[Decimal] = None
    utilization_pct: Optional[Decimal] = None
    max_participation_ratio: Optional[Decimal] = Field(
        None, description="Worst participation ratio observed, as a fraction"
    )
    max_participation_limit: Optional[Decimal] = None
    worst_participation_symbol: Optional[str] = None
    estimated_liquidation_days: Optional[Decimal] = Field(
        None, description="Days needed to unwind the book at the participation limit"
    )
    flagged_positions: List[Dict] = Field(default_factory=list)
    explanation: str


# ---------------------------------------------------------------------------
# Stress testing
# ---------------------------------------------------------------------------


class StressResultSchema(BaseModel):
    """Outcome of one deterministic stress simulation.

    A scenario is a **simulation on the current portfolio**, not a forecast. It applies fixed
    shocks to the holdings as they stand and reports the arithmetic consequence.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    name: str
    description: str
    scenario_type: StressScenarioType
    status: RiskStatus
    estimated_portfolio_return_pct: Optional[Decimal] = Field(
        None, description="Shocked portfolio return, as a percent of equity"
    )
    estimated_portfolio_loss_pct: Optional[Decimal] = Field(
        None, description="Positive magnitude of the loss, as a percent of equity"
    )
    worst_position_loss_pct: Optional[Decimal] = None
    worst_position_symbol: Optional[str] = None
    risk_limit_pct: Optional[Decimal] = None
    utilization_pct: Optional[Decimal] = None
    assumptions: List[str] = Field(default_factory=list)
    asset_shocks: Dict[str, Decimal] = Field(
        default_factory=dict, description="Per-symbol return shock, as a percent"
    )
    sector_shocks: Dict[str, Decimal] = Field(default_factory=dict)
    strategy_shocks: Dict[str, Decimal] = Field(default_factory=dict)
    position_impacts: Dict[str, Decimal] = Field(
        default_factory=dict, description="Per-position portfolio impact, as a percent of equity"
    )
    data_quality: Dict = Field(default_factory=dict)
    explanation: str


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


class RiskViolationSchema(BaseModel):
    """One breached or unevaluatable risk limit, with a human-readable explanation."""

    model_config = ConfigDict(extra="forbid")

    rule: str = Field(..., description="Machine-readable rule name")
    limit_name: Optional[str] = Field(None, description="Config limit that was evaluated")
    subject: Optional[str] = Field(None, description="Symbol, sector, strategy or scenario")
    limit: Optional[Decimal] = None
    observed: Optional[Decimal] = None
    utilization_pct: Optional[Decimal] = None
    excess_pct: Optional[Decimal] = Field(
        None, description="How far past the limit the observation sits, in percentage points"
    )
    is_mandatory: bool = Field(..., description="True when a surviving breach rejects the portfolio")
    status: RiskStatus
    explanation: str
    details: Dict = Field(default_factory=dict)


class RiskAdjustmentSchema(BaseModel):
    """One deterministic risk-reduction action.

    Reductions sell down a holding and leave the proceeds in cash: this engine never increases
    total exposure to compensate, so an adjusted portfolio always has the same equity as the one
    it started from and never more leverage.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="REDUCE (partial trim) or CLOSE (position removed)")
    symbol: str
    strategy: str = Field(..., description="Strategy lot the shares were sold from")
    quantity_sold: int = Field(..., gt=0)
    previous_quantity: int = Field(..., ge=0)
    new_quantity: int = Field(..., ge=0)
    previous_weight_pct: Decimal = Field(..., ge=0)
    new_weight_pct: Decimal = Field(..., ge=0)
    market_value_sold: Decimal = Field(..., ge=0)
    violated_constraint: str = Field(..., description="Rule that triggered this adjustment")
    priority: int = Field(..., ge=1, description="Order the adjustment was applied in")
    expected_risk_improvement: str
    reason: str


class RiskAdjustedPositionSchema(BaseModel):
    """One surviving lot after adjustment."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    quantity: int = Field(..., ge=0)
    price: Decimal = Field(..., gt=0)
    market_value: Decimal = Field(..., ge=0)
    weight_pct: Decimal = Field(..., ge=0)
    strategy: Optional[str] = None
    sector: Optional[str] = None


class RiskAdjustedPortfolioSchema(BaseModel):
    """The portfolio that should be handed downstream, after any adjustments."""

    model_config = ConfigDict(extra="forbid")

    portfolio_id: Optional[int] = None
    portfolio_value: Decimal = Field(..., gt=0)
    cash: Decimal = Field(..., ge=0)
    positions: List[RiskAdjustedPositionSchema] = Field(default_factory=list)
    cash_pct: Decimal = Field(..., ge=0)
    invested_pct: Decimal = Field(..., ge=0)
    exposure: Optional[PortfolioExposureSchema] = Field(
        None, description="Re-analysed exposure of the adjusted portfolio"
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class PortfolioRiskDecisionDetailSchema(BaseModel):
    """One explainable component of the final decision."""

    model_config = ConfigDict(extra="forbid")

    code: str
    explanation: str
    details: Dict = Field(default_factory=dict)


class PortfolioRiskReportSchema(BaseModel):
    """The canonical Phase 15 portfolio risk report.

    One object holds everything the analysis produced: exposures, risk metrics, risk
    contributions, correlation clusters, stress results, liquidity, violations, the adjustments
    applied, the resulting portfolio, data-quality flags and a plain-language explanation of the
    decision. Nothing is computed twice for the API: the engine produces this shape and the
    report is returned as-is.
    """

    model_config = ConfigDict(extra="forbid")

    portfolio_id: Optional[int] = None
    timestamp: Optional[datetime] = Field(
        None, description="When the report was produced; supplied by the caller, never read from a clock"
    )
    portfolio_value: Decimal = Field(..., gt=0)
    decision: PortfolioRiskDecision
    decision_explanation: str

    exposure: PortfolioExposureSchema
    concentration: List[ConcentrationMetricSchema] = Field(default_factory=list)

    risk_metrics: Dict[str, RiskMetricSchema] = Field(
        default_factory=dict, description="Keyed metric name: volatility, beta, max_drawdown, …"
    )
    risk_budgets: List[RiskBudgetSchema] = Field(default_factory=list)
    risk_contributions: List[RiskContributionSchema] = Field(default_factory=list)
    correlation_clusters: List[CorrelationClusterSchema] = Field(default_factory=list)
    correlation: Dict = Field(
        default_factory=dict, description="Correlation matrix, symbols, observations, data quality"
    )
    stress_tests: List[StressResultSchema] = Field(default_factory=list)
    liquidity: Optional[LiquidityAnalysisSchema] = None

    violations: List[RiskViolationSchema] = Field(default_factory=list)
    remaining_violations: List[RiskViolationSchema] = Field(
        default_factory=list, description="Violations still outstanding after adjustment"
    )
    adjustments: List[RiskAdjustmentSchema] = Field(default_factory=list)
    final_portfolio: Optional[RiskAdjustedPortfolioSchema] = None

    data_quality: Dict = Field(default_factory=dict)
    explanation: List[str] = Field(default_factory=list)
    decision_details: List[PortfolioRiskDecisionDetailSchema] = Field(default_factory=list)
    config_applied: Dict = Field(
        default_factory=dict, description="The policy actually applied, so a report is reproducible"
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper trading notice")


class PortfolioRiskAnalysisResponseSchema(BaseModel):
    """Response wrapper for portfolio risk analysis."""

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    data: PortfolioRiskReportSchema
    meta: Dict = Field(default_factory=dict)


class PortfolioRiskAdjustmentResponseSchema(BaseModel):
    """Response for the deterministic risk-adjustment endpoint."""

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    original_portfolio: RiskAdjustedPortfolioSchema
    adjusted_portfolio: RiskAdjustedPortfolioSchema
    risk_before: Dict[str, RiskMetricSchema] = Field(default_factory=dict)
    risk_after: Dict[str, RiskMetricSchema] = Field(default_factory=dict)
    adjustments: List[RiskAdjustmentSchema] = Field(default_factory=list)
    violations_before: List[RiskViolationSchema] = Field(default_factory=list)
    violations_after: List[RiskViolationSchema] = Field(default_factory=list)
    decision: PortfolioRiskDecision
    explanation: str
    data_quality: Dict = Field(default_factory=dict)
    meta: Dict = Field(default_factory=dict)


__all__ = [
    "RiskStatus",
    "PortfolioRiskDecision",
    "StressScenarioType",
    "PortfolioRiskPositionSchema",
    "PortfolioRiskConfigSchema",
    "PortfolioRiskOverridesSchema",
    "PortfolioRiskRequestSchema",
    "AssetExposureSchema",
    "StrategyExposureSchema",
    "SectorExposureSchema",
    "AssetClassExposureSchema",
    "ExposureSummarySchema",
    "PortfolioExposureSchema",
    "ConcentrationMetricSchema",
    "RiskMetricSchema",
    "RiskBudgetSchema",
    "RiskContributionSchema",
    "CorrelationEntrySchema",
    "CorrelationClusterSchema",
    "LiquidityAnalysisSchema",
    "StressResultSchema",
    "RiskViolationSchema",
    "RiskAdjustmentSchema",
    "RiskAdjustedPositionSchema",
    "RiskAdjustedPortfolioSchema",
    "PortfolioRiskDecisionDetailSchema",
    "PortfolioRiskReportSchema",
    "PortfolioRiskAnalysisResponseSchema",
    "PortfolioRiskAdjustmentResponseSchema",
]