"""Data access for the append-only portfolio transaction ledger (Phase 11).

Like :mod:`app.repositories.trade_repository`, this repository is **insert-only**: no update or
delete method exists, because a ledger entry that can be edited is not a ledger.

Reads are ordered newest first and always carry a deterministic secondary key (``id``), so two
entries written in the same millisecond — or, in a test, with an identical timestamp — still
come back in a stable order.
"""

from datetime import datetime, time
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.portfolio import PortfolioTransaction
from app.services.backtest.models import quantize_money

#: The ledger entries that represent capital crossing the portfolio boundary rather than a
#: trade. Named here, once, because "what is external flow" is an accounting decision and every
#: caller that needs it must mean the same two rows.
_EXTERNAL_FLOW_TYPES = ("DEPOSIT", "WITHDRAWAL")

_MIDNIGHT = time.min


class PortfolioTransactionRepository:
    """Database repository for the immutable portfolio cash ledger."""

    def add(self, db: Session, transaction: PortfolioTransaction) -> PortfolioTransaction:
        db.add(transaction)
        db.flush()
        return transaction

    def get_by_id(
        self, db: Session, portfolio_id: int, transaction_id: int
    ) -> Optional[PortfolioTransaction]:
        stmt = select(PortfolioTransaction).where(
            PortfolioTransaction.portfolio_id == portfolio_id,
            PortfolioTransaction.id == transaction_id,
        )
        return db.scalars(stmt).first()

    def list_history(
        self,
        db: Session,
        portfolio_id: int,
        skip: int = 0,
        limit: int = 100,
        stock_id: Optional[int] = None,
        transaction_type: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[PortfolioTransaction]:
        """Ledger entries, newest first.

        ``id`` is the tie-breaker so entries sharing a timestamp keep a stable order, which is
        what makes a replay of the ledger reproducible.
        """
        stmt = select(PortfolioTransaction).where(
            PortfolioTransaction.portfolio_id == portfolio_id
        )
        stmt = _apply_filters(
            stmt,
            stock_id=stock_id,
            transaction_type=transaction_type,
            start_date=start_date,
            end_date=end_date,
        )
        stmt = (
            stmt.order_by(
                PortfolioTransaction.timestamp.desc(), PortfolioTransaction.id.desc()
            )
            .offset(skip)
            .limit(limit)
        )
        return list(db.scalars(stmt).all())

    def list_chronological(
        self,
        db: Session,
        portfolio_id: int,
        skip: int = 0,
        limit: int = 1000,
        stock_id: Optional[int] = None,
    ) -> List[PortfolioTransaction]:
        """Ledger entries oldest first — the order a balance replay has to walk them in."""
        stmt = select(PortfolioTransaction).where(
            PortfolioTransaction.portfolio_id == portfolio_id
        )
        if stock_id is not None:
            stmt = stmt.where(PortfolioTransaction.stock_id == stock_id)
        stmt = (
            stmt.order_by(PortfolioTransaction.timestamp.asc(), PortfolioTransaction.id.asc())
            .offset(skip)
            .limit(limit)
        )
        return list(db.scalars(stmt).all())

    def count(
        self,
        db: Session,
        portfolio_id: int,
        stock_id: Optional[int] = None,
        transaction_type: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> int:
        """Total matching the same filters as the history query."""
        stmt = (
            select(func.count())
            .select_from(PortfolioTransaction)
            .where(PortfolioTransaction.portfolio_id == portfolio_id)
        )
        stmt = _apply_filters(
            stmt,
            stock_id=stock_id,
            transaction_type=transaction_type,
            start_date=start_date,
            end_date=end_date,
        )
        return int(db.scalar(stmt) or 0)

    def count_by_type(self, db: Session, portfolio_id: int) -> dict:
        """Entry count per transaction type, as ``{type: count}``."""
        stmt = (
            select(
                PortfolioTransaction.transaction_type,
                func.count(),
            )
            .select_from(PortfolioTransaction)
            .where(PortfolioTransaction.portfolio_id == portfolio_id)
            .group_by(PortfolioTransaction.transaction_type)
        )
        return {row[0]: int(row[1]) for row in db.execute(stmt).all()}

    def net_cash_impact(self, db: Session, portfolio_id: int) -> Decimal:
        """Sum of every entry's signed cash movement.

        Adding ``initial_capital`` to this reproduces the portfolio's current cash exactly,
        which is the invariant that makes the ledger auditable.
        """
        stmt = (
            select(func.coalesce(func.sum(PortfolioTransaction.net_cash_impact), 0))
            .select_from(PortfolioTransaction)
            .where(PortfolioTransaction.portfolio_id == portfolio_id)
        )
        return Decimal(str(db.scalar(stmt) or 0))

    def external_flow(self, db: Session, portfolio_id: int) -> Decimal:
        """Signed sum of the capital **moved in or out of** the portfolio.

        This is deliberately *not* the whole ledger: deposits and withdrawals only, never the
        BUY and SELL entries. It is what separates money that arrived from money that was earned,
        and therefore what stops a deposit from being reported as a profit. With no cash
        movements it is zero, which is why a portfolio that has only traded keeps exactly the
        figures it had before the ledger existed.
        """
        stmt = (
            select(func.coalesce(func.sum(PortfolioTransaction.net_cash_impact), 0))
            .select_from(PortfolioTransaction)
            .where(PortfolioTransaction.portfolio_id == portfolio_id)
            .where(PortfolioTransaction.transaction_type.in_(_EXTERNAL_FLOW_TYPES))
        )
        return quantize_money(Decimal(str(db.scalar(stmt) or 0)))


def _apply_filters(
    stmt,
    *,
    stock_id: Optional[int],
    transaction_type: Optional[str],
    start_date: Optional[datetime],
    end_date: Optional[datetime],
):
    """Narrow a ledger query.

    ``end_date`` is inclusive of a whole day: an upper bound at exactly midnight is treated as
    the *end* of that day rather than its first instant. Every listing endpoint in this project
    documents "a bare date includes the day", and a client sending ``end_date=2026-01-15`` means
    "everything up to and including the 15th" — the overwhelmingly common reading. The cost is
    that midnight cannot be used as an exclusive upper bound, which no filter needs.
    """
    if stock_id is not None:
        stmt = stmt.where(PortfolioTransaction.stock_id == stock_id)
    if transaction_type is not None:
        stmt = stmt.where(PortfolioTransaction.transaction_type == transaction_type)
    if start_date is not None:
        stmt = stmt.where(PortfolioTransaction.timestamp >= _start_of(start_date))
    if end_date is not None:
        stmt = stmt.where(PortfolioTransaction.timestamp <= _end_of(end_date))
    return stmt


def _start_of(value: datetime) -> datetime:
    """The first instant of a day, so a lower bound covers the whole day from the start."""
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)


def _end_of(value: datetime) -> datetime:
    """The last instant of a day, so an upper bound covers the whole day.

    A caller that passed a bare date — which the request model has already widened to midnight —
    gets the end of that day; a caller that passed a real time keeps it.
    """
    if not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, 23, 59, 59, 999999)
    if value.time() == _MIDNIGHT:
        return value.replace(hour=23, minute=59, second=59, microsecond=999999)
    return value


portfolio_transaction_repository = PortfolioTransactionRepository()