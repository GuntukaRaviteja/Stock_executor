"""Pydantic schemas for the Phase 8 backtesting engine.

These schemas describe the request/response structures for **historical** strategy
validation. A backtest result is a measurement of past behaviour only: it is not a
prediction, and past performance does not guarantee future performance.

Metrics that cannot be meaningfully computed from the available sample are returned as
``null`` rather than being replaced with a fabricated ``0``.
"""

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BacktestRequest(BaseModel):
    """Request to run a historical backtest."""

    stock_id: int = Field(..., ge=1, description="Internal stock ID")
    strategy: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Strategy identifier, e.g. 'phase7_technical'",
    )
    start_date: date_type = Field(..., description="Backtest start date (inclusive)")
    end_date: date_type = Field(..., description="Backtest end date (inclusive)")
    initial_capital: Decimal = Field(..., gt=0, description="Starting capital for the backtest")
    position_size_pct: Decimal = Field(
        default=Decimal("100"),
        gt=0,
        le=100,
        description="Position size as a percentage of available cash (0 < pct <= 100)",
    )
    commission_pct: Decimal = Field(
        default=Decimal("0"), ge=0, le=10, description="Commission percentage per side (0-10)"
    )
    slippage_pct: Decimal = Field(
        default=Decimal("0"), ge=0, le=10, description="Slippage percentage per side (0-10)"
    )
    stop_loss_pct: Optional[Decimal] = Field(
        default=None, gt=0, le=100, description="Stop-loss distance from entry, in percent"
    )
    take_profit_pct: Optional[Decimal] = Field(
        default=None, gt=0, le=1000, description="Take-profit distance from entry, in percent"
    )
    risk_free_rate: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=20,
        description="Annual risk-free rate in percent used by Sharpe/Sortino (defaults to 0)",
    )

    @model_validator(mode="after")
    def validate_date_range(self) -> "BacktestRequest":
        """Reject a window that does not move forward in time."""
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class BacktestTradeResponse(BaseModel):
    """A single completed round-trip trade."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Persisted trade id")
    stock_id: int = Field(..., description="Internal stock ID")
    entry_date: date_type = Field(..., description="Entry fill date (the bar open used)")
    entry_price: Decimal = Field(..., description="Entry fill price including slippage")
    exit_date: date_type = Field(..., description="Exit fill date")
    exit_price: Decimal = Field(..., description="Exit fill price including slippage")
    quantity: int = Field(..., description="Number of shares traded")
    gross_pnl: Decimal = Field(..., description="Price-move P&L before any costs")
    commission: Decimal = Field(..., description="Total commission paid (entry + exit)")
    slippage: Decimal = Field(..., description="Total slippage cost (entry + exit)")
    net_pnl: Decimal = Field(..., description="gross_pnl - commission - slippage")
    return_pct: Decimal = Field(..., description="net_pnl as a percentage of entry notional")
    holding_period: int = Field(..., description="Calendar days held")
    exit_reason: str = Field(
        ...,
        description="Why the position closed: signal, stop_loss, take_profit or end_of_period",
    )


class BacktestEquityPointResponse(BaseModel):
    """One row of the equity curve, marked at that bar's close."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Persisted equity point id")
    date: date_type = Field(..., description="Trading date of the bar")
    cash: Decimal = Field(..., description="Cash balance at the bar close")
    position_value: Decimal = Field(
        ..., description="Market value of any open position at the bar close"
    )
    equity: Decimal = Field(..., description="cash + position_value at the bar close")


