"""Phase 16 domain model — orders, fills, plans, validation results and the state machine.

**Simulation only.** Everything in this module describes a *simulated* execution. No type here
carries a broker endpoint, a credential, or a live order, and nothing in this package can reach
an exchange.

Pure by construction
--------------------
Every engine in :mod:`app.services.execution` is a function of the values it is handed. There is
no clock, no database, no randomness and no HTTP: a timestamp is always an argument, never
``now()``, and a fill price is always derived arithmetically from the reference price it was
given. That is what makes an execution reproducible — the same plan over the same account
produces the same orders, fills, positions and cash, every time, on any machine.

Which existing type is used, and why
------------------------------------
* money and percentages are ``Decimal``, quantised through the Phase 8 helpers so Phase 16
  cannot drift from Phases 8-15 on rounding;
* ``OrderSide`` / ``OrderType`` / ``OrderStatus`` are the shared Phase 9 enums, extended
  additively for the Phase 16 lifecycle. The lifecycle states this module needs are not a second
  vocabulary invented here: one enum means one order has one status, everywhere;
* position and cash arithmetic is **not** reimplemented. The functions in
  :mod:`app.services.portfolio.calculator` remain the single definition of how a fill moves a
  position, what realised P&L a sale books and how fees are charged; this module holds only the
  shapes of their results.

The three concepts kept apart
-----------------------------
``OrderPlanItem``  what *should* happen — a quantity and a direction, computed from a diff
``ExecutionOrder``  what *was* asked of an adapter, and where it has got to
``ExecutionFill``   what *actually happened* — one row per fill, possibly many per order
``PortfolioTrade`` (Phase 9, unchanged) the completed transaction record of a fully filled order

Collapsing these is how a partial fill starts duplicating positions, so they stay separate here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional, Tuple

from app.schemas.portfolio import OrderSide, OrderStatus, OrderType

from app.services.backtest.models import quantize_money
from app.services.portfolio.models import ZERO


class ExecutionEventType(str, Enum):
    """The names that appear in the immutable execution audit trail.

    Stored verbatim in ``portfolio_execution_events.event_type``, so these values are a
    persisted vocabulary: renaming one would orphan the historical events that carry it. They
    are the stable codes a consumer branches on, which is why they are uppercase and spelled
    out rather than numbered.
    """

    ORDER_CREATED = "ORDER_CREATED"
    ORDER_VALIDATED = "ORDER_VALIDATED"
    ORDER_REJECTED = "ORDER_REJECTED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_FAILED = "ORDER_FAILED"
    POSITION_UPDATED = "POSITION_UPDATED"
    CASH_UPDATED = "CASH_UPDATED"
    RECONCILIATION_COMPLETED = "RECONCILIATION_COMPLETED"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    PLAN_CREATED = "PLAN_CREATED"
    PLAN_VALIDATED = "PLAN_VALIDATED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class ExecutionEntityType(str, Enum):
    """What an audit event is about, so a log can be filtered by subject."""

    PLAN = "plan"
    ORDER = "order"
    FILL = "fill"
    POSITION = "position"
    CASH = "cash"
    RECONCILIATION = "reconciliation"


class ValidationSeverity(str, Enum):
    """How serious a validation finding or reconciliation discrepancy is.

    ``ERROR`` blocks execution; ``WARNING`` is reported and execution continues. The two are
    never merged, because "this plan is wrong" and "this plan is worth a look" are different
    answers and collapsing them would either hide a refusal or manufacture one.
    """

    ERROR = "ERROR"
    WARNING = "WARNING"


class ReconciliationStatus(str, Enum):
    """Outcome of comparing expected state against actual state.

    ``MATCHED``    no discrepancies and no warnings
    ``WARNING``    no blocking discrepancy, but something is worth reporting
    ``MISMATCHED`` at least one discrepancy that means the books do not agree
    """

    MATCHED = "MATCHED"
    WARNING = "WARNING"
    MISMATCHED = "MISMATCHED"


class RiskGateDecision(str, Enum):
    """The pre-trade gate's verdict. The only two answers the gate may give."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# ----------------------------------------------------------------------
