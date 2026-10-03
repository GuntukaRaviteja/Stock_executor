from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.portfolio import PortfolioSnapshot


class PortfolioSnapshotRepository:
    """Database repository for portfolio equity-curve snapshots.

    Snapshots are created on request by the service; Phase 9 runs no background scheduler.
    """

    def add(self, db: Session, snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
        db.add(snapshot)
        db.flush()
        return snapshot

    def get_by_id(self, db: Session, snapshot_id: int) -> Optional[PortfolioSnapshot]:
        stmt = select(PortfolioSnapshot).where(PortfolioSnapshot.id == snapshot_id)
        return db.scalars(stmt).first()

    def list_curve(
        self,
        db: Session,
        portfolio_id: int,
        skip: int = 0,
        limit: int = 100,
        descending: bool = False,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[PortfolioSnapshot]:
        """The recorded curve, oldest first, optionally bounded by timestamp.

        ``end_date`` is inclusive of the whole day, so ``end_date=2026-01-31T00:00`` still
        returns that day's points rather than silently excluding them.
        """
        stmt = select(PortfolioSnapshot).where(PortfolioSnapshot.portfolio_id == portfolio_id)
        if start_date is not None:
            stmt = stmt.where(PortfolioSnapshot.timestamp >= start_date)
        if end_date is not None:
            stmt = stmt.where(PortfolioSnapshot.timestamp <= _end_of_day(end_date))
        if descending:
            order = (PortfolioSnapshot.timestamp.desc(), PortfolioSnapshot.id.desc())
        else:
            order = (PortfolioSnapshot.timestamp.asc(), PortfolioSnapshot.id.asc())
        stmt = stmt.order_by(*order).offset(skip).limit(limit)
        return list(db.scalars(stmt).all())

    def count(
        self,
        db: Session,
        portfolio_id: int,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(PortfolioSnapshot)
            .where(PortfolioSnapshot.portfolio_id == portfolio_id)
        )
        if start_date is not None:
            stmt = stmt.where(PortfolioSnapshot.timestamp >= start_date)
        if end_date is not None:
            stmt = stmt.where(PortfolioSnapshot.timestamp <= _end_of_day(end_date))
        return int(db.scalar(stmt) or 0)

    def max_equity(self, db: Session, portfolio_id: int) -> Optional[Decimal]:
        """Highest equity ever recorded for a portfolio, or ``None`` when no snapshot exists."""
        stmt = (
            select(func.max(PortfolioSnapshot.equity))
            .select_from(PortfolioSnapshot)
            .where(PortfolioSnapshot.portfolio_id == portfolio_id)
        )
        return db.scalar(stmt)

    def latest_before(
        self, db: Session, portfolio_id: int, moment: datetime
    ) -> Optional[PortfolioSnapshot]:
        """The most recent snapshot recorded strictly before ``moment``.

        Used to find where the day was when it started: the last point recorded before
        ``moment`` is the portfolio's opening equity for the calendar day ``moment`` falls in.
        ``timestamp`` and then ``id`` break ties, so two snapshots recorded in the same
        microsecond still resolve to one deterministic row.
        """
        stmt = (
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.portfolio_id == portfolio_id)
            .where(PortfolioSnapshot.timestamp < moment)
            .order_by(PortfolioSnapshot.timestamp.desc(), PortfolioSnapshot.id.desc())
            .limit(1)
        )
        return db.scalars(stmt).first()

    def earliest_on(
        self, db: Session, portfolio_id: int, day: datetime
    ) -> Optional[PortfolioSnapshot]:
        """The first snapshot recorded on ``day``'s calendar date, or ``None``.

        The counterpart to :meth:`latest_before` for a portfolio that only started recording
        today: its first point of the day is the closest thing it has to an opening equity.
        """
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = (
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.portfolio_id == portfolio_id)
            .where(PortfolioSnapshot.timestamp >= start)
            .order_by(PortfolioSnapshot.timestamp.asc(), PortfolioSnapshot.id.asc())
            .limit(1)
        )
        return db.scalars(stmt).first()


def _end_of_day(value: datetime) -> datetime:
    """The last instant of ``value``'s day, so a date filter includes that whole day."""
    if value.hour or value.minute or value.second or value.microsecond:
        return value
    return value.replace(hour=23, minute=59, second=59, microsecond=999999)


portfolio_snapshot_repository = PortfolioSnapshotRepository()