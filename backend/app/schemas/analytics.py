from datetime import date as date_type
from decimal import Decimal
from typing import Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class TechnicalIndicatorPoint(BaseModel):
    """Single data point for a technical indicator."""

    date: date_type = Field(..., description="Trading date")
    value: Decimal = Field(..., description="Indicator value")


class SMAResponse(BaseModel):
    """Simple Moving Average response."""

    period: int = Field(..., description="SMA period")
    values: List[TechnicalIndicatorPoint] = Field(..., description="SMA values")


class EMAResponse(BaseModel):
    """Exponential Moving Average response."""

    period: int = Field(..., description="EMA period")
    values: List[TechnicalIndicatorPoint] = Field(..., description="EMA values")


class RSIResponse(BaseModel):
    """Relative Strength Index response."""

    period: int = Field(..., description="RSI period")
    values: List[TechnicalIndicatorPoint] = Field(..., description="RSI values")


class MACDResponse(BaseModel):
    """MACD response with MACD line, signal line, and histogram."""

    fast_period: int = Field(..., description="Fast EMA period")
    slow_period: int = Field(..., description="Slow EMA period")
    signal_period: int = Field(..., description="Signal EMA period")
    macd: List[TechnicalIndicatorPoint] = Field(..., description="MACD line values")
    signal: List[TechnicalIndicatorPoint] = Field(..., description="Signal line values")
    histogram: List[TechnicalIndicatorPoint] = Field(..., description="Histogram values")


class VolatilityResponse(BaseModel):
    """Historical volatility response."""

    period: int = Field(..., description="Rolling period for volatility calculation")
    annualized: bool = Field(..., description="Whether values are annualized")
    values: List[TechnicalIndicatorPoint] = Field(..., description="Volatility values")


class SupportResistanceResponse(BaseModel):
    """Support and resistance levels response."""

    support_levels: List[Decimal] = Field(..., description="Identified support levels")
    resistance_levels: List[Decimal] = Field(..., description="Identified resistance levels")


class ReturnsResponse(BaseModel):
    """Returns analysis response."""

    daily_returns: List[TechnicalIndicatorPoint] = Field(..., description="Daily returns")
    cumulative_return: Decimal = Field(..., description="Cumulative return over period")


class BollingerBandsResponse(BaseModel):
    """Bollinger Bands response (close-price based)."""

    period: int = Field(..., description="Rolling period used for the bands")
    std_dev_multiplier: Decimal = Field(..., description="Standard deviation multiplier")
    upper_band: Decimal = Field(..., description="Upper band value")
    middle_band: Decimal = Field(..., description="Middle band (SMA of period)")
    lower_band: Decimal = Field(..., description="Lower band value")
    percent_b: Decimal = Field(..., description="Position of close within the bands (0=lower, 1=upper)")
    bandwidth: Decimal = Field(..., description="Band width as a fraction of the middle band")


class VolumeAverageResponse(BaseModel):
    """Volume statistics response."""

    period: int = Field(..., description="Number of periods averaged")
    latest_volume: Optional[int] = Field(None, description="Latest recorded volume")
    average_volume: Optional[Decimal] = Field(None, description="Average volume over the period")
    ratio: Optional[Decimal] = Field(
        None, description="Latest volume divided by average volume (None when average is 0)"
    )


