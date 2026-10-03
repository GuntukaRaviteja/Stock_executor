from datetime import date
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_price_service, get_market_data_service, get_stock_service
from app.schemas.price import (
    PriceIngestResponse,
    PriceSyncRequest,
    PriceSyncResponse,
    StockPriceBulkCreate,
    StockPriceCreate,
    StockPriceResponse,
)
from app.services.market_data.service import MarketDataService
from app.services.price_service import PriceService
from app.services.stock_service import StockService

router = APIRouter(prefix="/stocks", tags=["Stock Prices"])


@router.get(
    "/{stock_id}/prices",
    response_model=List[StockPriceResponse],
    summary="Get stock historical prices",
    description="Retrieve historical daily price bars for a stock, filterable by date range.",
    status_code=status.HTTP_200_OK,
)
def get_price_history(
    stock_id: int,
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD) inclusive"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD) inclusive"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    db: Session = Depends(get_db),
    service: PriceService = Depends(get_price_service),
) -> List[StockPriceResponse]:
    return service.get_price_history(
        db, stock_id=stock_id, start_date=start_date, end_date=end_date, limit=limit
    )


@router.get(
    "/{stock_id}/prices/latest",
    response_model=StockPriceResponse,
    summary="Get latest stock price",
    description="Retrieve the most recent daily price record for a specified stock.",
    status_code=status.HTTP_200_OK,
)
def get_latest_price(
    stock_id: int,
    db: Session = Depends(get_db),
    service: PriceService = Depends(get_price_service),
) -> StockPriceResponse:
    return service.get_latest_price(db, stock_id=stock_id)


@router.post(
    "/{stock_id}/prices",
    response_model=StockPriceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a price record",
    description="Insert a single validated daily price record. Enforces OHLC logic and duplicate prevention.",
)
def add_price(
    stock_id: int,
    price_in: StockPriceCreate,
    db: Session = Depends(get_db),
    service: PriceService = Depends(get_price_service),
) -> StockPriceResponse:
    return service.create_price(db, stock_id=stock_id, price_in=price_in)


@router.post(
    "/{stock_id}/prices/bulk",
    status_code=status.HTTP_201_CREATED,
    summary="Bulk ingest price records",
    description="Insert multiple validated price records, automatically skipping dates that already exist.",
)
def bulk_add_prices(
    stock_id: int,
    payload: StockPriceBulkCreate,
    db: Session = Depends(get_db),
    service: PriceService = Depends(get_price_service),
) -> Dict[str, Any]:
    inserted, skipped = service.bulk_create_prices(db, stock_id, payload.prices)
    return {
        "success": True,
        "stock_id": stock_id,
        "inserted": inserted,
        "skipped": skipped,
    }


@router.post(
    "/{stock_id}/prices/sync",
    response_model=PriceSyncResponse,
    summary="Sync stock prices from market data provider",
    description="Fetch historical OHLCV data from the configured market data provider and upsert into database. Idempotent - safe to run multiple times.",
    status_code=status.HTTP_200_OK,
)
async def sync_stock_prices(
    stock_id: int,
    payload: PriceSyncRequest,
    db: Session = Depends(get_db),
    market_service: MarketDataService = Depends(get_market_data_service),
    stock_service: StockService = Depends(get_stock_service),
) -> PriceSyncResponse:
    """
    Sync stock prices from external provider.

    - **stock_id**: Internal stock ID
    - **start_date**: Start date for historical data (inclusive)
    - **end_date**: End date for historical data (inclusive)

    Returns sync statistics including records received, inserted, and updated.
    """
    # Verify stock exists
    stock = stock_service.get_stock(db, stock_id)

    # Ingest from provider
    result = await market_service.ingest_stock(
        db, stock, start_date=payload.start_date, end_date=payload.end_date
    )

    # Build response
    date_range = None
    if result.fetched > 0:
        date_range = {"start": payload.start_date, "end": payload.end_date}

    return PriceSyncResponse(
        stock_id=stock_id,
        symbol=stock.symbol,
        records_received=result.fetched,
        records_inserted=result.inserted,
        records_updated=result.updated,
        date_range=date_range,
    )


@router.post(
    "/{stock_id}/prices/ingest",
    response_model=PriceIngestResponse,
    summary="Ingest current/latest stock price from market data provider",
    description="Fetch the latest quote from the configured market data provider and upsert as today's price record. Idempotent - safe to run multiple times.",
    status_code=status.HTTP_200_OK,
)
async def ingest_current_price(
    stock_id: int,
    db: Session = Depends(get_db),
    market_service: MarketDataService = Depends(get_market_data_service),
    stock_service: StockService = Depends(get_stock_service),
) -> PriceIngestResponse:
    """
    Ingest current/latest price for a stock from external provider.

    - **stock_id**: Internal stock ID

    Returns ingestion statistics including records received, inserted, updated, and skipped.
    """
    # Verify stock exists
    stock = stock_service.get_stock(db, stock_id)

    # Ingest current price from provider
    result = await market_service.ingest_current_price(db, stock)

    return PriceIngestResponse(
        stock_id=stock_id,
        symbol=stock.symbol,
        records_received=result.fetched,
        records_inserted=result.inserted,
        records_updated=result.updated,
        records_skipped=result.skipped,
        status=result.status,
        error=result.error,
    )


@router.post(
    "/{stock_id}/prices/ingest-historical",
    response_model=PriceIngestResponse,
    summary="Ingest historical stock prices from market data provider",
    description="Fetch historical OHLCV data from the configured market data provider for a date range and upsert into database. Idempotent - safe to run multiple times.",
    status_code=status.HTTP_200_OK,
)
async def ingest_historical_prices(
    stock_id: int,
    payload: PriceSyncRequest,
    db: Session = Depends(get_db),
    market_service: MarketDataService = Depends(get_market_data_service),
    stock_service: StockService = Depends(get_stock_service),
) -> PriceIngestResponse:
    """
    Ingest historical prices for a stock from external provider.

    - **stock_id**: Internal stock ID
    - **start_date**: Start date for historical data (inclusive)
    - **end_date**: End date for historical data (inclusive)

    Returns ingestion statistics including records received, inserted, updated, and skipped.
    """
    # Verify stock exists
    stock = stock_service.get_stock(db, stock_id)

    # Ingest historical data from provider
    result = await market_service.ingest_stock(
        db, stock, start_date=payload.start_date, end_date=payload.end_date
    )

    # Build response
    date_range = None
    if result.fetched > 0:
        date_range = {"start": payload.start_date, "end": payload.end_date}

    return PriceIngestResponse(
        stock_id=stock_id,
        symbol=stock.symbol,
        records_received=result.fetched,
        records_inserted=result.inserted,
        records_updated=result.updated,
        records_skipped=result.skipped,
        status=result.status,
        error=result.error,
        date_range=date_range,
    )
