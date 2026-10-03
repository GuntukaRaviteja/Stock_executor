from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional
from app.core.exceptions import AppValidationError
from app.core.logging import logger


class MarketDataValidator:
    """Validates raw market data records before normalization and storage."""

    REQUIRED_FIELDS = ["trading_date", "open", "high", "low", "close"]

    def validate_record(self, record: Dict[str, Any], provider_symbol: str) -> Dict[str, Any]:
        """
        Validate a single market data record.

        Args:
            record: Normalized record from normalizer
            provider_symbol: Symbol used for logging

        Returns:
            Validated and cleaned record

        Raises:
            AppValidationError: If validation fails
        """
        # Check required fields exist
        for field in self.REQUIRED_FIELDS:
            if field not in record or record[field] is None:
                raise AppValidationError(
                    f"Missing required field '{field}' for {provider_symbol} on {record.get('trading_date', 'unknown date')}"
                )

        # Extract and validate values
        trading_date = record["trading_date"]
        if not isinstance(trading_date, date):
            raise AppValidationError(
                f"Invalid trading_date type for {provider_symbol}: expected date, got {type(trading_date)}"
            )

        try:
            open_price = Decimal(str(record["open"]))
            high_price = Decimal(str(record["high"]))
            low_price = Decimal(str(record["low"]))
            close_price = Decimal(str(record["close"]))
        except (ValueError, TypeError) as e:
            raise AppValidationError(
                f"Non-numeric price values for {provider_symbol} on {trading_date}: {e}"
            )

        # Validate prices >= 0
        for name, value in [
            ("open", open_price),
            ("high", high_price),
            ("low", low_price),
            ("close", close_price),
        ]:
            if value < 0:
                raise AppValidationError(
                    f"{name} cannot be negative for {provider_symbol} on {trading_date}: {value}"
                )

        # Validate OHLC relationships
        if high_price < low_price:
            raise AppValidationError(
                f"high ({high_price}) < low ({low_price}) for {provider_symbol} on {trading_date}"
            )

        if high_price < open_price:
            raise AppValidationError(
                f"high ({high_price}) < open ({open_price}) for {provider_symbol} on {trading_date}"
            )

        if high_price < close_price:
            raise AppValidationError(
                f"high ({high_price}) < close ({close_price}) for {provider_symbol} on {trading_date}"
            )

        if low_price > open_price:
            raise AppValidationError(
                f"low ({low_price}) > open ({open_price}) for {provider_symbol} on {trading_date}"
            )

        if low_price > close_price:
            raise AppValidationError(
                f"low ({low_price}) > close ({close_price}) for {provider_symbol} on {trading_date}"
            )

        # Validate volume
        volume = record.get("volume")
        if volume is not None:
            try:
                volume = int(volume)
                if volume < 0:
                    raise AppValidationError(
                        f"volume cannot be negative for {provider_symbol} on {trading_date}: {volume}"
                    )
            except (ValueError, TypeError) as e:
                raise AppValidationError(
                    f"Invalid volume for {provider_symbol} on {trading_date}: {e}"
                )
        else:
            volume = 0

        # Handle adjusted_close
        adjusted_close = record.get("adjusted_close")
        if adjusted_close is not None:
            try:
                adjusted_close = Decimal(str(adjusted_close))
                if adjusted_close < 0:
                    raise AppValidationError(
                        f"adjusted_close cannot be negative for {provider_symbol} on {trading_date}: {adjusted_close}"
                    )
            except (ValueError, TypeError) as e:
                raise AppValidationError(
                    f"Invalid adjusted_close for {provider_symbol} on {trading_date}: {e}"
                )

        return {
            "trading_date": trading_date,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "adjusted_close": adjusted_close,
            "volume": volume,
        }

    def validate_batch(
        self, records: List[Dict[str, Any]], provider_symbol: str
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Validate a batch of records.

        Returns:
            Tuple of (valid_records, rejected_records_with_errors)
        """
        valid = []
        rejected = []

        for i, record in enumerate(records):
            try:
                validated = self.validate_record(record, provider_symbol)
                valid.append(validated)
            except AppValidationError as e:
                rejected.append({
                    "index": i,
                    "record": record,
                    "error": str(e),
                })
                logger.warning(
                    f"Validation rejected record {i} for {provider_symbol}: {e}"
                )

        return valid, rejected


market_data_validator = MarketDataValidator()