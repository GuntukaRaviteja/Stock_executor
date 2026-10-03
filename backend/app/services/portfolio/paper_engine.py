"""Phase 10 paper-trading engine: sequential simulated execution over stored history.

This is the engine that answers "can trades be simulated sequentially using historical market
data?" It steps through one stock's stored bars in chronological order and, for each bar,
repeats the same three steps the Phase 8 simulator uses:

1. **execute** the decision taken on the previous bar, at this bar's open;
2. **mark** the open position to this bar's close and record an equity-curve point;
3. **decide** from this bar's close only, producing the decision for the next bar.

Look-ahead prevention
---------------------
Two independent guards, and the second is the one that matters for the decision itself:

* :func:`~app.services.portfolio.execution.assert_no_lookahead` refuses to resolve an
  execution price from a bar that did not exist at decision time.
* The indicator context fed to the Phase 7 signal engine is built by the **Phase 8**
  :class:`~app.services.backtest.indicators.PointInTimeContextBuilder`, which is explicitly
  point-in-time: ``contexts[i]`` contains no information from ``bars[i + 1:]``. Phase 10
  therefore reuses the Phase 8 indicator replay rather than calling the analytics service
  once per bar, which would have given every bar the whole history including the future.

Every order records both timestamps: ``signal_as_of`` (the bar whose close produced the
decision) and ``executed_at`` (the bar whose price was used). The look-ahead regression test
asserts that the signal timestamp never post-dates the execution timestamp, and that no fill
price ever equals a price from a bar later than the execution bar.

Every fill goes through the **same** validation, risk check, position update and atomic
persistence path as a manual order — the engine adds timing, not a second set of rules.
There is no leverage, no short selling and no order book: this is a deterministic simulation
of a fixed rule set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from app.core.exceptions import ExecutionPriceUnavailableError, StockNotFoundError
from app.core.logging import logger
from app.models.portfolio import Portfolio
from app.repositories.price_repository import price_repository
from app.repositories.stock_repository import stock_repository
from app.schemas.analytics import IndicatorContext
from app.schemas.portfolio import ExecutionPricePolicy, OrderSide, OrderSource
from app.services.backtest.indicators import PointInTimeContextBuilder
from app.services.backtest.models import Bar
from app.services.backtest.strategy import STRATEGY_NAME, StrategyAction, strategy_registry
from app.services.backtest.validator import load_bars, validate_bars
from app.services.portfolio.execution import resolve_execution_price
from app.services.portfolio.models import (
    ZERO,
    OrderIntent,
    PaperRunOrder,
    PaperRunResult,
    PositionSizingMethod,
)
from app.services.portfolio.position_sizing_service import position_sizing_service
from app.services.portfolio.service import PaperTradingService

#: Calendar days of history fetched before the first recorded bar so the indicators are warm.
#: Identical to the Phase 8 backtest service constant: SMA(50) needs 50 trading days and MACD
#: needs 26 + 9 observations.
INDICATOR_WARMUP_CALENDAR_DAYS = 400


class PaperTradingEngine:
    """Replays stored bars against a paper portfolio, one decision per bar."""

    def __init__(
        self,
        service: Optional[PaperTradingService] = None,
        price_repo=price_repository,
        stock_repo=stock_repository,
        context_builder: Optional[PointInTimeContextBuilder] = None,
        registry=strategy_registry,
        sizer=position_sizing_service,
    ):
        self.service = service or PaperTradingService()
        self.price_repo = price_repo
        self.stock_repo = stock_repo
        self.context_builder = context_builder or PointInTimeContextBuilder()
        self.registry = registry
        self.sizer = sizer

    # --- Public API ---

    def run(
        self,
        db: Session,
        portfolio: Portfolio,
        stock_id: int,
        *,
        start_date=None,
        end_date=None,
        strategy_name: str = STRATEGY_NAME,
        policy: ExecutionPricePolicy = ExecutionPricePolicy.OPEN,
        sizing=None,
    ) -> PaperRunResult:
        """Replay one stock's stored history against ``portfolio``.

        Raises:
            StockNotFoundError: unknown ``stock_id``.
            UnknownStrategyError: unknown strategy name.
            BacktestDataValidationError: the stored OHLCV cannot support a replay.
            ExecutionPriceUnavailableError: the window ends before the final pending decision
                could be filled, or a needed bar has no usable price.
        """
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            raise StockNotFoundError(stock_id)
        strategy = self.registry.get(strategy_name)
        if strategy is None:
            from app.core.exceptions import UnknownStrategyError

            raise UnknownStrategyError(strategy_name, self.registry.available)

        bars, recorded = self._load_bars(db, stock_id, start_date, end_date)
        if not recorded:
            logger.warning(
                "Paper run for portfolio %s stock %s had no recorded bars in the window",
                portfolio.id,
                stock_id,
            )
            return PaperRunResult(
                portfolio_id=portfolio.id,
                stock_id=stock.id,
                symbol=stock.symbol,
                strategy=strategy.strategy_name,
                start_date=start_date,
                end_date=end_date,
                execution_price_policy=policy,
                bars_processed=0,
            )

        contexts = self.context_builder.build(stock.id, stock.symbol, bars)
        orders: List[PaperRunOrder] = []

        # Maps a recorded bar to its index in ``bars`` so the execution price is always taken
        # from the *stored* bar and never recomputed.
        index_of = {bar.date: index for index, bar in enumerate(bars)}
        pending: Optional["_PendingDecision"] = None
        snapshots = 0

        # Only bars inside the recorded window are stepped through; earlier bars exist solely
        # to warm the indicators. Membership is tested by trading date.
        recorded_dates = {bar.date for bar in recorded}
        warmup_bars = len(bars) - len(recorded)
        for index, bar in enumerate(bars):
            if bar.date not in recorded_dates:
                continue

            # --- 1. Execute the decision taken on the previous recorded bar ---
            if pending is not None:
                produced = self._execute(db, portfolio, strategy, pending, bar, policy, index_of)
                if produced is not None:
                    orders.append(produced)
                pending = None

            # --- 2. Mark open positions to this bar's close and record the equity point ---
            self._mark_and_snapshot(db, portfolio, stock_id, bar)
            snapshots += 1

            # --- 3. Decide from this bar's close only, for the next recorded bar ---
            context = contexts[index]
            if context is not None and index < len(bars) - 1:
                pending = self._decide(
                    db,
                    portfolio,
                    stock_id,
                    strategy,
                    context,
                    bar,
                    sizing,
                )

        # A run that produced no trades never touches the order path, so its equity curve is
        # still only pending in this session. Commit here to make the curve durable either way.
        db.commit()

        logger.info(
            "Paper run finished for portfolio %s stock %s: %d bar(s), %d order(s), "
            "policy=%s",
            portfolio.id,
            stock.id,
            len(recorded),
            len(orders),
            policy.value,
        )
        return PaperRunResult(
            portfolio_id=portfolio.id,
            stock_id=stock.id,
            symbol=stock.symbol,
            strategy=strategy.strategy_name,
            start_date=start_date,
            end_date=end_date,
            execution_price_policy=policy,
            bars_processed=len(recorded),
            orders=tuple(orders),
            snapshots=snapshots,
            skipped_bars=warmup_bars,
        )

    # --- Steps ---

    def _load_bars(self, db: Session, stock_id: int, start_date, end_date) -> "tuple":
        """Warm-up bars plus the recorded window, validated as a single series."""
        from datetime import timedelta

        warmup_start = (
            start_date - timedelta(days=INDICATOR_WARMUP_CALENDAR_DAYS)
            if start_date is not None
            else None
        )
        rows = self.price_repo.get_history_asc(
            db, stock_id, start_date=warmup_start, end_date=end_date
        )
        bars = load_bars(rows)
        report = validate_bars(bars)
        recorded = [bar for bar in report.bars if end_date is None or bar.date <= end_date]
        if not recorded:
            return bars, []
        effective_start = start_date if start_date is not None else recorded[0].date
        window = [bar for bar in report.bars if bar.date >= effective_start]
        return report.bars, window

    def _decide(
        self,
        db: Session,
        portfolio: Portfolio,
        stock_id: int,
        strategy,
        context: IndicatorContext,
        bar: Bar,
        sizing,
    ) -> Optional["_PendingDecision"]:
        """Ask the Phase 8 rules what to do at the close of ``bar``.

        The decision carries only what was knowable at that moment: the action, the score, the
        indicators that fired and the signal bar itself. The order is built later, at
        execution time, from the execution bar's price.
        """
        position = self.service.get_position_state(db, portfolio.id, stock_id)
        action, score, signals = strategy.decide(is_long=position is not None, context=context)
        indicators = sorted({signal.indicator for signal in signals})
        # Compare by value, never by identity: ``StrategyAction`` is declared both in the
        # backtest schemas and in the portfolio API schemas, so an action produced elsewhere is
        # an equal *member* of a different enum class. An identity check would silently read a
        # HOLD as an ENTER, which would open a position that was never decided on.
        action = StrategyAction(action.value)
        if action is StrategyAction.HOLD:
            return None
        return _PendingDecision(
            stock_id=stock_id,
            action=action,
            composite_score=score,
            signal_indicators=indicators,
            signal_bar=bar,
            sizing=sizing,
        )

    def _execute(
        self,
        db: Session,
        portfolio: Portfolio,
        strategy,
        pending: "_PendingDecision",
        execution_bar: Bar,
        policy: ExecutionPricePolicy,
        index_of,
    ) -> Optional[PaperRunOrder]:
        """Fill the pending decision on ``execution_bar``.

        The execution price is resolved by the policy — never taken from a later bar — and the
        order then travels through the ordinary validation, risk and persistence path, so a
        rejected simulated trade is recorded as a rejected order exactly like a manual one.
        """
        reference = resolve_execution_price(pending.signal_bar, execution_bar, policy)
        price = reference.price
        account = self.service.account_state(db, portfolio)

        if pending.action is StrategyAction.EXIT:
            position = account.position_for(pending.stock_id)
            if position is None or position.quantity <= 0:
                logger.debug(
                    "Paper run: exit decision on %s has no position to close",
                    pending.signal_bar.date.isoformat(),
                )
                return None
            intent = OrderIntent(
                portfolio_id=portfolio.id,
                stock_id=pending.stock_id,
                side=OrderSide.SELL,
                quantity=position.quantity,
                requested_price=price,
                source=OrderSource.STRATEGY,
                strategy_event_id=_event_id(pending, policy),
                signal_as_of=_bar_timestamp(pending.signal_bar),
                execution_price_policy=policy,
                executed_at=_bar_timestamp(execution_bar),
                metadata=_metadata(pending, strategy),
            )
        else:
            quantity = self._entry_quantity(db, portfolio, account, pending, price)
            if quantity <= 0:
                logger.info(
                    "Paper run: entry decision on %s produced no affordable quantity at %s",
                    pending.signal_bar.date.isoformat(),
                    price,
                )
                return None
            intent = OrderIntent(
                portfolio_id=portfolio.id,
                stock_id=pending.stock_id,
                side=OrderSide.BUY,
                quantity=quantity,
                requested_price=price,
                source=OrderSource.STRATEGY,
                strategy_event_id=_event_id(pending, policy),
                signal_as_of=_bar_timestamp(pending.signal_bar),
                execution_price_policy=policy,
                executed_at=_bar_timestamp(execution_bar),
                metadata=_metadata(pending, strategy),
            )

        return self.service.submit(db, intent)

    def _entry_quantity(
        self, db: Session, portfolio: Portfolio, account, pending: "_PendingDecision", price: Decimal
    ) -> int:
        """Size an entry with the configured formula, falling back to a percentage of cash."""
        if pending.sizing is None:
            from app.services.portfolio.defaults import default_position_size_pct
            from app.services.portfolio.calculator import size_quantity_from_cash

            return size_quantity_from_cash(
                account.cash, default_position_size_pct(), price, self.service.trading_costs(portfolio)
            )
        plan = self.sizer.size(
            db,
            equity=account.equity,
            available_cash=account.cash,
            costs=self.service.trading_costs(portfolio),
            request=pending.sizing,
            price=price,
        )
        return plan.quantity

    def _mark_and_snapshot(
        self, db: Session, portfolio: Portfolio, stock_id: int, bar: Bar
    ) -> None:
        """Mark the position to ``bar``'s close and record an equity-curve point.

        Only the replayed stock's close is applied; every other position keeps its last mark,
        because this engine only has history for the stock it was asked to replay.
        """
        if bar.close is None or Decimal(bar.close) <= ZERO:
            return
        self.service.record_equity_point(
            db, portfolio, {stock_id: Decimal(str(bar.close))}, _bar_timestamp(bar)
        )


# ----------------------------------------------------------------------
# Internal value objects
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _PendingDecision:
    """A decision taken from one bar's close, waiting for the next bar to execute on."""

    stock_id: int
    action: StrategyAction
    composite_score: int
    signal_indicators: Sequence[str]
    signal_bar: Bar
    sizing: object = None


def _bar_timestamp(bar: Bar) -> datetime:
    """Naive-UTC midnight of a bar's trading date.

    Stored timestamps are naive UTC throughout the project, so a historical execution is
    stamped with the trading day it belongs to rather than the wall-clock time of the replay.
    """
    return datetime.combine(bar.date, time.min)


def _event_id(pending: "_PendingDecision", policy: ExecutionPricePolicy) -> str:
    """Deterministic duplicate-protection key for one decision.

    Keyed by the signal bar, so re-running the same signal returns the original order instead
    of executing twice. The policy is part of the key because the same signal bar legitimately
    produces different orders under different execution conventions.
    """
    return f"paper:{policy.value.lower()}:{pending.stock_id}:{pending.signal_bar.date.isoformat()}"


def _metadata(pending: "_PendingDecision", strategy) -> dict:
    """Audit metadata recorded on every simulated order."""
    return {
        "strategy": strategy.strategy_name,
        "strategy_version": strategy.strategy_version,
        "action": pending.action.value,
        "composite_score": pending.composite_score,
        "signal_indicators": list(pending.signal_indicators),
        "signal_date": pending.signal_bar.date.isoformat(),
    }


paper_trading_engine = PaperTradingEngine()