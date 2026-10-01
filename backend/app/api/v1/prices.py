from datetime import date
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_price_service
from app.schemas.price import (
    StockPriceBulkCreate,
    StockPriceCreate,
    StockPriceResponse,
)
from app.services.price_service import PriceService

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
