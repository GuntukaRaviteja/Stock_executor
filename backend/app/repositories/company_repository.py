from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from app.models.company import Company
from app.models.stock import Stock


class CompanyRepository:
    """Database repository for Company profile entities."""

    def create(self, db: Session, company_in: dict) -> Company:
        company = Company(**company_in)
        db.add(company)
        db.commit()
        db.refresh(company)
        return company

    def get_by_id(self, db: Session, company_id: int) -> Optional[Company]:
        stmt = select(Company).options(joinedload(Company.stock)).where(Company.id == company_id)
        return db.scalars(stmt).first()

    def get_by_stock_id(self, db: Session, stock_id: int) -> Optional[Company]:
        stmt = select(Company).options(joinedload(Company.stock)).where(Company.stock_id == stock_id)
        return db.scalars(stmt).first()

    def get_by_symbol(self, db: Session, symbol: str) -> Optional[Company]:
        stmt = (
            select(Company)
            .join(Stock, Company.stock_id == Stock.id)
            .options(joinedload(Company.stock))
            .where(Stock.symbol == symbol)
        )
        return db.scalars(stmt).first()

    def get_all(self, db: Session, skip: int = 0, limit: int = 100) -> List[Company]:
        stmt = (
            select(Company)
            .options(joinedload(Company.stock))
            .offset(skip)
            .limit(limit)
        )
        return list(db.scalars(stmt).all())

    def update(self, db: Session, company: Company, update_data: dict) -> Company:
        for field, value in update_data.items():
            if hasattr(company, field) and field not in ("id", "stock_id", "created_at"):
                setattr(company, field, value)
        db.commit()
        db.refresh(company)
        return company

    def upsert(self, db: Session, stock_id: int, company_data: dict) -> Company:
        existing = self.get_by_stock_id(db, stock_id)
        if existing:
            return self.update(db, existing, company_data)
        data = {**company_data, "stock_id": stock_id}
        return self.create(db, data)


company_repository = CompanyRepository()
