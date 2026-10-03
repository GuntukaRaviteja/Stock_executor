"""Pydantic schemas for the Phase 7 technical screener."""

from datetime import date as date_type
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.signals import SignalAlignment, SignalDirection

# Composite score range accepted by the screener filters.
SCORE_MIN = -100
SCORE_MAX = 100
# RSI is bounded by definition.
RSI_MIN = 0
RSI_MAX = 100


class TechnicalScreenerRequest(BaseModel):
    """Filter criteria for the technical screener.

    All filters are optional and are combined with AND logic. Stocks without
    sufficient price history are excluded from results.
    """

    direction: Optional[SignalDirection] = Field(
        None, description="Keep only bullish, bearish, or neutral alignments"
    )
    min_score: int = Field(
        SCORE_MIN, ge=SCORE_MIN, le=SCORE_MAX, description="Minimum composite score (-100 to +100)"
    )
    max_score: int = Field(
        SCORE_MAX, ge=SCORE_MIN, le=SCORE_MAX, description="Maximum composite score (-100 to +100)"
    )
    min_rsi: Optional[float] = Field(
        None, ge=RSI_MIN, le=RSI_MAX, description="Minimum latest RSI value"
    )
    max_rsi: Optional[float] = Field(
        None, ge=RSI_MIN, le=RSI_MAX, description="Maximum latest RSI value"
    )
    price_above_sma: Optional[int] = Field(
        None, ge=2, le=500, description="Keep stocks whose latest close is above SMA(period)"
    )
    price_below_sma: Optional[int] = Field(
        None, ge=2, le=500, description="Keep stocks whose latest close is below SMA(period)"
    )
    macd_direction: Optional[SignalDirection] = Field(
        None, description="Keep stocks whose MACD line is above/below its signal line"
    )
    min_volume: Optional[int] = Field(
        None, ge=0, description="Keep stocks whose latest traded volume is at least this value"
    )
    symbols: Optional[List[str]] = Field(
        None, description="Optional whitelist of stock symbols to screen (uppercase, max 200 entries)"
    )
    skip: int = Field(0, ge=0, description="Offset for pagination")
    limit: int = Field(50, ge=1, le=500, description="Maximum number of results to return")

    @model_validator(mode="after")
    def validate_filters(self) -> "TechnicalScreenerRequest":
        """Validate cross-field screener constraints."""
        if self.min_score > self.max_score:
            raise ValueError("min_score must be less than or equal to max_score")

        if self.min_rsi is not None and self.max_rsi is not None and self.min_rsi > self.max_rsi:
            raise ValueError("min_rsi must be less than or equal to max_rsi")

        if self.price_above_sma is not None and self.price_below_sma is not None:
            raise ValueError("price_above_sma and price_below_sma are mutually exclusive")

        if self.symbols is not None:
            if len(self.symbols) > 200:
                raise ValueError("symbols accepts at most 200 entries")
            self.symbols = [symbol.strip().upper() for symbol in self.symbols]

        return self


class TechnicalScreenerResult(BaseModel):
    """A single stock matched by the screener."""

    model_config = ConfigDict(from_attributes=True)

    stock_id: int = Field(..., description="Internal stock ID")
    symbol: str = Field(..., description="Stock symbol")
    composite_score: int = Field(..., ge=SCORE_MIN, le=SCORE_MAX, description="Composite score")
    classification: SignalAlignment = Field(..., description="Descriptive alignment classification")
    as_of: Optional[date_type] = Field(None, description="Trading date of the evaluated data")
    latest_price: Optional[Decimal] = Field(None, description="Latest closing price")
    rsi: Optional[Decimal] = Field(None, description="Latest RSI value")
    volume: Optional[int] = Field(None, description="Latest traded volume")
    signal_count: int = Field(0, description="Number of signals evaluated")


class TechnicalScreenerResponse(BaseModel):
    """Response returned by the technical screener."""

    model_config = ConfigDict(from_attributes=True)

    count: int = Field(..., description="Number of matching stocks")
    total: int = Field(..., description="Number of stocks evaluated before filters")
    skipped_insufficient_data: int = Field(
        0, description="Stocks excluded because they lack sufficient price history"
    )
    skip: int = Field(..., description="Pagination offset applied to the results")
    limit: int = Field(..., description="Pagination limit applied to the results")
    results: List[TechnicalScreenerResult] = Field(
        default_factory=list, description="Matching stocks ordered by composite score"
    )
    disclaimer: str = Field(
        default=(
            "Technical signals describe indicator alignment only. They are not financial "
            "advice and do not guarantee any future price movement."
        ),
        description="Analytical disclaimer",
    )


class ScreenerErrorResponse(BaseModel):
    """Error response shape for screener endpoints."""

    success: bool = False
    error: dict = Field(..., description="Error details")