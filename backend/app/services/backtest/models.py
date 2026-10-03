"""Internal, engine-facing domain models for the Phase 8 backtesting engine.

These dataclasses are deliberately separate from the SQLAlchemy models and the Pydantic
response schemas:

* :class:`Bar` is the normalised OHLCV bar the engine consumes.
* :class:`Position` / :class:`Trade` / :class:`EquityPoint` are normalised runtime
  representations used by the simulator.
* :class:`PerformanceMetrics` and :class:`BenchmarkResult` are pure value objects
  produced by the metrics and benchmark engines.

Every monetary value is a :class:`decimal.Decimal` so results are exactly reproducible
across runs. Rounding happens once, explicitly, via :func:`quantize_money` /
:func:`quantize_percent`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Dict, List, Optional

#: Scale used for monetary amounts (cash, equity, P&L).
MONEY_SCALE = Decimal("0.01")
#: Scale used for percentage values.
PERCENT_SCALE = Decimal("0.0001")
#: Scale used for ratios (Sharpe, Sortino, profit factor).
RATIO_SCALE = Decimal("0.0001")
#: Scale used for plain counts/durations such as the average holding period.
COUNT_SCALE = Decimal("0.01")

MONEY_QUANT = Decimal("0.01")
PERCENT_QUANT = Decimal("0.0001")
RATIO_QUANT = Decimal("0.0001")
COUNT_QUANT = Decimal("0.01")

#: Absolute zero used for sign comparisons of money amounts.
ZERO = Decimal("0")


def quantize_money(value: Decimal) -> Decimal:
    """Round a monetary amount to 2 decimal places using banker-free half-up rounding."""
    return Decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def quantize_percent(value: Decimal) -> Decimal:
    """Round a percentage to 4 decimal places."""
    return Decimal(value).quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)


def quantize_ratio(value: Decimal) -> Decimal:
    """Round a ratio (Sharpe/Sortino/profit factor) to 4 decimal places."""
    return Decimal(value).quantize(RATIO_QUANT, rounding=ROUND_HALF_UP)


def quantize_count(value: Decimal) -> Decimal:
    """Round a plain count or duration (e.g. average holding period) to 2 decimals."""
    return Decimal(value).quantize(COUNT_QUANT, rounding=ROUND_HALF_UP)


class ExitReason(str, Enum):
    """Why a position was closed.

    The value is persisted and returned by the API so a consumer can always explain how
    a trade ended. ``END_OF_PERIOD`` marks a position that was still open on the final
    bar of the backtest window and was therefore liquidated for reporting purposes.
    """

    SIGNAL = "signal"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    END_OF_PERIOD = "end_of_period"


class PositionState(str, Enum):
    """Lifecycle state of the simulated position book."""

    FLAT = "flat"
    LONG = "long"


@dataclass(frozen=True)
class Bar:
    """A single normalised OHLCV bar.

    ``date`` is the trading date. Price fields are ``Optional`` only so that the data
    quality validator can distinguish "missing" from "zero"; the simulator only ever sees
    bars whose four price fields are present and strictly positive.
    """

    date: date
    open: Optional[Decimal]
    high: Optional[Decimal]
    low: Optional[Decimal]
    close: Optional[Decimal]
    volume: Optional[int]

    @property
    def has_full_ohlcv(self) -> bool:
        """True when all four price fields are present."""
        return None not in (self.open, self.high, self.low, self.close)


@dataclass
class Position:
    """The single open long position, if any.

    Phase 8 is long-only: one position per stock, no pyramiding, no shorting. The engine
    therefore only ever transitions between :attr:`PositionState.FLAT` and
    :attr:`PositionState.LONG`.
    """

    stock_id: int
    quantity: int
    entry_date: date
    entry_price: Decimal
    entry_reference_price: Decimal
    entry_value: Decimal
    entry_commission: Decimal
    entry_slippage: Decimal
    entry_cash_out: Decimal
    entry_signal_score: int

    @property
    def state(self) -> PositionState:
        """The current position state (always ``LONG`` while the object exists)."""
        return PositionState.LONG

    def market_value(self, close: Decimal) -> Decimal:
        """Market value of the position at the given closing price."""
        return quantize_money(self.quantity * close)

    def unrealized_pnl(self, close: Decimal) -> Decimal:
        """Unrealised P&L of the position at the given closing price, net of entry costs."""
        gross = (close - self.entry_reference_price) * Decimal(self.quantity)
        return quantize_money(gross - self.entry_commission - self.entry_slippage)


@dataclass
class Trade:
    """A completed round-trip trade (entry fill and exit fill)."""

    stock_id: int
    entry_date: date
    entry_price: Decimal
    entry_reference_price: Decimal
    exit_date: date
    exit_price: Decimal
    exit_reference_price: Decimal
    quantity: int
    gross_pnl: Decimal
    commission: Decimal
    slippage: Decimal
    net_pnl: Decimal
    return_pct: Decimal
    holding_period: int
    exit_reason: ExitReason
    entry_signal_score: int
    exit_signal_score: Optional[int] = None


@dataclass
class EquityPoint:
    """One row of the equity curve."""

    date: date
    cash: Decimal
    position_value: Decimal
    equity: Decimal


@dataclass
class PerformanceMetrics:
    """Return, risk and trade statistics for one backtest.

    Every optional field is ``None`` when the underlying sample is too small or a
    denominator is zero. A value is never fabricated and never silently coerced to zero.
    """

    initial_capital: Decimal
    final_equity: Decimal
    total_return_pct: Decimal
    cagr_pct: Optional[Decimal]
    peak_equity: Decimal
    trough_equity: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    win_rate_pct: Optional[Decimal]
    profit_factor: Optional[Decimal]
    number_of_trades: int
    winning_trades: int
    losing_trades: int
    average_trade_return_pct: Optional[Decimal]
    average_winning_trade_pct: Optional[Decimal]
    average_losing_trade_pct: Optional[Decimal]
    average_holding_period: Optional[Decimal]
    best_trade_pct: Optional[Decimal]
    worst_trade_pct: Optional[Decimal]
    annualized_volatility_pct: Optional[Decimal]
    sharpe_ratio: Optional[Decimal]
    sortino_ratio: Optional[Decimal]


@dataclass
class BenchmarkResult:
    """Buy-and-hold baseline over the same window and with the same starting capital."""

    initial_capital: Decimal
    final_equity: Decimal
    benchmark_return_pct: Decimal
    benchmark_final_equity: Decimal


@dataclass
class BacktestOutcome:
    """Everything the simulator produced for one run.

    ``parameters`` is the fully resolved, deterministic configuration of the run. It is
    persisted verbatim so a backtest can be reproduced later from
    ``strategy_name + strategy_version + parameters + data period``.
    """

    stock_id: int
    symbol: str
    strategy_name: str
    strategy_version: str
    start_date: date
    end_date: date
    initial_capital: Decimal
    parameters: Dict[str, object]
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[EquityPoint] = field(default_factory=list)
    metrics: Optional[PerformanceMetrics] = None
    benchmark: Optional[BenchmarkResult] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def final_equity(self) -> Decimal:
        """Equity on the final bar of the recorded window."""
        if self.equity_curve:
            return self.equity_curve[-1].equity
        return self.initial_capital