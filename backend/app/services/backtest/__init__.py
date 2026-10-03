"""Phase 8 strategy backtesting and signal validation.

Historical validation only. Nothing in this package predicts future prices, places live
orders, or claims that a backtest result will be repeated.

Flow::

    OHLCV
      -> data quality validation
      -> point-in-time indicator contexts (Phase 6 math)
      -> Phase 7 signal engine
      -> strategy rules (phase7_technical)
      -> execution simulator (fills, costs, sizing)
      -> trades + equity curve
      -> performance / risk metrics + buy-and-hold benchmark
      -> persistence
"""

from app.services.backtest.benchmark import run_buy_and_hold
from app.services.backtest.indicators import PointInTimeContextBuilder
from app.services.backtest.models import (
    BacktestOutcome,
    Bar,
    BenchmarkResult,
    EquityPoint,
    ExitReason,
    PerformanceMetrics,
    Position,
    Trade,
)
from app.services.backtest.service import BacktestService, backtest_service
from app.services.backtest.simulator import ExecutionConfig, ExecutionSimulator
from app.services.backtest.strategy import (
    STRATEGY_NAME,
    STRATEGY_VERSION,
    Phase7TechnicalStrategy,
    StrategyParams,
    strategy_registry,
)
from app.services.backtest.validator import (
    BacktestDataValidationError,
    load_bars,
    validate_bars,
)

__all__ = [
    "BacktestService",
    "backtest_service",
    "BacktestDataValidationError",
    "Bar",
    "Trade",
    "Position",
    "EquityPoint",
    "ExitReason",
    "PerformanceMetrics",
    "BenchmarkResult",
    "BacktestOutcome",
    "ExecutionConfig",
    "ExecutionSimulator",
    "Phase7TechnicalStrategy",
    "StrategyParams",
    "strategy_registry",
    "STRATEGY_NAME",
    "STRATEGY_VERSION",
    "PointInTimeContextBuilder",
    "run_buy_and_hold",
    "load_bars",
    "validate_bars",
]