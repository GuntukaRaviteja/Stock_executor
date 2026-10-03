"""Pydantic schemas for the Phase 13 portfolio & risk management engine.

**Paper trading only.** Like every other schema in this package, nothing here carries a
brokerage identifier or credential, and nothing here can place a real order.

The engine's own dataclasses live in
:mod:`app.services.portfolio.models` and are converted here at the HTTP boundary, so no
internal database model or frozen dataclass is ever serialised directly. The split mirrors the
one the rest of the project already uses:

===================  =========================================================
request models       ``PortfolioRiskPolicySchema``, ``AllocateRequest``,
                     ``PositionSizeRequest``, ``PortfolioBacktestRequest``
response models      ``PortfolioRiskStateResponse``, ``AllocationResponse``,
                     ``PositionSizeResponse``, ``PortfolioBacktestResponse``
===================  =========================================================

Every monetary figure is a :class:`decimal.Decimal` with field constraints that produce the
OpenAPI contract and the 422s, so an invalid policy, price or stop is refused at the edge
rather than surfacing as a broken calculation deeper in.
"""

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import DEFAULT_CURRENCY
from app.schemas.portfolio import (
    PAPER_TRADING_DISCLAIMER,
    DecisionAction,
    OrderSide,
    PortfolioStatus,
    PositionSizingMethod,
    RejectionCode,
    RiskSeverity,
)

#: A percent value: strictly positive, at most 100. Percentages are in percent throughout the
#: portfolio package — ``20`` means 20% — so the range is the literal one.
_PCT = {"gt": 0, "le": 100}
#: A money amount: non-negative, two decimal places.
_MONEY = {"ge": 0}


class PortfolioRiskFlag(str, Enum):
    """Structured, machine-readable risk state.

    A flag answers "is this portfolio currently outside a configured limit?" — a statement about
    the portfolio as it stands. It is deliberately different from
    :class:`~app.schemas.portfolio.RejectionCode`, which answers "was this proposed trade
    refused, and by which rule?". A portfolio can be over-exposed with no trade pending, and a
    trade can be refused by a rule the portfolio is not currently outside of, so the two
    vocabularies cannot be merged.

    The evaluation order is fixed — concentration, sector, position count, exposure, cash,
    total risk, drawdown — so two reads of the same portfolio always report the same flags in
    the same sequence.
    """

    POSITION_CONCENTRATION_BREACHED = "POSITION_CONCENTRATION_BREACHED"
    SECTOR_CONCENTRATION_BREACHED = "SECTOR_CONCENTRATION_BREACHED"
    POSITION_LIMIT_BREACHED = "POSITION_LIMIT_BREACHED"
    EXPOSURE_LIMIT_BREACHED = "EXPOSURE_LIMIT_BREACHED"
    CASH_RESERVE_BREACHED = "CASH_RESERVE_BREACHED"
    TOTAL_RISK_LIMIT_BREACHED = "TOTAL_RISK_LIMIT_BREACHED"
    DRAWDOWN_LIMIT_BREACHED = "DRAWDOWN_LIMIT_BREACHED"


class BindingConstraint(str, Enum):
    """Which bound set a proposed position's quantity."""

    RISK = "risk"
    POSITION_WEIGHT = "position_weight"
    CASH = "cash"
    UNAFFORDABLE = "unaffordable"


