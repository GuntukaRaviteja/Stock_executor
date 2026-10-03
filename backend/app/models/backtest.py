from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class Backtest(Base):
    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    stock_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.0")
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    final_equity: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    total_return_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    cagr_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    peak_equity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    trough_equity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    annualized_volatility_pct: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 4), nullable=True
    )
    sharpe_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    sortino_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    win_rate_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    profit_factor: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    number_of_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    winning_trades: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    losing_trades: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    average_trade_return_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    average_winning_trade_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    average_losing_trade_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    average_holding_period: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    best_trade_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    worst_trade_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    benchmark_return_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    benchmark_final_equity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    strategy_vs_benchmark_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )

    # Relationships
    stock = relationship("Stock", back_populates="backtests")
    trades = relationship(
        "BacktestTrade",
        back_populates="backtest",
        cascade="all, delete-orphan",
        order_by="BacktestTrade.entry_date",
    )
    equity_curve = relationship(
        "BacktestEquityPoint",
        back_populates="backtest",
        cascade="all, delete-orphan",
        order_by="BacktestEquityPoint.date",
    )

    __table_args__ = (
        Index("ix_backtest_stock_date", "stock_id", "start_date", "end_date"),
    )

    def __repr__(self) -> str:
        return f"<Backtest(id={self.id}, stock_id={self.stock_id}, strategy={self.strategy_name}, return={self.total_return_pct}%)>"


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    backtest_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stock_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    exit_date: Mapped[date] = mapped_column(Date, nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    commission: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    slippage: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    return_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    holding_period: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(50), nullable=False)

    # Relationships
    backtest = relationship("Backtest", back_populates="trades")
    stock = relationship("Stock")

    def __repr__(self) -> str:
        return f"<BacktestTrade(id={self.id}, backtest_id={self.backtest_id}, entry={self.entry_date}, exit={self.exit_date}, pnl={self.net_pnl})>"


class BacktestEquityPoint(Base):
    __tablename__ = "backtest_equity"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    backtest_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("backtests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    position_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    equity: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    # Relationships
    backtest = relationship("Backtest", back_populates="equity_curve")

    __table_args__ = (
        Index("ix_backtest_equity_backtest_date", "backtest_id", "date", unique=True),
    )

    def __repr__(self) -> str:
        return f"<BacktestEquityPoint(backtest_id={self.backtest_id}, date={self.date}, equity={self.equity})>"