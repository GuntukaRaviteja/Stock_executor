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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class StockPrice(Base):
    __tablename__ = "stock_prices"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    stock_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    price_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    open_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 4), nullable=True)
    high_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 4), nullable=True)
    low_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 4), nullable=True)
    close_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 4), nullable=True)
    adjusted_close: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=True
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, onupdate=func.now(), nullable=True
    )

    # Relationships
    stock = relationship("Stock", back_populates="prices")

    __table_args__ = (
        UniqueConstraint("stock_id", "price_date", name="uq_stock_price_date"),
    )

    def __repr__(self) -> str:
        return f"<StockPrice(id={self.id}, stock_id={self.stock_id}, date={self.price_date}, close={self.close_price})>"