class PortfolioRiskPolicySchema(BaseModel):
    """The complete set of portfolio risk limits, as sent and accepted over HTTP.

    Every field is optional-with-a-default so a caller can override **one** limit without
    restating the rest, and the engine merges the result with the portfolio's stored policy.
    Sending a value the engine cannot act on is refused by the field constraints, which is why a
    negative percentage or a stop of 100% can never reach a calculation.
    """

    model_config = ConfigDict(extra="forbid")

    max_position_weight_pct: Decimal = Field(
        Decimal("20"),
        gt=0,
        le=100,
        description="Maximum share of equity a single position may represent",
    )
    max_exposure_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Maximum share of equity invested across all positions; null disables it",
    )
    max_risk_per_trade_pct: Decimal = Field(
        Decimal("1"),
        gt=0,
        le=100,
        description="Maximum share of equity one trade may risk to its stop",
    )
    max_drawdown_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Maximum tolerated decline from peak equity; breaching it blocks new buys",
    )
    max_positions: Optional[int] = Field(
        None, ge=1, description="Maximum simultaneously open positions; null disables it"
    )
    min_cash_reserve_pct: Decimal = Field(
        Decimal("0"),
        ge=0,
        le=100,
        description="Share of equity that must stay in cash after a buy; 0 means no reserve",
    )
    max_sector_weight_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Maximum share of equity one sector may represent",
    )
    max_total_risk_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Maximum share of equity at risk to the stops of all open positions",
    )
    stop_loss_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        lt=100,
        description=(
            "Stop-loss trigger, in percent below a position's average entry price. Required for "
            "RISK_AND_WEIGHT sizing: without a stop there is no per-share risk."
        ),
    )
    min_trade_value: Optional[Decimal] = Field(
        None, description="Smallest gross trade value the engine will propose"
    )
    commission_rate: Decimal = Field(
        Decimal("0.001"), description="Simulated commission as a fraction of gross value"
    )
    min_fee: Decimal = Field(Decimal("0"), description="Floor charged per side")
    slippage_rate: Decimal = Field(
        Decimal("0"), ge=0, lt=1, description="Adverse slippage applied to the fill price"
    )

    @model_validator(mode="after")
    def _check_ranges(self) -> "PortfolioRiskPolicySchema":
        """Reject a policy the engine cannot act on, naming the offending field.

        Refusing rather than clamping is the point: a risk policy repaired behind the caller's
        back produces a portfolio nobody designed.
        """
        for name in (
            "max_position_weight_pct",
            "max_risk_per_trade_pct",
        ):
            value = getattr(self, name)
            if not (Decimal("0") < value <= Decimal("100")):
                raise ValueError(f"{name} must be in (0, 100]")
        for name in (
            "max_exposure_pct",
            "max_drawdown_pct",
            "max_sector_weight_pct",
            "max_total_risk_pct",
        ):
            value = getattr(self, name)
            if value is not None and not (Decimal("0") < value <= Decimal("100")):
                raise ValueError(f"{name} must be in (0, 100] when set")
        if self.stop_loss_pct is not None and not (
            Decimal("0") < self.stop_loss_pct < Decimal("100")
        ):
            raise ValueError("stop_loss_pct must be in (0, 100) when set")
        if self.min_trade_value is not None and self.min_trade_value <= 0:
            raise ValueError("min_trade_value must be greater than zero when set")
        if self.commission_rate < 0:
            raise ValueError("commission_rate must not be negative")
        if self.min_fee < 0:
            raise ValueError("min_fee must not be negative")
        return self


class AllocationCandidateInput(BaseModel):
    """One candidate security offered to the allocation engine."""

    model_config = ConfigDict(extra="forbid")

    stock_id: int = Field(..., ge=1, description="Internal stock ID")
    entry_price: Optional[Decimal] = Field(
        None,
        gt=0,
        description="Price to size against; defaults to the latest stored close",
    )
    stop_price: Optional[Decimal] = Field(
        None, gt=0, description="Stop loss for this trade; must be strictly below the entry"
    )
    composite_score: Optional[int] = Field(
        None, description="Phase 7 signal score; higher is better and is the ranking key"
    )
    symbol: Optional[str] = Field(None, max_length=50, description="Ticker symbol, when known")
    sector: Optional[str] = Field(
        None, max_length=100, description="Sector or industry, for the concentration limit"
    )
    side: OrderSide = Field(
        OrderSide.BUY,
        description=(
            "Only BUY is supported. A short is refused explicitly rather than treated as a buy, "
            "because a silent buy in place of a requested short would describe the opposite "
            "trade while looking correct."
        ),
    )


class AllocateRequest(BaseModel):
    """Evaluate — and optionally apply — one or more simultaneous candidate signals.

    The same request shape covers the single-trade case (one candidate) and the rebalance case
    (several), because they are the same computation over a list of one or more: collect,
    validate, rank, allocate sequentially, enforce the portfolio limits after every allocation.
    """

    model_config = ConfigDict(extra="forbid")

    candidates: List[AllocationCandidateInput] = Field(
        ...,
        min_length=1,
        description="One or more candidate signals to evaluate, in any order",
    )
    prices: Optional[Dict[int, Decimal]] = Field(
        None,
        description="stock_id -> price overrides used to mark the portfolio and resolve entries",
    )
    policy: Optional[PortfolioRiskPolicySchema] = Field(
        None,
        description=(
            "Complete replacement policy. Omit it to use the portfolio's stored limits merged "
            "with the application defaults."
        ),
    )
    sizing_method: PositionSizingMethod = Field(
        PositionSizingMethod.RISK_AND_WEIGHT,
        description="RISK_AND_WEIGHT (risk budget and position weight) or MAX_POSITION_CAP",
    )
    as_of: Optional[datetime] = Field(
        None, description="Decision timestamp; defaults to now. Recorded, never read from."
    )
    apply: bool = Field(
        False,
        description=(
            "Submit the approved, unblocked allocations as simulated orders. Default false: an "
            "allocation is a recommendation until it is asked for."
        ),
    )


