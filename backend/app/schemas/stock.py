from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StockBase(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20, description="Ticker symbol (e.g. AAPL)")
    company_name: str = Field(..., min_length=1, max_length=255, description="Full company name")
    exchange: Optional[str] = Field(None, max_length=50, description="Stock exchange (e.g. NASDAQ, NYSE)")

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, v: str) -> str:
        cleaned = v.strip().upper()
        if not cleaned:
            raise ValueError("Symbol cannot be empty or whitespace only")
        return cleaned


class StockCreate(StockBase):
    pass


class StockUpdate(BaseModel):
    company_name: Optional[str] = Field(None, min_length=1, max_length=255)
    exchange: Optional[str] = Field(None, max_length=50)


class StockResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    company_name: str
    exchange: Optional[str] = None
    created_at: Optional[datetime] = None
