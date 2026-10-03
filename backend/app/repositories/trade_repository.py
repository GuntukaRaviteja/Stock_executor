from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.portfolio import PortfolioTrade


class TradeRepository:
    """Database repository for immutable paper-trade execution records.

    Trades are insert-only. No update or delete method exists by design.
    """

    def add(self, db: Session, trade: PortfolioTrade) -> PortfolioTrade:
        db.add(trade)
        db.flush()
        return trade

    def get_by_id(self, db: Session, trade_id: int) -> Optional[PortfolioTrade]:
        stmt = select(PortfolioTrade).where(PortfolioTrade.id == trade_id)
        return db.scalars(stmt).first()

    def get_by_order(self, db: Session, order_id: int) -> Optional[PortfolioTrade]:
        stmt = select(PortfolioTrade).where(PortfolioTrade.order_id == order_id)
        return db.scalars(stmt).first()

    def list_history(
        self,
        db: Session,
        portfolio_id: int,
        skip: int = 0,
        limit: int = 100,
        stock_id: Optional[int] = None,
    ) -> List[PortfolioTrade]:
        stmt = select(PortfolioTrade).where(PortfolioTrade.portfolio_id == portfolio_id)
        if stock_id is not None:
            stmt = stmt.where(PortfolioTrade.stock_id == stock_id)
        stmt = stmt.order_by(PortfolioTrade.executed_at.desc(), PortfolioTrade.id.desc()).offset(skip).limit(limit)
        return list(db.scalars(stmt).all())

    def count(self, db: Session, portfolio_id: int, stock_id: Optional[int] = None) -> int:
        """Total matching the same filters as the trade-history query."""
        stmt = (
            select(func.count())
            .select_from(PortfolioTrade)
            .where(PortfolioTrade.portfolio_id == portfolio_id)
        )
        if stock_id is not None:
            stmt = stmt.where(PortfolioTrade.stock_id == stock_id)
        return int(db.scalar(stmt) or 0)

    def total_fees(self, db: Session, portfolio_id: int):
        stmt = (
            select(func.coalesce(func.sum(PortfolioTrade.fees), 0))
            .select_from(PortfolioTrade)
            .where(PortfolioTrade.portfolio_id == portfolio_id)
        )
        return db.scalar(stmt) or 0


trade_repository = TradeRepository()