from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db
from app.schemas.market_data import (
    IngestionStockResult,
    MarketDataBackfillRequest,
    MarketDataBackfillResponse,
    MarketDataIngestionRequest,
    MarketDataIngestionResponse,
    MarketDataResponse,
)
from app.services.market_data import market_data_service

router = APIRouter(prefix="/market-data", tags=["Market Data"])


@router.get(
    "/{symbol}",
    response_model=MarketDataResponse,
    summary="Get historical market data for a stock",
    description="Retrieve historical daily OHLCV price data for a stock by its symbol, filterable by date range.",
    status_code=status.HTTP_200_OK,
)
def get_market_data(
    symbol: str,
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD) inclusive"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD) inclusive"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    db: Session = Depends(get_db),
) -> MarketDataResponse:
    """
    Get historical market data for a stock.

    - **symbol**: Stock symbol (e.g., RELIANCE, TCS, INFY)
    - **start_date**: Filter from this date (inclusive)
    - **end_date**: Filter to this date (inclusive)
    - **limit**: Maximum number of records to return (default 100, max 1000)
    """
    return market_data_service.get_stock_history(
        db, symbol=symbol, start_date=start_date, end_date=end_date, limit=limit
    )


@router.post(
    "/ingest",
    response_model=MarketDataIngestionResponse,
    summary="Ingest historical market data for multiple stocks",
    description="Fetch, validate, normalize, and store historical OHLCV data for a list of stocks. Idempotent - safe to run multiple times.",
    status_code=status.HTTP_200_OK,
)
async def ingest_market_data(
    request: MarketDataIngestionRequest,
    db: Session = Depends(get_db),
) -> MarketDataIngestionResponse:
    """
    Ingest historical market data for multiple stocks.

    - **symbols**: List of stock symbols (e.g., ["RELIANCE", "TCS", "INFY"])
    - **start_date**: Start date for historical data
    - **end_date**: End date for historical data
    - **provider**: Optional provider override (mock, yfinance, alphavantage)

    Returns detailed statistics per stock including:
    - Records fetched from provider
    - Records inserted (new)
    - Records updated (existing)
    - Records skipped (duplicates/invalid)
    - Errors if any
    """
    return await market_data_service.ingest_multiple_stocks(
        db,
        symbols=request.symbols,
        start_date=request.start_date,
        end_date=request.end_date,
    )


@router.post(
    "/backfill",
    response_model=MarketDataBackfillResponse,
    summary="Historical backfill for multiple stocks",
    description="Run a controlled historical backfill for a list of stocks over a specified date range.",
    status_code=status.HTTP_200_OK,
)
async def backfill_market_data(
    request: MarketDataBackfillRequest,
    db: Session = Depends(get_db),
) -> MarketDataBackfillResponse:
    """
    Historical backfill for market data.

    - **symbols**: List of stock symbols
    - **start_date**: Start date for backfill
    - **end_date**: End date for backfill

    Similar to ingest but designed for larger historical ranges.
    """
    response = await market_data_service.ingest_multiple_stocks(
        db,
        symbols=request.symbols,
        start_date=request.start_date,
        end_date=request.end_date,
    )

    return MarketDataBackfillResponse(
        status=response.status,
        requested=response.requested,
        successful=response.successful,
        failed=response.failed,
        not_found=response.not_found,
        total_records_fetched=response.total_records_fetched,
        total_records_inserted=response.total_records_inserted,
        total_records_updated=response.total_records_updated,
        total_records_skipped=response.total_records_skipped,
        results=response.results,
    )


@router.post(
    "/ingest/single",
    response_model=IngestionStockResult,
    summary="Ingest historical market data for a single stock",
    description="Fetch, validate, normalize, and store historical OHLCV data for a single stock. Idempotent.",
    status_code=status.HTTP_200_OK,
)
async def ingest_single_stock(
    symbol: str = Query(..., description="Stock symbol (e.g., RELIANCE)"),
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
) -> IngestionStockResult:
    """
    Ingest historical market data for a single stock.

    - **symbol**: Stock symbol
    - **start_date**: Start date for historical data
    - **end_date**: End date for historical data
    """
    from app.repositories.stock_repository import stock_repository
    from app.core.exceptions import StockNotFoundError

    stock = stock_repository.get_by_symbol(db, symbol.strip().upper())
    if not stock:
        raise StockNotFoundError(symbol)

    return await market_data_service.ingest_stock(db, stock, start_date, end_date)