# Plans
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TargetHolding:
    """One stock's target position, as an earlier phase produced it.

    The caller may express a target either as a share count or as a weight of portfolio equity.
    A target weight is converted with the existing
    :func:`app.services.portfolio.allocation.target_quantity_for_weight`, which floors the
    quantity and includes fees in the unit cost, so Phase 16 does not invent a second
    weight-to-quantity rule.

    Exactly one of ``target_quantity`` / ``target_weight_pct`` must be set. A target that
    carries neither is not a position of unknown size — it is an error, and refusing it is what
    stops an unresolvable target from silently becoming a zero-quantity sell.
    """

    stock_id: int
    target_quantity: Optional[int] = None
    target_weight_pct: Optional[Decimal] = None
    price: Optional[Decimal] = None
    symbol: Optional[str] = None
    sector: Optional[str] = None

    def __post_init__(self) -> None:
        if self.target_quantity is None and self.target_weight_pct is None:
            raise ValueError(
                f"stock {self.stock_id}: a target needs either a target_quantity or a "
                "target_weight_pct; a target of unknown size cannot be planned"
            )
        if self.target_quantity is not None and self.target_quantity < 0:
            raise ValueError(
                f"stock {self.stock_id}: target_quantity must be zero or more, got "
                f"{self.target_quantity}"
            )
        if self.target_weight_pct is not None and self.target_weight_pct < ZERO:
            raise ValueError(
                f"stock {self.stock_id}: target_weight_pct must be zero or more, got "
                f"{self.target_weight_pct}"
            )


@dataclass(frozen=True)
class OrderPlanItem:
    """One order the portfolio must place to reach the target: a diff, made explicit.

    ``quantity_delta`` is ``target_quantity - current_quantity`` and therefore carries the
    direction: positive to buy, negative to sell, zero for nothing to do. ``quantity`` is the
    whole-share absolute value that would be submitted, and ``estimated_value`` /
    ``estimated_fees`` are what the order is expected to cost at the reference price — the
    figures validation and the risk gate check before anything is submitted.

    An item with ``quantity_delta == 0`` is kept in the plan rather than dropped: "this stock
    was checked and is already right" is a result, and a plan that listed only the trades
    could not distinguish that from a stock nobody looked at.
    """

    stock_id: int
    symbol: Optional[str]
    side: OrderSide
    current_quantity: int
    target_quantity: int
    quantity_delta: int
    quantity: int
    reference_price: Decimal
    estimated_value: Decimal = ZERO
    estimated_fees: Decimal = ZERO
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[Decimal] = None
    target_weight_pct: Optional[Decimal] = None
    sector: Optional[str] = None

    @property
    def requires_order(self) -> bool:
        """True when this item is an actual transaction rather than a no-action check."""
        return self.quantity_delta != 0

    @property
    def action(self) -> str:
        """A stable word for what the item does, used in audit metadata."""
        if self.quantity_delta > 0:
            return "buy"
        if self.quantity_delta < 0:
            return "sell"
        return "hold"


@dataclass(frozen=True)
class OrderPlan:
    """Every order needed to move one portfolio from its current state to its target.

    Deterministic by construction: items are ordered by ``stock_id`` and every figure is derived
    from the current state, the target and the reference prices. Two runs over identical inputs
    produce an equal plan, which is what makes a plan safe to compare, to store and to replay.

    ``equity`` is the equity the target weights were measured against, recorded here because a
    plan whose weights cannot be traced back to the equity they came from cannot be explained.
    """

    portfolio_id: int
    plan_id: str
    items: Tuple[OrderPlanItem, ...]
    created_at: datetime
    equity: Decimal = ZERO
    cash: Decimal = ZERO
    #: Caller's idempotency key, or the generated one this plan's orders will carry.
    idempotency_key: Optional[str] = None

    @property
    def order_items(self) -> Tuple[OrderPlanItem, ...]:
        """Only the items that are transactions."""
        return tuple(item for item in self.items if item.requires_order)

    @property
    def buy_items(self) -> Tuple[OrderPlanItem, ...]:
        return tuple(item for item in self.items if item.quantity_delta > 0)

    @property
    def sell_items(self) -> Tuple[OrderPlanItem, ...]:
        return tuple(item for item in self.items if item.quantity_delta < 0)

    @property
    def hold_items(self) -> Tuple[OrderPlanItem, ...]:
        return tuple(item for item in self.items if item.quantity_delta == 0)

    @property
    def total_buy_value(self) -> Decimal:
        return quantize_money(
            sum((item.estimated_value for item in self.buy_items), ZERO)
        )

    @property
    def total_sell_value(self) -> Decimal:
        return quantize_money(
            sum((item.estimated_value for item in self.sell_items), ZERO)
        )

    @property
    def total_estimated_fees(self) -> Decimal:
        return quantize_money(sum((item.estimated_fees for item in self.items), ZERO))

    @property
    def cash_required(self) -> Decimal:
        """Cash the plan's buys need: value plus the fees those buys will charge."""
        return quantize_money(
            self.total_buy_value
            + sum((item.estimated_fees for item in self.buy_items), ZERO)
        )

    @property
    def cash_released(self) -> Decimal:
        """Cash the plan's sells return: value less the fees those sells will charge.

        Floored at zero, because a sell whose fees exceed its proceeds releases no cash — and
        saying so is different from reporting a negative "release" that would then be added to
        available cash.
        """
        released = quantize_money(
            self.total_sell_value
            - sum((item.estimated_fees for item in self.sell_items), ZERO)
        )
        return released if released > ZERO else quantize_money(ZERO)

    @property
    def is_empty(self) -> bool:
        return not self.order_items


