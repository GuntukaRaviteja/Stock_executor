"""Phase 13 position sizing — the capital-aware, risk-aware sizing engine.

This is a **pure** module: no clock, no database, no FastAPI, no randomness. Every figure is a
:class:`decimal.Decimal` rounded once through the shared Phase 8 helpers, so the same inputs
always produce the same quantity and the same explanation.

The primary model
-----------------
A long position is sized from the loss it may put at risk *and* from the capital a single
position may commit, and the answer is the smaller of the two::

    risk_budget          = equity * risk_per_trade
    risk_per_share       = entry_price - stop_price
    quantity_by_risk     = floor(risk_budget / risk_per_share)

    max_position_value   = equity * max_position_weight
    quantity_by_capital  = floor(max_position_value / entry_price)

    quantity             = floor(min(quantity_by_risk, quantity_by_capital))

``risk_per_trade`` and ``max_position_weight`` are **rates** in this notation. The engine stores
them as percentages — ``1`` meaning 1% — because that is the convention every other percentage in
the portfolio package uses, and divides by 100 exactly once, here.

A third bound is applied on top, and it is a real constraint rather than a refinement: the
portfolio's **cash**. ``quantity_by_cash`` is the largest whole-share quantity whose price *plus
fees* fits inside the available cash, so a sized order can always be funded and the cash balance
can never go negative. The final quantity is::

    quantity = floor(min(quantity_by_risk, quantity_by_capital, quantity_by_cash))

Why two bounds rather than one
------------------------------
The risk bound alone is not a capital policy. 1% of 100,000 risked through a 5-per-share stop is
20,000 shares — 100,000 of stock, on a 100,000 portfolio. The weight bound is what stops one
name from becoming the portfolio. Taking the minimum of the two is what makes the answer both
*safe* (the stop can be honoured) and *diversified* (no single name can dominate), and reporting
which of them bound is what makes the decision explainable.

Edge cases
----------
Every impossible input is **refused explicitly** rather than rounded into a number:

============================  =============================================================
``entry_price <= 0``          ``InvalidOrderError`` — a price of zero or less is not a price
``stop_price`` missing         ``InvalidStopPriceError`` — no stop, no per-share risk
``stop_price >= entry_price``  ``InvalidStopPriceError`` — a long cannot lose on a stop above entry
``stop_price <= 0``            ``InvalidStopPriceError`` — the position would be worth nothing
``equity <= 0``                quantity ``0`` — an empty portfolio opens nothing
``risk_budget <= 0``           quantity ``0`` — nothing is at risk, so nothing can be sized
fractional results             floored to whole shares, never rounded up
============================  =============================================================

A quantity of ``0`` is a legitimate answer and is always explained. It is never rounded *up* into
a trade: a position the policy cannot afford is not a smaller position, it is no position.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Optional

from app.core.exceptions import InvalidOrderError, InvalidStopPriceError
from app.services.backtest.models import quantize_money, quantize_percent
from app.services.portfolio.allocation import affordable_quantity
from app.services.portfolio.calculator import calculate_fee, gross_value, quantize_price
from app.services.portfolio.models import (
    CONSTRAINT_CASH,
    CONSTRAINT_POSITION_WEIGHT,
    CONSTRAINT_RISK,
    CONSTRAINT_UNAFFORDABLE,
    ZERO,
    PortfolioRiskPolicy,
    SizingBreakdown,
)

_ONE_HUNDRED = Decimal("100")

#: The sizing models this engine implements. They are the engine-side twin of
#: ``app.schemas.portfolio.PositionSizingMethod``; the enum member is compared by value so an
#: action produced elsewhere is recognised.
METHOD_RISK_AND_WEIGHT = "RISK_AND_WEIGHT"
METHOD_MAX_POSITION_CAP = "MAX_POSITION_CAP"


def risk_budget_of(equity: Decimal, risk_per_trade_pct: Decimal) -> Decimal:
    """``equity * risk_per_trade_pct / 100`` — the loss one trade may put at risk.

    Zero for non-positive equity, because there is nothing to risk. The caller distinguishes
    "no risk budget" from "a zero risk budget" through
    :attr:`~app.services.portfolio.models.SizingBreakdown.binding_constraint`.
    """
    if Decimal(equity) <= ZERO:
        return quantize_money(ZERO)
    return quantize_money(Decimal(equity) * Decimal(risk_per_trade_pct) / _ONE_HUNDRED)


def risk_per_share_long(entry_price: Decimal, stop_price: Decimal) -> Decimal:
    """``entry_price - stop_price`` for a **long** position.

    Signed rather than absolute, which is what makes the long-only check meaningful: a negative
    value means the stop sits above the entry, so the "risk" being measured is actually upside.

    Raises:
        InvalidStopPriceError: the stop is missing, zero or negative, at or above the entry, or
            equal to it (which would divide by zero).
    """
    entry = Decimal(entry_price)
    if stop_price is None:
        raise InvalidStopPriceError(
            entry,
            "None",
            message=(
                f"Invalid stop price None: a long position's stop must be given as a price "
                f"below the entry of {entry}."
            ),
        )
    stop = Decimal(stop_price)

    if entry <= ZERO:
        raise InvalidOrderError(
            f"Invalid entry price {entry}: an entry price must be greater than zero.",
            details={"entry_price": str(entry)},
        )
    if stop <= ZERO:
        raise InvalidStopPriceError(
            entry,
            stop,
            message=(
                f"Invalid stop price {stop}: a long position's stop must be greater than zero, "
                "because a stop at zero would write the whole position off before it is sold."
            ),
        )
    if stop >= entry:
        raise InvalidStopPriceError(entry, stop)
    return quantize_price(entry - stop)


def max_position_value_of(equity: Decimal, max_position_weight_pct: Decimal) -> Decimal:
    """``equity * max_position_weight_pct / 100`` — the largest position value allowed."""
    if Decimal(equity) <= ZERO:
        return quantize_money(ZERO)
    return quantize_money(Decimal(equity) * Decimal(max_position_weight_pct) / _ONE_HUNDRED)


def quantity_by_risk(risk_budget: Decimal, risk_per_share: Decimal) -> int:
    """``floor(risk_budget / risk_per_share)`` whole shares.

    Floored, never rounded up: a position one share larger than the risk budget allows would put
    more than the configured risk at risk, which is the exact thing the budget exists to prevent.
    """
    if Decimal(risk_budget) <= ZERO or Decimal(risk_per_share) <= ZERO:
        return 0
    quantity = (Decimal(risk_budget) / Decimal(risk_per_share)).to_integral_value(
        rounding=ROUND_DOWN
    )
    return max(int(quantity), 0)


def quantity_by_capital(max_position_value: Decimal, entry_price: Decimal) -> int:
    """``floor(max_position_value / entry_price)`` whole shares.

    Fees are deliberately **excluded** here. The capital bound answers "how large may this
    position be", and a position's size is its market value; whether the resulting order is
    additionally *affordable* is the cash bound's question, and folding the two together would
    make the two limits indistinguishable in the explanation.
    """
    if Decimal(max_position_value) <= ZERO or Decimal(entry_price) <= ZERO:
        return 0
    quantity = (Decimal(max_position_value) / Decimal(entry_price)).to_integral_value(
        rounding=ROUND_DOWN
    )
    return max(int(quantity), 0)


class PositionSizingEngine:
    """Derives a whole-share quantity from a portfolio's capital and risk policy.

    Pure and stateless, so the same policy and the same candidate always produce the same
    quantity and the same explanation — which is what lets the allocation engine, the risk
    engine and the backtester all size a trade the same way.
    """

    def __init__(self, policy: Optional[PortfolioRiskPolicy] = None):
        self.policy = policy or PortfolioRiskPolicy()

    # --- Public API ---

    def size(
        self,
        *,
        equity: Decimal,
        available_cash: Decimal,
        entry_price: Decimal,
        stop_price: Optional[Decimal] = None,
        policy: Optional[PortfolioRiskPolicy] = None,
        method: str = METHOD_RISK_AND_WEIGHT,
        existing_quantity: int = 0,
        remaining_weight_capacity: Optional[Decimal] = None,
    ) -> SizingBreakdown:
        """Size one long position.

        ``method`` selects the model:

        * ``RISK_AND_WEIGHT`` (default) — the primary model documented in this module's header.
          Requires ``stop_price``; a missing or unusable stop raises
          :class:`~app.core.exceptions.InvalidStopPriceError` rather than silently degrading to
          a capital-only size, because "sized without knowing the risk" is not a risk-based size.
        * ``MAX_POSITION_CAP`` — capital only: ``floor(max_position_value / entry_price)``, then
          capped by cash. Used when the caller has deliberately opted out of stop-based sizing.

        ``remaining_weight_capacity`` overrides the weight bound with a smaller one — how the
        allocation engine spends what is left of the exposure ceiling across several candidates
        without any single one of them being able to take more than its share of what remains.

        ``existing_quantity`` is the shares already held. It does not change the *marginal* size;
        it is reported so a caller can tell "opened" from "added to", and so a position that is
        already at its weight cap can be recognised.
        """
        policy = policy or self.policy
        costs = policy.as_trading_costs()
        entry = quantize_price(Decimal(entry_price))

        if entry <= ZERO:
            raise InvalidOrderError(
                f"Invalid entry price {entry}: an entry price must be greater than zero.",
                details={"entry_price": str(entry)},
            )

        equity = Decimal(equity)
        cash = Decimal(available_cash)
        budget = policy.risk_budget(equity)
        cap_value = policy.max_position_value(equity)
        if remaining_weight_capacity is not None:
            cap_value = min(cap_value, quantize_money(Decimal(remaining_weight_capacity)))

        if method == METHOD_MAX_POSITION_CAP:
            return self._capital_only(
                equity=equity,
                cash=cash,
                entry=entry,
                cap_value=cap_value,
                budget=budget,
                costs=costs,
                policy=policy,
            )
        return self._risk_and_weight(
            equity=equity,
            cash=cash,
            entry=entry,
            stop_price=stop_price,
            budget=budget,
            cap_value=cap_value,
            costs=costs,
            policy=policy,
            existing_quantity=existing_quantity,
        )

    # --- Models ---

    def _risk_and_weight(
        self,
        *,
        equity: Decimal,
        cash: Decimal,
        entry: Decimal,
        stop_price: Optional[Decimal],
        budget: Decimal,
        cap_value: Decimal,
        costs,
        policy: PortfolioRiskPolicy,
        existing_quantity: int = 0,
    ) -> SizingBreakdown:
        """The primary model: the minimum of the risk bound, the weight bound and the cash bound."""
        if stop_price is None:
            raise InvalidStopPriceError(
                entry,
                "None",
                message=(
                    "Risk-based sizing requires a stop loss: entry_price - stop_price is the "
                    "per-share risk, so without a stop no defensible position size exists. "
                    "Use MAX_POSITION_CAP sizing to size from capital alone."
                ),
            )
        unit_risk = risk_per_share_long(entry, Decimal(stop_price))

        by_risk = quantity_by_risk(budget, unit_risk)
        by_capital = quantity_by_capital(cap_value, entry)
        by_cash = affordable_quantity(cash, entry, costs)
        quantity, constraint = _smallest(by_risk, by_capital, by_cash)

        return self._build(
            quantity=quantity,
            constraint=constraint,
            equity=equity,
            cash=cash,
            entry=entry,
            stop=quantize_price(Decimal(stop_price)),
            unit_risk=unit_risk,
            budget=budget,
            by_risk=by_risk,
            cap_value=cap_value,
            by_capital=by_capital,
            by_cash=by_cash,
            costs=costs,
            policy=policy,
            existing_quantity=existing_quantity,
        )

    def _capital_only(
        self,
        *,
        equity: Decimal,
        cash: Decimal,
        entry: Decimal,
        cap_value: Decimal,
        budget: Decimal,
        costs,
        policy: PortfolioRiskPolicy,
    ) -> SizingBreakdown:
        """Capital-only sizing: the weight bound, capped by cash. No risk bound is claimed."""
        by_capital = quantity_by_capital(cap_value, entry)
        by_cash = affordable_quantity(cash, entry, costs)
        quantity, constraint = _smallest(None, by_capital, by_cash)
        return self._build(
            quantity=quantity,
            constraint=constraint,
            equity=equity,
            cash=cash,
            entry=entry,
            stop=None,
            unit_risk=None,
            budget=budget,
            by_risk=0,
            cap_value=cap_value,
            by_capital=by_capital,
            by_cash=by_cash,
            costs=costs,
            policy=policy,
        )

    # --- Assembly ---

    def _build(
        self,
        *,
        quantity: int,
        constraint: str,
        equity: Decimal,
        cash: Decimal,
        entry: Decimal,
        stop: Optional[Decimal],
        unit_risk: Optional[Decimal],
        budget: Decimal,
        by_risk: int,
        cap_value: Decimal,
        by_capital: int,
        by_cash: int,
        costs,
        policy: PortfolioRiskPolicy,
        existing_quantity: int = 0,
    ) -> SizingBreakdown:
        """Turn the three bounds into a quantity, its costs and its explanation."""
        capital = gross_value(quantity, entry)
        fees = calculate_fee(capital, costs)
        capital_required = quantize_money(capital + fees)

        if policy.min_trade_value is not None and capital_required < Decimal(
            policy.min_trade_value
        ):
            constraint = CONSTRAINT_UNAFFORDABLE
            refused_value = capital_required
            quantity = 0
            capital_required = quantize_money(ZERO)
            fees = quantize_money(ZERO)
            explanation_tail = (
                f" Refused: the size the bounds allow would commit {refused_value}, which is "
                f"below the configured minimum trade value of {policy.min_trade_value}, so no "
                "position is opened."
            )
        elif quantity <= 0:
            explanation_tail = (
                " No whole share fits inside every bound, so no position is opened: a size the "
                "policy cannot afford is no position, not a smaller one."
            )
        else:
            explanation_tail = ""

        risk_amount = (
            quantize_money(Decimal(quantity) * unit_risk)
            if unit_risk is not None and quantity > 0
            else quantize_money(ZERO)
        )

        return SizingBreakdown(
            quantity=quantity,
            entry_price=entry,
            stop_price=stop,
            equity=quantize_money(equity),
            risk_budget=budget,
            risk_per_share=unit_risk,
            quantity_by_risk=by_risk,
            max_position_value=cap_value,
            quantity_by_capital=by_capital,
            quantity_by_cash=by_cash,
            capital_required=capital_required,
            fees=fees,
            risk_amount=risk_amount,
            binding_constraint=constraint,
            explanation=self._explain(
                equity=equity,
                cash=cash,
                entry=entry,
                stop=stop,
                unit_risk=unit_risk,
                budget=budget,
                by_risk=by_risk,
                cap_value=cap_value,
                by_capital=by_capital,
                by_cash=by_cash,
                quantity=quantity,
                constraint=constraint,
                capital_required=capital_required,
                fees=fees,
                risk_amount=risk_amount,
                policy=policy,
                existing_quantity=existing_quantity,
            )
            + explanation_tail,
        )

    @staticmethod
    def _explain(
        *,
        equity: Decimal,
        cash: Decimal,
        entry: Decimal,
        stop: Optional[Decimal],
        unit_risk: Optional[Decimal],
        budget: Decimal,
        by_risk: int,
        cap_value: Decimal,
        by_capital: int,
        by_cash: int,
        quantity: int,
        constraint: str,
        capital_required: Decimal,
        fees: Decimal,
        risk_amount: Decimal,
        policy: PortfolioRiskPolicy,
        existing_quantity: int = 0,
    ) -> str:
        """The arithmetic, in the order it was computed, with the numbers in the sentence.

        Every number a decision rests on appears here. A size the caller cannot re-derive is a
        size the caller has to trust, which is what "explainable" rules out.
        """
        parts: list[str] = [
            f"Equity {equity}. "
            f"Risk budget = {equity} * {policy.max_risk_per_trade_pct}% = {budget}. "
            f"Max position value = {equity} * {policy.max_position_weight_pct}% = {cap_value}. "
            f"Cash available = {cash}."
        ]
        if unit_risk is not None and stop is not None:
            parts.append(
                f"Risk per share = {entry} - {stop} = {unit_risk}, so "
                f"floor({budget} / {unit_risk}) = {by_risk} share(s) by risk."
            )
        parts.append(f"floor({cap_value} / {entry}) = {by_capital} share(s) by position weight.")
        parts.append(f"{by_cash} share(s) are affordable with the available cash.")
        bounds = (
            f"risk {by_risk}, weight {by_capital}, cash {by_cash}"
            if unit_risk is not None
            else f"weight {by_capital}, cash {by_cash}"
        )
        parts.append(
            f"min({bounds}) = {quantity} share(s); "
            f"the binding constraint is '{constraint}'."
        )
        parts.append(
            f"Capital required = {capital_required} including {fees} of fees; "
            f"risk at the stop = {risk_amount}."
        )
        if existing_quantity:
            parts.append(f"The portfolio already holds {existing_quantity} share(s) of this stock.")
        return " ".join(parts)


def available_cash_of(policy: PortfolioRiskPolicy, equity: Decimal) -> Decimal:
    """Cash implied by ``equity`` when no explicit balance is supplied.

    Exposed for the explanation text only: the engine always sizes against the *actual* cash
    balance it is given, never against this derived figure. Having the fallback named keeps the
    prose honest about which number it is quoting.
    """
    return quantize_money(Decimal(equity) - policy.cash_reserve_required(equity))


def _smallest(by_risk: Optional[int], by_capital: int, by_cash: int) -> tuple:
    """The smallest of the bounds that apply, and which one it was.

    ``by_risk`` is ``None`` for capital-only sizing, where no risk bound was computed and
    therefore none can be named as binding. Ties resolve in the fixed order
    **risk → position weight → cash**, so the reported binding constraint is deterministic:
    the same three numbers always name the same constraint.
    """
    bounds = [(quantity, name) for quantity, name in ((by_risk, CONSTRAINT_RISK),
                                                      (by_capital, CONSTRAINT_POSITION_WEIGHT),
                                                      (by_cash, CONSTRAINT_CASH))
              if quantity is not None]
    quantity = min(value for value, _ in bounds)
    if quantity <= 0:
        return 0, CONSTRAINT_UNAFFORDABLE
    for value, name in bounds:
        if value == quantity:
            return quantity, name
    raise AssertionError("unreachable: the minimum is always one of the bounds")


def weight_pct_of(value: Decimal, equity: Decimal) -> Optional[Decimal]:
    """``value / equity * 100``, or ``None`` when equity is not positive."""
    if Decimal(equity) <= ZERO:
        return None
    return quantize_percent(Decimal(value) / Decimal(equity) * _ONE_HUNDRED)


#: Module-level engine for callers that do not need a per-policy instance.
position_sizing_engine = PositionSizingEngine()