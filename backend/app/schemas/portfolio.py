"""Pydantic schemas for the Phase 9 paper-trading / portfolio domain.

**Paper trading only.** Everything here describes virtual portfolios, simulated orders and
simulated fills. No schema carries a brokerage identifier, credential or real-money
endpoint, and nothing in this module can place a real order.

Monetary fields are ``Decimal`` end to end, including across the HTTP boundary, so no
floating-point value is ever parsed into or serialised out of a financial figure.
"""

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.core.config import default_currency

#: Attached to portfolio responses so the simulated nature of the data travels with it.
PAPER_TRADING_DISCLAIMER = (
    "PAPER TRADING SIMULATION. This portfolio holds no real money and no order here "
    "reaches a broker or an exchange. Results are a deterministic simulation of a fixed "
    "rule set and are not investment advice, a prediction, or a guarantee of any outcome."
)


class PortfolioStatus(str, Enum):
    """Lifecycle state of a virtual portfolio.

    Only ``ACTIVE`` accepts orders. ``PAUSED`` and ``CLOSED`` reject every new order
    without mutating cash, positions or realised P&L.
    """

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"


class OrderSide(str, Enum):
    """Direction of a simulated order. Long-only: a SELL reduces or closes a position."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Supported order types.

    ``MARKET`` fills immediately at the supplied simulation price and is the only type the
    Phase 9 engine produces. ``LIMIT`` is accepted by the Phase 16 execution layer, which
    validates it and requires an explicit limit price; a limit order is never silently
    converted into a market order, so it is reported as unsupported rather than filled at a
    price nothing asked for.
    """

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    """Lifecycle state of a simulated order.

    The first four values are Phase 9's single-shot states and keep their meaning: an order
    placed through the Phase 9 path is ``PENDING`` and becomes ``FILLED``, ``REJECTED`` or
    ``CANCELLED``.

    Phase 16 adds the intermediate states a *planned* order passes through, because an order
    that is filled by an execution adapter does not go straight from intent to result: it is
    created, validated, risk-approved, submitted, and then filled either in full or in parts.
    The extra values are purely additive — nothing in Phases 9-12 writes or reads them, so
    those paths behave exactly as before.
    """

    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FAILED = "FAILED"


class OrderSource(str, Enum):
    """How an order reached the paper engine."""

    MANUAL = "MANUAL"
    STRATEGY = "STRATEGY"


class RejectionCode(str, Enum):
    """Stable, machine-readable reason an order was not executed.

    The same codes drive the persisted ``rejection_code`` column, the domain exception
    raised by the service and the HTTP error payload.
    """

    PORTFOLIO_INACTIVE = "PORTFOLIO_INACTIVE"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    INVALID_PRICE = "INVALID_PRICE"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    RISK_LIMIT_EXCEEDED = "RISK_LIMIT_EXCEEDED"

    # Rule-specific codes. Every configured limit has its own code so a caller can branch on
    # *which* rule fired without parsing prose, while ``RISK_LIMIT_EXCEEDED`` remains the
    # documented catch-all for a risk refusal.
    MAX_DRAWDOWN_EXCEEDED = "MAX_DRAWDOWN_EXCEEDED"
    MAX_POSITIONS_EXCEEDED = "MAX_POSITIONS_EXCEEDED"
    MAX_POSITION_QUANTITY_EXCEEDED = "MAX_POSITION_QUANTITY_EXCEEDED"
    MAX_POSITION_SIZE_EXCEEDED = "MAX_POSITION_SIZE_EXCEEDED"
    MAX_POSITION_ALLOCATION_EXCEEDED = "MAX_POSITION_ALLOCATION_EXCEEDED"
    MAX_EXPOSURE_EXCEEDED = "MAX_EXPOSURE_EXCEEDED"
    MAX_TRADE_ALLOCATION_EXCEEDED = "MAX_TRADE_ALLOCATION_EXCEEDED"
    MIN_CASH_RESERVE_BREACHED = "MIN_CASH_RESERVE_BREACHED"
    MAX_DAILY_LOSS_EXCEEDED = "MAX_DAILY_LOSS_EXCEEDED"
    MIN_TRADE_VALUE_BELOW = "MIN_TRADE_VALUE_BELOW"

    # Phase 13 codes. The Phase 13 allocation engine speaks its own, more specific vocabulary:
    # it names the limit in the terms the caller configured it in ("position weight", "portfolio
    # exposure", "risk per trade"), so a client can branch on the rule without a lookup table
    # translating one engine's wording into another's. The Phase 9-12 codes above are untouched
    # and keep their meaning.
    MAX_POSITION_WEIGHT_EXCEEDED = "MAX_POSITION_WEIGHT_EXCEEDED"
    MAX_PORTFOLIO_EXPOSURE_EXCEEDED = "MAX_PORTFOLIO_EXPOSURE_EXCEEDED"
    MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED"
    MAX_RISK_PER_TRADE_EXCEEDED = "MAX_RISK_PER_TRADE_EXCEEDED"
    MAX_SECTOR_WEIGHT_EXCEEDED = "MAX_SECTOR_WEIGHT_EXCEEDED"
    MAX_TOTAL_RISK_EXCEEDED = "MAX_TOTAL_RISK_EXCEEDED"
    INVALID_STOP_PRICE = "INVALID_STOP_PRICE"
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    INVALID_PORTFOLIO_STATE = "INVALID_PORTFOLIO_STATE"
    UNSUPPORTED_SIDE = "UNSUPPORTED_SIDE"

    # Phase 16 codes. The execution layer validates a whole *plan* rather than one order, so it
    # needs reasons the single-order codes above do not name: a plan that asks for the same
    # trade twice, a target built from state that has since moved, an order asked to change
    # state it has already left, and an execution whose result does not reconcile. They are
    # additive; every earlier code keeps its meaning.
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    STALE_PORTFOLIO = "STALE_PORTFOLIO"
    INVALID_TARGET = "INVALID_TARGET"
    UNSUPPORTED_ORDER_TYPE = "UNSUPPORTED_ORDER_TYPE"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    ORPHAN_ORDER = "ORPHAN_ORDER"


class StrategyAction(str, Enum):
    """What the Phase 7/8 strategy rules decided for a stock."""

    HOLD = "hold"
    ENTER = "enter"
    EXIT = "exit"


class ExecutionPricePolicy(str, Enum):
    """Which price of which bar a simulated fill is taken from.

    ``OPEN`` (the default) is the Phase 8 convention and the only one that is
    unambiguously free of look-ahead: a decision taken from the close of bar *N* is filled
    at the **open of bar N+1**, so no part of bar N+1 can influence the decision.

    ``CLOSE`` fills at the close of the bar that produced the signal. That close is already
    known at the moment the decision is taken, so this policy is also free of look-ahead —
    it simply executes a bar earlier than the Phase 8 simulator does, and pays no overnight
    gap. It exists so a caller can choose between the two conventions explicitly; the engine
    never silently switches between them.
    """

    OPEN = "OPEN"
    CLOSE = "CLOSE"


class PositionSizingMethod(str, Enum):
    """How the quantity of a proposed order is derived.

    The first three are the Phase 9 formulas. The last two are the Phase 11 allocation
    formulas and are additive: they extend the existing sizing mechanism rather than
    introducing a second one, so a caller reaches every sizing rule through one request type.

    ``EQUAL_WEIGHT``
        ``target_weight = 1 / N`` over the eligible candidates, then capped by
        ``max_position_weight_pct``.
    ``MAX_POSITION_CAP``
        the requested allocation, truncated at ``max_position_weight_pct``.
    ``RISK_AND_WEIGHT`` (Phase 13)
        ``min(floor(equity * risk_pct / 100 / (entry - stop)), floor(equity * max_position_weight_pct
        / 100 / entry))``, then capped by the cash the portfolio actually holds. The only
        difference from ``RISK_BASED`` is the *capital* bound: ``RISK_BASED`` caps at the whole
        cash pile, this caps at what one position may be worth.
    """

    FIXED_CAPITAL = "FIXED_CAPITAL"
    PERCENTAGE = "PERCENTAGE"
    RISK_BASED = "RISK_BASED"
    EQUAL_WEIGHT = "EQUAL_WEIGHT"
    MAX_POSITION_CAP = "MAX_POSITION_CAP"
    RISK_AND_WEIGHT = "RISK_AND_WEIGHT"


