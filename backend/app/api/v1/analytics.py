from datetime import date
from typing import List, Optional
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_analytics_service
from app.schemas.analytics import (
    AnalyticsRequest,
    BollingerBandsResponse,
    ConsolidatedAnalyticsResponse,
    EMAResponse,
    MACDResponse,
    RSIResponse,
    SMAResponse,
    SupportResistanceResponse,
    VolatilityResponse,
    ReturnsResponse,
    VolumeAverageResponse,
)
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/stocks", tags=["Stock Analytics"])


@router.get(
    "/{stock_id}/analytics",
    response_model=ConsolidatedAnalyticsResponse,
    summary="Get consolidated technical analysis",
    description="Retrieve comprehensive technical analysis including returns, SMA, EMA, RSI, MACD, volatility, and support/resistance levels.",
    status_code=status.HTTP_200_OK,
)
def get_consolidated_analytics(
    stock_id: int,
    start_date: Optional[date] = Query(None, description="Analysis start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="Analysis end date (YYYY-MM-DD)"),
    sma_periods: List[int] = Query([20, 50, 200], description="SMA periods to calculate"),
    ema_periods: List[int] = Query([12, 26], description="EMA periods to calculate"),
    rsi_period: int = Query(14, ge=2, le=100, description="RSI period"),
    macd_fast: int = Query(12, ge=2, le=100, description="MACD fast EMA period"),
    macd_slow: int = Query(26, ge=2, le=200, description="MACD slow EMA period"),
    macd_signal: int = Query(9, ge=2, le=100, description="MACD signal EMA period"),
    volatility_period: int = Query(20, ge=2, le=252, description="Volatility rolling period"),
    volatility_annualize: bool = Query(True, description="Annualize volatility (252 trading days)"),
    sr_lookback: int = Query(20, ge=5, le=252, description="Support/resistance lookback period"),
    sr_tolerance_pct: float = Query(0.02, ge=0, le=1, description="Clustering tolerance as percentage"),
    db: Session = Depends(get_db),
    service: AnalyticsService = Depends(get_analytics_service),
) -> ConsolidatedAnalyticsResponse:
    """Get consolidated technical analysis for a stock."""
    request = AnalyticsRequest(
        start_date=start_date,
        end_date=end_date,
        sma_periods=sma_periods,
        ema_periods=ema_periods,
        rsi_period=rsi_period,
        macd_fast=macd_fast,
        macd_slow=macd_slow,
        macd_signal=macd_signal,
        volatility_period=volatility_period,
        volatility_annualize=volatility_annualize,
        sr_lookback=sr_lookback,
        sr_tolerance_pct=sr_tolerance_pct,
    )
    return service.get_consolidated_analytics(db, stock_id, request)


@router.get(
    "/{stock_id}/analytics/returns",
    response_model=ReturnsResponse,
    summary="Get returns analysis",
    description="Calculate daily returns and cumulative return over the specified period.",
    status_code=status.HTTP_200_OK,
)
def get_returns(
    stock_id: int,
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    service: AnalyticsService = Depends(get_analytics_service),
) -> ReturnsResponse:
    """Get returns analysis for a stock."""
    return service.calculate_returns(db, stock_id, start_date, end_date)


@router.get(
    "/{stock_id}/analytics/sma",
    response_model=SMAResponse,
    summary="Get Simple Moving Average",
    description="Calculate SMA for a specified period.",
    status_code=status.HTTP_200_OK,
)
def get_sma(
    stock_id: int,
    period: int = Query(20, ge=2, le=500, description="SMA period"),
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    service: AnalyticsService = Depends(get_analytics_service),
) -> SMAResponse:
    """Get Simple Moving Average for a stock."""
    return service.calculate_sma(db, stock_id, period, start_date, end_date)


@router.get(
    "/{stock_id}/analytics/ema",
    response_model=EMAResponse,
    summary="Get Exponential Moving Average",
    description="Calculate EMA for a specified period.",
    status_code=status.HTTP_200_OK,
)
def get_ema(
    stock_id: int,
    period: int = Query(12, ge=2, le=500, description="EMA period"),
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    service: AnalyticsService = Depends(get_analytics_service),
) -> EMAResponse:
    """Get Exponential Moving Average for a stock."""
    return service.calculate_ema(db, stock_id, period, start_date, end_date)


@router.get(
    "/{stock_id}/analytics/rsi",
    response_model=RSIResponse,
    summary="Get Relative Strength Index",
    description="Calculate RSI for a specified period (default 14).",
    status_code=status.HTTP_200_OK,
)
def get_rsi(
    stock_id: int,
    period: int = Query(14, ge=2, le=100, description="RSI period"),
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    service: AnalyticsService = Depends(get_analytics_service),
) -> RSIResponse:
    """Get RSI for a stock."""
    return service.calculate_rsi(db, stock_id, period, start_date, end_date)


@router.get(
    "/{stock_id}/analytics/macd",
    response_model=MACDResponse,
    summary="Get MACD",
    description="Calculate MACD with signal line and histogram.",
    status_code=status.HTTP_200_OK,
)
def get_macd(
    stock_id: int,
    fast: int = Query(12, ge=2, le=100, description="Fast EMA period"),
    slow: int = Query(26, ge=2, le=200, description="Slow EMA period"),
    signal: int = Query(9, ge=2, le=100, description="Signal EMA period"),
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    service: AnalyticsService = Depends(get_analytics_service),
) -> MACDResponse:
    """Get MACD for a stock."""
    if fast >= slow:
        from app.core.exceptions import AppValidationError
        raise AppValidationError("MACD fast period must be less than slow period")
    return service.calculate_macd(db, stock_id, fast, slow, signal, start_date, end_date)


@router.get(
    "/{stock_id}/analytics/volatility",
    response_model=VolatilityResponse,
    summary="Get Historical Volatility",
    description="Calculate rolling historical volatility from log returns.",
    status_code=status.HTTP_200_OK,
)
def get_volatility(
    stock_id: int,
    period: int = Query(20, ge=2, le=252, description="Rolling period"),
    annualize: bool = Query(True, description="Annualize using 252 trading days"),
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    service: AnalyticsService = Depends(get_analytics_service),
) -> VolatilityResponse:
    """Get historical volatility for a stock."""
    return service.calculate_volatility(db, stock_id, period, annualize, start_date, end_date)


@router.get(
    "/{stock_id}/analytics/support-resistance",
    response_model=SupportResistanceResponse,
    summary="Get Support and Resistance Levels",
    description="Calculate basic support and resistance levels from local price extremes.",
    status_code=status.HTTP_200_OK,
)
def get_support_resistance(
    stock_id: int,
    lookback: int = Query(20, ge=5, le=252, description="Lookback period for level detection"),
    tolerance: float = Query(0.02, ge=0, le=1, description="Clustering tolerance as percentage"),
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    service: AnalyticsService = Depends(get_analytics_service),
) -> SupportResistanceResponse:
    """Get support and resistance levels for a stock."""
    from decimal import Decimal
    return service.calculate_support_resistance(
        db, stock_id, lookback, Decimal(str(tolerance)), start_date, end_date
    )


@router.get(
    "/{stock_id}/analytics/bollinger-bands",
    response_model=BollingerBandsResponse,
    summary="Get Bollinger Bands",
    description=(
        "Calculate Bollinger Bands from the most recent closing prices. "
        "Consumed by the Phase 7 signal engine to produce band conditions."
    ),
    status_code=status.HTTP_200_OK,
)
def get_bollinger_bands(
    stock_id: int,
    period: int = Query(20, ge=2, le=500, description="Rolling period for the bands"),
    std_dev: float = Query(2.0, gt=0, le=5, description="Standard deviation multiplier"),
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    service: AnalyticsService = Depends(get_analytics_service),
) -> BollingerBandsResponse:
    """Get Bollinger Bands for a stock."""
    from decimal import Decimal

    return service.calculate_bollinger_bands(
        db, stock_id, period, Decimal(str(std_dev)), start_date, end_date
    )


@router.get(
    "/{stock_id}/analytics/volume",
    response_model=VolumeAverageResponse,
    summary="Get Volume Statistics",
    description=(
        "Calculate average traded volume over the most recent period. "
        "Consumed by the Phase 7 signal engine to produce volume conditions."
    ),
    status_code=status.HTTP_200_OK,
)
def get_volume_average(
    stock_id: int,
    period: int = Query(20, ge=1, le=500, description="Number of periods averaged"),
    start_date: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    service: AnalyticsService = Depends(get_analytics_service),
) -> VolumeAverageResponse:
    """Get volume statistics for a stock."""
    return service.calculate_volume_average(db, stock_id, period, start_date, end_date)