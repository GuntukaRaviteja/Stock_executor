from typing import List, Optional
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models.stock import Stock


class StockRepository:
    """Database repository for Stock entities."""

    def create(self, db: Session, stock_in: dict) -> Stock:
        stock = Stock(**stock_in)
        db.add(stock)
        db.commit()
        db.refresh(stock)
        return stock

    def get_by_id(self, db: Session, stock_id: int) -> Optional[Stock]:
        stmt = select(Stock).where(Stock.id == stock_id)
        return db.scalars(stmt).first()

    def get_by_symbol(self, db: Session, symbol: str) -> Optional[Stock]:
        stmt = select(Stock).where(Stock.symbol == symbol)
        return db.scalars(stmt).first()

    def get_all(self, db: Session, skip: int = 0, limit: int = 100) -> List[Stock]:
        stmt = select(Stock).order_by(Stock.symbol.asc()).offset(skip).limit(limit)
        return list(db.scalars(stmt).all())

    def update(self, db: Session, stock: Stock, update_data: dict) -> Stock:
        for field, value in update_data.items():
            if hasattr(stock, field):
                setattr(stock, field, value)
        db.commit()
        db.refresh(stock)
        return stock

    def delete(self, db: Session, stock: Stock) -> None:
        db.delete(stock)
        db.commit()

    def count(self, db: Session) -> int:
        stmt = select(func.count(Stock.id))
        return db.scalar(stmt) or 0


stock_repository = StockRepository()