# ----------------------------------------------------------------------
# Orders and fills
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionOrder:
    """One order as the execution layer tracks it.

    ``order_id`` is the database id and is ``None`` until the order is persisted; before that
    the order is identified by ``client_order_id``, a deterministic string derived from the
    idempotency key. Deriving it rather than generating a random one is what lets a repeat
    submission find the order it already created.

    The remaining fields mirror what the order row will hold: what was asked for
    (``plan_item``), how much of it has happened (``filled_quantity`` / ``remaining_quantity``)
    and why it stopped (``rejection_code`` / ``rejection_reason``).
    """

    client_order_id: str
    portfolio_id: int
    plan_item: OrderPlanItem
    idempotency_key: str
    status: OrderStatus = OrderStatus.CREATED
    order_id: Optional[int] = None
    plan_id: Optional[str] = None
    created_at: Optional[datetime] = None
    validated_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    filled_quantity: int = 0
    remaining_quantity: int = 0
    average_fill_price: Optional[Decimal] = None
    total_fees: Decimal = ZERO
    gross_value: Decimal = ZERO
    net_cash_impact: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    execution_reference: Optional[str] = None
    rejection_code: Optional[str] = None
    rejection_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.remaining_quantity == 0 and self.plan_item.requires_order:
            object.__setattr__(
                self, "remaining_quantity", self.plan_item.quantity
            )

    @property
    def is_terminal(self) -> bool:
        """True when no further transition is possible."""
        return self.status in TERMINAL_ORDER_STATUSES

    @property
    def is_active(self) -> bool:
        """True while the order is still working at the adapter."""
        return self.status in ACTIVE_ORDER_STATUSES

    @property
    def is_filled(self) -> bool:
        return self.status is OrderStatus.FILLED

    @property
    def side(self) -> OrderSide:
        return self.plan_item.side

    @property
    def identifier(self) -> str:
        """How the order is named in an audit event: its database id once it has one."""
        return str(self.order_id) if self.order_id is not None else self.client_order_id


@dataclass(frozen=True)
class ExecutionFill:
    """One fill: a quantity of one stock that actually changed hands.

    Produced by an execution adapter, never computed by this package from a target. The
    accounting figures are carried alongside the broker-side facts so that position, cash, P&L
    and the recorded fill can never disagree about what happened.

    ``net_cash_impact`` is signed — negative for a buy, positive for a sell — and
    ``realized_pnl`` is always 0 for a buy.
    """

    execution_id: str
    order_id: str
    portfolio_id: int
    stock_id: int
    symbol: Optional[str]
    side: OrderSide
    quantity: int
    execution_price: Decimal
    fees: Decimal
    gross_value: Decimal
    net_cash_impact: Decimal
    realized_pnl: Decimal
    executed_at: datetime
    execution_reference: Optional[str] = None


@dataclass(frozen=True)
class PositionChange:
    """What one fill did to a position, for reporting and for the audit trail.

    ``new_quantity`` is 0 when the fill closed the position: the position row is then removed
    rather than stored at zero, and the realised P&L it booked survives on the portfolio and in
    the trade history.
    """

    stock_id: int
    symbol: Optional[str]
    previous_quantity: int
    new_quantity: int
    previous_average_entry_price: Optional[Decimal]
    new_average_entry_price: Optional[Decimal]
    previous_market_value: Decimal
    new_market_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    closed: bool = False