class TransactionType(str, Enum):
    """The kind of cash movement recorded in the portfolio transaction ledger.

    ``BUY`` / ``SELL`` mirror the simulated executions already stored in
    ``portfolio_trades``; ``DEPOSIT`` and ``WITHDRAWAL`` are capital movements that have no
    order and no trade, which is why they need a ledger of their own.
    """

    BUY = "BUY"
    SELL = "SELL"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"


class DecisionAction(str, Enum):
    """What the portfolio construction engine decided to do with one stock.

    ``BUY`` / ``REDUCE`` add or trim a holding towards its target weight, ``SELL`` closes it,
    ``REBALANCE`` trades towards a target weight supplied for rebalancing, and ``HOLD`` means
    the portfolio is already where it should be — including "no action needed".
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    REBALANCE = "REBALANCE"


class RiskAction(str, Enum):
    """What the risk engine recommends for a position that breaches a limit.

    Recommendations only. Nothing here executes a trade: the recommendation is returned to the
    construction engine and to the caller, and it becomes an order only when the simulation
    explicitly applies the decision.
    """

    NONE = "NONE"
    REDUCE = "REDUCE"
    SELL = "SELL"


class RiskSeverity(str, Enum):
    """How serious a reported risk breach is.

    ``INFO`` is a measurement worth surfacing (a threshold that is close), ``WARNING`` is a
    limit that would refuse a new buy, and ``BREACH`` is a limit the portfolio is already
    outside.
    """

    INFO = "INFO"
    WARNING = "WARNING"
    BREACH = "BREACH"


# --- Configuration objects ---------------------------------------------------


class TradingCostSettings(BaseModel):
    """Simulated execution costs applied to every paper order.

    Defaults come from the application settings so the cost model is centralised in one
    place and configurable per portfolio. These are simulation assumptions, not the fee
    schedule of any real broker.
    """

    model_config = ConfigDict(from_attributes=True)

    commission_rate: Decimal = Field(
        Decimal("0.001"), ge=0, le=1, description="Fraction of gross value charged per side, e.g. 0.001 = 0.1%"
    )
    min_fee: Decimal = Field(
        Decimal("0"), ge=0, description="Minimum fee per side; 0 means only the percentage applies"
    )
    slippage_rate: Decimal = Field(
        Decimal("0"),
        ge=0,
        le=1,
        description="Fractional slippage applied adversely to the fill price (buys higher, sells lower)",
    )


class RiskLimitSettings(BaseModel):
    """Deterministic portfolio risk limits.

    Every limit is optional: ``null`` (or ``false``) means the rule is disabled. Limits are
    evaluated before an order is executed, and a violation rejects the order without
    mutating portfolio state.
    """

    model_config = ConfigDict(from_attributes=True)

    max_position_quantity: Optional[int] = Field(
        None, ge=1, description="Maximum shares held in a single stock"
    )
    max_position_value: Optional[Decimal] = Field(
        None, gt=0, description="Maximum market value of a single position"
    )
    max_position_allocation_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Maximum share of portfolio equity a single position may represent",
    )
    max_drawdown_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Drawdown from peak equity above which new BUY orders are blocked",
    )
    max_daily_loss_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description=(
            "Loss on the current UTC day, in percent of that day's opening equity, above which "
            "new BUY orders are blocked. Needs at least one snapshot a day to be meaningful."
        ),
    )
    min_trade_value: Optional[Decimal] = Field(
        None,
        gt=0,
        description=(
            "Smallest gross value an order may commit. Applies to BUY and SELL alike, so a "
            "dust order cannot be used to move a position inexpensively."
        ),
    )
    max_exposure_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Maximum share of portfolio equity that may be invested across all positions",
    )
    max_trade_allocation_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Maximum share of portfolio equity a single trade may commit",
    )
    min_cash_reserve_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Share of equity that must remain in cash; a buy breaching it is rejected",
    )
    max_positions: Optional[int] = Field(
        None, ge=1, description="Maximum number of simultaneously open positions"
    )

    @model_validator(mode="after")
    def validate_allocation(self) -> "RiskLimitSettings":
        if (
            self.max_position_allocation_pct is not None
            and self.max_position_allocation_pct > 100
        ):
            raise ValueError("max_position_allocation_pct must be in (0, 100]")
        return self


class PortfolioAllocationConfig(BaseModel):
    """How a portfolio is *constructed* and how much risk it carries — the Phase 11 config.

    This is deliberately a separate object from :class:`RiskLimitSettings`:

    * :class:`TradingCostSettings` — what a simulated execution costs;
    * :class:`RiskLimitSettings` — the Phase 9/10 hard limits a single order may never breach;
    * :class:`PortfolioAllocationConfig` — the portfolio-level *policy*: how much to put into
      each name, how much risk to carry per trade, when to stop out and when to rebalance.

    Every rule is optional except the four that have to exist for the engine to be able to
    answer at all (the sizing method, the position cap, the risk budget and the cash reserve).
    Every default comes from the application settings, so no allocation constant is hardcoded
    in the engine and a future strategy can supply a different configuration.

    Percentages are in percent (20 means 20%), never fractions, matching every other
    percentage field in this module.
    """

    model_config = ConfigDict(from_attributes=True)

    sizing_method: PositionSizingMethod = Field(
        PositionSizingMethod.EQUAL_WEIGHT,
        description="Allocation formula used to turn eligible candidates into target weights",
    )
    max_position_weight_pct: Decimal = Field(
        Decimal("20"),
        gt=0,
        le=100,
        description="Maximum share of portfolio equity a single position may represent",
    )
    max_sector_weight_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description=(
            "Maximum share of portfolio equity one sector may represent. Evaluated only for "
            "positions whose sector is known (Company.sector); unset disables the rule."
        ),
    )
    max_positions: Optional[int] = Field(
        None, ge=1, description="Maximum number of simultaneously open positions"
    )
    max_exposure_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Maximum share of portfolio equity that may be invested across all positions",
    )
    max_position_loss_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description=(
            "Maximum tolerated loss on a single position, in percent of its average entry "
            "price. A position that loses more than this is a SELL candidate."
        ),
    )
    stop_loss_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Stop-loss trigger, in percent below a position's average entry price",
    )
    take_profit_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Take-profit trigger, in percent above a position's average entry price",
    )
    rebalance_threshold_pct: Decimal = Field(
        Decimal("0.5"),
        ge=0,
        le=100,
        description=(
            "Weight drift that must be reached before a rebalancing trade is proposed. "
            "Keeps rounding noise from generating transactions."
        ),
    )
    min_position_value: Optional[Decimal] = Field(
        None, gt=0, description="Smallest position value the construction engine will open"
    )
    risk_per_trade_pct: Decimal = Field(
        Decimal("1"),
        gt=0,
        le=100,
        description=(
            "Share of portfolio equity risked on a single trade. Used as the risk budget by "
            "RISK_BASED sizing and as the per-share risk budget by the risk engine."
        ),
    )
    cash_reserve_pct: Decimal = Field(
        Decimal("10"),
        ge=0,
        le=100,
        description="Share of portfolio equity that must remain in cash after a buy",
    )
    target_position_count: Optional[int] = Field(
        None,
        ge=1,
        le=500,
        description=(
            "How many names to target when sizing equally. Defaults to the number of eligible "
            "candidates, capped by max_positions."
        ),
    )

    @model_validator(mode="after")
    def validate_allocation(self) -> "PortfolioAllocationConfig":
        if self.sizing_method is PositionSizingMethod.RISK_BASED and self.stop_loss_pct is None:
            raise ValueError(
                "RISK_BASED sizing requires 'stop_loss_pct': without a stop there is no "
                "per-share risk, and a position size cannot be derived from it."
            )
        if (
            self.target_position_count is not None
            and self.max_positions is not None
            and self.target_position_count > self.max_positions
        ):
            raise ValueError("target_position_count cannot exceed max_positions")
        return self


# --- Requests ----------------------------------------------------------------


class PortfolioCreate(BaseModel):
    """Create a virtual paper portfolio.

    A new portfolio starts with cash and equity equal to ``initial_capital`` and holds no
    positions.
    """

    name: str = Field(..., min_length=1, max_length=100, description="Portfolio name")
    description: Optional[str] = Field(
        None, max_length=255, description="Optional human-readable description"
    )
    initial_capital: Decimal = Field(
        ..., gt=0, description="Starting virtual capital; also the initial cash balance"
    )
    currency: str = Field(
        default_factory=default_currency,
        min_length=3,
        max_length=3,
        description=(
            "ISO-4217 code every monetary figure of this portfolio is denominated in. The engine "
            "performs no currency conversion, so this states the unit of the figures rather than "
            "enabling conversion. Fixed at creation, because changing it would make the "
            "portfolio's recorded history ambiguous."
        ),
    )
    trading_costs: Optional[TradingCostSettings] = Field(
        None, description="Simulated cost model; defaults to the application settings"
    )
    risk_limits: Optional[RiskLimitSettings] = Field(
        None, description="Portfolio risk limits; omitted rules stay disabled"
    )
    allocation_config: Optional[PortfolioAllocationConfig] = Field(
        None,
        description=(
            "Portfolio construction policy: sizing method, position cap, cash reserve, risk "
            "budget, stop-loss, take-profit and rebalancing threshold. Defaults to the "
            "application settings."
        ),
    )


class PortfolioStatusUpdate(BaseModel):
    """Change the lifecycle state of a portfolio."""

    status: PortfolioStatus = Field(..., description="New portfolio status")


class PortfolioUpdate(BaseModel):
    """Partially update a portfolio.

    Every field is optional; ``None`` means "leave unchanged", so an empty body is a no-op
    rather than an error. ``CLOSED`` remains terminal.
    """

    name: Optional[str] = Field(
        None, min_length=1, max_length=100, description="New portfolio name"
    )
    description: Optional[str] = Field(
        None, max_length=255, description="New portfolio description"
    )
    status: Optional[PortfolioStatus] = Field(None, description="New portfolio status")
    risk_limits: Optional[RiskLimitSettings] = Field(
        None,
        description=(
            "Replaces the whole risk-limit set for this portfolio. Send every limit that "
            "should remain in force; omitted limits are disabled, so this is a replacement "
            "rather than a merge."
        ),
    )
    allocation_config: Optional[PortfolioAllocationConfig] = Field(
        None,
        description=(
            "Replaces the whole allocation configuration for this portfolio, on the same "
            "replacement-not-merge basis as 'risk_limits'."
        ),
    )


class PortfolioPriceInput(BaseModel):
    """A single market price used to re-value a portfolio."""

    stock_id: int = Field(..., ge=1, description="Internal stock ID")
    price: Decimal = Field(..., gt=0, description="Current market price")


class PortfolioValuationRequest(BaseModel):
    """Mark open positions to new market prices.

    Valuation only: it never creates an order, a trade, or any cash movement.
    """

    prices: List[PortfolioPriceInput] = Field(
        default_factory=list,
        description="Current market prices, one entry per stock held",
    )
    as_of: Optional[datetime] = Field(
        None, description="Optional valuation timestamp; defaults to now (UTC)"
    )


class PortfolioSnapshotCreate(BaseModel):
    """Create one point-in-time equity-curve snapshot."""

    prices: List[PortfolioPriceInput] = Field(
        default_factory=list,
        description="Optional market prices to apply before capturing the snapshot",
    )
    as_of: Optional[datetime] = Field(
        None, description="Snapshot timestamp; defaults to now (UTC)"
    )


class OrderCreate(BaseModel):
    """Submit a simulated market order.

    The order is validated, risk-checked and, if accepted, filled immediately at
    ``requested_price`` (adjusted only by any configured slippage). Nothing is sent to a
    broker.
    """

    stock_id: int = Field(..., ge=1, description="Internal stock ID")
    side: OrderSide = Field(..., description="BUY or SELL")
    order_type: OrderType = Field(
        OrderType.MARKET, description="Only MARKET orders are supported in Phase 9"
    )
    quantity: int = Field(..., gt=0, description="Whole shares; must be positive")
    requested_price: Decimal = Field(
        ..., gt=0, description="Simulated execution price supplied by the caller"
    )
    idempotency_key: Optional[str] = Field(
        None,
        max_length=128,
        description="Repeat submissions of the same key return the original order instead of executing again",
    )
    defer_execution: bool = Field(
        False,
        description=(
            "Record the order as PENDING instead of filling it immediately. A deferred order "
            "is executed later with POST /portfolios/{id}/orders/{order_id}/execute"
        ),
    )
    signal_as_of: Optional[datetime] = Field(
        None,
        description=(
            "When the decision that justified this order was taken. Recorded for audit; a "
            "historical fill must never carry a signal timestamp later than its execution"
        ),
    )
    execution_price_policy: Optional[ExecutionPricePolicy] = Field(
        None,
        description="Which price of which bar the fill reference was taken from; null for manual orders",
    )
    metadata: Optional[Dict[str, object]] = Field(
        None, description="Free-form audit metadata stored with the order"
    )


class RiskCheckRequest(BaseModel):
    """A hypothetical order to evaluate against the portfolio's risk limits.

    Nothing is persisted and no state changes: this answers "would this order be accepted?".
    Exactly one of ``requested_price`` or ``use_market_price`` is required — pass a price to
    model a specific fill, or ask the engine to use the latest stored close.
    """

    stock_id: int = Field(..., ge=1, description="Internal stock ID")
    side: OrderSide = Field(..., description="BUY or SELL")
    quantity: int = Field(..., description="Hypothetical shares; may be 0 or negative to be rejected")
    requested_price: Optional[Decimal] = Field(
        None, gt=0, description="Simulated execution price to evaluate against"
    )
    use_market_price: bool = Field(
        False,
        description="Evaluate against the latest stored close instead of an explicit price",
    )
    as_of: Optional[datetime] = Field(
        None,
        description=(
            "The moment the daily-loss rule is measured against; defaults to now. A portfolio "
            "with a max_daily_loss_pct limit can therefore be checked 'as of' any past day."
        ),
    )

    @model_validator(mode="after")
    def require_a_price(self) -> "RiskCheckRequest":
        if self.requested_price is None and not self.use_market_price:
            raise ValueError("supply requested_price or set use_market_price=true")
        return self


class RiskViolationResponse(BaseModel):
    """One risk rule a hypothetical trade breaks."""

    rule: str = Field(..., description="Stable machine-readable rule name")
    reason: str = Field(..., description="Human-readable explanation including the numbers")
    limit: Optional[Decimal] = Field(None, description="Configured limit")
    observed: Optional[Decimal] = Field(None, description="Value that breached the limit")
    details: Dict[str, object] = Field(
        default_factory=dict, description="Machine-readable context for the breach"
    )


class RiskCheckResponse(BaseModel):
    """Whether the portfolio's risk engine would accept a proposed trade."""

    portfolio_id: int = Field(..., description="Portfolio id")
    stock_id: int = Field(..., description="Internal stock ID")
    side: OrderSide = Field(..., description="BUY or SELL")
    quantity: int = Field(..., description="Hypothetical shares evaluated")
    evaluation_price: Optional[Decimal] = Field(
        None, description="Price the trade was evaluated at"
    )
    accepted: bool = Field(..., description="True when no risk rule is violated")
    rejection_code: Optional[RejectionCode] = Field(
        None, description="Machine-readable rejection reason when not accepted"
    )
    violations: List[RiskViolationResponse] = Field(
        default_factory=list, description="Every violated rule, not only the first"
    )
    equity: Decimal = Field(..., description="Portfolio equity used as the risk basis")
    cash: Decimal = Field(..., description="Available cash used as the risk basis")
    peak_equity: Decimal = Field(..., description="Highest equity ever recorded")
    current_drawdown_pct: Decimal = Field(
        ..., description="Drawdown from peak equity as a positive percentage"
    )
    exposure_pct: Optional[Decimal] = Field(
        None, description="Current exposure: market value as a percentage of equity"
    )
    position_count: int = Field(..., description="Open positions before the hypothetical trade")
    evaluated_at: datetime = Field(..., description="Evaluation timestamp")
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class PositionRiskResponse(BaseModel):
    """Risk figures for one open position.

    ``risk_amount`` is what the position would cost to close right now: the loss it would book
    if it fell to its stop, floored at zero so a position above water never reports a negative
    risk. ``risk_contribution_pct`` is that amount as a percentage of portfolio equity, which
    is what makes a risk engine explainable — it says *which* position carries the risk.
    """

    stock_id: int = Field(..., description="Internal stock ID")
    symbol: Optional[str] = Field(None, description="Stock symbol, when known")
    sector: Optional[str] = Field(None, description="Sector, when a company profile exists")
    quantity: int = Field(..., description="Shares held")
    average_entry_price: Decimal = Field(..., description="Quantity-weighted average entry price")
    current_price: Decimal = Field(..., description="Price the position is marked at")
    market_value: Decimal = Field(..., description="quantity * current_price")
    weight_pct: Optional[Decimal] = Field(
        None, description="Position market value as a percentage of portfolio equity"
    )
    unrealized_pnl: Decimal = Field(..., description="Gross mark-to-market P&L")
    unrealized_pnl_pct: Optional[Decimal] = Field(
        None, description="(current_price - average_entry_price) / average_entry_price * 100"
    )
    stop_price: Optional[Decimal] = Field(
        None, description="Stop-loss price implied by the configuration, when one is set"
    )
    take_profit_price: Optional[Decimal] = Field(
        None, description="Take-profit price implied by the configuration, when one is set"
    )
    stop_loss_breached: bool = Field(
        False, description="True when current_price is at or below the stop price"
    )
    take_profit_triggered: bool = Field(
        False, description="True when current_price is at or above the take-profit price"
    )
    risk_amount: Decimal = Field(
        Decimal("0"),
        description="Loss this position would book if it fell to its stop; never negative",
    )
    risk_contribution_pct: Optional[Decimal] = Field(
        None, description="risk_amount as a percentage of portfolio equity"
    )


