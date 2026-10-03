"""Pure allocation math for Phase 11 — target weights, stops, targets and drift.

Every function here is deterministic, side-effect free, and independent of FastAPI and of the
database, so the allocation rules can be tested on their own. There are no strategy constants
in this module: a percentage always arrives as an argument, which is what lets a future
strategy supply a different configuration without touching the arithmetic.

Determinism
-----------
* No function reads the clock, the network, the database or any global mutable state.
* Quantisation is delegated to the shared Phase 8 helpers, so Phase 8 (backtesting), Phase 9/10
  (paper trading) and Phase 11 (construction) round identically.
* Money totals are rounded once, at the end.

Accounting conventions
----------------------
* Percentages are in percent (``20`` means 20%), matching every other percentage in the
  portfolio package.
* **Target weight** is a percentage of *equity*, not of available cash. Equity is what a weight
  must be measured against: a weight measured against cash would change every time a trade
  settled, even though the portfolio had not changed.
* Weights are allowed to leave real cash unallocated. After the equal-weight split the portfolio
  also holds its cash reserve, so the invested weights must be able to sum to less than 100.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Optional, Sequence

from app.services.backtest.models import quantize_money, quantize_percent
from app.services.portfolio.calculator import (
    quantize_price,
    size_quantity_by_percentage,
    size_quantity_by_risk,
)
from app.services.portfolio.models import ZERO

_ONE_HUNDRED = Decimal("100")


def side_value(side) -> str:
    """The uppercase *value* of an order side, from an enum member or a plain string.

    ``str(OrderSide.BUY)`` renders as ``'ORDERSIDE.BUY'`` rather than ``'BUY'``, so a naive
    ``str(side).upper() == "BUY"`` comparison rejects every real buy. Reading ``.value`` first
    makes the check work for an enum member, a plain string and a mixed caller alike.
    """
    return str(getattr(side, "value", side)).upper()


def equal_weight_target_pct(candidate_count: int) -> Decimal:
    """Target weight per candidate: ``100 / N`` percent.

    Returns ``0`` for zero candidates rather than raising: "there is nothing to allocate" is a
    normal outcome of a screen that matched nothing, and the caller reports it as an empty
    decision list rather than as an error.
    """
    if candidate_count <= 0:
        return quantize_percent(ZERO)
    return quantize_percent(_ONE_HUNDRED / Decimal(candidate_count))


def capped_target_pct(target_pct: Decimal, max_position_weight_pct: Decimal) -> Decimal:
    """The target weight after the single-position cap is applied.

    Equal weight over few candidates can exceed the cap — ``1/2`` is 50% — so the cap is always
    applied after the split, never before. Surplus capital is simply left unallocated.
    """
    return quantize_percent(min(Decimal(target_pct), Decimal(max_position_weight_pct)))


def weight_difference_pct(current_pct: Optional[Decimal], target_pct: Decimal) -> Optional[Decimal]:
    """``current - target``. Negative means underweight, positive means overweight."""
    if current_pct is None:
        return None
    return quantize_percent(Decimal(current_pct) - Decimal(target_pct))


def stop_price(entry_price: Decimal, stop_loss_pct: Optional[Decimal]) -> Optional[Decimal]:
    """Stop price for a long position: ``entry * (1 - stop_loss_pct / 100)``.

    ``None`` when no stop is configured, which is how "no stop" stays distinguishable from
    "a stop at the entry price" — the latter would define no per-share risk at all.
    """
    if stop_loss_pct is None:
        return None
    return quantize_price(Decimal(entry_price) * (_ONE_HUNDRED - Decimal(stop_loss_pct)) / _ONE_HUNDRED)


def take_profit_price(entry_price: Decimal, take_profit_pct: Optional[Decimal]) -> Optional[Decimal]:
    """Take-profit price for a long position: ``entry * (1 + take_profit_pct / 100)``."""
    if take_profit_pct is None:
        return None
    return quantize_price(Decimal(entry_price) * (_ONE_HUNDRED + Decimal(take_profit_pct)) / _ONE_HUNDRED)


def gain_pct(entry_price: Decimal, current_price: Decimal) -> Optional[Decimal]:
    """Signed return from entry to current price, in percent.

    ``None`` for a zero entry price, where the ratio is undefined; the engine reports that
    rather than inventing a return.
    """
    entry = Decimal(entry_price)
    if entry == ZERO:
        return None
    return quantize_percent((Decimal(current_price) - entry) / entry * _ONE_HUNDRED)


def loss_pct(entry_price: Decimal, current_price: Decimal) -> Optional[Decimal]:
    """Loss from entry to current price as a **positive** percentage. Zero above water."""
    change = gain_pct(entry_price, current_price)
    if change is None:
        return None
    return quantize_percent(max(-change, ZERO))


def stop_loss_breached(
    entry_price: Decimal, current_price: Decimal, stop_loss_pct: Optional[Decimal]
) -> bool:
    """True when the position has fallen to or through its stop.

    The comparison is **inclusive**: sitting exactly on the stop counts as breached, because the
    stop is the level at which the position is no longer being tolerated. A stop of ``0`` is
    treated as "no stop", since a stop at the entry price defines no loss to react to.
    """
    stop = stop_price(entry_price, stop_loss_pct)
    if stop is None:
        return False
    return Decimal(current_price) <= stop


def take_profit_triggered(
    entry_price: Decimal, current_price: Decimal, take_profit_pct: Optional[Decimal]
) -> bool:
    """True when the position has risen to or through its take-profit, inclusive."""
    target = take_profit_price(entry_price, take_profit_pct)
    if target is None:
        return False
    return Decimal(current_price) >= target


def position_loss_breached(
    entry_price: Decimal, current_price: Decimal, max_position_loss_pct: Optional[Decimal]
) -> bool:
    """True when the position's loss exceeds ``max_position_loss_pct``, exclusive.

    Exclusive on purpose: this limit is a *tolerance* ("no single position may lose more than
    20%"), so a loss of exactly the limit is still inside it. The stop-loss rule is the
    inclusive one, because a stop is a *level to react at*.
    """
    loss = loss_pct(entry_price, current_price)
    if loss is None or max_position_loss_pct is None:
        return False
    return loss > Decimal(max_position_loss_pct)


def target_quantity_for_weight(
    equity: Decimal,
    target_weight_pct: Decimal,
    price: Decimal,
    costs,
) -> int:
    """Whole shares whose value is ``target_weight_pct`` of ``equity``.

    Floored, never rounded up: buying one share more than the target is how a position silently
    breaches its own cap. Fees are included in the unit cost, so the returned quantity is always
    fundable and the cash balance can never go negative because of rounding.
    """
    if Decimal(target_weight_pct) <= ZERO or Decimal(equity) <= ZERO:
        return 0
    return size_quantity_by_percentage(equity, target_weight_pct, price, costs)


def affordable_quantity(
    capital_budget: Decimal, price: Decimal, costs
) -> int:
    """Whole shares whose full cost — fees included — fits inside ``capital_budget``.

    The counterpart to :func:`target_quantity_for_weight`: that one asks "how many shares does
    this target weight buy", this one asks "how many can this budget afford". The lower of the
    two is what the portfolio can actually hold, which is what keeps a reserve from turning into
    a refusal instead of a smaller position.
    """
    budget = Decimal(capital_budget)
    unit_price = quantize_price(Decimal(price))
    if budget <= ZERO or unit_price <= ZERO:
        return 0
    unit_cost = unit_price * (Decimal("1") + Decimal(costs.commission_rate))
    if unit_cost <= ZERO:
        return 0
    return max(int((budget / unit_cost).to_integral_value(rounding=ROUND_DOWN)), 0)


def risk_budget(equity: Decimal, risk_per_trade_pct: Decimal) -> Decimal:
    """``equity * risk_per_trade_pct / 100`` — the loss a single trade may put at risk."""
    if Decimal(equity) <= ZERO:
        return quantize_money(ZERO)
    return quantize_money(Decimal(equity) * Decimal(risk_per_trade_pct) / _ONE_HUNDRED)


def risk_based_quantity(
    equity: Decimal,
    risk_per_trade_pct: Decimal,
    entry_price: Decimal,
    stop_loss_pct: Optional[Decimal],
    costs,
    max_capital: Optional[Decimal] = None,
) -> int:
    """Shares whose loss at the configured stop equals the risk budget.

    ``quantity = floor((equity * risk_per_trade_pct / 100) / (entry - stop))``.

    The risk budget is deliberately **not** a cash budget: risking 1% of 100,000 through a
    5-per-share stop is 200 shares, which costs 20,000. ``max_capital`` (the portfolio's
    investable cash) is what turns the risk answer into a fundable order.

    Returns ``0`` when no stop is configured, because without one there is no per-share risk
    and no defensible size.
    """
    if stop_loss_pct is None:
        return 0
    entry = quantize_price(Decimal(entry_price))
    stop = stop_price(entry, stop_loss_pct)
    if stop is None or entry <= stop:
        return 0
    return size_quantity_by_risk(
        equity, risk_per_trade_pct, entry, stop, costs, max_capital=max_capital
    )


def risk_amount(quantity: int, entry_price: Decimal, stop_loss_pct: Optional[Decimal]) -> Decimal:
    """Loss ``quantity`` shares would book at the stop. Never negative."""
    stop = stop_price(entry_price, stop_loss_pct)
    if stop is None or quantity <= 0:
        return quantize_money(ZERO)
    per_share = Decimal(entry_price) - stop
    if per_share <= ZERO:
        return quantize_money(ZERO)
    return quantize_money(Decimal(quantity) * per_share)


def budget_for_weight(equity: Decimal, target_weight_pct: Decimal) -> Decimal:
    """Capital a target weight represents: ``equity * target_weight_pct / 100``."""
    if Decimal(equity) <= ZERO:
        return quantize_money(ZERO)
    return quantize_money(Decimal(equity) * Decimal(target_weight_pct) / _ONE_HUNDRED)


def normalise_weights(
    weights: dict, *, max_total_pct: Optional[Decimal] = None
) -> dict:
    """Validate and round a ``{stock_id: weight_pct}`` mapping.

    Rejects negative and non-finite weights, and rejects a set whose weights exceed
    ``max_total_pct`` when one is given. Over-allocating is refused rather than scaled down:
    silently rescaling a caller's targets would make the executed portfolio differ from the one
    that was requested.
    """
    if not weights:
        return {}
    normalised: dict = {}
    for stock_id, raw in weights.items():
        value = Decimal(str(raw))
        if value != value or value.is_infinite():
            raise ValueError(f"target weight for stock {stock_id} is not a finite number")
        if value < ZERO:
            raise ValueError(f"target weight for stock {stock_id} must not be negative")
        if value > _ONE_HUNDRED:
            raise ValueError(f"target weight for stock {stock_id} must be at most 100%")
        normalised[int(stock_id)] = quantize_percent(value)

    total = quantize_percent(sum(normalised.values(), ZERO))
    if max_total_pct is not None and total > Decimal(max_total_pct):
        raise ValueError(
            f"target weights total {total}% of equity, above the {max_total_pct}% "
            "allocation limit"
        )
    return normalised


def allocate_equal_weights(
    candidate_ids: Sequence[int],
    *,
    max_position_weight_pct: Decimal,
    target_count: Optional[int] = None,
) -> dict:
    """Equal-weight targets for ``candidate_ids``, capped and deterministically ordered.

    ``target_count`` narrows the allocation to the first ``target_count`` candidates — the
    caller is responsible for having ordered them first (see
    :meth:`app.services.portfolio.models.Candidate.sort_key`). Names beyond the count get no
    target, which is how "hold the best five" is expressed without dropping the others silently.

    Equal weight over one candidate is 100%, which the cap then reduces; equal weight over two is
    50%, which the 20% default cap also reduces. That is correct behaviour, not a special case:
    the cap is a portfolio-level limit and the split is only a starting point.
    """
    if not candidate_ids:
        return {}
    ids = list(dict.fromkeys(int(stock_id) for stock_id in candidate_ids))
    selected = ids if target_count is None else ids[: max(target_count, 0)]
    if not selected:
        return {}
    share = equal_weight_target_pct(len(selected))
    capped = capped_target_pct(share, max_position_weight_pct)
    return {stock_id: capped for stock_id in sorted(selected)}