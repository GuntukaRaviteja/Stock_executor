"""Phase 16 position and cash accounting — what one fill does to the books.

Every figure here is produced by the existing Phase 9 money functions —
:func:`~app.services.portfolio.calculator.apply_buy`, :func:`apply_sell`,
:func:`~app.services.portfolio.calculator.calculate_fee` — so Phase 16 cannot disagree with
Phases 9-15 about a weighted average entry price, a realised P&L, or what a fill cost. This
module adds no arithmetic of its own; it applies those rules one fill at a time and reports what
changed.

Accounting convention (inherited, not redefined)
------------------------------------------------
**Positions are long-only and average-cost.**

* a **buy** adds to the position and moves ``average_entry_price`` to the quantity-weighted
  average of the old position and the fill, with entry fees accumulated separately so the average
  stays a clean average of execution prices;
* a **sell** reduces the position without resetting its average cost, and books realised P&L of
  ``quantity * (fill price - average entry price) - exit fees - allocated entry fees`` — net of
  the entry fees attributable to the shares sold, allocated pro rata;
* a sell that takes the quantity to zero **closes** the position. There is no row at zero: the
  position is removed, and its realised P&L survives on the portfolio and in the trade history.

**Cash** moves by ``execution_value + fees`` out on a buy and ``execution_value - fees`` in on a
sell, rounded once. Cash is never allowed below zero: a fill that would take it below zero is
refused before it is applied, which is what makes "cash can never go negative" a property of the
engine rather than an outcome it hopes for.

Partial fills are just repeated application
-------------------------------------------
Two fills of the same order are two applications of the same rules. Nothing about a partial fill
is special-cased: the second fill sees the state the first one produced and updates it the same
way. That is what stops a partial fill from creating a second position — there is only ever one
row per (portfolio, stock), and the average price moves once per fill.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional, Tuple

from app.core.logging import logger
from app.schemas.portfolio import OrderSide
from app.services.execution.models import (
    CashChange,
    ExecutionFill,
    PositionChange,
)
from app.services.backtest.models import quantize_money
from app.services.portfolio.calculator import (
    apply_buy,
    apply_sell,
    market_value,
    unrealized_pnl,
)
from app.services.portfolio.models import ZERO, PositionState


class InsufficientCashForFillError(ValueError):
    """A fill would take cash below zero.

    Raised rather than applied. Allowing it would make equity, exposure and every weight
    measured against equity meaningless — the same reason the existing Phase 9 withdrawal and
    order paths refuse an unfunded movement instead of tolerating a negative balance.
    """


class InsufficientHoldingsError(ValueError):
    """A fill would take a position negative.

    The engine is long-only, so a sell beyond the holding is refused rather than turned into a
    short. ``requested`` and ``owned`` are carried so the refusal can name both numbers.
    """

    def __init__(self, stock_id: int, requested: int, owned: int):
        super().__init__(
            f"cannot sell {requested} share(s) of stock {stock_id} while holding {owned}: "
            "this engine is long-only"
        )
        self.stock_id = stock_id
        self.requested = requested
        self.owned = owned


def apply_fill_to_position(
    position: Optional[PositionState],
    fill: ExecutionFill,
) -> Tuple[Optional[PositionState], PositionChange]:
    """Apply one fill to one position, returning the new position state and what changed.

    ``new_position`` is ``None`` when the fill closed the position. The returned
    :class:`~app.services.execution.models.PositionChange` still reports the quantities either
    way, with ``closed`` set, so the audit trail records that the position went to zero rather
    than that nothing happened.

    Selling more than is held raises :class:`InsufficientHoldingsError`. Long-only means the
    alternative would be a short position, and a negative quantity is the one outcome this
    engine will never record.
    """
    if fill.side is OrderSide.BUY:
        new_position, _ = apply_buy(
            position, fill.quantity, fill.execution_price, fill.fees, fill.stock_id
        )
        change = _describe(position, new_position, fill, realized=ZERO, closed=False)
        return new_position, change

    if position is None or position.quantity <= 0:
        raise InsufficientHoldingsError(fill.stock_id, fill.quantity, 0)
    if fill.quantity > position.quantity:
        raise InsufficientHoldingsError(fill.stock_id, fill.quantity, position.quantity)

    remaining, realized, _ = apply_sell(
        position, fill.quantity, fill.execution_price, fill.fees
    )
    change = _describe(
        position,
        remaining,
        fill,
        realized=realized,
        closed=remaining is None,
    )
    return remaining, change


def _describe(
    before: Optional[PositionState],
    after: Optional[PositionState],
    fill: ExecutionFill,
    *,
    realized: Decimal,
    closed: bool,
) -> PositionChange:
    """Summarise what a fill did to a position, at the fill's own price as the new mark.

    The fill price is used as the post-fill mark because it is the last price this engine knows
    the stock traded at. Marking to anything else would report an unrealised P&L for a price no
    fill or valuation actually observed.
    """
    previous_quantity = before.quantity if before else 0
    previous_average = before.average_entry_price if before else None
    previous_market = before.market_value() if before else ZERO
    new_quantity = after.quantity if after else 0
    new_average = after.average_entry_price if after else None
    new_market = (
        market_value(new_quantity, fill.execution_price) if after else quantize_money(ZERO)
    )
    new_unrealized = (
        unrealized_pnl(new_quantity, new_average, fill.execution_price)
        if after and new_average is not None
        else quantize_money(ZERO)
    )
    return PositionChange(
        stock_id=fill.stock_id,
        symbol=fill.symbol,
        previous_quantity=previous_quantity,
        new_quantity=new_quantity,
        previous_average_entry_price=previous_average,
        new_average_entry_price=new_average,
        previous_market_value=previous_market,
        new_market_value=new_market,
        realized_pnl=quantize_money(realized),
        unrealized_pnl=new_unrealized,
        closed=closed,
    )


def apply_fill_to_cash(cash: Decimal, fill: ExecutionFill) -> CashChange:
    """Move cash by one fill's signed impact and report the balance before and after."""
    cash_before = quantize_money(cash)
    cash_after = quantize_money(cash_before + fill.net_cash_impact)
    if cash_after < ZERO:
        raise InsufficientCashForFillError(
            f"fill {fill.execution_id} would take cash from {cash_before} to {cash_after}; a "
            "long-only portfolio with no margin cannot fund it"
        )
    return CashChange(
        portfolio_id=fill.portfolio_id,
        execution_id=fill.execution_id,
        cash_before=cash_before,
        cash_delta=fill.net_cash_impact,
        cash_after=cash_after,
    )


