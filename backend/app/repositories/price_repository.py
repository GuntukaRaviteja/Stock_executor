from datetime import date
from typing import List, Optional
from sqlalchemy import desc, select
from sqlalchemy.orm import Session
from app.models.price import StockPrice


class PriceRepository:
    """Database repository for StockPrice entities."""

    def create(self, db: Session, price_in: dict) -> StockPrice:
        price = StockPrice(**price_in)
        db.add(price)
        db.commit()
        db.refresh(price)
        return price

    def bulk_create(self, db: Session, prices_in: List[dict]) -> List[StockPrice]:
        """Insert multiple prices inside a transaction."""
        objects = [StockPrice(**item) for item in prices_in]
        db.add_all(objects)
        db.commit()
        for obj in objects:
            db.refresh(obj)
        return objects

    def get_by_date(self, db: Session, stock_id: int, price_date: date) -> Optional[StockPrice]:
        stmt = select(StockPrice).where(
            StockPrice.stock_id == stock_id,
            StockPrice.price_date == price_date,
        )
        return db.scalars(stmt).first()

    def get_latest(self, db: Session, stock_id: int) -> Optional[StockPrice]:
        stmt = (
            select(StockPrice)
            .where(StockPrice.stock_id == stock_id)
            .order_by(desc(StockPrice.price_date))
            .limit(1)
        )
        return db.scalars(stmt).first()

    def get_history(
        self,
        db: Session,
        stock_id: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 100,
    ) -> List[StockPrice]:
        stmt = select(StockPrice).where(StockPrice.stock_id == stock_id)

        if start_date:
            stmt = stmt.where(StockPrice.price_date >= start_date)
        if end_date:
            stmt = stmt.where(StockPrice.price_date <= end_date)

        stmt = stmt.order_by(desc(StockPrice.price_date)).limit(limit)
        return list(db.scalars(stmt).all())

    def get_existing_dates_for_stock(
        self,
        db: Session,
        stock_id: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> set[date]:
        """Fetch set of dates already recorded for a stock to assist in deduplication."""
        stmt = select(StockPrice.price_date).where(StockPrice.stock_id == stock_id)
        if start_date:
            stmt = stmt.where(StockPrice.price_date >= start_date)
        if end_date:
            stmt = stmt.where(StockPrice.price_date <= end_date)
        return set(db.scalars(stmt).all())


price_repository = PriceRepository()
