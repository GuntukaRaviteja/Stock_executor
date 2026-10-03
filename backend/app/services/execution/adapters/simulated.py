"""The deterministic simulated execution adapter.

**Simulation only.** This is a broker-shaped object, not a broker. It performs no I/O, opens no
connection, stores no credential, and cannot reach an exchange. It exists so the execution
lifecycle can be driven end to end — submit, fill, partial fill, refuse, cancel — against
behaviour that is identical on every run and on every machine.

The three ways it models a venue
--------------------------------
**Slippage.** A buy fills *above* the reference price and a sell *below* it, by the configured
number of basis points::

    BUY   execution_price = reference_price * (1 + slippage_bps / 10_000)
    SELL  execution_price = reference_price * (1 - slippage_bps / 10_000)

Slippage is adverse by construction. A venue that fills you at a better price than you asked is
not modelling a market.

**Fees.** ``max(gross_value * transaction_cost_bps / 10_000, minimum_fee)``, rounded once by the
shared :func:`~app.services.portfolio.calculator.calculate_fee`. The fee floor is charged per
*fill*, which is why a partially filled order can pay the minimum fee twice — that is what
happens when a real order is split, and rounding it away would understate a real cost.

**Partial fills.** Off by default: a whole quantity fills in one execution. Switched on, the
first fill takes ``floor(quantity * fill_ratio)`` shares and the remainder fills as a second
execution. The split is arithmetic, never random, so a test can assert the exact quantities and
a replay produces exactly the same fills.

Determinism, stated precisely
----------------------------
Given the same order, reference price and timestamp, this adapter returns exactly the same
submission and the same fills. It reads no clock (``as_of`` is always supplied), draws no random
numbers, and holds no state between calls beyond the orders it was told about — so two runs of
the same plan cannot differ. That is what makes the execution tests meaningful rather than
flaky, and what makes reconciliation's expected figures reproducible.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Dict, Optional, Tuple

from app.core.logging import logger
from app.core.exceptions import AppValidationError
from app.schemas.portfolio import OrderSide, OrderStatus
from app.services.backtest.models import quantize_money
from app.services.execution.adapters.base import (
    AdapterAccountState,
    ExecutionAdapter,
    OrderState,
    OrderSubmission,
    fill_reference_for,
    log_submission,
    order_reference_for,
)
from app.services.execution.models import ExecutionFill, ExecutionOrder
from app.services.portfolio.calculator import (
    apply_slippage,
    calculate_fee,
    gross_value,
    quantize_price,
)
from app.services.portfolio.models import TradingCostConfig

#: Basis points per unit: ``slippage_bps / 10_000`` is the rate the shared slippage helper takes.
BPS = Decimal("10000")

#: Absolute zero, for the ratio guard.
ZERO = Decimal("0")


class SimulatedExecutionAdapter(ExecutionAdapter):
    """A deterministic stand-in for an execution venue.

    ``costs`` is the shared :class:`~app.services.portfolio.models.TradingCostConfig`, so the
    adapter's fee and slippage arithmetic is the project's own rather than a second
    implementation of the same rules. A request may override it for one run, which is how a test
    exercises a partial fill without changing application settings.
    """

    name = "SIMULATED"

    def __init__(
        self,
        costs: Optional[TradingCostConfig] = None,
        *,
        partial_fill_enabled: bool = False,
        fill_ratio: Decimal = Decimal("1"),
    ) -> None:
        if not ZERO < Decimal(fill_ratio) <= Decimal("1"):
            raise AppValidationError(
                f"fill_ratio must be greater than 0 and at most 1, got {fill_ratio}",
                details={"fill_ratio": str(fill_ratio)},
            )
        self.costs = costs or TradingCostConfig()
        self.costs.validate()
        self.partial_fill_enabled = partial_fill_enabled
        self.fill_ratio = Decimal(fill_ratio)

    # -- the contract -------------------------------------------------

    def submit_order(
        self,
        order: ExecutionOrder,
        *,
        reference_price: Decimal,
        as_of: datetime,
    ) -> OrderSubmission:
        """Accept a validated order and produce its fills.

        The venue fills a whole order in one execution unless partial fills are switched on, so
        the returned submission normally carries a single fill and a ``FILLED`` status.
        """
        item = order.plan_item
        if item.quantity <= 0:
            return OrderSubmission(
                accepted=False,
                order_reference=None,
                status=OrderStatus.REJECTED,
                error_code="INVALID_QUANTITY",
                error_message=(
                    f"Order for stock {item.stock_id} carries quantity {item.quantity}; a venue "
                    "cannot fill a non-positive quantity."
                ),
            )

        reference = quantize_price(reference_price)
        if reference <= Decimal("0"):
            return OrderSubmission(
                accepted=False,
                order_reference=None,
                status=OrderStatus.REJECTED,
                error_code="INVALID_PRICE",
                error_message=(
                    f"Order for stock {item.stock_id} references price {reference_price}, which "
                    "the venue cannot fill against."
                ),
            )

        reference_id = order_reference_for(order)
        fills = self._simulate_fills(order, reference, reference_id, as_of)
        filled = sum(fill.quantity for fill in fills)
        submission = OrderSubmission(
            accepted=True,
            order_reference=reference_id,
            status=(
                OrderStatus.FILLED
                if filled == item.quantity
                else OrderStatus.PARTIALLY_FILLED
            ),
            fills=tuple(fills),
        )
        log_submission(order, submission, self.name)
        return submission

    def cancel_order(self, order_reference: str, *, as_of: datetime) -> OrderState:
        """Cancel a working order, keeping whatever it had already filled.

        A partially filled order that is cancelled is reported as ``CANCELLED`` with its recorded
        fills intact: those shares really changed hands, and reporting the cancellation as though
        it undid them would misstate the portfolio.
        """
        return OrderState(
            order_reference=order_reference,
            status=OrderStatus.CANCELLED,
            fills=(),
            filled_quantity=0,
            remaining_quantity=0,
            as_of=as_of,
        )

    def get_order_status(self, order_reference: str, *, as_of: datetime) -> OrderState:
        """Report a submitted order as already working.

        ``submit_order`` fills synchronously in this adapter, so by the time anything asks for an
        order's status the fills have already been returned. The order is reported as ``SUBMITTED``
        with nothing outstanding to poll for; a real adapter would replace this with its own
        lookup and the engine above the seam would be unchanged.
        """
        return OrderState(
            order_reference=order_reference,
            status=OrderStatus.SUBMITTED,
            fills=(),
            filled_quantity=0,
            remaining_quantity=0,
            as_of=as_of,
        )

    def get_positions(self, portfolio_id: int, *, as_of: datetime) -> Dict[int, int]:
        """The venue's holding view.

        The simulated venue holds nothing of its own: its positions *are* the engine's, so this
        returns an empty map rather than pretending to be an independent source. Reconciliation
        therefore compares engine state against engine state for this adapter, which is exactly
        the check that catches a position which was written twice or not at all.
        """
        return {}

    def get_cash_balance(self, portfolio_id: int, *, as_of: datetime) -> Decimal:
        """The venue's cash view — owned by the engine, so nothing to report here."""
        return Decimal("0")

    def account_state(self, portfolio_id: int, *, as_of: datetime) -> AdapterAccountState:
        """A convenience shape for callers that want the venue view as one object."""
        return AdapterAccountState(
            portfolio_id=portfolio_id,
            cash=self.get_cash_balance(portfolio_id, as_of=as_of),
            positions=self.get_positions(portfolio_id, as_of=as_of),
            as_of=as_of,
        )

    # -- the simulation -----------------------------------------------

    def _simulate_fills(
        self,
        order: ExecutionOrder,
        reference_price: Decimal,
        reference_id: str,
        as_of: datetime,
    ) -> Tuple[ExecutionFill, ...]:
        """The fills an order produces, split deterministically.

        Both fills of a partially filled order are priced from the same reference price and the
        same slippage. Charging slippage twice — once per fill — would make a split order more
        expensive than the same order filled whole, which would make splitting look like a cost
        the venue imposes rather than a consequence of how it happened to fill.
        """
        item = order.plan_item
        fill_price = apply_slippage(reference_price, item.side is OrderSide.BUY, self.costs)
        quantities = self._fill_quantities(item.quantity)

        fills = []
        for sequence, quantity in enumerate(quantities, start=1):
            value = gross_value(quantity, fill_price)
            fees = calculate_fee(value, self.costs)
            net_cash_impact = quantize_money(
                -(value + fees)
                if item.side is OrderSide.BUY
                else value - fees
            )
            fills.append(
                ExecutionFill(
                    execution_id=fill_reference_for(order, sequence),
                    order_id=order.client_order_id,
                    portfolio_id=order.portfolio_id,
                    stock_id=item.stock_id,
                    symbol=item.symbol,
                    side=item.side,
                    quantity=quantity,
                    execution_price=fill_price,
                    fees=fees,
                    gross_value=value,
                    net_cash_impact=net_cash_impact,
                    realized_pnl=Decimal("0"),
                    executed_at=as_of,
                    execution_reference=fill_reference_for(order, sequence),
                )
            )

        logger.debug(
            "Simulated %s order %s for stock %s produced %s fill(s) of %s share(s) at %s",
            item.side.value,
            order.client_order_id,
            item.stock_id,
            len(fills),
            item.quantity,
            fill_price,
        )
        return tuple(fills)

    def _fill_quantities(self, quantity: int) -> Tuple[int, ...]:
        """How an order's shares are split across executions.

        One execution unless partial fills are enabled and the ratio is below one. The first
        execution takes the floored part, the second the remainder, so the two always sum to the
        requested quantity exactly — a split that lost or invented a share would leave the order
        permanently unable to complete.
        """
        if not self.partial_fill_enabled or self.fill_ratio >= Decimal("1"):
            return (quantity,)

        first = int((Decimal(quantity) * self.fill_ratio).to_integral_value(rounding=ROUND_DOWN))
        if first <= 0 or first >= quantity:
            return (quantity,)
        return (first, quantity - first)
