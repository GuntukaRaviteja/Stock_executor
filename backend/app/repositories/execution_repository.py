"""Repositories for the Phase 16 execution records: fills and audit events.

Both repositories are **insert-only by design**. No update method and no delete method exists
for either table, and that absence is the guarantee: a fill cannot be rewritten, and a recorded
event cannot be un-recorded. The rest of the project already draws this line for trades
(:mod:`app.repositories.trade_repository`), and Phase 16 extends it to the whole execution
lifecycle.

Reads exist for reporting; writes happen only through the execution service, inside the same
transaction as the position and cash changes the fill caused.
"""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.execution import PortfolioExecution, PortfolioExecutionEvent


class ExecutionRepository:
    """Database repository for individual order fills."""

    def add(self, db: Session, execution: PortfolioExecution) -> PortfolioExecution:
        db.add(execution)
        db.flush()
        return execution

    def add_all(self, db: Session, executions: List[PortfolioExecution]) -> List[PortfolioExecution]:
        for execution in executions:
            db.add(execution)
        db.flush()
        return executions

    def get_by_id(self, db: Session, execution_id: int) -> Optional[PortfolioExecution]:
        return db.scalars(
            select(PortfolioExecution).where(PortfolioExecution.id == execution_id)
        ).first()

    def get_by_reference(
        self, db: Session, portfolio_id: int, execution_reference: str
    ) -> Optional[PortfolioExecution]:
        """The fill with this adapter-assigned reference, if one is already recorded.

        The lookup the idempotency check uses. A repeat execution request produces the same
        adapter references — they are derived from the order's idempotency key — so finding a row
        here is how a duplicate booking is caught before it happens rather than after.
        """
        return db.scalars(
            select(PortfolioExecution).where(
                PortfolioExecution.portfolio_id == portfolio_id,
                PortfolioExecution.execution_reference == execution_reference,
            )
        ).first()

    def get_by_order_id(self, db: Session, order_id: int) -> List[PortfolioExecution]:
        """Every fill of one order, oldest first."""
        stmt = (
            select(PortfolioExecution)
            .where(PortfolioExecution.order_id == order_id)
            .order_by(PortfolioExecution.id)
        )
        return list(db.scalars(stmt).all())

    def list_history(
        self,
        db: Session,
        portfolio_id: int,
        skip: int = 0,
        limit: int = 100,
        order_id: Optional[int] = None,
        stock_id: Optional[int] = None,
        execution_plan_id: Optional[str] = None,
    ) -> List[PortfolioExecution]:
        """Fill history, newest first."""
        stmt = select(PortfolioExecution).where(PortfolioExecution.portfolio_id == portfolio_id)
        if order_id is not None:
            stmt = stmt.where(PortfolioExecution.order_id == order_id)
        if stock_id is not None:
            stmt = stmt.where(PortfolioExecution.stock_id == stock_id)
        if execution_plan_id is not None:
            stmt = stmt.where(PortfolioExecution.execution_plan_id == execution_plan_id)
        stmt = stmt.order_by(
            PortfolioExecution.executed_at.desc(), PortfolioExecution.id.desc()
        ).offset(skip).limit(limit)
        return list(db.scalars(stmt).all())

    def count(
        self,
        db: Session,
        portfolio_id: int,
        order_id: Optional[int] = None,
        stock_id: Optional[int] = None,
        execution_plan_id: Optional[str] = None,
    ) -> int:
        """Total matching the same filters as :meth:`list_history`."""
        from sqlalchemy import func

        stmt = (
            select(func.count())
            .select_from(PortfolioExecution)
            .where(PortfolioExecution.portfolio_id == portfolio_id)
        )
        if order_id is not None:
            stmt = stmt.where(PortfolioExecution.order_id == order_id)
        if stock_id is not None:
            stmt = stmt.where(PortfolioExecution.stock_id == stock_id)
        if execution_plan_id is not None:
            stmt = stmt.where(PortfolioExecution.execution_plan_id == execution_plan_id)
        return int(db.scalar(stmt) or 0)


class ExecutionEventRepository:
    """Database repository for the immutable execution audit trail.

    Append-only. There is deliberately no ``update`` and no ``delete``: an audit event that can
    be rewritten is not an audit trail, and the method would be the only thing standing between
    the log and a correction.
    """

    def add(self, db: Session, event: PortfolioExecutionEvent) -> PortfolioExecutionEvent:
        db.add(event)
        db.flush()
        return event

    def add_all(
        self, db: Session, events: List[PortfolioExecutionEvent]
    ) -> List[PortfolioExecutionEvent]:
        for event in events:
            db.add(event)
        db.flush()
        return events

    def list_history(
        self,
        db: Session,
        portfolio_id: int,
        skip: int = 0,
        limit: int = 100,
        event_type: Optional[str] = None,
        correlation_id: Optional[str] = None,
        execution_plan_id: Optional[str] = None,
    ) -> List[PortfolioExecutionEvent]:
        """Audit history, oldest first.

        Ascending on purpose: this list is read as the story of a run, and a descending log has
        to be reversed in the reader's head to be understood.
        """
        stmt = select(PortfolioExecutionEvent).where(
            PortfolioExecutionEvent.portfolio_id == portfolio_id
        )
        if event_type is not None:
            stmt = stmt.where(PortfolioExecutionEvent.event_type == event_type)
        if correlation_id is not None:
            stmt = stmt.where(PortfolioExecutionEvent.correlation_id == correlation_id)
        if execution_plan_id is not None:
            stmt = stmt.where(PortfolioExecutionEvent.execution_plan_id == execution_plan_id)
        stmt = stmt.order_by(
            PortfolioExecutionEvent.timestamp.asc(), PortfolioExecutionEvent.id.asc()
        ).offset(skip).limit(limit)
        return list(db.scalars(stmt).all())

    def count(
        self,
        db: Session,
        portfolio_id: int,
        event_type: Optional[str] = None,
        correlation_id: Optional[str] = None,
        execution_plan_id: Optional[str] = None,
    ) -> int:
        """Total matching the same filters as :meth:`list_history`."""
        from sqlalchemy import func

        stmt = (
            select(func.count())
            .select_from(PortfolioExecutionEvent)
            .where(PortfolioExecutionEvent.portfolio_id == portfolio_id)
        )
        if event_type is not None:
            stmt = stmt.where(PortfolioExecutionEvent.event_type == event_type)
        if correlation_id is not None:
            stmt = stmt.where(PortfolioExecutionEvent.correlation_id == correlation_id)
        if execution_plan_id is not None:
            stmt = stmt.where(PortfolioExecutionEvent.execution_plan_id == execution_plan_id)
        return int(db.scalar(stmt) or 0)


execution_repository = ExecutionRepository()
execution_event_repository = ExecutionEventRepository()