@dataclass(frozen=True)
class CashChange:
    """What one fill did to a portfolio's cash, recorded per fill rather than in aggregate.

    ``cash_after`` is the balance after *this* fill, so the sequence of fills of one order can
    be verified without replaying anything.
    """

    portfolio_id: int
    execution_id: str
    cash_before: Decimal
    cash_delta: Decimal
    cash_after: Decimal


# ----------------------------------------------------------------------
# Validation, risk and reconciliation results
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationFinding:
    """One structured reason a plan is not acceptable, or is acceptable with a caveat.

    ``code`` is a stable machine-readable string — normally the shared
    :class:`~app.schemas.portfolio.RejectionCode` value — so a caller can branch on the reason
    without parsing ``message``, which is prose written for a human.
    """

    code: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.ERROR
    stock_id: Optional[int] = None
    details: Dict = field(default_factory=dict)


@dataclass(frozen=True)
class PlanValidationResult:
    """The structured outcome of validating a plan.

    Every finding is kept, not just the first: a plan that is short of cash *and* over its
    position cap should say so in one response, and reporting one problem per round trip would
    let a caller fix them one at a time.
    """

    portfolio_id: int
    plan_id: str
    findings: Tuple[ValidationFinding, ...] = ()

    @property
    def errors(self) -> Tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity is ValidationSeverity.ERROR)

    @property
    def warnings(self) -> Tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity is ValidationSeverity.WARNING)

    @property
    def valid(self) -> bool:
        """True when nothing blocking was found. Warnings never make a plan invalid."""
        return not self.errors

    @property
    def error_codes(self) -> Tuple[str, ...]:
        return tuple(f.code for f in self.errors)

    def with_finding(
        self,
        code: str,
        message: str,
        severity: ValidationSeverity = ValidationSeverity.ERROR,
        stock_id: Optional[int] = None,
        details: Optional[Dict] = None,
    ) -> "PlanValidationResult":
        """Return a copy with one more finding appended."""
        return PlanValidationResult(
            portfolio_id=self.portfolio_id,
            plan_id=self.plan_id,
            findings=self.findings
            + (
                ValidationFinding(
                    code=code,
                    message=message,
                    severity=severity,
                    stock_id=stock_id,
                    details=details or {},
                ),
            ),
        )


@dataclass(frozen=True)
class RiskGateResult:
    """The pre-trade gate's verdict, with the reasons that produced it.

    ``approved`` is the only thing the execution engine branches on. ``findings`` is kept even
    when approved, because an approved order that came close to a limit is the one an operator
    wants to see afterwards.
    """

    approved: bool
    findings: Tuple[ValidationFinding, ...] = ()
    decision: RiskGateDecision = RiskGateDecision.APPROVED

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision",
            RiskGateDecision.APPROVED if self.approved else RiskGateDecision.REJECTED,
        )


@dataclass(frozen=True)
class Discrepancy:
    """One difference between expected and actual portfolio state.

    Recorded, never repaired. An unexplained repair would leave the books agreeing with each
    other and disagreeing with the fills that produced them.
    """

    code: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.ERROR
    stock_id: Optional[int] = None
    expected: Optional[str] = None
    actual: Optional[str] = None
    details: Dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationResult:
    """The outcome of comparing expected state against the state actually recorded.

    ``expected`` is derived from the plan and the fills that were supposed to happen; ``actual``
    is read back from the portfolio after the write. Comparing the two independently is the
    whole point — deriving "actual" from the same arithmetic that produced "expected" would
    make the check incapable of failing.
    """

    portfolio_id: int
    status: ReconciliationStatus
    checked_at: datetime
    expected_cash: Optional[Decimal] = None
    actual_cash: Optional[Decimal] = None
    expected_equity: Optional[Decimal] = None
    actual_equity: Optional[Decimal] = None
    discrepancies: Tuple[Discrepancy, ...] = ()
    warnings: Tuple[Discrepancy, ...] = ()

    @property
    def is_clean(self) -> bool:
        return self.status is ReconciliationStatus.MATCHED

    @property
    def has_discrepancies(self) -> bool:
        return bool(self.discrepancies)

    @classmethod
    def build(
        cls,
        portfolio_id: int,
        checked_at: datetime,
        discrepancies: Tuple[Discrepancy, ...],
        warnings: Tuple[Discrepancy, ...] = (),
        expected_cash: Optional[Decimal] = None,
        actual_cash: Optional[Decimal] = None,
        expected_equity: Optional[Decimal] = None,
        actual_equity: Optional[Decimal] = None,
    ) -> "ReconciliationResult":
        """Derive the status from the findings, so the three can never disagree.

        Any error-severity discrepancy makes the result ``MISMATCHED``; warnings alone make it
        ``WARNING``; neither leaves it ``MATCHED``. Computing the status here rather than at
        each call site is what stops a report claiming ``MATCHED`` while listing a discrepancy.
        """
        if any(d.severity is ValidationSeverity.ERROR for d in discrepancies):
            status = ReconciliationStatus.MISMATCHED
        elif warnings:
            status = ReconciliationStatus.WARNING
        else:
            status = ReconciliationStatus.MATCHED
        return cls(
            portfolio_id=portfolio_id,
            status=status,
            checked_at=checked_at,
            expected_cash=expected_cash,
            actual_cash=actual_cash,
            expected_equity=expected_equity,
            actual_equity=actual_equity,
            discrepancies=discrepancies,
            warnings=warnings,
        )


