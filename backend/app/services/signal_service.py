"""Phase 7 signal engine and signal service.

``SignalEngine`` converts the technical indicators calculated in Phase 6 into
structured, deterministic and explainable market signals. ``SignalService``
handles the data access around it.

Design rules enforced by this module:

* **No indicator math here.** Every numeric input comes from
  :meth:`app.services.analytics_service.AnalyticsService.get_indicator_context`,
  which delegates to the Phase 6 calculations. This module only applies rules.
* **Deterministic.** The same indicator context always produces the same signals.
* **Explainable.** Every signal carries the values used by the rule and a
  description of the condition — never a prediction.
* **No advice.** Signals describe technical conditions only. They are not buy/sell
  recommendations, expected returns, or guaranteed outcomes.

Strength scoring
----------------

All constants below are documented rule parameters. Changing them changes
published behaviour.

===============================  ===============================================
Rule                             Strength
===============================  ===============================================
RSI <= 20                        100 (extreme)
20 < RSI < 30                    linear 45-99: ``45 + (30 - rsi) / 10 * 54``
30 <= RSI <= 70                  no signal (neutral momentum)
70 < RSI < 80                    linear 45-99: ``45 + (rsi - 70) / 10 * 54``
RSI >= 80                        100 (extreme)
Price vs moving average          40-100 from relative distance
Moving average alignment         60
EMA alignment                    60
Moving average crossover         80
MACD vs signal line              50-100 from normalised histogram
MACD crossover                   80
Bollinger outside band           50-100 from distance outside the band
Bollinger inside band            30 (neutral)
Volume vs average                10-100 from volume ratio (neutral direction)
Single period momentum           0-100 from absolute percentage change
===============================  ===============================================

Composite score
---------------

See :meth:`SignalEngine.calculate_composite_score` and :data:`FAMILY_CAPS`.
"""

from datetime import date as date_type
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.core.exceptions import AppValidationError
from app.core.logging import logger
from app.repositories.price_repository import price_repository
from app.repositories.stock_repository import stock_repository
from app.schemas.analytics import IndicatorContext
from app.schemas.signals import (
    CompositeSignalResponse,
    IndicatorSnapshot,
    SignalAlignment,
    SignalDirection,
    SignalItem,
    SignalStatus,
    SignalType,
    strength_band,
)
from app.services.analytics_service import AnalyticsService, analytics_service

# --- Documented rule parameters -------------------------------------------------

#: Minimum number of usable price records required to emit any signal.
#: Chosen so that the Phase 6 RSI(14) calculation is meaningful.
MIN_SIGNAL_RECORDS = 15

#: RSI thresholds used by the momentum rules.
RSI_OVERSOLD = Decimal("30")
RSI_OVERBOUGHT = Decimal("70")
#: RSI value at or beyond which the oversold/overbought condition is called extreme.
RSI_EXTREME_LOW = Decimal("20")
RSI_EXTREME_HIGH = Decimal("80")

#: Short and long moving average periods used for alignment and crossover rules.
MA_SHORT_PERIOD = 20
MA_LONG_PERIOD = 50

#: Short and long EMA periods used for the EMA trend alignment rule.
EMA_SHORT_PERIOD = 12
EMA_LONG_PERIOD = 26

#: Strength assigned to a moving average alignment signal.
MA_ALIGNMENT_STRENGTH = 60
#: Strength assigned to a crossover signal (SMA or MACD).
CROSSOVER_STRENGTH = 80
#: Strength assigned when price sits inside the Bollinger bands.
BOLLINGER_NEUTRAL_STRENGTH = 30

#: Volume strength at a ratio of 1.0, and the scaling applied per unit of ratio.
VOLUME_BASE_STRENGTH = 40
VOLUME_RATIO_SCALE = 60

#: Strength when the MACD line sits exactly on its signal line.
MACD_BASE_STRENGTH = 50
#: Histogram size (as a percentage of price) that maps to the full 0-100 strength range.
MACD_NORMALISATION_PCT = Decimal("0.5")

# --- Composite score configuration ------------------------------------------------

SCORE_MIN = -100
SCORE_MAX = 100

#: Classification thresholds for the composite score.
STRONG_BULLISH_MIN = 60
BULLISH_MIN = 20
NEUTRAL_MIN = -19
BEARISH_MIN = -59

