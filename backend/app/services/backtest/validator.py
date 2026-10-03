"""Data-quality validation for Phase 8 backtests.

Historical data is never silently repaired. If a stored OHLCV series cannot support a
reliable backtest, a :class:`BacktestDataValidationError` is raised and the API returns a
422 that names the offending dates. Prices are never manufactured or interpolated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, List, Optional, Sequence

from app.core.exceptions import AppValidationError
from app.core.logging import logger
from app.services.backtest.models import Bar

#: Minimum number of bars required before any backtest can be attempted.
#:
#: One bar cannot produce a round trip and two bars cannot produce an indicator, so a
#: single-bar "backtest" would be meaningless. The threshold is deliberately independent
#: of the Phase 7 warm-up requirement so a *data volume* problem is reported separately
#: from an *indicator warm-up* problem.
MIN_BARS_FOR_BACKTEST = 2

#: Cap on how many offending dates are listed in an error message so a badly corrupted
#: range cannot produce an unusable response body.
MAX_REPORTED_DATES = 10

#: Calendar gap (in days) above which a warning is emitted. Weekends and short market
#: holidays are normal; a multi-day gap means the equity return is compounded in one step.
GAP_WARNING_THRESHOLD_DAYS = 4


class BacktestDataValidationError(AppValidationError):
    """Raised when stored OHLCV data cannot support a reliable backtest."""

    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, details=details)


@dataclass
class DataQualityReport:
    """Outcome of a data-quality check."""

    bars: List[Bar]
    warnings: List[str]

    @property
    def bar_count(self) -> int:
        """Number of validated bars."""
        return len(self.bars)


def load_bars(rows: Iterable) -> List[Bar]:
    """Convert repository rows into normalised :class:`Bar` objects.

    Rows are sorted by trading date so the engine always iterates chronologically,
    regardless of the order the repository returned them in. Missing values are
    preserved as ``None`` so :func:`validate_bars` can report them precisely instead of
    silently substituting a zero price.
    """
    bars: List[Bar] = []
    for row in rows:
        if row.price_date is None:
            continue
        bars.append(
            Bar(
                date=row.price_date,
                open=_optional_decimal(row.open_price),
                high=_optional_decimal(row.high_price),
                low=_optional_decimal(row.low_price),
                close=_optional_decimal(row.close_price),
                volume=int(row.volume) if row.volume is not None else None,
            )
        )
    bars.sort(key=lambda bar: bar.date)
    return bars


def _optional_decimal(value: object) -> Optional[Decimal]:
    """Convert a stored numeric column to ``Decimal`` while preserving ``None``."""
    if value is None:
        return None
    return Decimal(str(value))


def validate_bars(bars: Sequence[Bar]) -> DataQualityReport:
    """Validate a bar series and return it, raising on anything that blocks backtesting.

    Checks performed, in order:

    1. at least :data:`MIN_BARS_FOR_BACKTEST` bars exist;
    2. no duplicate trading dates;
    3. dates are strictly increasing (catches unsorted input and duplicate dates);
    4. every price field is present (no missing OHLCV values);
    5. every price is strictly positive;
    6. OHLC relationships hold (``low <= min(open, close)`` and ``high >= max(open, close)``);
    7. volume is non-negative.

    Args:
        bars: chronologically ordered bars.

    Raises:
        BacktestDataValidationError: when any blocking problem is found. ``details.reason``
            identifies which check failed so a client can react programmatically.
    """
    if len(bars) < MIN_BARS_FOR_BACKTEST:
        raise BacktestDataValidationError(
            f"Insufficient OHLCV data for a backtest: {len(bars)} bar(s) available, "
            f"at least {MIN_BARS_FOR_BACKTEST} required",
            details={
                "reason": "insufficient_bars",
                "available_bars": len(bars),
                "required_bars": MIN_BARS_FOR_BACKTEST,
            },
        )

    _reject(_find_duplicate_dates(bars), "duplicate_dates", "Duplicate trading dates")
    _reject(_find_out_of_order(bars), "out_of_order", "Historical data is not chronological")
    _reject(_find_missing_prices(bars), "missing_ohlc", "Missing OHLC values")
    _reject(_find_non_positive_prices(bars), "non_positive_price", "Non-positive prices")
    _reject(_find_broken_ohlc(bars), "invalid_ohlc_relationship", "Invalid OHLC relationships")
    _reject(_find_negative_volume(bars), "negative_volume", "Negative trading volume")

    # Non-blocking observations worth surfacing, but which must not stop a backtest.
    warnings: List[str] = []
    gaps = _find_calendar_gaps(bars)
    if gaps:
        warnings.append(
            f"{len(gaps)} calendar gap(s) longer than {GAP_WARNING_THRESHOLD_DAYS} days "
            "between consecutive trading dates; returns across those gaps are compounded "
            "as a single step"
        )

    logger.info(
        "Backtest data validation passed: %d bars, %d warning(s)", len(bars), len(warnings)
    )
    return DataQualityReport(bars=list(bars), warnings=warnings)


def _reject(dates: Sequence[date], reason: str, label: str) -> None:
    """Raise a validation error naming the offending dates when ``dates`` is non-empty."""
    if not dates:
        return
    raise BacktestDataValidationError(
        f"{label} found in historical data on {_format_dates(dates)}",
        details={
            "reason": reason,
            "dates": [d.isoformat() for d in dates[:MAX_REPORTED_DATES]],
            "count": len(dates),
        },
    )


def _find_duplicate_dates(bars: Sequence[Bar]) -> List[date]:
    seen = set()
    duplicates: List[date] = []
    for bar in bars:
        if bar.date in seen:
            duplicates.append(bar.date)
        seen.add(bar.date)
    return duplicates


def _find_out_of_order(bars: Sequence[Bar]) -> List[date]:
    return [
        current.date
        for previous, current in zip(bars, bars[1:])
        if current.date <= previous.date
    ]


def _find_missing_prices(bars: Sequence[Bar]) -> List[date]:
    return [
        bar.date
        for bar in bars
        if bar.open is None or bar.high is None or bar.low is None or bar.close is None
    ]


def _find_non_positive_prices(bars: Sequence[Bar]) -> List[date]:
    invalid: List[date] = []
    for bar in bars:
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(price is not None and price <= 0 for price in prices):
            invalid.append(bar.date)
    return invalid


def _find_broken_ohlc(bars: Sequence[Bar]) -> List[date]:
    broken: List[date] = []
    for bar in bars:
        if bar.open is None or bar.high is None or bar.low is None or bar.close is None:
            continue
        if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
            broken.append(bar.date)
    return broken


def _find_negative_volume(bars: Sequence[Bar]) -> List[date]:
    return [bar.date for bar in bars if bar.volume is not None and bar.volume < 0]


def _find_calendar_gaps(bars: Sequence[Bar]) -> List[date]:
    return [
        current.date
        for previous, current in zip(bars, bars[1:])
        if (current.date - previous.date).days > GAP_WARNING_THRESHOLD_DAYS
    ]


def _format_dates(dates: Sequence[date]) -> str:
    formatted = ", ".join(d.isoformat() for d in dates[:MAX_REPORTED_DATES])
    if len(dates) > MAX_REPORTED_DATES:
        formatted += f", ... (+{len(dates) - MAX_REPORTED_DATES} more)"
    return formatted