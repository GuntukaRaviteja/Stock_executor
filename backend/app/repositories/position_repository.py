from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.portfolio import PortfolioPosition


class PositionRepository:
    """Database repository for open portfolio positions.

    Long-only: a row exists only while ``quantity > 0``. Closing a position deletes the
    row; the realised P&L stays on the portfolio and in the trade history.
    """

    def add(self, db: Session, position: PortfolioPosition) -> PortfolioPosition:
        db.add(position)
        db.flush()
        return position

    def get(self, db: Session, portfolio_id: int, stock_id: int) -> Optional[PortfolioPosition]:
        stmt = select(PortfolioPosition).where(
            PortfolioPosition.portfolio_id == portfolio_id,
            PortfolioPosition.stock_id == stock_id,
        )
        return db.scalars(stmt).first()

    def list_open(
        self, db: Session, portfolio_id: int, stock_id: Optional[int] = None
    ) -> List[PortfolioPosition]:
        stmt = select(PortfolioPosition).where(
            PortfolioPosition.portfolio_id == portfolio_id,
            PortfolioPosition.quantity > 0,
        )
        if stock_id is not None:
            stmt = stmt.where(PortfolioPosition.stock_id == stock_id)
        stmt = stmt.order_by(PortfolioPosition.stock_id)
        return list(db.scalars(stmt).all())

    def total_market_value(self, db: Session, portfolio_id: int) -> Decimal:
        """Sum of the persisted market values of every open position."""
        from sqlalchemy import func

        stmt = select(func.coalesce(func.sum(PortfolioPosition.market_value), 0)).where(
            PortfolioPosition.portfolio_id == portfolio_id,
            PortfolioPosition.quantity > 0,
        )
        return db.scalar(stmt) or 0

    def delete(self, db: Session, position: PortfolioPosition) -> None:
        db.delete(position)
        db.flush()


position_repository = PositionRepository()