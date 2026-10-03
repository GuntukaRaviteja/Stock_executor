"""Point-in-time indicator contexts for backtesting, reusing the Phase 6/7 engines.

Why this module exists
----------------------
The Phase 7 ``SignalEngine`` is a pure rule layer, but the only documented way to feed it
is ``AnalyticsService.get_indicator_context``, which reads whatever is in the database.
Running that once per historical bar would be look-ahead biased in the worst way: every
bar would see the *whole* history, including the future.

This module rebuilds the exact same indicator values **as they were known on each trading
date**, by reusing the Phase 6 calculations verbatim rather than re-implementing them:

* Series-shaped indicators (SMA, EMA, RSI, MACD, returns) are computed **once** over the
  full series and then indexed by date. This is safe because every Phase 6 calculation is
  *causal* — the value recorded at date *N* is a function of dates ``<= N`` only. The
  look-ahead regression test in ``tests/test_backtest_lookahead.py`` proves this by
  mutating future bars and asserting that earlier contexts are byte-identical.
* "Latest only" indicators (Bollinger Bands, volume average) are recomputed per bar
  through a trailing window, which is exactly equivalent because both are rolling
  statistics over the trailing ``period`` observations.

No indicator formula is duplicated here, and no indicator is fed a value it could not
have had on the date being simulated.
"""

from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from app.core.logging import logger
from app.schemas.analytics import IndicatorContext
from app.services.analytics_service import AnalyticsService, analytics_service
from app.services.backtest.models import Bar
from app.services.signal_service import (
    EMA_LONG_PERIOD,
    EMA_SHORT_PERIOD,
    MA_LONG_PERIOD,
    MA_SHORT_PERIOD,
)

#: Trailing window handed to the Phase 6 "latest only" statistics. Both Bollinger Bands
#: (20) and the volume average (20) are computed from ``prices[-period:]``, so a window
#: that comfortably exceeds the longest period yields identical values while keeping the
#: per-bar cost constant instead of linear.
ROLLING_WINDOW_BARS = 60

#: Periods used for the SMA/EMA/RSI/MACD series. These are the Phase 7 defaults, imported
#: rather than re-declared so the two layers can never drift apart.
SMA_PERIODS = (MA_SHORT_PERIOD, MA_LONG_PERIOD)
EMA_PERIODS = (EMA_SHORT_PERIOD, EMA_LONG_PERIOD)
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BOLLINGER_PERIOD = 20
BOLLINGER_STD = Decimal("2")
VOLUME_PERIOD = 20


class _InMemoryStock:
    """Duck-typed stand-in for the ``Stock`` ORM row (only ``id``/``symbol`` are used)."""

    def __init__(self, stock_id: int, symbol: str):
        self.id = stock_id
        self.symbol = symbol


class _InMemoryPriceRepository:
    """Repository shim that serves bars from memory instead of from the database.

    Only the two methods the Phase 6 analytics service actually calls for an indicator
    context are implemented: ``get_history_asc`` and ``get_latest``. Both accept and
    ignore the SQLAlchemy session, which is why the caller can pass ``None`` for ``db``.
    """

    def __init__(self, rows: Sequence):
        self._rows = list(rows)
        self._window: Optional[Sequence] = None

    def use_window(self, rows: Optional[Sequence]) -> None:
        """Restrict (or, with ``None``, unrestrict) the rows visible to the analytics service."""
        self._window = list(rows) if rows is not None else None

    @property
    def rows(self) -> Sequence:
        return self._window if self._window is not None else self._rows

    def get_history_asc(self, db=None, stock_id=None, start_date=None, end_date=None, limit=None):
        rows = self.rows
        if start_date:
            rows = [row for row in rows if row.price_date >= start_date]
        if end_date:
            rows = [row for row in rows if row.price_date <= end_date]
        if limit:
            rows = rows[:limit]
        return list(rows)

    def get_latest(self, db=None, stock_id=None):
        rows = self.rows
        return rows[-1] if rows else None

    def get_by_date(self, db=None, stock_id=None, price_date=None):
        for row in self.rows:
            if row.price_date == price_date:
                return row
        return None


class _InMemoryStockRepository:
    """Repository shim returning a single in-memory stock row."""

    def __init__(self, stock_id: int, symbol: str):
        self._stock = _InMemoryStock(stock_id, symbol)

    def get_by_id(self, db=None, stock_id=None):
        return self._stock


