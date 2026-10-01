from datetime import datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class CompanyBase(BaseModel):
    sector: Optional[str] = Field(None, max_length=100, description="Industry sector")
    industry: Optional[str] = Field(None, max_length=100, description="Sub-industry classification")
    description: Optional[str] = Field(None, description="Overview of company operations")
    ceo: Optional[str] = Field(None, max_length=100, description="Chief Executive Officer")
    website: Optional[str] = Field(None, max_length=255, description="Official company URL")
    headquarters: Optional[str] = Field(None, max_length=255, description="Primary HQ location")
    employees: Optional[int] = Field(None, ge=0, description="Number of full-time employees")
    market_cap: Optional[Decimal] = Field(None, ge=0, description="Market capitalization")


class CompanyCreate(CompanyBase):
    stock_id: int = Field(..., description="Foreign key to stock ID")


class CompanyUpdate(CompanyBase):
    pass


class CompanyResponse(CompanyBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    stock_id: int
    symbol: Optional[str] = None
    company_name: Optional[str] = None
    exchange: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
