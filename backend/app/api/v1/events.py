from typing import List, Optional
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_event_service
from app.schemas.event import (
    RecentEventResponse,
    StockEventCreate,
    StockEventResponse,
)
from app.services.event_service import EventService

router = APIRouter(tags=["Stock Events"])


@router.get(
    "/stocks/{stock_id}/events",
    response_model=List[StockEventResponse],
    summary="Get events for a stock",
    description="Retrieve corporate events, news, and earnings milestones for a stock.",
    status_code=status.HTTP_200_OK,
)
def get_stock_events(
    stock_id: int,
    limit: int = Query(100, ge=1, le=500, description="Max events to return"),
    event_type: Optional[str] = Query(None, description="Optional filter by event_type (e.g. EARNINGS, NEWS)"),
    db: Session = Depends(get_db),
    service: EventService = Depends(get_event_service),
) -> List[StockEventResponse]:
    return service.get_stock_events(db, stock_id=stock_id, limit=limit, event_type=event_type)


@router.post(
    "/stocks/{stock_id}/events",
    response_model=StockEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an event for a stock",
    description="Record a new corporate event or news item. Duplicate events are safely rejected.",
)
def create_stock_event(
    stock_id: int,
    event_in: StockEventCreate,
    db: Session = Depends(get_db),
    service: EventService = Depends(get_event_service),
) -> StockEventResponse:
    return service.create_event(db, stock_id=stock_id, event_in=event_in)


@router.get(
    "/events/recent",
    response_model=List[RecentEventResponse],
    summary="Get recent events across all stocks",
    description="Retrieve the latest corporate events and news chronologically across all tracked stocks.",
    status_code=status.HTTP_200_OK,
)
def get_recent_events(
    limit: int = Query(50, ge=1, le=200, description="Max events to return"),
    db: Session = Depends(get_db),
    service: EventService = Depends(get_event_service),
) -> List[RecentEventResponse]:
    events = service.get_recent_events(db, limit=limit)
    response_items = []
    for ev in events:
        item = RecentEventResponse.model_validate(ev)
        if ev.stock:
            item.stock_symbol = ev.stock.symbol
            item.company_name = ev.stock.company_name
        response_items.append(item)
    return response_items