def _to_price_row(bar: Bar):
    """Build a detached ``StockPrice`` ORM instance for a bar.

    Detached instances are safe to read: every column is populated in memory and the
    object is never added to a session, so no query is ever issued. Using the real ORM
    class (rather than a duck-typed stand-in) means the Phase 6 code touches exactly the
    attribute names it expects.
    """
    from app.models.price import StockPrice

    return StockPrice(
        price_date=bar.date,
        open_price=bar.open,
        high_price=bar.high,
        low_price=bar.low,
        close_price=bar.close,
        volume=bar.volume,
    )


def _series_by_date(points) -> Dict[date_type, Decimal]:
    """Index a list of ``TechnicalIndicatorPoint`` by trading date."""
    return {point.date: point.value for point in points}


def _safe(value: Optional[Decimal]) -> Optional[Decimal]:
    """Normalise an indicator value, mapping ``None``/NaN to ``None``."""
    if value is None:
        return None
    decimal_value = Decimal(str(value))
    if decimal_value.is_nan() or decimal_value.is_infinite():
        return None
    return decimal_value


class PointInTimeContextBuilder:
    """Builds one :class:`IndicatorContext` per bar, as known on that bar's close.

    The returned list is aligned index-for-index with ``bars``: ``contexts[i]`` is the
    Phase 7 signal engine's input for ``bars[i]`` and contains no information from
    ``bars[i + 1:]``.
    """

    def __init__(self, analytics: Optional[AnalyticsService] = None):
        self.analytics = analytics or analytics_service

    def build(
        self, stock_id: int, symbol: str, bars: Sequence[Bar]
    ) -> List[IndicatorContext]:
        """Return a point-in-time indicator context for every bar.

        Indicators that cannot be computed for a given amount of history are reported in
        that context's ``unavailable_indicators`` rather than being filled with placeholders,
        matching the Phase 7 convention.
        """
        if not bars:
            return []

        rows = [_to_price_row(bar) for bar in bars]
        price_repo = _InMemoryPriceRepository(rows)
        stock_repo = _InMemoryStockRepository(stock_id, symbol)
        service = AnalyticsService(price_repo=price_repo, stock_repo=stock_repo)

        series = self._build_series(service, rows)

        contexts: List[IndicatorContext] = []
        for index, bar in enumerate(bars):
            trailing = rows[max(0, index - ROLLING_WINDOW_BARS + 1) : index + 1]
            price_repo.use_window(trailing)
            contexts.append(
                self._context_for_index(
                    service=service,
                    series=series,
                    bars=bars,
                    index=index,
                    rows=rows,
                )
            )
        price_repo.use_window(None)

        logger.info(
            "Built %d point-in-time indicator contexts for stock %d (signal-ready from index %d)",
            len(contexts),
            stock_id,
            sum(1 for c in contexts if not c.unavailable_indicators),
        )
        return contexts

    # --- Series construction (each Phase 6 method is called exactly once) ---

    def _build_series(self, service: AnalyticsService, rows: Sequence) -> Dict[str, object]:
        """Compute every point-in-time series once and index it by date.

        A failure here simply means the series is unavailable for the whole window (too
        little history); the per-bar context builder records that in
        ``unavailable_indicators`` instead of raising.
        """
        series: Dict[str, object] = {"sma": {}, "ema": {}, "macd": {}, "returns": {}}

        for period in SMA_PERIODS:
            try:
                series["sma"][period] = _series_by_date(service.calculate_sma(None, 1, period).values)
            except Exception:  # noqa: BLE001 - insufficient history simply yields no series
                logger.debug("SMA(%s) series unavailable for backtest", period)

        for period in EMA_PERIODS:
            try:
                series["ema"][period] = _series_by_date(service.calculate_ema(None, 1, period).values)
            except Exception:  # noqa: BLE001 - insufficient history simply yields no series
                logger.debug("EMA(%s) series unavailable for backtest", period)

        try:
            series["rsi"] = _series_by_date(service.calculate_rsi(None, 1, RSI_PERIOD).values)
        except Exception:  # noqa: BLE001 - insufficient history simply yields no series
            series["rsi"] = None
            logger.debug("RSI series unavailable for backtest")

        try:
            macd = service.calculate_macd(None, 1, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
            series["macd_line"] = _series_by_date(macd.macd)
            series["macd_signal"] = _series_by_date(macd.signal)
            series["macd_histogram"] = _series_by_date(macd.histogram)
        except Exception:  # noqa: BLE001 - insufficient history simply yields no series
            series["macd_line"] = None
            logger.debug("MACD series unavailable for backtest")

        try:
            series["returns"] = _series_by_date(service.calculate_returns(None, 1).daily_returns)
        except Exception:  # noqa: BLE001 - insufficient history simply yields no series
            series["returns"] = None
            logger.debug("Returns series unavailable for backtest")

        return series

    # --- Per-bar context ---

    def _context_for_index(
        self,
        *,
        service: AnalyticsService,
        series: Dict[str, object],
        bars: Sequence[Bar],
        index: int,
        rows: Sequence,
    ) -> IndicatorContext:
        """Assemble the indicator context for ``bars[index]`` using data up to that bar."""
        bar = bars[index]
        previous_bar = bars[index - 1] if index > 0 else None
        unavailable: List[str] = []

        context = IndicatorContext(
            as_of=bar.date,
            records=index + 1,
            sma={},
            sma_previous={},
            ema={},
        )
        context.latest_price = _safe(bar.close)
        if previous_bar is not None:
            context.previous_price = _safe(previous_bar.close)

        returns = series.get("returns")
        if returns:
            context.latest_daily_return = _safe(returns.get(bar.date))
            if context.latest_daily_return is None:
                unavailable.append("returns")
        else:
            unavailable.append("returns")

        rsi_series = series.get("rsi")
        if rsi_series:
            context.rsi = _safe(rsi_series.get(bar.date))
            if context.rsi is None:
                unavailable.append("rsi")
        else:
            unavailable.append("rsi")

        sma_series = series["sma"]
        for period in SMA_PERIODS:
            values = sma_series.get(period) or {}
            value = _safe(values.get(bar.date))
            if value is None:
                unavailable.append(f"sma_{period}")
                continue
            context.sma[str(period)] = value
            # The crossover rule needs the SMA value one bar earlier. A single value is
            # not enough, so it is reported as unavailable exactly as Phase 6 does.
            if previous_bar is not None:
                previous_value = _safe(values.get(previous_bar.date))
                if previous_value is not None:
                    context.sma_previous[str(period)] = previous_value
            if str(period) not in context.sma_previous:
                unavailable.append(f"sma_{period}_crossover")

        ema_series = series["ema"]
        for period in EMA_PERIODS:
            value = _safe((ema_series.get(period) or {}).get(bar.date))
            if value is None:
                unavailable.append(f"ema_{period}")
            else:
                context.ema[str(period)] = value

        macd_line = series.get("macd_line")
        macd_signal = series.get("macd_signal")
        if macd_line and macd_signal:
            line = _safe(macd_line.get(bar.date))
            signal = _safe(macd_signal.get(bar.date))
            context.macd_line = line
            context.macd_signal = signal
            context.macd_histogram = _safe((series["macd_histogram"] or {}).get(bar.date))
            if line is not None and signal is not None and previous_bar is not None:
                context.macd_previous_line = _safe(macd_line.get(previous_bar.date))
                context.macd_previous_signal = _safe(macd_signal.get(previous_bar.date))
            if context.macd_previous_line is None or context.macd_previous_signal is None:
                unavailable.append("macd_crossover")
        else:
            unavailable.append("macd")

        bands = self._bollinger(service)
        if bands is None:
            unavailable.append("bollinger_bands")
        else:
            context.bollinger_upper = bands[0]
            context.bollinger_middle = bands[1]
            context.bollinger_lower = bands[2]

        volume, volume_average = self._volume(service)
        if volume is None:
            unavailable.append("volume")
        else:
            context.volume = volume
            context.volume_average = volume_average

        # Pydantic v2 copies list fields at construction time, so the accumulated gaps
        # are assigned once at the end rather than being mutated in place.
        context.unavailable_indicators = unavailable

        return context

    @staticmethod
    def _bollinger(service: AnalyticsService):
        """Bollinger bands at the window end, or ``None`` when unavailable."""
        try:
            bands = service.calculate_bollinger_bands(None, 1, BOLLINGER_PERIOD, BOLLINGER_STD)
            return bands.upper_band, bands.middle_band, bands.lower_band
        except Exception:  # noqa: BLE001 - unavailable rather than fabricated
            return None

    @staticmethod
    def _volume(service: AnalyticsService):
        """Latest volume and its trailing average, or ``(None, None)`` when unavailable."""
        try:
            volume = service.calculate_volume_average(None, 1, VOLUME_PERIOD)
            return volume.latest_volume, volume.average_volume
        except Exception:  # noqa: BLE001 - unavailable rather than fabricated
            return None, None


point_in_time_context_builder = PointInTimeContextBuilder()