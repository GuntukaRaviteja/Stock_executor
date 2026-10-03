from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StockBase(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20, description="Ticker symbol (e.g. AAPL)")
    company_name: str = Field(..., min_length=1, max_length=255, description="Full company name")
    exchange: Optional[str] = Field(None, max_length=50, description="Stock exchange (e.g. NASDAQ, NYSE)")
    market: Optional[str] = Field(None, max_length=50, description="Market (e.g. INDIA, US)")
    provider_symbol: Optional[str] = Field(None, max_length=50, description="Provider-specific symbol (e.g. RELIANCE.NS)")

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, v: str) -> str:
        cleaned = v.strip().upper()
        if not cleaned:
            raise ValueError("Symbol cannot be empty or whitespace only")
        return cleaned

    @field_validator("exchange")
    @classmethod
    def normalize_exchange(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().upper() if v else None

    @field_validator("market")
    @classmethod
    def normalize_market(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().upper() if v else None


class StockCreate(StockBase):
    pass


class StockUpdate(BaseModel):
    company_name: Optional[str] = Field(None, min_length=1, max_length=255)
    exchange: Optional[str] = Field(None, max_length=50)
    market: Optional[str] = Field(None, max_length=50)
    provider_symbol: Optional[str] = Field(None, max_length=50)


class StockResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    company_name: str
    exchange: Optional[str] = None
    market: Optional[str] = None
    provider_symbol: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