#: Maximum absolute contribution each indicator family may add to the composite score.
#:
#: Two properties matter here:
#:
#: 1. Related indicators (price vs SMA, SMA alignment, SMA crossover) share the
#:    ``moving_average`` family so they cannot be counted repeatedly.
#: 2. The caps are not equal. Moving averages carry the most trend information so they
#:    have the largest cap; a single extreme RSI reading cannot cancel out all of the
#:    trend evidence, and volume contributes nothing because it has no direction.
#:
#: The denominator of the composite score is the sum of these caps (460), which is
#: constant. Normalising against a fixed denominator means a lone signal can never be
#: reported as "strong" alignment on its own.
FAMILY_CAPS: Dict[str, int] = {
    "moving_average": 120,
    "macd": 80,
    "rsi": 70,
    "crossover": 60,
    "bollinger_bands": 50,
    "momentum": 40,
    "volume": 40,
}

#: Sum of :data:`FAMILY_CAPS`; used as the fixed composite score denominator.
TOTAL_FAMILY_CAP = sum(FAMILY_CAPS.values())


def _clamp(value: int, low: int = SCORE_MIN, high: int = SCORE_MAX) -> int:
    """Clamp an integer into an inclusive range."""
    return max(low, min(high, value))


def _rsi_strength(rsi: Decimal) -> int:
    """Strength for an RSI overbought/oversold condition (see module docstring)."""
    if rsi <= RSI_EXTREME_LOW:
        return 100
    if rsi < RSI_OVERSOLD:
        return _clamp(int(45 + (RSI_OVERSOLD - rsi) / Decimal("10") * Decimal("54")), 0, 99)
    if rsi >= RSI_EXTREME_HIGH:
        return 100
    if rsi > RSI_OVERBOUGHT:
        return _clamp(int(45 + (rsi - RSI_OVERBOUGHT) / Decimal("10") * Decimal("54")), 0, 99)
    return 0


def _ma_distance_strength(price: Decimal, average: Decimal) -> int:
    """Strength for the distance between price and a moving average (40-100)."""
    if average <= 0:
        return 40
    distance_pct = abs(price - average) / average * Decimal("100")
    return _clamp(int(Decimal("40") + distance_pct * Decimal("10")), 40, 100)


