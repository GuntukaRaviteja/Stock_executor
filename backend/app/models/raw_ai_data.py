from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class RawAIData(Base):
    __tablename__ = "raw_ai_data"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_reference: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    structured_output: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ai_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    processing_status: Mapped[Optional[str]] = mapped_column(
        Enum("PENDING", "PROCESSED", "FAILED", name="processing_status_enum"),
        default="PENDING",
        nullable=True,
    )
    processing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stock_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("stocks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<RawAIData(id={self.id}, source_type='{self.source_type}', status='{self.processing_status}')>"
