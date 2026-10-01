from typing import List, Optional
from sqlalchemy.orm import Session
from app.core.exceptions import DuplicateStockError, StockNotFoundError
from app.core.logging import logger
from app.models.stock import Stock
from app.repositories.stock_repository import stock_repository
from app.schemas.stock import StockCreate, StockUpdate


class StockService:
    """Service layer handling business logic for Stock entities."""

    def __init__(self, repo=stock_repository):
        self.repo = repo

    def get_stock(self, db: Session, stock_id: int) -> Stock:
        stock = self.repo.get_by_id(db, stock_id)
        if not stock:
            raise StockNotFoundError(stock_id)
        return stock

    def get_by_symbol(self, db: Session, symbol: str) -> Stock:
        normalized = symbol.strip().upper()
        stock = self.repo.get_by_symbol(db, normalized)
        if not stock:
            raise StockNotFoundError(normalized)
        return stock

    def list_stocks(self, db: Session, skip: int = 0, limit: int = 100) -> List[Stock]:
        return self.repo.get_all(db, skip=skip, limit=limit)

    def create_stock(self, db: Session, stock_in: StockCreate) -> Stock:
        normalized_symbol = stock_in.symbol.strip().upper()
        existing = self.repo.get_by_symbol(db, normalized_symbol)
        if existing:
            logger.warning(f"StockService: Attempt to create duplicate symbol '{normalized_symbol}'")
            raise DuplicateStockError(normalized_symbol)

        stock_data = {
            "symbol": normalized_symbol,
            "company_name": stock_in.company_name.strip(),
            "exchange": stock_in.exchange.strip().upper() if stock_in.exchange else None,
        }
        stock = self.repo.create(db, stock_data)
        logger.info(f"StockService: Created stock id={stock.id} symbol='{stock.symbol}'")
        return stock

    def update_stock(self, db: Session, stock_id: int, stock_in: StockUpdate) -> Stock:
        stock = self.get_stock(db, stock_id)
        update_dict = stock_in.model_dump(exclude_unset=True)
        if "exchange" in update_dict and update_dict["exchange"]:
            update_dict["exchange"] = update_dict["exchange"].strip().upper()
        if "company_name" in update_dict and update_dict["company_name"]:
            update_dict["company_name"] = update_dict["company_name"].strip()

        updated = self.repo.update(db, stock, update_dict)
        logger.info(f"StockService: Updated stock id={stock_id}")
        return updated

    def delete_stock(self, db: Session, stock_id: int) -> None:
        stock = self.get_stock(db, stock_id)
        self.repo.delete(db, stock)
        logger.info(f"StockService: Deleted stock id={stock_id} symbol='{stock.symbol}'")


stock_service = StockService()
