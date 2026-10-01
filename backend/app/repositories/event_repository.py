from datetime import date
from typing import List, Optional, Tuple
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, joinedload
from app.models.event import StockEvent
from app.models.stock import Stock


class EventRepository:
    """Database repository for StockEvent entities."""

    def create(self, db: Session, event_in: dict) -> StockEvent:
        event = StockEvent(**event_in)
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    def get_by_id(self, db: Session, event_id: int) -> Optional[StockEvent]:
        stmt = select(StockEvent).where(StockEvent.id == event_id)
        return db.scalars(stmt).first()

    def get_by_stock(
        self,
        db: Session,
        stock_id: int,
        limit: int = 100,
        event_type: Optional[str] = None,
    ) -> List[StockEvent]:
        stmt = select(StockEvent).where(StockEvent.stock_id == stock_id)
        if event_type:
            stmt = stmt.where(StockEvent.event_type == event_type.upper())
        stmt = stmt.order_by(desc(StockEvent.event_date), desc(StockEvent.id)).limit(limit)
        return list(db.scalars(stmt).all())

    def get_recent(self, db: Session, limit: int = 50) -> List[StockEvent]:
        stmt = (
            select(StockEvent)
            .options(joinedload(StockEvent.stock))
            .order_by(desc(StockEvent.event_date), desc(StockEvent.id))
            .limit(limit)
        )
        return list(db.scalars(stmt).all())

    def find_duplicate(
        self,
        db: Session,
        stock_id: int,
        event_date: Optional[date],
        event_type: str,
        event_title: Optional[str],
        source_raw_id: Optional[int] = None,
    ) -> Optional[StockEvent]:
        """Check for existing event to prevent duplicates."""
        stmt = select(StockEvent).where(
            StockEvent.stock_id == stock_id,
            StockEvent.event_type == event_type,
        )
        if event_date is not None:
            stmt = stmt.where(StockEvent.event_date == event_date)
        if source_raw_id is not None:
            stmt = stmt.where(StockEvent.source_raw_id == source_raw_id)
        elif event_title:
            stmt = stmt.where(StockEvent.event_title == event_title)

        return db.scalars(stmt).first()


event_repository = EventRepository()
