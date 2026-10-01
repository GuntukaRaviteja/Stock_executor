from datetime import date, datetime
from typing import Optional
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class StockEvent(Base):
    __tablename__ = "stock_events"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    stock_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    event_title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    event_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    event_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    impact: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    source_raw_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("raw_ai_data.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )

    # Relationships
    stock = relationship("Stock", back_populates="events")
    raw_source = relationship("RawAIData")

    __table_args__ = (
        Index(
            "uq_stock_event_identity",
            "stock_id",
            "event_date",
            "event_type",
            "source_raw_id",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return f"<StockEvent(id={self.id}, stock_id={self.stock_id}, type='{self.event_type}', date={self.event_date})>"