class RiskRecommendationResponse(BaseModel):
    """A risk-driven action the engine *recommends*; nothing is executed automatically."""

    stock_id: int = Field(..., description="Internal stock ID")
    symbol: Optional[str] = Field(None, description="Stock symbol, when known")
    action: RiskAction = Field(..., description="NONE, REDUCE or SELL")
    quantity: int = Field(..., ge=0, description="Shares the recommendation covers")
    price: Decimal = Field(..., description="Price the recommendation was computed at")
    rule: str = Field(..., description="Stable machine-readable rule that fired")
    reason: str = Field(..., description="Human-readable explanation including the numbers")
    details: Dict[str, object] = Field(
        default_factory=dict, description="Machine-readable context for the recommendation"
    )


class PortfolioRiskResponse(BaseModel):
    """The portfolio's effective risk configuration and the rules currently in breach.

    ``violations`` are *current-state* breaches (for example the drawdown guard is already
    triggered), which is what distinguishes a portfolio that is currently over-exposed from
    one that merely could not take another trade.
    """

    portfolio_id: int = Field(..., description="Portfolio id")
    status: PortfolioStatus = Field(..., description="Current portfolio status")
    equity: Decimal = Field(..., description="cash + market value")
    cash: Decimal = Field(..., description="Available cash")
    cash_reserve_required: Decimal = Field(
        ..., description="Minimum cash the configured reserve rule requires"
    )
    market_value: Decimal = Field(..., description="Sum of open position market values")
    exposure_pct: Optional[Decimal] = Field(
        None, description="market value as a percentage of equity"
    )
    invested_capital: Decimal = Field(..., description="Sum of open position cost bases")
    position_count: int = Field(..., description="Open positions")
    max_positions: Optional[int] = Field(None, description="Configured position-count limit")
    peak_equity: Decimal = Field(..., description="Highest equity ever recorded")
    current_drawdown_pct: Decimal = Field(
        ..., description="Drawdown from peak equity as a positive percentage"
    )
    drawdown_limit_pct: Optional[Decimal] = Field(
        None, description="Configured drawdown guard; null disables it"
    )
    drawdown_breached: bool = Field(
        ..., description="True when the drawdown guard is configured and currently triggered"
    )
    risk_limits: RiskLimitSettings = Field(..., description="Limits in force for this portfolio")
    violations: List[RiskViolationResponse] = Field(
        default_factory=list, description="Rules currently breached"
    )
    # --- Phase 11: portfolio-level risk report ---
    allocation_config: PortfolioAllocationConfig = Field(
        default_factory=PortfolioAllocationConfig,
        description="Construction and risk policy in force for this portfolio",
    )
    gross_exposure_pct: Optional[Decimal] = Field(
        None,
        description=(
            "Sum of the absolute market values of every position, as a percentage of equity. "
            "Equal to exposure_pct for a long-only portfolio; kept separate so a future "
            "long/short portfolio reports shorts correctly without changing this field."
        ),
    )
    net_exposure_pct: Optional[Decimal] = Field(
        None,
        description=(
            "Long market value minus short market value, as a percentage of equity. Equal to "
            "gross_exposure_pct while the portfolio is long-only."
        ),
    )
    cash_pct: Optional[Decimal] = Field(
        None, description="cash as a percentage of equity; null when equity is not positive"
    )
    invested_pct: Optional[Decimal] = Field(
        None,
        description="sum of open position cost bases as a percentage of equity; null when "
        "equity is not positive",
    )
    top_position_weight_pct: Optional[Decimal] = Field(
        None, description="Weight of the single largest position, as a percentage of equity"
    )
    top_positions_weight_pct: Optional[Decimal] = Field(
        None,
        description=(
            "Combined weight of the three largest positions, as a percentage of equity. "
            "Reported alongside the largest single weight so concentration is visible even "
            "when no single name breaches the cap."
        ),
    )
    sector_weights: Dict[str, Decimal] = Field(
        default_factory=dict,
        description="Combined weight per sector, as a percentage of equity, for sectors that are known",
    )
    total_risk_amount: Decimal = Field(
        Decimal("0"),
        description="Sum of every position's risk to its stop; never negative",
    )
    position_risks: List[PositionRiskResponse] = Field(
        default_factory=list,
        description="Per-position risk, ordered by weight descending then stock id ascending",
    )
    recommendations: List[RiskRecommendationResponse] = Field(
        default_factory=list,
        description=(
            "Risk-driven actions the engine recommends. Recommendations only — they are "
            "applied only when the caller explicitly asks for it."
        ),
    )
    as_of: datetime = Field(..., description="Evaluation timestamp")
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class PositionSizingRequest(BaseModel):
    """Derive the quantity of an order from a capital budget, a portfolio percentage or risk.

    ``method`` selects the formula; the fields each formula needs are validated together so
    a request can never be accepted with an incomplete set of inputs.
    """

    method: PositionSizingMethod = Field(..., description="Sizing formula to apply")
    stock_id: Optional[int] = Field(
        None, ge=1, description="Internal stock ID; only used to price the market-price fallback"
    )
    price: Optional[Decimal] = Field(
        None, gt=0, description="Execution price to size against; omit to use the stored market price"
    )
    use_market_price: bool = Field(
        False, description="Use the latest stored close as the sizing price"
    )
    capital: Optional[Decimal] = Field(
        None, gt=0, description="FIXED_CAPITAL: capital committed to this trade"
    )
    allocation_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="PERCENTAGE: share of portfolio equity to commit",
    )
    risk_pct: Optional[Decimal] = Field(
        None, gt=0, le=100, description="RISK_BASED: share of equity at risk on this trade"
    )
    entry_price: Optional[Decimal] = Field(
        None, gt=0, description="RISK_BASED: planned entry price"
    )
    stop_loss_price: Optional[Decimal] = Field(
        None, gt=0, description="RISK_BASED: stop-loss price that defines the risk per share"
    )
    candidate_count: Optional[int] = Field(
        None,
        ge=1,
        description=(
            "EQUAL_WEIGHT: how many eligible candidates the weight is split across "
            "(100 / candidate_count)"
        ),
    )
    max_position_weight_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description=(
            "EQUAL_WEIGHT / MAX_POSITION_CAP: the single-position cap the resulting weight may "
            "not exceed"
        ),
    )

    @model_validator(mode="after")
    def validate_inputs(self) -> "PositionSizingRequest":
        if self.method is PositionSizingMethod.FIXED_CAPITAL and self.capital is None:
            raise ValueError("FIXED_CAPITAL sizing requires 'capital'")
        if self.method is PositionSizingMethod.PERCENTAGE and self.allocation_pct is None:
            raise ValueError("PERCENTAGE sizing requires 'allocation_pct'")
        if self.method is PositionSizingMethod.EQUAL_WEIGHT:
            missing = [
                name
                for name in ("candidate_count", "max_position_weight_pct")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"EQUAL_WEIGHT sizing requires {', '.join(missing)}")
        if self.method is PositionSizingMethod.MAX_POSITION_CAP:
            missing = [
                name
                for name in ("allocation_pct", "max_position_weight_pct")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"MAX_POSITION_CAP sizing requires {', '.join(missing)}")
        if self.method is PositionSizingMethod.RISK_BASED:
            missing = [
                name
                for name in ("risk_pct", "entry_price", "stop_loss_price")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"RISK_BASED sizing requires {', '.join(missing)}"
                )
        return self


