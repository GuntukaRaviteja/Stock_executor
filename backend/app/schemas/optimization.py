"""Pydantic schemas for the Phase 14 strategy capital-allocation optimizer.

**Paper trading only.** Like every other schema in this package, nothing here carries a
brokerage identifier or credential, and nothing here can place a real order.

The optimizer's own dataclasses live in :mod:`app.services.portfolio.optimization` and are
converted here at the HTTP boundary, mirroring the split used by the rest of the project.
Every monetary figure is a :class:`decimal.Decimal` with field constraints that produce the
OpenAPI contract and the 422s, so an invalid capital amount or weight is refused at the edge
rather than surfacing as a broken calculation deeper in.
"""

from datetime import date as date_type
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import DEFAULT_CURRENCY
from app.schemas.portfolio import PAPER_TRADING_DISCLAIMER


class AllocationMethod(str, Enum):
    """How strategy capital is distributed."""

    EQUAL_WEIGHT = "equal_weight"
    RISK_ADJUSTED = "risk_adjusted"
    INVERSE_VOLATILITY = "inverse_volatility"
    USER_WEIGHTS = "user_weights"


class StrategyEligibility(str, Enum):
    """Whether a strategy is eligible to receive allocation."""

    ELIGIBLE = "eligible"
    INSUFFICIENT_DATA = "insufficient_data"
    EXCLUDED = "excluded"


class CorrelationStatus(str, Enum):
    """Whether the correlation matrix has enough observations."""

    SUFFICIENT = "sufficient"
    INSUFFICIENT_DATA = "insufficient_data"
    SINGLE_STRATEGY = "single_strategy"


class StrategyPerformanceSchema(BaseModel):
    """Pre-computed performance summary for one strategy."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(..., max_length=100, description="Stable strategy identifier")
    strategy_name: str = Field(..., max_length=200, description="Human-readable strategy name")
    total_return_pct: Decimal = Field(..., description="Total return over the window, as a percent")
    sharpe_ratio: Optional[Decimal] = Field(None, description="Annualised Sharpe ratio")
    max_drawdown_pct: Decimal = Field(..., description="Worst peak-to-trough drawdown, as a percent")
    annualized_volatility_pct: Optional[Decimal] = Field(
        None, description="Annualised volatility of returns, as a percent"
    )
    number_of_trades: int = Field(0, ge=0, description="Number of closed trades in the window")
    win_rate_pct: Optional[Decimal] = Field(None, description="Win rate as a percent")
    profit_factor: Optional[Decimal] = Field(None, description="Gross profit / gross loss")
    window_start: Optional[date_type] = Field(None, description="First day of the performance window")
    window_end: Optional[date_type] = Field(None, description="Last day of the performance window")


class CorrelationEntrySchema(BaseModel):
    """One entry in the correlation matrix."""

    model_config = ConfigDict(extra="forbid")

    strategy_a: str = Field(..., max_length=100)
    strategy_b: str = Field(..., max_length=100)
    correlation: Decimal = Field(..., ge=Decimal("-1"), le=Decimal("1"))


class CorrelationMatrixSchema(BaseModel):
    """Pairwise Pearson correlation between strategy returns."""

    model_config = ConfigDict(extra="forbid")

    status: CorrelationStatus
    observation_count: int = Field(..., ge=0)
    min_observations: int = Field(2, ge=2)
    matrix: List[CorrelationEntrySchema] = Field(default_factory=list)
    insufficient_pairs: List[List[str]] = Field(default_factory=list)


class StrategyWeightSchema(BaseModel):
    """A caller-supplied weight for one strategy."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(..., max_length=100)
    weight_pct: Decimal = Field(..., ge=0, le=100, description="Desired weight as a percent of capital")