class PositionSizeRequest(BaseModel):
    """Size one candidate trade against the portfolio's stored capital and risk policy."""

    model_config = ConfigDict(extra="forbid")

    stock_id: int = Field(..., ge=1, description="Internal stock ID")
    entry_price: Optional[Decimal] = Field(
        None, gt=0, description="Price to size against; defaults to the latest stored close"
    )
    stop_price: Optional[Decimal] = Field(
        None,
        gt=0,
        description="Stop loss; required for RISK_AND_WEIGHT sizing and must be below the entry",
    )
    side: OrderSide = Field(
        OrderSide.BUY, description="Only BUY is supported; a short is refused explicitly"
    )
    method: PositionSizingMethod = Field(
        PositionSizingMethod.RISK_AND_WEIGHT,
        description="RISK_AND_WEIGHT (risk budget and position weight) or MAX_POSITION_CAP",
    )
    policy: Optional[PortfolioRiskPolicySchema] = Field(
        None, description="Complete replacement policy; omit to use the stored limits"
    )


# ----------------------------------------------------------------------
# Responses
# ----------------------------------------------------------------------


class PositionSizeResponse(BaseModel):
    """One sizing decision, with every intermediate figure that produced it."""

    portfolio_id: int
    stock_id: int
    method: PositionSizingMethod
    entry_price: Decimal
    stop_price: Optional[Decimal]
    quantity: int = Field(..., description="Whole shares; never negative, 0 when unaffordable")
    equity: Decimal
    risk_budget: Decimal = Field(..., description="equity * max_risk_per_trade_pct / 100")
    risk_per_share: Optional[Decimal] = Field(..., description="entry_price - stop_price, for a long")
    quantity_by_risk: int = Field(..., description="floor(risk_budget / risk_per_share)")
    max_position_value: Decimal = Field(..., description="equity * max_position_weight_pct / 100")
    quantity_by_capital: int = Field(..., description="floor(max_position_value / entry_price)")
    quantity_by_cash: int = Field(..., description="Whole shares the available cash can fund")
    capital_required: Decimal = Field(..., description="Trade value including fees")
    fees: Decimal
    risk_amount: Decimal = Field(..., description="Loss at the stop; never negative")
    portfolio_weight_pct: Optional[Decimal]
    binding_constraint: BindingConstraint
    explanation: str
    policy: PortfolioRiskPolicySchema
    currency: str = Field(DEFAULT_CURRENCY, description="ISO-4217 code of the portfolio's money")
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class AllocationRejectionResponse(BaseModel):
    """One reason an allocation was refused, with the numbers behind it."""

    code: RejectionCode = Field(..., description="Stable, machine-readable reason")
    reason: str = Field(..., description="Prose carrying the numbers behind the refusal")


class AllocationDecisionResponse(BaseModel):
    """The verdict for one candidate: what to do, and why."""

    stock_id: int
    symbol: Optional[str] = None
    sector: Optional[str] = None
    action: DecisionAction = Field(
        DecisionAction.BUY, description="BUY when approved; HOLD when the candidate was refused"
    )
    approved: bool
    quantity: int = Field(..., description="Whole shares proposed; 0 when refused")
    entry_price: Decimal
    stop_price: Optional[Decimal] = None
    capital_required: Decimal = Field(..., description="Cash the trade commits, fees included")
    risk_amount: Decimal = Field(..., description="Loss at the stop; never negative")
    portfolio_weight_pct: Optional[Decimal] = Field(
        None, description="Resulting position weight as a percent of equity"
    )
    resulting_exposure_pct: Optional[Decimal] = Field(
        None, description="Total invested as a percent of equity after this allocation"
    )
    remaining_cash: Decimal = Field(..., description="Cash left after this allocation")
    binding_constraint: Optional[BindingConstraint] = None
    composite_score: int = Field(0, description="Phase 7 signal score used for ranking")
    rejection_reasons: List[AllocationRejectionResponse] = Field(
        default_factory=list, description="Every rule this candidate breaks, in evaluation order"
    )
    explanation: List[str] = Field(default_factory=list, description="Prose with the arithmetic")
    applied: bool = Field(False, description="Whether the decision was submitted as an order")
    order_id: Optional[int] = Field(None, description="Order created when applied")
    trade_id: Optional[int] = Field(None, description="Trade created when applied")


