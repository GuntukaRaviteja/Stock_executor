"""Phase 16 order planner — the deterministic target-versus-current diff engine.

The whole question this module answers is one subtraction::

    target_quantity - current_quantity = quantity_delta

    delta > 0  ->  BUY   (a new or increased position)
    delta < 0  ->  SELL  (a reduced position or a complete exit)
    delta = 0  ->  no action at all

Determinism
-----------
The result is a pure function of the inputs. Symbols are visited in ``stock_id`` order, money
is quantised once through the shared helpers, and nothing reads a clock or a random source. The
same current state and the same target therefore always produce an equal plan — which is what
makes a plan safe to compare against, to store, to re-submit, and to reconcile afterwards.

A symbol that has no target is treated as a target of **zero shares**, i.e. a complete exit,
when ``exit_unlisted`` is set (the default). That default is the conservative one for a long-only
book: a portfolio that stops naming a stock no longer wants to hold it. Passing
``exit_unlisted=False`` instead holds it and says so in the plan's ``held_unlisted`` — which is
the right behaviour for a screener-driven target that only lists its candidates.

Weights to quantities
---------------------
A target expressed as a weight is converted with
:func:`app.services.portfolio.allocation.target_quantity_for_weight`, which floors the share
count and includes fees in the unit cost. Reusing it is deliberate: a second weight-to-quantity
rule would be a second definition of what a target weight means, and the two would disagree by a
share somewhere.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Dict, Mapping, Optional, Sequence, Tuple

from app.core.logging import logger
from app.schemas.portfolio import OrderSide, OrderType
from app.services.execution.models import OrderPlan, OrderPlanItem, TargetHolding
from app.services.portfolio.allocation import target_quantity_for_weight
from app.services.portfolio.calculator import calculate_fee, gross_value, quantize_price
from app.services.portfolio.models import TradingCostConfig


def resolve_target_quantity(
    holding: TargetHolding,
    equity: Decimal,
    costs: TradingCostConfig,
) -> Optional[int]:
    """The share count a target holding asks for, or ``None`` when it cannot be derived.

    ``None`` means "this target cannot be turned into a number of shares" — a target weight with
    no usable price, or with an equity of zero. It is deliberately not ``0``: zero is a real
    target meaning "close this position", and returning it for an underivable weight would sell
    a holding because its price was missing.
    """
    if holding.target_quantity is not None:
        return holding.target_quantity
    if holding.price is None:
        return None
    return target_quantity_for_weight(
        equity, holding.target_weight_pct or Decimal("0"), holding.price, costs
    )


def build_order_plan(
    *,
    portfolio_id: int,
    plan_id: str,
    current_positions: Mapping[int, int],
    targets: Sequence[TargetHolding],
    prices: Mapping[int, Decimal],
    costs: TradingCostConfig,
    equity: Decimal,
    cash: Decimal,
    as_of: datetime,
    symbols: Optional[Mapping[int, str]] = None,
    sectors: Optional[Mapping[int, str]] = None,
    order_type: OrderType = OrderType.MARKET,
    exit_unlisted: bool = True,
    idempotency_key: Optional[str] = None,
) -> OrderPlan:
    """Diff the current holdings against the target and return every resulting item.

    ``current_positions`` maps ``stock_id`` to the shares held right now. ``targets`` is the
    earlier phases' output: :class:`~app.services.execution.models.TargetHolding` values, each
    carrying a share count or a weight. ``prices`` supplies the reference price used to value a
    plan item; a held stock with no price falls back to its own last mark, so an unpriced
    position is never valued at zero.

    Items with a zero delta are kept in ``items`` and absent from ``order_items``: the caller can
    see which holdings were checked and left alone.
    """
    costs.validate()
    symbols = symbols or {}
    sectors = sectors or {}

    target_map: Dict[int, TargetHolding] = {}
    for holding in targets:
        if holding.stock_id in target_map:
            raise ValueError(
                f"stock {holding.stock_id} appears more than once in the target portfolio; a "
                "target must state one position per symbol"
            )
        target_map[holding.stock_id] = holding

    considered = set(current_positions) | set(target_map)
    if not exit_unlisted:
        considered &= set(target_map)

    items = []
    for stock_id in sorted(considered):
        current_quantity = current_positions.get(stock_id, 0)
        holding = target_map.get(stock_id)
        target_quantity = resolve_target_quantity(holding, equity, costs) if holding else 0

        reference_price = _reference_price(stock_id, holding, prices)
        if reference_price is None:
            logger.warning(
                "Order plan item for stock %s cannot be valued: no reference price is "
                "available. No order is planned for it.",
                stock_id,
            )
            continue

        items.append(
            _plan_item(
                stock_id=stock_id,
                symbol=(holding.symbol if holding else None) or symbols.get(stock_id),
                sector=(holding.sector if holding else None) or sectors.get(stock_id),
                current_quantity=current_quantity,
                target_quantity=target_quantity,
                reference_price=reference_price,
                costs=costs,
                order_type=order_type,
                target_weight_pct=holding.target_weight_pct if holding else None,
            )
        )

    plan = OrderPlan(
        portfolio_id=portfolio_id,
        plan_id=plan_id,
        items=tuple(items),
        created_at=as_of,
        equity=equity,
        cash=cash,
        idempotency_key=idempotency_key,
    )
    logger.info(
        "Order plan %s built for portfolio %s: %s item(s), %s buy, %s sell, cash required %s, "
        "cash released %s",
        plan_id,
        portfolio_id,
        len(plan.items),
        len(plan.buy_items),
        len(plan.sell_items),
        plan.cash_required,
        plan.cash_released,
    )
    return plan


def _reference_price(
    stock_id: int,
    holding: Optional[TargetHolding],
    prices: Mapping[int, Decimal],
) -> Optional[Decimal]:
    """The price an item is valued at: the supplied mark, then the target's own price.

    A price of zero or less is treated as absent. It cannot produce a meaningful share count and
    would report a target that is worth nothing as an exit rather than as unvaluable.
    """
    price = prices.get(stock_id)
    if price is None and holding is not None:
        price = holding.price
    if price is None:
        return None
    price = quantize_price(price)
    return price if price > Decimal("0") else None


def _plan_item(
    *,
    stock_id: int,
    symbol: Optional[str],
    sector: Optional[str],
    current_quantity: int,
    target_quantity: int,
    reference_price: Decimal,
    costs: TradingCostConfig,
    order_type: OrderType,
    target_weight_pct: Optional[Decimal],
) -> OrderPlanItem:
    """Build one item, with its estimated value and fees at the reference price.

    The estimates use the same :func:`~app.services.portfolio.calculator.calculate_fee` the fill
    path uses, so the cash the plan says a buy needs is the cash the buy will actually take
    (allowing for slippage, which moves the price and therefore the fee). Validation compares
    cash against these figures *before* submission, so they must not be optimistic.
    """
    delta = target_quantity - current_quantity
    side = OrderSide.BUY if delta > 0 else OrderSide.SELL
    quantity = abs(delta)
    value = gross_value(quantity, reference_price)
    fees = calculate_fee(value, costs)
    return OrderPlanItem(
        stock_id=stock_id,
        symbol=symbol,
        side=side,
        current_quantity=current_quantity,
        target_quantity=target_quantity,
        quantity_delta=delta,
        quantity=quantity,
        reference_price=reference_price,
        estimated_value=value,
        estimated_fees=fees,
        order_type=order_type,
        target_weight_pct=target_weight_pct,
        sector=sector,
    )