class BacktestResponse(BaseModel):
    """Complete result of one backtest run.

    ``null`` on any metric means the sample was too small or a denominator was zero.
    Those values are never replaced with a fabricated number.
    """

    model_config = ConfigDict(from_attributes=True)

    backtest_id: int = Field(..., description="Backtest id")
    stock_id: int = Field(..., description="Internal stock ID")
    symbol: str = Field(..., description="Stock symbol")
    strategy: str = Field(..., description="Strategy name used for the run")
    strategy_version: str = Field(..., description="Strategy rule-set version")
    description: str = Field(..., description="Human-readable description of the strategy")

    start_date: date_type = Field(..., description="Requested window start (inclusive)")
    end_date: date_type = Field(..., description="Requested window end (inclusive)")
    actual_start_date: date_type = Field(..., description="First trading date actually simulated")
    actual_end_date: date_type = Field(..., description="Last trading date actually simulated")
    bars: int = Field(..., description="Number of trading days simulated")

    initial_capital: Decimal = Field(..., description="Starting capital")
    final_equity: Decimal = Field(..., description="Equity at the end of the window")

    total_return_pct: Decimal = Field(..., description="(final/initial - 1) * 100")
    cagr_pct: Optional[Decimal] = Field(
        None, description="Annualised return; null when the window is shorter than 30 days"
    )
    peak_equity: Decimal = Field(..., description="Highest equity observed")
    trough_equity: Decimal = Field(..., description="Equity at the end of the drawdown window")
    max_drawdown: Decimal = Field(..., description="Largest peak-to-trough decline, in currency")
    max_drawdown_pct: Decimal = Field(..., description="Largest drawdown as a percentage of peak")
    annualized_volatility_pct: Optional[Decimal] = Field(
        None, description="Annualised volatility of equity returns (252 trading days)"
    )
    sharpe_ratio: Optional[Decimal] = Field(
        None, description="Annualised Sharpe ratio; null when there is no return dispersion"
    )
    sortino_ratio: Optional[Decimal] = Field(
        None, description="Annualised Sortino ratio; null when there is no downside dispersion"
    )

    number_of_trades: int = Field(..., description="Completed trades")
    winning_trades: int = Field(..., description="Trades with positive net P&L")
    losing_trades: int = Field(..., description="Trades with zero or negative net P&L")
    win_rate_pct: Optional[Decimal] = Field(None, description="Winners / total closed trades")
    profit_factor: Optional[Decimal] = Field(
        None, description="gross profit / gross loss; null when there are no losing trades"
    )
    average_trade_return_pct: Optional[Decimal] = Field(None, description="Mean trade return")
    average_winning_trade_pct: Optional[Decimal] = Field(None, description="Mean winning return")
    average_losing_trade_pct: Optional[Decimal] = Field(None, description="Mean losing return")
    average_holding_period: Optional[Decimal] = Field(
        None, description="Mean calendar days held per trade"
    )
    best_trade_pct: Optional[Decimal] = Field(None, description="Best single trade return")
    worst_trade_pct: Optional[Decimal] = Field(None, description="Worst single trade return")

    benchmark_return_pct: Optional[Decimal] = Field(
        None, description="Buy-and-hold return over the same window with the same capital"
    )
    benchmark_final_equity: Optional[Decimal] = Field(
        None, description="Buy-and-hold final equity"
    )
    strategy_vs_benchmark_pct: Optional[Decimal] = Field(
        None, description="Strategy total return minus benchmark return, in percentage points"
    )

    parameters: Dict[str, Any] = Field(
        default_factory=dict, description="Fully resolved parameters needed to reproduce this run"
    )
    warnings: List[str] = Field(
        default_factory=list, description="Non-fatal observations about this run"
    )
    disclaimer: str = Field(..., description="Historical performance disclaimer")

    trades: List[BacktestTradeResponse] = Field(
        default_factory=list, description="Completed trades in chronological order"
    )
    equity_curve: List[BacktestEquityPointResponse] = Field(
        default_factory=list, description="Equity per trading day, marked at each bar close"
    )
    created_at: Optional[datetime] = Field(None, description="When the backtest was stored")


class BacktestSummaryResponse(BaseModel):
    """Backtest list entry (metrics only, without trades or the equity curve)."""

    model_config = ConfigDict(from_attributes=True)

    backtest_id: int
    stock_id: int
    strategy: str
    strategy_version: str
    start_date: date_type
    end_date: date_type
    initial_capital: Decimal
    final_equity: Decimal
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    number_of_trades: int
    benchmark_return_pct: Optional[Decimal]
    created_at: Optional[datetime]


class BacktestListResponse(BaseModel):
    """Envelope listing stored backtests."""

    success: bool = True
    data: List[BacktestSummaryResponse]
    count: int


class BacktestTradesResponse(BaseModel):
    """Envelope for a page of backtest trades."""

    success: bool = True
    data: List[BacktestTradeResponse]
    count: int
    backtest_id: int


class BacktestEquityResponse(BaseModel):
    """Envelope for a page of the backtest equity curve."""

    success: bool = True
    data: List[BacktestEquityPointResponse]
    count: int
    backtest_id: int
    total: int


class BacktestErrorResponse(BaseModel):
    """Error response shape for backtest endpoints."""

    success: bool = False
    error: Dict[str, str] = Field(..., description="Error details")