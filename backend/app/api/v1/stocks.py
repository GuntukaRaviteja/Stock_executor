from typing import List
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_stock_service
from app.schemas.stock import StockCreate, StockResponse, StockUpdate
from app.services.stock_service import StockService

router = APIRouter(prefix="/stocks", tags=["Stocks"])


@router.get(
    "",
    response_model=List[StockResponse],
    summary="List all stocks",
    description="Retrieve a paginated list of all stocks available in the platform.",
    status_code=status.HTTP_200_OK,
)
def list_stocks(
    skip: int = Query(0, ge=0, description="Offset for pagination"),
    limit: int = Query(100, ge=1, le=500, description="Limit for pagination"),
    db: Session = Depends(get_db),
    service: StockService = Depends(get_stock_service),
) -> List[StockResponse]:
    return service.list_stocks(db, skip=skip, limit=limit)


@router.post(
    "",
    response_model=StockResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new stock",
    description="Add a new stock ticker, company name, and exchange. Prevents duplicates.",
)
def create_stock(
    stock_in: StockCreate,
    db: Session = Depends(get_db),
    service: StockService = Depends(get_stock_service),
) -> StockResponse:
    return service.create_stock(db, stock_in)


@router.get(
    "/symbol/{symbol}",
    response_model=StockResponse,
    summary="Get stock by ticker symbol",
    description="Retrieve stock metadata by its normalized symbol (e.g. AAPL, MSFT).",
    status_code=status.HTTP_200_OK,
)
def get_stock_by_symbol(
    symbol: str,
    db: Session = Depends(get_db),
    service: StockService = Depends(get_stock_service),
) -> StockResponse:
    return service.get_by_symbol(db, symbol)


@router.get(
    "/{stock_id}",
    response_model=StockResponse,
    summary="Get stock by ID",
    description="Retrieve stock metadata by primary key ID.",
    status_code=status.HTTP_200_OK,
)
def get_stock_by_id(
    stock_id: int,
    db: Session = Depends(get_db),
    service: StockService = Depends(get_stock_service),
) -> StockResponse:
    return service.get_stock(db, stock_id)


@router.put(
    "/{stock_id}",
    response_model=StockResponse,
    summary="Update stock details",
    description="Update company name or exchange for a specified stock.",
    status_code=status.HTTP_200_OK,
)
def update_stock(
    stock_id: int,
    stock_in: StockUpdate,
    db: Session = Depends(get_db),
    service: StockService = Depends(get_stock_service),
) -> StockResponse:
    return service.update_stock(db, stock_id, stock_in)


@router.delete(
    "/{stock_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a stock",
    description="Delete a stock and its associated historical data.",
)
def delete_stock(
    stock_id: int,
    db: Session = Depends(get_db),
    service: StockService = Depends(get_stock_service),
) -> None:
    service.delete_stock(db, stock_id)
