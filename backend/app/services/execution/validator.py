"""Phase 16 order-plan validation — what must be true before anything is submitted.

Validation never repairs a plan. It reports every problem it finds, with a stable code and a
message that names the numbers behind it, and leaves the plan exactly as it was given. Silently
trimming an unaffordable buy or dropping an unsellable share would produce a portfolio that
nobody asked for and an order plan that disagrees with the target it came from.

What is checked here, and what is not
-------------------------------------
Structural and funding checks live in this module: quantities, prices, sides, order types, cash
for buys, holdings for sells, duplicate orders and stale target state.

*Risk limits are not checked here.* Maximum position size, exposure, drawdown, cash reserve and
the rest are the existing :class:`~app.services.portfolio.models.RiskLimits` rules, applied by
the pre-trade gate in :mod:`app.services.execution.risk_gate` through the same
:class:`~app.services.portfolio.risk.RiskManager` Phases 9-15 already use. A second copy of those
rules would be a second answer to "may this order trade", and the two would eventually disagree.

The check order is fixed, so a plan that breaks several rules always reports them in the same
sequence and a test can assert on the codes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Dict, Mapping, Optional, Sequence, Set

from app.core.logging import logger
from app.schemas.portfolio import OrderSide, OrderType, RejectionCode
from app.services.execution.models import (
    OrderPlan,
    PlanValidationResult,
    ValidationFinding,
    ValidationSeverity,
)
from app.services.backtest.models import quantize_money
from app.services.portfolio.models import ZERO


def validate_plan(
    plan: OrderPlan,
    *,
    cash: Decimal,
    holdings: Mapping[int, int],
    portfolio_status_active: bool,
    max_age_seconds: Optional[int] = None,
    known_idempotency_keys: Optional[Set[str]] = None,
    settled_idempotency_keys: Optional[Set[str]] = None,
    now: Optional[datetime] = None,
) -> PlanValidationResult:
    """Validate a whole plan against the account it is about to trade on.

    ``holdings`` maps ``stock_id`` to the shares held right now. ``cash`` is the spendable cash
    before any of the plan's orders executes.

    ``known_idempotency_keys`` are keys already recorded against the portfolio in any state;
    ``settled_idempotency_keys`` are those already ``FILLED`` or ``REJECTED``. A key in both is a
    genuine duplicate — a repeat submission of work already finished — and is refused with
    ``DUPLICATE_ORDER``. A key that is only *known* is reported as a warning: the request is
    consistent with a partially executed previous run, and re-running it must be possible after a
    failure.

    ``max_age_seconds`` is the staleness bound. It is ``None`` by default, because a plan is a
    value object with no clock of its own and the caller knows how old its target is; when it is
    given, ``now`` must be supplied with it.
    """
    result = PlanValidationResult(portfolio_id=plan.portfolio_id, plan_id=plan.plan_id)
    known = known_idempotency_keys or set()
    settled = settled_idempotency_keys or set()

    if not portfolio_status_active:
        return result.with_finding(
            RejectionCode.PORTFOLIO_INACTIVE.value,
            f"Portfolio {plan.portfolio_id} is not ACTIVE and cannot accept orders. Nothing "
            "was submitted.",
            details={"portfolio_id": plan.portfolio_id},
        )

    if max_age_seconds is not None and now is not None:
        result = _check_staleness(plan, now, max_age_seconds, result)

    if plan.idempotency_key is not None:
        result = _check_duplicate_plan(plan.idempotency_key, known, settled, result)

    for item in plan.order_items:
        result = _check_item(item, holdings, plan, result)

    result = _check_funding(plan, cash, result)

    if result.valid:
        logger.info(
            "Order plan %s validated for portfolio %s: %s order(s), no blocking finding",
            plan.plan_id,
            plan.portfolio_id,
            len(plan.order_items),
        )
    else:
        logger.warning(
            "Order plan %s for portfolio %s refused with %s",
            plan.plan_id,
            plan.portfolio_id,
            ", ".join(result.error_codes),
        )
    return result


def _check_staleness(
    plan: OrderPlan,
    now: datetime,
    max_age_seconds: int,
    result: PlanValidationResult,
) -> PlanValidationResult:
    """Refuse a target built from state that has since moved on.

    A plan priced against old marks is not a slightly wrong plan: the reference prices it carries
    are the inputs to every cash and risk check downstream, so executing it can spend cash the
    account no longer has and breach a limit it would not have breached at the current marks.
    """
    age_seconds = int((now - plan.created_at).total_seconds())
    if age_seconds <= max_age_seconds:
        return result
    return result.with_finding(
        RejectionCode.STALE_PORTFOLIO.value,
        f"Order plan {plan.plan_id} was created {age_seconds}s ago, beyond the "
        f"{max_age_seconds}s staleness limit. Its reference prices are out of date; rebuild "
        "the plan from current marks.",
        details={
            "plan_id": plan.plan_id,
            "age_seconds": str(age_seconds),
            "max_age_seconds": str(max_age_seconds),
        },
    )


def _check_duplicate_plan(
    idempotency_key: str,
    known: Set[str],
    settled: Set[str],
    result: PlanValidationResult,
) -> PlanValidationResult:
    """Refuse a plan whose key has already been executed, warn about a partially applied one."""
    if idempotency_key in settled:
        return result.with_finding(
            RejectionCode.DUPLICATE_ORDER.value,
            f"Execution request '{idempotency_key}' has already been processed. Re-running it "
            "would book the same trades twice; no order was submitted.",
            details={"idempotency_key": idempotency_key},
        )
    if idempotency_key in known:
        return result.with_finding(
            RejectionCode.DUPLICATE_ORDER.value,
            f"Execution request '{idempotency_key}' is already recorded against this "
            "portfolio, but not as a settled run. Re-submitting will continue from the "
            "recorded state rather than repeat a completed order.",
            severity=ValidationSeverity.WARNING,
            details={"idempotency_key": idempotency_key},
        )
    return result


def _check_item(
    item,
    holdings: Mapping[int, int],
    plan: OrderPlan,
    result: PlanValidationResult,
) -> PlanValidationResult:
    """Structural checks for one order, in a fixed order.

    Quantity, then side, then order type, then price, then coverage. Coverage is checked before
    funding so a sell that cannot be covered is reported as the position problem it is, rather
    than as an unrelated cash one.
    """
    if item.quantity <= 0:
        result = result.with_finding(
            RejectionCode.INVALID_QUANTITY.value,
            f"Stock {item.stock_id}: an order must request a positive whole number of shares, "
            f"got {item.quantity}.",
            stock_id=item.stock_id,
            details={"quantity": str(item.quantity), "plan_id": plan.plan_id},
        )

    if item.side not in (OrderSide.BUY, OrderSide.SELL):
        result = result.with_finding(
            RejectionCode.UNSUPPORTED_SIDE.value,
            f"Stock {item.stock_id}: '{item.side}' is not a direction this long-only engine "
            "can execute.",
            stock_id=item.stock_id,
            details={"side": str(item.side)},
        )

    if item.order_type is OrderType.LIMIT and item.limit_price is None:
        result = result.with_finding(
            RejectionCode.UNSUPPORTED_ORDER_TYPE.value,
            f"Stock {item.stock_id}: a LIMIT order requires an explicit limit price. The order "
            "is refused rather than submitted as a MARKET order, which would fill at a price "
            "nothing asked for.",
            stock_id=item.stock_id,
            details={"order_type": item.order_type.value},
        )
    elif item.order_type not in (OrderType.MARKET, OrderType.LIMIT):
        result = result.with_finding(
            RejectionCode.UNSUPPORTED_ORDER_TYPE.value,
            f"Stock {item.stock_id}: order type '{item.order_type}' is not supported.",
            stock_id=item.stock_id,
            details={"order_type": str(item.order_type)},
        )
    elif item.order_type is OrderType.LIMIT:
        limit = Decimal(item.limit_price or ZERO)
        if limit <= ZERO:
            result = result.with_finding(
                RejectionCode.INVALID_PRICE.value,
                f"Stock {item.stock_id}: a LIMIT order needs a limit price greater than zero, "
                f"got {limit}.",
                stock_id=item.stock_id,
                details={"limit_price": str(limit)},
            )

    if item.reference_price <= ZERO:
        result = result.with_finding(
            RejectionCode.INVALID_PRICE.value,
            f"Stock {item.stock_id}: an order needs a reference price greater than zero to be "
            f"executed against, got {item.reference_price}.",
            stock_id=item.stock_id,
            details={"reference_price": str(item.reference_price)},
        )

    if item.side is OrderSide.SELL:
        owned = int(holdings.get(item.stock_id, 0))
        if item.quantity > owned:
            result = result.with_finding(
                RejectionCode.INSUFFICIENT_POSITION.value,
                f"Stock {item.stock_id}: cannot sell {item.quantity} share(s) while holding "
                f"{owned}. This engine is long-only, so a sell larger than the holding would "
                "create a short position.",
                stock_id=item.stock_id,
                details={
                    "requested_quantity": str(item.quantity),
                    "owned_quantity": str(owned),
                },
            )

    return result


def _check_funding(
    plan: OrderPlan, cash: Decimal, result: PlanValidationResult
) -> PlanValidationResult:
    """Check that the plan's buys can be funded in full, sell proceeds included.

    The check is against the *whole* plan rather than order by order, because sells in the same
    plan release the cash that buys in the same plan spend. Testing each buy against the opening
    balance alone would refuse a perfectly fundable rebalance — the case this layer exists to
    handle — while testing them in isolation against a running balance would depend on the order
    the caller happened to iterate in.

    ``min_gross_sell_value`` is subtracted from the sale proceeds a plan may rely on, so a sell so
    small that its fees exceed its value cannot be used to justify a buy the portfolio could not
    otherwise afford.
    """
    required = plan.cash_required
    available = quantize_money(Decimal(cash) + plan.cash_released)
    if required > available:
        return result.with_finding(
            RejectionCode.INSUFFICIENT_CASH.value,
            f"The plan needs {required} of cash (buys plus their fees, less {plan.cash_released} "
            f"released by its sells) but only {cash} is available. No order was submitted.",
            details={
                "required_cash": str(required),
                "available_cash": str(cash),
                "cash_released": str(plan.cash_released),
            },
        )
    return result


def order_keys_for(plan: OrderPlan, prefix: Optional[str] = None) -> Dict[int, str]:
    """The idempotency key each of a plan's orders will carry, keyed by ``stock_id``.

    Derived as ``<prefix>-<stock_id>-<side>``, so the same request produces the same keys on
    every run and a repeat submission finds the orders it already created instead of placing new
    ones. Including the side keeps a buy and a sell of the same stock — which cannot coexist in
    one plan, but can across two plans — from colliding on one key.
    """
    base = prefix or plan.idempotency_key or plan.plan_id
    return {
        item.stock_id: f"{base}-{item.stock_id}-{item.side.value}"
        for item in plan.order_items
    }


def findings_by_code(findings: Sequence[ValidationFinding]) -> Dict[str, ValidationFinding]:
    """Index findings by code, for callers that branch on one reason."""
    return {finding.code: finding for finding in findings}