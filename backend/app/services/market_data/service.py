from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import desc
from sqlalchemy.orm import Session
from app.core.exceptions import ProviderError, StockNotFoundError
from app.core.logging import logger
from app.models.price import StockPrice
from app.models.stock import Stock
from app.repositories.price_repository import price_repository
from app.repositories.stock_repository import stock_repository
from app.schemas.market_data import (
    IngestionStockResult,
    MarketDataBackfillRequest,
    MarketDataBackfillResponse,
    MarketDataIngestionRequest,
    MarketDataIngestionResponse,
    MarketDataPoint,
    MarketDataResponse,
)
from app.schemas.price import StockPriceCreate
from app.services.market_data.fetcher import MarketDataFetcher, get_market_data_fetcher
from app.services.market_data.normalizer import market_data_normalizer
from app.services.market_data.validator import market_data_validator
from app.services.price_service import price_service


class MarketDataService:
    """Central service for market data ingestion and retrieval."""

    def __init__(
        self,
        fetcher: Optional[MarketDataFetcher] = None,
        stock_repo=stock_repository,
        price_repo=price_repository,
        price_svc=price_service,
    ):
        self.fetcher = fetcher or get_market_data_fetcher()
        self.stock_repo = stock_repo
        self.price_repo = price_repo
        self.price_svc = price_svc

    def _resolve_provider_symbol(self, stock: Stock) -> str:
        """Get the provider-specific symbol for a stock."""
        if stock.provider_symbol:
            return stock.provider_symbol

        # Default formatting for Indian NSE stocks
        if stock.exchange and stock.exchange.upper() == "NSE":
            return f"{stock.symbol}.NS"
        elif stock.exchange and stock.exchange.upper() == "BSE":
            return f"{stock.symbol}.BO"

        # Default to symbol as-is
        return stock.symbol

    async def fetch_historical_data(
        self,
        symbol: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        provider_symbol: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch raw historical data from provider.

        Args:
            symbol: Internal stock symbol
            start_date: Start date
            end_date: End date
            provider_symbol: Optional override for provider symbol

        Returns:
            Raw provider records
        """
        target_symbol = provider_symbol or symbol
        return await self.fetcher.fetch_historical(target_symbol, start_date, end_date)

    async def ingest_current_price(
        self,
        db: Session,
        stock: Stock,
    ) -> IngestionStockResult:
        """
        Ingest the latest/current price for a single stock.

        This fetches the latest quote from the provider and upserts it as today's price record.

        Args:
            db: Database session
            stock: Stock entity

        Returns:
            IngestionStockResult with statistics
        """
        provider_symbol = self._resolve_provider_symbol(stock)
        logger.info(
            f"[MARKET_DATA] Starting current price ingestion for {stock.symbol} (provider: {provider_symbol})"
        )

        start_time = datetime.now()
        fetched_count = 0
        inserted_count = 0
        updated_count = 0
        skipped_count = 0
        rejected_count = 0

        try:
            # Step 1: Fetch latest quote from provider
            raw_quote = await self.fetcher.fetch_latest_quote(provider_symbol)
            fetched_count = 1

            # Step 2: Normalize
            normalized = market_data_normalizer.normalize(
                [raw_quote], provider_symbol, self.fetcher.provider_name
            )

            # Step 3: Validate
            valid_records, rejected = market_data_validator.validate_batch(
                normalized, provider_symbol
            )
            rejected_count = len(rejected)

            if not valid_records:
                logger.warning(f"[MARKET_DATA] No valid current price record for {stock.symbol}")
                return IngestionStockResult(
                    symbol=stock.symbol,
                    status="success",
                    fetched=fetched_count,
                    inserted=0,
                    updated=0,
                    skipped=fetched_count,
                )

            # Step 4: Upsert into database
            for record in valid_records:
                try:
                    trading_date = record["trading_date"]
                    existing = self.price_repo.get_by_date(db, stock.id, trading_date)

                    price_data = StockPriceCreate(
                        price_date=trading_date,
                        open_price=record["open"],
                        high_price=record["high"],
                        low_price=record["low"],
                        close_price=record["close"],
                        volume=record["volume"],
                    )

                    if existing:
                        # Update existing record
                        self.price_repo.update(db, existing, {
                            "open_price": record["open"],
                            "high_price": record["high"],
                            "low_price": record["low"],
                            "close_price": record["close"],
                            "adjusted_close": record.get("adjusted_close"),
                            "volume": record["volume"],
                            "updated_at": datetime.now(timezone.utc),
                        })
                        updated_count += 1
                    else:
                        # Insert new record
                        data = price_data.model_dump()
                        data["stock_id"] = stock.id
                        data["adjusted_close"] = record.get("adjusted_close")
                        self.price_repo.create(db, data)
                        inserted_count += 1

                except Exception as e:
                    logger.warning(
                        f"[MARKET_DATA] Failed to upsert current price for {stock.symbol} "
                        f"on {record['trading_date']}: {e}"
                    )
                    rejected_count += 1
                    continue

            skipped_count = fetched_count - inserted_count - updated_count - rejected_count

            duration = (datetime.now() - start_time).total_seconds()
            logger.info(
                f"[MARKET_DATA] Completed current price {stock.symbol}: "
                f"inserted={inserted_count}, updated={updated_count}, "
                f"skipped={skipped_count}, rejected={rejected_count}, duration={duration:.2f}s"
            )

            return IngestionStockResult(
                symbol=stock.symbol,
                status="success",
                fetched=fetched_count,
                inserted=inserted_count,
                updated=updated_count,
                skipped=skipped_count,
            )

        except ProviderError as e:
            logger.error(f"[MARKET_DATA] Provider error for current price {stock.symbol}: {e}")
            return IngestionStockResult(
                symbol=stock.symbol,
                status="failed",
                fetched=0,
                inserted=0,
                updated=0,
                skipped=0,
                error=str(e),
            )
        except Exception as e:
            logger.error(f"[MARKET_DATA] Unexpected error for current price {stock.symbol}: {e}")
            return IngestionStockResult(
                symbol=stock.symbol,
                status="failed",
                fetched=fetched_count,
                inserted=inserted_count,
                updated=updated_count,
                skipped=skipped_count,
                error=str(e),
            )

    async def ingest_stock(
        self,
        db: Session,
        stock: Stock,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> IngestionStockResult:
        """
        Ingest historical data for a single stock.

        This is the core ingestion pipeline:
        1. Resolve provider symbol
        2. Fetch data from provider
        3. Normalize records
        4. Validate records
        5. Upsert into database
        6. Return statistics
        """
        provider_symbol = self._resolve_provider_symbol(stock)
        logger.info(
            f"[MARKET_DATA] Starting ingestion for {stock.symbol} (provider: {provider_symbol}) "
            f"from {start_date} to {end_date}"
        )

        start_time = datetime.now()
        fetched_count = 0
        inserted_count = 0
        updated_count = 0
        skipped_count = 0
        rejected_count = 0

        try:
            # Step 1: Fetch from provider
            raw_records = await self.fetcher.fetch_historical(
                provider_symbol, start_date, end_date
            )
            fetched_count = len(raw_records)

            if fetched_count == 0:
                logger.info(f"[MARKET_DATA] No data returned for {stock.symbol}")
                return IngestionStockResult(
                    symbol=stock.symbol,
                    status="success",
                    fetched=0,
                    inserted=0,
                    updated=0,
                    skipped=0,
                )

            # Step 2: Normalize
            normalized = market_data_normalizer.normalize(
                raw_records, provider_symbol, self.fetcher.provider_name
            )

            # Step 3: Validate
            valid_records, rejected = market_data_validator.validate_batch(
                normalized, provider_symbol
            )
            rejected_count = len(rejected)

            if not valid_records:
                logger.warning(f"[MARKET_DATA] No valid records after validation for {stock.symbol}")
                return IngestionStockResult(
                    symbol=stock.symbol,
                    status="success",
                    fetched=fetched_count,
                    inserted=0,
                    updated=0,
                    skipped=fetched_count,
                )

            # Step 4: Upsert into database
            for record in valid_records:
                try:
                    # Check if record exists
                    existing = self.price_repo.get_by_date(
                        db, stock.id, record["trading_date"]
                    )

                    price_data = StockPriceCreate(
                        price_date=record["trading_date"],
                        open_price=record["open"],
                        high_price=record["high"],
                        low_price=record["low"],
                        close_price=record["close"],
                        volume=record["volume"],
                    )

                    if existing:
                        # Update existing record
                        self.price_repo.update(db, existing, {
                            "open_price": record["open"],
                            "high_price": record["high"],
                            "low_price": record["low"],
                            "close_price": record["close"],
                            "adjusted_close": record.get("adjusted_close"),
                            "volume": record["volume"],
                            "updated_at": datetime.now(timezone.utc),
                        })
                        updated_count += 1
                    else:
                        # Insert new record
                        data = price_data.model_dump()
                        data["stock_id"] = stock.id
                        data["adjusted_close"] = record.get("adjusted_close")
                        self.price_repo.create(db, data)
                        inserted_count += 1

                except Exception as e:
                    logger.warning(
                        f"[MARKET_DATA] Failed to upsert record for {stock.symbol} "
                        f"on {record['trading_date']}: {e}"
                    )
                    rejected_count += 1
                    continue

            skipped_count = fetched_count - inserted_count - updated_count - rejected_count

            duration = (datetime.now() - start_time).total_seconds()
            logger.info(
                f"[MARKET_DATA] Completed {stock.symbol}: fetched={fetched_count}, "
                f"inserted={inserted_count}, updated={updated_count}, "
                f"skipped={skipped_count}, rejected={rejected_count}, duration={duration:.2f}s"
            )

            return IngestionStockResult(
                symbol=stock.symbol,
                status="success",
                fetched=fetched_count,
                inserted=inserted_count,
                updated=updated_count,
                skipped=skipped_count,
            )

        except ProviderError as e:
            logger.error(f"[MARKET_DATA] Provider error for {stock.symbol}: {e}")
            return IngestionStockResult(
                symbol=stock.symbol,
                status="failed",
                fetched=0,
                inserted=0,
                updated=0,
                skipped=0,
                error=str(e),
            )
        except Exception as e:
            logger.error(f"[MARKET_DATA] Unexpected error for {stock.symbol}: {e}")
            return IngestionStockResult(
                symbol=stock.symbol,
                status="failed",
                fetched=fetched_count,
                inserted=inserted_count,
                updated=updated_count,
                skipped=skipped_count,
                error=str(e),
            )

    async def ingest_multiple_stocks(
        self,
        db: Session,
        symbols: List[str],
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> MarketDataIngestionResponse:
        """
        Ingest historical data for multiple stocks.

        Args:
            db: Database session
            symbols: List of internal stock symbols
            start_date: Start date
            end_date: End date

        Returns:
            Aggregated ingestion response
        """
        results: List[IngestionStockResult] = []
        successful = 0
        failed = 0
        not_found = 0
        total_fetched = 0
        total_inserted = 0
        total_updated = 0
        total_skipped = 0

        for symbol in symbols:
            # Look up stock in database
            stock = self.stock_repo.get_by_symbol(db, symbol.strip().upper())
            if not stock:
                logger.warning(f"[MARKET_DATA] Stock '{symbol}' not found in database")
                results.append(IngestionStockResult(
                    symbol=symbol,
                    status="not_found",
                    error="STOCK_NOT_FOUND",
                ))
                not_found += 1
                continue

            result = await self.ingest_stock(db, stock, start_date, end_date)
            results.append(result)

            if result.status == "success":
                successful += 1
            elif result.status == "failed":
                failed += 1

            total_fetched += result.fetched
            total_inserted += result.inserted
            total_updated += result.updated
            total_skipped += result.skipped

        overall_status = "completed"
        if failed > 0 and successful == 0:
            overall_status = "failed"
        elif failed > 0 or not_found > 0:
            overall_status = "partial"

        return MarketDataIngestionResponse(
            status=overall_status,
            requested=len(symbols),
            successful=successful,
            failed=failed,
            not_found=not_found,
            total_records_fetched=total_fetched,
            total_records_inserted=total_inserted,
            total_records_updated=total_updated,
            total_records_skipped=total_skipped,
            results=results,
        )

    def get_stock_history(
        self,
        db: Session,
        symbol: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 100,
    ) -> MarketDataResponse:
        """
        Get historical market data for a stock.

        Args:
            db: Database session
            symbol: Stock symbol
            start_date: Start date filter
            end_date: End date filter
            limit: Maximum records to return

        Returns:
            MarketDataResponse with normalized data points
        """
        stock = self.stock_repo.get_by_symbol(db, symbol.strip().upper())
        if not stock:
            raise StockNotFoundError(symbol)

        prices = self.price_repo.get_history(
            db, stock.id, start_date=start_date, end_date=end_date, limit=limit
        )

        data_points = []
        for price in reversed(prices):  # Chronological order
            data_points.append(MarketDataPoint(
                date=price.price_date,
                open=price.open_price or Decimal("0"),
                high=price.high_price or Decimal("0"),
                low=price.low_price or Decimal("0"),
                close=price.close_price or Decimal("0"),
                adjusted_close=price.adjusted_close,
                volume=price.volume or 0,
            ))

        return MarketDataResponse(
            symbol=stock.symbol,
            exchange=stock.exchange or "UNKNOWN",
            market=stock.market or "UNKNOWN",
            provider_symbol=stock.provider_symbol,
            start_date=start_date or (prices[-1].price_date if prices else date.today()),
            end_date=end_date or (prices[0].price_date if prices else date.today()),
            count=len(data_points),
            data=data_points,
        )


def get_market_data_service() -> MarketDataService:
    """Factory function for MarketDataService."""
    return MarketDataService()


market_data_service = MarketDataService()