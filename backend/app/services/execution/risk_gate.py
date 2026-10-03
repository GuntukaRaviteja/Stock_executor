"""Phase 16 pre-trade risk gate — the existing risk engine, applied to a whole plan.

The gate answers one question with one of two answers: **APPROVED** or **REJECTED**. It exists
as a separate stage, before any adapter sees an order, because a refusal that never reaches a
venue is the cheapest kind of refusal: there is nothing to cancel, nothing partially filled and
nothing to reconcile away.

It computes nothing new
----------------------
Every rule is the existing :class:`~app.services.portfolio.risk.RiskManager`, configured with the
portfolio's own :class:`~app.services.portfolio.models.RiskLimits` — the same object Phases 9-12
use for a manual order and the same Phase 13-15 risk policy is built from. This module projects
the account state a plan would produce and hands it to that manager. Re-deriving any limit here
would create a second definition of "may this trade", and a portfolio whose pre-trade check
disagreed with its own configured policy would be worse than one with no gate at all.

Why the projection matters
--------------------------
A risk limit is a statement about the state *after* the trade, so the gate must supply the
post-fill figures: the position's new quantity and value, the cash left after the buy's fees, the
market value of the whole book, and the equity those two produce. For a plan, those are projected
across every order rather than one at a time — a buy that is individually within every limit can
still breach ``max_positions`` or ``max_exposure_pct`` once it is applied alongside its siblings,
and checking orders in isolation would approve exactly that plan.

Sells are gated differently, on purpose
---------------------------------------
A sell reduces or closes exposure, so it is checked only against the rules that apply to it:
minimum trade value, and the position rules. It is never blocked by the drawdown, daily-loss or
exposure rules — a portfolio that has fallen far enough to trip those guards must still be able
to de-risk itself. This is the existing manager's own behaviour, inherited unchanged.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional, Sequence

from app.core.logging import logger
from app.schemas.portfolio import OrderSide, OrderSource, RejectionCode
from app.services.execution.models import (
    OrderPlan,
    OrderPlanItem,
    RiskGateResult,
    ValidationFinding,
    ValidationSeverity,
)
from app.services.backtest.models import quantize_money
from app.services.portfolio.calculator import equity, market_value
from app.services.portfolio.models import (
    ZERO,
    OrderIntent,
    PortfolioAccount,
    RiskLimits,
)
from app.services.portfolio.risk import RiskManager


def evaluate_risk_gate(
    plan: OrderPlan,
    account: PortfolioAccount,
    *,
    limits: RiskLimits,
    peak_equity: Optional[Decimal] = None,
    day_open_equity: Optional[Decimal] = None,
    cash: Optional[Decimal] = None,
) -> RiskGateResult:
    """Decide whether the plan's orders may be submitted.

    Returns the projected account state for each order so the caller can persist exactly what was
    approved. ``APPROVED`` means every order in the plan passed; ``REJECTED`` means at least one
    did not, and carries every refusal found rather than only the first.

    ``cash`` overrides the account's cash when a caller is projecting a mid-plan state; it
    defaults to the account's own balance.
    """
    risk = RiskManager(limits)
    available = account.cash if cash is None else Decimal(cash)
    projected = _ProjectedAccount(available_cash=available, account=account)

    findings = []
    for item in plan.order_items:
        findings.extend(
            _check_item(
                item,
                account=account,
                projected=projected,
                risk=risk,
                peak_equity=peak_equity,
                day_open_equity=day_open_equity,
            )
        )
        projected.apply(item)

    approved = not any(
        finding.severity is ValidationSeverity.ERROR for finding in findings
    )
    if approved:
        logger.info(
            "Risk gate APPROVED plan %s for portfolio %s (%s order(s))",
            plan.plan_id,
            plan.portfolio_id,
            len(plan.order_items),
        )
    else:
        logger.warning(
            "Risk gate REJECTED plan %s for portfolio %s: %s",
            plan.plan_id,
            plan.portfolio_id,
            "; ".join(finding.code for finding in findings),
        )
    return RiskGateResult(approved=approved, findings=tuple(findings))


class _ProjectedAccount:
    """The account as the plan's orders, applied in order, would leave it.

    Mutating a working copy rather than the live account is what lets the gate ask "what would
    this plan do" without touching the portfolio: a refused plan leaves this object discarded and
    the stored state untouched.
    """

    def __init__(self, available_cash: Decimal, account: PortfolioAccount):
        self.cash = quantize_money(available_cash)
        self.quantities: Dict[int, int] = {
            position.stock_id: position.quantity for position in account.positions
        }
        self.values: Dict[int, Decimal] = {
            position.stock_id: (
                quantize_money(position.market_value())
                if position.current_price is not None
                else ZERO
            )
            for position in account.positions
        }
        self.total_market_value = quantize_money(account.total_market_value)

    def apply(self, item: OrderPlanItem) -> None:
        """Apply one item's estimated effect to the projection."""
        previous_value = self.values.get(item.stock_id, ZERO)
        resulting_quantity = max(self.quantities.get(item.stock_id, 0) + item.quantity_delta, 0)
        resulting_value = market_value(resulting_quantity, item.reference_price)
        self.quantities[item.stock_id] = resulting_quantity
        self.values[item.stock_id] = resulting_value
        self.total_market_value = quantize_money(
            self.total_market_value - previous_value + resulting_value
        )
        if item.side is OrderSide.BUY:
            self.cash = quantize_money(self.cash - item.estimated_value - item.estimated_fees)
        else:
            self.cash = quantize_money(self.cash + item.estimated_value - item.estimated_fees)


