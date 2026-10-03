"""Phase 9 SQLAlchemy models — virtual paper-trading portfolios.

These tables hold **simulated** state only. Nothing in this module connects to a broker,
stores brokerage credentials, or represents a real financial transaction. Every monetary
column is ``Numeric`` (``DECIMAL``): floating point is never used for cash, prices, fees
or P&L.

Table layout
------------
``portfolios``            one virtual account: cash, realised P&L, derived equity/P&L
``portfolio_positions``   open long positions, one row per (portfolio, stock)
``portfolio_orders``      every submitted order, including rejected ones (audit trail)
``portfolio_trades``      one immutable execution record per filled order
``portfolio_snapshots``   point-in-time equity curve used for reporting and drawdown
``portfolio_transactions`` append-only cash ledger: fills plus deposits and withdrawals
"""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import DEFAULT_CURRENCY, default_currency
from app.core.database import Base

#: Stored on every portfolio and order so a consumer can never mistake simulated state
#: for a live brokerage position. Phase 9 only ever writes this value.
PAPER_TRADING_MODE = "PAPER"

#: Currency a portfolio falls back to when a caller does not state one, read once at import.
#: Phase 12 stores the currency on the portfolio so every monetary figure of a portfolio is
#: unambiguous; the engine performs no cross-currency conversion, so a portfolio's figures are
#: only ever in its own currency.
#:
#: The column's Python-side default is :func:`default_currency`, which resolves the setting on
#: every call; this module-level name exists for the SQL ``server_default``, which must be a
#: literal, and for the migration that backfills existing rows.
DEFAULT_PORTFOLIO_CURRENCY = DEFAULT_CURRENCY


def _pk() -> BigInteger:
    """Portable primary-key type: BIGINT on MySQL, INTEGER on SQLite (tests)."""
    return BigInteger().with_variant(Integer, "sqlite")


class Portfolio(Base):
    """A virtual paper-trading account."""

    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(_pk(), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    trading_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PAPER_TRADING_MODE, server_default=PAPER_TRADING_MODE
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE", index=True)
    #: ISO-4217 code every monetary figure of this portfolio is denominated in. The engine never
    #: converts between currencies, so this states the unit rather than enabling conversion.
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=default_currency,
        server_default=DEFAULT_PORTFOLIO_CURRENCY,
    )

    initial_capital: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    current_cash: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    #: Gross mark-to-market P&L of open positions (quantity * (price - average entry)).
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    #: equity - initial_capital. Authoritative portfolio P&L: net of every fee paid.
    total_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    #: cash + sum(position market values). Denormalised so a portfolio is readable without
    #: aggregating positions, and recomputed on every mutation.
    equity: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))

    #: Serialised TradingCostConfig; keeps every fill reproducible from the record.
    cost_config_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Serialised RiskLimits; ``None`` entries mean the corresponding rule is disabled.
    risk_config_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Serialised Phase 11 ``PortfolioAllocationConfig``: sizing method, position cap, cash
    #: reserve, risk budget, stop-loss, take-profit, rebalancing threshold. Kept separate from
    #: ``risk_config_json`` because the two answer different questions — this one says how the
    #: portfolio is *built*, that one says what a single order may never breach.
    allocation_config_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )

    # Relationships
    positions: Mapped[List["PortfolioPosition"]] = relationship(
        "PortfolioPosition",
        back_populates="portfolio",
        cascade="all, delete-orphan",
    )
    orders: Mapped[List["PortfolioOrder"]] = relationship(
        "PortfolioOrder",
        back_populates="portfolio",
        cascade="all, delete-orphan",
    )
    trades: Mapped[List["PortfolioTrade"]] = relationship(
        "PortfolioTrade",
        back_populates="portfolio",
        cascade="all, delete-orphan",
    )
    snapshots: Mapped[List["PortfolioSnapshot"]] = relationship(
        "PortfolioSnapshot",
        back_populates="portfolio",
        cascade="all, delete-orphan",
    )
    transactions: Mapped[List["PortfolioTransaction"]] = relationship(
        "PortfolioTransaction",
        back_populates="portfolio",
        cascade="all, delete-orphan",
    )

    __table_args__ = (Index("ix_portfolio_status_created", "status", "created_at"),)

    def __repr__(self) -> str:
        return (
            f"<Portfolio(id={self.id}, name='{self.name}', status={self.status}, "
            f"cash={self.current_cash}, equity={self.equity})>"
        )