class SignalEngine:
    """Stateless, reusable engine that turns an indicator context into signals.

    The engine performs no database access and can therefore be exercised in unit
    tests with a hand-built :class:`IndicatorContext`.
    """

    # --- A. RSI ---

    def rsi_signal(self, rsi: Decimal, as_of: date_type) -> Optional[SignalItem]:
        """Oversold (<30) or overbought (>70) momentum condition.

        The explanation describes the condition only. It does not claim the price
        will rise or fall.
        """
        if rsi < RSI_OVERSOLD:
            direction = SignalDirection.BULLISH
            threshold = RSI_OVERSOLD
            explanation = (
                f"RSI is below the oversold level of {int(RSI_OVERSOLD)} "
                f"(RSI {rsi:.2f}); this is a low-momentum condition."
            )
        elif rsi > RSI_OVERBOUGHT:
            direction = SignalDirection.BEARISH
            threshold = RSI_OVERBOUGHT
            explanation = (
                f"RSI is above the overbought level of {int(RSI_OVERBOUGHT)} "
                f"(RSI {rsi:.2f}); this is a high-momentum condition."
            )
        else:
            return None

        strength = _rsi_strength(rsi)
        return SignalItem(
            indicator="rsi",
            signal_type=SignalType.MOMENTUM,
            direction=direction,
            strength=strength,
            strength_band=strength_band(strength),
            value=rsi,
            reference=threshold,
            explanation=explanation,
            as_of=as_of,
        )

    # --- B. Moving averages ---

    def price_vs_ma_signal(
        self, price: Decimal, period: int, average: Decimal, as_of: date_type
    ) -> SignalItem:
        """Latest close compared against a moving average."""
        above = price > average
        direction = SignalDirection.BULLISH if above else SignalDirection.BEARISH
        strength = _ma_distance_strength(price, average)
        return SignalItem(
            indicator=f"price_vs_ma_{period}",
            signal_type=SignalType.TREND,
            direction=direction,
            strength=strength,
            strength_band=strength_band(strength),
            value=price,
            reference=average,
            explanation=(
                f"Latest close {price:.4f} is {'above' if above else 'below'} the "
                f"{period}-period moving average {average:.4f}."
            ),
            as_of=as_of,
        )

    def ma_alignment_signal(
        self,
        short_period: int,
        short_value: Decimal,
        long_period: int,
        long_value: Decimal,
        as_of: date_type,
        family: str = "sma",
    ) -> SignalItem:
        """Short moving average compared against the long moving average."""
        above = short_value > long_value
        direction = SignalDirection.BULLISH if above else SignalDirection.BEARISH
        return SignalItem(
            indicator=f"ma_alignment_{family}_{short_period}_{long_period}",
            signal_type=SignalType.TREND,
            direction=direction,
            strength=MA_ALIGNMENT_STRENGTH,
            strength_band=strength_band(MA_ALIGNMENT_STRENGTH),
            value=short_value,
            reference=long_value,
            explanation=(
                f"{family.upper()}({short_period}) {short_value:.4f} is "
                f"{'above' if above else 'below'} {family.upper()}({long_period}) "
                f"{long_value:.4f}."
            ),
            as_of=as_of,
        )

    # --- C. Moving average crossover ---

    def ma_crossover_signal(
        self,
        short_period: int,
        short_previous: Decimal,
        short_current: Decimal,
        long_period: int,
        long_previous: Decimal,
        long_current: Decimal,
        as_of: date_type,
        family: str = "sma",
    ) -> Optional[SignalItem]:
        """Detect a crossover between the short and long moving averages.

        Bullish: previous short <= previous long AND current short > current long.
        Bearish: previous short >= previous long AND current short < current long.
        """
        bullish_cross = short_previous <= long_previous and short_current > long_current
        bearish_cross = short_previous >= long_previous and short_current < long_current

        if not bullish_cross and not bearish_cross:
            return None

        direction = SignalDirection.BULLISH if bullish_cross else SignalDirection.BEARISH
        return SignalItem(
            indicator=f"ma_crossover_{family}_{short_period}_{long_period}",
            signal_type=SignalType.CROSSOVER,
            direction=direction,
            strength=CROSSOVER_STRENGTH,
            strength_band=strength_band(CROSSOVER_STRENGTH),
            value=short_current,
            reference=long_current,
            explanation=(
                f"The {short_period}-period moving average "
                f"{'crossed above' if bullish_cross else 'crossed below'} the "
                f"{long_period}-period moving average "
                f"({short_previous:.4f} -> {short_current:.4f} vs "
                f"{long_previous:.4f} -> {long_current:.4f})."
            ),
            as_of=as_of,
        )

    # --- D. MACD ---

    def macd_signal(
        self,
        macd_line: Decimal,
        signal_line: Decimal,
        previous_macd: Optional[Decimal] = None,
        previous_signal: Optional[Decimal] = None,
        price: Optional[Decimal] = None,
        as_of: date_type = None,  # type: ignore[assignment]
    ) -> List[SignalItem]:
        """MACD line relative to its signal line, plus a crossover when detectable."""
        signals: List[SignalItem] = []
        histogram = macd_line - signal_line

        if histogram == 0:
            direction = SignalDirection.NEUTRAL
        else:
            direction = SignalDirection.BULLISH if histogram > 0 else SignalDirection.BEARISH

        if price is not None and price > 0:
            normalised = abs(histogram) / price * Decimal("100") / MACD_NORMALISATION_PCT
            strength = _clamp(int(MACD_BASE_STRENGTH + normalised), MACD_BASE_STRENGTH, 100)
        else:
            strength = MACD_BASE_STRENGTH

        signals.append(
            SignalItem(
                indicator="macd",
                signal_type=SignalType.MOMENTUM,
                direction=direction,
                strength=strength,
                strength_band=strength_band(strength),
                value=macd_line,
                reference=signal_line,
                explanation=(
                    f"MACD line {macd_line:.4f} is "
                    f"{'above' if histogram > 0 else 'below' if histogram < 0 else 'on'} "
                    f"its signal line {signal_line:.4f} (histogram {histogram:.4f})."
                ),
                as_of=as_of,
            )
        )

        if previous_macd is not None and previous_signal is not None:
            bullish_cross = previous_macd <= previous_signal and macd_line > signal_line
            bearish_cross = previous_macd >= previous_signal and macd_line < signal_line
            if bullish_cross or bearish_cross:
                direction_cross = (
                    SignalDirection.BULLISH if bullish_cross else SignalDirection.BEARISH
                )
                signals.append(
                    SignalItem(
                        indicator="macd_crossover",
                        signal_type=SignalType.CROSSOVER,
                        direction=direction_cross,
                        strength=CROSSOVER_STRENGTH,
                        strength_band=strength_band(CROSSOVER_STRENGTH),
                        value=macd_line,
                        reference=signal_line,
                        explanation=(
                            f"MACD line crossed "
                            f"{'above' if bullish_cross else 'below'} its signal line "
                            f"({previous_macd:.4f} -> {macd_line:.4f} vs "
                            f"{previous_signal:.4f} -> {signal_line:.4f})."
                        ),
                        as_of=as_of,
                    )
                )

        return signals

    # --- E. Bollinger bands ---

    def bollinger_signal(
        self, price: Decimal, upper: Decimal, lower: Decimal, as_of: date_type
    ) -> SignalItem:
        """Latest close relative to the Bollinger bands.

        A price outside the bands is reported as a band condition only; no reversal
        is implied.
        """
        if price < lower:
            direction = SignalDirection.BULLISH
            reference: Optional[Decimal] = lower
            strength = self._band_strength(price, lower, below=True)
            explanation = (
                f"Latest close {price:.4f} is below the lower Bollinger band "
                f"{lower:.4f}; this is a below-band condition."
            )
        elif price > upper:
            direction = SignalDirection.BEARISH
            reference = upper
            strength = self._band_strength(price, upper, below=False)
            explanation = (
                f"Latest close {price:.4f} is above the upper Bollinger band "
                f"{upper:.4f}; this is an above-band condition."
            )
        else:
            direction = SignalDirection.NEUTRAL
            reference = None
            strength = BOLLINGER_NEUTRAL_STRENGTH
            explanation = (
                f"Latest close {price:.4f} is between the Bollinger bands "
                f"{lower:.4f} and {upper:.4f}."
            )

        return SignalItem(
            indicator="bollinger_bands",
            signal_type=SignalType.VOLATILITY,
            direction=direction,
            strength=strength,
            strength_band=strength_band(strength),
            value=price,
            reference=reference,
            explanation=explanation,
            as_of=as_of,
        )

    @staticmethod
    def _band_strength(price: Decimal, band: Decimal, below: bool) -> int:
        """Strength 50-100 from the distance outside the band, relative to price."""
        if price <= 0:
            return 50
        distance = (band - price) if below else (price - band)
        if distance <= 0:
            return 50
        return _clamp(
            int(Decimal("50") + distance / price * Decimal("100") * Decimal("20")), 50, 100
        )

    # --- F. Volume ---

    def volume_signal(
        self, volume: int, average: Decimal, as_of: date_type
    ) -> Optional[SignalItem]:
        """Latest volume compared against its recent average.

        Volume carries no directional bias: it is reported as a neutral activity
        condition and contributes 0 to the composite score.
        """
        if average <= 0:
            return None

        ratio = Decimal(str(volume)) / average
        strength = _clamp(
            int(
                Decimal(str(VOLUME_BASE_STRENGTH))
                + (ratio - Decimal("1")) * Decimal(str(VOLUME_RATIO_SCALE))
            ),
            10,
            100,
        )
        at_or_above = ratio >= 1
        return SignalItem(
            indicator="volume",
            signal_type=SignalType.VOLUME,
            direction=SignalDirection.NEUTRAL,
            strength=strength,
            strength_band=strength_band(strength),
            value=Decimal(str(volume)),
            reference=average,
            explanation=(
                f"Latest volume {volume} is "
                f"{'at or above' if at_or_above else 'below'} the recent average "
                f"{average:.2f} (ratio {ratio:.2f})."
            ),
            as_of=as_of,
        )

    # --- G. Price momentum ---

    def momentum_signal(self, daily_return: Decimal, as_of: date_type) -> Optional[SignalItem]:
        """Most recent single-period price change as a momentum condition."""
        if daily_return == 0:
            return None

        direction = SignalDirection.BULLISH if daily_return > 0 else SignalDirection.BEARISH
        pct = daily_return * Decimal("100")
        strength = _clamp(int(abs(pct) * Decimal("20")), 1, 100)
        return SignalItem(
            indicator="momentum",
            signal_type=SignalType.MOMENTUM,
            direction=direction,
            strength=strength,
            strength_band=strength_band(strength),
            value=pct,
            reference=None,
            explanation=(
                f"Most recent period return is {pct:+.2f}%; "
                "this describes the direction of the last close-to-close change."
            ),
            as_of=as_of,
        )

    # --- Aggregation ---

    @staticmethod
    def family_of(signal: SignalItem) -> str:
        """Map a signal onto the composite-score family used for capping."""
        indicator = signal.indicator
        if indicator == "rsi":
            return "rsi"
        if indicator.startswith("ma_crossover") or indicator == "macd_crossover":
            return "crossover"
        if indicator == "macd":
            return "macd"
        if indicator == "bollinger_bands":
            return "bollinger_bands"
        if indicator == "volume":
            return "volume"
        if indicator == "momentum":
            return "momentum"
        if indicator.startswith("price_vs_ma") or indicator.startswith("ma_alignment"):
            return "moving_average"
        return indicator

    def calculate_composite_score(self, signals: Iterable[SignalItem]) -> int:
        """Aggregate signals into a composite score on a -100..+100 scale.

        Method:

        1. Each directional signal contributes ``+strength`` when bullish,
           ``-strength`` when bearish and ``0`` when neutral.
        2. Contributions are summed per indicator family
           (:data:`FAMILY_CAPS`) so related indicators — for example "price vs
           SMA(20)", "SMA alignment" and "SMA crossover" — are not counted
           repeatedly and cannot swamp the aggregate.
        3. Each family's net contribution is capped at its documented cap.
        4. The sum of family contributions is divided by :data:`TOTAL_FAMILY_CAP`
           (the sum of all caps, a constant) and scaled to -100..+100.

        Using a fixed denominator means a single condition can never by itself be
        reported as "strong" alignment.

        The result describes technical alignment only. It is not a probability,
        an expected return, or a trade recommendation.
        """
        totals: Dict[str, int] = {}
        for signal in signals:
            if signal.direction is SignalDirection.BULLISH:
                family = self.family_of(signal)
                totals[family] = totals.get(family, 0) + signal.strength
            elif signal.direction is SignalDirection.BEARISH:
                family = self.family_of(signal)
                totals[family] = totals.get(family, 0) - signal.strength

        if not totals:
            return 0

        capped_total = 0
        for family, value in totals.items():
            cap = FAMILY_CAPS.get(family, 100)
            capped_total += _clamp(value, -cap, cap)

        return _clamp(int(round(capped_total / TOTAL_FAMILY_CAP * SCORE_MAX)))

    @staticmethod
    def classify(score: int) -> SignalAlignment:
        """Classify a composite score into a descriptive alignment label."""
        if score >= STRONG_BULLISH_MIN:
            return SignalAlignment.STRONG_BULLISH_ALIGNMENT
        if score >= BULLISH_MIN:
            return SignalAlignment.BULLISH_ALIGNMENT
        if score >= NEUTRAL_MIN:
            return SignalAlignment.NEUTRAL_ALIGNMENT
        if score >= BEARISH_MIN:
            return SignalAlignment.BEARISH_ALIGNMENT
        return SignalAlignment.STRONG_BEARISH_ALIGNMENT

    def build_signals(self, context: IndicatorContext) -> List[SignalItem]:
        """Apply every rule that the available data supports.

        Indicators that are unavailable are skipped; the context reports them in
        ``unavailable_indicators`` so the API can explain the gap. No placeholder
        values are ever substituted for missing indicators.
        """
        as_of = context.as_of
        signals: List[SignalItem] = []

        # A. RSI
        if context.rsi is not None:
            rsi_signal = self.rsi_signal(context.rsi, as_of)
            if rsi_signal is not None:
                signals.append(rsi_signal)

        # B. Price vs moving average
        if context.latest_price is not None:
            for period in (MA_SHORT_PERIOD, MA_LONG_PERIOD):
                average = context.sma.get(str(period))
                if average is not None:
                    signals.append(
                        self.price_vs_ma_signal(context.latest_price, period, average, as_of)
                    )

        # B. SMA alignment
        short_ma = context.sma.get(str(MA_SHORT_PERIOD))
        long_ma = context.sma.get(str(MA_LONG_PERIOD))
        if short_ma is not None and long_ma is not None:
            signals.append(
                self.ma_alignment_signal(
                    MA_SHORT_PERIOD, short_ma, MA_LONG_PERIOD, long_ma, as_of, family="sma"
                )
            )

        # B. EMA alignment (equivalent rule for the EMA family)
        ema_short = context.ema.get(str(EMA_SHORT_PERIOD))
        ema_long = context.ema.get(str(EMA_LONG_PERIOD))
        if ema_short is not None and ema_long is not None:
            signals.append(
                self.ma_alignment_signal(
                    EMA_SHORT_PERIOD, ema_short, EMA_LONG_PERIOD, ema_long, as_of, family="ema"
                )
            )

        # C. SMA crossover
        short_prev = context.sma_previous.get(str(MA_SHORT_PERIOD))
        long_prev = context.sma_previous.get(str(MA_LONG_PERIOD))
        if None not in (short_ma, long_ma, short_prev, long_prev):
            crossover = self.ma_crossover_signal(
                MA_SHORT_PERIOD, short_prev, short_ma, MA_LONG_PERIOD, long_prev, long_ma, as_of
            )
            if crossover is not None:
                signals.append(crossover)

        # D. MACD (position + crossover)
        if context.macd_line is not None and context.macd_signal is not None:
            signals.extend(
                self.macd_signal(
                    context.macd_line,
                    context.macd_signal,
                    context.macd_previous_line,
                    context.macd_previous_signal,
                    context.latest_price,
                    as_of,
                )
            )

        # E. Bollinger bands
        if (
            context.latest_price is not None
            and context.bollinger_upper is not None
            and context.bollinger_lower is not None
        ):
            signals.append(
                self.bollinger_signal(
                    context.latest_price, context.bollinger_upper, context.bollinger_lower, as_of
                )
            )

        # F. Volume (reported separately from price direction)
        if context.volume is not None and context.volume_average is not None:
            volume_signal = self.volume_signal(context.volume, context.volume_average, as_of)
            if volume_signal is not None:
                signals.append(volume_signal)

        # G. Price momentum
        if context.latest_daily_return is not None:
            momentum = self.momentum_signal(context.latest_daily_return, as_of)
            if momentum is not None:
                signals.append(momentum)

        return signals

    @staticmethod
    def to_snapshot(context: IndicatorContext) -> IndicatorSnapshot:
        """Convert the analytics context into the public indicator snapshot."""
        return IndicatorSnapshot(
            latest_price=context.latest_price,
            previous_price=context.previous_price,
            rsi=context.rsi,
            sma=dict(context.sma),
            ema=dict(context.ema),
            macd_line=context.macd_line,
            macd_signal=context.macd_signal,
            macd_histogram=context.macd_histogram,
            bollinger_upper=context.bollinger_upper,
            bollinger_middle=context.bollinger_middle,
            bollinger_lower=context.bollinger_lower,
            volume=context.volume,
            volume_average=context.volume_average,
            records=context.records,
            unavailable_indicators=list(context.unavailable_indicators),
        )

    def evaluate(
        self,
        stock_id: int,
        symbol: str,
        context: Optional[IndicatorContext] = None,
        status: SignalStatus = SignalStatus.OK,
        message: Optional[str] = None,
    ) -> CompositeSignalResponse:
        """Build the complete signal summary for a stock from an indicator context.

        When ``status`` is not :attr:`SignalStatus.OK` the summary deliberately carries
        no signals and a score of 0, so insufficient data can never be mistaken for a
        neutral reading. The indicator snapshot is still returned so callers can see how
        much data actually exists.
        """
        if context is None:
            return CompositeSignalResponse(
                stock_id=stock_id,
                symbol=symbol,
                as_of=None,
                status=status,
                message=message,
                composite_score=0,
                classification=SignalAlignment.INSUFFICIENT_DATA,
                signal_count=0,
                bullish_count=0,
                bearish_count=0,
                neutral_count=0,
                signals=[],
                indicator_snapshot=IndicatorSnapshot(),
            )

        if status is not SignalStatus.OK:
            return CompositeSignalResponse(
                stock_id=stock_id,
                symbol=symbol,
                as_of=context.as_of,
                status=status,
                message=message,
                composite_score=0,
                classification=SignalAlignment.INSUFFICIENT_DATA,
                signal_count=0,
                bullish_count=0,
                bearish_count=0,
                neutral_count=0,
                signals=[],
                indicator_snapshot=self.to_snapshot(context),
            )

        signals = self.build_signals(context)
        score = self.calculate_composite_score(signals)

        return CompositeSignalResponse(
            stock_id=stock_id,
            symbol=symbol,
            as_of=context.as_of,
            status=status,
            message=message,
            composite_score=score,
            classification=self.classify(score),
            signal_count=len(signals),
            bullish_count=sum(1 for s in signals if s.direction is SignalDirection.BULLISH),
            bearish_count=sum(1 for s in signals if s.direction is SignalDirection.BEARISH),
            neutral_count=sum(1 for s in signals if s.direction is SignalDirection.NEUTRAL),
            signals=signals,
            indicator_snapshot=self.to_snapshot(context),
        )


