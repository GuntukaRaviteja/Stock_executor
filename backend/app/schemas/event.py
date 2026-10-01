from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StockEventBase(BaseModel):
    event_type: str = Field(..., min_length=1, max_length=100, description="Type of event (e.g. EARNINGS, NEWS, DIVIDEND)")
    event_title: Optional[str] = Field(None, max_length=500, description="Short title or headline")
    event_description: Optional[str] = Field(None, description="Detailed description of the event")
    event_date: Optional[date] = Field(None, description="Date when event occurred or was published")
    impact: Optional[str] = Field(None, max_length=50, description="Estimated impact (e.g. POSITIVE, NEGATIVE, NEUTRAL)")
    source_raw_id: Optional[int] = Field(None, description="ID referencing raw_ai_data if applicable")

    @field_validator("event_type")
    @classmethod
    def clean_event_type(cls, v: str) -> str:
        cleaned = v.strip().upper()
        if not cleaned:
            raise ValueError("event_type cannot be empty")
        return cleaned


class StockEventCreate(StockEventBase):
    pass


class StockEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    stock_id: int
    event_type: str
    event_title: Optional[str] = None
    event_description: Optional[str] = None
    event_date: Optional[date] = None
    impact: Optional[str] = None
    source_raw_id: Optional[int] = None
    created_at: Optional[datetime] = None


class RecentEventResponse(StockEventResponse):
    stock_symbol: Optional[str] = None
    company_name: Optional[str] = None
