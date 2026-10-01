from datetime import date
from typing import Dict, List, Optional, Tuple
from sqlalchemy.orm import Session
from app.core.exceptions import (
    AppValidationError,
    DuplicatePriceError,
    PriceNotFoundError,
    StockNotFoundError,
)
from app.core.logging import logger
from app.models.price import StockPrice
from app.repositories.price_repository import price_repository
from app.repositories.stock_repository import stock_repository
from app.schemas.price import StockPriceCreate


class PriceService:
    """Service layer handling business logic and integrity checks for Stock Prices."""

    def __init__(self, repo=price_repository, stock_repo=stock_repository):
        self.repo = repo
        self.stock_repo = stock_repo

    def validate_price_integrity(self, price_in: StockPriceCreate) -> None:
        """Validate financial OHLC logic."""
        if price_in.open_price < 0 or price_in.high_price < 0 or price_in.low_price < 0 or price_in.close_price < 0:
            raise AppValidationError("Stock prices must be non-negative")
        if price_in.volume is not None and price_in.volume < 0:
            raise AppValidationError("Trading volume must be non-negative")

        max_oc = max(price_in.open_price, price_in.close_price)
        min_oc = min(price_in.open_price, price_in.close_price)

        if price_in.high_price < max_oc:
            raise AppValidationError(
                f"high_price ({price_in.high_price}) cannot be lower than max(open, close) ({max_oc})"
            )
        if price_in.low_price > min_oc:
            raise AppValidationError(
                f"low_price ({price_in.low_price}) cannot be higher than min(open, close) ({min_oc})"
            )

    def create_price(self, db: Session, stock_id: int, price_in: StockPriceCreate) -> StockPrice:
        # Verify stock exists
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            raise StockNotFoundError(stock_id)

        # Validate business bounds
        self.validate_price_integrity(price_in)

        # Check duplicate
        existing = self.repo.get_by_date(db, stock_id, price_in.price_date)
        if existing:
            logger.warning(f"PriceService: Duplicate price for stock_id={stock_id} date={price_in.price_date}")
            raise DuplicatePriceError(stock_id, str(price_in.price_date))

        data = price_in.model_dump()
        data["stock_id"] = stock_id
        created = self.repo.create(db, data)
        logger.info(f"PriceService: Created price for stock_id={stock_id} date={price_in.price_date}")
        return created

    def bulk_create_prices(
        self, db: Session, stock_id: int, prices_in: List[StockPriceCreate]
    ) -> Tuple[int, int]:
        """Insert a batch of prices, skipping already recorded dates."""
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            raise StockNotFoundError(stock_id)

        if not prices_in:
            return 0, 0

        # Validate all items
        for item in prices_in:
            self.validate_price_integrity(item)

        dates = [p.price_date for p in prices_in]
        existing_dates = self.repo.get_existing_dates_for_stock(
            db, stock_id, min(dates), max(dates)
        )

        to_insert = []
        skipped_count = 0

        for item in prices_in:
            if item.price_date in existing_dates:
                skipped_count += 1
            else:
                d = item.model_dump()
                d["stock_id"] = stock_id
                to_insert.append(d)
                existing_dates.add(item.price_date)

        if to_insert:
            self.repo.bulk_create(db, to_insert)
            logger.info(
                f"PriceService: Bulk created {len(to_insert)} prices, skipped {skipped_count} for stock_id={stock_id}"
            )

        return len(to_insert), skipped_count

    def get_latest_price(self, db: Session, stock_id: int) -> StockPrice:
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            raise StockNotFoundError(stock_id)

        latest = self.repo.get_latest(db, stock_id)
        if not latest:
            raise PriceNotFoundError(f"latest price for stock_id={stock_id}")
        return latest

    def get_price_history(
        self,
        db: Session,
        stock_id: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 100,
    ) -> List[StockPrice]:
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            raise StockNotFoundError(stock_id)

        if limit > 1000:
            limit = 1000

        return self.repo.get_history(
            db, stock_id, start_date=start_date, end_date=end_date, limit=limit
        )


price_service = PriceService()
