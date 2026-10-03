"""Phase 8 API endpoints for strategy backtesting and signal validation.

These endpoints are **historical only**. Nothing here predicts future prices, places
orders, or advises on a trade. Historical backtest performance does not guarantee future
performance.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_backtest_service, get_db
from app.schemas.backtest import (
    BacktestEquityPointResponse,
    BacktestEquityResponse,
    BacktestListResponse,
    BacktestRequest,
    BacktestResponse,
    BacktestSummaryResponse,
    BacktestTradeResponse,
    BacktestTradesResponse,
)
from app.services.backtest.service import BacktestService, MAX_PAGE_LIMIT
from app.services.backtest.strategy import strategy_registry

router = APIRouter(prefix="/backtests", tags=["Backtesting"])

#: Attached to every backtest response so the constraint travels with the data.
HISTORICAL_PERFORMANCE_DISCLAIMER = (
    "Historical backtest performance does not guarantee future performance. "
    "These results describe how a fixed rule set would have behaved on past prices, "
    "under a simplified execution model, and are not a prediction or advice."
)

_EXECUTION_MODEL_NOTES = (
    "Execution model: signals are computed from the close of bar N and filled at the "
    "open of bar N+1, so no decision can use information from a bar it was not yet "
    "available at. Commission and slippage are charged per side and are configurable; "
    "both default to 0 for deterministic testing. Position size is a fixed percentage "
    "of available cash. The engine is long-only, one position at a time, with no "
    "pyramiding and no short selling."
)


def _build_response(
    backtest,
    symbol: str,
    *,
    trades: Optional[List] = None,
    equity_curve: Optional[List] = None,
    warnings: Optional[List[str]] = None,
) -> BacktestResponse:
    """Assemble the public response from a persisted backtest.

    ``null`` metrics stay ``null``: they are reported honestly rather than being coerced
    into a zero that would read as a real measurement.
    """
    import json

    parameters: dict = {}
    if backtest.parameters_json:
        try:
            parameters = json.loads(backtest.parameters_json)
        except json.JSONDecodeError:
            parameters = {"raw": backtest.parameters_json}

    curve = equity_curve or []
    dates = [point.date for point in curve]

    return BacktestResponse(
        backtest_id=backtest.id,
        stock_id=backtest.stock_id,
        symbol=symbol,
        strategy=backtest.strategy_name,
        strategy_version=backtest.strategy_version,
        description=(
            f"{backtest.strategy_name} v{backtest.strategy_version}: long-only, rule-based "
            "strategy driven by the Phase 7 technical signal engine."
        ),
        start_date=backtest.start_date,
        end_date=backtest.end_date,
        actual_start_date=dates[0] if dates else backtest.start_date,
        actual_end_date=dates[-1] if dates else backtest.end_date,
        bars=len(curve),
        initial_capital=backtest.initial_capital,
        final_equity=backtest.final_equity,
        total_return_pct=backtest.total_return_pct,
        cagr_pct=backtest.cagr_pct,
        peak_equity=backtest.peak_equity,
        trough_equity=backtest.trough_equity,
        max_drawdown=backtest.max_drawdown,
        max_drawdown_pct=backtest.max_drawdown_pct,
        annualized_volatility_pct=backtest.annualized_volatility_pct,
        sharpe_ratio=backtest.sharpe_ratio,
        sortino_ratio=backtest.sortino_ratio,
        number_of_trades=backtest.number_of_trades,
        winning_trades=backtest.winning_trades,
        losing_trades=backtest.losing_trades,
        win_rate_pct=backtest.win_rate_pct,
        profit_factor=backtest.profit_factor,
        average_trade_return_pct=backtest.average_trade_return_pct,
        average_winning_trade_pct=backtest.average_winning_trade_pct,
        average_losing_trade_pct=backtest.average_losing_trade_pct,
        average_holding_period=backtest.average_holding_period,
        best_trade_pct=backtest.best_trade_pct,
        worst_trade_pct=backtest.worst_trade_pct,
        benchmark_return_pct=backtest.benchmark_return_pct,
        benchmark_final_equity=backtest.benchmark_final_equity,
        strategy_vs_benchmark_pct=backtest.strategy_vs_benchmark_pct,
        parameters=parameters,
        warnings=warnings or [],
        disclaimer=HISTORICAL_PERFORMANCE_DISCLAIMER,
        trades=[BacktestTradeResponse.model_validate(trade) for trade in (trades or [])],
        equity_curve=[
            BacktestEquityPointResponse.model_validate(point) for point in curve
        ],
        created_at=backtest.created_at,
    )


@router.get(
    "/strategies",
    response_model=List[str],
    summary="List available backtest strategies",
    description=(
        "Returns the strategy identifiers accepted by `POST /backtests`. Each identifier "
        "maps to a fixed, versioned rule set so a run can be reproduced exactly."
    ),
    status_code=status.HTTP_200_OK,
)
def list_strategies() -> List[str]:
    """List the registered strategy names."""
    return strategy_registry.available


@router.post(
    "",
    response_model=BacktestResponse,
    summary="Run a backtest against historical OHLCV data",
    description=(
        "Applies a deterministic strategy to stored historical prices, simulates the "
        "resulting trades, and returns metrics, a buy-and-hold benchmark, the trade list "
        "and the equity curve.\n\n"
        f"{_EXECUTION_MODEL_NOTES}\n\n"
        "**No look-ahead bias:** indicators for a bar are rebuilt from data available on "
        "that bar only, so mutating a future candle cannot change an earlier decision.\n\n"
        f"**{HISTORICAL_PERFORMANCE_DISCLAIMER}**\n\n"
        "The result is persisted and can be retrieved later with `GET /backtests/{id}`."
    ),
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Backtest completed and stored"},
        404: {"description": "Stock not found"},
        422: {"description": "Invalid parameters, unknown strategy, or unusable OHLCV data"},
    },
)
def run_backtest(
    payload: BacktestRequest,
    include_equity_curve: bool = Query(
        True, description="Include the full per-day equity curve in the response"
    ),
    db: Session = Depends(get_db),
    service: BacktestService = Depends(get_backtest_service),
) -> BacktestResponse:
    """Run a backtest, persist it, and return the full result."""
    outcome = service.run_backtest(db, payload)
    backtest = service.get_backtest(db, outcome.id)
    symbol = db.get(_stock_model(), backtest.stock_id).symbol
    trades = service.get_trades(db, backtest.id, skip=0, limit=MAX_PAGE_LIMIT)
    equity = (
        service.get_equity_curve(db, backtest.id, skip=0, limit=MAX_PAGE_LIMIT)
        if include_equity_curve
        else []
    )
    return _build_response(backtest, symbol, trades=trades, equity_curve=equity)


@router.get(
    "",
    response_model=BacktestListResponse,
    summary="List stored backtests",
    description="Returns stored backtests, newest first, optionally filtered by stock.",
    status_code=status.HTTP_200_OK,
)
def list_backtests(
    stock_id: Optional[int] = Query(None, ge=1, description="Filter by internal stock ID"),
    skip: int = Query(0, ge=0, description="Records to skip"),
    limit: int = Query(50, ge=1, le=MAX_PAGE_LIMIT, description="Maximum records to return"),
    db: Session = Depends(get_db),
    service: BacktestService = Depends(get_backtest_service),
) -> BacktestListResponse:
    """List stored backtest summaries."""
    from app.models.backtest import Backtest

    records = service.list_backtests(db, stock_id=stock_id, skip=skip, limit=limit)
    summaries = [
        BacktestSummaryResponse(
            backtest_id=record.id,
            stock_id=record.stock_id,
            strategy=record.strategy_name,
            strategy_version=record.strategy_version,
            start_date=record.start_date,
            end_date=record.end_date,
            initial_capital=record.initial_capital,
            final_equity=record.final_equity,
            total_return_pct=record.total_return_pct,
            max_drawdown_pct=record.max_drawdown_pct,
            number_of_trades=record.number_of_trades,
            benchmark_return_pct=record.benchmark_return_pct,
            created_at=record.created_at,
        )
        for record in records
    ]
    return BacktestListResponse(data=summaries, count=len(summaries))


@router.get(
    "/{backtest_id}",
    response_model=BacktestResponse,
    summary="Get a stored backtest",
    description=(
        "Returns a previously computed backtest with its metrics, trades and equity "
        "curve, exactly as they were produced at run time."
    ),
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "Backtest not found"}},
)
def get_backtest(
    backtest_id: int = Path(..., ge=1, description="Backtest ID"),
    include_equity_curve: bool = Query(
        True, description="Include the full per-day equity curve in the response"
    ),
    db: Session = Depends(get_db),
    service: BacktestService = Depends(get_backtest_service),
) -> BacktestResponse:
    """Retrieve a stored backtest by id."""
    backtest = service.get_backtest(db, backtest_id)
    symbol = db.get(_stock_model(), backtest.stock_id).symbol
    trades = service.get_trades(db, backtest_id, skip=0, limit=MAX_PAGE_LIMIT)
    equity = (
        service.get_equity_curve(db, backtest_id, skip=0, limit=MAX_PAGE_LIMIT)
        if include_equity_curve
        else []
    )
    return _build_response(backtest, symbol, trades=trades, equity_curve=equity)


@router.get(
    "/{backtest_id}/trades",
    response_model=BacktestTradesResponse,
    summary="Get the trades of a backtest",
    description=(
        "Returns the completed round-trip trades in chronological order, including "
        "commission, slippage, net P&L and the reason each position was closed."
    ),
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "Backtest not found"}},
)
def get_backtest_trades(
    backtest_id: int = Path(..., ge=1, description="Backtest ID"),
    skip: int = Query(0, ge=0, description="Records to skip"),
    limit: int = Query(100, ge=1, le=MAX_PAGE_LIMIT, description="Maximum records to return"),
    db: Session = Depends(get_db),
    service: BacktestService = Depends(get_backtest_service),
) -> BacktestTradesResponse:
    """Return a page of trades for a stored backtest."""
    trades = service.get_trades(db, backtest_id, skip=skip, limit=limit)
    return BacktestTradesResponse(
        backtest_id=backtest_id,
        data=[BacktestTradeResponse.model_validate(trade) for trade in trades],
        count=len(trades),
    )


@router.get(
    "/{backtest_id}/equity",
    response_model=BacktestEquityResponse,
    summary="Get the equity curve of a backtest",
    description=(
        "Returns cash, position value and total equity for every simulated trading day, "
        "marked at that day's close."
    ),
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "Backtest not found"}},
)
def get_backtest_equity(
    backtest_id: int = Path(..., ge=1, description="Backtest ID"),
    skip: int = Query(0, ge=0, description="Records to skip"),
    limit: int = Query(100, ge=1, le=MAX_PAGE_LIMIT, description="Maximum records to return"),
    db: Session = Depends(get_db),
    service: BacktestService = Depends(get_backtest_service),
) -> BacktestEquityResponse:
    """Return a page of the equity curve for a stored backtest."""
    points = service.get_equity_curve(db, backtest_id, skip=skip, limit=limit)
    return BacktestEquityResponse(
        backtest_id=backtest_id,
        data=[BacktestEquityPointResponse.model_validate(point) for point in points],
        count=len(points),
        total=service.count_equity_points(db, backtest_id),
    )


def _stock_model():
    """Lazy import of the ``Stock`` ORM class to keep the module import graph flat."""
    from app.models.stock import Stock

    return Stock


__all__: List[str] = ["router"]