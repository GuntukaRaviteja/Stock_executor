from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.portfolio import PortfolioOrder


class OrderRepository:
    """Database repository for simulated portfolio orders.

    Orders are never mutated after creation except to be filled or rejected, which always
    happens inside the same transaction as the trade and the portfolio state change.
    """

    def add(self, db: Session, order: PortfolioOrder) -> PortfolioOrder:
        db.add(order)
        db.flush()
        return order

    def get_by_id(self, db: Session, order_id: int) -> Optional[PortfolioOrder]:
        stmt = select(PortfolioOrder).where(PortfolioOrder.id == order_id)
        return db.scalars(stmt).first()

    def get_by_idempotency_key(
        self, db: Session, portfolio_id: int, idempotency_key: str
    ) -> Optional[PortfolioOrder]:
        stmt = select(PortfolioOrder).where(
            PortfolioOrder.portfolio_id == portfolio_id,
            PortfolioOrder.idempotency_key == idempotency_key,
        )
        return db.scalars(stmt).first()

    def get_by_strategy_event(
        self, db: Session, portfolio_id: int, strategy_event_id: str
    ) -> Optional[PortfolioOrder]:
        stmt = select(PortfolioOrder).where(
            PortfolioOrder.portfolio_id == portfolio_id,
            PortfolioOrder.strategy_event_id == strategy_event_id,
        )
        stmt = stmt.order_by(PortfolioOrder.id)
        return db.scalars(stmt).first()

    def list_history(
        self,
        db: Session,
        portfolio_id: int,
        skip: int = 0,
        limit: int = 100,
        status: Optional[str] = None,
        stock_id: Optional[int] = None,
    ) -> List[PortfolioOrder]:
        stmt = select(PortfolioOrder).where(PortfolioOrder.portfolio_id == portfolio_id)
        if status is not None:
            stmt = stmt.where(PortfolioOrder.status == status)
        if stock_id is not None:
            stmt = stmt.where(PortfolioOrder.stock_id == stock_id)
        stmt = stmt.order_by(PortfolioOrder.created_at.desc(), PortfolioOrder.id.desc()).offset(skip).limit(limit)
        return list(db.scalars(stmt).all())

    def count(
        self,
        db: Session,
        portfolio_id: int,
        status: Optional[str] = None,
        stock_id: Optional[int] = None,
    ) -> int:
        """Total matching the same filters as :meth:`list_history`.

        ``count`` and ``list_history`` must agree, so a caller paging through a filtered
        history is told the size of the set it is actually paging through.
        """
        from sqlalchemy import func

        stmt = (
            select(func.count())
            .select_from(PortfolioOrder)
            .where(PortfolioOrder.portfolio_id == portfolio_id)
        )
        if status is not None:
            stmt = stmt.where(PortfolioOrder.status == status)
        if stock_id is not None:
            stmt = stmt.where(PortfolioOrder.stock_id == stock_id)
        return int(db.scalar(stmt) or 0)


order_repository = OrderRepository()