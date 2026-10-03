"""Pydantic schemas for the Phase 7 technical signal engine.

These schemas describe *analytical conditions* derived from technical indicators.
They are not investment advice, predictions, or guaranteed outcomes.
"""

from datetime import date as date_type
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class SignalDirection(str, Enum):
    """Directional bias produced by a signal rule."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class SignalType(str, Enum):
    """Category of information a signal is derived from."""

    MOMENTUM = "momentum"
    TREND = "trend"
    CROSSOVER = "crossover"
    VOLATILITY = "volatility"
    VOLUME = "volume"
    MEAN_REVERSION = "mean_reversion"


class StrengthBand(str, Enum):
    """Label for a numeric strength value.

    0-39 weak, 40-69 moderate, 70-100 strong.
    """

    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class SignalStatus(str, Enum):
    """Status of a signal evaluation."""

    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    NO_PRICE_DATA = "no_price_data"


class SignalAlignment(str, Enum):
    """Descriptive classification of the composite score.

    These labels describe technical indicator alignment only. They are never
    ``BUY``/``SELL``/``HOLD`` recommendations.

    ``INSUFFICIENT_DATA`` is not an alignment reading: it is returned instead of a
    classification whenever there is not enough price history to evaluate signals,
    so that missing data is never presented as a neutral reading.
    """

    STRONG_BULLISH_ALIGNMENT = "strong_bullish_alignment"
    BULLISH_ALIGNMENT = "bullish_alignment"
    NEUTRAL_ALIGNMENT = "neutral_alignment"
    BEARISH_ALIGNMENT = "bearish_alignment"
    STRONG_BEARISH_ALIGNMENT = "strong_bearish_alignment"
    INSUFFICIENT_DATA = "insufficient_data"


def strength_band(strength: int) -> StrengthBand:
    """Map a 0-100 strength value to its documented band."""
    if strength >= 70:
        return StrengthBand.STRONG
    if strength >= 40:
        return StrengthBand.MODERATE
    return StrengthBand.WEAK


class SignalItem(BaseModel):
    """A single deterministic technical signal."""

    model_config = ConfigDict(from_attributes=True)

    indicator: str = Field(..., description="Indicator that produced the signal (e.g. rsi, sma, macd)")
    signal_type: SignalType = Field(..., description="Category of the signal")
    direction: SignalDirection = Field(..., description="Directional bias of the condition")
    strength: int = Field(
        ...,
        ge=0,
        le=100,
        description="Rule-based strength 0-100. Not a probability or expected return.",
    )
    strength_band: StrengthBand = Field(..., description="Strength band derived from strength")
    value: Optional[Decimal] = Field(None, description="Indicator value that triggered the rule")
    reference: Optional[Decimal] = Field(
        None, description="Threshold, band, or comparison value used by the rule"
    )
    explanation: str = Field(..., description="Plain-language description of the condition only")
    as_of: date_type = Field(..., description="Trading date of the underlying price record")


class IndicatorSnapshot(BaseModel):
    """Latest indicator values available for the evaluated stock."""

    latest_price: Optional[Decimal] = Field(None, description="Latest closing price")
    previous_price: Optional[Decimal] = Field(None, description="Previous closing price")
    rsi: Optional[Decimal] = Field(None, description="Latest RSI")
    sma: Dict[str, Decimal] = Field(default_factory=dict, description="Latest SMA values by period")
    ema: Dict[str, Decimal] = Field(default_factory=dict, description="Latest EMA values by period")
    macd_line: Optional[Decimal] = Field(None, description="Latest MACD line")
    macd_signal: Optional[Decimal] = Field(None, description="Latest MACD signal line")
    macd_histogram: Optional[Decimal] = Field(None, description="Latest MACD histogram")
    bollinger_upper: Optional[Decimal] = Field(None, description="Latest upper Bollinger band")
    bollinger_middle: Optional[Decimal] = Field(None, description="Latest middle Bollinger band")
    bollinger_lower: Optional[Decimal] = Field(None, description="Latest lower Bollinger band")
    volume: Optional[int] = Field(None, description="Latest traded volume")
    volume_average: Optional[Decimal] = Field(None, description="Average traded volume")
    records: int = Field(0, description="Number of usable price records")
    unavailable_indicators: List[str] = Field(
        default_factory=list, description="Indicators unavailable for the available history"
    )


class CompositeSignalResponse(BaseModel):
    """Complete technical signal summary for a single stock."""

    model_config = ConfigDict(from_attributes=True)

    stock_id: int = Field(..., description="Internal stock ID")
    symbol: str = Field(..., description="Stock symbol")
    as_of: Optional[date_type] = Field(None, description="Trading date of the evaluated data")
    status: SignalStatus = Field(
        SignalStatus.OK, description="Whether signals could be generated for the available data"
    )
    message: Optional[str] = Field(
        None, description="Explanation of the status when signals are unavailable"
    )
    composite_score: int = Field(
        ..., ge=-100, le=100, description="Aggregate technical alignment score from -100 to +100"
    )
    classification: SignalAlignment = Field(..., description="Descriptive alignment classification")
    signal_count: int = Field(0, description="Number of signals evaluated")
    bullish_count: int = Field(0, description="Number of bullish signals")
    bearish_count: int = Field(0, description="Number of bearish signals")
    neutral_count: int = Field(0, description="Number of neutral signals")
    signals: List[SignalItem] = Field(default_factory=list, description="Individual signals")
    indicator_snapshot: IndicatorSnapshot = Field(
        default_factory=IndicatorSnapshot, description="Latest indicator values"
    )


class SignalResponse(BaseModel):
    """Envelope used when returning a single signal summary."""

    model_config = ConfigDict(from_attributes=True)

    success: bool = Field(True, description="Whether the request succeeded")
    data: CompositeSignalResponse = Field(..., description="Technical signal summary")


class SignalErrorResponse(BaseModel):
    """Error response shape for signal endpoints."""

    success: bool = False
    error: Dict[str, str] = Field(..., description="Error details")