class PositionSizingResponse(BaseModel):
    """The quantity a sizing request produced, and the arithmetic behind it."""

    portfolio_id: int = Field(..., description="Portfolio id")
    method: PositionSizingMethod = Field(..., description="Sizing formula applied")
    stock_id: Optional[int] = Field(None, description="Internal stock ID, when one was resolved")
    price: Decimal = Field(..., description="Price the quantity was sized against")
    quantity: int = Field(
        ..., ge=0, description="Whole shares; floored, never rounded up into a trade"
    )
    capital: Decimal = Field(..., description="Capital actually committed by this quantity")
    budget: Decimal = Field(..., description="Capital the formula was allowed to spend")
    equity: Decimal = Field(..., description="Portfolio equity used as the sizing basis")
    risk_per_share: Optional[Decimal] = Field(
        None, description="RISK_BASED: abs(entry_price - stop_loss_price)"
    )
    allowed_risk: Optional[Decimal] = Field(
        None, description="RISK_BASED: equity * risk_pct"
    )
    reason: str = Field(..., description="How the quantity was derived")
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class PaperRunRequest(BaseModel):
    """Replay stored history for one stock against a paper portfolio, bar by bar.

    The engine decides from the close of bar *N* and, under the ``OPEN`` policy, fills at the
    open of bar *N+1*. It never reads a bar beyond the one it is acting on.
    """

    stock_id: int = Field(..., ge=1, description="Internal stock ID to replay")
    start_date: Optional[date_type] = Field(
        None, description="First recorded bar (inclusive); earlier bars warm the indicators up"
    )
    end_date: Optional[date_type] = Field(
        None, description="Last recorded bar (inclusive); later bars are never read"
    )
    strategy: str = Field(
        "phase7_technical",
        min_length=1,
        max_length=100,
        description="Registered strategy name; the rules are reused unchanged from Phase 8",
    )
    execution_price_policy: Optional[ExecutionPricePolicy] = Field(
        None,
        description="Fill reference; defaults to the portfolio/application setting (OPEN)",
    )
    position_sizing: PositionSizingRequest = Field(
        default_factory=lambda: PositionSizingRequest(
            method=PositionSizingMethod.PERCENTAGE, allocation_pct=Decimal("10")
        ),
        description="How each entry is sized",
    )
    risk_free_rate_pct: Decimal = Field(
        Decimal("0"), ge=0, le=100, description="Annual risk-free rate used for Sharpe reporting"
    )

    @model_validator(mode="after")
    def validate_window(self) -> "PaperRunRequest":
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must not be earlier than start_date")
        return self


