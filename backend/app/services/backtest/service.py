"""Phase 8 backtest service.

This module is the only place that touches the database for backtesting. It coordinates,
in order:

1. request validation and stock lookup;
2. historical OHLCV retrieval plus data-quality validation;
3. indicator warm-up (Phase 6 math, replayed point-in-time);
4. strategy signal generation (Phase 7 ``SignalEngine``);
5. execution simulation and equity curve construction;
6. performance and risk metrics;
7. buy-and-hold benchmark;
8. persistence of the run, its trades and its equity curve.

It contains no indicator math, no HTTP concerns and no SQL.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    BacktestNotFoundError,
    StockNotFoundError,
    UnknownStrategyError,
)
from app.core.logging import logger
from app.models.backtest import Backtest, BacktestEquityPoint, BacktestTrade
from app.repositories.price_repository import price_repository
from app.repositories.stock_repository import stock_repository
from app.schemas.backtest import BacktestRequest
from app.services.backtest.benchmark import run_buy_and_hold
from app.services.backtest.indicators import PointInTimeContextBuilder
from app.services.backtest.metrics import build_performance_metrics
from app.services.backtest.models import (
    BacktestOutcome,
    Trade,
    quantize_money,
    quantize_percent,
)
from app.services.backtest.simulator import ExecutionConfig, ExecutionSimulator
from app.services.backtest.strategy import (
    STRATEGY_NAME,
    Phase7TechnicalStrategy,
    StrategyParams,
    strategy_registry,
)
from app.services.backtest.validator import load_bars, validate_bars

#: Calendar days of history fetched before ``start_date`` so indicators are already warm
#: on the first recorded bar. SMA(50) needs 50 trading days (~70 calendar days) and MACD
#: needs 26 + 9 observations, so 400 calendar days leaves generous headroom without
#: pretending that more history would help.
INDICATOR_WARMUP_CALENDAR_DAYS = 400

#: Default values persisted with every run so a backtest is reproducible from the record.
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000


class BacktestService:
    """Coordinates historical strategy validation and result persistence."""

    def __init__(
        self,
        price_repo=price_repository,
        stock_repo=stock_repository,
        context_builder: Optional[PointInTimeContextBuilder] = None,
    ):
        self.price_repo = price_repo
        self.stock_repo = stock_repo
        self.context_builder = context_builder or PointInTimeContextBuilder()

    # --- Public API ---

    def run_backtest(self, db: Session, request: BacktestRequest) -> Backtest:
        """Run a backtest and persist the result.

        Raises:
            StockNotFoundError: unknown ``stock_id``.
            UnknownStrategyError: unknown ``strategy``.
            BacktestDataValidationError: the stored OHLCV data cannot support a backtest.
        """
        stock = self.stock_repo.get_by_id(db, request.stock_id)
        if not stock:
            raise StockNotFoundError(request.stock_id)

        strategy = self._resolve_strategy(request.strategy)
        params = StrategyParams()

        bars, warmup_bars = self._load_and_validate_bars(db, request)
        execution_config = self._build_execution_config(request)

        contexts = self.context_builder.build(stock.id, stock.symbol, bars)
        simulator = ExecutionSimulator(execution_config, strategy)
        simulation = simulator.run(
            stock_id=stock.id,
            bars=bars,
            contexts=contexts,
            record_start=request.start_date,
            record_end=request.end_date,
        )

        recorded_bars = [bar for bar in bars if request.start_date <= bar.date <= request.end_date]
        outcome = self._assemble_outcome(
            stock_id=stock.id,
            symbol=stock.symbol,
            request=request,
            strategy=strategy,
            params=params,
            simulation=simulation,
            recorded_bars=recorded_bars,
            warmup_bars=warmup_bars,
        )
        return self._persist(db, outcome, request)

    def get_backtest(self, db: Session, backtest_id: int) -> Backtest:
        """Load a persisted backtest by id."""
        backtest = db.get(Backtest, backtest_id)
        if not backtest:
            raise BacktestNotFoundError(backtest_id)
        return backtest

    def get_trades(
        self, db: Session, backtest_id: int, skip: int = 0, limit: int = DEFAULT_PAGE_LIMIT
    ) -> List[BacktestTrade]:
        """Trades for a backtest, in chronological entry order, with pagination."""
        self.get_backtest(db, backtest_id)
        limit = min(max(limit, 1), MAX_PAGE_LIMIT)
        stmt = (
            select(BacktestTrade)
            .where(BacktestTrade.backtest_id == backtest_id)
            .order_by(BacktestTrade.entry_date, BacktestTrade.id)
            .offset(skip)
            .limit(limit)
        )
        return list(db.scalars(stmt).all())

    def get_equity_curve(
        self, db: Session, backtest_id: int, skip: int = 0, limit: int = DEFAULT_PAGE_LIMIT
    ) -> List[BacktestEquityPoint]:
        """Equity curve for a backtest, in date order, with pagination."""
        self.get_backtest(db, backtest_id)
        limit = min(max(limit, 1), MAX_PAGE_LIMIT)
        stmt = (
            select(BacktestEquityPoint)
            .where(BacktestEquityPoint.backtest_id == backtest_id)
            .order_by(BacktestEquityPoint.date)
            .offset(skip)
            .limit(limit)
        )
        return list(db.scalars(stmt).all())

    def count_equity_points(self, db: Session, backtest_id: int) -> int:
        """Total number of equity points stored for a backtest."""
        stmt = (
            select(func.count())
            .select_from(BacktestEquityPoint)
            .where(BacktestEquityPoint.backtest_id == backtest_id)
        )
        return int(db.scalar(stmt) or 0)

    def list_backtests(
        self,
        db: Session,
        stock_id: Optional[int] = None,
        strategy_name: Optional[str] = None,
        skip: int = 0,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> List[Backtest]:
        """Persisted backtests, newest first, optionally filtered by stock or strategy."""
        limit = min(max(limit, 1), MAX_PAGE_LIMIT)
        stmt = select(Backtest)
        if stock_id is not None:
            stmt = stmt.where(Backtest.stock_id == stock_id)
        if strategy_name is not None:
            stmt = stmt.where(Backtest.strategy_name == strategy_name)
        stmt = stmt.order_by(Backtest.id.desc()).offset(skip).limit(limit)
        return list(db.scalars(stmt).all())

    # --- Internals ---

    def _resolve_strategy(self, name: str) -> Phase7TechnicalStrategy:
        """Look up a registered strategy, raising a 422 for an unknown name."""
        strategy = strategy_registry.get(name)
        if strategy is None:
            raise UnknownStrategyError(name, strategy_registry.available)
        return strategy

    def _load_and_validate_bars(self, db: Session, request: BacktestRequest):
        """Fetch warm-up plus recorded bars and validate the whole series.

        Warm-up bars are included so that indicators are already computed on
        ``start_date``. They can never open a position because the simulator only records
        (and only acts) on bars inside the requested window.
        """
        warmup_start = request.start_date - timedelta(days=INDICATOR_WARMUP_CALENDAR_DAYS)
        rows = self.price_repo.get_history_asc(
            db, request.stock_id, start_date=warmup_start, end_date=request.end_date
        )
        bars = validate_bars(load_bars(rows)).bars
        warmup_bars = sum(1 for bar in bars if bar.date < request.start_date)

        recorded = [bar for bar in bars if request.start_date <= bar.date <= request.end_date]
        if len(recorded) < MIN_RECORDED_BARS:
            from app.services.backtest.validator import BacktestDataValidationError

            raise BacktestDataValidationError(
                f"No usable OHLCV data inside the requested window "
                f"{request.start_date}..{request.end_date}: {len(recorded)} bar(s) found",
                details={
                    "reason": "no_data_in_window",
                    "start_date": request.start_date.isoformat(),
                    "end_date": request.end_date.isoformat(),
                    "available_bars": len(recorded),
                },
            )
        return bars, warmup_bars

    @staticmethod
    def _build_execution_config(request: BacktestRequest) -> ExecutionConfig:
        """Translate the API request into simulator configuration."""
        return ExecutionConfig(
            initial_capital=Decimal(str(request.initial_capital)),
            position_size_pct=Decimal(str(request.position_size_pct)),
            commission_pct=Decimal(str(request.commission_pct)),
            slippage_pct=Decimal(str(request.slippage_pct)),
            stop_loss_pct=(
                Decimal(str(request.stop_loss_pct)) if request.stop_loss_pct is not None else None
            ),
            take_profit_pct=(
                Decimal(str(request.take_profit_pct))
                if request.take_profit_pct is not None
                else None
            ),
        )

    def _assemble_outcome(
        self,
        *,
        stock_id: int,
        symbol: str,
        request: BacktestRequest,
        strategy: Phase7TechnicalStrategy,
        params: StrategyParams,
        simulation,
        recorded_bars,
        warmup_bars: int,
    ) -> BacktestOutcome:
        """Combine simulation output with metrics and the benchmark."""
        metrics = build_performance_metrics(
            initial_capital=Decimal(str(request.initial_capital)),
            final_equity=simulation.final_equity,
            equity_curve=simulation.equity_curve,
            trades=simulation.trades,
            risk_free_rate_pct=Decimal(str(request.risk_free_rate)),
            start_date=request.start_date,
            end_date=request.end_date,
        )

        config = self._build_execution_config(request)
        benchmark = run_buy_and_hold(recorded_bars, config)

        warnings = list(simulation.warnings)
        if not simulation.trades:
            warnings.append(
                "The strategy produced no trades in this window. This is a legitimate "
                "result: the entry conditions were never all satisfied simultaneously."
            )

        return BacktestOutcome(
            stock_id=stock_id,
            symbol=symbol,
            strategy_name=strategy.strategy_name,
            strategy_version=strategy.strategy_version,
            start_date=request.start_date,
            end_date=request.end_date,
            initial_capital=Decimal(str(request.initial_capital)),
            parameters=_serializable_parameters(request, params, warmup_bars),
            trades=list(simulation.trades),
            equity_curve=list(simulation.equity_curve),
            metrics=metrics,
            benchmark=benchmark,
            warnings=warnings,
        )

    def _persist(
        self, db: Session, outcome: BacktestOutcome, request: BacktestRequest
    ) -> Backtest:
        """Write the run, its trades and its equity curve in one transaction."""
        metrics = outcome.metrics
        benchmark = outcome.benchmark

        strategy_return = metrics.total_return_pct
        benchmark_return = benchmark.benchmark_return_pct if benchmark else None
        excess = (
            quantize_percent(strategy_return - benchmark_return)
            if benchmark_return is not None
            else None
        )

        backtest = Backtest(
            stock_id=outcome.stock_id,
            strategy_name=outcome.strategy_name,
            strategy_version=outcome.strategy_version,
            start_date=outcome.start_date,
            end_date=outcome.end_date,
            initial_capital=quantize_money(outcome.initial_capital),
            final_equity=metrics.final_equity,
            total_return_pct=metrics.total_return_pct,
            cagr_pct=metrics.cagr_pct,
            peak_equity=metrics.peak_equity,
            trough_equity=metrics.trough_equity,
            max_drawdown=metrics.max_drawdown,
            max_drawdown_pct=metrics.max_drawdown_pct,
            annualized_volatility_pct=metrics.annualized_volatility_pct,
            sharpe_ratio=metrics.sharpe_ratio,
            sortino_ratio=metrics.sortino_ratio,
            win_rate_pct=metrics.win_rate_pct,
            profit_factor=metrics.profit_factor,
            number_of_trades=metrics.number_of_trades,
            winning_trades=metrics.winning_trades,
            losing_trades=metrics.losing_trades,
            average_trade_return_pct=metrics.average_trade_return_pct,
            average_winning_trade_pct=metrics.average_winning_trade_pct,
            average_losing_trade_pct=metrics.average_losing_trade_pct,
            average_holding_period=metrics.average_holding_period,
            best_trade_pct=metrics.best_trade_pct,
            worst_trade_pct=metrics.worst_trade_pct,
            benchmark_return_pct=benchmark_return,
            benchmark_final_equity=benchmark.benchmark_final_equity if benchmark else None,
            strategy_vs_benchmark_pct=excess,
            parameters_json=json.dumps(outcome.parameters, sort_keys=True, default=str),
        )
        db.add(backtest)
        db.flush()

        for trade in outcome.trades:
            db.add(_to_trade_row(backtest.id, trade))
        for point in outcome.equity_curve:
            db.add(_to_equity_row(backtest.id, point))

        db.commit()
        db.refresh(backtest)
        logger.info(
            "Persisted backtest %s: stock=%s strategy=%s trades=%d final_equity=%s",
            backtest.id,
            outcome.symbol,
            outcome.strategy_name,
            metrics.number_of_trades,
            backtest.final_equity,
        )
        return backtest


#: Minimum number of bars inside the requested window.
MIN_RECORDED_BARS = 2


def _serializable_parameters(request: BacktestRequest, params: StrategyParams, warmup_bars: int) -> dict:
    """The complete, reproducible description of a run."""
    return {
        "execution": {
            "signal_timing": "close_of_bar_n",
            "fill_timing": "open_of_bar_n_plus_1",
            "position_sizing": "fixed_pct_of_available_cash",
            "pyramiding": False,
            "short_selling": False,
        },
        "request": {
            "initial_capital": str(request.initial_capital),
            "position_size_pct": str(request.position_size_pct),
            "commission_pct": str(request.commission_pct),
            "slippage_pct": str(request.slippage_pct),
            "stop_loss_pct": None if request.stop_loss_pct is None else str(request.stop_loss_pct),
            "take_profit_pct": (
                None if request.take_profit_pct is None else str(request.take_profit_pct)
            ),
            "risk_free_rate": str(request.risk_free_rate),
        },
        "strategy": params.to_dict(),
        "data": {
            "warmup_bars": warmup_bars,
            "warmup_calendar_days": INDICATOR_WARMUP_CALENDAR_DAYS,
        },
    }


def _to_trade_row(backtest_id: int, trade: Trade) -> BacktestTrade:
    """Map a runtime trade onto its persisted representation."""
    return BacktestTrade(
        backtest_id=backtest_id,
        stock_id=trade.stock_id,
        entry_date=trade.entry_date,
        entry_price=trade.entry_price,
        exit_date=trade.exit_date,
        exit_price=trade.exit_price,
        quantity=trade.quantity,
        gross_pnl=trade.gross_pnl,
        commission=trade.commission,
        slippage=trade.slippage,
        net_pnl=trade.net_pnl,
        return_pct=trade.return_pct,
        holding_period=trade.holding_period,
        exit_reason=trade.exit_reason.value,
    )


def _to_equity_row(backtest_id: int, point) -> BacktestEquityPoint:
    """Map a runtime equity point onto its persisted representation."""
    return BacktestEquityPoint(
        backtest_id=backtest_id,
        date=point.date,
        cash=point.cash,
        position_value=point.position_value,
        equity=point.equity,
    )


backtest_service = BacktestService()