class IndicatorContext(BaseModel):
    """Latest available indicator values used by the Phase 7 signal engine.

    Every value is produced by the existing Phase 6 analytics calculations. The
    signal engine never recalculates indicators; it only reads this context and
    applies deterministic rules to it.
    """

    as_of: date_type = Field(..., description="Trading date of the latest price record")
    latest_price: Optional[Decimal] = Field(None, description="Latest closing price")
    previous_price: Optional[Decimal] = Field(None, description="Previous closing price")
    latest_daily_return: Optional[Decimal] = Field(
        None, description="Most recent daily return as a decimal fraction"
    )
    records: int = Field(..., description="Number of usable price records")

    rsi: Optional[Decimal] = Field(None, description="Latest RSI value")
    sma: Dict[str, Decimal] = Field(default_factory=dict, description="Latest SMA by period")
    sma_previous: Dict[str, Decimal] = Field(
        default_factory=dict, description="Previous SMA by period (for crossover detection)"
    )
    ema: Dict[str, Decimal] = Field(default_factory=dict, description="Latest EMA by period")

    macd_line: Optional[Decimal] = Field(None, description="Latest MACD line")
    macd_signal: Optional[Decimal] = Field(None, description="Latest MACD signal line")
    macd_histogram: Optional[Decimal] = Field(None, description="Latest MACD histogram")
    macd_previous_line: Optional[Decimal] = Field(None, description="Previous MACD line")
    macd_previous_signal: Optional[Decimal] = Field(None, description="Previous MACD signal line")

    bollinger_upper: Optional[Decimal] = Field(None, description="Latest upper Bollinger band")
    bollinger_middle: Optional[Decimal] = Field(None, description="Latest middle Bollinger band")
    bollinger_lower: Optional[Decimal] = Field(None, description="Latest lower Bollinger band")

    volume: Optional[int] = Field(None, description="Latest traded volume")
    volume_average: Optional[Decimal] = Field(None, description="Average traded volume")

    unavailable_indicators: List[str] = Field(
        default_factory=list,
        description="Indicators that could not be calculated for the available data",
    )


class ConsolidatedAnalyticsResponse(BaseModel):
    """Consolidated technical analysis response."""

    model_config = ConfigDict(from_attributes=True)

    stock_id: int = Field(..., description="Internal stock ID")
    symbol: str = Field(..., description="Stock symbol")
    analysis_start: date_type = Field(..., description="Analysis start date")
    analysis_end: date_type = Field(..., description="Analysis end date")
    latest_price: Decimal = Field(..., description="Latest closing price")
    latest_price_date: date_type = Field(..., description="Date of latest price")
    returns: ReturnsResponse = Field(..., description="Returns analysis")
    sma: Dict[str, List[TechnicalIndicatorPoint]] = Field(..., description="SMA values by period")
    ema: Dict[str, List[TechnicalIndicatorPoint]] = Field(..., description="EMA values by period")
    rsi: Dict[str, List[TechnicalIndicatorPoint]] = Field(..., description="RSI values by period")
    macd: MACDResponse = Field(..., description="MACD analysis")
    volatility: VolatilityResponse = Field(..., description="Volatility analysis")
    support_resistance: SupportResistanceResponse = Field(..., description="Support/resistance levels")


class AnalyticsRequest(BaseModel):
    """Request parameters for analytics calculation."""

    start_date: Optional[date_type] = Field(None, description="Analysis start date (inclusive)")
    end_date: Optional[date_type] = Field(None, description="Analysis end date (inclusive)")
    sma_periods: List[int] = Field(default_factory=lambda: [20, 50, 200], description="SMA periods to calculate")
    ema_periods: List[int] = Field(default_factory=lambda: [12, 26], description="EMA periods to calculate")
    rsi_period: int = Field(default=14, ge=2, le=100, description="RSI period")
    macd_fast: int = Field(default=12, ge=2, le=100, description="MACD fast EMA period")
    macd_slow: int = Field(default=26, ge=2, le=200, description="MACD slow EMA period")
    macd_signal: int = Field(default=9, ge=2, le=100, description="MACD signal EMA period")
    volatility_period: int = Field(default=20, ge=2, le=252, description="Volatility rolling period")
    volatility_annualize: bool = Field(default=True, description="Annualize volatility (assumes 252 trading days)")
    sr_lookback: int = Field(default=20, ge=5, le=252, description="Lookback period for support/resistance")
    sr_tolerance_pct: Decimal = Field(default=Decimal("0.02"), ge=0, le=1, description="Clustering tolerance as percentage")

    def validate_periods(self) -> None:
        """Validate period constraints."""
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be less than macd_slow")
        for period in self.sma_periods:
            if period <= 0:
                raise ValueError("SMA periods must be positive")
        for period in self.ema_periods:
            if period <= 0:
                raise ValueError("EMA periods must be positive")


class AnalyticsErrorResponse(BaseModel):
    """Error response for analytics endpoints."""

    success: bool = False
    error: Dict[str, str] = Field(..., description="Error details")