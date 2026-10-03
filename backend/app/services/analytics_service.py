from datetime import date
from decimal import Decimal, InvalidOperation
from math import sqrt, log
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session
from app.core.exceptions import AppValidationError
from app.core.logging import logger
from app.models.price import StockPrice
from app.repositories.price_repository import price_repository
from app.repositories.stock_repository import stock_repository
from app.schemas.analytics import (
    AnalyticsRequest,
    BollingerBandsResponse,
    ConsolidatedAnalyticsResponse,
    IndicatorContext,
    MACDResponse,
    ReturnsResponse,
    SMAResponse,
    EMAResponse,
    RSIResponse,
    SupportResistanceResponse,
    TechnicalIndicatorPoint,
    VolatilityResponse,
    VolumeAverageResponse,
)


class AnalyticsService:
    """Service for calculating technical indicators from stored price data."""

    MIN_RECORDS = 2  # Minimum records for any calculation

    def __init__(
        self,
        price_repo=price_repository,
        stock_repo=stock_repository,
    ):
        self.price_repo = price_repo
        self.stock_repo = stock_repo

    def _get_price_data(
        self,
        db: Session,
        stock_id: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[StockPrice]:
        """Retrieve and validate price data for analysis."""
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            from app.core.exceptions import StockNotFoundError
            raise StockNotFoundError(stock_id)

        prices = self.price_repo.get_history_asc(
            db, stock_id, start_date=start_date, end_date=end_date
        )

        if len(prices) < self.MIN_RECORDS:
            raise AppValidationError(
                f"Insufficient price data: {len(prices)} records available, "
                f"minimum {self.MIN_RECORDS} required",
                details={"available_records": len(prices), "minimum_required": self.MIN_RECORDS},
            )

        # Filter out records with missing close prices
        valid_prices = [p for p in prices if p.close_price is not None]
        if len(valid_prices) < self.MIN_RECORDS:
            raise AppValidationError(
                f"Insufficient valid close prices: {len(valid_prices)} records available"
            )

        return valid_prices

    def _safe_decimal(self, value: float | Decimal | None) -> Decimal:
        """Safely convert to Decimal, handling NaN and infinity."""
        if value is None:
            return Decimal("0")
        if isinstance(value, Decimal):
            if value.is_nan() or value.is_infinite():
                return Decimal("0")
            return value
        try:
            d = Decimal(str(value))
            if d.is_nan() or d.is_infinite():
                return Decimal("0")
            return d
        except (InvalidOperation, ValueError):
            return Decimal("0")

    def _to_indicator_points(
        self, dates: List[date], values: List[Decimal]
    ) -> List[TechnicalIndicatorPoint]:
        """Convert parallel date/value lists to TechnicalIndicatorPoint objects."""
        points = []
        for d, v in zip(dates, values):
            safe_v = self._safe_decimal(v)
            points.append(TechnicalIndicatorPoint(date=d, value=safe_v))
        return points

    # --- Returns Calculation ---

    def calculate_returns(
        self,
        db: Session,
        stock_id: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> ReturnsResponse:
        """Calculate daily and cumulative returns."""
        prices = self._get_price_data(db, stock_id, start_date, end_date)

        dates = [p.price_date for p in prices]
        # Convert close prices to Decimal safely
        closes = [self._safe_decimal(p.close_price) for p in prices]

        daily_returns = []
        daily_dates = []

        for i in range(1, len(closes)):
            prev_close = closes[i - 1]
            curr_close = closes[i]
            if prev_close and prev_close > 0:
                daily_return = (curr_close / prev_close) - Decimal("1")
                daily_returns.append(daily_return)
                daily_dates.append(dates[i])

        # Cumulative return
        initial_close = closes[0]
        final_close = closes[-1]
        if initial_close and initial_close > 0:
            cumulative = (final_close / initial_close) - Decimal("1")
        else:
            cumulative = Decimal("0")

        return ReturnsResponse(
            daily_returns=self._to_indicator_points(daily_dates, daily_returns),
            cumulative_return=self._safe_decimal(cumulative),
        )

    # --- SMA Calculation ---

    def calculate_sma(
        self,
        db: Session,
        stock_id: int,
        period: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> SMAResponse:
        """Calculate Simple Moving Average."""
        if period <= 0:
            raise AppValidationError("SMA period must be positive")

        prices = self._get_price_data(db, stock_id, start_date, end_date)

        if len(prices) < period:
            raise AppValidationError(
                f"Insufficient data for SMA({period}): {len(prices)} records, need {period}",
                details={"available": len(prices), "required": period},
            )

        closes = [p.close_price for p in prices]
        dates = [p.price_date for p in prices]

        sma_values = []
        sma_dates = []

        for i in range(period - 1, len(closes)):
            window = closes[i - period + 1 : i + 1]
            sma = sum(window) / Decimal(str(period))
            sma_values.append(sma)
            sma_dates.append(dates[i])

        return SMAResponse(period=period, values=self._to_indicator_points(sma_dates, sma_values))

    def calculate_multiple_sma(
        self,
        db: Session,
        stock_id: int,
        periods: List[int],
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> dict[str, List[TechnicalIndicatorPoint]]:
        """Calculate SMA for multiple periods."""
        result = {}
        for period in periods:
            try:
                response = self.calculate_sma(db, stock_id, period, start_date, end_date)
                result[str(period)] = response.values
            except AppValidationError as e:
                logger.warning(f"SMA({period}) calculation failed: {e.message}")
                result[str(period)] = []
        return result

    # --- EMA Calculation ---

    def calculate_ema(
        self,
        db: Session,
        stock_id: int,
        period: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> EMAResponse:
        """Calculate Exponential Moving Average."""
        if period <= 0:
            raise AppValidationError("EMA period must be positive")

        prices = self._get_price_data(db, stock_id, start_date, end_date)

        if len(prices) < period:
            raise AppValidationError(
                f"Insufficient data for EMA({period}): {len(prices)} records, need {period}",
                details={"available": len(prices), "required": period},
            )

        closes = [p.close_price for p in prices]
        dates = [p.price_date for p in prices]

        alpha = Decimal("2") / Decimal(str(period + 1))

        # Initialize EMA with SMA of first 'period' values
        initial_sma = sum(closes[:period]) / Decimal(str(period))
        ema_values = [initial_sma]
        ema_dates = [dates[period - 1]]

        # Calculate subsequent EMA values
        for i in range(period, len(closes)):
            ema = closes[i] * alpha + ema_values[-1] * (Decimal("1") - alpha)
            ema_values.append(ema)
            ema_dates.append(dates[i])

        return EMAResponse(period=period, values=self._to_indicator_points(ema_dates, ema_values))

    def calculate_multiple_ema(
        self,
        db: Session,
        stock_id: int,
        periods: List[int],
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> dict[str, List[TechnicalIndicatorPoint]]:
        """Calculate EMA for multiple periods."""
        result = {}
        for period in periods:
            try:
                response = self.calculate_ema(db, stock_id, period, start_date, end_date)
                result[str(period)] = response.values
            except AppValidationError as e:
                logger.warning(f"EMA({period}) calculation failed: {e.message}")
                result[str(period)] = []
        return result

    def _calculate_ema_series(
        self, closes: List[Decimal], period: int
    ) -> List[Decimal]:
        """Calculate EMA series for internal use (returns full series aligned with input)."""
        if period <= 0 or len(closes) < period:
            return [Decimal("0")] * len(closes)

        alpha = Decimal("2") / Decimal(str(period + 1))
        ema = [Decimal("0")] * len(closes)

        # Initialize with SMA
        initial_sma = sum(closes[:period]) / Decimal(str(period))
        ema[period - 1] = initial_sma

        # Forward fill initial period-1 values with None/0 equivalent
        # Then calculate forward
        for i in range(period, len(closes)):
            ema[i] = closes[i] * alpha + ema[i - 1] * (Decimal("1") - alpha)

        return ema

    # --- RSI Calculation ---

    def calculate_rsi(
        self,
        db: Session,
        stock_id: int,
        period: int = 14,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> RSIResponse:
        """Calculate Relative Strength Index using Wilder's smoothing method."""
        if period < 2:
            raise AppValidationError("RSI period must be at least 2")

        prices = self._get_price_data(db, stock_id, start_date, end_date)

        if len(prices) < period + 1:
            raise AppValidationError(
                f"Insufficient data for RSI({period}): {len(prices)} records, need {period + 1}",
                details={"available": len(prices), "required": period + 1},
            )

        closes = [p.close_price for p in prices]
        dates = [p.price_date for p in prices]

        # Calculate price changes
        changes = []
        for i in range(1, len(closes)):
            if closes[i - 1] and closes[i - 1] > 0:
                change = closes[i] - closes[i - 1]
                changes.append(change)
            else:
                changes.append(Decimal("0"))

        # Separate gains and losses
        gains = [max(Decimal("0"), c) for c in changes]
        losses = [max(Decimal("0"), -c) for c in changes]

        # Calculate initial average gain/loss (simple average for first period)
        avg_gain = sum(gains[:period]) / Decimal(str(period))
        avg_loss = sum(losses[:period]) / Decimal(str(period))

        rsi_values = []
        rsi_dates = []

        # First RSI value at index 'period'
        if avg_loss == 0:
            rsi = Decimal("100")
        else:
            rs = avg_gain / avg_loss
            rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
        rsi_values.append(rsi)
        rsi_dates.append(dates[period])

        # Subsequent values using Wilder's smoothing
        for i in range(period, len(changes)):
            avg_gain = (avg_gain * Decimal(str(period - 1)) + gains[i]) / Decimal(str(period))
            avg_loss = (avg_loss * Decimal(str(period - 1)) + losses[i]) / Decimal(str(period))

            if avg_loss == 0:
                rsi = Decimal("100")
            else:
                rs = avg_gain / avg_loss
                rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))

            # Clamp to valid range
            rsi = max(Decimal("0"), min(Decimal("100"), rsi))
            rsi_values.append(rsi)
            rsi_dates.append(dates[i + 1])

        return RSIResponse(period=period, values=self._to_indicator_points(rsi_dates, rsi_values))

    def calculate_multiple_rsi(
        self,
        db: Session,
        stock_id: int,
        periods: List[int],
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> dict[str, List[TechnicalIndicatorPoint]]:
        """Calculate RSI for multiple periods."""
        result = {}
        for period in periods:
            try:
                response = self.calculate_rsi(db, stock_id, period, start_date, end_date)
                result[str(period)] = response.values
            except AppValidationError as e:
                logger.warning(f"RSI({period}) calculation failed: {e.message}")
                result[str(period)] = []
        return result

    # --- MACD Calculation ---

    def calculate_macd(
        self,
        db: Session,
        stock_id: int,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> MACDResponse:
        """Calculate MACD with signal line and histogram."""
        if fast_period >= slow_period:
            raise AppValidationError("MACD fast period must be less than slow period")
        if signal_period <= 0:
            raise AppValidationError("MACD signal period must be positive")

        prices = self._get_price_data(db, stock_id, start_date, end_date)

        if len(prices) < slow_period:
            raise AppValidationError(
                f"Insufficient data for MACD: {len(prices)} records, need at least {slow_period}",
                details={"available": len(prices), "required": slow_period},
            )

        closes = [p.close_price for p in prices]
        dates = [p.price_date for p in prices]

        # Calculate fast and slow EMAs
        fast_ema = self._calculate_ema_series(closes, fast_period)
        slow_ema = self._calculate_ema_series(closes, slow_period)

        # MACD line = fast EMA - slow EMA (starts from slow_period - 1)
        macd_line = []
        macd_dates = []

        for i in range(slow_period - 1, len(closes)):
            macd_val = fast_ema[i] - slow_ema[i]
            macd_line.append(macd_val)
            macd_dates.append(dates[i])

        # Calculate signal line (EMA of MACD line)
        if len(macd_line) < signal_period:
            raise AppValidationError(
                f"Insufficient MACD values for signal line: {len(macd_line)} values, need {signal_period}"
            )

        signal_ema = self._calculate_ema_series(macd_line, signal_period)

        # Signal line starts at signal_period - 1 of MACD line
        signal_line = []
        signal_dates = []
        histogram = []
        hist_dates = []

        for i in range(signal_period - 1, len(macd_line)):
            signal_val = signal_ema[i]
            signal_line.append(signal_val)
            signal_dates.append(macd_dates[i])

            hist_val = macd_line[i] - signal_val
            histogram.append(hist_val)
            hist_dates.append(macd_dates[i])

        return MACDResponse(
            fast_period=fast_period,
            slow_period=slow_period,
            signal_period=signal_period,
            macd=self._to_indicator_points(macd_dates, macd_line),
            signal=self._to_indicator_points(signal_dates, signal_line),
            histogram=self._to_indicator_points(hist_dates, histogram),
        )

    # --- Volatility Calculation ---

    def calculate_volatility(
        self,
        db: Session,
        stock_id: int,
        period: int = 20,
        annualize: bool = True,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> VolatilityResponse:
        """Calculate historical volatility from log returns."""
        if period < 2:
            raise AppValidationError("Volatility period must be at least 2")

        prices = self._get_price_data(db, stock_id, start_date, end_date)

        if len(prices) < period + 1:
            raise AppValidationError(
                f"Insufficient data for volatility({period}): {len(prices)} records, need {period + 1}",
                details={"available": len(prices), "required": period + 1},
            )

        closes = [p.close_price for p in prices]
        dates = [p.price_date for p in prices]

        # Calculate log returns
        log_returns = []
        return_dates = []

        for i in range(1, len(closes)):
            if closes[i - 1] and closes[i - 1] > 0 and closes[i] and closes[i] > 0:
                log_ret = Decimal(str(log(float(closes[i] / closes[i - 1]))))
                log_returns.append(log_ret)
                return_dates.append(dates[i])
            else:
                log_returns.append(Decimal("0"))
                return_dates.append(dates[i])

        # Rolling standard deviation
        vol_values = []
        vol_dates = []

        for i in range(period - 1, len(log_returns)):
            window = log_returns[i - period + 1 : i + 1]
            n = Decimal(str(len(window)))
            if n > 1:
                mean = sum(window) / n
                variance = sum((x - mean) ** 2 for x in window) / (n - Decimal("1"))
                std_dev = Decimal(str(sqrt(float(variance))))
            else:
                std_dev = Decimal("0")

            if annualize:
                std_dev = std_dev * Decimal(str(sqrt(252)))

            vol_values.append(std_dev)
            vol_dates.append(return_dates[i])

        return VolatilityResponse(
            period=period,
            annualized=annualize,
            values=self._to_indicator_points(vol_dates, vol_values),
        )

    # --- Support/Resistance Calculation ---

    def calculate_support_resistance(
        self,
        db: Session,
        stock_id: int,
        lookback: int = 20,
        tolerance_pct: Decimal = Decimal("0.02"),
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> SupportResistanceResponse:
        """Calculate basic support and resistance levels from local extremes."""
        if lookback < 5:
            raise AppValidationError("Support/resistance lookback must be at least 5")

        prices = self._get_price_data(db, stock_id, start_date, end_date)

        if len(prices) < lookback:
            raise AppValidationError(
                f"Insufficient data for support/resistance: {len(prices)} records, need {lookback}"
            )

        # Use recent window
        recent_prices = prices[-lookback:]

        highs = [p.high_price for p in recent_prices if p.high_price is not None]
        lows = [p.low_price for p in recent_prices if p.low_price is not None]

        if not highs or not lows:
            return SupportResistanceResponse(support_levels=[], resistance_levels=[])

        # Find local maxima (resistance) and minima (support)
        resistance_candidates = []
        support_candidates = []

        for i in range(2, len(recent_prices) - 2):
            # Local high: higher than 2 bars on each side
            if (
                recent_prices[i].high_price
                and recent_prices[i - 1].high_price
                and recent_prices[i - 2].high_price
                and recent_prices[i + 1].high_price
                and recent_prices[i + 2].high_price
            ):
                h = recent_prices[i].high_price
                if (
                    h >= recent_prices[i - 1].high_price
                    and h >= recent_prices[i - 2].high_price
                    and h >= recent_prices[i + 1].high_price
                    and h >= recent_prices[i + 2].high_price
                ):
                    resistance_candidates.append(h)

            # Local low: lower than 2 bars on each side
            if (
                recent_prices[i].low_price
                and recent_prices[i - 1].low_price
                and recent_prices[i - 2].low_price
                and recent_prices[i + 1].low_price
                and recent_prices[i + 2].low_price
            ):
                l = recent_prices[i].low_price
                if (
                    l <= recent_prices[i - 1].low_price
                    and l <= recent_prices[i - 2].low_price
                    and l <= recent_prices[i + 1].low_price
                    and l <= recent_prices[i + 2].low_price
                ):
                    support_candidates.append(l)

        # Cluster nearby levels
        def cluster_levels(levels: List[Decimal], tolerance: Decimal) -> List[Decimal]:
            if not levels:
                return []
            sorted_levels = sorted(levels)
            clusters = []
            current_cluster = [sorted_levels[0]]

            for level in sorted_levels[1:]:
                # Check if within tolerance of cluster average
                cluster_avg = sum(current_cluster) / Decimal(str(len(current_cluster)))
                if abs(level - cluster_avg) / cluster_avg <= tolerance:
                    current_cluster.append(level)
                else:
                    # Finalize current cluster
                    clusters.append(sum(current_cluster) / Decimal(str(len(current_cluster))))
                    current_cluster = [level]

            # Add last cluster
            if current_cluster:
                clusters.append(sum(current_cluster) / Decimal(str(len(current_cluster))))

            return clusters

        avg_price = sum(p.close_price for p in recent_prices if p.close_price) / Decimal(
            str(len([p for p in recent_prices if p.close_price]))
        )
        tolerance = avg_price * tolerance_pct

        support_levels = cluster_levels(support_candidates, tolerance)
        resistance_levels = cluster_levels(resistance_candidates, tolerance)

        # Filter: support below current price, resistance above
        current_price = recent_prices[-1].close_price if recent_prices[-1].close_price else Decimal("0")

        support_levels = [s for s in support_levels if s < current_price]
        resistance_levels = [r for r in resistance_levels if r > current_price]

        # Sort and limit
        support_levels = sorted(support_levels, reverse=True)[:5]
        resistance_levels = sorted(resistance_levels)[:5]

        return SupportResistanceResponse(
            support_levels=support_levels,
            resistance_levels=resistance_levels,
        )

    # --- Bollinger Bands Calculation ---

    def calculate_bollinger_bands(
        self,
        db: Session,
        stock_id: int,
        period: int = 20,
        std_dev_multiplier: Decimal = Decimal("2"),
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> BollingerBandsResponse:
        """Calculate Bollinger Bands from the most recent closing prices.

        Bands are derived from the simple moving average of ``period`` closes and
        the sample standard deviation of the same window.
        """
        if period < 2:
            raise AppValidationError("Bollinger Bands period must be at least 2")
        if std_dev_multiplier <= 0:
            raise AppValidationError("Bollinger Bands std_dev_multiplier must be positive")

        prices = self._get_price_data(db, stock_id, start_date, end_date)

        if len(prices) < period:
            raise AppValidationError(
                f"Insufficient data for Bollinger Bands({period}): "
                f"{len(prices)} records, need {period}",
                details={"available": len(prices), "required": period},
            )

        closes = [self._safe_decimal(p.close_price) for p in prices[-period:]]
        latest_close = closes[-1]

        n = Decimal(str(len(closes)))
        middle = sum(closes) / n
        variance = sum((c - middle) ** 2 for c in closes) / (n - Decimal("1"))
        std_dev = Decimal(str(sqrt(float(variance))))

        upper = middle + std_dev * std_dev_multiplier
        lower = middle - std_dev * std_dev_multiplier

        band_width = upper - lower
        percent_b = (
            (latest_close - lower) / band_width if band_width != 0 else Decimal("0.5")
        )
        bandwidth = band_width / middle if middle != 0 else Decimal("0")

        return BollingerBandsResponse(
            period=period,
            std_dev_multiplier=std_dev_multiplier,
            upper_band=upper,
            middle_band=middle,
            lower_band=lower,
            percent_b=percent_b,
            bandwidth=bandwidth,
        )

    # --- Volume Average Calculation ---

    def calculate_volume_average(
        self,
        db: Session,
        stock_id: int,
        period: int = 20,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> VolumeAverageResponse:
        """Calculate average traded volume over the most recent ``period`` records."""
        if period < 1:
            raise AppValidationError("Volume average period must be at least 1")

        prices = self._get_price_data(db, stock_id, start_date, end_date)

        volumes = [p.volume for p in prices[-period:] if p.volume is not None]
        if not volumes:
            return VolumeAverageResponse(period=period, latest_volume=None, average_volume=None, ratio=None)

        latest_volume = prices[-1].volume
        average = sum(volumes) / Decimal(str(len(volumes)))
        ratio = Decimal(str(latest_volume)) / average if average != 0 else None

        return VolumeAverageResponse(
            period=period,
            latest_volume=latest_volume,
            average_volume=average,
            ratio=ratio,
        )

    # --- Consolidated Analytics ---

    def get_consolidated_analytics(
        self,
        db: Session,
        stock_id: int,
        request: AnalyticsRequest,
    ) -> ConsolidatedAnalyticsResponse:
        """Get complete technical analysis for a stock."""
        request.validate_periods()

        # Validate stock exists
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            from app.core.exceptions import StockNotFoundError
            raise StockNotFoundError(stock_id)

        # Get all price data needed (single query)
        prices = self._get_price_data(db, stock_id, request.start_date, request.end_date)

        # Determine analysis date range
        analysis_start = prices[0].price_date
        analysis_end = prices[-1].price_date
        latest_price = prices[-1].close_price or Decimal("0")
        latest_price_date = prices[-1].price_date

        # Calculate all indicators
        returns = self.calculate_returns(db, stock_id, request.start_date, request.end_date)
        sma = self.calculate_multiple_sma(
            db, stock_id, request.sma_periods, request.start_date, request.end_date
        )
        ema = self.calculate_multiple_ema(
            db, stock_id, request.ema_periods, request.start_date, request.end_date
        )
        rsi = self.calculate_multiple_rsi(
            db, stock_id, [request.rsi_period], request.start_date, request.end_date
        )
        macd = self.calculate_macd(
            db,
            stock_id,
            request.macd_fast,
            request.macd_slow,
            request.macd_signal,
            request.start_date,
            request.end_date,
        )
        volatility = self.calculate_volatility(
            db,
            stock_id,
            request.volatility_period,
            request.volatility_annualize,
            request.start_date,
            request.end_date,
        )
        support_resistance = self.calculate_support_resistance(
            db,
            stock_id,
            request.sr_lookback,
            request.sr_tolerance_pct,
            request.start_date,
            request.end_date,
        )

        return ConsolidatedAnalyticsResponse(
            stock_id=stock_id,
            symbol=stock.symbol,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
            latest_price=latest_price,
            latest_price_date=latest_price_date,
            returns=returns,
            sma=sma,
            ema=ema,
            rsi=rsi,
            macd=macd,
            volatility=volatility,
            support_resistance=support_resistance,
        )


# --- Signal Engine Input ---

    def get_indicator_context(
        self,
        db: Session,
        stock_id: int,
        sma_periods: List[int] = (20, 50),
        ema_periods: List[int] = (12, 26),
        rsi_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        bollinger_period: int = 20,
        bollinger_std: Decimal = Decimal("2"),
        volume_period: int = 20,
    ) -> IndicatorContext:
        """Build the indicator context consumed by the Phase 7 signal engine.

        Every value here is produced by the calculations defined in Phase 6 (plus the
        Bollinger Bands / volume statistics added alongside them). Indicators that cannot
        be computed because of insufficient history are reported in
        ``unavailable_indicators`` rather than being filled with placeholder values.

        Raises:
            StockNotFoundError: when the stock does not exist.
            AppValidationError: when no price data exists at all.
        """
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            from app.core.exceptions import StockNotFoundError

            raise StockNotFoundError(stock_id)

        # No rows at all is distinct from "not enough rows for indicators".
        latest_row = self.price_repo.get_latest(db, stock_id)
        if latest_row is None:
            raise AppValidationError(
                f"No price data available for stock {stock_id}",
                details={"stock_id": stock_id, "reason": "no_price_data"},
            )

        context = IndicatorContext(
            as_of=latest_row.price_date,
            records=0,
            sma={},
            sma_previous={},
            ema={},
            unavailable_indicators=[],
        )

        prices = [p for p in self.price_repo.get_history_asc(db, stock_id) if p.close_price is not None]
        context.records = len(prices)

        if prices:
            context.latest_price = self._safe_decimal(prices[-1].close_price)
        if len(prices) >= 2:
            context.previous_price = self._safe_decimal(prices[-2].close_price)

        # --- Returns (momentum input) ---
        try:
            returns = self.calculate_returns(db, stock_id)
            if returns.daily_returns:
                context.latest_daily_return = self._safe_decimal(returns.daily_returns[-1].value)
        except AppValidationError:
            context.unavailable_indicators.append("returns")

        # --- RSI ---
        try:
            rsi = self.calculate_rsi(db, stock_id, rsi_period)
            if rsi.values:
                context.rsi = self._safe_decimal(rsi.values[-1].value)
        except AppValidationError:
            context.unavailable_indicators.append("rsi")

        # --- SMA (latest + previous value per period, needed for crossovers) ---
        for period in sma_periods:
            try:
                sma = self.calculate_sma(db, stock_id, period)
                if not sma.values:
                    context.unavailable_indicators.append(f"sma_{period}")
                    continue
                context.sma[str(period)] = self._safe_decimal(sma.values[-1].value)
                if len(sma.values) >= 2:
                    context.sma_previous[str(period)] = self._safe_decimal(sma.values[-2].value)
                else:
                    context.unavailable_indicators.append(f"sma_{period}_crossover")
            except AppValidationError:
                context.unavailable_indicators.append(f"sma_{period}")

        # --- EMA ---
        for period in ema_periods:
            try:
                ema = self.calculate_ema(db, stock_id, period)
                if ema.values:
                    context.ema[str(period)] = self._safe_decimal(ema.values[-1].value)
                else:
                    context.unavailable_indicators.append(f"ema_{period}")
            except AppValidationError:
                context.unavailable_indicators.append(f"ema_{period}")

        # --- MACD ---
        try:
            macd = self.calculate_macd(db, stock_id, macd_fast, macd_slow, macd_signal)
            if macd.macd:
                context.macd_line = self._safe_decimal(macd.macd[-1].value)
                if len(macd.macd) >= 2:
                    context.macd_previous_line = self._safe_decimal(macd.macd[-2].value)
                else:
                    context.unavailable_indicators.append("macd_crossover")
            if macd.signal:
                context.macd_signal = self._safe_decimal(macd.signal[-1].value)
                if len(macd.signal) >= 2:
                    context.macd_previous_signal = self._safe_decimal(macd.signal[-2].value)
            if macd.histogram:
                context.macd_histogram = self._safe_decimal(macd.histogram[-1].value)
        except AppValidationError:
            context.unavailable_indicators.append("macd")

        # --- Bollinger Bands ---
        try:
            bands = self.calculate_bollinger_bands(
                db, stock_id, bollinger_period, bollinger_std
            )
            context.bollinger_upper = bands.upper_band
            context.bollinger_middle = bands.middle_band
            context.bollinger_lower = bands.lower_band
        except AppValidationError:
            context.unavailable_indicators.append("bollinger_bands")

        # --- Volume statistics ---
        try:
            volume = self.calculate_volume_average(db, stock_id, volume_period)
            context.volume = volume.latest_volume
            context.volume_average = volume.average_volume
        except AppValidationError:
            context.unavailable_indicators.append("volume")

        return context


analytics_service = AnalyticsService()