class PortfolioPosition(Base):
    """An open long position.

    Long-only: ``quantity`` is always positive and a row is deleted once it reaches zero.
    ``average_entry_price`` is the quantity-weighted average of the execution prices of the
    buys that built the position (fees are tracked separately in ``entry_fees_total`` so the
    average price stays a clean average of execution prices).
    """

    __tablename__ = "portfolio_positions"

    id: Mapped[int] = mapped_column(_pk(), primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        _pk(), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stock_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stocks.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    average_entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    #: Entry fees charged on the buys still held, allocated pro rata on a partial sell.
    entry_fees_total: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    #: Realised P&L booked by sells of this stock in this portfolio (never decreases).
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    #: Last known mark. ``None`` until a valuation or a fill establishes one.
    current_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4), nullable=True)
    market_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    unrealized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)

    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )

    # Relationships
    portfolio = relationship("Portfolio", back_populates="positions")
    stock = relationship("Stock")

    __table_args__ = (UniqueConstraint("portfolio_id", "stock_id", name="uq_position_portfolio_stock"),)

    def __repr__(self) -> str:
        return (
            f"<PortfolioPosition(portfolio_id={self.portfolio_id}, stock_id={self.stock_id}, "
            f"quantity={self.quantity}, avg={self.average_entry_price})>"
        )


class PortfolioOrder(Base):
    """A simulated order.

    Every submission is persisted, including rejected ones, so order history always
    explains why something did or did not execute. A rejected order never changes cash,
    positions or realised P&L.
    """

    __tablename__ = "portfolio_orders"

    id: Mapped[int] = mapped_column(_pk(), primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        _pk(), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stock_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stocks.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    trading_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PAPER_TRADING_MODE, server_default=PAPER_TRADING_MODE
    )

    side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(20), nullable=False, default="MARKET")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="MANUAL")

    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    execution_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4), nullable=True)
    gross_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    fees: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    net_cash_impact: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    realized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)

    rejection_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    #: When the decision behind this order was taken. Recorded separately from
    #: ``executed_at`` so a historical fill can be audited: the signal timestamp must never
    #: be later than the execution timestamp. ``None`` for a manual order, where the caller
    #: states the execution price directly.
    signal_as_of: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    #: Which price of which bar the fill reference came from (``OPEN`` or ``CLOSE``).
    execution_price_policy: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    #: Free-form audit metadata (strategy score, run id, ...) stored verbatim as JSON.
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    #: Deterministic de-duplication key derived from a strategy event (Phase 7/8 signal).
    strategy_event_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    #: Caller-supplied idempotency key. Repeat submissions return the original order.
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    #: Phase 16 execution lifecycle. All six columns are written only by the execution layer;
    #: a Phase 9 order leaves them at their defaults, so the single-shot path is unchanged.
    #:
    #: ``filled_quantity`` / ``remaining_quantity`` are what make a partial fill expressible:
    #: ``portfolio_trades`` holds exactly one row per order and therefore cannot record an order
    #: that filled in two parts, whereas this pair can. ``remaining_quantity`` is the *whole*
    #: outstanding quantity of the order, not just the last fill's remainder.
    filled_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    remaining_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Identifier the execution adapter returned for this order. For the simulated adapter it is
    #: a deterministic id derived from the idempotency key, which is what a real broker's order
    #: id would occupy; the column is named for the role, not for any particular broker.
    execution_reference: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: Groups every order produced by one execution plan, so a plan and all of its orders can be
    #: read back together.
    execution_plan_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    #: When the order reached the execution adapter, and when it reached a terminal state.
    #: ``executed_at`` remains the time of the *final* fill, so a Phase 9 reader is unaffected.
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)

    # Relationships
    portfolio = relationship("Portfolio", back_populates="orders")
    stock = relationship("Stock")
    trade: Mapped[Optional["PortfolioTrade"]] = relationship(
        "PortfolioTrade", back_populates="order", uselist=False
    )
    #: Phase 16: the individual fills this order produced. Empty for a Phase 9 order, which
    #: records its single fill as a ``PortfolioTrade`` instead.
    executions: Mapped[list] = relationship(
        "PortfolioExecution",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="PortfolioExecution.id",
    )

    __table_args__ = (
        Index("ix_portfolio_order_history", "portfolio_id", "created_at", "id"),
        UniqueConstraint("portfolio_id", "idempotency_key", name="uq_order_portfolio_idempotency"),
    )

    def __repr__(self) -> str:
        return (
            f"<PortfolioOrder(id={self.id}, portfolio_id={self.portfolio_id}, side={self.side}, "
            f"quantity={self.quantity}, status={self.status})>"
        )


