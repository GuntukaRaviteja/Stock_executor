"""Phase 14 orchestration — the DB-facing layer for strategy capital allocation.

This module is the **only** Phase 14 code that touches the database. It gathers historical
backtest data for each strategy, computes per-strategy performance and correlation, hands
those values to the pure engine in :mod:`app.services.portfolio.optimization`, and returns
the result as a :class:`~app.services.portfolio.optimization.OptimizationResult`.

It contains no allocation arithmetic of its own. That split is deliberate: the arithmetic
is unit-testable without a fixture, and this layer stays a thin translation between stored
backtests and engine inputs.

Phase 14 does **not** re-implement Phase 13. When capital is assigned to a strategy, the
caller (typically the API endpoint) uses Phase 13's
:class:`~app.services.portfolio.allocation_service.PortfolioAllocationService` to turn that
capital into a sized, risk-checked portfolio. This service only decides **how much** each
strategy gets and **why**.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.backtest import Backtest
from app.services.backtest.metrics import (
    annualized_volatility_pct,
    max_drawdown,
    sharpe_ratio,
)
from app.services.backtest.models import EquityPoint
from app.services.backtest.service import BacktestService, backtest_service
from app.services.backtest.strategy import strategy_registry
from app.services.portfolio.optimization import (
    AllocationMethod as EngineAllocationMethod,
    CorrelationMatrix,
    OptimizationRequest as EngineOptimizationRequest,
    OptimizationResult,
    StrategyOptimizer,
    StrategyPerformance,
    compute_correlation_matrix,
)
from app.schemas.optimization import (
    AllocationMethod,
    ClampedWeightSchema,
    CorrelationEntrySchema,
    CorrelationMatrixSchema,
    CorrelationStatus as SchemaCorrelationStatus,
    OptimizeRequest,
    OptimizationResultSchema,
    RedistributionEntrySchema,
    StrategyAssignmentSchema,
    StrategyPerformanceSchema,
)

#: Zero constant reused throughout this module.
ZERO = Decimal("0")

#: Minimum number of trades a strategy must have to be considered eligible.
_MIN_TRADES = 2


class StrategyOptimizationService:
    """Resolves strategy performance from historical backtests and delegates to the pure engine."""

    def __init__(
        self,
        backtest_svc: Optional[BacktestService] = None,
        optimizer: Optional[StrategyOptimizer] = None,
    ):
        self.backtest_service = backtest_svc or backtest_service
        self.optimizer = optimizer or StrategyOptimizer()

    # ------------------------------------------------------------------
    # Performance gathering
    # ------------------------------------------------------------------

    def gather_strategy_performance(
        self,
        db: Session,
        strategy_names: Optional[Sequence[str]] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[StrategyPerformance]:
        """Gather performance for each strategy from its persisted backtests.

        Backtests are filtered to ``[start_date, end_date]`` when provided. For each strategy
        the equity curves of all matching backtests are aligned by date and aggregated into a
        single daily return series, from which total return, Sharpe, volatility, drawdown and
        trade counts are computed.

        Returns one :class:`StrategyPerformance` per strategy, sorted by ``strategy_id``.
        """
        if strategy_names is None:
            strategy_names = list(strategy_registry.available)

        performances: List[StrategyPerformance] = []

        for name in sorted(strategy_names):
            backtests = self._fetch_backtests(db, name, start_date, end_date)
            if not backtests:
                logger.warning(
                    "StrategyOptimizationService: strategy '%s' has no backtests in the "
                    "requested window",
                    name,
                )
                performances.append(
                    StrategyPerformance(
                        strategy_id=name,
                        strategy_name=name,
                        total_return_pct=ZERO,
                        sharpe_ratio=None,
                        max_drawdown_pct=ZERO,
                        annualized_volatility_pct=None,
                        number_of_trades=0,
                    )
                )
                continue

            returns, equity_curve, trades = self._aggregate_equity_curve(db, backtests)
            perf = self._compute_performance(name, backtests, returns, equity_curve, trades)
            performances.append(perf)

        return performances

    def _fetch_backtests(
        self,
        db: Session,
        strategy_name: str,
        start_date: Optional[date],
        end_date: Optional[date],
    ) -> List[Backtest]:
        """Fetch all persisted backtests for one strategy, optionally windowed.

        Uses pagination to pull every matching row so the aggregation sees the full set.
        """
        from app.services.backtest.service import MAX_PAGE_LIMIT

        results: List[Backtest] = []
        skip = 0
        while True:
            batch = self.backtest_service.list_backtests(
                db,
                strategy_name=strategy_name,
                skip=skip,
                limit=MAX_PAGE_LIMIT,
            )
            if start_date is not None:
                batch = [
                    b for b in batch
                    if b.end_date is not None and b.end_date >= start_date
                ]
            if end_date is not None:
                batch = [
                    b for b in batch
                    if b.start_date is not None and b.start_date <= end_date
                ]
            results.extend(batch)
            if len(batch) < MAX_PAGE_LIMIT:
                break
            skip += MAX_PAGE_LIMIT
        return results

    def _aggregate_equity_curve(
        self,
        db: Session,
        backtests: Sequence[Backtest],
    ) -> Tuple[List[Decimal], List[Tuple[date, Decimal]], int]:
        """Aggregate equity curves from multiple backtests into one daily return series.

        For each date present in any backtest's equity curve, the daily return is the average
        of the daily returns of all backtests that have a return on that date. The aggregated
        equity curve is the cumulative product of (1 + average daily return), seeded at 1.0.

        Returns ``(daily_returns, equity_curve_points, total_trades)``.
        """
        date_to_returns: Dict[date, List[Decimal]] = defaultdict(list)
        total_trades = 0

        for backtest in backtests:
            points = self.backtest_service.get_equity_curve(db, backtest.id)
            if len(points) < 2:
                continue

            for prev, curr in zip(points, points[1:]):
                if prev.equity <= 0:
                    continue
                ret = (curr.equity - prev.equity) / prev.equity
                date_to_returns[curr.date].append(ret)

            total_trades += backtest.number_of_trades

        sorted_dates = sorted(date_to_returns.keys())
        daily_returns: List[Decimal] = []
        equity: Decimal = Decimal("1")
        equity_curve: List[Tuple[date, Decimal]] = []

        for d in sorted_dates:
            rets = date_to_returns[d]
            avg_ret = sum(rets, ZERO) / Decimal(len(rets))
            daily_returns.append(avg_ret)
            equity = equity * (Decimal("1") + avg_ret)
            equity_curve.append((d, equity.quantize(Decimal("0.0001"))))

        return daily_returns, equity_curve, total_trades

    def _compute_performance(
        self,
        strategy_name: str,
        backtests: Sequence[Backtest],
        daily_returns: List[Decimal],
        equity_curve: List[Tuple[date, Decimal]],
        total_trades: int,
    ) -> StrategyPerformance:
        """Compute a :class:`StrategyPerformance` from aggregated returns and equity curve."""
        window_start = backtests[0].start_date if backtests else None
        window_end = backtests[0].end_date if backtests else None
        if backtests:
            for bt in backtests[1:]:
                if bt.start_date and (window_start is None or bt.start_date < window_start):
                    window_start = bt.start_date
                if bt.end_date and (window_end is None or bt.end_date > window_end):
                    window_end = bt.end_date

        if equity_curve:
            initial = Decimal("1")
            final = equity_curve[-1][1]
            total_ret = ((final / initial - Decimal("1")) * Decimal("100")).quantize(Decimal("0.0001"))
        else:
            returns_list = [bt.total_return_pct for bt in backtests if bt.total_return_pct is not None]
            total_ret = sum(returns_list, ZERO) / Decimal(len(returns_list)) if returns_list else ZERO

        sharpe = sharpe_ratio(daily_returns, ZERO)
        vol = annualized_volatility_pct(daily_returns)

        fake_points = [
            EquityPoint(date=d, cash=e, position_value=ZERO, equity=e)
            for d, e in equity_curve
        ]
        if fake_points:
            _, _, _, drawdown_pct = max_drawdown(fake_points)
        else:
            drawdown_pct = ZERO

        return StrategyPerformance(
            strategy_id=strategy_name,
            strategy_name=strategy_name,
            total_return_pct=total_ret,
            sharpe_ratio=sharpe,
            max_drawdown_pct=drawdown_pct,
            annualized_volatility_pct=vol,
            number_of_trades=total_trades,
            window_start=window_start,
            window_end=window_end,
        )

    # ------------------------------------------------------------------
    # Correlation gathering
    # ------------------------------------------------------------------

    def gather_correlation(
        self,
        db: Session,
        strategy_names: Sequence[str],
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> CorrelationMatrix:
        """Build a correlation matrix from per-strategy aggregated daily returns.

        Each strategy's equity curve is aggregated across all its backtests (same as
        :meth:`gather_strategy_performance`), and the overlapping tail of the daily return
        series for each pair is correlated.
        """
        strategy_returns: Dict[str, List[Decimal]] = {}

        for name in sorted(strategy_names):
            backtests = self._fetch_backtests(db, name, start_date, end_date)
            if backtests:
                returns, _, _ = self._aggregate_equity_curve(db, backtests)
                strategy_returns[name] = returns
            else:
                strategy_returns[name] = []

        return compute_correlation_matrix(strategy_returns, min_observations=2)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def optimize(
        self,
        db: Session,
        request: OptimizeRequest,
    ) -> OptimizationResult:
        """Run one strategy-level optimization, gathering performance from backtests when needed."""
        strategies: List[StrategyPerformance]

        if request.strategies is not None:
            strategies = [
                StrategyPerformance(
                    strategy_id=s.strategy_id,
                    strategy_name=s.strategy_name,
                    total_return_pct=s.total_return_pct,
                    sharpe_ratio=s.sharpe_ratio,
                    max_drawdown_pct=s.max_drawdown_pct,
                    annualized_volatility_pct=s.annualized_volatility_pct,
                    number_of_trades=s.number_of_trades,
                    win_rate_pct=s.win_rate_pct,
                    profit_factor=s.profit_factor,
                    window_start=s.window_start,
                    window_end=s.window_end,
                )
                for s in request.strategies
            ]
        else:
            strategies = self.gather_strategy_performance(db, None)

        user_weights: Optional[Dict[str, Decimal]] = None
        if request.user_weights is not None:
            user_weights = {w.strategy_id: w.weight_pct for w in request.user_weights}

        strategy_ids = [s.strategy_id for s in strategies]
        correlation: Optional[CorrelationMatrix] = None
        if len(strategy_ids) > 1:
            correlation = self.gather_correlation(db, strategy_ids)

        engine_request = EngineOptimizationRequest(
            total_capital=Decimal(str(request.total_capital)),
            method=EngineAllocationMethod(request.method.value),
            strategies=strategies,
            user_weights=user_weights,
            min_weight_pct=Decimal(str(request.min_weight_pct)),
            max_weight_pct=Decimal(str(request.max_weight_pct)),
            correlation=correlation,
            risk_free_rate_pct=Decimal(str(request.risk_free_rate_pct)),
        )

        return self.optimizer.optimize(engine_request)

    def optimize_to_schema(
        self,
        db: Session,
        request: OptimizeRequest,
    ) -> OptimizationResultSchema:
        """Run optimization and convert the result to its response schema."""
        result = self.optimize(db, request)
        return _result_to_schema(result)


#: Module-level service singleton, matching the pattern of every other service in this package.
strategy_optimization_service = StrategyOptimizationService()


def _result_to_schema(result: OptimizationResult) -> OptimizationResultSchema:
    """Convert an :class:`OptimizationResult` to :class:`OptimizationResultSchema`."""
    assignments = [
        StrategyAssignmentSchema(
            strategy_id=a.strategy_id,
            strategy_name=a.strategy_name,
            weight_pct=a.weight_pct,
            capital=a.capital,
            risk_budget=a.risk_budget,
            eligibility=a.eligibility,
            reasons=list(a.reasons),
        )
        for a in result.assignments
    ]

    corr_matrix_schema: Optional[CorrelationMatrixSchema] = None
    if result.correlation is not None:
        corr_matrix_schema = CorrelationMatrixSchema(
            status=SchemaCorrelationStatus(result.correlation.status.value),
            observation_count=result.correlation.observation_count,
            min_observations=result.correlation.min_observations,
            matrix=[
                CorrelationEntrySchema(strategy_a=a, strategy_b=b, correlation=corr)
                for (a, b), corr in result.correlation.matrix.items()
                if a != b
            ],
            insufficient_pairs=[
                [a, b] for (a, b) in result.correlation.insufficient_pairs
            ],
        )

    return OptimizationResultSchema(
        total_capital=result.total_capital,
        method=AllocationMethod(result.method.value),
        correlation_status=SchemaCorrelationStatus(result.correlation_status.value),
        correlation=corr_matrix_schema,
        assignments=assignments,
        clamped_weights=[
            ClampedWeightSchema(strategy_id=sid, raw=raw, clamped=clamped)
            for sid, raw, clamped in result.clamped_weights
        ],
        redistribution=[
            RedistributionEntrySchema(strategy_id=sid, from_weight=old, to_weight=new)
            for sid, old, new in result.redistribution
        ],
        effective_min_weight_pct=result.effective_min_weight_pct,
        effective_max_weight_pct=result.effective_max_weight_pct,
        warnings=list(result.warnings),
    )