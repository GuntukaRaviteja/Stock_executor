from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio


class PortfolioRepository:
    """Database repository for Portfolio entities.

    Persistence only: no portfolio calculation, fee or risk decision happens here.
    """

    def add(self, db: Session, portfolio: Portfolio) -> Portfolio:
        """Stage a new portfolio without committing, so the caller owns the transaction."""
        db.add(portfolio)
        db.flush()
        return portfolio

    def get_by_id(self, db: Session, portfolio_id: int) -> Optional[Portfolio]:
        stmt = select(Portfolio).where(Portfolio.id == portfolio_id)
        return db.scalars(stmt).first()

    def get_by_id_for_update(self, db: Session, portfolio_id: int) -> Optional[Portfolio]:
        """Load a portfolio and hold a row lock on it until the transaction ends.

        Every order path takes this lock before it reads cash or positions. Two simultaneous
        BUY requests therefore serialise on the portfolio row instead of both reading the same
        balance and each spending it: the second one blocks, and once it proceeds it reads the
        cash the first one committed. This is a plain database row lock rather than a
        distributed lock, because the portfolio row is the only thing both orders contend on.

        Dialects without row-level locking (SQLite, used by the test suite) ignore the clause,
        so the code path is identical everywhere; the guarantee comes from the database that
        actually supports it.
        """
        stmt = (
            select(Portfolio)
            .where(Portfolio.id == portfolio_id)
            .with_for_update()
        )
        return db.scalars(stmt).first()

    def list_all(self, db: Session, skip: int = 0, limit: int = 100) -> List[Portfolio]:
        stmt = select(Portfolio).order_by(Portfolio.id.desc()).offset(skip).limit(limit)
        return list(db.scalars(stmt).all())

    def update(self, db: Session, portfolio: Portfolio, update_data: dict) -> Portfolio:
        for field, value in update_data.items():
            if hasattr(portfolio, field):
                setattr(portfolio, field, value)
        db.flush()
        return portfolio


portfolio_repository = PortfolioRepository()