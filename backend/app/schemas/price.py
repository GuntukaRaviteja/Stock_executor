from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StockPriceBase(BaseModel):
    price_date: date = Field(..., description="Date of the price record")
    open_price: Decimal = Field(..., ge=0, description="Opening price, must be >= 0")
    high_price: Decimal = Field(..., ge=0, description="Highest price, must be >= 0")
    low_price: Decimal = Field(..., ge=0, description="Lowest price, must be >= 0")
    close_price: Decimal = Field(..., ge=0, description="Closing price, must be >= 0")
    volume: Optional[int] = Field(None, ge=0, description="Trading volume, must be >= 0")

    @model_validator(mode="after")
    def validate_price_bounds(self) -> "StockPriceBase":
        max_open_close = max(self.open_price, self.close_price)
        min_open_close = min(self.open_price, self.close_price)

        if self.high_price < max_open_close:
            raise ValueError(
                f"high_price ({self.high_price}) cannot be less than max(open_price, close_price) ({max_open_close})"
            )
        if self.low_price > min_open_close:
            raise ValueError(
                f"low_price ({self.low_price}) cannot be greater than min(open_price, close_price) ({min_open_close})"
            )
        return self


class StockPriceCreate(StockPriceBase):
    pass


class StockPriceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    stock_id: int
    price_date: date
    open_price: Optional[Decimal] = None
    high_price: Optional[Decimal] = None
    low_price: Optional[Decimal] = None
    close_price: Optional[Decimal] = None
    volume: Optional[int] = None
    created_at: Optional[datetime] = None


class StockPriceBulkCreate(BaseModel):
    prices: List[StockPriceCreate] = Field(..., min_length=1, description="List of price records to insert")
