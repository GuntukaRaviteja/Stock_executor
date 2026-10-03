"""Phase 13 portfolio backtesting — several stocks, one portfolio, chronological.

This is **not** a second backtesting system. It reuses Phase 8 end to end:

* :class:`~app.services.backtest.models.Bar` for the normalised OHLCV bars;
* :class:`~app.services.backtest.indicators.PointInTimeContextBuilder` for the indicator
  replay, so ``contexts[i]`` contains nothing from ``bars[i + 1:]``;
* :class:`~app.services.backtest.strategy.strategy_registry` for the entry/exit rules;
* :func:`~app.services.backtest.metrics.build_performance_metrics` for the performance report;
* :class:`~app.services.backtest.models.Trade` / ``EquityPoint`` for the results;
* the Phase 9 cost model (:func:`~app.services.portfolio.calculator.calculate_fee`,
  :func:`~app.services.portfolio.calculator.apply_slippage`) for commission and slippage.

What is new is the **portfolio**: several stocks share one cash balance, compete for one
exposure ceiling and one position count, and are sized by the Phase 13 risk-and-weight rule
rather than by a fixed percentage of cash.

No look-ahead
-------------
The per-bar order of operations is Phase 8's, unchanged:

1. fill the decision taken from the previous bar of **that stock**, at that bar's open;
2. evaluate the resting stop against that bar's own open/low;
3. mark every position to that bar's close and append an equity point;
4. decide from that bar's close, for that stock's next bar.

Two properties follow, and both are asserted by tests rather than asserted in prose:

* **A decision never sees its own future.** Step 1 of date *t* uses a decision made at the
  close of a bar strictly before *t*, priced at *t*'s open. No close of *t*, and no bar after
  *t*, can influence whether the trade at *t* happened or how big it was.
* **Sizing uses only stale marks.** A position in stock *A* is marked at its last close when
  stock *B*'s entry is being sized, because a portfolio equity figure has to be computable
  before the day is finished. This is the conservative choice: it never uses a price the
  portfolio had not yet seen.

The intrabar stop is the one exception to "nothing within the bar", and it is deliberate: a
stop is a **resting order in the book**, so it is evaluated from the bar's own open and low —
the same convention Phase 8 uses, and the same one. A gap straight through the stop fills at the
open, because the stop price was never available.

Position sizing during the replay
--------------------------------
An entry is sized with :class:`~app.services.portfolio.sizing.PositionSizingEngine` against the
portfolio state *at the moment of the fill*: equity marked from last-known closes, cash after
everything already filled that morning, the weight capacity left in that position, and the
policy's exposure ceiling. Every refusal is counted and reported; none is retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.exceptions import AppValidationError, UnknownStrategyError
from app.core.logging import logger
from app.schemas.analytics import IndicatorContext
from app.services.backtest.metrics import build_performance_metrics
from app.services.backtest.models import (
    Bar,
    EquityPoint,
    ExitReason,
    PerformanceMetrics,
    Trade,
    quantize_money,
    quantize_percent,
)
from app.services.backtest.strategy import STRATEGY_NAME, StrategyAction, strategy_registry

#: The default strategy lookup, named so the constructor default reads as what it is.
STRATEGY_REGISTRY = strategy_registry
from app.services.portfolio.allocation import stop_price as stop_from_pct
from app.services.portfolio.calculator import (
    apply_slippage,
    calculate_fee,
    quantize_price,
)
from app.services.portfolio.models import ZERO, PortfolioRiskPolicy
from app.services.portfolio.sizing import (
    METHOD_MAX_POSITION_CAP,
    METHOD_RISK_AND_WEIGHT,
    PositionSizingEngine,
)

_ONE_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class BacktestStock:
    """One stock's replayable history, already validated and indicator-warmed.

    ``bars`` is the **whole** series including the warm-up bars that precede the recorded
    window, and ``contexts`` is aligned with it index for index. ``stop_price`` overrides the
    policy's ``stop_loss_pct`` for this name when supplied.
    """

    stock_id: int
    symbol: str
    bars: Tuple[Bar, ...]
    contexts: Tuple[IndicatorContext, ...]
    stop_price: Optional[Decimal] = None


@dataclass(frozen=True)
class PortfolioEquityPoint:
    """One day of the portfolio equity curve, with the risk figures of that day."""

    date: date_type
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    exposure_pct: Optional[Decimal]
    drawdown_pct: Decimal
    position_count: int


@dataclass(frozen=True)
class PortfolioBacktestResult:
    """Everything one portfolio backtest produced.

    Reuses :class:`~app.services.backtest.models.PerformanceMetrics` for every return, risk and
    trade statistic Phase 8 already computes, rather than re-deriving them: a portfolio backtest
    and a single-stock backtest must report the same figure for the same numbers.
    """

    portfolio_id: int
    initial_capital: Decimal
    final_equity: Decimal
    ending_cash: Decimal
    ending_invested_capital: Decimal
    total_fees: Decimal
    days_processed: int
    rejected_signals: int
    metrics: PerformanceMetrics
    trades: Tuple[Trade, ...] = ()
    equity_curve: Tuple[PortfolioEquityPoint, ...] = ()
    average_position_size: Decimal = ZERO
    average_exposure_pct: Optional[Decimal] = None
    maximum_exposure_pct: Optional[Decimal] = None
    policy: Optional[PortfolioRiskPolicy] = None
    #: The fill prices used, paired with the date they were taken from, so a reader can verify
    #: that no fill was priced from a bar later than the one that produced its signal.
    execution_log: Tuple = ()
    warnings: Tuple = ()

    @property
    def total_pnl(self) -> Decimal:
        """Equity change over the run."""
        return quantize_money(self.final_equity - self.initial_capital)

    @property
    def no_look_ahead(self) -> bool:
        """True when no fill post-dates the bar that produced its decision.

        Verified from the run's own execution log rather than asserted: a backtest that cannot
        show its fills are ordered correctly should not claim they are.
        """
        return all(
            signal_date < fill_date for signal_date, fill_date in self.execution_log
        )


@dataclass
class _OpenPosition:
    """One simulated open long position inside the replay."""

    stock_id: int
    quantity: int
    entry_date: date_type
    entry_price: Decimal
    entry_reference_price: Decimal
    entry_value: Decimal
    entry_fees: Decimal
    entry_cash_out: Decimal
    stop_price: Optional[Decimal]
    score: int
    last_close: Decimal


@dataclass(frozen=True)
class _Pending:
    """A decision taken at one bar's close, waiting for that stock's next bar to fill it."""

    action: str
    score: int
    signal_date: date_type


class PortfolioBacktestEngine:
    """Replays several stocks against one simulated portfolio, chronologically.

    Pure: bars go in, a result comes out. Loading bars from the database is the caller's job,
    which is what makes the whole simulation testable without a fixture.

    ``registry`` is the strategy lookup, injectable so a test can replay against a strategy
    whose decisions it controls exactly, the same seam
    :class:`~app.services.portfolio.paper_engine.PaperTradingEngine` exposes.
    """

    def __init__(
        self,
        sizer: Optional[PositionSizingEngine] = None,
        registry=STRATEGY_REGISTRY,
    ):
        self.sizer = sizer or PositionSizingEngine()
        self.registry = registry

    def run(
        self,
        *,
        portfolio_id: int,
        stocks: Sequence[BacktestStock],
        policy: PortfolioRiskPolicy,
        initial_capital: Decimal,
        start_date: date_type,
        end_date: date_type,
        strategy_name: str = STRATEGY_NAME,
        exit_on_signal: bool = True,
        mark_unpriceable_positions: bool = True,
        risk_free_rate_pct: Decimal = ZERO,
    ) -> PortfolioBacktestResult:
        """Replay ``stocks`` from ``start_date`` to ``end_date`` against one portfolio.

        Raises:
            ValueError: no stocks, a non-positive initial capital, or an empty recorded window.
            UnknownStrategyError: ``strategy_name`` is not registered.
        """
        if not stocks:
            raise ValueError("a portfolio backtest needs at least one stock to replay")
        if Decimal(initial_capital) <= ZERO:
            raise ValueError("initial_capital must be greater than zero")
        policy.validate()

        strategy = self.registry.get(strategy_name)
        if strategy is None:
            raise UnknownStrategyError(strategy_name, self.registry.available)

        book = _Book(
            stocks={stock.stock_id: stock for stock in stocks},
            policy=policy,
            initial_capital=quantize_money(initial_capital),
            start_date=start_date,
            end_date=end_date,
            exit_on_signal=exit_on_signal,
            mark_unpriceable_positions=mark_unpriceable_positions,
        )
        self._replay(book, strategy)
        return self._assemble(book, portfolio_id, risk_free_rate_pct)

    # --- Replay ---

    def _replay(self, book: "_Book", strategy) -> None:
        """Step through the merged calendar once, in order.

        Visiting dates in ascending order across **all** stocks — rather than running each
        stock's series independently — is what makes this a *portfolio* backtest: every stock
        competes for the same cash on the same day, and no stock's decisions can be influenced
        by another stock's later bars.
        """
        for day in book.calendar():
            # --- 1. Execute each stock's pending decision, at that stock's own bar open ---
            for stock_id in book.stock_ids_on(day):
                pending = book.pending.get(stock_id)
                if pending is None:
                    continue
                book.pending.pop(stock_id, None)
                self._execute(book, stock_id, day, pending, strategy)

            # --- 2. Resting stops, evaluated from each bar's own open and low ---
            for stock_id in book.stock_ids_on(day):
                if book.positions.get(stock_id) is not None:
                    self._check_stop(book, stock_id, day)

            # --- 3. Mark every position and record the day's equity point ---
            self._mark_and_record(book, day)

            # --- 4. Decide from each bar's close, for that stock's next bar ---
            for stock_id in book.stock_ids_on(day):
                self._decide(book, stock_id, day, strategy)

        self._liquidate(book)

    def _execute(
        self,
        book: "_Book",
        stock_id: int,
        day: date_type,
        pending: _Pending,
        strategy,
    ) -> None:
        """Fill a pending decision for ``stock_id`` on its own bar for ``day``."""
        bar = book.bar_for(stock_id, day)
        if bar is None or bar.open is None or Decimal(bar.open) <= ZERO:
            return
        reference = quantize_price(Decimal(bar.open))

        if pending.action == StrategyAction.EXIT.value:
            position = book.positions.get(stock_id)
            if position is None:
                return
            self._close(book, stock_id, day, reference, ExitReason.SIGNAL, pending.score)
            return

        if book.positions.get(stock_id) is not None:
            # No pyramiding: a signal on a name already held is a no-op, reported as such.
            book.rejected_signals += 1
            return

        if pending.action != StrategyAction.ENTER.value:
            return

        self._open(book, stock_id, day, reference, pending)

    def _open(
        self,
        book: "_Book",
        stock_id: int,
        day: date_type,
        reference: Decimal,
        pending: _Pending,
    ) -> None:
        """Size and fill an entry, then apply every portfolio limit to the result."""
        stock = book.stocks[stock_id]
        costs = book.policy.as_trading_costs()
        fill_price = apply_slippage(reference, True, costs)
        stop = self._stop_for(book, stock, fill_price)

        equity = book.equity_at_last_close()
        result = self._size(
            book=book,
            stock_id=stock_id,
            equity=equity,
            cash=book.cash,
            entry_price=fill_price,
            stop=stop,
        )
        if result is None:
            book.rejected_signals += 1
            return

        quantity, capital_required, fees = result
        book.cash = quantize_money(book.cash - capital_required)
        book.positions[stock_id] = _OpenPosition(
            stock_id=stock_id,
            quantity=quantity,
            entry_date=day,
            entry_price=fill_price,
            entry_reference_price=reference,
            entry_value=quantize_money(fill_price * Decimal(quantity)),
            entry_fees=fees,
            entry_cash_out=capital_required,
            stop_price=stop,
            score=pending.score,
            last_close=fill_price,
        )
        book.execution_log.append((pending.signal_date, day))
        logger.debug(
            "Portfolio backtest opened %s x%d at %s on %s (equity %s)",
            stock_id,
            quantity,
            fill_price,
            day,
            equity,
        )

    def _size(
        self,
        *,
        book: "_Book",
        stock_id: int,
        equity: Decimal,
        cash: Decimal,
        entry_price: Decimal,
        stop: Optional[Decimal],
    ) -> Optional[tuple]:
        """Size an entry with the Phase 13 rule and apply the portfolio limits to it.

        Returns ``(quantity, capital_required, fees)`` when the entry is accepted, or ``None``
        when any configured limit refuses it. A refusal is **counted and never retried**: the
        next signal for this stock is a new decision, not a retry of this one, because the price
        that produced this decision no longer exists.
        """
        policy = book.policy
        # The weight cap is per position, so what is already held *in this stock* is what
        # reduces its allowance. Charging every other position against it would let the first
        # name to fill its allowance leave every later name with none.
        held = book.positions.get(stock_id)
        existing_value = quantize_money(
            ZERO if held is None else held.last_close * Decimal(held.quantity)
        )
        capacity = quantize_money(policy.max_position_value(equity) - existing_value)

        # A stop is optional in the policy, so the sizing model follows it: with a stop the
        # entry is sized from the risk it puts at risk, without one it falls back to the
        # capital-only model rather than refusing every entry in a stop-less backtest.
        method = METHOD_RISK_AND_WEIGHT if stop is not None else METHOD_MAX_POSITION_CAP

        try:
            breakdown = self.sizer.size(
                equity=equity,
                available_cash=cash,
                entry_price=entry_price,
                stop_price=stop,
                policy=policy,
                method=method,
                remaining_weight_capacity=capacity,
            )
        except (AppValidationError, ValueError, ArithmeticError):
            # A refused size is a refusal, not a crash: the signal is counted as rejected.
            logger.debug(
                "Portfolio backtest could not size stock %s at %s under the active policy",
                stock_id,
                entry_price,
            )
            return None

        quantity = breakdown.quantity
        if quantity <= 0:
            return None
        capital_required = breakdown.capital_required
        if capital_required > cash:
            return None

        trade_value = quantize_money(entry_price * Decimal(quantity))
        invested = quantize_money(book.invested_at_last_close() + trade_value)
        if policy.max_exposure_pct is not None:
            if equity > ZERO and (invested / equity * _ONE_HUNDRED) > policy.max_exposure_pct:
                return None
        if policy.max_positions is not None and len(book.positions) >= policy.max_positions:
            return None
        cash_after = quantize_money(cash - capital_required)
        if policy.min_cash_reserve_pct > ZERO:
            if cash_after < policy.cash_reserve_required(equity):
                return None
        risk_limit = policy.max_total_risk(equity)
        if risk_limit is not None:
            if book.total_risk_at_last_close() + breakdown.risk_amount > risk_limit:
                return None
        return quantity, capital_required, breakdown.fees

    def _check_stop(self, book: "_Book", stock_id: int, day: date_type) -> None:
        """Fill the resting stop if this bar traded through it.

        Uses only the bar's own open and low, exactly as Phase 8 does. A gap straight through
        the stop fills at the open rather than at the stop, because the stop price was never
        available — which is the honest outcome, not a pessimistic one.
        """
        position = book.positions.get(stock_id)
        bar = book.bar_for(stock_id, day)
        if position is None or bar is None or position.stop_price is None:
            return
        if bar.low is None:
            return
        low = Decimal(bar.low)
        if low > position.stop_price:
            return
        reference = (
            Decimal(bar.open)
            if bar.open is not None and Decimal(bar.open) <= position.stop_price
            else position.stop_price
        )
        self._close(book, stock_id, day, quantize_price(reference), ExitReason.STOP_LOSS, position.score)

    def _decide(self, book: "_Book", stock_id: int, day: date_type, strategy) -> None:
        """Take a decision from this stock's bar for ``day``, to fill on its next bar.

        An exit signal is ignored when ``exit_on_signal`` is off: the caller has asked for the
        position to be held to its stop or to the end of the window, and a stop-only run is a
        legitimate thing to want.
        """
        context = book.context_for(stock_id, day)
        if context is None:
            return
        is_long = stock_id in book.positions
        action, score, _ = strategy.decide(is_long=is_long, context=context)
        action = StrategyAction(action.value)
        if action is StrategyAction.HOLD:
            book.pending.pop(stock_id, None)
            return
        if action is StrategyAction.EXIT and not book.exit_on_signal:
            book.pending.pop(stock_id, None)
            return
        book.pending[stock_id] = _Pending(action=action.value, score=int(score), signal_date=day)

    # --- Closing ---

    def _close(
        self,
        book: "_Book",
        stock_id: int,
        day: date_type,
        reference: Decimal,
        reason: ExitReason,
        score: int,
    ) -> None:
        """Fill a sell, book the realised P&L and release the cash.

        ``net_pnl`` is the **actual change in cash** the round trip caused, so the ledger and the
        reported P&L cannot disagree — the same identity Phase 8's simulator relies on.
        """
        position = book.positions.pop(stock_id, None)
        if position is None:
            return
        costs = book.policy.as_trading_costs()
        fill_price = apply_slippage(reference, False, costs)
        quantity = Decimal(position.quantity)

        exit_notional = quantize_money(fill_price * quantity)
        exit_fees = calculate_fee(exit_notional, costs)
        proceeds = quantize_money(exit_notional - exit_fees)

        net_pnl = quantize_money(proceeds - position.entry_cash_out)
        total_fees = quantize_money(position.entry_fees + exit_fees)
        # Slippage is always a positive cost: the entry filled above its reference price and the
        # exit fills below its own. Reporting it as a cost is what lets the identity
        # ``net_pnl == gross_pnl - commission - slippage`` hold exactly.
        entry_slippage = quantize_money(
            (position.entry_price - position.entry_reference_price) * quantity
        )
        exit_slippage = quantize_money((reference - fill_price) * quantity)
        slippage = quantize_money(entry_slippage + exit_slippage)
        gross_pnl = quantize_money(net_pnl + total_fees + slippage)

        book.cash = quantize_money(book.cash + proceeds)
        book.total_fees = quantize_money(book.total_fees + total_fees)
        book.trades.append(
            Trade(
                stock_id=stock_id,
                entry_date=position.entry_date,
                entry_price=position.entry_price,
                entry_reference_price=position.entry_reference_price,
                exit_date=day,
                exit_price=fill_price,
                exit_reference_price=quantize_money(reference),
                quantity=position.quantity,
                gross_pnl=gross_pnl,
                commission=total_fees,
                slippage=slippage,
                net_pnl=net_pnl,
                return_pct=quantize_percent(
                    (net_pnl / position.entry_value * _ONE_HUNDRED)
                    if position.entry_value > ZERO
                    else ZERO
                ),
                holding_period=(day - position.entry_date).days,
                exit_reason=reason,
                entry_signal_score=position.score,
                exit_signal_score=score if reason is ExitReason.SIGNAL else None,
            )
        )

    def _liquidate(self, book: "_Book") -> None:
        """Close anything still open at its last recorded close, for reporting.

        A position that is still open on the final day is marked as if it were sold there, so
        the reported equity is comparable with a run that happened to be flat. The exit is
        labelled ``END_OF_PERIOD`` so it is never mistaken for a strategy decision.
        """
        for stock_id in sorted(book.positions):
            position = book.positions[stock_id]
            self._close(
                book,
                stock_id,
                book.days[-1] if book.days else position.entry_date,
                position.last_close,
                ExitReason.END_OF_PERIOD,
                position.score,
            )
        if book.equity_curve:
            last = book.equity_curve[-1]
            book.equity_curve[-1] = PortfolioEquityPoint(
                date=last.date,
                cash=book.cash,
                market_value=ZERO,
                equity=book.cash,
                exposure_pct=ZERO,
                drawdown_pct=last.drawdown_pct,
                position_count=0,
            )

    # --- Marking ---

    def _mark_and_record(self, book: "_Book", day: date_type) -> None:
        """Mark every position to ``day`` and append one equity point."""
        market_value = ZERO
        for stock_id, position in book.positions.items():
            close = book.close_on(stock_id, day)
            if close is None:
                if not book.mark_unpriceable_positions:
                    continue
                close = position.last_close
            position.last_close = close
            market_value = quantize_money(market_value + close * Decimal(position.quantity))

        equity = quantize_money(book.cash + market_value)
        book.peak = max(book.peak, equity)
        drawdown = quantize_percent(
            (equity - book.peak) / book.peak * _ONE_HUNDRED if book.peak > ZERO else ZERO
        )
        exposure = (
            quantize_percent(market_value / equity * _ONE_HUNDRED) if equity > ZERO else None
        )
        book.equity_curve.append(
            PortfolioEquityPoint(
                date=day,
                cash=book.cash,
                market_value=market_value,
                equity=equity,
                exposure_pct=exposure,
                drawdown_pct=drawdown,
                position_count=len(book.positions),
            )
        )
        book.days.append(day)

    # --- Result ---

    def _assemble(
        self,
        book: "_Book",
        portfolio_id: int,
        risk_free_rate_pct: Decimal,
    ) -> PortfolioBacktestResult:
        """Turn the replay into a result, reusing the Phase 8 metrics engine."""
        phase8_curve = [
            EquityPoint(date=point.date, cash=point.cash, position_value=point.market_value, equity=point.equity)
            for point in book.equity_curve
        ]
        final_equity = phase8_curve[-1].equity if phase8_curve else book.cash
        metrics = build_performance_metrics(
            initial_capital=book.initial_capital,
            final_equity=final_equity,
            equity_curve=phase8_curve,
            trades=book.trades,
            risk_free_rate_pct=Decimal(risk_free_rate_pct),
            start_date=book.start_date,
            end_date=book.end_date,
        )
        exposures = [
            point.exposure_pct for point in book.equity_curve if point.exposure_pct is not None
        ]
        sizes = [trade_entry_size(trade) for trade in book.trades]
        average_size = (
            quantize_money(sum(sizes, ZERO) / Decimal(len(sizes))) if sizes else quantize_money(ZERO)
        )
        average_exposure = (
            quantize_percent(sum(exposures, ZERO) / Decimal(len(exposures)))
            if exposures
            else None
        )

        warnings: List[str] = []
        if book.rejected_signals:
            warnings.append(
                f"{book.rejected_signals} entry signal(s) were refused by the configured risk "
                "policy. Each was counted once and never retried."
            )
        if not book.trades:
            warnings.append(
                "No trade was executed in this window. That is a legitimate result: the entry "
                "conditions were never satisfied, or the policy refused every candidate."
            )

        return PortfolioBacktestResult(
            portfolio_id=portfolio_id,
            initial_capital=book.initial_capital,
            final_equity=final_equity,
            ending_cash=book.cash,
            ending_invested_capital=(
                sum(
                    (
                        position.last_close * Decimal(position.quantity)
                        for position in book.positions.values()
                    ),
                    ZERO,
                )
            ),
            total_fees=book.total_fees,
            days_processed=len(book.equity_curve),
            rejected_signals=book.rejected_signals,
            metrics=metrics,
            trades=tuple(book.trades),
            equity_curve=tuple(book.equity_curve),
            average_position_size=average_size,
            average_exposure_pct=average_exposure,
            maximum_exposure_pct=max(exposures) if exposures else None,
            policy=book.policy,
            execution_log=tuple(book.execution_log),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _stop_for(
        book: "_Book", stock: BacktestStock, fill_price: Decimal
    ) -> Optional[Decimal]:
        """The stop for an entry: the stock's absolute override, else the policy's percentage."""
        if stock.stop_price is not None:
            return quantize_price(Decimal(stock.stop_price))
        if book.policy.stop_loss_pct is None:
            return None
        return stop_from_pct(fill_price, book.policy.stop_loss_pct)


def trade_entry_size(trade: Trade) -> Decimal:
    """Capital an entry committed: quantity times the fill price, fees included."""
    return quantize_money(trade.entry_price * Decimal(trade.quantity) + trade.commission)


class _Book:
    """The mutable state of one portfolio replay. Never leaves the engine."""

    def __init__(
        self,
        *,
        stocks: Dict[int, BacktestStock],
        policy: PortfolioRiskPolicy,
        initial_capital: Decimal,
        start_date: date_type,
        end_date: date_type,
        exit_on_signal: bool,
        mark_unpriceable_positions: bool,
    ):
        self.stocks = stocks
        self.policy = policy
        self.initial_capital = quantize_money(initial_capital)
        self.start_date = start_date
        self.end_date = end_date
        self.exit_on_signal = exit_on_signal
        self.mark_unpriceable_positions = mark_unpriceable_positions

        self.cash: Decimal = self.initial_capital
        self.positions: Dict[int, _OpenPosition] = {}
        self.pending: Dict[int, _Pending] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[PortfolioEquityPoint] = []
        self.execution_log: List[tuple] = []
        self.days: List[date_type] = []
        self.peak: Decimal = self.initial_capital
        self.total_fees: Decimal = ZERO
        self.rejected_signals: int = 0

        # Bar lookup per stock, and the recorded-window dates each stock actually has.
        self._bars: Dict[int, Dict[date_type, Bar]] = {
            stock_id: {
                bar.date: bar
                for bar in stock.bars
                if start_date <= bar.date <= end_date
            }
            for stock_id, stock in stocks.items()
        }
        self._index: Dict[int, Dict[date_type, int]] = {
            stock_id: {bar.date: index for index, bar in enumerate(stock.bars)}
            for stock_id, stock in stocks.items()
        }
        self._closes: Dict[int, Dict[date_type, Decimal]] = {}
        for stock_id, stock in stocks.items():
            closes: Dict[date_type, Decimal] = {}
            for bar in stock.bars:
                if bar.close is not None and Decimal(bar.close) > ZERO:
                    closes[bar.date] = Decimal(bar.close)
            self._closes[stock_id] = closes

    # --- Calendar and lookups ---

    def calendar(self) -> List[date_type]:
        """Every recorded trading date, ascending, deduplicated across stocks.

        Built once and reused: iterating a set or a dict would give the same days in an
        unspecified order, and the whole replay is defined by its order.
        """
        dates: set = set()
        for bars in self._bars.values():
            dates.update(bars)
        return sorted(dates)

    def stock_ids_on(self, day: date_type) -> List[int]:
        """Stocks with a bar on ``day``, ascending by id. Deterministic within a day too."""
        return sorted(
            stock_id for stock_id, bars in self._bars.items() if day in bars
        )

    def bar_for(self, stock_id: int, day: date_type) -> Optional[Bar]:
        return self._bars.get(stock_id, {}).get(day)

    def context_for(self, stock_id: int, day: date_type) -> Optional[IndicatorContext]:
        """The point-in-time indicator context for that stock's bar on ``day``.

        Built by the Phase 8 builder, so it contains nothing from a later bar — the guarantee
        this module's look-ahead argument rests on.
        """
        index = self._index.get(stock_id, {}).get(day)
        if index is None:
            return None
        contexts = self.stocks[stock_id].contexts
        if index >= len(contexts):
            return None
        return contexts[index]

    def close_on(self, stock_id: int, day: date_type) -> Optional[Decimal]:
        """The close of ``stock_id`` on ``day``, or ``None`` when it has no usable close."""
        return self._closes.get(stock_id, {}).get(day)

    # --- Portfolio aggregates used for sizing ---

    def equity_at_last_close(self) -> Decimal:
        """``cash + positions marked at their most recent close``.

        Deliberately built from **last known** marks: an entry is being sized before the day is
        finished, and using a price the portfolio had not yet seen would be look-ahead.
        """
        return quantize_money(self.cash + self.invested_at_last_close())

    def invested_at_last_close(self) -> Decimal:
        return quantize_money(
            sum(
                (
                    position.last_close * Decimal(position.quantity)
                    for position in self.positions.values()
                ),
                ZERO,
            )
        )

    def total_risk_at_last_close(self) -> Decimal:
        """Sum of every open position's loss at its stop, from last-known marks."""
        total = ZERO
        for position in self.positions.values():
            if position.stop_price is None:
                continue
            per_share = position.entry_price - position.stop_price
            if per_share > ZERO:
                total = quantize_money(total + per_share * Decimal(position.quantity))
        return total

    def sector_weights(self) -> Dict[str, Decimal]:
        """Current weight per sector, for the sector cap.

        The replay carries no sector classification — that lives on the company profile, not on
        the price series — so this is empty and the sector cap simply never fires during a
        replay. That is stated rather than faked: a concentration rule evaluated against invented
        sector labels would report a weight belonging to no real sector.
        """
        return {}