def expected_cash_after(
    opening_cash: Decimal, fills: Tuple[ExecutionFill, ...]
) -> Decimal:
    """The cash a set of fills must leave behind.

    Derived from the fills' own signed impacts, not recomputed from prices and fees, so the
    figure reconciliation checks against is the one the booking path actually used.
    """
    return quantize_money(
        Decimal(opening_cash) + sum((fill.net_cash_impact for fill in fills), ZERO)
    )


def expected_positions(
    positions: Dict[int, PositionState], fills: Tuple[ExecutionFill, ...]
) -> Dict[int, PositionState]:
    """The position states a set of fills must produce, keyed by ``stock_id``.

    Used by reconciliation to build the *expected* side of the comparison from the fills alone.
    A stock whose fills close the position is absent from the result rather than present at
    zero, because a closed position has no row: an "expected" set that kept one would report a
    spurious discrepancy against a portfolio that closed it correctly.
    """
    expected: Dict[int, PositionState] = dict(positions)
    for fill in fills:
        state = expected.get(fill.stock_id)
        new_state = apply_fill_to_position(state, fill)[0]
        if new_state is None:
            expected.pop(fill.stock_id, None)
        else:
            expected[fill.stock_id] = new_state
    return expected


def total_realized(fills: Tuple[ExecutionFill, ...]) -> Decimal:
    """Realised P&L booked across a set of fills, rounded once."""
    return quantize_money(sum((fill.realized_pnl for fill in fills), ZERO))


def log_fill(fill: ExecutionFill, change: PositionChange, cash: CashChange) -> None:
    """One line per booked fill, naming the position and cash effects together.

    A fill is one event that moves several books at once; logging the position update without the
    cash movement would make the two look like independent events that could disagree.
    """
    logger.info(
        "Booked fill %s: %s %s share(s) of stock %s at %s (fees %s) -> position %s -> %s, "
        "cash %s -> %s, realised P&L %s",
        fill.execution_id,
        fill.side.value,
        fill.quantity,
        fill.stock_id,
        fill.execution_price,
        fill.fees,
        change.previous_quantity,
        change.new_quantity,
        cash.cash_before,
        cash.cash_after,
        change.realized_pnl,
    )