class OptimizeRequest(BaseModel):
    """Request to optimise strategy-level capital allocation.

    ``strategies`` provides the performance of each candidate strategy. When omitted, the
    service layer will gather performance from historical backtests for all registered
    strategies. ``method`` selects the weighting rule; ``user_weights`` supplies explicit
    weights when ``method`` is ``USER_WEIGHTS``.
    """

    model_config = ConfigDict(extra="forbid")

    total_capital: Decimal = Field(..., gt=0, description="Total capital to distribute across strategies")
    method: AllocationMethod = Field(
        AllocationMethod.EQUAL_WEIGHT, description="Weighting method for the allocation"
    )
    strategies: Optional[List[StrategyPerformanceSchema]] = Field(
        None,
        description="Pre-computed performance per strategy; omit to gather from backtests",
    )
    user_weights: Optional[List[StrategyWeightSchema]] = Field(
        None,
        description="Explicit weights, required when method is USER_WEIGHTS",
    )
    min_weight_pct: Decimal = Field(
        Decimal("0"), ge=0, le=100, description="Minimum weight any strategy may receive"
    )
    max_weight_pct: Decimal = Field(
        Decimal("100"), gt=0, le=100, description="Maximum weight any strategy may receive"
    )
    risk_free_rate_pct: Decimal = Field(
        Decimal("0"), description="Annual risk-free rate used for Sharpe, as a percent"
    )

    @model_validator(mode="after")
    def _validate(self) -> "OptimizeRequest":
        if self.min_weight_pct > self.max_weight_pct:
            raise ValueError("min_weight_pct must not exceed max_weight_pct")
        if self.method is AllocationMethod.USER_WEIGHTS and not self.user_weights:
            raise ValueError("user_weights is required when method is 'user_weights'")
        if self.method is not AllocationMethod.USER_WEIGHTS and self.user_weights is not None:
            raise ValueError("user_weights is only valid when method is 'user_weights'")
        return self


class StrategyAssignmentSchema(BaseModel):
    """The final verdict for one strategy."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(..., max_length=100)
    strategy_name: str = Field(..., max_length=200)
    weight_pct: Decimal = Field(..., ge=0, le=100, description="Allocated weight as a percent")
    capital: Decimal = Field(..., ge=0, description="Capital allocated to this strategy")
    risk_budget: Decimal = Field(..., ge=0, description="Notional exposure budget")
    eligibility: StrategyEligibility
    reasons: List[str] = Field(default_factory=list)


class ClampedWeightSchema(BaseModel):
    """A weight that was clamped to a bound."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(..., max_length=100)
    raw: Decimal
    clamped: Decimal


class RedistributionEntrySchema(BaseModel):
    """A residual weight adjustment that makes the total sum to 100%."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(..., max_length=100)
    from_weight: Decimal
    to_weight: Decimal


class OptimizationResultSchema(BaseModel):
    """The full output of a strategy-level allocation run."""

    model_config = ConfigDict(extra="forbid")

    total_capital: Decimal
    method: AllocationMethod
    correlation_status: CorrelationStatus
    correlation: Optional[CorrelationMatrixSchema] = None
    assignments: List[StrategyAssignmentSchema]
    clamped_weights: List[ClampedWeightSchema] = Field(
        default_factory=list, description="Weights clamped by min/max bounds"
    )
    redistribution: List[RedistributionEntrySchema] = Field(
        default_factory=list, description="Residual redistributed to make weights sum to 100"
    )
    effective_min_weight_pct: Decimal
    effective_max_weight_pct: Decimal
    currency: str = Field(DEFAULT_CURRENCY, description="ISO-4217 code of the portfolio's money")
    warnings: List[str] = Field(default_factory=list)
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


__all__ = [
    "AllocationMethod",
    "StrategyEligibility",
    "CorrelationStatus",
    "StrategyPerformanceSchema",
    "CorrelationEntrySchema",
    "CorrelationMatrixSchema",
    "StrategyWeightSchema",
    "OptimizeRequest",
    "StrategyAssignmentSchema",
    "ClampedWeightSchema",
    "RedistributionEntrySchema",
    "OptimizationResultSchema",
]
