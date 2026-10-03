"""Deterministic risk controls for the Phase 10 paper-trading engine.

The engine enforces a fixed, explainable rule set. Every rule is a pure function of the
account state, the proposed order and the configured limits, so the same inputs always
produce the same rejection with a stable code and a clear reason. Rules never modify state:
rejecting an order preserves the portfolio exactly as it was.

Rules
-----
1. **Minimum trade value** — an order too small to be worth its own fees, on either side.
2. **Insufficient cash** — a buy can never commit more cash than the portfolio holds.
3. **Overselling** — a sell can never exceed the shares actually held, and a position can
   never become negative.
4. **Maximum position quantity** — shares held in one stock.
5. **Maximum position value** — market value of one position.
6. **Maximum position allocation** — one position's share of portfolio equity.
7. **Maximum exposure** — all positions' combined share of portfolio equity.
8. **Maximum trade allocation** — capital a single order may commit.
9. **Minimum cash reserve** — equity that must stay in cash after a buy.
10. **Maximum number of positions** — how many stocks may be held at once.
11. **Maximum drawdown** — blocks *new buys* once the portfolio has fallen too far from its
    peak equity.
12. **Maximum daily loss** — blocks *new buys* once the portfolio has lost too much on the
    current day.

Rule 1 applies to both sides: it describes whether an order is worth executing, not whether the
portfolio may grow. Rules 4-12 apply to buys only. A sell never increases exposure and is
therefore never blocked by them, so a losing portfolio can always de-risk itself. Structural
problems (rule 2, rule 3, invalid quantity, invalid price, an inactive portfolio) are checked by
the executor before the risk manager runs, so the risk manager only ever sees a well-formed
order.

"Today" is a UTC calendar day, and the day's opening equity is supplied by the caller rather
than looked up here: this module stays a pure function of the numbers it is handed, so the same
inputs always produce the same decision.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.schemas.portfolio import RejectionCode
from app.services.backtest.models import quantize_money
from app.services.portfolio.calculator import allocation_pct, daily_loss_pct, drawdown_pct
from app.services.portfolio.models import (
    ZERO,
    OrderIntent,
    PortfolioAccount,
    Rejection,
    RiskLimits,
)

_ONE_HUNDRED = Decimal("100")


class RiskManager:
    """Applies the portfolio's configured risk limits to a proposed order."""

    def __init__(self, limits: Optional[RiskLimits] = None):
        self.limits = limits or RiskLimits()

    def validate_buy(
        self,
        account: PortfolioAccount,
        *,
        resulting_quantity: int,
        resulting_position_value: Decimal,
        projected_equity: Decimal,
        peak_equity: Optional[Decimal],
        resulting_total_market_value: Optional[Decimal] = None,
        cash_after: Optional[Decimal] = None,
        trade_value: Optional[Decimal] = None,
        open_position_count: Optional[int] = None,
        opening_new_position: bool = False,
        day_open_equity: Optional[Decimal] = None,
    ) -> Optional[Rejection]:
        """Risk checks for a buy that has already passed the cash check.

        ``resulting_quantity`` / ``resulting_position_value`` describe the position *after*
        the fill, and ``projected_equity`` is the equity after the fees are charged, which
        is the basis a risk limit is meant to be measured against.

        The optional keyword arguments describe the *portfolio-wide* effect of the order and
        default to "unchanged", so a caller that only cares about the single-position rules
        keeps working unchanged:

        * ``resulting_total_market_value`` — market value of every position after the fill
          (defaults to "this order adds no exposure");
        * ``cash_after`` — cash remaining after the fill (defaults to the account's cash);
        * ``trade_value`` — gross value this order commits (defaults to nothing);
        * ``open_position_count`` / ``opening_new_position`` — how many positions exist and
          whether this order creates a new one (used by ``max_positions``);
        * ``day_open_equity`` — equity at the start of the current UTC day (used by
          ``max_daily_loss_pct``).

        Returns the **first** violated rule in the fixed evaluation order, so a request that
        breaks several rules always reports the same one.
        """
        for rejection in self.violations_for_buy(
            resulting_quantity=resulting_quantity,
            resulting_position_value=resulting_position_value,
            projected_equity=projected_equity,
            peak_equity=peak_equity,
            resulting_total_market_value=resulting_total_market_value,
            cash_after=cash_after,
            trade_value=trade_value,
            open_position_count=open_position_count,
            opening_new_position=opening_new_position,
            day_open_equity=day_open_equity,
        ):
            return rejection
        return None

    def violations_for_buy(
        self,
        *,
        resulting_quantity: int,
        resulting_position_value: Decimal,
        projected_equity: Decimal,
        peak_equity: Optional[Decimal],
        resulting_total_market_value: Optional[Decimal] = None,
        cash_after: Optional[Decimal] = None,
        trade_value: Optional[Decimal] = None,
        open_position_count: Optional[int] = None,
        opening_new_position: bool = False,
        day_open_equity: Optional[Decimal] = None,
    ) -> list:
        """Every rule a projected buy violates, in evaluation order.

        This is what powers the pre-trade risk check, which reports all breaches rather than
        only the first one. It evaluates exactly the same rules as :meth:`validate_buy`.
        """
        limits = self.limits
        violations: list = []

        for rejection in self.min_trade_value_violation(trade_value):
            violations.append(rejection)

        if limits.max_daily_loss_pct is not None and day_open_equity is not None:
            daily_loss = daily_loss_pct(day_open_equity, projected_equity)
            if daily_loss >= limits.max_daily_loss_pct:
                violations.append(
                    Rejection(
                        code=RejectionCode.MAX_DAILY_LOSS_EXCEEDED,
                        reason=(
                            f"Daily loss limit reached: equity is {daily_loss}% below the "
                            f"{day_open_equity} the portfolio started today with, at or beyond "
                            f"the configured {limits.max_daily_loss_pct}% limit, so new BUY "
                            "orders are blocked. SELL orders remain allowed."
                        ),
                        details={
                            "rule": "max_daily_loss_pct",
                            "daily_loss_pct": str(daily_loss),
                            "limit_pct": str(limits.max_daily_loss_pct),
                            "day_open_equity": str(day_open_equity),
                            "projected_equity": str(projected_equity),
                        },
                    )
                )

        if limits.max_drawdown_pct is not None and peak_equity is not None:
            current_drawdown = drawdown_pct(peak_equity, projected_equity)
            if current_drawdown >= limits.max_drawdown_pct:
                violations.append(
                    Rejection(
                        code=RejectionCode.MAX_DRAWDOWN_EXCEEDED,
                        reason=(
                            f"Drawdown protection: equity is {current_drawdown}% below the peak "
                            f"equity of {peak_equity}, at or beyond the configured "
                            f"{limits.max_drawdown_pct}% limit, so new BUY orders are blocked. "
                            "SELL orders remain allowed."
                        ),
                        details={
                            "rule": "max_drawdown_pct",
                            "current_drawdown_pct": str(current_drawdown),
                            "limit_pct": str(limits.max_drawdown_pct),
                            "peak_equity": str(peak_equity),
                        },
                    )
                )

        if limits.max_positions is not None and opening_new_position:
            existing = open_position_count or 0
            if existing >= limits.max_positions:
                violations.append(
                    Rejection(
                        code=RejectionCode.MAX_POSITIONS_EXCEEDED,
                        reason=(
                            f"Maximum positions reached: the portfolio already holds {existing} "
                            f"open position(s) and the limit is {limits.max_positions}. "
                            "Close a position before opening a new one."
                        ),
                        details={
                            "rule": "max_positions",
                            "open_position_count": str(existing),
                            "limit": str(limits.max_positions),
                        },
                    )
                )

        if (
            limits.max_position_quantity is not None
            and resulting_quantity > limits.max_position_quantity
        ):
            violations.append(
                Rejection(
                    code=RejectionCode.MAX_POSITION_QUANTITY_EXCEEDED,
                    reason=(
                        f"Maximum position quantity exceeded: this order would hold "
                        f"{resulting_quantity} share(s) but the limit is "
                        f"{limits.max_position_quantity}."
                    ),
                    details={
                        "rule": "max_position_quantity",
                        "resulting_quantity": str(resulting_quantity),
                        "limit": str(limits.max_position_quantity),
                    },
                )
            )

        if (
            limits.max_position_value is not None
            and resulting_position_value > limits.max_position_value
        ):
            violations.append(
                Rejection(
                    code=RejectionCode.MAX_POSITION_SIZE_EXCEEDED,
                    reason=(
                        f"Maximum position value exceeded: this order would give the "
                        f"position a market value of {resulting_position_value} but the "
                        f"limit is {limits.max_position_value}."
                    ),
                    details={
                        "rule": "max_position_value",
                        "resulting_position_value": str(resulting_position_value),
                        "limit": str(limits.max_position_value),
                    },
                )
            )

        if limits.max_position_allocation_pct is not None:
            projected_allocation = allocation_pct(resulting_position_value, projected_equity)
            if (
                projected_allocation is not None
                and projected_allocation > limits.max_position_allocation_pct
            ):
                violations.append(
                    Rejection(
                        code=RejectionCode.MAX_POSITION_ALLOCATION_EXCEEDED,
                        reason=(
                            f"Maximum position allocation exceeded: this order would make the "
                            f"position {projected_allocation}% of portfolio equity but the "
                            f"limit is {limits.max_position_allocation_pct}%."
                        ),
                        details={
                            "rule": "max_position_allocation_pct",
                            "projected_allocation_pct": str(projected_allocation),
                            "limit_pct": str(limits.max_position_allocation_pct),
                            "projected_equity": str(projected_equity),
                        },
                    )
                )

        if limits.max_exposure_pct is not None and resulting_total_market_value is not None:
            projected_exposure = allocation_pct(resulting_total_market_value, projected_equity)
            if projected_exposure is not None and projected_exposure > limits.max_exposure_pct:
                violations.append(
                    Rejection(
                        code=RejectionCode.MAX_EXPOSURE_EXCEEDED,
                        reason=(
                            f"Maximum portfolio exposure exceeded: this order would leave "
                            f"{resulting_total_market_value} invested, "
                            f"{projected_exposure}% of portfolio equity, above the configured "
                            f"{limits.max_exposure_pct}% limit."
                        ),
                        details={
                            "rule": "max_exposure_pct",
                            "projected_exposure_pct": str(projected_exposure),
                            "limit_pct": str(limits.max_exposure_pct),
                            "resulting_market_value": str(resulting_total_market_value),
                            "projected_equity": str(projected_equity),
                        },
                    )
                )

        if limits.max_trade_allocation_pct is not None and trade_value is not None:
            trade_allocation = allocation_pct(trade_value, projected_equity)
            if trade_allocation is not None and trade_allocation > limits.max_trade_allocation_pct:
                violations.append(
                    Rejection(
                        code=RejectionCode.MAX_TRADE_ALLOCATION_EXCEEDED,
                        reason=(
                            f"Maximum trade allocation exceeded: this order commits "
                            f"{trade_value}, {trade_allocation}% of portfolio equity, above the "
                            f"configured {limits.max_trade_allocation_pct}% limit."
                        ),
                        details={
                            "rule": "max_trade_allocation_pct",
                            "trade_allocation_pct": str(trade_allocation),
                            "limit_pct": str(limits.max_trade_allocation_pct),
                            "trade_value": str(trade_value),
                            "projected_equity": str(projected_equity),
                        },
                    )
                )

        if limits.min_cash_reserve_pct is not None and cash_after is not None:
            reserve_required = quantize_money(
                projected_equity * limits.min_cash_reserve_pct / _ONE_HUNDRED
            )
            if cash_after < reserve_required:
                violations.append(
                    Rejection(
                        code=RejectionCode.MIN_CASH_RESERVE_BREACHED,
                        reason=(
                            f"Minimum cash reserve breached: this order would leave {cash_after} "
                            f"in cash but {reserve_required} is required "
                            f"({limits.min_cash_reserve_pct}% of equity)."
                        ),
                        details={
                            "rule": "min_cash_reserve_pct",
                            "cash_after": str(cash_after),
                            "required_reserve": str(reserve_required),
                            "limit_pct": str(limits.min_cash_reserve_pct),
                            "projected_equity": str(projected_equity),
                        },
                    )
                )

        return violations

    def min_trade_value_violation(
        self, trade_value: Optional[Decimal]
    ) -> list:
        """Rejection list for an order whose gross value is below the configured minimum.

        Returns a list rather than a single :class:`Rejection` so it can be spliced into either
        side's rule set unchanged. An unset ``min_trade_value``, or an unknown trade value,
        produces no rejection — the rule simply cannot say anything.
        """
        minimum = self.limits.min_trade_value
        if minimum is None or trade_value is None or Decimal(trade_value) >= Decimal(minimum):
            return []
        return [
            Rejection(
                code=RejectionCode.MIN_TRADE_VALUE_BELOW,
                reason=(
                    f"Order value too small: this order commits {trade_value}, below the "
                    f"configured minimum trade value of {minimum}. Small orders pay the same "
                    "per-order fees as large ones."
                ),
                details={
                    "rule": "min_trade_value",
                    "trade_value": str(trade_value),
                    "limit": str(minimum),
                },
            )
        ]

    def validate_sell(
        self,
        account: PortfolioAccount,
        intent: OrderIntent,
        *,
        trade_value: Optional[Decimal] = None,
    ) -> Optional[Rejection]:
        """Risk checks for a sell.

        Only the position rules and the minimum trade value apply: a sell never increases
        exposure, and it is never blocked by the drawdown or daily-loss rules so a losing
        portfolio can always be de-risked.

        ``trade_value`` is the gross value the sell would commit at its fill price. It is only
        consulted when ``min_trade_value`` is configured, which is why a caller that does not
        compute it keeps working.
        """
        for rejection in self.min_trade_value_violation(trade_value):
            return rejection

        position = account.position_for(intent.stock_id)
        owned = position.quantity if position else 0

        if owned <= 0:
            return Rejection(
                code=RejectionCode.INSUFFICIENT_POSITION,
                reason=(
                    f"No open position in stock {intent.stock_id}, so {intent.quantity} "
                    "share(s) cannot be sold. Paper trading is long-only."
                ),
                details={
                    "rule": "oversell_protection",
                    "stock_id": str(intent.stock_id),
                    "requested_quantity": str(intent.quantity),
                    "owned_quantity": str(owned),
                },
            )

        if intent.quantity > owned:
            return Rejection(
                code=RejectionCode.INSUFFICIENT_POSITION,
                reason=(
                    f"Cannot sell {intent.quantity} share(s): only {owned} held in stock "
                    f"{intent.stock_id}. A position can never go negative."
                ),
                details={
                    "rule": "oversell_protection",
                    "stock_id": str(intent.stock_id),
                    "requested_quantity": str(intent.quantity),
                    "owned_quantity": str(owned),
                },
            )

        return None


def insufficient_cash_rejection(required: Decimal, available: Decimal) -> Rejection:
    """Build the deterministic rejection used when a buy cannot be funded."""
    return Rejection(
        code=RejectionCode.INSUFFICIENT_CASH,
        reason=(
            f"Insufficient cash: this buy needs {required} (value plus fees) but the "
            f"portfolio has {available} available. No cash, position or P&L was changed."
        ),
        details={
            "rule": "insufficient_cash_protection",
            "required_cash": str(required),
            "available_cash": str(available),
        },
    )


def peak_equity(candidates) -> Decimal:
    """Highest equity among the supplied candidates, ignoring ``None``.

    Used to combine the initial capital, every recorded snapshot and the current equity into
    a single peak, so a drawdown measured before the first snapshot is never zero.
    """
    values = [Decimal(value) for value in candidates if value is not None]
    if not values:
        return ZERO
    return max(values)