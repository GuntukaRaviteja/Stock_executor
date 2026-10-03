"""Phase 11 rebalancing — which trades would bring a portfolio to its target weights.

Pure, deterministic and independent of FastAPI and the database.

The problem it solves
---------------------
Prices move, deposits arrive, and a position that was sized at 8% of equity can drift to 11%
without anything having been decided. Rebalancing turns that drift back into an explicit,
explainable trade — or deliberately into **no** trade, which is the harder half:

* drift **inside** ``rebalance_threshold_pct`` produces a ``HOLD``, so rounding noise never
  generates a transaction. A target of 10% against a current weight of 9.7% with a 0.5% threshold
  is a hold; 8.9% is a buy;
* a drift **at or beyond** the threshold produces a trade, with the required share count derived
  from the target *value*, floored so a rebalancing trade can never overshoot its own target;
* a held stock with **no target** is left alone unless ``drop_unlisted`` is set, which treats the
  missing target as zero and proposes closing it.

Sizing is always against the target value rather than the current value: rebalancing to a weight
means holding that weight, so the quantity is derived from what the position *should* be worth.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Mapping, Optional, Sequence

from app.schemas.portfolio import DecisionAction, OrderSide, PositionSizingMethod

from app.services.backtest.models import quantize_percent
from app.services.portfolio.allocation import (
    allocate_equal_weights,
    budget_for_weight,
    target_quantity_for_weight,
    weight_difference_pct,
)
from app.services.portfolio.calculator import (
    allocation_pct,
    calculate_fee,
    gross_value,
    quantize_price,
)
from app.services.portfolio.models import (
    ZERO,
    AllocationConfig,
    PortfolioValuation,
    PositionValuation,
    RebalanceItem,
    TradingCostConfig,
)

#: Stable reason codes attached to every rebalancing verdict.
REASON_BELOW_THRESHOLD = "below_rebalance_threshold"
REASON_AT_TARGET = "at_target_weight"
REASON_UNDERWEIGHT = "underweight"
REASON_OVERWEIGHT = "overweight"
REASON_NEW_POSITION = "new_position"
REASON_POSITION_REMOVAL = "position_removal"
REASON_NO_PRICE = "no_price"
REASON_UNLISTED_HOLDING = "unlisted_holding_kept"
REASON_ZERO_QUANTITY = "target_below_one_share"


def plan_rebalance(
    *,
    equity: Decimal,
    positions: Sequence[PositionValuation],
    target_weights: Mapping[int, Decimal],
    config: AllocationConfig,
    costs: TradingCostConfig,
    prices: Optional[Mapping[int, Decimal]] = None,
    drop_unlisted: bool = False,
    valuation: Optional[PortfolioValuation] = None,
    symbols: Optional[Mapping[int, str]] = None,
    sectors: Optional[Mapping[int, Optional[str]]] = None,
) -> tuple:
    """Every stock's rebalancing verdict, ordered by stock id ascending.

    Returns an item for **every** target and every held position, including the holds. A
    rebalance that returns five items, three of them "no trade needed", is a complete answer;
    a rebalance that returns only the trades leaves the caller unable to tell the difference
    between "checked and correct" and "not looked at".

    Determinism: the result is sorted by ``stock_id``, which is unique, so the same targets over
    the same positions always produce the same sequence regardless of the order the positions
    were loaded in.
    """
    prices = prices or {}
    symbols = symbols or {}
    sectors = sectors or {}
    held = {position.stock_id: position for position in positions}

    considered = sorted(set(target_weights) | (set(held) if drop_unlisted else set(target_weights)))
    items = []
    for stock_id in considered:
        items.append(
            _rebalance_item(
                stock_id=stock_id,
                equity=equity,
                position=held.get(stock_id),
                target_weight_pct=target_weights.get(stock_id, quantize_percent(ZERO)),
                config=config,
                costs=costs,
                price=_price_for(stock_id, held.get(stock_id), prices),
                drop_unlisted=drop_unlisted,
                symbol=symbols.get(stock_id),
                sector=sectors.get(stock_id),
            )
        )
    return tuple(items)


def _price_for(
    stock_id: int,
    position: Optional[PositionValuation],
    prices: Mapping[int, Decimal],
) -> Optional[Decimal]:
    """The price to measure drift at: an explicit mark, then the position's own mark.

    A target with no position and no supplied price has no mark, and its item reports a target
    with no tradeable quantity rather than inventing one.
    """
    if stock_id in prices:
        return quantize_price(Decimal(prices[stock_id]))
    if position is not None and position.current_price is not None:
        return quantize_price(Decimal(position.current_price))
    return None


def _rebalance_item(
    *,
    stock_id: int,
    equity: Decimal,
    position: Optional[PositionValuation],
    target_weight_pct: Decimal,
    config: AllocationConfig,
    costs: TradingCostConfig,
    price: Optional[Decimal],
    drop_unlisted: bool,
    symbol: Optional[str],
    sector: Optional[str],
) -> RebalanceItem:
    """The verdict for one stock."""
    reasons = []
    explanation = []
    current_quantity = position.quantity if position else 0
    # A percentage, rounded as a percentage. Quantising it as money would silently drop it to
    # two decimals — so a 12.3456% target would be sized off 12.35% — and would print the same
    # number with a different precision in the explanation than the field carries.
    target_weight_pct = quantize_percent(Decimal(target_weight_pct))

    if position is None and target_weight_pct <= ZERO:
        # A target of zero for a stock that is not held: nothing to do and nothing to say.
        return RebalanceItem(
            stock_id=stock_id,
            action=DecisionAction.HOLD,
            current_quantity=0,
            target_quantity=0,
            target_weight_pct=target_weight_pct,
            reasons=(REASON_AT_TARGET,),
            explanation=(f"Stock {stock_id} is not held and has no target; no action needed.",),
            symbol=symbol,
            sector=sector,
        )

    current_weight = _current_weight(position, equity)
    difference = weight_difference_pct(current_weight, target_weight_pct)

    if price is None:
        reasons.append(REASON_NO_PRICE)
        explanation.append(
            f"No market price is available for stock {stock_id}, so its target of "
            f"{target_weight_pct}% cannot be turned into a quantity. No trade is proposed and "
            "the position is left exactly as it is."
        )
        return RebalanceItem(
            stock_id=stock_id,
            action=DecisionAction.HOLD,
            current_quantity=current_quantity,
            target_quantity=current_quantity,
            target_weight_pct=target_weight_pct,
            current_weight_pct=current_weight,
            weight_difference_pct=difference,
            reasons=tuple(reasons),
            explanation=tuple(explanation),
            symbol=symbol,
            sector=sector,
        )

    target_budget = budget_for_weight(equity, target_weight_pct)
    target_quantity = target_quantity_for_weight(equity, target_weight_pct, price, costs)
    required = target_quantity - current_quantity

    if difference is not None and abs(difference) < config.rebalance_threshold_pct:
        if abs(difference) == ZERO:
            reasons.append(REASON_AT_TARGET)
            explanation.append(
                f"Stock {stock_id} is already at its {target_weight_pct}% target "
                f"({current_weight}% of equity); no trade needed."
            )
        else:
            reasons.append(REASON_BELOW_THRESHOLD)
            explanation.append(
                f"Stock {stock_id} is {current_weight}% of equity against a "
                f"{target_weight_pct}% target: a drift of {difference}%, inside the "
                f"{config.rebalance_threshold_pct}% rebalancing threshold, so no trade is "
                "proposed."
            )
        return RebalanceItem(
            stock_id=stock_id,
            action=DecisionAction.HOLD,
            current_quantity=current_quantity,
            target_quantity=target_quantity,
            target_weight_pct=target_weight_pct,
            current_weight_pct=current_weight,
            weight_difference_pct=difference,
            price=price,
            below_threshold=True,
            reasons=tuple(reasons),
            explanation=tuple(explanation),
            symbol=symbol,
            sector=sector,
        )

    if required == 0 and difference is not None and abs(difference) >= config.rebalance_threshold_pct:
        # The drift is past the threshold but the target value is not a whole share away. Flooring
        # is what makes this reachable, and proposing a zero-share trade would be noise.
        reasons.append(REASON_ZERO_QUANTITY)
        explanation.append(
            f"Stock {stock_id} is {current_weight}% of equity against a "
            f"{target_weight_pct}% target, but the {target_budget} target value is not a whole "
            f"share ({target_quantity} share(s) at {price}) away from the {current_quantity} "
            "held. No trade is proposed."
        )
        return RebalanceItem(
            stock_id=stock_id,
            action=DecisionAction.HOLD,
            current_quantity=current_quantity,
            target_quantity=target_quantity,
            target_weight_pct=target_weight_pct,
            current_weight_pct=current_weight,
            weight_difference_pct=difference,
            price=price,
            below_threshold=True,
            reasons=tuple(reasons),
            explanation=tuple(explanation),
            symbol=symbol,
            sector=sector,
        )

    action = DecisionAction.REBALANCE
    if position is None:
        reasons.append(REASON_NEW_POSITION)
        explanation.append(
            f"Stock {stock_id} is not held and targets {target_weight_pct}% of "
            f"{equity} equity ({target_budget}) = {target_quantity} share(s) at {price}."
        )
    elif required < 0:
        reasons.append(REASON_OVERWEIGHT)
        explanation.append(
            f"Stock {stock_id} is {current_weight}% of equity against a "
            f"{target_weight_pct}% target: a drift of {difference}%, at or beyond the "
            f"{config.rebalance_threshold_pct}% threshold. Selling "
            f"{abs(required)} share(s) to {target_quantity} brings it back to target."
        )
    else:
        reasons.append(REASON_UNDERWEIGHT)
        explanation.append(
            f"Stock {stock_id} is {current_weight}% of equity against a "
            f"{target_weight_pct}% target: a drift of {difference}%, at or beyond the "
            f"{config.rebalance_threshold_pct}% threshold. Buying {required} share(s) to "
            f"{target_quantity} brings it back to target."
        )

    if position is not None and target_weight_pct <= ZERO and drop_unlisted:
        reasons.append(REASON_POSITION_REMOVAL)
        explanation.append(
            f"Stock {stock_id} has no target weight and drop_unlisted is set, so it targets "
            f"0%: its {current_quantity} share(s) are proposed for closure."
        )

    trade_value = gross_value(abs(required), price)
    return RebalanceItem(
        stock_id=stock_id,
        action=action,
        current_quantity=current_quantity,
        target_quantity=target_quantity,
        target_weight_pct=target_weight_pct,
        current_weight_pct=current_weight,
        weight_difference_pct=difference,
        price=price,
        required_quantity=required,
        estimated_trade_value=trade_value,
        estimated_fees=calculate_fee(trade_value, costs),
        reasons=tuple(reasons),
        explanation=tuple(explanation),
        symbol=symbol,
        sector=sector,
    )


def _current_weight(position: Optional[PositionValuation], equity: Decimal) -> Optional[Decimal]:
    """Current weight of a held position, as a percentage of equity."""
    if position is None:
        return None
    return allocation_pct(position.market_value, equity)


def derive_target_weights(
    candidate_ids: Sequence[int],
    config: AllocationConfig,
) -> Dict[int, Decimal]:
    """Equal-weight targets for a candidate list, capped by the configured position cap.

    Delegates to the same allocation helper the construction engine uses, so "the target a
    rebalance moves towards" and "the target the construction engine would open" can never
    disagree.

    Only ``EQUAL_WEIGHT`` can derive a target from a bare candidate list: the other formulas need
    a per-candidate input (a price, a stop) that a list of ids does not carry. Rather than
    invent one, a non-equal-weight rebalance must supply explicit ``target_weights``.
    """
    if config.sizing_method is not PositionSizingMethod.EQUAL_WEIGHT:
        raise ValueError(
            f"sizing_method {config.sizing_method.value} cannot derive rebalance targets from a "
            "candidate list; supply 'target_weights' explicitly"
        )
    return allocate_equal_weights(
        candidate_ids,
        max_position_weight_pct=config.max_position_weight_pct,
        target_count=config.target_position_count,
    )


def side_for(item: RebalanceItem) -> Optional[OrderSide]:
    """``BUY`` for a positive required quantity, ``SELL`` for a negative one, else ``None``."""
    if item.required_quantity > 0:
        return OrderSide.BUY
    if item.required_quantity < 0:
        return OrderSide.SELL
    return None


def target_weight_map(items: Sequence[RebalanceItem]) -> Dict[str, Decimal]:
    """``{stock_id: target weight}`` for reporting, keyed as strings for JSON stability."""
    return {str(item.stock_id): item.target_weight_pct for item in items}