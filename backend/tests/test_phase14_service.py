"""Phase 14 service-layer tests — StrategyOptimizationService with a real DB.

These exercise the DB-facing layer: gather_strategy_performance from persisted
backtests, gather_correlation from aggregated equity curves, and the end-to-end
optimize flow when strategies are gathered from the database rather than
supplied inline.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import List

import pytest

from app.models.backtest import Backtest, BacktestEquityPoint
from app.models.stock import Stock
from app.schemas.optimization import (
    AllocationMethod,
    OptimizeRequest,
)
from app.services.backtest.service import BacktestService
from app.services.portfolio.optimization import (
    AllocationMethod as EngineAllocationMethod,
    CorrelationStatus,
    OptimizationRequest as EngineOptimizationRequest,
    StrategyOptimizer,
    StrategyPerformance,
)
from app.services.portfolio.optimization_service import StrategyOptimizationService
from tests.phase14_helpers import (
    CAPITAL,
    strategy_perf,
    optimize_request,
    create_strategy,
    persist_equity_curve,
    ANCHOR,
    ZERO,
)

_ANCHOR = ANCHOR  # 2024-01-01


# ----------------------------------------------------------------------
# Helper: build a backtest with an equity curve
# ----------------------------------------------------------------------


def _make_backtest(
    db_session,
    strategy_name: str,
    start: date,
    end: date,
    closes: List[Decimal],
    initial_capital: Decimal = Decimal("100000"),
    total_return_pct: Decimal = Decimal("10.0000"),
    max_drawdown_pct: Decimal = Decimal("5.0000"),
    annualized_volatility_pct: Decimal = Decimal("15.0000"),
    sharpe_ratio: Decimal = Decimal("1.2"),
    number_of_trades: int = 10,
) -> Backtest:
    """Insert a single backtest with an equity curve derived from *closes*."""
    symbol = strategy_name[:4].upper()
    stock = db_session.query(Stock).filter_by(symbol=symbol).first()
    if stock is None:
        stock = Stock(symbol=symbol, company_name=f"{strategy_name} Ltd", exchange="NSE", market="INDIA")
        db_session.add(stock)
        db_session.flush()

    bt = Backtest(
        stock_id=stock.id,
        strategy_name=strategy_name,
        strategy_version="1.0",
        start_date=start,
        end_date=end,
        initial_capital=initial_capital,
        final_equity=initial_capital,
        total_return_pct=total_return_pct,
        max_drawdown_pct=max_drawdown_pct,
        annualized_volatility_pct=annualized_volatility_pct,
        sharpe_ratio=sharpe_ratio,
        number_of_trades=number_of_trades,
        parameters_json="{}",
    )
    db_session.add(bt)
    db_session.flush()

    persist_equity_curve(db_session, bt.id, closes, start)
    return bt


def _linear_closes(n: int, start: Decimal = Decimal("100"), step: Decimal = Decimal("1.0")) -> List[Decimal]:
    """Generate a monotonically increasing close series for deterministic returns."""
    return [start + step * Decimal(i) for i in range(n)]


# ----------------------------------------------------------------------
# gather_strategy_performance
# ----------------------------------------------------------------------


class TestGatherStrategyPerformance:
    """gather_strategy_performance returns StrategyPerformance for each strategy."""

    def test_strategy_with_backtest_returns_nonzero_performance(self, db_session):
        closes = _linear_closes(20)
        start = _ANCHOR
        _make_backtest(
            db_session,
            "alpha",
            start=start,
            end=start + timedelta(days=20),
            closes=closes,
            number_of_trades=15,
        )

        svc = StrategyOptimizationService()
        perfs = svc.gather_strategy_performance(db_session, ["alpha"])

        assert len(perfs) == 1
        perf = perfs[0]
        assert perf.strategy_id == "alpha"
        assert perf.number_of_trades == 15
        assert perf.total_return_pct != ZERO
        assert perf.annualized_volatility_pct is not None

    def test_strategy_without_backtest_returns_zeroed_performance(self, db_session):
        svc = StrategyOptimizationService()
        perfs = svc.gather_strategy_performance(db_session, ["nonexistent_strategy"])

        assert len(perfs) == 1
        perf = perfs[0]
        assert perf.strategy_id == "nonexistent_strategy"
        assert perf.number_of_trades == 0
        assert perf.total_return_pct == ZERO
        assert perf.annualized_volatility_pct is None
        assert perf.max_drawdown_pct == ZERO

    def test_results_sorted_by_strategy_id(self, db_session):
        closes = _linear_closes(10)
        start = _ANCHOR
        for name in ["zeta", "alpha", "mid"]:
            _make_backtest(
                db_session, name, start=start, end=start + timedelta(days=10), closes=closes
            )

        svc = StrategyOptimizationService()
        perfs = svc.gather_strategy_performance(db_session)

        ids = [p.strategy_id for p in perfs]
        assert ids == sorted(ids)

    def test_date_filter_excludes_out_of_window_backtests(self, db_session):
        closes = _linear_closes(10)
        early_start = date(2023, 1, 1)
        early_end = date(2023, 1, 10)
        _make_backtest(
            db_session, "alpha",
            start=early_start, end=early_end, closes=closes,
            number_of_trades=5,
        )

        svc = StrategyOptimizationService()
        # Window that excludes the early backtest
        perfs = svc.gather_strategy_performance(
            db_session, ["alpha"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 1),
        )
        assert len(perfs) == 1
        assert perfs[0].number_of_trades == 0

    def test_date_filter_includes_overlapping_backtests(self, db_session):
        closes = _linear_closes(10)
        start = date(2024, 3, 1)
        end = date(2024, 3, 10)
        _make_backtest(
            db_session, "alpha",
            start=start, end=end, closes=closes,
            number_of_trades=5,
        )

        svc = StrategyOptimizationService()
        perfs = svc.gather_strategy_performance(
            db_session, ["alpha"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 1),
        )
        assert perfs[0].number_of_trades == 5

    def test_multiple_backtests_aggregated_for_one_strategy(self, db_session):
        closes1 = _linear_closes(10)
        closes2 = _linear_closes(10, start=Decimal("110"), step=Decimal("2.0"))
        start1 = _ANCHOR
        end1 = start1 + timedelta(days=10)
        start2 = _ANCHOR + timedelta(days=11)
        end2 = start2 + timedelta(days=10)

        _make_backtest(db_session, "alpha", start=start1, end=end1, closes=closes1, number_of_trades=5)
        _make_backtest(db_session, "alpha", start=start2, end=end2, closes=closes2, number_of_trades=5)

        svc = StrategyOptimizationService()
        perfs = svc.gather_strategy_performance(db_session, ["alpha"])

        assert len(perfs) == 1
        assert perfs[0].number_of_trades == 10

    def test_no_strategy_names_gathers_all_registered(self, db_session):
        closes = _linear_closes(5)
        start = _ANCHOR
        _make_backtest(
            db_session, "phase7_technical",
            start=start, end=start + timedelta(days=5), closes=closes,
            number_of_trades=8,
        )

        svc = StrategyOptimizationService()
        perfs = svc.gather_strategy_performance(db_session)

        # Only registered strategies are gathered
        assert any(p.strategy_id == "phase7_technical" for p in perfs)
        pt = next(p for p in perfs if p.strategy_id == "phase7_technical")
        assert pt.number_of_trades == 8


# ----------------------------------------------------------------------
# gather_correlation
# ----------------------------------------------------------------------


class TestGatherCorrelation:
    """gather_correlation builds a CorrelationMatrix from aggregated returns."""

    def test_single_strategy_returns_single_strategy_status(self, db_session):
        closes = _linear_closes(10)
        start = _ANCHOR
        _make_backtest(db_session, "alpha", start=start, end=start + timedelta(days=10), closes=closes)

        svc = StrategyOptimizationService()
        corr = svc.gather_correlation(db_session, ["alpha"])

        assert corr.status == CorrelationStatus.SINGLE_STRATEGY

    def test_two_strategies_with_overlapping_returns(self, db_session):
        closes_a = [Decimal("100"), Decimal("101"), Decimal("102"), Decimal("103"), Decimal("104")]
        closes_b = [Decimal("100"), Decimal("101.5"), Decimal("103"), Decimal("104.5"), Decimal("106")]
        start = _ANCHOR
        end = start + timedelta(days=4)
        _make_backtest(db_session, "alpha", start=start, end=end, closes=closes_a, number_of_trades=10)
        _make_backtest(db_session, "beta", start=start, end=end, closes=closes_b, number_of_trades=10)

        svc = StrategyOptimizationService()
        corr = svc.gather_correlation(db_session, ["alpha", "beta"])

        assert corr.status == CorrelationStatus.SUFFICIENT
        assert corr.observation_count >= 2
        # Should have at least one matrix entry
        assert len(corr.matrix) >= 1

    def test_short_series_insufficient_overlap(self, db_session):
        """When one strategy has fewer than min_observations returns, the pair is insufficient."""
        closes_a = [Decimal("100"), Decimal("101"), Decimal("102"), Decimal("103")]
        closes_b = [Decimal("100")]  # 1 close → 2 equity points → 1 return
        start = _ANCHOR
        end = start + timedelta(days=3)
        _make_backtest(db_session, "alpha", start=start, end=end, closes=closes_a, number_of_trades=10)
        _make_backtest(db_session, "beta", start=start, end=end, closes=closes_b, number_of_trades=10)

        svc = StrategyOptimizationService()
        corr = svc.gather_correlation(db_session, ["alpha", "beta"])

        # alpha has 4 returns, beta has 1 return → overlap = min(4, 1) = 1 < 2
        assert corr.status == CorrelationStatus.INSUFFICIENT_DATA
        assert ("alpha", "beta") in corr.insufficient_pairs

    def test_strategy_with_no_backtest_handled(self, db_session):
        closes = _linear_closes(5)
        start = _ANCHOR
        _make_backtest(
            db_session, "alpha", start=start, end=start + timedelta(days=4), closes=closes,
            number_of_trades=10,
        )

        svc = StrategyOptimizationService()
        corr = svc.gather_correlation(db_session, ["alpha", "nonexistent"])

        # 'alpha' has data, 'nonexistent' has none
        assert corr.status in (CorrelationStatus.INSUFFICIENT_DATA, CorrelationStatus.SINGLE_STRATEGY)


class TestOptimizeFromDatabase:
    """End-to-end optimize when strategies are gathered from backtests."""

    def test_optimize_without_strategies_uses_db_gathered(self, db_session):
        closes_a = [Decimal("100"), Decimal("101"), Decimal("102"), Decimal("103"), Decimal("104")]
        closes_b = [Decimal("100"), Decimal("102"), Decimal("104"), Decimal("106"), Decimal("108")]
        start = _ANCHOR
        end = start + timedelta(days=4)
        _make_backtest(db_session, "phase7_technical", start=start, end=end, closes=closes_a, number_of_trades=10)
        _make_backtest(db_session, "custom_strat", start=start, end=end, closes=closes_b, number_of_trades=12)

        svc = StrategyOptimizationService()

        # Override strategy_names to include custom_strat
        original = svc.gather_strategy_performance
        def _gather(db, names=None, **kwargs):
            return original(db, ["phase7_technical", "custom_strat"], **{k: v for k, v in kwargs.items() if k != "strategy_names"})

        svc.gather_strategy_performance = _gather

        from app.schemas.optimization import OptimizeRequest as OR
        from datetime import date as date_type

        request = OR(
            total_capital=Decimal("1000000"),
            method=AllocationMethod.EQUAL_WEIGHT,
            strategies=None,
        )

        result = svc.optimize(db_session, request)

        assert len(result.assignments) == 2
        weights = [a.weight_pct for a in result.assignments]
        assert sum(weights) == Decimal("100")

    def test_optimize_with_explicit_strategies_skips_db_gather(self, db_session):
        svc = StrategyOptimizationService()
        s1 = strategy_perf("a", number_of_trades=10)
        s2 = strategy_perf("b", number_of_trades=10)

        body = optimize_request(strategies=[s1, s2], method=AllocationMethod.EQUAL_WEIGHT)
        request = OptimizeRequest(**body)

        # Spy on gather_strategy_performance — it should NOT be called
        called = []
        original = svc.gather_strategy_performance
        def _spy(*args, **kwargs):
            called.append(True)
            return []
        svc.gather_strategy_performance = _spy

        result = svc.optimize(db_session, request)
        assert len(called) == 0
        assert len(result.assignments) == 2

    def test_optimize_gathers_correlation_for_explicit_strategies(self, db_session):
        s1 = strategy_perf("alpha", number_of_trades=10)
        s2 = strategy_perf("beta", number_of_trades=10)

        body = optimize_request(strategies=[s1, s2], method=AllocationMethod.EQUAL_WEIGHT)
        request = OptimizeRequest(**body)

        svc = StrategyOptimizationService()
        result = svc.optimize(db_session, request)

        # Correlation is always gathered when > 1 strategy
        assert result.correlation_status == CorrelationStatus.INSUFFICIENT_DATA