class AllocationResponse(BaseModel):
    """The whole outcome of one allocation run over an ordered candidate list."""

    portfolio_id: int
    status: PortfolioStatus
    as_of: datetime
    currency: str = Field(DEFAULT_CURRENCY, description="ISO-4217 code of the portfolio's money")
    equity: Decimal
    cash_before: Decimal
    cash_after: Decimal
    invested_before: Decimal
    invested_after: Decimal
    exposure_before_pct: Optional[Decimal]
    exposure_after_pct: Optional[Decimal]
    position_count_before: int
    position_count_after: int
    candidates_considered: int
    approved_count: int
    rejected_count: int
    total_capital_committed: Decimal
    total_risk_committed: Decimal
    stop_reason: Optional[str] = Field(
        None,
        description=(
            "Why the run stopped early — capital exhausted, exposure ceiling reached, position "
            "count reached — or null when every candidate was considered"
        ),
    )
    policy: PortfolioRiskPolicySchema
    decisions: List[AllocationDecisionResponse]
    warnings: List[str] = Field(default_factory=list)
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class RiskFlagDetailResponse(BaseModel):
    """The numbers behind one raised risk flag."""

    flag: PortfolioRiskFlag
    severity: RiskSeverity
    rule: str = Field(..., description="Stable configuration key the flag was measured against")
    reason: str = Field(..., description="Prose carrying the numbers")
    limit: Optional[Decimal] = None
    observed: Optional[Decimal] = None


class PositionExposureResponse(BaseModel):
    """One open position, as the risk engine reads it."""

    stock_id: int
    symbol: Optional[str] = None
    sector: Optional[str] = None
    quantity: int
    average_entry_price: Decimal
    current_price: Decimal
    market_value: Decimal
    cost_basis: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_pct: Optional[Decimal] = Field(
        None, description="(current_price - average_entry_price) / average_entry_price * 100"
    )
    portfolio_weight_pct: Optional[Decimal]
    stop_price: Optional[Decimal] = None
    risk_amount: Decimal = Field(..., description="Loss at the stop; never negative")


class SectorWeightResponse(BaseModel):
    """One sector's share of portfolio equity.

    Positions whose sector is unknown are omitted entirely rather than collected into an
    "unknown" bucket: an invented bucket would report a weight belonging to no real sector and
    would let a concentrated portfolio pass the sector check.
    """

    sector: str = Field(..., description="Sector name as recorded on the company profile")
    weight_pct: Decimal = Field(..., description="Sector market value / equity * 100")


class PortfolioRiskStateResponse(BaseModel):
    """Where the portfolio stands against its policy, right now.

    A *state*, not a verdict on a trade: it is safe to read at any time and mutates nothing.
    """

    portfolio_id: int
    status: PortfolioStatus
    as_of: datetime
    currency: str = Field(DEFAULT_CURRENCY, description="ISO-4217 code of the portfolio's money")
    equity: Decimal
    initial_capital: Decimal
    cash: Decimal
    market_value: Decimal
    invested_capital: Decimal
    exposure_pct: Optional[Decimal] = Field(
        None, description="sum(position market value) / equity * 100"
    )
    cash_weight_pct: Optional[Decimal] = Field(None, description="cash / equity * 100")
    invested_weight_pct: Optional[Decimal] = None
    available_capital: Decimal = Field(
        ..., description="Cash that is available to open a new position right now"
    )
    position_count: int
    largest_position_weight_pct: Optional[Decimal] = None
    largest_position_stock_id: Optional[int] = None
    total_risk_amount: Decimal = Field(..., description="Sum of every position's risk to its stop")
    total_risk_pct: Optional[Decimal] = None
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    portfolio_return_pct: Decimal
    peak_equity: Decimal
    current_drawdown_pct: Decimal = Field(
        ..., description="(peak_equity - equity) / peak_equity * 100; zero at a peak"
    )
    max_drawdown_pct: Decimal
    max_drawdown: Decimal
    cash_reserve_required: Decimal
    sector_weights: List[SectorWeightResponse] = Field(
        default_factory=list,
        description="Weight of each sector that is known, sorted by sector name",
    )
    breached: bool = Field(..., description="True when at least one configured limit is breached")
    flags: List[PortfolioRiskFlag] = Field(
        default_factory=list, description="Raised flags, in the fixed evaluation order"
    )
    flag_details: List[RiskFlagDetailResponse] = Field(default_factory=list)
    positions: List[PositionExposureResponse] = Field(default_factory=list)
    policy: PortfolioRiskPolicySchema
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class BacktestCandidateInput(BaseModel):
    """One stock to replay inside a portfolio backtest."""

    model_config = ConfigDict(extra="forbid")

    stock_id: int = Field(..., ge=1, description="Internal stock ID")
    stop_price: Optional[Decimal] = Field(
        None, gt=0, description="Fixed stop for this name; overrides the policy's stop_loss_pct"
    )


