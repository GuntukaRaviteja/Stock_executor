"""Internal, engine-facing domain models for the Phase 9 paper-trading engine.

These dataclasses are deliberately separate from the SQLAlchemy models and the Pydantic
response schemas, exactly as Phase 8 does it:

* :class:`TradingCostConfig` / :class:`RiskLimits` are the centralised, deterministic
  configuration of one portfolio — no magic numbers anywhere in the engine;
* :class:`PositionState` / :class:`PortfolioAccount` are the runtime state an order is
  validated and applied to;
* :class:`OrderIntent` is a request to execute;
* :class:`ExecutionPlan` is the result: either an accepted fill with all its derived
  figures, or a rejection with a stable code and an explanation;
* :class:`PortfolioValuation` / :class:`PortfolioSnapshotValue` are pure value objects
  produced by the valuation engine.

Every monetary value is a :class:`decimal.Decimal`, and rounding is delegated to the Phase 8
quantisation helpers so Phase 8 and Phase 9 cannot drift apart on precision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, Optional

from app.core.logging import logger
from app.schemas.portfolio import (
    DecisionAction,
    ExecutionPricePolicy,
    OrderSide,
    OrderSource,
    OrderStatus,
    OrderType,
    PortfolioStatus,
    PositionSizingMethod,
    RejectionCode,
    RiskAction,
)
from app.services.backtest.models import quantize_money

#: Absolute zero used for sign comparisons of money amounts.
ZERO = Decimal("0")
#: One hundred, the scale of every percentage field in this package.
_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class TradingCostConfig:
    """Deterministic simulated execution costs.

    ``commission_rate`` is a fraction of gross value (``0.001`` = 0.1%). ``min_fee`` is the
    floor charged per side. ``slippage_rate`` is applied adversely to the fill price: a buy
    fills above the requested price and a sell below it, mirroring Phase 8 semantics.
    """

    commission_rate: Decimal = Decimal("0.001")
    min_fee: Decimal = ZERO
    slippage_rate: Decimal = ZERO

    def validate(self) -> None:
        if self.commission_rate < ZERO:
            raise ValueError("commission_rate must not be negative")
        if self.min_fee < ZERO:
            raise ValueError("min_fee must not be negative")
        if self.slippage_rate < ZERO:
            raise ValueError("slippage_rate must not be negative")


@dataclass(frozen=True)
class RiskLimits:
    """Deterministic portfolio risk limits.

    ``None`` disables the corresponding rule, which keeps "no limit" distinguishable from
    "a limit of zero" and keeps the engine explainable.

    Limits fall into two groups:

    * *position / exposure* rules — ``max_position_quantity``, ``max_position_value``,
      ``max_position_allocation_pct``, ``max_exposure_pct``, ``max_positions``;
    * *capital* rules — ``max_trade_allocation_pct``, ``min_cash_reserve_pct``,
      ``min_trade_value``;
    * plus the two optional equity-guard rules, ``max_drawdown_pct`` and
      ``max_daily_loss_pct``, which block new buys once the portfolio has fallen too far from its
      peak equity, or has lost too much on the current day.

    ``min_trade_value`` is the only limit that applies to a sell as well as a buy: it does not
    describe growth or risk, it describes whether an order is worth executing at all. Every other
    limit except ``max_drawdown_pct``, ``max_daily_loss_pct`` and ``min_cash_reserve_pct`` only
    ever blocks a BUY: a sell always reduces risk and must never be blocked by a growth rule.
    """

    max_position_quantity: Optional[int] = None
    max_position_value: Optional[Decimal] = None
    max_position_allocation_pct: Optional[Decimal] = None
    max_drawdown_pct: Optional[Decimal] = None
    #: Loss on the current UTC day, in percent of that day's opening equity, above which new
    #: BUY orders are blocked. Disabled unless configured, and never blocks a sell.
    max_daily_loss_pct: Optional[Decimal] = None
    #: Smallest gross value any order may commit. Applies to buys and sells alike.
    min_trade_value: Optional[Decimal] = None
    #: Maximum share of portfolio equity that may be invested across all open positions.
    max_exposure_pct: Optional[Decimal] = None
    #: Maximum share of portfolio equity a single trade may commit.
    max_trade_allocation_pct: Optional[Decimal] = None
    #: Share of equity that must stay in cash; a buy that would breach it is rejected.
    min_cash_reserve_pct: Optional[Decimal] = None
    #: Maximum number of simultaneously open positions.
    max_positions: Optional[int] = None

    def validate(self) -> None:
        if self.max_position_quantity is not None and self.max_position_quantity < 1:
            raise ValueError("max_position_quantity must be at least 1 when set")
        if self.max_positions is not None and self.max_positions < 1:
            raise ValueError("max_positions must be at least 1 when set")
        for name in (
            "max_position_value",
            "max_position_allocation_pct",
            "max_drawdown_pct",
            "max_daily_loss_pct",
            "max_exposure_pct",
            "max_trade_allocation_pct",
            "min_cash_reserve_pct",
            "min_trade_value",
        ):
            value = getattr(self, name)
            if value is not None and value <= ZERO:
                raise ValueError(f"{name} must be greater than zero when set")
        for name in (
            "max_position_allocation_pct",
            "max_drawdown_pct",
            "max_daily_loss_pct",
            "max_exposure_pct",
            "max_trade_allocation_pct",
            "min_cash_reserve_pct",
        ):
            value = getattr(self, name)
            if value is not None and value > Decimal("100"):
                raise ValueError(f"{name} must be in (0, 100] when set")


@dataclass(frozen=True)
class PositionState:
    """Runtime state of one open long position."""

    stock_id: int
    quantity: int
    average_entry_price: Decimal
    cost_basis: Decimal
    entry_fees_total: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    current_price: Optional[Decimal] = None

    def market_value(self) -> Decimal:
        if self.current_price is None:
            return ZERO
        return quantize_money(self.current_price * Decimal(self.quantity))


@dataclass(frozen=True)
class PortfolioAccount:
    """Runtime state a single order is validated and applied against.

    ``total_market_value`` covers *every* open position in the portfolio, not only the one
    the order touches, because portfolio-level risk rules are measured against total equity.

    ``positions`` carries every open position, so a caller working through several stocks from
    one load — the paper engine executing a run's decisions — can ask about any of them without
    re-reading. ``position`` is the one the caller *named*, and is ``None`` when it named none.
    """

    portfolio_id: int
    name: str
    status: PortfolioStatus
    initial_capital: Decimal
    cash: Decimal
    realized_pnl: Decimal
    total_market_value: Decimal
    position: Optional[PositionState] = None
    #: Number of open positions in the portfolio, used by the ``max_positions`` rule.
    position_count: int = 0
    #: Every open position, in load order.
    positions: tuple = ()

    @property
    def equity(self) -> Decimal:
        """``cash + total_market_value``."""
        return quantize_money(self.cash + self.total_market_value)

    def position_for(self, stock_id: int) -> Optional[PositionState]:
        """The open position for ``stock_id``, or ``None`` when the portfolio is flat."""
        for position in self.positions:
            if position.stock_id == stock_id:
                return position
        if self.position is not None and self.position.stock_id == stock_id:
            return self.position
        return None


@dataclass(frozen=True)
class OrderIntent:
    """A request to execute a simulated market order.

    ``signal_as_of`` records when the decision that produced this order was taken and
    ``execution_price_policy`` records which price of which bar was used as the fill
    reference. Both are ``None`` for a manual order, where the caller states the execution
    price directly. Keeping them on the intent — and therefore on the persisted order — is
    what makes a historical fill auditable: the signal timestamp always precedes the
    execution timestamp.
    """

    portfolio_id: int
    stock_id: int
    side: OrderSide
    quantity: int
    requested_price: Decimal
    order_type: OrderType = OrderType.MARKET
    source: OrderSource = OrderSource.MANUAL
    idempotency_key: Optional[str] = None
    strategy_event_id: Optional[str] = None
    signal_as_of: Optional[datetime] = None
    execution_price_policy: Optional[ExecutionPricePolicy] = None
    executed_at: Optional[datetime] = None
    metadata: Optional[dict] = None


@dataclass(frozen=True)
class Rejection:
    """Why an order was not executed. Carries no portfolio state change."""

    code: RejectionCode
    reason: str
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionPlan:
    """The deterministic result of processing one order intent.

    ``accepted`` decides the outcome. When it is ``True`` every derived figure is filled in
    (fill price, fees, cash movement, resulting position). When it is ``False`` only
    ``rejection`` is populated and no cash, position or P&L field is modified.
    """

    accepted: bool
    stock_id: int
    side: OrderSide
    quantity: int
    order_status: OrderStatus
    execution_price: Decimal = ZERO
    gross_value: Decimal = ZERO
    fees: Decimal = ZERO
    net_cash_impact: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    cash_before: Decimal = ZERO
    cash_after: Decimal = ZERO
    position_before: Optional[PositionState] = None
    position_after: Optional[PositionState] = None
    rejection: Optional[Rejection] = None
    risk_rule: Optional[str] = None
    resulting_allocation_pct: Optional[Decimal] = None

    @classmethod
    def reject(
        cls,
        intent: OrderIntent,
        rejection: Rejection,
        account: PortfolioAccount,
        risk_rule: Optional[str] = None,
    ) -> "ExecutionPlan":
        """Build a rejected plan. Nothing about the account is changed."""
        return cls(
            accepted=False,
            stock_id=intent.stock_id,
            side=intent.side,
            quantity=intent.quantity,
            order_status=OrderStatus.REJECTED,
            cash_before=account.cash,
            cash_after=account.cash,
            position_before=account.position_for(intent.stock_id),
            position_after=account.position_for(intent.stock_id),
            rejection=rejection,
            risk_rule=risk_rule,
        )


@dataclass(frozen=True)
class PositionValuation:
    """Mark-to-market view of one position, used by responses and snapshots."""

    stock_id: int
    quantity: int
    average_entry_price: Decimal
    current_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    entry_fees_total: Decimal
    cost_basis: Decimal

    def allocation_pct(self, equity: Decimal) -> Optional[Decimal]:
        """Share of portfolio equity this position represents."""
        from app.services.portfolio.calculator import allocation_pct

        return allocation_pct(self.market_value, equity)


@dataclass(frozen=True)
class PortfolioValuation:
    """Complete mark-to-market view of a portfolio at one point in time."""

    portfolio_id: int
    initial_capital: Decimal
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    total_fees: Decimal
    #: Entry fees already paid on shares still held. Subtracting them reconciles the books:
    #: ``realized + unrealized - open_entry_fees == total_pnl``.
    open_entry_fees: Decimal
    return_pct: Decimal
    #: Sum of the open positions' cost bases — the capital currently at work.
    invested_capital: Decimal = ZERO
    #: ``market_value / equity * 100``; ``None`` when equity is not positive.
    exposure_pct: Optional[Decimal] = None
    positions: tuple[PositionValuation, ...] = ()

    def position(self, stock_id: int) -> Optional[PositionValuation]:
        for valuation in self.positions:
            if valuation.stock_id == stock_id:
                return valuation
        return None


@dataclass(frozen=True)
class RiskViolation:
    """One risk rule a proposed trade breaks.

    ``rule`` is the stable machine-readable rule name (``max_exposure_pct``, ...) and
    ``reason`` explains the numbers behind the breach.
    """

    rule: str
    reason: str
    limit: Optional[Decimal] = None
    observed: Optional[Decimal] = None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RiskAssessment:
    """The outcome of evaluating a proposed trade against a portfolio's risk limits.

    ``accepted`` is ``True`` only when ``violations`` is empty. The assessment is a pure
    reading of the portfolio state plus the proposed trade: it never mutates anything, so it
    is always safe to ask "would this order be accepted?" before submitting it.
    """

    portfolio_id: int
    accepted: bool
    violations: tuple[RiskViolation, ...] = ()
    equity: Decimal = ZERO
    cash: Decimal = ZERO
    peak_equity: Decimal = ZERO
    current_drawdown_pct: Decimal = ZERO
    exposure_pct: Optional[Decimal] = None
    position_count: int = 0
    limits: Optional[RiskLimits] = None

    @property
    def rules(self) -> tuple[str, ...]:
        """Machine-readable names of the broken rules."""
        return tuple(violation.rule for violation in self.violations)


@dataclass(frozen=True)
class PositionSizePlan:
    """The result of a position-sizing request.

    ``quantity`` is always a whole number of shares floored from the requested budget, and
    ``capital`` is what that quantity actually commits. A plan with ``quantity == 0`` is
    never silently rounded up into a trade.
    """

    method: PositionSizingMethod
    quantity: int
    price: Decimal
    capital: Decimal = ZERO
    budget: Decimal = ZERO
    requested: Optional[Decimal] = None
    equity: Decimal = ZERO
    risk_per_share: Optional[Decimal] = None
    allowed_risk: Optional[Decimal] = None
    reason: str = ""


@dataclass(frozen=True)
class DrawdownPoint:
    """One point of the drawdown curve."""

    timestamp: datetime
    equity: Decimal
    peak_equity: Decimal
    #: Signed drawdown from the running peak: ``0`` at a peak, negative below it.
    drawdown_pct: Decimal
    exposure_pct: Optional[Decimal] = None


@dataclass(frozen=True)
class PortfolioSnapshotValue:
    """One equity-curve point built from a valuation."""

    portfolio_id: int
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    total_fees: Decimal
    return_pct: Decimal
    #: Signed drawdown from the running peak at this point: ``0`` at a peak, negative below.
    drawdown_pct: Decimal = ZERO
    #: ``market_value / equity * 100`` at this point; ``None`` when equity is not positive.
    exposure_pct: Optional[Decimal] = None
    peak_equity: Decimal = ZERO


@dataclass(frozen=True)
class PortfolioPerformance:
    """Portfolio-level performance and activity summary.

    Purely descriptive: it is a reading of the simulated ledger, not a prediction or a
    recommendation.
    """

    portfolio_id: int
    name: str
    status: PortfolioStatus
    initial_capital: Decimal
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    total_fees: Decimal
    open_entry_fees: Decimal
    return_pct: Decimal
    position_count: int = 0
    order_count: int = 0
    filled_order_count: int = 0
    rejected_order_count: int = 0
    trade_count: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    snapshot_count: int = 0
    peak_equity: Decimal = ZERO
    current_drawdown_pct: Decimal = ZERO
    max_drawdown: Decimal = ZERO
    max_drawdown_pct: Decimal = ZERO
    #: ``market_value / equity * 100``; ``None`` when equity is not positive.
    exposure_pct: Optional[Decimal] = None
    #: Sum of the open positions' cost bases: capital currently at work.
    invested_capital: Decimal = ZERO
    #: Percentage of closed round trips with positive realised P&L; ``None`` without trades.
    win_rate_pct: Optional[Decimal] = None
    #: Sells recorded. Each sell closes a round trip, so this is the trade-statistics sample.
    closed_trades: int = 0
    #: Mean realised P&L across closed sells; ``None`` when none closed.
    average_realized_pnl: Optional[Decimal] = None
    #: Mean realised P&L of winning sells; ``None`` when there are none.
    average_winning_trade: Optional[Decimal] = None
    #: Mean realised P&L of losing sells; ``None`` when there are none.
    average_losing_trade: Optional[Decimal] = None
    #: Gross profit / gross loss over closed sells; ``None`` when undefined.
    profit_factor: Optional[Decimal] = None
    #: Number of equity-curve observations the return-based metrics are based on.
    return_observations: int = 0
    #: Annualised volatility of daily portfolio returns; ``None`` with fewer than 2 returns.
    annualized_volatility_pct: Optional[Decimal] = None
    #: Annualised Sharpe ratio; ``None`` when it cannot be meaningfully computed.
    sharpe_ratio: Optional[Decimal] = None
    #: Annualised Sortino ratio (downside volatility only); ``None`` without negative returns.
    sortino_ratio: Optional[Decimal] = None
    #: Compound annual growth rate of equity, in percent; ``None`` for a window under a year.
    cagr_pct: Optional[Decimal] = None
    #: Days between the first and last recorded equity observation; the CAGR window.
    cagr_window_days: Optional[int] = None
    #: Days spanned by the recorded equity curve the drawdown metrics are based on.
    max_drawdown_pct_observed_over_days: Optional[int] = None
    #: Sum of the absolute position market values as a percentage of equity. Equals
    #: ``exposure_pct`` while long-only; reported separately so long/short support later needs
    #: no change to this shape.
    gross_exposure_pct: Optional[Decimal] = None
    #: Long market value minus short market value as a percentage of equity. Equals
    #: ``gross_exposure_pct`` while long-only.
    net_exposure_pct: Optional[Decimal] = None
    #: Cash as a percentage of equity; ``None`` when equity is not positive.
    cash_pct: Optional[Decimal] = None
    #: Invested capital as a percentage of equity; ``None`` when equity is not positive.
    invested_pct: Optional[Decimal] = None
    #: Equity at the start of the current UTC day the daily figures below are measured from:
    #: the last snapshot recorded before today, the first snapshot recorded today, or the
    #: invested basis when the portfolio has recorded neither.
    day_open_equity: Decimal = ZERO
    #: Profit or loss booked on the current UTC day: ``equity - day_open_equity``. Signed.
    daily_pnl: Decimal = ZERO
    #: ``daily_pnl`` as a percentage of ``day_open_equity``; ``None`` when the day opened at
    #: zero or below equity, because the ratio is then undefined.
    daily_return_pct: Optional[Decimal] = None
    #: Metrics left null because the portfolio has too little history, each naming what it needs.
    insufficient_data: tuple = ()
    #: Yearly risk-free rate used for the Sharpe ratio, in percent.
    risk_free_rate_pct: Decimal = ZERO
    positions: tuple[PositionValuation, ...] = ()


@dataclass(frozen=True)
class StrategyOrderPlan:
    """What the Phase 7/8 strategy rules decided, and the order it implies.

    ``intent`` is ``None`` when the rules produced no order (``hold``), or when an entry
    could not be sized with the cash available.
    """

    stock_id: int
    action: str
    composite_score: int
    signal_indicators: list[str]
    as_of: Optional[str]
    reason: str
    intent: Optional[OrderIntent] = None
    strategy_name: str = ""


@dataclass(frozen=True)
class PaperRunOrder:
    """One order produced by a sequential historical paper run.

    ``signal_date`` and ``executed_at`` are recorded separately so a reader can verify the
    execution never happened before the information that justified it existed.
    """

    order_id: Optional[int]
    trade_id: Optional[int]
    stock_id: int
    side: OrderSide
    quantity: int
    status: OrderStatus
    signal_date: Optional[datetime]
    executed_at: Optional[datetime]
    reference_price: Decimal
    execution_price: Decimal = ZERO
    fees: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    rejection_code: Optional[RejectionCode] = None
    rejection_reason: Optional[str] = None
    cash_after: Decimal = ZERO
    equity_after: Decimal = ZERO


@dataclass(frozen=True)
class PaperRunResult:
    """Everything one sequential historical paper run produced."""

    portfolio_id: int
    stock_id: int
    symbol: str
    strategy: str
    start_date: Optional[date]
    end_date: Optional[date]
    execution_price_policy: ExecutionPricePolicy
    bars_processed: int
    orders: tuple[PaperRunOrder, ...] = ()
    snapshots: int = 0
    #: Bars loaded only to warm the indicators. They are never traded or recorded, so this
    #: explains a run whose series was long enough to analyse but short enough to signal on.
    skipped_bars: int = 0


# --- Phase 11: portfolio construction & risk --------------------------------


@dataclass(frozen=True)
class AllocationConfig:
    """The portfolio construction policy, as the engine uses it.

    The runtime twin of ``app.schemas.portfolio.PortfolioAllocationConfig``. It exists for the
    same reason :class:`RiskLimits` and :class:`TradingCostConfig` do: the engine owns its own
    validation, and the wire model is the one that produces the OpenAPI contract and the 422s.

    Every percentage is in percent (``20`` means 20%). ``None`` disables the corresponding rule,
    which keeps "no limit" distinguishable from "a limit of zero".
    """

    sizing_method: PositionSizingMethod = PositionSizingMethod.EQUAL_WEIGHT
    max_position_weight_pct: Decimal = Decimal("20")
    max_sector_weight_pct: Optional[Decimal] = None
    max_positions: Optional[int] = None
    max_exposure_pct: Optional[Decimal] = None
    max_position_loss_pct: Optional[Decimal] = None
    stop_loss_pct: Optional[Decimal] = None
    take_profit_pct: Optional[Decimal] = None
    rebalance_threshold_pct: Decimal = Decimal("0.5")
    min_position_value: Optional[Decimal] = None
    risk_per_trade_pct: Decimal = Decimal("1")
    cash_reserve_pct: Decimal = Decimal("10")
    target_position_count: Optional[int] = None

    def validate(self) -> None:
        """Reject a configuration the engine cannot act on, before any calculation runs."""
        if self.max_position_weight_pct <= ZERO or self.max_position_weight_pct > _HUNDRED:
            raise ValueError("max_position_weight_pct must be in (0, 100]")
        if self.risk_per_trade_pct <= ZERO or self.risk_per_trade_pct > _HUNDRED:
            raise ValueError("risk_per_trade_pct must be in (0, 100]")
        if self.cash_reserve_pct < ZERO or self.cash_reserve_pct > _HUNDRED:
            raise ValueError("cash_reserve_pct must be in [0, 100]")
        if self.rebalance_threshold_pct < ZERO or self.rebalance_threshold_pct > _HUNDRED:
            raise ValueError("rebalance_threshold_pct must be in [0, 100]")
        for name in (
            "max_sector_weight_pct",
            "max_exposure_pct",
            "max_position_loss_pct",
            "stop_loss_pct",
            "take_profit_pct",
        ):
            value = getattr(self, name)
            if value is not None and (value <= ZERO or value > _HUNDRED):
                raise ValueError(f"{name} must be in (0, 100] when set")
        if self.min_position_value is not None and self.min_position_value <= ZERO:
            raise ValueError("min_position_value must be greater than zero when set")
        if self.max_positions is not None and self.max_positions < 1:
            raise ValueError("max_positions must be at least 1 when set")
        if (
            self.target_position_count is not None
            and self.target_position_count < 1
        ):
            raise ValueError("target_position_count must be at least 1 when set")
        if (
            self.sizing_method is PositionSizingMethod.RISK_BASED
            and self.stop_loss_pct is None
        ):
            raise ValueError(
                "RISK_BASED sizing requires stop_loss_pct: without a stop there is no "
                "per-share risk, so no position size can be derived"
            )

    def replace(self, **changes) -> "AllocationConfig":
        """A copy with the given fields replaced — how a per-request override is applied."""
        from dataclasses import replace as _replace

        return _replace(self, **changes)


@dataclass(frozen=True)
class Candidate:
    """A stock offered to the construction engine, with everything needed to size it.

    ``eligible`` and ``ineligible_reason`` exist so a candidate that cannot be held is still
    *reported*: "which stocks were considered, and why was each one skipped" is part of an
    explainable decision, and silently dropping an unpriceable candidate would hide exactly
    the case a caller needs to see.
    """

    stock_id: int
    price: Optional[Decimal] = None
    composite_score: int = 0
    symbol: Optional[str] = None
    sector: Optional[str] = None
    eligible: bool = True
    ineligible_reason: Optional[str] = None

    def sort_key(self) -> tuple:
        """The single deterministic ordering used for candidates.

        Composite score descending, then stock id ascending. Stock id is unique, so the
        ordering is total: two runs over the same candidates can never swap positions, and
        equal scores never depend on insertion order or on dictionary iteration order.
        """
        return (-self.composite_score, self.stock_id)


@dataclass(frozen=True)
class PositionRisk:
    """Risk figures for one open position, as computed by the risk engine."""

    stock_id: int
    quantity: int
    average_entry_price: Decimal
    current_price: Decimal
    market_value: Decimal
    weight_pct: Optional[Decimal]
    unrealized_pnl: Decimal
    unrealized_pnl_pct: Optional[Decimal]
    stop_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None
    stop_loss_breached: bool = False
    take_profit_triggered: bool = False
    #: Loss the position would book if it fell to its stop. Never negative: a position above
    #: its stop carries no downside by that measure.
    risk_amount: Decimal = ZERO
    risk_contribution_pct: Optional[Decimal] = None
    sector: Optional[str] = None
    symbol: Optional[str] = None


@dataclass(frozen=True)
class RiskRecommendation:
    """A risk-driven action the engine recommends for one position.

    A recommendation never mutates anything. It travels to the construction engine, which folds
    it into a :class:`PortfolioDecision`, and it becomes an order only when the caller asks for
    the decisions to be applied.
    """

    stock_id: int
    action: RiskAction
    quantity: int
    price: Decimal
    rule: str
    reason: str
    symbol: Optional[str] = None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PortfolioRiskReport:
    """The portfolio-level risk reading the Phase 11 risk engine produces.

    ``violations`` states what the portfolio is *currently* outside of; ``recommendations``
    states what the engine would do about it. Keeping them separate is what lets a caller see a
    portfolio that is over-exposed without the engine having traded anything.

    Exposure is reported both gross and net. For the long-only engine of this phase they are
    equal, and they are kept as separate fields so adding short support later is a change of the
    short-value term only, not a change of the response shape.
    """

    portfolio_id: int
    equity: Decimal
    cash: Decimal
    market_value: Decimal
    invested_capital: Decimal
    gross_exposure_pct: Optional[Decimal]
    net_exposure_pct: Optional[Decimal]
    cash_pct: Optional[Decimal]
    invested_pct: Optional[Decimal]
    peak_equity: Decimal = ZERO
    current_drawdown_pct: Decimal = ZERO
    cash_reserve_required: Decimal = ZERO
    top_position_weight_pct: Optional[Decimal] = None
    top_positions_weight_pct: Optional[Decimal] = None
    sector_weights: tuple = ()
    position_risks: tuple = ()
    violations: tuple = ()
    recommendations: tuple = ()
    #: Count of open positions and the configured ceiling, kept so a caller reading only the
    #: report can tell "at the limit" from "no limit is set" without re-deriving it.
    position_count: int = 0
    max_positions: Optional[int] = None
    #: The drawdown guard, and whether it is currently triggered. ``None`` means the guard is
    #: not configured at all, which is different from being configured and not breached.
    drawdown_limit_pct: Optional[Decimal] = None
    drawdown_breached: bool = False
    #: Sum of every position's risk to its stop. Never negative: a position above water has no
    #: risk to close at, not negative risk.
    total_risk_amount: Decimal = ZERO
    #: The policy the report was evaluated against, so the numbers can be interpreted without
    #: re-reading the portfolio's stored configuration.
    configuration: Optional[AllocationConfig] = None
    #: When the report was taken, resolved by the caller so a report and the decision run
    #: behind it can share one timestamp.
    as_of: Optional[datetime] = None

    @property
    def sector_weight_map(self) -> Dict[str, Decimal]:
        """``{sector: weight_pct}``, for callers that want the mapping rather than the pairs.

        ``sector_weights`` is a sorted tuple of pairs so the report's own ordering is
        deterministic; this view is for the ones that only need to look a sector up.
        """
        return dict(self.sector_weights)

    def recommendation_for(self, stock_id: int) -> Optional[RiskRecommendation]:
        """The recommendation covering ``stock_id``, or ``None`` when there is none."""
        for recommendation in self.recommendations:
            if recommendation.stock_id == stock_id:
                return recommendation
        return None

    def has_violation(self, rule: str) -> bool:
        """True when the named rule is currently breached."""
        return any(violation.rule == rule for violation in self.violations)


@dataclass(frozen=True)
class PortfolioDecision:
    """One explainable portfolio decision.

    ``reasons`` are stable machine-readable codes and ``explanation`` carries the prose with the
    numbers behind them, kept in the same order so a client can pair them up. Nothing about a
    decision is scored opaquely: an unexplained decision would be indistinguishable from a
    guess.
    """

    stock_id: int
    action: DecisionAction
    target_weight_pct: Decimal = ZERO
    target_quantity: int = 0
    current_quantity: int = 0
    current_weight_pct: Optional[Decimal] = None
    weight_difference_pct: Optional[Decimal] = None
    price: Optional[Decimal] = None
    estimated_trade_value: Decimal = ZERO
    risk_budget: Optional[Decimal] = None
    risk_exposure: Optional[Decimal] = None
    eligible: bool = True
    blocked: bool = False
    reasons: tuple = ()
    explanation: tuple = ()
    symbol: Optional[str] = None
    sector: Optional[str] = None

    @property
    def requires_trade(self) -> bool:
        """True when the decision changes the portfolio if it is applied."""
        return self.action in (
            DecisionAction.BUY,
            DecisionAction.SELL,
            DecisionAction.REDUCE,
            DecisionAction.REBALANCE,
        )


@dataclass(frozen=True)
class PortfolioDecisions:
    """The whole result of one construction run."""

    portfolio_id: int
    status: PortfolioStatus
    as_of: datetime
    sizing_method: PositionSizingMethod
    equity: Decimal
    cash: Decimal
    invested_value: Decimal
    cash_reserve_required: Decimal
    candidate_count: int
    eligible_count: int
    decisions: tuple = ()
    skipped: tuple = ()
    risk_violations: tuple = ()
    #: The effective policy the decisions were built from, including any per-request override,
    #: so a caller can see the rules in force for *this* run rather than only the stored ones.
    configuration: Optional[AllocationConfig] = None


@dataclass(frozen=True)
class RebalanceItem:
    """One stock's rebalancing verdict.

    An item with ``required_quantity == 0`` is a HOLD: the weight drift is inside the
    threshold, so no transaction is worth making. It is still returned, because "this position
    was checked and needs nothing" is a result, not an absence of one.
    """

    stock_id: int
    action: DecisionAction
    current_quantity: int
    target_quantity: int
    target_weight_pct: Decimal
    current_weight_pct: Optional[Decimal] = None
    weight_difference_pct: Optional[Decimal] = None
    price: Optional[Decimal] = None
    required_quantity: int = 0
    estimated_trade_value: Decimal = ZERO
    estimated_fees: Decimal = ZERO
    below_threshold: bool = False
    blocked: bool = False
    reasons: tuple = ()
    explanation: tuple = ()
    symbol: Optional[str] = None
    sector: Optional[str] = None

    @property
    def requires_trade(self) -> bool:
        return self.required_quantity != 0 and not self.blocked

    @property
    def side(self) -> Optional[OrderSide]:
        """The direction of the proposed trade, or ``None`` when none is proposed.

        A positive ``required_quantity`` means buy, a negative one means sell, and zero means no
        trade — so an item whose direction cannot be stated is exactly an item with nothing to
        do, which is why this is derived rather than stored.
        """
        if self.required_quantity > 0:
            return OrderSide.BUY
        if self.required_quantity < 0:
            return OrderSide.SELL
        return None


@dataclass(frozen=True)
class RebalancePlan:
    """The whole result of one rebalancing pass."""

    portfolio_id: int
    status: PortfolioStatus
    as_of: datetime
    equity: Decimal
    cash: Decimal
    invested_value: Decimal
    rebalance_threshold_pct: Decimal
    target_weights: tuple = ()
    items: tuple = ()
    #: The policy the targets were derived from, reported so a caller can see *why* the targets
    #: are what they are without re-reading the portfolio's stored configuration.
    configuration: Optional[AllocationConfig] = None

    @property
    def trades(self) -> tuple:
        """Only the items that actually propose a trade."""
        return tuple(item for item in self.items if item.requires_trade)

    @property
    def estimated_total_value(self) -> Decimal:
        from app.services.backtest.models import quantize_money

        return quantize_money(sum((item.estimated_trade_value for item in self.trades), ZERO))

    @property
    def estimated_total_fees(self) -> Decimal:
        from app.services.backtest.models import quantize_money

        return quantize_money(sum((item.estimated_fees for item in self.trades), ZERO))


@dataclass(frozen=True)
class CashMovement:
    """The result of one deposit or withdrawal, as a pure calculation.

    ``cash_impact`` is signed: positive for a deposit, negative for a withdrawal. The engine
    never lets a withdrawal drive cash below zero — that is refused before this value is built.
    """

    portfolio_id: int
    movement_type: str
    amount: Decimal
    cash_before: Decimal
    cash_after: Decimal
    net_cash_impact: Decimal
    fees: Decimal = ZERO
    gross_value: Decimal = ZERO


# --- Phase 13: portfolio & risk management engine -------------------------
#
# The Phase 9-12 models describe a *paper-trading account* and its *construction decisions*.
# The models below describe the reusable portfolio/risk engine that sits above them: one
# policy that states every limit in one place, the sizing arithmetic broken down into its
# parts, the allocation decision for one candidate, the whole allocation result, and the
# portfolio's current risk state. They are pure value objects — no session, no clock, no SQL.


@dataclass(frozen=True)
class PortfolioRiskPolicy:
    """Every portfolio risk limit, stated in one place.

    The engine already carried this information split across three objects — ``AllocationConfig``
    for the construction policy, ``RiskLimits`` for the pre-trade order rules, and
    ``TradingCostConfig`` for execution costs. A *policy* gathers all three so a sizing or
    allocation question can be answered from a single argument, and so a caller can send one
    override object rather than three.

    Nothing here replaces the existing objects: :meth:`as_allocation_config`,
    :meth:`as_risk_limits` and :meth:`as_costs` hand each one back unchanged, so the Phase 9-12
    code paths keep reading exactly the configuration they read before.

    Percentages are in percent (``1`` means 1%). ``None`` disables the corresponding limit,
    which keeps "no limit" distinguishable from "a limit of zero" — a zero exposure limit is a
    real, and very strict, policy.

    Every limit is validated by :meth:`validate`, which raises ``ValueError``; the API layer
    turns that into :class:`~app.core.exceptions.InvalidRiskPolicyError`. A limit is never
    silently clamped, because a repaired risk policy produces a portfolio nobody designed.
    """

    #: Maximum share of equity a single position may represent.
    max_position_weight_pct: Decimal = Decimal("20")
    #: Maximum share of equity that may be invested across all positions at once.
    max_exposure_pct: Optional[Decimal] = None
    #: Maximum share of equity a single trade may put at risk to its stop.
    max_risk_per_trade_pct: Decimal = Decimal("1")
    #: Maximum tolerated decline from peak equity, in percent. Breaching it blocks new buys.
    max_drawdown_pct: Optional[Decimal] = None
    #: Maximum number of simultaneously open positions.
    max_positions: Optional[int] = None
    #: Share of equity that must stay in cash after a buy.
    min_cash_reserve_pct: Decimal = Decimal("0")
    #: Maximum share of equity one sector or industry may represent; unset disables the rule.
    max_sector_weight_pct: Optional[Decimal] = None
    #: Maximum share of equity that may be at risk to the stops of *all* open positions.
    max_total_risk_pct: Optional[Decimal] = None
    #: Stop-loss trigger, in percent below a position's average entry price. ``None`` means
    #: "no stop", which makes risk-based sizing unavailable rather than risk-free.
    stop_loss_pct: Optional[Decimal] = None
    #: Smallest gross trade value the engine will propose; unset disables the rule.
    min_trade_value: Optional[Decimal] = None
    #: Simulated commission rate as a fraction of gross value (``0.001`` = 0.1%).
    commission_rate: Decimal = Decimal("0.001")
    #: Floor charged per side, in the portfolio's currency.
    min_fee: Decimal = ZERO
    #: Adverse slippage rate applied to the fill price (``0`` means no slippage).
    slippage_rate: Decimal = ZERO

    def validate(self) -> None:
        """Reject a policy the engine cannot act on, before any calculation runs."""
        _require_percentage("max_position_weight_pct", self.max_position_weight_pct)
        _require_percentage("max_risk_per_trade_pct", self.max_risk_per_trade_pct)
        for name in ("max_exposure_pct", "max_drawdown_pct", "max_sector_weight_pct", "max_total_risk_pct"):
            _require_optional_percentage(name, getattr(self, name))
        for name in ("min_cash_reserve_pct", "min_trade_value"):
            value = getattr(self, name)
            if value is not None and value < ZERO:
                raise ValueError(f"{name} must not be negative")
        if self.min_cash_reserve_pct > _HUNDRED:
            raise ValueError("min_cash_reserve_pct must be in [0, 100]")
        if self.min_trade_value is not None and self.min_trade_value <= ZERO:
            raise ValueError("min_trade_value must be greater than zero when set")
        if self.max_positions is not None and self.max_positions < 1:
            raise ValueError("max_positions must be at least 1 when set")
        if self.stop_loss_pct is not None and not (ZERO < self.stop_loss_pct < _HUNDRED):
            raise ValueError("stop_loss_pct must be in (0, 100) when set")
        for name in ("commission_rate", "min_fee", "slippage_rate"):
            if getattr(self, name) < ZERO:
                raise ValueError(f"{name} must not be negative")
        if self.slippage_rate >= _ONE:
            raise ValueError("slippage_rate must be less than 1 (a rate of 1 would fill a sell at zero)")

    def replace(self, **changes) -> "PortfolioRiskPolicy":
        """A copy with the given fields replaced — how a per-request override is applied."""
        from dataclasses import replace as _replace

        return _replace(self, **changes)

    def as_trading_costs(self) -> TradingCostConfig:
        """The Phase 9-12 execution-cost object this policy carries."""
        return TradingCostConfig(
            commission_rate=self.commission_rate,
            min_fee=self.min_fee,
            slippage_rate=self.slippage_rate,
        )

    def as_allocation_config(self) -> AllocationConfig:
        """The Phase 11 construction policy this policy carries, unchanged in meaning."""
        return AllocationConfig(
            sizing_method=PositionSizingMethod.RISK_BASED
            if self.stop_loss_pct is not None
            else PositionSizingMethod.EQUAL_WEIGHT,
            max_position_weight_pct=self.max_position_weight_pct,
            max_sector_weight_pct=self.max_sector_weight_pct,
            max_positions=self.max_positions,
            max_exposure_pct=self.max_exposure_pct,
            stop_loss_pct=self.stop_loss_pct,
            min_position_value=self.min_trade_value,
            risk_per_trade_pct=self.max_risk_per_trade_pct,
            cash_reserve_pct=self.min_cash_reserve_pct,
        )

    def as_risk_limits(self) -> RiskLimits:
        """The Phase 9-12 pre-trade order limits this policy carries, unchanged in meaning."""
        return RiskLimits(
            max_position_allocation_pct=self.max_position_weight_pct,
            max_drawdown_pct=self.max_drawdown_pct,
            max_exposure_pct=self.max_exposure_pct,
            max_trade_allocation_pct=self.max_position_weight_pct,
            min_cash_reserve_pct=self.min_cash_reserve_pct or None,
            min_trade_value=self.min_trade_value,
            max_positions=self.max_positions,
        )

    def risk_budget(self, equity: Decimal) -> Decimal:
        """Capital this policy allows a single trade to put at risk: ``equity * pct / 100``."""
        if Decimal(equity) <= ZERO:
            return quantize_money(ZERO)
        return quantize_money(Decimal(equity) * Decimal(self.max_risk_per_trade_pct) / _HUNDRED)

    def max_position_value(self, equity: Decimal) -> Decimal:
        """Largest position value this policy allows: ``equity * max_position_weight_pct / 100``."""
        if Decimal(equity) <= ZERO:
            return quantize_money(ZERO)
        return quantize_money(Decimal(equity) * Decimal(self.max_position_weight_pct) / _HUNDRED)

    def cash_reserve_required(self, equity: Decimal) -> Decimal:
        """Cash that must remain after a buy: ``equity * min_cash_reserve_pct / 100``."""
        if Decimal(equity) <= ZERO:
            return quantize_money(ZERO)
        return quantize_money(Decimal(equity) * Decimal(self.min_cash_reserve_pct) / _HUNDRED)

    def max_total_risk(self, equity: Decimal) -> Optional[Decimal]:
        """Total risk-to-stop this policy allows, or ``None`` when the guard is disabled."""
        if self.max_total_risk_pct is None or Decimal(equity) <= ZERO:
            return None
        return quantize_money(Decimal(equity) * Decimal(self.max_total_risk_pct) / _HUNDRED)


#: One hundred, used by the policy helpers above.
_ONE = Decimal("1")


def _require_percentage(name: str, value: Decimal) -> None:
    if not (ZERO < Decimal(value) <= _HUNDRED):
        raise ValueError(f"{name} must be in (0, 100]")


def _require_optional_percentage(name: str, value: Optional[Decimal]) -> None:
    if value is not None and not (ZERO < Decimal(value) <= _HUNDRED):
        raise ValueError(f"{name} must be in (0, 100] when set")


@dataclass(frozen=True)
class SizingBreakdown:
    """Every intermediate figure behind one position-size decision.

    The quantity is never returned on its own: a size without its arithmetic is a number the
    caller cannot check, which is exactly the property the engine is required not to have. Each
    bound is kept so the explanation can name which of the two constraints actually bound.
    """

    quantity: int
    entry_price: Decimal
    stop_price: Optional[Decimal]
    equity: Decimal
    #: ``equity * max_risk_per_trade_pct / 100`` — the loss this trade may put at risk.
    risk_budget: Decimal
    #: ``entry_price - stop_price`` for a long. ``None`` when no usable stop was supplied.
    risk_per_share: Optional[Decimal]
    #: ``floor(risk_budget / risk_per_share)``.
    quantity_by_risk: int
    #: ``equity * max_position_weight_pct / 100``.
    max_position_value: Decimal
    #: ``floor(max_position_value / entry_price)``.
    quantity_by_capital: int
    #: Whole shares the policy's cash can fund, fees included. ``None`` when no cash cap applies.
    quantity_by_cash: Optional[int]
    #: Cash actually consumed by ``quantity`` shares, fees included.
    capital_required: Decimal
    #: Fees charged on the proposed trade.
    fees: Decimal
    #: ``quantity * (entry_price - stop_price)`` — the loss at the stop. Never negative.
    risk_amount: Decimal
    #: Which constraint set the final quantity: ``risk``, ``position_weight``, ``cash`` or
    #: ``unaffordable``.
    binding_constraint: str
    #: Prose explanation carrying the numbers, in the order they were computed.
    explanation: str

    @property
    def affordable(self) -> bool:
        """True when at least one whole share can be funded."""
        return self.quantity > 0


#: Stable values of :attr:`SizingBreakdown.binding_constraint`.
CONSTRAINT_RISK = "risk"
CONSTRAINT_POSITION_WEIGHT = "position_weight"
CONSTRAINT_CASH = "cash"
CONSTRAINT_UNAFFORDABLE = "unaffordable"


@dataclass(frozen=True)
class AllocationCandidate:
    """One stock offered to the Phase 13 allocation engine.

    ``composite_score`` is the **Phase 7 signal score**, carried through unchanged: the engine
    ranks candidates by it and never rescales it, so an allocation can always be traced back to
    the signal that produced it. ``stock_id`` is the tie-breaker, which makes the ordering total.
    """

    stock_id: int
    entry_price: Decimal
    stop_price: Optional[Decimal] = None
    composite_score: int = 0
    symbol: Optional[str] = None
    sector: Optional[str] = None
    #: Number of shares already held, so the engine can distinguish adding to a position from
    #: opening a new one — the position-count limit only applies to the latter.
    held_quantity: int = 0
    #: Set by the engine when a candidate is structurally unusable (bad price, bad stop), with
    #: the reason. A candidate that cannot be evaluated is still *reported*, because "which
    #: stocks were considered and why each was skipped" is part of an explainable decision.
    eligible: bool = True
    ineligible_reason: Optional[str] = None

    def sort_key(self) -> tuple:
        """Composite score descending, then stock id ascending — the existing Phase 11 key.

        Deliberately the same key :meth:`app.services.portfolio.models.Candidate.sort_key`
        defines, so the two engines can never disagree about which candidate is better.
        """
        return (-self.composite_score, self.stock_id)


@dataclass(frozen=True)
class AllocationDecision:
    """The verdict for one candidate: whether to open it, and the arithmetic behind it.

    ``approved`` is ``True`` only when ``rejection_reasons`` is empty. Every rejection reason is
    reported, not only the first, so one call tells a caller everything that is wrong.
    """

    stock_id: int
    approved: bool
    #: Whole shares proposed. ``0`` for a rejected or unaffordable candidate, and never
    #: negative.
    quantity: int = 0
    entry_price: Decimal = ZERO
    stop_price: Optional[Decimal] = None
    #: Cash the trade commits, fees included.
    capital_required: Decimal = ZERO
    #: ``quantity * (entry_price - stop_price)`` — the loss at the stop. Never negative.
    risk_amount: Decimal = ZERO
    #: ``quantity * entry_price / equity`` as a percentage, after this allocation.
    portfolio_weight_pct: Optional[Decimal] = None
    #: Total invested as a percentage of equity, after this allocation.
    resulting_exposure_pct: Optional[Decimal] = None
    #: Cash left after this allocation.
    remaining_cash: Decimal = ZERO
    #: Stable machine-readable codes, in evaluation order. Empty when approved.
    rejection_reasons: tuple = ()
    #: Prose carrying the numbers, paired positionally with ``rejection_reasons`` and followed by
    #: the sizing explanation.
    explanation: tuple = ()
    #: Which constraint set the quantity (``risk`` / ``position_weight`` / ``cash``).
    binding_constraint: Optional[str] = None
    composite_score: int = 0
    symbol: Optional[str] = None
    sector: Optional[str] = None
    #: The full sizing arithmetic, so the decision can be re-derived independently.
    sizing: Optional[SizingBreakdown] = None

    @property
    def requires_trade(self) -> bool:
        """True when applying this decision would change the portfolio."""
        return self.approved and self.quantity > 0


@dataclass(frozen=True)
class AllocationResult:
    """The whole outcome of one allocation run over an ordered candidate list.

    The decisions come back in the order they were *considered*, which is the deterministic
    ranking order, so an identical request produces an identical sequence every time.
    """

    portfolio_id: int
    as_of: datetime
    equity: Decimal
    cash_before: Decimal
    cash_after: Decimal
    invested_before: Decimal
    invested_after: Decimal
    exposure_before_pct: Optional[Decimal]
    exposure_after_pct: Optional[Decimal]
    position_count_before: int
    position_count_after: int
    policy: PortfolioRiskPolicy
    candidates_considered: int = 0
    decisions: tuple = ()
    #: Free-form reason the run stopped early, or ``None`` when every candidate was considered.
    stop_reason: Optional[str] = None
    warnings: tuple = ()

    @property
    def approved(self) -> tuple:
        return tuple(decision for decision in self.decisions if decision.approved)

    @property
    def rejected(self) -> tuple:
        return tuple(decision for decision in self.decisions if not decision.approved)

    def decision_for(self, stock_id: int) -> Optional[AllocationDecision]:
        """The decision for ``stock_id``, or ``None`` when it was never considered."""
        for decision in self.decisions:
            if decision.stock_id == stock_id:
                return decision
        return None


@dataclass(frozen=True)
class PositionExposure:
    """Risk figures for one open position, as reported by the risk-state engine.

    This is the Phase 13 reading of a position: the market figures, its weight of the
    portfolio, and the loss it would book if it fell to its stop.
    """

    stock_id: int
    quantity: int
    average_entry_price: Decimal
    current_price: Decimal
    market_value: Decimal
    cost_basis: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_pct: Optional[Decimal]
    #: ``market_value / equity * 100``.
    portfolio_weight_pct: Optional[Decimal]
    #: ``(average_entry_price - stop_price) * quantity``; zero when no stop applies.
    risk_amount: Decimal
    stop_price: Optional[Decimal] = None
    sector: Optional[str] = None
    symbol: Optional[str] = None


@dataclass(frozen=True)
class PortfolioRiskState:
    """Where a portfolio stands against its policy, right now.

    ``flags`` is the machine-readable answer to "is anything breached": it is a tuple, ordered
    by the fixed evaluation order in :mod:`app.services.portfolio.risk_state`, so two runs over
    the same portfolio always report the same flags in the same sequence.
    """

    portfolio_id: int
    as_of: datetime
    equity: Decimal
    cash: Decimal
    market_value: Decimal
    #: ``market_value / equity * 100``.
    exposure_pct: Optional[Decimal]
    #: ``cash / equity * 100``.
    cash_weight_pct: Optional[Decimal]
    #: Sum of the open positions' cost bases.
    invested_capital: Decimal
    #: ``invested_capital / equity * 100``.
    invested_weight_pct: Optional[Decimal]
    position_count: int
    #: ``market_value / equity * 100`` of the single largest position; ``None`` when flat.
    largest_position_weight_pct: Optional[Decimal]
    #: Stock id of that largest position; ``None`` when flat.
    largest_position_stock_id: Optional[int] = None
    #: Sum of every position's risk to its stop.
    total_risk_amount: Decimal = ZERO
    #: ``total_risk_amount / equity * 100``.
    total_risk_pct: Optional[Decimal] = None
    realized_pnl: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO
    total_pnl: Decimal = ZERO
    #: ``(equity - initial_capital) / initial_capital * 100``.
    portfolio_return_pct: Decimal = ZERO
    peak_equity: Decimal = ZERO
    #: ``(peak_equity - equity) / peak_equity * 100`` — positive magnitude, zero at a peak.
    current_drawdown_pct: Decimal = ZERO
    #: Worst drawdown over the recorded equity history; zero when there is no history.
    max_drawdown_pct: Decimal = ZERO
    #: Worst drawdown in money over the recorded equity history.
    max_drawdown: Decimal = ZERO
    #: ``{sector: weight_pct}`` for the sectors that are actually known, sorted by sector name.
    sector_weights: tuple = ()
    flags: tuple = ()
    #: Structured detail for each flag, paired positionally with ``flags``.
    flag_details: tuple = ()
    positions: tuple = ()
    policy: Optional[PortfolioRiskPolicy] = None

    @property
    def breached(self) -> bool:
        """True when at least one configured limit is currently breached."""
        return bool(self.flags)

    def has_flag(self, flag) -> bool:
        """True when ``flag`` is present."""
        return flag in self.flags

    @property
    def sector_weight_map(self) -> Dict[str, Decimal]:
        return dict(self.sector_weights)


@dataclass(frozen=True)
class RiskFlagDetail:
    """The numbers behind one raised risk flag."""

    flag: str
    rule: str
    reason: str
    limit: Optional[Decimal] = None
    observed: Optional[Decimal] = None
    details: dict = field(default_factory=dict)


# --- Coercion from the API request models ---------------------------------
#
# ``TradingCostConfig`` and ``RiskLimits`` exist twice on purpose: as Pydantic request models
# in ``app.schemas.portfolio`` (where field constraints produce the OpenAPI contract and the
# 422 errors) and as the frozen dataclasses above (where the engine owns validation and
# uses them). The two carry identical fields; these helpers convert the wire model into the
# engine model so there is exactly one source of truth inside the engine.


def as_trading_cost_config(value) -> TradingCostConfig:
    """Coerce an API ``TradingCostConfig`` (or an engine one) into the engine dataclass."""
    from app.services.portfolio.defaults import default_trading_cost_config

    if value is None:
        return default_trading_cost_config()
    if isinstance(value, TradingCostConfig):
        return value
    return TradingCostConfig(
        commission_rate=Decimal(str(value.commission_rate)),
        min_fee=Decimal(str(value.min_fee)),
        slippage_rate=Decimal(str(value.slippage_rate)),
    )


def as_risk_limits(value) -> RiskLimits:
    """Coerce an API ``RiskLimits`` (or an engine one) into the engine dataclass."""
    from app.services.portfolio.defaults import default_risk_limits

    if value is None:
        return default_risk_limits()
    if isinstance(value, RiskLimits):
        return value

    def optional_decimal(raw) -> Optional[Decimal]:
        return None if raw is None else Decimal(str(raw))

    return RiskLimits(
        max_position_quantity=value.max_position_quantity,
        max_position_value=optional_decimal(value.max_position_value),
        max_position_allocation_pct=optional_decimal(value.max_position_allocation_pct),
        max_drawdown_pct=optional_decimal(value.max_drawdown_pct),
        max_daily_loss_pct=optional_decimal(getattr(value, "max_daily_loss_pct", None)),
        min_trade_value=optional_decimal(getattr(value, "min_trade_value", None)),
        max_exposure_pct=optional_decimal(getattr(value, "max_exposure_pct", None)),
        max_trade_allocation_pct=optional_decimal(getattr(value, "max_trade_allocation_pct", None)),
        min_cash_reserve_pct=optional_decimal(getattr(value, "min_cash_reserve_pct", None)),
        max_positions=getattr(value, "max_positions", None),
    )


def as_allocation_config(value) -> AllocationConfig:
    """Coerce an API ``PortfolioAllocationConfig`` (or an engine one) into the engine dataclass."""
    from app.services.portfolio.defaults import default_allocation_config

    if value is None:
        return default_allocation_config()
    if isinstance(value, AllocationConfig):
        return value

    def optional_decimal(raw) -> Optional[Decimal]:
        return None if raw is None else Decimal(str(raw))

    return AllocationConfig(
        sizing_method=value.sizing_method,
        max_position_weight_pct=Decimal(str(value.max_position_weight_pct)),
        max_sector_weight_pct=optional_decimal(value.max_sector_weight_pct),
        max_positions=value.max_positions,
        max_exposure_pct=optional_decimal(value.max_exposure_pct),
        max_position_loss_pct=optional_decimal(value.max_position_loss_pct),
        stop_loss_pct=optional_decimal(value.stop_loss_pct),
        take_profit_pct=optional_decimal(value.take_profit_pct),
        rebalance_threshold_pct=Decimal(str(value.rebalance_threshold_pct)),
        min_position_value=optional_decimal(value.min_position_value),
        risk_per_trade_pct=Decimal(str(value.risk_per_trade_pct)),
        cash_reserve_pct=Decimal(str(value.cash_reserve_pct)),
        target_position_count=value.target_position_count,
    )


def allocation_config_to_dict(config: AllocationConfig) -> Dict[str, object]:
    """Serialise an allocation config for ``portfolios.allocation_config_json``.

    Decimals become strings so a configuration round-trips exactly rather than through binary
    floating point, and every field is written — including the ``None`` ones — so a stored
    configuration always describes the complete policy.
    """
    return {
        "sizing_method": config.sizing_method.value,
        "max_position_weight_pct": str(config.max_position_weight_pct),
        "max_sector_weight_pct": _money_or_none(config.max_sector_weight_pct),
        "max_positions": config.max_positions,
        "max_exposure_pct": _money_or_none(config.max_exposure_pct),
        "max_position_loss_pct": _money_or_none(config.max_position_loss_pct),
        "stop_loss_pct": _money_or_none(config.stop_loss_pct),
        "take_profit_pct": _money_or_none(config.take_profit_pct),
        "rebalance_threshold_pct": str(config.rebalance_threshold_pct),
        "min_position_value": _money_or_none(config.min_position_value),
        "risk_per_trade_pct": str(config.risk_per_trade_pct),
        "cash_reserve_pct": str(config.cash_reserve_pct),
        "target_position_count": config.target_position_count,
    }


def _money_or_none(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


def as_risk_policy(value) -> PortfolioRiskPolicy:
    """Coerce an API ``PortfolioRiskPolicySchema`` (or an engine policy) into the engine dataclass.

    ``None`` yields the application defaults. The engine dataclass is returned unchanged, so a
    caller that already holds a validated policy pays nothing. The conversion mirrors
    :func:`as_risk_limits` and :func:`as_allocation_config`: ``Decimal`` from strings, ``None``
    preserved as "no limit".
    """
    from app.services.portfolio.defaults import default_portfolio_risk_policy

    if value is None:
        return default_portfolio_risk_policy()
    if isinstance(value, PortfolioRiskPolicy):
        return value

    def optional_decimal(raw) -> Optional[Decimal]:
        return None if raw is None else Decimal(str(raw))

    def decimal(raw) -> Decimal:
        return Decimal(str(raw))

    return PortfolioRiskPolicy(
        max_position_weight_pct=decimal(value.max_position_weight_pct),
        max_exposure_pct=optional_decimal(value.max_exposure_pct),
        max_risk_per_trade_pct=decimal(value.max_risk_per_trade_pct),
        max_drawdown_pct=optional_decimal(value.max_drawdown_pct),
        max_positions=value.max_positions,
        min_cash_reserve_pct=decimal(value.min_cash_reserve_pct),
        max_sector_weight_pct=optional_decimal(value.max_sector_weight_pct),
        max_total_risk_pct=optional_decimal(value.max_total_risk_pct),
        stop_loss_pct=optional_decimal(value.stop_loss_pct),
        min_trade_value=optional_decimal(value.min_trade_value),
        commission_rate=decimal(value.commission_rate),
        min_fee=decimal(value.min_fee),
        slippage_rate=decimal(value.slippage_rate),
    )


def policy_from_allocation(
    allocation: Optional[AllocationConfig] = None,
    limits: Optional[RiskLimits] = None,
    costs: Optional[TradingCostConfig] = None,
) -> PortfolioRiskPolicy:
    """Build one :class:`PortfolioRiskPolicy` from the Phase 9-12 configuration objects.

    Each source contributes only the limits it owns, so nothing is invented and nothing is
    lost:

    * the **allocation config** supplies the weight cap, exposure cap, position count, sector
      cap, stop and risk-per-trade;
    * the **risk limits** supply the drawdown guard and the cash reserve when the allocation
      config left them unset;
    * the **cost config** supplies commission, minimum fee and slippage.

    A value present in more than one place is taken from the allocation config, which is the
    more specific of the two: it is the policy stored *with* the portfolio.
    """
    allocation = allocation or AllocationConfig()
    limits = limits or RiskLimits()

    def first(*values):
        for value in values:
            if value is not None:
                return value
        return None

    return PortfolioRiskPolicy(
        max_position_weight_pct=Decimal(str(allocation.max_position_weight_pct)),
        max_exposure_pct=first(allocation.max_exposure_pct, limits.max_exposure_pct),
        max_risk_per_trade_pct=Decimal(str(allocation.risk_per_trade_pct)),
        max_drawdown_pct=limits.max_drawdown_pct,
        max_positions=first(allocation.max_positions, limits.max_positions),
        min_cash_reserve_pct=(
            allocation.cash_reserve_pct
            if allocation.cash_reserve_pct > ZERO
            else (limits.min_cash_reserve_pct or ZERO)
        ),
        max_sector_weight_pct=allocation.max_sector_weight_pct,
        max_total_risk_pct=None,
        stop_loss_pct=allocation.stop_loss_pct,
        min_trade_value=first(allocation.min_position_value, limits.min_trade_value),
        commission_rate=Decimal(str(costs.commission_rate)) if costs else Decimal("0.001"),
        min_fee=Decimal(str(costs.min_fee)) if costs else ZERO,
        slippage_rate=Decimal(str(costs.slippage_rate)) if costs else ZERO,
    )


def allocation_config_from_dict(payload: Optional[dict]) -> Optional[AllocationConfig]:
    """Rebuild an :class:`AllocationConfig` from its stored JSON.

    Returns ``None`` for a missing or unreadable payload so the caller falls back to the
    application defaults instead of guessing a configuration. A row that is *partially* written
    is treated the same way and logged: every config this codebase writes contains every field,
    so a missing key means the value cannot be trusted, and reporting that is preferable to
    inventing the missing half of a financial policy.
    """
    if not isinstance(payload, dict):
        return None

    def optional(raw) -> Optional[Decimal]:
        return None if raw is None or raw == "" else Decimal(str(raw))

    required_keys = (
        "max_position_weight_pct",
        "rebalance_threshold_pct",
        "risk_per_trade_pct",
        "cash_reserve_pct",
    )
    if any(payload.get(key) is None for key in required_keys):
        logger.warning(
            "Stored portfolio allocation configuration is incomplete; using application defaults"
        )
        return None

    try:
        config = AllocationConfig(
            sizing_method=PositionSizingMethod(
                payload.get("sizing_method", PositionSizingMethod.EQUAL_WEIGHT.value)
            ),
            max_position_weight_pct=Decimal(str(payload["max_position_weight_pct"])),
            max_sector_weight_pct=optional(payload.get("max_sector_weight_pct")),
            max_positions=payload.get("max_positions"),
            max_exposure_pct=optional(payload.get("max_exposure_pct")),
            max_position_loss_pct=optional(payload.get("max_position_loss_pct")),
            stop_loss_pct=optional(payload.get("stop_loss_pct")),
            take_profit_pct=optional(payload.get("take_profit_pct")),
            rebalance_threshold_pct=Decimal(str(payload["rebalance_threshold_pct"])),
            min_position_value=optional(payload.get("min_position_value")),
            risk_per_trade_pct=Decimal(str(payload["risk_per_trade_pct"])),
            cash_reserve_pct=Decimal(str(payload["cash_reserve_pct"])),
            target_position_count=payload.get("target_position_count"),
        )
        config.validate()
    except (ValueError, ArithmeticError, TypeError, KeyError) as exc:
        # A configuration the engine cannot act on is reported, never silently repaired.
        logger.warning(
            "Stored portfolio allocation configuration is unusable (%s); using application "
            "defaults",
            exc,
        )
        return None
    return config