# ----------------------------------------------------------------------
# Order state machine
# ----------------------------------------------------------------------


#: States from which no further transition is possible. A terminal order is settled: its fills
#: are recorded, its cash has moved, and re-submitting it would book the trade twice.
TERMINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    }
)

#: States in which an order is still working and can still receive fills.
ACTIVE_ORDER_STATUSES = frozenset(
    {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}
)

#: Every legal transition, and nothing else.
#:
#: The shape of the lifecycle is the one the execution path actually walks:
#:
#:     CREATED -> VALIDATED -> SUBMITTED -> PARTIALLY_FILLED -> FILLED
#:                  |             |                |               |
#:                  v             v                v               v (terminal)
#:              REJECTED     FAILED/CANCELLED   CANCELLED
#:
#: Two absences are deliberate. ``FILLED -> SUBMITTED`` is impossible because an order that has
#: filled cannot be sent again, and ``PARTIALLY_FILLED -> SUBMITTED`` is impossible because a
#: working order that has already filled is not being re-sent — it is waiting for the rest.
#:
#: ``PENDING`` is Phase 9's single-shot state and is included so the machine describes the whole
#: enum. Nothing in Phase 9 consults it: the Phase 9 path sets ``FILLED`` directly, exactly as
#: before.
VALID_ORDER_TRANSITIONS: Dict[OrderStatus, frozenset] = {
    OrderStatus.CREATED: frozenset({OrderStatus.VALIDATED, OrderStatus.REJECTED}),
    OrderStatus.VALIDATED: frozenset(
        {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.FAILED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.FAILED}
    ),
    OrderStatus.PENDING: frozenset(
        {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.FAILED: frozenset(),
}


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    """True when ``current -> target`` is a legal move.

    Unknown states are refused rather than defaulted: an order whose status is not one this
    machine knows has been written by something else, and guessing that it may move would be
    the one answer that cannot be justified.
    """
    allowed = VALID_ORDER_TRANSITIONS.get(current)
    if allowed is None:
        return False
    return target in allowed


def legal_transitions(current: OrderStatus) -> frozenset:
    """The states ``current`` may move to — the empty set for a terminal or unknown state."""
    return VALID_ORDER_TRANSITIONS.get(current, frozenset())


def status_after_fill(
    current: OrderStatus, filled_quantity: int, requested_quantity: int
) -> OrderStatus:
    """The status an order takes after a fill of ``filled_quantity`` shares.

    ``PARTIALLY_FILLED`` while anything is still outstanding, ``FILLED`` once the outstanding
    quantity reaches zero. A fill that would take the order *past* the requested quantity is
    refused by the caller rather than clamped here: an over-fill is a defect, and reporting it
    as a full fill would hide it.
    """
    if filled_quantity > requested_quantity:
        raise ValueError(
            f"filled quantity {filled_quantity} exceeds the requested {requested_quantity}"
        )
    if filled_quantity >= requested_quantity:
        return OrderStatus.FILLED
    return OrderStatus.PARTIALLY_FILLED