class PortfolioTrade(Base):
    """An immutable execution record — one filled order, exactly one trade.

    Written once, inside the same transaction as the order, and never updated afterwards.
    """

    __tablename__ = "portfolio_trades"

    id: Mapped[int] = mapped_column(_pk(), primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        _pk(), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[int] = mapped_column(
        _pk(),
        ForeignKey("portfolio_orders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    stock_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stocks.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    execution_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    gross_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    fees: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    #: Signed effect on cash: negative for a buy, positive for a sell.
    net_cash_impact: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    #: Profit or loss booked by this execution. Always 0 for a buy.
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    executed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), nullable=True, index=True
    )

    # Relationships
    portfolio = relationship("Portfolio", back_populates="trades")
    order = relationship("PortfolioOrder", back_populates="trade")
    stock = relationship("Stock")

    __table_args__ = (Index("ix_portfolio_trade_history", "portfolio_id", "executed_at", "id"),)

    def __repr__(self) -> str:
        return (
            f"<PortfolioTrade(id={self.id}, portfolio_id={self.portfolio_id}, side={self.side}, "
            f"quantity={self.quantity}, realized_pnl={self.realized_pnl})>"
        )


class PortfolioSnapshot(Base):
    """A point-in-time equity-curve point.

    Snapshots are created on request — Phase 9 runs no background scheduler — and form the
    equity curve used for reporting and for the optional drawdown risk rule.
    """

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(_pk(), primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        _pk(), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    cash: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    market_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    total_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    total_fees: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    return_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    #: Signed drawdown from the running peak equity at this point: 0 at a peak, negative
    #: below it. Stored signed because a curve reads naturally as "below the peak".
    drawdown_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("0")
    )
    #: market_value / equity * 100 at this point.
    exposure_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("0")
    )
    #: The running peak this point's drawdown is measured against.
    peak_equity: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )

    # Relationships
    portfolio = relationship("Portfolio", back_populates="snapshots")

    __table_args__ = (Index("ix_portfolio_snapshot_curve", "portfolio_id", "timestamp", "id"),)

    def __repr__(self) -> str:
        return (
            f"<PortfolioSnapshot(id={self.id}, portfolio_id={self.portfolio_id}, "
            f"timestamp={self.timestamp}, equity={self.equity})>"
        )


class PortfolioTransaction(Base):
    """One entry of the append-only portfolio cash ledger (Phase 11).

    The ledger records **every** movement of ``current_cash`` in one place:

    * ``BUY`` / ``SELL`` — one entry per filled simulated order, carrying the same figures as
      the linked ``PortfolioTrade`` plus the resulting cash balance;
    * ``DEPOSIT`` / ``WITHDRAWAL`` — capital added to or taken out of the portfolio. These have
      no order and no trade, which is exactly why they need a ledger of their own rather than
      being inferred from orders.

    Entries are written once, inside the same transaction as the cash movement itself, and are
    never updated or deleted. That is what makes the recorded cash history reproducible: a
    reader can replay every entry from the opening balance and arrive at the current cash
    without trusting any aggregate column.

    ``stock_id``, ``quantity`` and ``price`` are ``NULL`` for a cash movement, and the
    ``portfolio_id + stock_id`` and ``portfolio_id + timestamp`` indexes serve the two questions
    the ledger is actually asked: "what happened to this stock" and "what happened over this
    period".
    """

    __tablename__ = "portfolio_transactions"

    id: Mapped[int] = mapped_column(_pk(), primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        _pk(), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: ``BUY``, ``SELL``, ``DEPOSIT`` or ``WITHDRAWAL``.
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    #: ``NULL`` for a cash movement, which has no stock.
    stock_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("stocks.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    quantity: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4), nullable=True)
    fees: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    gross_value: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    #: Signed effect on cash: negative for a buy and for a withdrawal, positive otherwise.
    net_cash_impact: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    #: P&L booked by this entry. Always 0 for a buy, a deposit and a withdrawal.
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    #: Cash immediately after this entry, so a reader can verify the sequence without replaying.
    cash_after: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    #: The simulated order / execution this entry came from; ``NULL`` for a cash movement.
    order_id: Mapped[Optional[int]] = mapped_column(
        _pk(), ForeignKey("portfolio_orders.id", ondelete="CASCADE"), nullable=True
    )
    trade_id: Mapped[Optional[int]] = mapped_column(
        _pk(), ForeignKey("portfolio_trades.id", ondelete="CASCADE"), nullable=True
    )

    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: Free-form audit metadata stored verbatim as JSON.
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    portfolio = relationship("Portfolio", back_populates="transactions")
    stock = relationship("Stock")

    __table_args__ = (
        Index("ix_portfolio_transaction_stock", "portfolio_id", "stock_id", "timestamp"),
        Index("ix_portfolio_transaction_history", "portfolio_id", "timestamp", "id"),
    )

    def __repr__(self) -> str:
        return (
            f"<PortfolioTransaction(id={self.id}, portfolio_id={self.portfolio_id}, "
            f"type={self.transaction_type}, net_cash_impact={self.net_cash_impact})>"
        )