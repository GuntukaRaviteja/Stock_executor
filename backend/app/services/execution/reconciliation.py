"""Phase 16 portfolio reconciliation — expected state versus actual state.

What it is for
--------------
Execution writes several rows per fill: an execution, a position change, a cash movement, a
trade, a ledger entry. A defect anywhere in that sequence shows up as a portfolio that no longer
adds up, and the only way to find it is to check the arithmetic independently of the code that
produced it. Reconciliation does exactly that:

* **expected** is derived from the plan and the fills that were supposed to happen;
* **actual** is read back from the portfolio *after* the write.

Deriving both sides from the same arithmetic would leave the check incapable of failing, so
nothing here reuses the booking path's intermediate results. The expected quantities are reached
by replaying the recorded fills over the state that preceded them; the expected cash is the
opening balance plus the fills' own signed impacts. The actual figures are whatever is stored
right now.

What it will find
-----------------
A quantity that does not match its target, a cash balance that does not match the fills, a
position that should exist and does not, one that should not exist and does, an average entry
price that drifted, an equity that does not reconcile, an order left in a non-terminal state, and
a fill recorded twice.

It repairs nothing
------------------
A mismatch is reported, never corrected. A silent repair would leave the books agreeing with each
other and disagreeing with the fills that produced them — the exact state reconciliation exists
to expose. Repair is an explicit operation, performed by a human, from the discrepancy list.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from app.schemas.portfolio import OrderStatus
from app.services.execution.accounting import expected_cash_after, expected_positions
from app.services.execution.models import (
    Discrepancy,
    ExecutionFill,
    OrderPlan,
    ReconciliationResult,
    ValidationSeverity,
)
from app.services.backtest.models import quantize_money
from app.services.portfolio.models import ZERO, PositionState

#: Order states in which an order is still working and has not reached an outcome. An order left
#: in one of these after the portfolio has moved on is an orphan: it may still fill later, and
#: nothing in the books has accounted for it.
WORKING_ORDER_STATUSES = frozenset(
    {
        OrderStatus.CREATED.value,
        OrderStatus.VALIDATED.value,
        OrderStatus.SUBMITTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
        OrderStatus.PENDING.value,
    }
)


def reconcile_execution(
    plan: OrderPlan,
    *,
    fills: Sequence[ExecutionFill],
    opening_cash: Decimal,
    opening_positions: Mapping[int, PositionState],
    actual_cash: Decimal,
    actual_positions: Mapping[int, PositionState],
    actual_equity: Optional[Decimal] = None,
    order_statuses: Mapping[int, str] = (),
    filled_quantities: Mapping[int, int] = (),
    checked_at: datetime,
) -> ReconciliationResult:
    """Compare what the plan intended with what the portfolio now holds.

    ``opening_cash`` / ``opening_positions`` are the state execution started from — captured
    *before* the first write — so the expected side can be replayed from a known baseline rather
    than inferred from the current one.

    ``order_statuses`` and ``filled_quantities`` are keyed by stock id. Without them the
    incomplete- and duplicate-execution checks are skipped rather than guessed: reporting "this
    order never completed" from data that was never collected would be a claim with nothing
    behind it.
    """
    booked = tuple(fills)
    expected = expected_positions(opening_positions, booked)
    discrepancies: List[Discrepancy] = []
    warnings: List[Discrepancy] = []

    discrepancies.extend(_check_target_quantities(plan, expected))
    discrepancies.extend(_check_positions(expected, actual_positions))
    discrepancies.extend(_check_cash(expected_cash_after(opening_cash, booked), actual_cash))
    discrepancies.extend(_check_duplicate_fills(booked))

    if filled_quantities:
        discrepancies.extend(_check_filled_quantities(plan, filled_quantities))
    if order_statuses:
        discrepancies.extend(_check_orphan_orders(order_statuses))

    if actual_equity is not None:
        warnings.extend(_check_valuation(expected, expected_cash_after(opening_cash, booked), actual_equity))

    return ReconciliationResult.build(
        portfolio_id=plan.portfolio_id,
        checked_at=checked_at,
        discrepancies=tuple(discrepancies),
        warnings=tuple(warnings),
        expected_cash=expected_cash_after(opening_cash, booked),
        actual_cash=quantize_money(actual_cash),
        actual_equity=None if actual_equity is None else quantize_money(actual_equity),
    )


def reconcile_account(
    *,
    portfolio_id: int,
    expected: Mapping[int, int],
    actual: Mapping[int, int],
    expected_cash: Optional[Decimal] = None,
    actual_cash: Optional[Decimal] = None,
    checked_at: datetime,
) -> ReconciliationResult:
    """Reconcile a stored portfolio against a caller-supplied expected state.

    This is the standalone check behind the reconciliation endpoint, for verifying a portfolio
    against the target it is meant to satisfy with no execution run involved. The expected
    quantities are supplied rather than derived, because here the target is a fact about what the
    portfolio *should* hold rather than a consequence of fills already recorded.
    """
    discrepancies: List[Discrepancy] = list(_check_quantities(expected, actual))
    if expected_cash is not None and actual_cash is not None:
        discrepancies.extend(
            _check_cash(quantize_money(expected_cash), quantize_money(actual_cash))
        )

    return ReconciliationResult.build(
        portfolio_id=portfolio_id,
        checked_at=checked_at,
        discrepancies=tuple(discrepancies),
        expected_cash=None if expected_cash is None else quantize_money(expected_cash),
        actual_cash=None if actual_cash is None else quantize_money(actual_cash),
    )


# ----------------------------------------------------------------------
# Individual checks
# ----------------------------------------------------------------------


def _check_target_quantities(
    plan: OrderPlan, expected: Mapping[int, PositionState]
) -> Tuple[Discrepancy, ...]:
    """Every stock the plan traded must now hold the quantity the plan targeted.

    An under-filled item is a discrepancy, not a warning: the portfolio does not hold what it was
    told to hold, and softening that to a warning would let a partially executed plan read as
    acceptable.
    """
    found = []
    for item in plan.order_items:
        state = expected.get(item.stock_id)
        quantity = state.quantity if state else 0
        if quantity == item.target_quantity:
            continue
        found.append(
            Discrepancy(
                code="incomplete_execution",
                message=(
                    f"Stock {item.stock_id} was planned to reach {item.target_quantity} share(s) "
                    f"from {item.current_quantity} but the recorded fills leave {quantity}. The "
                    "order did not complete."
                ),
                stock_id=item.stock_id,
                expected=str(item.target_quantity),
                actual=str(quantity),
                details={
                    "quantity_delta": str(item.quantity_delta),
                    "side": item.side.value,
                },
            )
        )
    return tuple(found)


def _check_positions(
    expected: Mapping[int, PositionState], actual: Mapping[int, PositionState]
) -> Tuple[Discrepancy, ...]:
    """Compare the replayed position set with the stored one, symbol by symbol."""
    found = []
    for stock_id in sorted(set(expected) | set(actual)):
        expected_state = expected.get(stock_id)
        actual_state = actual.get(stock_id)

        if expected_state is None:
            if actual_state is not None:
                found.append(
                    Discrepancy(
                        code="unexpected_position",
                        message=(
                            f"Stock {stock_id} holds {actual_state.quantity} share(s) but the "
                            "recorded fills should have left the portfolio flat in it."
                        ),
                        stock_id=stock_id,
                        expected="0",
                        actual=str(actual_state.quantity),
                    )
                )
            continue

        if actual_state is None:
            found.append(
                Discrepancy(
                    code="missing_position",
                    message=(
                        f"Stock {stock_id} should hold {expected_state.quantity} share(s) after "
                        "the recorded fills, but no position is stored."
                    ),
                    stock_id=stock_id,
                    expected=str(expected_state.quantity),
                    actual="0",
                )
            )
            continue

        if expected_state.quantity != actual_state.quantity:
            found.append(
                Discrepancy(
                    code="quantity_mismatch",
                    message=(
                        f"Stock {stock_id} holds {actual_state.quantity} share(s) but the "
                        f"recorded fills imply {expected_state.quantity}."
                    ),
                    stock_id=stock_id,
                    expected=str(expected_state.quantity),
                    actual=str(actual_state.quantity),
                )
            )
            continue

        if quantize_money(expected_state.average_entry_price) != quantize_money(
            actual_state.average_entry_price
        ):
            found.append(
                Discrepancy(
                    code="average_price_mismatch",
                    message=(
                        f"Stock {stock_id} has an average entry price of "
                        f"{actual_state.average_entry_price} but the recorded fills imply "
                        f"{expected_state.average_entry_price}."
                    ),
                    stock_id=stock_id,
                    expected=str(expected_state.average_entry_price),
                    actual=str(actual_state.average_entry_price),
                )
            )
    return tuple(found)


def _check_quantities(
    expected: Mapping[int, int], actual: Mapping[int, int]
) -> Tuple[Discrepancy, ...]:
    """Compare share counts, naming a missing or an unexpected holding explicitly.

    "Should hold nothing but holds 40" and "should hold 40 but holds none" are different
    failures, and reporting both as a bare quantity mismatch would leave a reader to work out
    which one happened.
    """
    found = []
    for stock_id in sorted(set(expected) | set(actual)):
        expected_quantity = int(expected.get(stock_id, 0))
        actual_quantity = int(actual.get(stock_id, 0))
        if expected_quantity == actual_quantity:
            continue
        if expected_quantity > 0 and actual_quantity == 0:
            code = "missing_position"
            message = f"Stock {stock_id} should hold {expected_quantity} share(s) but holds none."
        elif expected_quantity == 0 and actual_quantity > 0:
            code = "unexpected_position"
            message = (
                f"Stock {stock_id} should hold nothing but holds {actual_quantity} share(s)."
            )
        else:
            code = "quantity_mismatch"
            message = (
                f"Stock {stock_id} holds {actual_quantity} share(s) but should hold "
                f"{expected_quantity}."
            )
        found.append(
            Discrepancy(
                code=code,
                message=message,
                stock_id=stock_id,
                expected=str(expected_quantity),
                actual=str(actual_quantity),
            )
        )
    return tuple(found)


def _check_cash(expected: Decimal, actual: Decimal) -> Tuple[Discrepancy, ...]:
    """Record a cash mismatch, naming the direction and size of the gap."""
    difference = quantize_money(Decimal(actual) - Decimal(expected))
    if difference == ZERO:
        return ()
    direction = "less" if difference < ZERO else "more"
    return (
        Discrepancy(
            code="cash_mismatch",
            message=(
                f"Portfolio cash is {actual} but the recorded fills imply {expected} — "
                f"{abs(difference)} {direction} than they account for."
            ),
            expected=str(expected),
            actual=str(actual),
        ),
    )


def _check_filled_quantities(
    plan: OrderPlan, filled_quantities: Mapping[int, int]
) -> Tuple[Discrepancy, ...]:
    """Each planned order must have filled exactly what it asked for."""
    found = []
    for item in plan.order_items:
        filled = int(filled_quantities.get(item.stock_id, item.quantity))
        if filled == item.quantity:
            continue
        found.append(
            Discrepancy(
                code="incomplete_execution",
                message=(
                    f"Stock {item.stock_id}: {filled} of {item.quantity} planned share(s) were "
                    "filled."
                ),
                stock_id=item.stock_id,
                expected=str(item.quantity),
                actual=str(filled),
            )
        )
    return tuple(found)


def _check_orphan_orders(order_statuses: Mapping[int, str]) -> Tuple[Discrepancy, ...]:
    """Any order still working after the run is an orphan."""
    return tuple(
        Discrepancy(
            code="orphan_order",
            message=(
                f"Order {order_id} is still {status} with no terminal state. The portfolio it "
                "was placed against has moved on without it."
            ),
            expected=OrderStatus.FILLED.value,
            actual=status,
        )
        for order_id, status in sorted(order_statuses.items())
        if status in WORKING_ORDER_STATUSES
    )


def _check_duplicate_fills(fills: Sequence[ExecutionFill]) -> Tuple[Discrepancy, ...]:
    """The same fill recorded twice is a double booking.

    Grouped by execution id, so two identical quantities at identical prices on different orders
    are not mistaken for a duplicate: what makes a fill a duplicate is its identity, not its
    shape.
    """
    seen: Dict[str, int] = {}
    found = []
    for fill in fills:
        seen[fill.execution_id] = seen.get(fill.execution_id, 0) + 1
        if seen[fill.execution_id] == 2:
            found.append(
                Discrepancy(
                    code="duplicate_execution",
                    message=(
                        f"Fill {fill.execution_id} is recorded more than once for stock "
                        f"{fill.stock_id}. The same fill has been booked twice."
                    ),
                    stock_id=fill.stock_id,
                    expected="1",
                    actual="2",
                )
            )
    return tuple(found)


def _check_valuation(
    expected: Mapping[int, PositionState],
    expected_cash: Decimal,
    actual_equity: Decimal,
) -> Tuple[Discrepancy, ...]:
    """Compare equity against cash plus the position values implied by the fills.

    A **warning**, not a discrepancy: equity is also moved by marks applied after the fills were
    booked, so a portfolio whose positions were re-marked legitimately disagrees with a
    reconciliation computed from fill prices. It still deserves to be reported, because a large
    unexplained gap is exactly the kind of thing that is otherwise invisible.
    """
    implied = quantize_money(
        expected_cash
        + sum(
            (
                position.market_value()
                for position in expected.values()
                if position.current_price is not None
            ),
            ZERO,
        )
    )
    if quantize_money(actual_equity) == implied:
        return ()
    return (
        Discrepancy(
            code="valuation_mismatch",
            message=(
                f"Portfolio equity is {actual_equity} but the reconciled cash and positions "
                f"imply {implied}. The positions may have been re-marked after the fills were "
                "booked, or a fill may be missing from the books."
            ),
            severity=ValidationSeverity.WARNING,
            expected=str(implied),
            actual=str(actual_equity),
        ),
    )