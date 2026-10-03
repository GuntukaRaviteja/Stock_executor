"""Phase 16 SQLAlchemy models — the execution lifecycle's two missing tables.

Phase 9 already owns orders (``portfolio_orders``), completed transactions
(``portfolio_trades``), positions, snapshots and the cash ledger. Two records that a *planned,
broker-style* execution needs had no home, and both are added here:

``portfolio_executions``      one row per **fill**: what actually happened, at what price

``portfolio_execution_events`` one row per **audit event**: an append-only lifecycle log

Why a fill needs its own table
------------------------------
``portfolio_trades`` is deliberately one row per order (``order_id`` is ``UNIQUE``), because a
trade is the *completed transaction record* of a fully filled order. That is exactly right for
Phase 9, where an order fills once. It cannot express an order that fills in two parts, so a
partial fill has nowhere to go.

An execution is the different concept: the broker-side fact that a quantity of shares changed
hands at a price, at a moment, for a fee. An order may produce many of them, and one of them
may be the only one that ever happens before the order is cancelled. Keeping the two apart is
what lets ``portfolio_trades`` keep its Phase 9 meaning unchanged while Phase 16 records
partial fills faithfully.

Nothing here connects to a brokerage, stores credentials, or represents a real financial
transaction. Every monetary column is ``Numeric``, exactly as in ``app.models.portfolio``.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

from app.models.portfolio import PAPER_TRADING_MODE, _pk


class PortfolioExecution(Base):
    """One fill: a quantity of one stock changing hands at one price, at one moment.

    Written once, inside the same transaction as the position, cash, trade and event rows it
    belongs to, and never updated afterwards. The ``execution_id`` is the adapter's own
    identifier for the fill, which is how a fill found in an adapter's history later is
    recognised as one already recorded here.
    """

    __tablename__ = "portfolio_executions"

    id: Mapped[int] = mapped_column(_pk(), primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        _pk(), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[int] = mapped_column(
        _pk(), ForeignKey("portfolio_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stock_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stocks.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    trading_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PAPER_TRADING_MODE, server_default=PAPER_TRADING_MODE
    )

    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    execution_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    gross_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    fees: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    #: Signed effect on cash: negative for a buy, positive for a sell.
    net_cash_impact: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    #: P&L booked by this fill. Always 0 for a buy; net of allocated entry and exit fees on a
    #: sell, following the Phase 9 accounting convention.
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )

    #: Adapter-assigned id of this fill, and of the order that produced it.
    execution_reference: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Cash immediately after this fill, so a reader can verify the sequence without replaying.
    cash_after: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    #: Position quantity immediately after this fill.
    quantity_after: Mapped[int] = mapped_column(BigInteger, nullable=False)

    executed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )
    #: Groups the fills produced by one execution plan.
    execution_plan_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # Relationships
    order = relationship("PortfolioOrder", back_populates="executions")
    stock = relationship("Stock")

    __table_args__ = (
        # One adapter fill id per portfolio: the persistence-level guarantee that the same
        # fill cannot be booked twice, whatever the caller does.
        Index(
            "uq_execution_portfolio_reference",
            "portfolio_id",
            "execution_reference",
            unique=True,
        ),
        Index("ix_execution_history", "portfolio_id", "executed_at", "id"),
        Index("ix_execution_order_history", "order_id", "executed_at", "id"),
    )

    def __repr__(self) -> str:
        return (
            f"<PortfolioExecution(id={self.id}, portfolio_id={self.portfolio_id}, "
            f"order_id={self.order_id}, side={self.side}, quantity={self.quantity}, "
            f"price={self.execution_price})>"
        )


class PortfolioExecutionEvent(Base):
    """One immutable entry in a portfolio's execution audit trail.

    The log is append-only by construction: this repository exposes no update and no delete, and
    an event that has been recorded is never rewritten. A refused order is recorded exactly as
    a filled one is — that is the point of the log, because "why did nothing happen" is
    otherwise unanswerable after the fact.

    ``details_json`` holds the figures behind the event verbatim. Decimals are stored as
    strings so a value read back later is the value that was recorded.
    """

    __tablename__ = "portfolio_execution_events"

    id: Mapped[int] = mapped_column(_pk(), primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        _pk(), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Stable machine-readable event name (``ORDER_FILLED``, ``RISK_REJECTED``, ...).
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    #: What the event is about: ``plan``, ``order``, ``fill``, ``position``, ``cash`` or
    #: ``reconciliation``.
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Id of the entity the event is about (order id, fill id, plan id, stock id).
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: Links every event of one execution run together, so a plan, its orders and its
    #: reconciliation are readable as a single story.
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    execution_plan_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )

    # Relationships
    portfolio = relationship("Portfolio")

    __table_args__ = (
        Index("ix_execution_event_history", "portfolio_id", "timestamp", "id"),
        Index("ix_execution_event_entity", "portfolio_id", "entity_type", "entity_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<PortfolioExecutionEvent(id={self.id}, portfolio_id={self.portfolio_id}, "
            f"type={self.event_type}, entity={self.entity_type}:{self.entity_id})>"
        )


__all__ = ["PortfolioExecution", "PortfolioExecutionEvent"]