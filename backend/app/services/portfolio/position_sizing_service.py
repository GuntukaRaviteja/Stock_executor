"""Reusable position sizing for the paper-trading engine.

Three deterministic formulas, all producing **whole shares floored** from a budget:

``FIXED_CAPITAL``
    ``quantity = floor(capital / price)``
    "Commit exactly this much money to this trade."

``PERCENTAGE``
    ``quantity = floor(equity * allocation_pct / price)``
    "Make this position worth this share of the portfolio." The basis is **equity**, not
    available cash, so the answer answers a portfolio question rather than a cash question;
    the cash-reserve rule is what later guarantees the order is fundable.

``RISK_BASED``
    ``risk_per_share = abs(entry_price - stop_loss_price)``
    ``allowed_risk   = equity * risk_pct``
    ``quantity       = floor(allowed_risk / risk_per_share)``
    "Lose at most this share of the portfolio if the stop is hit."

``EQUAL_WEIGHT`` (Phase 11)
    ``target_weight  = 100 / candidate_count``, capped at ``max_position_weight_pct``
    ``quantity       = floor(equity * target_weight / price)``
    "Split the portfolio equally across the eligible candidates."

``MAX_POSITION_CAP`` (Phase 11)
    ``target_weight  = min(allocation_pct, max_position_weight_pct)``
    ``quantity       = floor(equity * target_weight / price)``
    "Give this position the requested weight, never more than the cap."

Formulas
--------
Fees are included in the unit cost whenever a *capital budget* is being spent, so a sized
order can always be funded and cash can never go negative. The risk budget is deliberately
**not** treated as a cash budget: 1% of 100,000 risked over a 5-per-share stop is 200 shares,
which costs far more than 1,000. ``RISK_BASED`` therefore caps the result at the portfolio's
available cash instead of at the risk budget, and reports the cap in its reason.

Every result is a :class:`~app.services.portfolio.models.PositionSizePlan` carrying the
arithmetic, and a plan whose quantity is ``0`` is reported as such — never rounded up into a
trade the portfolio cannot make.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.core.exceptions import InvalidPositionSizeError
from app.core.logging import logger
from app.repositories.stock_repository import stock_repository
from app.schemas.portfolio import PositionSizingMethod, PositionSizingRequest
from app.services.backtest.models import quantize_money
from app.services.portfolio.allocation import capped_target_pct, equal_weight_target_pct
from app.services.portfolio.calculator import (
    gross_value,
    quantize_price,
    risk_per_share,
    size_quantity_by_percentage,
    size_quantity_by_risk,
    size_quantity_fixed_capital,
)
from app.services.portfolio.defaults import default_trading_cost_config
from app.services.portfolio.models import ZERO, PositionSizePlan


class PositionSizingService:
    """Derives the quantity of an order from capital, portfolio percentage or risk."""

    def __init__(self, stock_repo=stock_repository):
        self.stock_repo = stock_repo

    def size(
        self,
        db: Session,
        equity: Decimal,
        available_cash: Decimal,
        costs=None,
        request: Optional[PositionSizingRequest] = None,
        *,
        price: Optional[Decimal] = None,
    ) -> PositionSizePlan:
        """Size an order for a portfolio with ``equity`` and ``available_cash``.

        ``price`` overrides the sizing price; when it is omitted and the request does not
        carry one, the latest stored close is used (which requires ``stock_id``).

        Raises:
            InvalidPositionSizeError: the price cannot be resolved, or the inputs are
                inconsistent for the requested method (for example a stop at the entry price).
        """
        costs = costs or default_trading_cost_config()
        resolved_price = self._resolve_price(db, request, price)
        method = request.method if request is not None else PositionSizingMethod.PERCENTAGE

        if method is PositionSizingMethod.FIXED_CAPITAL:
            return self._fixed_capital(request, resolved_price, costs, equity)
        if method is PositionSizingMethod.PERCENTAGE:
            return self._percentage(request, resolved_price, costs, equity)
        if method is PositionSizingMethod.EQUAL_WEIGHT:
            return self._equal_weight(request, resolved_price, costs, equity)
        if method is PositionSizingMethod.MAX_POSITION_CAP:
            return self._max_position_cap(request, resolved_price, costs, equity)
        return self._risk_based(request, resolved_price, costs, equity, available_cash)

    # --- Formulas ---

    def _fixed_capital(self, request, price: Decimal, costs, equity: Decimal) -> PositionSizePlan:
        capital = Decimal(request.capital)
        quantity = size_quantity_fixed_capital(capital, price, costs)
        return PositionSizePlan(
            method=PositionSizingMethod.FIXED_CAPITAL,
            quantity=quantity,
            price=price,
            capital=gross_value(quantity, price),
            budget=capital,
            requested=capital,
            equity=equity,
            reason=(
                f"Fixed capital: floor({capital} / {price}) = {quantity} share(s) "
                f"({gross_value(quantity, price)} including fees)."
            ),
        )

    def _percentage(self, request, price: Decimal, costs, equity: Decimal) -> PositionSizePlan:
        allocation = Decimal(request.allocation_pct)
        budget = quantize_money(equity * allocation / Decimal("100"))
        quantity = size_quantity_by_percentage(equity, allocation, price, costs)
        return PositionSizePlan(
            method=PositionSizingMethod.PERCENTAGE,
            quantity=quantity,
            price=price,
            capital=gross_value(quantity, price),
            budget=budget,
            requested=allocation,
            equity=equity,
            reason=(
                f"Portfolio percentage: {allocation}% of {equity} equity is {budget}; "
                f"floor({budget} / {price}) = {quantity} share(s)."
            ),
        )

    def _equal_weight(
        self, request, price: Decimal, costs, equity: Decimal
    ) -> PositionSizePlan:
        """Equal-weight sizing: split the portfolio across the eligible candidates.

        ``candidate_count`` is how many names the split is over. It is required, because an equal
        weight without knowing ``N`` is not a number: guessing one would silently size a position
        as though the screen had matched a different number of stocks.
        """
        count = int(request.candidate_count)
        share = equal_weight_target_pct(count)
        cap = Decimal(str(request.max_position_weight_pct))
        weight = capped_target_pct(share, cap)
        budget = quantize_money(equity * weight / Decimal("100"))
        quantity = size_quantity_by_percentage(equity, weight, price, costs)
        return PositionSizePlan(
            method=PositionSizingMethod.EQUAL_WEIGHT,
            quantity=quantity,
            price=price,
            capital=gross_value(quantity, price),
            budget=budget,
            requested=share,
            equity=equity,
            reason=(
                f"Equal weight over {count} candidate(s): 100/{count} = {share}%, capped at "
                f"{cap}% -> {weight}% of {equity} equity is {budget}; "
                f"floor({budget} / {price}) = {quantity} share(s)."
            ),
        )

    def _max_position_cap(
        self, request, price: Decimal, costs, equity: Decimal
    ) -> PositionSizePlan:
        """Cap sizing: the requested allocation, never above the single-position cap.

        Equal weight and cap sizing differ only in where the weight comes from — a division by
        ``N`` versus the caller's own number — so both reduce to "what does this weight of equity
        buy at this price", which is why the arithmetic below is shared with ``PERCENTAGE``.
        """
        allocation = Decimal(request.allocation_pct)
        cap = Decimal(str(request.max_position_weight_pct))
        weight = capped_target_pct(allocation, cap)
        budget = quantize_money(equity * weight / Decimal("100"))
        quantity = size_quantity_by_percentage(equity, weight, price, costs)
        capped_note = (
            f" of the {cap}% cap, so {allocation}% becomes {weight}%"
            if weight < allocation
            else ""
        )
        return PositionSizePlan(
            method=PositionSizingMethod.MAX_POSITION_CAP,
            quantity=quantity,
            price=price,
            capital=gross_value(quantity, price),
            budget=budget,
            requested=allocation,
            equity=equity,
            reason=(
                f"Position cap: {allocation}% requested{capped_note} -> {weight}% of {equity} "
                f"equity is {budget}; floor({budget} / {price}) = {quantity} share(s)."
            ),
        )

    def _risk_based(
        self, request, price: Decimal, costs, equity: Decimal, available_cash: Decimal
    ) -> PositionSizePlan:
        entry = Decimal(request.entry_price)
        stop = Decimal(request.stop_loss_price)
        risk_pct = Decimal(request.risk_pct)
        try:
            unit_risk = risk_per_share(entry, stop)
        except ValueError as exc:
            raise InvalidPositionSizeError(str(exc), {"entry_price": str(entry), "stop_loss_price": str(stop)}) from exc

        if stop > entry:
            # The engine is long-only, so a level above the entry is not a stop: it is a
            # take-profit. Sizing against it would report "risk" that is really upside, and
            # produce a quantity larger than the risk budget can justify.
            raise InvalidPositionSizeError(
                f"stop_loss_price ({stop}) must be below entry_price ({entry}) for a long "
                "position: a level above the entry is a take-profit, not a stop-loss",
                {"entry_price": str(entry), "stop_loss_price": str(stop)},
            )

        allowed_risk = quantize_money(equity * risk_pct / Decimal("100"))
        capped_by_cash = size_quantity_by_risk(
            equity, risk_pct, entry, stop, costs, max_capital=available_cash
        )
        uncapped = size_quantity_by_risk(equity, risk_pct, entry, stop, costs)
        quantity = min(capped_by_cash, uncapped)

        reason = (
            f"Risk based: risk per share {unit_risk} (entry {entry}, stop {stop}), "
            f"allowed risk {risk_pct}% of {equity} = {allowed_risk}; "
            f"floor({allowed_risk} / {unit_risk}) = {uncapped} share(s)."
        )
        if quantity < uncapped:
            reason += (
                f" Capped at {quantity} share(s): the portfolio only has {available_cash} cash "
                "available at this price."
            )
        return PositionSizePlan(
            method=PositionSizingMethod.RISK_BASED,
            quantity=quantity,
            price=price,
            capital=gross_value(quantity, entry),
            budget=allowed_risk,
            requested=risk_pct,
            equity=equity,
            risk_per_share=unit_risk,
            allowed_risk=allowed_risk,
            reason=reason,
        )

    # --- Internals ---

    def _resolve_price(
        self, db: Session, request: Optional[PositionSizingRequest], price: Optional[Decimal]
    ) -> Decimal:
        """The price to size against, from the request, an override, or the market."""
        if price is not None:
            return quantize_price(price)
        if request is not None and request.price is not None:
            return quantize_price(request.price)

        stock_id = request.stock_id if request is not None else None
        if stock_id is None:
            raise InvalidPositionSizeError(
                "No price to size against: supply 'price' or 'stock_id' for the stored "
                "market price."
            )
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            raise InvalidPositionSizeError(f"Stock '{stock_id}' was not found.", {"stock_id": stock_id})
        market_price = self._latest_price(db, stock_id)
        if market_price is None or Decimal(market_price) <= ZERO:
            raise InvalidPositionSizeError(
                f"Stock '{stock_id}' has no stored close price to size against.",
                {"stock_id": stock_id},
            )
        logger.debug(
            "Position sizing for stock %s resolved the market price %s", stock_id, market_price
        )
        return quantize_price(Decimal(market_price))

    def _latest_price(self, db: Session, stock_id: int) -> Optional[Decimal]:
        from app.repositories.price_repository import price_repository

        latest = price_repository.get_latest(db, stock_id)
        if latest is None:
            return None
        return Decimal(str(latest.close_price))


position_sizing_service = PositionSizingService()