class PortfolioBacktestRequest(BaseModel):
    """Replay several stocks chronologically against one simulated portfolio.

    The portfolio backtest is the existing Phase 8 engine's answer applied at *portfolio* level:
    same bars, same point-in-time indicator context, same cost model, same chronological
    stepping — with the Phase 13 sizing and limit rules in place of a single-stock position size.
    """

    model_config = ConfigDict(extra="forbid")

    candidates: List[BacktestCandidateInput] = Field(
        ..., min_length=1, description="Stocks to replay; must not be empty"
    )
    start_date: date_type = Field(..., description="First trading day of the replay")
    end_date: date_type = Field(..., description="Last trading day of the replay")
    initial_capital: Optional[Decimal] = Field(
        None, description="Overrides the portfolio's stored initial capital"
    )
    strategy: str = Field("phase7_technical", description="Phase 8 strategy name")
    policy: Optional[PortfolioRiskPolicySchema] = Field(
        None, description="Complete replacement policy; omit to use the stored limits"
    )
    exit_on_signal: bool = Field(
        True,
        description="Close a position when the strategy emits an exit signal, not only at its stop",
    )
    mark_unpriceable_positions: bool = Field(
        True,
        description="Hold a position's last mark on a day its stock has no bar, so equity stays complete",
    )

    @model_validator(mode="after")
    def _check_window(self) -> "PortfolioBacktestRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be earlier than start_date")
        if self.initial_capital is not None and self.initial_capital <= 0:
            raise ValueError("initial_capital must be greater than zero when set")
        return self

    def unique_candidates(self) -> List[int]:
        """Candidate stock ids, de-duplicated, first occurrence first.

        Replaying one stock twice would size the same position twice from the same bars, so a
        repeated candidate is collapsed rather than silently doubling its weight. Deterministic:
        first-seen order is preserved, so the same request always replays the same way.
        """
        return list(dict.fromkeys(candidate.stock_id for candidate in self.candidates))

    def stop_for(self, stock_id: int) -> Optional[Decimal]:
        """The fixed stop requested for ``stock_id``, or ``None`` when none was given."""
        for candidate in self.candidates:
            if candidate.stock_id == stock_id:
                return candidate.stop_price
        return None


class BacktestTradeResponse(BaseModel):
    """One executed trade inside a portfolio backtest."""

    stock_id: int
    symbol: Optional[str] = None
    side: OrderSide
    entry_date: date_type
    exit_date: date_type
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    return_pct: Decimal
    holding_period: int
    exit_reason: str = Field(..., description="signal, stop_loss, take_profit or end_of_period")


class BacktestEquityPointResponse(BaseModel):
    """One day of the portfolio equity curve."""

    date: date_type
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    exposure_pct: Optional[Decimal] = None
    drawdown_pct: Decimal = Field(0, description="Signed drawdown from the running peak")
    position_count: int


class PortfolioBacktestResponse(BaseModel):
    """Everything one portfolio backtest produced.

    **A simulation.** It replays stored history under a fixed rule set; it is not a prediction,
    and it is not a recommendation to trade.
    """

    portfolio_id: int
    initial_capital: Decimal
    final_equity: Decimal
    ending_cash: Decimal
    ending_invested_capital: Decimal
    total_pnl: Decimal
    total_return_pct: Decimal
    total_fees: Decimal
    number_of_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: Optional[Decimal] = Field(None, description="Null when no trade closed")
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    peak_equity: Decimal
    average_position_size: Decimal = Field(
        ..., description="Mean capital committed per executed entry"
    )
    average_exposure_pct: Optional[Decimal] = Field(
        None, description="Mean of the daily exposure percentages over the recorded window"
    )
    maximum_exposure_pct: Optional[Decimal] = Field(
        None, description="Highest daily exposure percentage over the recorded window"
    )
    days_processed: int
    rejected_signals: int = Field(
        ..., description="Entries the policy refused; counted, and reported, never retried"
    )
    trades: List[BacktestTradeResponse] = Field(default_factory=list)
    equity_curve: List[BacktestEquityPointResponse] = Field(default_factory=list)
    policy: PortfolioRiskPolicySchema
    currency: str = Field(DEFAULT_CURRENCY, description="ISO-4217 code of the portfolio's money")
    execution_price_policy: str = Field(
        "OPEN", description="Which price of which bar a fill is taken from"
    )
    no_look_ahead: bool = Field(
        True,
        description=(
            "True when every fill was priced from the bar after the one that produced its "
            "signal. Reported rather than assumed: it is a property of the run, not a promise."
        ),
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")