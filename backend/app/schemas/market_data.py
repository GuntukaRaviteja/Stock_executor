from datetime import date as date_type
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class MarketDataPoint(BaseModel):
    """Normalized market data point for a single trading day."""

    date: date_type = Field(..., description="Trading date")
    open: Decimal = Field(..., ge=0, description="Opening price")
    high: Decimal = Field(..., ge=0, description="Highest price")
    low: Decimal = Field(..., ge=0, description="Lowest price")
    close: Decimal = Field(..., ge=0, description="Closing price")
    adjusted_close: Optional[Decimal] = Field(None, ge=0, description="Adjusted closing price")
    volume: int = Field(..., ge=0, description="Trading volume")

    @field_validator("high")
    @classmethod
    def high_gte_low(cls, v: Decimal, info) -> Decimal:
        if "low" in info.data and v < info.data["low"]:
            raise ValueError("high must be >= low")
        return v

    @field_validator("high")
    @classmethod
    def high_gte_open_close(cls, v: Decimal, info) -> Decimal:
        if "open" in info.data and v < info.data["open"]:
            raise ValueError("high must be >= open")
        if "close" in info.data and v < info.data["close"]:
            raise ValueError("high must be >= close")
        return v

    @field_validator("low")
    @classmethod
    def low_lte_open_close(cls, v: Decimal, info) -> Decimal:
        if "open" in info.data and v > info.data["open"]:
            raise ValueError("low must be <= open")
        if "close" in info.data and v > info.data["close"]:
            raise ValueError("low must be <= close")
        return v


class MarketDataResponse(BaseModel):
    """Response schema for market data query."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str = Field(..., description="Stock symbol (e.g., RELIANCE)")
    exchange: str = Field(..., description="Stock exchange (e.g., NSE)")
    market: str = Field(..., description="Market (e.g., INDIA)")
    provider_symbol: Optional[str] = Field(None, description="Provider-specific symbol (e.g., RELIANCE.NS)")
    start_date: date_type = Field(..., description="Query start date")
    end_date: date_type = Field(..., description="Query end date")
    count: int = Field(..., description="Number of data points returned")
    data: List[MarketDataPoint] = Field(..., description="Historical market data points")


class MarketDataIngestionRequest(BaseModel):
    """Request schema for market data ingestion."""

    symbols: List[str] = Field(..., min_length=1, description="List of stock symbols to ingest")
    start_date: date_type = Field(..., description="Start date for historical data")
    end_date: date_type = Field(..., description="End date for historical data")
    provider: Optional[str] = Field(None, description="Override provider (mock, alphavantage, yfinance)")

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date_type, info) -> date_type:
        if "start_date" in info.data and v < info.data["start_date"]:
            raise ValueError("end_date must be >= start_date")
        return v


class IngestionStockResult(BaseModel):
    """Result for a single stock in ingestion response."""

    symbol: str
    status: str  # "success", "failed", "not_found"
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    error: Optional[str] = None


class MarketDataIngestionResponse(BaseModel):
    """Response schema for market data ingestion."""

    status: str = Field(..., description="Overall status: completed, partial, failed")
    requested: int = Field(..., description="Number of symbols requested")
    successful: int = Field(..., description="Number of symbols successfully processed")
    failed: int = Field(..., description="Number of symbols that failed")
    not_found: int = Field(..., description="Number of symbols not found in database")
    total_records_fetched: int = Field(..., description="Total records fetched from provider")
    total_records_inserted: int = Field(..., description="Total new records inserted")
    total_records_updated: int = Field(..., description="Total existing records updated")
    total_records_skipped: int = Field(..., description="Total records skipped (duplicates/invalid)")
    results: List[IngestionStockResult] = Field(..., description="Per-stock results")


class MarketDataBackfillRequest(BaseModel):
    """Request schema for historical backfill."""

    symbols: List[str] = Field(..., min_length=1, description="List of stock symbols to backfill")
    start_date: date_type = Field(..., description="Start date for backfill")
    end_date: date_type = Field(..., description="End date for backfill")

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date_type, info) -> date_type:
        if "start_date" in info.data and v < info.data["start_date"]:
            raise ValueError("end_date must be >= start_date")
        return v


class MarketDataBackfillResponse(BaseModel):
    """Response schema for backfill operation."""

    status: str
    requested: int
    successful: int
    failed: int
    not_found: int
    total_records_fetched: int
    total_records_inserted: int
    total_records_updated: int
    total_records_skipped: int
    results: List[IngestionStockResult]