class SignalService:
    """Service that loads indicators through Phase 6 and evaluates signals.

    The service contains no FastAPI-specific code and performs no SQL of its own:
    all data access is delegated to the analytics service and repositories.
    """

    def __init__(
        self,
        analytics: AnalyticsService = analytics_service,
        stock_repo=stock_repository,
        price_repo=price_repository,
        engine: Optional[SignalEngine] = None,
    ):
        self.analytics = analytics
        self.stock_repo = stock_repo
        self.price_repo = price_repo
        self.engine = engine or SignalEngine()

    def build_context(self, db: Session, stock_id: int) -> Optional[IndicatorContext]:
        """Load the indicator context for a stock through the Phase 6 analytics service.

        Returns ``None`` when the stock has no price data at all. Indicators that are
        unavailable because of insufficient history are reported inside the context.

        Raises:
            StockNotFoundError: propagated from the analytics service when the stock
                does not exist.
        """
        try:
            return self.analytics.get_indicator_context(db, stock_id)
        except AppValidationError as exc:
            if (exc.details or {}).get("reason") == "no_price_data":
                return None
            raise

    def get_stock_signals(self, db: Session, stock_id: int) -> CompositeSignalResponse:
        """Return the current technical signal summary for a stock.

        Distinguishes the following cases:

        * unknown stock -> raises ``StockNotFoundError`` (HTTP 404)
        * no price rows -> ``status=no_price_data`` with no signals
        * fewer than :data:`MIN_SIGNAL_RECORDS` usable records -> ``status=insufficient_data``
        * indicators unavailable for the available history -> reported in
          ``indicator_snapshot.unavailable_indicators``
        """
        stock = self.stock_repo.get_by_id(db, stock_id)
        if not stock:
            from app.core.exceptions import StockNotFoundError

            raise StockNotFoundError(stock_id)

        context = self.build_context(db, stock_id)

        if context is None:
            return self.engine.evaluate(
                stock_id,
                stock.symbol,
                context=None,
                status=SignalStatus.NO_PRICE_DATA,
                message="No price data is stored for this stock, so no indicators can be calculated.",
            )

        if context.records < MIN_SIGNAL_RECORDS:
            return self.engine.evaluate(
                stock_id,
                stock.symbol,
                context=context,
                status=SignalStatus.INSUFFICIENT_DATA,
                message=(
                    f"Insufficient price history: {context.records} usable records, "
                    f"{MIN_SIGNAL_RECORDS} required to evaluate signals."
                ),
            )

        return self.engine.evaluate(stock_id, stock.symbol, context=context)

    def get_latest_signal(self, db: Session, stock_id: int) -> Optional[SignalItem]:
        """Return the single strongest signal for a stock, or ``None``.

        "Strongest" is the signal with the highest numeric strength; ties are broken
        by document order. Strength is not a probability.
        """
        summary = self.get_stock_signals(db, stock_id)
        if not summary.signals:
            return None
        return max(summary.signals, key=lambda signal: signal.strength)

    def calculate_signals(self, db: Session, stock_id: int) -> List[SignalItem]:
        """Return the raw list of signals for a stock."""
        return self.get_stock_signals(db, stock_id).signals

    def calculate_composite_score(self, signals: Iterable[SignalItem]) -> int:
        """Aggregate a list of signals into a composite score (-100..+100)."""
        return self.engine.calculate_composite_score(signals)

    def screen_contexts(
        self, db: Session, stock_ids: Iterable[int]
    ) -> List[CompositeSignalResponse]:
        """Evaluate a batch of stocks, skipping those without enough data.

        Used by the screener. Errors for individual stocks are logged and skipped so
        one bad symbol cannot fail the whole screen.
        """
        summaries: List[CompositeSignalResponse] = []
        for stock_id in stock_ids:
            try:
                summaries.append(self.get_stock_signals(db, stock_id))
            except Exception as exc:  # noqa: BLE001 - one bad stock must not break the screen
                logger.warning(f"SignalService: skipping stock {stock_id} during screening: {exc}")
        return summaries


signal_service = SignalService()
signal_engine = SignalEngine()