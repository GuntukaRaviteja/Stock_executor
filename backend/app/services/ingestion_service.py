from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session
from app.core.exceptions import AppValidationError, DuplicateEventError, ProviderError
from app.core.logging import logger
from app.models.stock import Stock
from app.providers.market_data import BaseMarketDataProvider, get_market_data_provider
from app.providers.news_data import BaseNewsDataProvider, get_news_data_provider
from app.repositories.stock_repository import stock_repository
from app.schemas.event import StockEventCreate
from app.schemas.price import StockPriceCreate
from app.services.event_service import event_service
from app.services.price_service import price_service


class IngestionService:
    """Service orchestrating market data and corporate events ingestion pipelines."""

    def __init__(
        self,
        market_provider: Optional[BaseMarketDataProvider] = None,
        news_provider: Optional[BaseNewsDataProvider] = None,
        stock_repo=stock_repository,
        price_svc=price_service,
        event_svc=event_service,
    ):
        self.market_provider = market_provider or get_market_data_provider()
        self.news_provider = news_provider or get_news_data_provider()
        self.stock_repo = stock_repo
        self.price_svc = price_svc
        self.event_svc = event_svc

    async def ingest_prices_for_stock(
        self,
        db: Session,
        stock: Stock,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Fetch, validate, deduplicate, and persist historical prices for a single stock."""
        symbol = stock.symbol
        try:
            logger.info(f"IngestionService: Fetching historical market data for {symbol}...")
            raw_records = await self.market_provider.get_historical_data(
                symbol, start_date=start_date, end_date=end_date
            )

            fetched_count = len(raw_records)
            if fetched_count == 0:
                logger.info(f"IngestionService: No market data records returned for {symbol}")
                return {
                    "symbol": symbol,
                    "fetched": 0,
                    "inserted": 0,
                    "skipped": 0,
                    "status": "success",
                }

            # Transform and validate through Pydantic
            valid_prices: List[StockPriceCreate] = []
            for item in raw_records:
                try:
                    price_schema = StockPriceCreate(
                        price_date=item["price_date"],
                        open_price=Decimal(str(item["open_price"])),
                        high_price=Decimal(str(item["high_price"])),
                        low_price=Decimal(str(item["low_price"])),
                        close_price=Decimal(str(item["close_price"])),
                        volume=int(item.get("volume") or 0),
                    )
                    valid_prices.append(price_schema)
                except Exception as val_err:
                    logger.warning(
                        f"IngestionService: Skipping invalid record for {symbol} on {item.get('price_date')}: {val_err}"
                    )

            # Persist and deduplicate
            inserted, skipped = self.price_svc.bulk_create_prices(
                db, stock.id, valid_prices
            )

            # Account for records invalid at parse level as skipped
            skipped += (fetched_count - len(valid_prices))

            return {
                "symbol": symbol,
                "fetched": fetched_count,
                "inserted": inserted,
                "skipped": skipped,
                "status": "success",
            }
        except Exception as e:
            logger.error(f"IngestionService: Failed ingesting prices for {symbol}: {e}")
            return {
                "symbol": symbol,
                "fetched": 0,
                "inserted": 0,
                "skipped": 0,
                "status": "failed",
                "error": str(e),
            }

    async def ingest_events_for_stock(
        self,
        db: Session,
        stock: Stock,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Fetch, normalize, and store corporate events/news for a single stock."""
        symbol = stock.symbol
        try:
            logger.info(f"IngestionService: Fetching news/events for {symbol}...")
            raw_events = await self.news_provider.get_recent_news(symbol, limit=limit)
            fetched_count = len(raw_events)

            inserted = 0
            skipped = 0

            for raw in raw_events:
                try:
                    event_schema = StockEventCreate(
                        event_type=raw.get("event_type", "NEWS"),
                        event_title=raw.get("event_title"),
                        event_description=raw.get("event_description"),
                        event_date=raw.get("event_date"),
                        impact=raw.get("impact"),
                        source_raw_id=raw.get("source_raw_id"),
                    )
                    self.event_svc.create_event(db, stock.id, event_schema)
                    inserted += 1
                except DuplicateEventError:
                    skipped += 1
                except Exception as err:
                    logger.warning(f"IngestionService: Skipping event for {symbol}: {err}")
                    skipped += 1

            return {
                "symbol": symbol,
                "fetched": fetched_count,
                "inserted": inserted,
                "skipped": skipped,
                "status": "success",
            }
        except Exception as e:
            logger.error(f"IngestionService: Failed ingesting events for {symbol}: {e}")
            return {
                "symbol": symbol,
                "fetched": 0,
                "inserted": 0,
                "skipped": 0,
                "status": "failed",
                "error": str(e),
            }

    async def run_pipeline(
        self,
        db: Session,
        symbols: Optional[List[str]] = None,
        days: int = 30,
        include_events: bool = True,
    ) -> Dict[str, Any]:
        """Run batch market ingestion pipeline across target stocks."""
        if symbols:
            stocks = []
            for s in symbols:
                stock = self.stock_repo.get_by_symbol(db, s.strip().upper())
                if stock:
                    stocks.append(stock)
                else:
                    logger.warning(f"IngestionService: Symbol '{s}' not found in database, skipping")
        else:
            stocks = self.stock_repo.get_all(db, limit=1000)

        end_date = date.today()
        start_date = end_date - timedelta(days=days)

        results: Dict[str, Dict[str, Any]] = {}
        for stock in stocks:
            price_res = await self.ingest_prices_for_stock(
                db, stock, start_date=start_date, end_date=end_date
            )
            results[stock.symbol] = price_res

            if include_events:
                await self.ingest_events_for_stock(db, stock, limit=5)

        return results


ingestion_service = IngestionService()
