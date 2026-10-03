"""Phase 16 execution adapter interface — the seam a real broker plugs into.

**No real broker is connected in Phase 16.** This module defines the contract and nothing
else: no HTTP client, no credential, no session, no endpoint. The only implementation shipped
is :class:`~app.services.execution.adapters.simulated.SimulatedExecutionAdapter`.

Why the seam exists
-------------------
Portfolio logic must not know how an order becomes a fill. Planning, validation, the risk gate,
position accounting, cash accounting and reconciliation all speak in terms of *requests*
(:class:`~app.services.execution.models.ExecutionOrder`) and *results* (submissions, fills,
statuses). An adapter turns the first into the second. Because the seam is this narrow, a future
adapter that talks to a real broker's API is a new implementation of six methods and **no change
to any portfolio code, any endpoint, or any test above the seam**.

What the contract deliberately does not promise
-----------------------------------------------
*Realistic* fill behaviour. ``submit_order`` returns what the venue said; it does not invent a
fill for an order it refused, and it does not report a fill it did not produce. That honesty is
what makes reconciliation worth running — a seam that quietly manufactured fills would leave
reconciliation comparing a number against itself.

A note on the vocabulary
------------------------
The reference fields are named ``execution_reference`` / ``order_reference`` rather than
anything broker-specific. They hold whatever the venue calls an order or a fill id, which is
exactly what a real adapter would put there; the names state the role, never a particular
vendor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, Optional, Tuple

from app.core.logging import logger
from app.schemas.portfolio import OrderStatus
from app.services.execution.models import ExecutionFill, ExecutionOrder


@dataclass(frozen=True)
class AdapterAccountState:
    """What the venue reports about the account.

    ``positions`` maps ``stock_id`` to the shares the venue believes are held. For the simulated
    adapter this is the engine's own view; for a real adapter it is the broker's holding, which
    is precisely why reconciliation compares the two instead of trusting either one.
    """

    portfolio_id: int
    cash: Decimal
    positions: Dict[int, int] = field(default_factory=dict)
    as_of: Optional[datetime] = None


@dataclass(frozen=True)
class OrderSubmission:
    """What the venue said when the order was submitted.

    ``accepted`` false is a refusal, and it carries a reason: an order refused without one cannot
    be reported to a caller or recorded in the audit trail. An accepted order with no fills is
    legitimate and normal — that is a working order.
    """

    accepted: bool
    order_reference: Optional[str] = None
    status: OrderStatus = OrderStatus.SUBMITTED
    fills: Tuple[ExecutionFill, ...] = ()
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def filled_quantity(self) -> int:
        return sum(fill.quantity for fill in self.fills)


@dataclass(frozen=True)
class OrderState:
    """The venue's current view of one order: its status and any fills it has produced."""

    order_reference: Optional[str]
    status: OrderStatus
    fills: Tuple[ExecutionFill, ...] = ()
    filled_quantity: int = 0
    remaining_quantity: int = 0
    as_of: Optional[datetime] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.FAILED,
        )


class ExecutionAdapter(ABC):
    """The contract every execution venue implements.

    Implementations must never fabricate a fill: a fill returned here is booked as a position
    change, a cash movement and a trade record, so an invented one is a fabricated financial
    transaction.
    """

    #: Identifies the implementation in audit metadata, e.g. ``SIMULATED``. A real adapter would
    #: report its own name. Nothing above this seam branches on it.
    name: str = "ADAPTER"

    @abstractmethod
    def submit_order(
        self,
        order: ExecutionOrder,
        *,
        reference_price: Decimal,
        as_of: datetime,
    ) -> OrderSubmission:
        """Submit one validated order and report what the venue did with it.

        Returning an accepted order with no fills is valid and means the order is working. An
        adapter that fills synchronously returns those fills here; one that does not returns none
        and is polled through :meth:`get_order_status`.
        """

    @abstractmethod
    def cancel_order(self, order_reference: str, *, as_of: datetime) -> OrderState:
        """Cancel a working order.

        Cancelling never unwinds a fill that already happened: fills recorded before the
        cancellation stay booked and only the unfilled remainder is released.
        """

    @abstractmethod
    def get_order_status(self, order_reference: str, *, as_of: datetime) -> OrderState:
        """The venue's current view of an order, including any new fills."""

    @abstractmethod
    def get_positions(self, portfolio_id: int, *, as_of: datetime) -> Dict[int, int]:
        """The shares the venue believes are held, keyed by ``stock_id``."""

    @abstractmethod
    def get_cash_balance(self, portfolio_id: int, *, as_of: datetime) -> Decimal:
        """The cash the venue believes is available."""


def order_reference_for(order: ExecutionOrder) -> str:
    """The deterministic venue reference for an order.

    Derived from the order's idempotency key rather than generated, so re-submitting the same
    request addresses the same venue order instead of creating a second one. That is the whole
    reason idempotency can be enforced above the seam at all.
    """
    return order.idempotency_key


def fill_reference_for(order: ExecutionOrder, sequence: int) -> str:
    """The deterministic venue reference for one fill of an order.

    ``sequence`` is the fill's 1-based position within the order, so the second fill of a
    partially filled order gets a different reference from the first and each is individually
    de-duplicable against ``portfolio_executions``.
    """
    return f"{order.idempotency_key}-{sequence}"


def log_submission(order: ExecutionOrder, submission: OrderSubmission, adapter_name: str) -> None:
    """One log line for every submission, accepted or refused.

    A refusal is logged at warning level and an acceptance at info level, so "what was refused
    and why" is findable without reading the audit table.
    """
    reason = f": {submission.error_message}" if submission.error_message else ""
    logger.log(
        30 if not submission.accepted else 20,
        "Adapter %s %s order %s (portfolio=%s stock=%s side=%s qty=%s) -> %s%s",
        adapter_name,
        "accepted" if submission.accepted else "refused",
        order.client_order_id,
        order.portfolio_id,
        order.plan_item.stock_id,
        order.plan_item.side.value,
        order.plan_item.quantity,
        submission.status.value,
        reason,
    )