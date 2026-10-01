from typing import List, Optional
from sqlalchemy.orm import Session
from app.core.exceptions import CompanyNotFoundError, StockNotFoundError
from app.core.logging import logger
from app.models.company import Company
from app.repositories.company_repository import company_repository
from app.repositories.stock_repository import stock_repository
from app.schemas.company import CompanyCreate, CompanyUpdate


class CompanyService:
    """Service layer handling company profiles and details."""

    def __init__(self, repo=company_repository, stock_repo=stock_repository):
        self.repo = repo
        self.stock_repo = stock_repo

    def get_by_stock_id(self, db: Session, stock_id: int) -> Company:
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            raise StockNotFoundError(stock_id)

        company = self.repo.get_by_stock_id(db, stock_id)
        if not company:
            # Auto-create empty profile linked to stock so consumer gets company metadata
            company = self.repo.create(db, {"stock_id": stock_id})
        return company

    def get_by_symbol(self, db: Session, symbol: str) -> Company:
        normalized = symbol.strip().upper()
        stock = self.stock_repo.get_by_symbol(db, normalized)
        if not stock:
            raise StockNotFoundError(normalized)

        return self.get_by_stock_id(db, stock.id)

    def update_company(
        self, db: Session, stock_id: int, company_in: CompanyUpdate
    ) -> Company:
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            raise StockNotFoundError(stock_id)

        update_data = company_in.model_dump(exclude_unset=True)
        company = self.repo.upsert(db, stock_id, update_data)
        logger.info(f"CompanyService: Upserted company profile for stock_id={stock_id}")
        return company

    def list_companies(self, db: Session, skip: int = 0, limit: int = 100) -> List[Company]:
        return self.repo.get_all(db, skip=skip, limit=limit)


company_service = CompanyService()