class StrategyOrderRequest(BaseModel):
    """Turn the existing Phase 7 signal / Phase 8 strategy rules into a paper order."""

    stock_id: int = Field(..., ge=1, description="Internal stock ID")
    price: Optional[Decimal] = Field(
        None,
        gt=0,
        description="Simulated execution price; defaults to the latest stored close for the stock",
    )
    strategy: str = Field(
        "phase7_technical",
        min_length=1,
        max_length=100,
        description="Registered strategy name; the rules are reused unchanged from Phase 8",
    )
    position_size_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Entry budget as a percentage of available cash; defaults to the portfolio setting",
    )
    quantity: Optional[int] = Field(
        None, gt=0, description="Explicit entry quantity; overrides position sizing when supplied"
    )
    idempotency_key: Optional[str] = Field(
        None, max_length=128, description="Optional caller key for duplicate protection"
    )


# --- Responses ---------------------------------------------------------------


class PositionResponse(BaseModel):
    """An open long position, marked to its last known price."""

    model_config = ConfigDict(from_attributes=True)

    position_id: int = Field(..., description="Position id")
    portfolio_id: int = Field(..., description="Owning portfolio id")
    stock_id: int = Field(..., description="Internal stock ID")
    symbol: Optional[str] = Field(None, description="Stock symbol, when known")
    quantity: int = Field(..., description="Shares held; always positive")
    average_entry_price: Decimal = Field(
        ..., description="Quantity-weighted average of the buy execution prices"
    )
    cost_basis: Decimal = Field(..., description="quantity * average_entry_price")
    entry_fees_total: Decimal = Field(
        ..., description="Entry fees charged on the shares still held"
    )
    current_price: Optional[Decimal] = Field(None, description="Last known market price")
    market_value: Optional[Decimal] = Field(None, description="quantity * current_price")
    unrealized_pnl: Optional[Decimal] = Field(
        None, description="quantity * (current_price - average_entry_price), before fees"
    )
    realized_pnl: Decimal = Field(
        ..., description="Realised P&L booked by sells of this stock in this portfolio"
    )
    allocation_pct: Optional[Decimal] = Field(
        None, description="Position market value as a percentage of portfolio equity"
    )
    opened_at: Optional[datetime] = Field(None, description="When the position was opened")
    updated_at: Optional[datetime] = Field(None, description="Last state change")


class OrderResponse(BaseModel):
    """A simulated order, filled or rejected."""

    model_config = ConfigDict(from_attributes=True)

    order_id: int = Field(..., description="Order id")
    portfolio_id: int = Field(..., description="Owning portfolio id")
    stock_id: int = Field(..., description="Internal stock ID")
    symbol: Optional[str] = Field(None, description="Stock symbol, when known")
    trading_mode: str = Field("PAPER", description="Always PAPER: orders are simulated")
    side: OrderSide = Field(..., description="BUY or SELL")
    order_type: OrderType = Field(..., description="MARKET")
    status: OrderStatus = Field(..., description="PENDING, FILLED, REJECTED or CANCELLED")
    source: OrderSource = Field(..., description="MANUAL or STRATEGY")
    quantity: int = Field(..., description="Requested shares")
    requested_price: Decimal = Field(..., description="Price supplied by the caller")
    execution_price: Optional[Decimal] = Field(
        None, description="Simulated fill price; null when the order was rejected"
    )
    gross_value: Optional[Decimal] = Field(
        None, description="quantity * execution_price; null when rejected"
    )
    fees: Optional[Decimal] = Field(None, description="Simulated fees charged")
    net_cash_impact: Optional[Decimal] = Field(
        None, description="Signed cash movement: negative for a buy, positive for a sell"
    )
    realized_pnl: Optional[Decimal] = Field(
        None, description="P&L booked by this execution; 0 for a buy"
    )
    rejection_code: Optional[RejectionCode] = Field(
        None, description="Machine-readable rejection reason"
    )
    rejection_reason: Optional[str] = Field(
        None, description="Human-readable explanation of a rejection"
    )
    strategy_event_id: Optional[str] = Field(
        None, description="Strategy event key used for duplicate protection"
    )
    idempotency_key: Optional[str] = Field(None, description="Caller idempotency key")
    signal_as_of: Optional[datetime] = Field(
        None,
        description="When the decision behind this order was taken; null for manual orders",
    )
    execution_price_policy: Optional[ExecutionPricePolicy] = Field(
        None, description="Fill reference policy; null for manual orders"
    )
    metadata: Optional[Dict[str, object]] = Field(
        None, description="Free-form audit metadata stored with the order"
    )
    created_at: Optional[datetime] = Field(None, description="Submission time")
    executed_at: Optional[datetime] = Field(None, description="Simulated fill time")


