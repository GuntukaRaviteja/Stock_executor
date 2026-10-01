from datetime import datetime
from typing import List, Optional
from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class Stock(Base):
    __tablename__ = "stocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    exchange: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )

    # Relationships
    prices: Mapped[List["StockPrice"]] = relationship(
        "StockPrice",
        back_populates="stock",
        cascade="all, delete-orphan",
        order_by="desc(StockPrice.price_date)",
    )
    events: Mapped[List["StockEvent"]] = relationship(
        "StockEvent",
        back_populates="stock",
        cascade="all, delete-orphan",
        order_by="desc(StockEvent.event_date)",
    )
    company_profile: Mapped[Optional["Company"]] = relationship(
        "Company",
        back_populates="stock",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Stock(id={self.id}, symbol='{self.symbol}', company_name='{self.company_name}')>"
