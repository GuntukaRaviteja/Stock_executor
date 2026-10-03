"""Phase 9 bridge from the existing Phase 7 signal engine to paper orders.

The rules are **reused, not reimplemented**. This module:

* asks the existing :class:`~app.services.signal_service.SignalService` for the Phase 6
  indicator context of a stock;
* hands that context to the existing Phase 8
  :class:`~app.services.backtest.strategy.Phase7TechnicalStrategy` from the strategy registry;
* translates its ``enter`` / ``exit`` / ``hold`` decision into a single paper
  :class:`~app.services.portfolio.models.OrderIntent`.

No indicator, signal or entry/exit rule is written here. If Phase 7 or Phase 8 changes its
behaviour, this bridge changes with it automatically, and the Phase 7/8 test suites keep
their meaning.

Order sizing on entry mirrors the Phase 8 model — a percentage of available cash, floored to
whole shares with fees included in the unit cost — so a paper entry and a backtest entry size
the same way.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.core.exceptions import StockNotFoundError, UnknownStrategyError
from app.core.logging import logger
from app.repositories.stock_repository import stock_repository
from app.schemas.portfolio import OrderSide, OrderSource
from app.services.backtest.strategy import (
    STRATEGY_NAME,
    StrategyAction,
    strategy_registry,
)
from app.services.portfolio.calculator import size_quantity_from_cash
from app.services.portfolio.defaults import default_position_size_pct
from app.services.portfolio.models import (
    ZERO,
    OrderIntent,
    PortfolioAccount,
    StrategyOrderPlan,
    TradingCostConfig,
)
from app.services.signal_service import signal_service


class PaperStrategyBridge:
    """Turns Phase 7 signals plus Phase 8 strategy rules into paper orders."""

    def __init__(self, signals=signal_service, registry=strategy_registry, stock_repo=stock_repository):
        self.signals = signals
        self.registry = registry
        self.stock_repo = stock_repo

    def build_plan(
        self,
        db: Session,
        account: PortfolioAccount,
        stock_id: int,
        *,
        costs: TradingCostConfig,
        strategy_name: str = STRATEGY_NAME,
        price: Optional[Decimal] = None,
        position_size_pct: Optional[Decimal] = None,
        quantity: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> StrategyOrderPlan:
        """Decide what the existing strategy rules want to do for ``stock_id``.

        Raises:
            StockNotFoundError: unknown ``stock_id``.
            UnknownStrategyError: unknown strategy name.
        """
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            raise StockNotFoundError(stock_id)

        strategy = self.registry.get(strategy_name)
        if strategy is None:
            raise UnknownStrategyError(strategy_name, self.registry.available)

        context = self.signals.build_context(db, stock_id)
        if context is None:
            return StrategyOrderPlan(
                stock_id=stock_id,
                action=StrategyAction.HOLD.value,
                composite_score=0,
                signal_indicators=[],
                as_of=None,
                reason="No price data is stored for this stock, so no signal could be produced.",
                intent=None,
                strategy_name=strategy.strategy_name,
            )

        position = account.position_for(stock_id)
        action, score, signals = strategy.decide(
            is_long=position is not None, context=context
        )
        indicators = sorted({signal.indicator for signal in signals})
        as_of = context.as_of.isoformat()

        if action is StrategyAction.ENTER:
            return self._entry_plan(
                account,
                stock_id,
                price=price if price is not None else context.latest_price,
                costs=costs,
                score=score,
                indicators=indicators,
                as_of=as_of,
                strategy_name=strategy.strategy_name,
                position_size_pct=position_size_pct,
                quantity=quantity,
                idempotency_key=idempotency_key,
            )

        if action is StrategyAction.EXIT and position is not None:
            exit_price = price if price is not None else (context.latest_price or position.current_price)
            if exit_price is None or Decimal(exit_price) <= ZERO:
                return StrategyOrderPlan(
                    stock_id=stock_id,
                    action=action.value,
                    composite_score=score,
                    signal_indicators=indicators,
                    as_of=as_of,
                    reason=(
                        "The strategy produced an exit signal but no execution price is "
                        "available, so no order was placed."
                    ),
                    intent=None,
                    strategy_name=strategy.strategy_name,
                )
            intent = OrderIntent(
                portfolio_id=account.portfolio_id,
                stock_id=stock_id,
                side=OrderSide.SELL,
                quantity=position.quantity,
                requested_price=Decimal(str(exit_price)),
                source=OrderSource.STRATEGY,
                idempotency_key=idempotency_key,
                strategy_event_id=build_strategy_event_id(stock_id, as_of),
            )
            return StrategyOrderPlan(
                stock_id=stock_id,
                action=action.value,
                composite_score=score,
                signal_indicators=indicators,
                as_of=as_of,
                reason=(
                    f"Phase 8 strategy rules produced an exit signal (composite score "
                    f"{score}); closing {position.quantity} share(s)."
                ),
                intent=intent,
                strategy_name=strategy.strategy_name,
            )

        return StrategyOrderPlan(
            stock_id=stock_id,
            action=StrategyAction.HOLD.value,
            composite_score=score,
            signal_indicators=indicators,
            as_of=as_of,
            reason=(
                f"Phase 8 strategy rules produced no entry or exit condition "
                f"(composite score {score}). No order was placed."
            ),
            intent=None,
            strategy_name=strategy.strategy_name,
        )

    # --- Internals ---

    def _entry_plan(
        self,
        account: PortfolioAccount,
        stock_id: int,
        *,
        price: Optional[Decimal],
        costs: TradingCostConfig,
        score: int,
        indicators,
        as_of: str,
        strategy_name: str,
        position_size_pct: Optional[Decimal],
        quantity: Optional[int],
        idempotency_key: Optional[str],
    ) -> StrategyOrderPlan:
        """Build an entry order, or explain why no order could be built."""
        entry_price = price
        if entry_price is None or Decimal(entry_price) <= ZERO:
            return StrategyOrderPlan(
                stock_id=stock_id,
                action=StrategyAction.ENTER.value,
                composite_score=score,
                signal_indicators=indicators,
                as_of=as_of,
                reason=(
                    "The strategy produced an entry signal but no execution price is "
                    "available, so no order was placed."
                ),
                intent=None,
                strategy_name=strategy_name,
            )

        if quantity is None:
            budget_pct = (
                Decimal(str(position_size_pct))
                if position_size_pct is not None
                else default_position_size_pct()
            )
            quantity = size_quantity_from_cash(account.cash, budget_pct, entry_price, costs)

        if quantity <= 0:
            logger.info(
                "Strategy entry for stock %s produced no affordable quantity from %s cash",
                stock_id,
                account.cash,
            )
            return StrategyOrderPlan(
                stock_id=stock_id,
                action=StrategyAction.ENTER.value,
                composite_score=score,
                signal_indicators=indicators,
                as_of=as_of,
                reason=(
                    "The strategy produced an entry signal but the available cash cannot buy "
                    "a single share at this price, so no order was placed."
                ),
                intent=None,
                strategy_name=strategy_name,
            )

        intent = OrderIntent(
            portfolio_id=account.portfolio_id,
            stock_id=stock_id,
            side=OrderSide.BUY,
            quantity=quantity,
            requested_price=Decimal(str(entry_price)),
            source=OrderSource.STRATEGY,
            idempotency_key=idempotency_key,
            strategy_event_id=build_strategy_event_id(stock_id, as_of),
        )
        return StrategyOrderPlan(
            stock_id=stock_id,
            action=StrategyAction.ENTER.value,
            composite_score=score,
            signal_indicators=indicators,
            as_of=as_of,
            reason=(
                f"Phase 8 strategy rules produced an entry signal (composite score {score}); "
                f"buying {quantity} share(s)."
            ),
            intent=intent,
            strategy_name=strategy_name,
        )


def build_strategy_event_id(stock_id: int, as_of: str) -> str:
    """Deterministic duplicate-protection key for one strategy decision.

    The key is derived from the signal date, so re-running the same signal bar in the same
    portfolio returns the original order instead of executing a second time — without any
    distributed locking.
    """
    return f"phase9:{stock_id}:{as_of}"