class TradeResponse(BaseModel):
    """An immutable execution record created for every filled order."""

    model_config = ConfigDict(from_attributes=True)

    trade_id: int = Field(..., description="Trade id")
    order_id: int = Field(..., description="Order that produced this execution")
    portfolio_id: int = Field(..., description="Owning portfolio id")
    stock_id: int = Field(..., description="Internal stock ID")
    symbol: Optional[str] = Field(None, description="Stock symbol, when known")
    side: OrderSide = Field(..., description="BUY or SELL")
    quantity: int = Field(..., description="Shares executed")
    execution_price: Decimal = Field(..., description="Simulated fill price")
    gross_value: Decimal = Field(..., description="quantity * execution_price")
    fees: Decimal = Field(..., description="Simulated fees charged")
    net_cash_impact: Decimal = Field(
        ..., description="Signed cash movement: negative for a buy, positive for a sell"
    )
    realized_pnl: Decimal = Field(
        ..., description="P&L booked by this execution, net of allocated entry and exit fees; 0 for a buy"
    )
    executed_at: Optional[datetime] = Field(None, description="Simulated execution time")


class PortfolioResponse(BaseModel):
    """Portfolio summary with cash, equity and P&L."""

    model_config = ConfigDict(from_attributes=True)

    portfolio_id: int = Field(..., description="Portfolio id")
    name: str = Field(..., description="Portfolio name")
    description: Optional[str] = Field(None, description="Portfolio description")
    trading_mode: str = Field("PAPER", description="Always PAPER: this is a simulation")
    status: PortfolioStatus = Field(..., description="ACTIVE, PAUSED or CLOSED")
    currency: str = Field(
        default_factory=default_currency,
        description=(
            "ISO-4217 code every monetary figure of this portfolio is denominated in. No "
            "conversion is performed: these figures are in this currency."
        ),
    )
    initial_capital: Decimal = Field(..., description="Starting virtual capital")
    current_cash: Decimal = Field(..., description="Available cash")
    market_value: Decimal = Field(..., description="Sum of open position market values")
    equity: Decimal = Field(..., description="current_cash + market_value")
    invested_capital: Decimal = Field(
        ..., description="Sum of the open positions' cost bases: capital currently at work"
    )
    exposure_pct: Optional[Decimal] = Field(
        None, description="market_value / equity * 100; null when equity is not positive"
    )
    realized_pnl: Decimal = Field(
        ..., description="Cumulative realised P&L, net of allocated entry and exit fees"
    )
    unrealized_pnl: Decimal = Field(
        ..., description="Sum of open-position unrealised P&L, before fees"
    )
    total_pnl: Decimal = Field(
        ..., description="equity - initial_capital; authoritative, net of all fees"
    )
    total_fees: Decimal = Field(..., description="Total simulated fees paid")
    open_entry_fees: Decimal = Field(
        ...,
        description=(
            "Entry fees already paid on shares still held. Realised P&L is net of exit fees "
            "and of the entry fees of the shares sold, so the books reconcile exactly as "
            "realized_pnl + unrealized_pnl - open_entry_fees == total_pnl"
        ),
    )
    return_pct: Decimal = Field(
        ..., description="(total_pnl / initial_capital) * 100; 0 when initial capital is 0"
    )
    position_count: int = Field(..., description="Number of open positions")
    order_count: int = Field(..., description="Number of orders submitted")
    trade_count: int = Field(..., description="Number of executions")
    transaction_count: int = Field(
        0,
        description=(
            "Entries in the append-only cash ledger. Equals the number of fills plus the number "
            "of deposits and withdrawals, and is 0 for a portfolio that has neither traded nor "
            "been funded beyond its opening balance."
        ),
    )
    trading_costs: TradingCostSettings = Field(..., description="Simulated cost model in force")
    risk_limits: RiskLimitSettings = Field(..., description="Configured risk limits")
    allocation_config: PortfolioAllocationConfig = Field(
        default_factory=PortfolioAllocationConfig,
        description="Portfolio construction and risk policy in force",
    )
    created_at: Optional[datetime] = Field(None, description="Creation time")
    updated_at: Optional[datetime] = Field(None, description="Last state change")
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class PortfolioValuationResponse(BaseModel):
    """Result of marking a portfolio to the supplied market prices."""

    model_config = ConfigDict(from_attributes=True)

    portfolio_id: int = Field(..., description="Portfolio id")
    as_of: datetime = Field(..., description="Valuation timestamp")
    current_cash: Decimal = Field(..., description="Cash, unchanged by a valuation")
    market_value: Decimal = Field(..., description="Sum of open position market values")
    equity: Decimal = Field(..., description="current_cash + market_value")
    realized_pnl: Decimal = Field(..., description="Cumulative realised P&L")
    unrealized_pnl: Decimal = Field(..., description="Sum of open-position unrealised P&L")
    total_pnl: Decimal = Field(..., description="equity - initial_capital")
    total_fees: Decimal = Field(..., description="Total simulated fees paid")
    open_entry_fees: Decimal = Field(
        ...,
        description=(
            "Entry fees already paid on shares still held; "
            "realized_pnl + unrealized_pnl - open_entry_fees == total_pnl"
        ),
    )
    return_pct: Decimal = Field(..., description="(total_pnl / initial_capital) * 100")
    positions: List[PositionResponse] = Field(
        default_factory=list, description="Updated open positions"
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class PortfolioSnapshotResponse(BaseModel):
    """One point-in-time equity-curve point."""

    model_config = ConfigDict(from_attributes=True)

    snapshot_id: int = Field(..., description="Snapshot id")
    portfolio_id: int = Field(..., description="Portfolio id")
    timestamp: datetime = Field(..., description="Snapshot timestamp")
    cash: Decimal = Field(..., description="Cash at the snapshot")
    market_value: Decimal = Field(..., description="Position market value at the snapshot")
    equity: Decimal = Field(..., description="cash + market_value")
    realized_pnl: Decimal = Field(..., description="Cumulative realised P&L at the snapshot")
    unrealized_pnl: Decimal = Field(..., description="Unrealised P&L at the snapshot")
    total_pnl: Decimal = Field(..., description="equity - initial_capital")
    total_fees: Decimal = Field(..., description="Cumulative simulated fees at the snapshot")
    return_pct: Decimal = Field(..., description="(total_pnl / initial_capital) * 100")
    drawdown_pct: Decimal = Field(
        ...,
        description=(
            "Signed drawdown from the running peak equity: 0 at a peak, negative below it "
            "(current - peak) / peak * 100"
        ),
    )
    exposure_pct: Optional[Decimal] = Field(
        None, description="market_value / equity * 100 at the snapshot"
    )
    peak_equity: Decimal = Field(..., description="Running peak equity at the snapshot")


#: The Phase 10 name for :class:`PortfolioSnapshotResponse` (equity-curve point).
EquitySnapshotResponse = PortfolioSnapshotResponse


class PortfolioPerformanceResponse(BaseModel):
    """Portfolio-level performance and activity summary."""

    model_config = ConfigDict(from_attributes=True)

    portfolio_id: int = Field(..., description="Portfolio id")
    name: str = Field(..., description="Portfolio name")
    status: PortfolioStatus = Field(..., description="Current portfolio status")
    currency: str = Field(
        default_factory=default_currency,
        description=(
            "ISO-4217 code every monetary figure below is denominated in. No conversion is "
            "performed."
        ),
    )
    as_of: datetime = Field(..., description="Time the figures were computed")
    initial_capital: Decimal = Field(..., description="Starting virtual capital")
    current_cash: Decimal = Field(..., description="Available cash")
    market_value: Decimal = Field(..., description="Sum of open position market values")
    equity: Decimal = Field(..., description="current_cash + market_value")
    realized_pnl: Decimal = Field(..., description="Cumulative realised P&L")
    unrealized_pnl: Decimal = Field(..., description="Sum of open-position unrealised P&L")
    total_pnl: Decimal = Field(..., description="equity - initial_capital")
    total_fees: Decimal = Field(..., description="Total simulated fees paid")
    open_entry_fees: Decimal = Field(
        ...,
        description=(
            "Entry fees already paid on shares still held; "
            "realized_pnl + unrealized_pnl - open_entry_fees == total_pnl"
        ),
    )
    return_pct: Decimal = Field(..., description="(total_pnl / initial_capital) * 100")
    position_count: int = Field(..., description="Open positions")
    order_count: int = Field(..., description="Orders submitted")
    filled_order_count: int = Field(..., description="Orders that executed")
    rejected_order_count: int = Field(..., description="Orders rejected before execution")
    trade_count: int = Field(..., description="Executions recorded")
    winning_trades: int = Field(..., description="Sells booked a positive realised P&L")
    losing_trades: int = Field(..., description="Sells booked a zero or negative realised P&L")
    snapshot_count: int = Field(..., description="Equity-curve snapshots recorded")
    peak_equity: Decimal = Field(
        ..., description="Highest equity ever recorded, including initial capital"
    )
    current_drawdown_pct: Decimal = Field(
        ..., description="Drawdown from peak equity as a percentage"
    )
    max_drawdown_pct: Decimal = Field(
        ..., description="Largest peak-to-trough drawdown observed across snapshots"
    )
    max_drawdown: Decimal = Field(
        ..., description="Largest peak-to-trough decline in currency"
    )
    exposure_pct: Optional[Decimal] = Field(
        None, description="market_value / equity * 100"
    )
    invested_capital: Decimal = Field(
        Decimal("0"), description="Sum of the open positions' cost bases"
    )
    win_rate_pct: Optional[Decimal] = Field(
        None,
        description="Percentage of closed round trips with positive realised P&L; null without trades",
    )
    closed_trades: int = Field(
        ..., description="Sells recorded: each one closes a round trip, so they are the sample size"
    )
    average_realized_pnl: Optional[Decimal] = Field(
        None, description="Mean realised P&L across closed sells; null when none closed"
    )
    average_winning_trade: Optional[Decimal] = Field(
        None, description="Mean realised P&L of winning sells; null when there are none"
    )
    average_losing_trade: Optional[Decimal] = Field(
        None, description="Mean realised P&L of losing sells; null when there are none"
    )
    profit_factor: Optional[Decimal] = Field(
        None,
        description="Gross profit / gross loss over closed sells; null when there are no losses",
    )
    return_observations: int = Field(
        ..., description="Equity-curve observations backing the volatility/Sharpe metrics"
    )
    annualized_volatility_pct: Optional[Decimal] = Field(
        None,
        description="Annualised volatility of daily portfolio returns; null with fewer than two returns",
    )
    sharpe_ratio: Optional[Decimal] = Field(
        None, description="Annualised Sharpe ratio; null when it cannot be meaningfully computed"
    )
    sortino_ratio: Optional[Decimal] = Field(
        None,
        description=(
            "Annualised Sortino ratio, which penalises only downside volatility. Null when "
            "there are no negative returns or not enough observations."
        ),
    )
    cagr_pct: Optional[Decimal] = Field(
        None,
        description=(
            "Compound annual growth rate of portfolio equity, in percent. Null until the "
            "equity curve spans at least a year — annualising a shorter window would report a "
            "misleading rate."
        ),
    )
    cagr_window_days: Optional[int] = Field(
        None,
        description="Days between the first and last equity observation used for the CAGR",
    )
    max_drawdown_pct_observed_over_days: Optional[int] = Field(
        None,
        description=(
            "Days spanned by the recorded equity curve the drawdown metrics are based on. Null "
            "until a second observation on a later day exists."
        ),
    )
    gross_exposure_pct: Optional[Decimal] = Field(
        None,
        description="Sum of the absolute position market values as a percentage of equity; "
        "equals exposure_pct while long-only",
    )
    net_exposure_pct: Optional[Decimal] = Field(
        None,
        description="Long market value minus short market value as a percentage of equity; "
        "equals gross_exposure_pct while long-only",
    )
    cash_pct: Optional[Decimal] = Field(
        None, description="cash as a percentage of equity; null when equity is not positive"
    )
    invested_pct: Optional[Decimal] = Field(
        None,
        description="Sum of open position cost bases as a percentage of equity; null when "
        "equity is not positive",
    )
    day_open_equity: Decimal = Field(
        Decimal("0"),
        description=(
            "Equity at the start of the current UTC day: the last snapshot recorded before "
            "today, the first snapshot recorded today, or the invested basis when neither "
            "exists. This is what the daily figures below are measured against."
        ),
    )
    daily_pnl: Decimal = Field(
        Decimal("0"),
        description="Profit or loss booked on the current UTC day: equity - day_open_equity. "
        "Signed, so a loss is negative",
    )
    daily_return_pct: Optional[Decimal] = Field(
        None,
        description="daily_pnl as a percentage of day_open_equity; null when the day opened at "
        "zero or below equity, because the ratio is then undefined",
    )
    insufficient_data: List[str] = Field(
        default_factory=list,
        description=(
            "Metrics left null because the portfolio has too little history, each naming the "
            "observation it needs. A non-empty list is the explicit 'insufficient data' "
            "state; it never means the metrics are zero."
        ),
    )
    risk_free_rate_pct: Decimal = Field(
        Decimal("0"), description="Annual risk-free rate assumed for the Sharpe ratio, in percent"
    )
    positions: List[PositionResponse] = Field(
        default_factory=list, description="Open positions with their allocation"
    )
    risk_limits: RiskLimitSettings = Field(..., description="Configured risk limits")
    allocation_config: PortfolioAllocationConfig = Field(
        default_factory=PortfolioAllocationConfig,
        description="Portfolio construction and risk policy in force",
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def average_gain(self) -> Optional[Decimal]:
        """Mean realised P&L of the winning closed round trips.

        An alias of ``average_winning_trade``: both names are reported from the one figure, so
        a caller can use whichever term matches its vocabulary without the engine keeping two
        copies of the same number.
        """
        return self.average_winning_trade

    @computed_field  # type: ignore[prop-decorator]
    @property
    def average_loss(self) -> Optional[Decimal]:
        """Mean realised P&L of the losing closed round trips (alias of ``average_losing_trade``)."""
        return self.average_losing_trade


class PortfolioSummary(BaseModel):
    """Everything a caller needs to size or risk-check a trade against a portfolio.

    This is the Phase 10 name for the portfolio summary: it adds the exposure and
    invested-capital figures the risk engine measures against, so a caller never has to
    derive them from the raw cash/market-value pair.
    """

    model_config = ConfigDict(from_attributes=True)

    portfolio_id: int = Field(..., description="Portfolio id")
    name: str = Field(..., description="Portfolio name")
    description: Optional[str] = Field(None, description="Portfolio description")
    trading_mode: str = Field("PAPER", description="Always PAPER: this is a simulation")
    status: PortfolioStatus = Field(..., description="ACTIVE, PAUSED or CLOSED")
    base_currency: str = Field(
        default_factory=default_currency,
        description=(
            "Currency all monetary figures are denominated in, taken from the portfolio. No "
            "conversion is performed."
        ),
    )
    initial_capital: Decimal = Field(..., description="Starting virtual capital")
    current_cash: Decimal = Field(..., description="Available cash")
    market_value: Decimal = Field(..., description="Sum of open position market values")
    equity: Decimal = Field(..., description="current_cash + market_value")
    invested_capital: Decimal = Field(
        ..., description="Sum of the open positions' cost bases: capital currently at work"
    )
    exposure_pct: Optional[Decimal] = Field(
        None, description="market_value / equity * 100"
    )
    realized_pnl: Decimal = Field(..., description="Cumulative realised P&L")
    unrealized_pnl: Decimal = Field(..., description="Sum of open-position unrealised P&L")
    total_pnl: Decimal = Field(..., description="equity - initial_capital")
    return_pct: Decimal = Field(..., description="(total_pnl / initial_capital) * 100")
    position_count: int = Field(..., description="Open positions")
    order_count: int = Field(..., description="Orders submitted")
    trade_count: int = Field(..., description="Executions recorded")
    peak_equity: Decimal = Field(..., description="Highest equity ever recorded")
    current_drawdown_pct: Decimal = Field(
        ..., description="Drawdown from peak equity as a positive percentage"
    )
    risk_limits: RiskLimitSettings = Field(
        default_factory=RiskLimitSettings,
        description="Limits in force for this portfolio, with unset rules disabled",
    )
    allocation_config: PortfolioAllocationConfig = Field(
        default_factory=PortfolioAllocationConfig,
        description="Portfolio construction and risk policy in force",
    )
    created_at: Optional[datetime] = Field(None, description="Creation time")
    updated_at: Optional[datetime] = Field(None, description="Last state change")
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class DrawdownPointResponse(BaseModel):
    """One point of the drawdown curve."""

    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime = Field(..., description="Snapshot timestamp")
    equity: Decimal = Field(..., description="Equity at that point")
    peak_equity: Decimal = Field(..., description="Running peak equity at that point")
    drawdown_pct: Decimal = Field(
        ..., description="Signed drawdown: 0 at a peak, negative below it"
    )
    exposure_pct: Optional[Decimal] = Field(None, description="Exposure at that point")


class PortfolioDrawdownResponse(BaseModel):
    """Drawdown of a portfolio: current, maximum and the full curve.

    Percentages are reported in both conventions on purpose. ``*_pct`` is the **positive
    magnitude** of the decline (the Phase 8 / performance convention), while
    ``*_signed_pct`` is ``(current - peak) / peak * 100`` and is therefore ``0`` at a peak
    and negative below it. Reporting both removes the ambiguity of an unsigned percentage
    without changing any existing field.
    """

    portfolio_id: int = Field(..., description="Portfolio id")
    as_of: datetime = Field(..., description="Evaluation timestamp")
    peak_equity: Decimal = Field(..., description="Highest equity ever recorded")
    current_equity: Decimal = Field(..., description="Equity right now")
    current_drawdown_pct: Decimal = Field(
        ..., description="Current drawdown from peak as a positive percentage"
    )
    current_drawdown_signed_pct: Decimal = Field(
        ..., description="Current drawdown as (current - peak) / peak * 100"
    )
    max_drawdown: Decimal = Field(..., description="Largest peak-to-trough decline in currency")
    max_drawdown_pct: Decimal = Field(
        ..., description="Largest peak-to-trough decline as a positive percentage"
    )
    max_drawdown_signed_pct: Decimal = Field(
        ..., description="Largest peak-to-trough decline as (trough - peak) / peak * 100"
    )
    peak_timestamp: Optional[datetime] = Field(
        None, description="Timestamp of the peak that precedes the maximum drawdown"
    )
    trough_timestamp: Optional[datetime] = Field(
        None, description="Timestamp of the trough of the maximum drawdown"
    )
    points: List[DrawdownPointResponse] = Field(
        default_factory=list, description="The drawdown curve, oldest first"
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class PaperRunOrderResponse(BaseModel):
    """One order produced by a sequential historical paper run."""

    model_config = ConfigDict(from_attributes=True)

    order_id: Optional[int] = Field(None, description="Persisted order id")
    trade_id: Optional[int] = Field(None, description="Execution record id, when the order filled")
    stock_id: int = Field(..., description="Internal stock ID")
    side: OrderSide = Field(..., description="BUY or SELL")
    quantity: int = Field(..., description="Shares")
    status: OrderStatus = Field(..., description="Resulting order status")
    signal_date: Optional[datetime] = Field(
        ..., description="Bar whose close produced the decision"
    )
    executed_at: Optional[datetime] = Field(
        ..., description="Bar whose price was used for the fill; never earlier than signal_date"
    )
    reference_price: Decimal = Field(..., description="Price of the signal bar used for the decision")
    execution_price: Decimal = Field(..., description="Actual simulated fill price")
    fees: Decimal = Field(..., description="Simulated fees charged")
    realized_pnl: Decimal = Field(..., description="P&L booked by this execution")
    rejection_code: Optional[RejectionCode] = Field(None, description="Rejection reason code")
    rejection_reason: Optional[str] = Field(None, description="Rejection explanation")
    cash_after: Decimal = Field(..., description="Cash after this execution")
    equity_after: Decimal = Field(..., description="Equity after this execution")


class PaperRunResponse(BaseModel):
    """Outcome of a sequential historical paper run against stored market data."""

    portfolio_id: int = Field(..., description="Portfolio id the run traded")
    stock_id: int = Field(..., description="Internal stock ID replayed")
    symbol: str = Field(..., description="Stock symbol")
    strategy: str = Field(..., description="Strategy name and version in force")
    start_date: Optional[date_type] = Field(None, description="First recorded bar")
    end_date: Optional[date_type] = Field(None, description="Last recorded bar")
    execution_price_policy: ExecutionPricePolicy = Field(
        ..., description="Fill reference policy actually applied"
    )
    bars_processed: int = Field(..., description="Recorded bars the engine stepped through")
    skipped_bars: int = Field(
        0,
        description=(
            "Earlier bars loaded only to warm the indicators. They were never traded, so a run "
            "with many skipped bars and few orders had too little evaluable history to signal on."
        ),
    )
    orders: List[PaperRunOrderResponse] = Field(
        default_factory=list, description="Orders the run produced, chronologically"
    )
    performance: Optional[PortfolioPerformanceResponse] = Field(
        None, description="Portfolio performance after the run"
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class OrderExecutionResponse(BaseModel):
    """Result of submitting an order: the order plus its trade when it filled."""

    model_config = ConfigDict(from_attributes=True)

    order: OrderResponse = Field(..., description="The simulated order")
    trade: Optional[TradeResponse] = Field(
        None, description="The execution record; null when the order was rejected"
    )
    portfolio: PortfolioResponse = Field(..., description="Portfolio state after the submission")
    idempotent_replay: bool = Field(
        False, description="True when an existing order was returned instead of executing again"
    )


class StrategyOrderResponse(BaseModel):
    """Outcome of running the existing Phase 7/8 strategy rules for one stock.

    The strategy logic is reused unchanged from Phase 8; this endpoint only translates its
    ``enter`` / ``exit`` / ``hold`` decision into a simulated paper order.
    """

    model_config = ConfigDict(from_attributes=True)

    portfolio_id: int = Field(..., description="Portfolio id")
    stock_id: int = Field(..., description="Internal stock ID")
    symbol: Optional[str] = Field(None, description="Stock symbol")
    strategy: str = Field(..., description="Strategy name and version in force")
    action: StrategyAction = Field(..., description="enter, exit or hold")
    composite_score: int = Field(
        ..., ge=-100, le=100, description="Phase 7 composite alignment score"
    )
    signal_indicators: List[str] = Field(
        default_factory=list, description="Indicators that produced the signals"
    )
    as_of: Optional[str] = Field(
        None, description="Trading date of the data the decision was made from"
    )
    reason: str = Field(..., description="Why the engine did or did not place an order")
    order: Optional[OrderResponse] = Field(
        None, description="The paper order created, when the decision produced one"
    )
    trade: Optional[TradeResponse] = Field(
        None, description="The execution record, when the order filled"
    )
    idempotent_replay: bool = Field(
        False,
        description="True when the decision had already been processed and the original order was returned",
    )
    portfolio: PortfolioResponse = Field(..., description="Portfolio state after the decision")
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


# --- Envelopes ---------------------------------------------------------------


class PortfolioListResponse(BaseModel):
    """Envelope listing portfolios."""

    success: bool = True
    data: List[PortfolioResponse]
    count: int


class PortfolioPositionsResponse(BaseModel):
    """Envelope listing open positions."""

    success: bool = True
    data: List[PositionResponse]
    count: int
    portfolio_id: int


class PortfolioOrdersResponse(BaseModel):
    """Envelope listing order history, newest first."""

    success: bool = True
    data: List[OrderResponse]
    count: int
    total: int
    portfolio_id: int


class PortfolioTradesResponse(BaseModel):
    """Envelope listing trade history, newest first."""

    success: bool = True
    data: List[TradeResponse]
    count: int
    total: int
    portfolio_id: int


class PortfolioSnapshotsResponse(BaseModel):
    """Envelope listing the equity curve, oldest first."""

    success: bool = True
    data: List[PortfolioSnapshotResponse]
    count: int
    total: int
    portfolio_id: int


class PortfolioErrorResponse(BaseModel):
    """Error response shape for portfolio endpoints."""

    success: bool = False
    error: Dict[str, object] = Field(..., description="Error details")