def _check_item(
    item: OrderPlanItem,
    *,
    account: PortfolioAccount,
    projected: _ProjectedAccount,
    risk: RiskManager,
    peak_equity: Optional[Decimal],
    day_open_equity: Optional[Decimal],
) -> Sequence[ValidationFinding]:
    """Run the existing risk rules against one order's projected outcome."""
    if item.side is OrderSide.SELL:
        return _check_sell(item, account=account, risk=risk)
    return _check_buy(
        item,
        account=account,
        projected=projected,
        risk=risk,
        peak_equity=peak_equity,
        day_open_equity=day_open_equity,
    )


def _check_buy(
    item: OrderPlanItem,
    *,
    account: PortfolioAccount,
    projected: _ProjectedAccount,
    risk: RiskManager,
    peak_equity: Optional[Decimal],
    day_open_equity: Optional[Decimal],
) -> Sequence[ValidationFinding]:
    """Every buy rule, evaluated against the state this order would produce.

    ``violations_for_buy`` is used rather than ``validate_buy`` so that a plan breaching several
    limits reports all of them at once, instead of making the caller fix one rule per attempt and
    discover the next only after re-running.
    """
    previous_quantity = max(projected.quantities.get(item.stock_id, 0), 0)
    previous_value = projected.values.get(item.stock_id, ZERO)
    resulting_quantity = max(previous_quantity + item.quantity_delta, 0)
    resulting_value = market_value(resulting_quantity, item.reference_price)

    total_cost = quantize_money(item.estimated_value + item.estimated_fees)
    cash_after = quantize_money(projected.cash - total_cost)
    resulting_total = quantize_money(
        projected.total_market_value - previous_value + resulting_value
    )
    projected_equity = equity(cash_after, resulting_total)

    violations = risk.violations_for_buy(
        resulting_quantity=resulting_quantity,
        resulting_position_value=resulting_value,
        projected_equity=projected_equity,
        peak_equity=peak_equity,
        resulting_total_market_value=resulting_total,
        cash_after=cash_after,
        trade_value=item.estimated_value,
        open_position_count=len(projected.quantities),
        opening_new_position=previous_quantity == 0,
        day_open_equity=day_open_equity,
    )
    return [_finding_from_rejection(violation, item) for violation in violations]


def _check_sell(
    item: OrderPlanItem, *, account: PortfolioAccount, risk: RiskManager
) -> Sequence[ValidationFinding]:
    """The sell rules, applied through the same manager.

    The manager takes an :class:`~app.services.portfolio.models.OrderIntent` for a sell, so one
    is built from the plan item. That is the manager's existing signature, not a new interface —
    the alternative would be copying its coverage check, which is the one rule a sell can fail
    on account state.
    """
    intent = OrderIntent(
        portfolio_id=account.portfolio_id,
        stock_id=item.stock_id,
        side=item.side,
        quantity=item.quantity,
        requested_price=item.reference_price,
        source=OrderSource.STRATEGY,
    )
    rejection = risk.validate_sell(account, intent, trade_value=item.estimated_value)
    return [_finding_from_rejection(rejection, item)] if rejection is not None else ()


def _finding_from_rejection(rejection, item: OrderPlanItem) -> ValidationFinding:
    """Convert an existing ``Rejection`` into a plan-level finding, keeping its code and text.

    The code is the shared :class:`~app.schemas.portfolio.RejectionCode` value, so a caller that
    branches on a Phase 9 refusal reason branches on the same value here.
    """
    code = rejection.code.value if isinstance(rejection.code, RejectionCode) else str(
        rejection.code
    )
    return ValidationFinding(
        code=code,
        message=rejection.reason,
        severity=ValidationSeverity.ERROR,
        stock_id=item.stock_id,
        details=dict(rejection.details or {}),
    )