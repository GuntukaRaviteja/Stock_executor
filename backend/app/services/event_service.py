from typing import List, Optional
from sqlalchemy.orm import Session
from app.core.exceptions import (
    DuplicateEventError,
    EventNotFoundError,
    StockNotFoundError,
)
from app.core.logging import logger
from app.models.event import StockEvent
from app.repositories.event_repository import event_repository
from app.repositories.stock_repository import stock_repository
from app.schemas.event import StockEventCreate


class EventService:
    """Service layer handling corporate events and news."""

    def __init__(self, repo=event_repository, stock_repo=stock_repository):
        self.repo = repo
        self.stock_repo = stock_repo

    def create_event(self, db: Session, stock_id: int, event_in: StockEventCreate) -> StockEvent:
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            raise StockNotFoundError(stock_id)

        clean_type = event_in.event_type.strip().upper()
        clean_title = event_in.event_title.strip() if event_in.event_title else None

        # Check duplicate
        duplicate = self.repo.find_duplicate(
            db=db,
            stock_id=stock_id,
            event_date=event_in.event_date,
            event_type=clean_type,
            event_title=clean_title,
            source_raw_id=event_in.source_raw_id,
        )
        if duplicate:
            logger.warning(
                f"EventService: Duplicate event detected for stock_id={stock_id} type='{clean_type}' date={event_in.event_date}"
            )
            raise DuplicateEventError(
                f"Event '{clean_title or clean_type}' on date {event_in.event_date} for stock {stock.symbol}"
            )

        data = event_in.model_dump()
        data["stock_id"] = stock_id
        data["event_type"] = clean_type
        if clean_title:
            data["event_title"] = clean_title
        if "impact" in data and data["impact"]:
            data["impact"] = data["impact"].strip().upper()

        event = self.repo.create(db, data)
        logger.info(f"EventService: Created event id={event.id} for stock_id={stock_id}")
        return event

    def get_stock_events(
        self,
        db: Session,
        stock_id: int,
        limit: int = 100,
        event_type: Optional[str] = None,
    ) -> List[StockEvent]:
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            raise StockNotFoundError(stock_id)

        return self.repo.get_by_stock(db, stock_id, limit=limit, event_type=event_type)

    def get_recent_events(self, db: Session, limit: int = 50) -> List[StockEvent]:
        if limit > 200:
            limit = 200
        return self.repo.get_recent(db, limit=limit)


event_service = EventService()
