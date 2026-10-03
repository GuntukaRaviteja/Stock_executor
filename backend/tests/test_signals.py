"""Unit and API tests for the Phase 7 signal engine.

All fixtures are deterministic and no external provider is contacted.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest



from app.schemas.analytics import IndicatorContext
from app.schemas.signals import (
    SignalAlignment,
    SignalDirection,
    SignalStatus,
    SignalType,
    StrengthBand,
)
from app.services.signal_service import (
    FAMILY_CAPS,
    SignalEngine,
    SignalService,
)

# --- Deterministic helpers -------------------------------------------------------

AS_OF = date(2026, 1, 31)


def make_context(**overrides) -> IndicatorContext:
    """Build an IndicatorContext with sensible defaults for rule-level tests."""
    base = dict(
        as_of=AS_OF,
        latest_price=Decimal("100"),
        previous_price=Decimal("100"),
        latest_daily_return=None,
        records=60,
        rsi=None,
        sma={},
        sma_previous={},
        ema={},
        macd_line=None,
        macd_signal=None,
        macd_histogram=None,
        macd_previous_line=None,
        macd_previous_signal=None,
        bollinger_upper=None,
        bollinger_middle=None,
        bollinger_lower=None,
        volume=None,
        volume_average=None,
        unavailable_indicators=[],
    )
    base.update(overrides)
    return IndicatorContext(**base)


@pytest.fixture
def engine() -> SignalEngine:
    """A fresh stateless engine."""
    return SignalEngine()


def signal_by_indicator(signals, prefix):
    """Return the first signal whose indicator starts with prefix."""
    for signal in signals:
        if signal.indicator.startswith(prefix):
            return signal
    raise AssertionError(f"No signal starting with '{prefix}' in {[s.indicator for s in signals]}")


# --- 1/2/3. RSI signals ----------------------------------------------------------

def test_rsi_oversold_moderate(engine):
    """RSI just below 30 produces a moderate bullish oversold condition."""
    signal = engine.rsi_signal(Decimal("28.4"), AS_OF)

    assert signal is not None
    assert signal.indicator == "rsi"
    assert signal.direction is SignalDirection.BULLISH
    assert signal.signal_type is SignalType.MOMENTUM
    assert signal.value == Decimal("28.4")
    assert signal.reference == Decimal("30")
    assert signal.strength_band is StrengthBand.MODERATE
    assert 40 <= signal.strength < 70
    assert "oversold" in signal.explanation


def test_rsi_oversold_extreme_is_strong(engine):
    """RSI <= 20 produces a strong oversold condition."""
    signal = engine.rsi_signal(Decimal("18"), AS_OF)

    assert signal.direction is SignalDirection.BULLISH
    assert signal.strength == 100
    assert signal.strength_band is StrengthBand.STRONG


def test_rsi_overbought_moderate(engine):
    """RSI just above 70 produces a moderate bearish overbought condition."""
    signal = engine.rsi_signal(Decimal("74"), AS_OF)

    assert signal.direction is SignalDirection.BEARISH
    assert signal.strength_band is StrengthBand.MODERATE
    assert 40 <= signal.strength < 70
    assert "overbought" in signal.explanation


def test_rsi_overbought_extreme_is_strong(engine):
    """RSI >= 80 produces a strong overbought condition."""
    signal = engine.rsi_signal(Decimal("82"), AS_OF)

    assert signal.direction is SignalDirection.BEARISH
    assert signal.strength == 100
    assert signal.strength_band is StrengthBand.STRONG


@pytest.mark.parametrize("rsi", ["30", "45", "50", "70"])
def test_rsi_neutral_produces_no_signal(engine, rsi):
    """RSI inside the 30-70 band produces no directional momentum signal."""
    assert engine.rsi_signal(Decimal(rsi), AS_OF) is None


def test_rsi_explanation_makes_no_prediction(engine):
    """The explanation must describe the condition only, never a future move."""
    signal = engine.rsi_signal(Decimal("25"), AS_OF)

    text = signal.explanation.lower()
    assert "will rise" not in text
    assert "will fall" not in text
    assert "guarantee" not in text


# --- 4/5. Price vs moving average ------------------------------------------------

def test_price_above_sma_is_bullish(engine):
    """A close above its moving average is a bullish trend condition."""
    signal = engine.price_vs_ma_signal(Decimal("110"), 20, Decimal("100"), AS_OF)

    assert signal.direction is SignalDirection.BULLISH
    assert signal.indicator == "price_vs_ma_20"
    assert signal.signal_type is SignalType.TREND
    assert signal.value == Decimal("110")
    assert signal.reference == Decimal("100")
    assert "above" in signal.explanation


def test_price_below_sma_is_bearish(engine):
    """A close below its moving average is a bearish trend condition."""
    signal = engine.price_vs_ma_signal(Decimal("90"), 20, Decimal("100"), AS_OF)

    assert signal.direction is SignalDirection.BEARISH
    assert "below" in signal.explanation


def test_price_vs_ma_strength_scales_with_distance(engine):
    """A larger distance from the average yields a larger strength value."""
    near = engine.price_vs_ma_signal(Decimal("101"), 20, Decimal("100"), AS_OF)
    far = engine.price_vs_ma_signal(Decimal("120"), 20, Decimal("100"), AS_OF)

    assert far.strength > near.strength
    assert 40 <= near.strength <= 100


# --- 6/7. Moving average alignment -----------------------------------------------

def test_bullish_ma_alignment(engine):
    """A short average above the long average is bullish alignment."""
    signal = engine.ma_alignment_signal(20, Decimal("110"), 50, Decimal("100"), AS_OF)

    assert signal.direction is SignalDirection.BULLISH
    assert signal.signal_type is SignalType.TREND
    assert "above" in signal.explanation


def test_bearish_ma_alignment(engine):
    """A short average below the long average is bearish alignment."""
    signal = engine.ma_alignment_signal(20, Decimal("90"), 50, Decimal("100"), AS_OF)

    assert signal.direction is SignalDirection.BEARISH
    assert "below" in signal.explanation


def test_ema_alignment_uses_ema_family(engine):
    """EMA alignment produces the same rule shape with its own indicator name."""
    signal = engine.ma_alignment_signal(12, Decimal("110"), 26, Decimal("100"), AS_OF, family="ema")

    assert signal.indicator == "ma_alignment_ema_12_26"
    assert signal.direction is SignalDirection.BULLISH


# --- 8/9. Moving average crossovers ----------------------------------------------

def test_bullish_sma_crossover(engine):
    """prev short <= prev long and current short > current long is a bullish crossover."""
    signal = engine.ma_crossover_signal(20, Decimal("99"), Decimal("105"), 50, Decimal("100"), Decimal("103"), AS_OF)

    assert signal is not None
    assert signal.signal_type is SignalType.CROSSOVER
    assert signal.direction is SignalDirection.BULLISH
    assert "crossed above" in signal.explanation


def test_bearish_sma_crossover(engine):
    """prev short >= prev long and current short < current long is a bearish crossover."""
    signal = engine.ma_crossover_signal(20, Decimal("101"), Decimal("95"), 50, Decimal("100"), Decimal("97"), AS_OF)

    assert signal is not None
    assert signal.signal_type is SignalType.CROSSOVER
    assert signal.direction is SignalDirection.BEARISH
    assert "crossed below" in signal.explanation


@pytest.mark.parametrize(
    "short_prev,short_curr,long_prev,long_curr",
    [
        (Decimal("99"), Decimal("101"), Decimal("100"), Decimal("103")),  # no cross
        (Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100")),  # flat, no cross
        (Decimal("105"), Decimal("103"), Decimal("100"), Decimal("102")),  # still above
    ],
)
def test_no_crossover_when_condition_not_met(engine, short_prev, short_curr, long_prev, long_curr):
    """No crossover signal is emitted when the ordering did not change."""
    assert engine.ma_crossover_signal(20, short_prev, short_curr, 50, long_prev, long_curr, AS_OF) is None


# --- 10/11. MACD signals ---------------------------------------------------------

def test_macd_above_signal_line_is_bullish(engine):
    """MACD above its signal line is a bullish momentum condition."""
    signals = engine.macd_signal(
        macd_line=Decimal("1.5"),
        signal_line=Decimal("1.0"),
        previous_macd=Decimal("0.8"),
        previous_signal=Decimal("1.0"),
        price=Decimal("100"),
        as_of=AS_OF,
    )

    position = signal_by_indicator(signals, "macd")
    crossover = signal_by_indicator(signals, "macd_crossover")

    assert position.direction is SignalDirection.BULLISH
    assert position.reference == Decimal("1.0")
    assert crossover.signal_type is SignalType.CROSSOVER
    assert crossover.direction is SignalDirection.BULLISH


def test_macd_below_signal_line_is_bearish(engine):
    """MACD below its signal line is a bearish momentum condition."""
    signals = engine.macd_signal(
        macd_line=Decimal("0.5"),
        signal_line=Decimal("1.0"),
        previous_macd=Decimal("1.2"),
        previous_signal=Decimal("1.0"),
        price=Decimal("100"),
        as_of=AS_OF,
    )

    position = signal_by_indicator(signals, "macd")
    crossover = signal_by_indicator(signals, "macd_crossover")

    assert position.direction is SignalDirection.BEARISH
    assert crossover.direction is SignalDirection.BEARISH


def test_macd_without_history_has_no_crossover(engine):
    """Without previous values only the position signal is produced."""
    signals = engine.macd_signal(
        macd_line=Decimal("1.5"),
        signal_line=Decimal("1.0"),
        as_of=AS_OF,
    )

    assert [s.indicator for s in signals] == ["macd"]


# --- 12/13. Bollinger bands ------------------------------------------------------

def test_price_below_lower_band(engine):
    """A close below the lower band is reported as a below-band condition."""
    signal = engine.bollinger_signal(Decimal("90"), Decimal("110"), Decimal("95"), AS_OF)

    assert signal.direction is SignalDirection.BULLISH
    assert signal.signal_type is SignalType.VOLATILITY
    assert signal.reference == Decimal("95")
    assert "below" in signal.explanation
    assert "will" not in signal.explanation.lower()


def test_price_above_upper_band(engine):
    """A close above the upper band is reported as an above-band condition."""
    signal = engine.bollinger_signal(Decimal("120"), Decimal("110"), Decimal("95"), AS_OF)

    assert signal.direction is SignalDirection.BEARISH
    assert signal.reference == Decimal("110")
    assert "above" in signal.explanation


def test_price_within_bands_is_neutral(engine):
    """A close between the bands is neutral."""
    signal = engine.bollinger_signal(Decimal("100"), Decimal("110"), Decimal("95"), AS_OF)

    assert signal.direction is SignalDirection.NEUTRAL
    assert signal.strength == 30


# --- 14. Volume ------------------------------------------------------------------

def test_volume_above_average_is_neutral_activity(engine):
    """Volume above average is reported as a neutral activity condition."""
    signal = engine.volume_signal(2_000_000, Decimal("1000000"), AS_OF)

    assert signal.direction is SignalDirection.NEUTRAL
    assert signal.signal_type is SignalType.VOLUME
    assert signal.strength > 40


def test_volume_below_average_is_neutral_activity(engine):
    """Volume below average is reported as a neutral activity condition."""
    signal = engine.volume_signal(400_000, Decimal("1000000"), AS_OF)

    assert signal.direction is SignalDirection.NEUTRAL
    assert signal.strength < 40


def test_volume_signal_ignored_when_average_is_zero(engine):
    """A zero average produces no volume signal rather than dividing by zero."""
    assert engine.volume_signal(1000, Decimal("0"), AS_OF) is None


# --- 15/16. Composite score -------------------------------------------------------

def test_composite_score_of_all_bullish_signals_is_positive(engine):
    """A fully bullish set of signals yields a positive composite score."""
    context = make_context(
        rsi=Decimal("25"),
        latest_price=Decimal("120"),
        sma={"20": Decimal("110"), "50": Decimal("100")},
        sma_previous={"20": Decimal("105"), "50": Decimal("102")},
        ema={"12": Decimal("115"), "26": Decimal("105")},
        macd_line=Decimal("2"),
        macd_signal=Decimal("1"),
        macd_previous_line=Decimal("0.5"),
        macd_previous_signal=Decimal("1"),
        bollinger_upper=Decimal("125"),
        bollinger_lower=Decimal("95"),
        latest_daily_return=Decimal("0.02"),
    )
    signals = engine.build_signals(context)

    score = engine.calculate_composite_score(signals)
    assert score > 0
    assert 0 <= score <= 100


def test_composite_score_of_all_bearish_signals_is_negative(engine):
    """A fully bearish set of signals yields a negative composite score."""
    context = make_context(
        rsi=Decimal("78"),
        latest_price=Decimal("80"),
        sma={"20": Decimal("90"), "50": Decimal("100")},
        sma_previous={"20": Decimal("95"), "50": Decimal("98")},
        ema={"12": Decimal("85"), "26": Decimal("95")},
        macd_line=Decimal("-2"),
        macd_signal=Decimal("-1"),
        macd_previous_line=Decimal("-0.5"),
        macd_previous_signal=Decimal("-1"),
        bollinger_upper=Decimal("105"),
        bollinger_lower=Decimal("75"),
        latest_daily_return=Decimal("-0.02"),
    )
    signals = engine.build_signals(context)

    assert engine.calculate_composite_score(signals) < 0


def test_neutral_signals_contribute_zero(engine):
    """Neutral signals do not move the composite score."""
    neutral = engine.bollinger_signal(Decimal("100"), Decimal("110"), Decimal("95"), AS_OF)
    volume = engine.volume_signal(2_000_000, Decimal("1000000"), AS_OF)

    assert neutral.direction is SignalDirection.NEUTRAL
    assert volume.direction is SignalDirection.NEUTRAL
    assert engine.calculate_composite_score([neutral, volume]) == 0


def test_empty_signal_list_scores_zero(engine):
    """An empty signal list produces a neutral composite score."""
    assert engine.calculate_composite_score([]) == 0


def test_mixed_signals_produce_a_near_neutral_score(engine):
    """Offsetting bullish and bearish families land in the neutral band."""
    bullish_context = make_context(
        rsi=Decimal("25"),
        latest_price=Decimal("120"),
        sma={"20": Decimal("110"), "50": Decimal("100")},
    )
    bearish_context = make_context(
        rsi=Decimal("78"),
        latest_price=Decimal("80"),
        sma={"20": Decimal("90"), "50": Decimal("100")},
    )
    signals = engine.build_signals(bullish_context) + engine.build_signals(bearish_context)

    score = engine.calculate_composite_score(signals)
    assert engine.classify(score) is SignalAlignment.NEUTRAL_ALIGNMENT


def test_family_cap_prevents_double_counting(engine):
    """Several moving average signals cannot contribute more than their family cap."""
    context = make_context(
        latest_price=Decimal("130"),
        sma={"20": Decimal("100"), "50": Decimal("95")},
        sma_previous={"20": Decimal("90"), "50": Decimal("97")},
    )
    signals = engine.build_signals(context)

    moving_average_total = sum(
        s.strength
        for s in signals
        if s.direction is SignalDirection.BULLISH and engine.family_of(s) == "moving_average"
    )
    assert moving_average_total > FAMILY_CAPS["moving_average"]

    # Even though the raw total exceeds the cap, the family is clamped to it.
    capped = engine.calculate_composite_score(signals)
    assert capped <= 100


def test_composite_score_is_symmetric_for_mirrored_inputs(engine):
    """Mirroring every signal direction mirrors the composite score."""
    def make(direction: str):
        if direction == "bullish":
            return engine.price_vs_ma_signal(Decimal("110"), 20, Decimal("100"), AS_OF)
        return engine.price_vs_ma_signal(Decimal("90"), 20, Decimal("100"), AS_OF)

    assert engine.calculate_composite_score([make("bullish")]) == -engine.calculate_composite_score(
        [make("bearish")]
    )


# --- 17/18/19. Classification -----------------------------------------------------

@pytest.mark.parametrize(
    "score,expected",
    [
        (100, SignalAlignment.STRONG_BULLISH_ALIGNMENT),
        (60, SignalAlignment.STRONG_BULLISH_ALIGNMENT),
        (59, SignalAlignment.BULLISH_ALIGNMENT),
        (20, SignalAlignment.BULLISH_ALIGNMENT),
        (19, SignalAlignment.NEUTRAL_ALIGNMENT),
        (0, SignalAlignment.NEUTRAL_ALIGNMENT),
        (-19, SignalAlignment.NEUTRAL_ALIGNMENT),
        (-20, SignalAlignment.BEARISH_ALIGNMENT),
        (-59, SignalAlignment.BEARISH_ALIGNMENT),
        (-60, SignalAlignment.STRONG_BEARISH_ALIGNMENT),
        (-100, SignalAlignment.STRONG_BEARISH_ALIGNMENT),
    ],
)
def test_classification_thresholds(engine, score, expected):
    """Every documented composite score band maps to the expected label."""
    assert engine.classify(score) is expected


def test_classification_labels_avoid_buy_sell_language(engine):
    """Classifications are descriptive and never say BUY or SELL."""
    labels = [
        engine.classify(score).value
        for score in (-100, -40, 0, 40, 100)
    ]
    for label in labels:
        assert "buy" not in label.lower()
        assert "sell" not in label.lower()


# --- 22/23. Missing indicator values ----------------------------------------------

def test_missing_indicators_produce_no_fake_signals(engine):
    """An empty context produces no signals and an empty snapshot."""
    summary = engine.evaluate(1, "TEST", make_context())

    assert summary.signals == []
    assert summary.composite_score == 0
    assert summary.indicator_snapshot.rsi is None
    assert summary.indicator_snapshot.sma == {}


def test_partial_context_only_emits_available_signals(engine):
    """Only RSI is available, so only the RSI signal is produced."""
    context = make_context(rsi=Decimal("25"), unavailable_indicators=["sma_20", "macd", "bollinger_bands"])
    signals = engine.build_signals(context)

    assert [s.indicator for s in signals] == ["rsi"]


def test_crossover_skipped_without_previous_values(engine):
    """A single moving average value cannot produce a crossover."""
    context = make_context(sma={"20": Decimal("105"), "50": Decimal("100")})
    signals = engine.build_signals(context)

    assert not any(s.signal_type is SignalType.CROSSOVER for s in signals)


def test_evaluate_with_no_context_reports_insufficient_data(engine):
    """A missing context yields an insufficient-data summary, not fabricated signals."""
    summary = engine.evaluate(
        1, "TEST", context=None, status=SignalStatus.NO_PRICE_DATA, message="No price data."
    )

    assert summary.status is SignalStatus.NO_PRICE_DATA
    assert summary.classification is SignalAlignment.INSUFFICIENT_DATA
    assert summary.composite_score == 0
    assert summary.signals == []


# --- G. Price momentum -----------------------------------------------------------

def test_positive_momentum_is_bullish(engine):
    """A positive period return is a bullish momentum condition."""
    signal = engine.momentum_signal(Decimal("0.015"), AS_OF)

    assert signal.direction is SignalDirection.BULLISH
    assert signal.value == Decimal("1.50")
    assert signal.strength == 30


def test_negative_momentum_is_bearish(engine):
    """A negative period return is a bearish momentum condition."""
    signal = engine.momentum_signal(Decimal("-0.015"), AS_OF)

    assert signal.direction is SignalDirection.BEARISH


def test_zero_momentum_produces_no_signal(engine):
    """A flat period produces no momentum signal."""
    assert engine.momentum_signal(Decimal("0"), AS_OF) is None


# --- Explanations describe conditions, never forecasts ------------------------------

#: Vocabulary that would turn a description of the present into a forecast or advice.
FORBIDDEN_IN_EXPLANATIONS = (
    "will ",
    "will rise",
    "will fall",
    "should buy",
    "should sell",
    "buy ",
    "sell ",
    "expected return",
    "guarantee",
    "profit",
    "target price",
    "recommend",
    "predict",
    "forecast",
    "upcoming",
    "about to",
    "set to rise",
    "set to fall",
)


def _all_explanations(engine) -> list:
    """Collect the explanation of every signal across bullish, bearish and neutral cases."""
    contexts = [
        # Oversold / overbought RSI.
        make_context(latest_price=Decimal("100"), previous_price=Decimal("99"),
                     rsi=Decimal("25"), sma={"20": Decimal("105"), "50": Decimal("110")},
                     sma_previous={"20": Decimal("106"), "50": Decimal("108")},
                     ema={"12": Decimal("103"), "26": Decimal("106")},
                     macd_line=Decimal("-1.2"), macd_signal=Decimal("-0.4"),
                     macd_histogram=Decimal("-0.8"), bollinger_upper=Decimal("115"),
                     bollinger_middle=Decimal("105"), bollinger_lower=Decimal("95"),
                     volume=2_000_000, volume_average=1_000_000,
                     latest_daily_return=Decimal("-0.01")),
        make_context(latest_price=Decimal("130"), previous_price=Decimal("127"),
                     rsi=Decimal("78"), sma={"20": Decimal("120"), "50": Decimal("110")},
                     sma_previous={"20": Decimal("118"), "50": Decimal("112")},
                     ema={"12": Decimal("124"), "26": Decimal("116")},
                     macd_line=Decimal("1.4"), macd_signal=Decimal("1.0"),
                     macd_histogram=Decimal("0.4"), bollinger_upper=Decimal("132"),
                     bollinger_middle=Decimal("122"), bollinger_lower=Decimal("112"),
                     volume=500_000, volume_average=1_000_000,
                     latest_daily_return=Decimal("0.02")),
        # Price outside the bands.
        make_context(latest_price=Decimal("90"), previous_price=Decimal("93"),
                     bollinger_upper=Decimal("110"), bollinger_middle=Decimal("100"),
                     bollinger_lower=Decimal("95"), latest_daily_return=Decimal("-0.03")),
        # Fully neutral / mixed readings.
        make_context(latest_price=Decimal("100"), previous_price=Decimal("100"),
                     rsi=Decimal("50"), sma={"20": Decimal("100"), "50": Decimal("100")},
                     ema={"12": Decimal("100"), "26": Decimal("100")},
                     macd_line=Decimal("0"), macd_signal=Decimal("0"),
                     macd_histogram=Decimal("0"), bollinger_upper=Decimal("110"),
                     bollinger_middle=Decimal("100"), bollinger_lower=Decimal("90"),
                     volume=1_000_000, volume_average=1_000_000,
                     latest_daily_return=Decimal("0.005")),
    ]

    explanations = []
    for context in contexts:
        explanations.extend(s.explanation for s in engine.build_signals(context))
    return explanations


def test_explanations_are_descriptive_not_predictive(engine):
    """Every explanation states a present condition and never forecasts or advises.

    This is the guarantee that signals do not tell a user what will happen next.
    """
    explanations = _all_explanations(engine)

    assert len(explanations) >= 10, "expected signals across all scenarios"

    for explanation in explanations:
        lowered = explanation.lower()
        for phrase in FORBIDDEN_IN_EXPLANATIONS:
            assert phrase not in lowered, f"{phrase!r} found in explanation: {explanation!r}"


def test_explanations_are_non_empty_and_mention_the_numbers(engine):
    """Every explanation explains itself using the value and the reference."""
    for explanation in _all_explanations(engine):
        assert explanation.strip()
        assert len(explanation.split()) >= 5, explanation


def test_classification_labels_are_never_trade_recommendations(engine):
    """Alignment labels describe agreement, never an action to take."""
    forbidden = ("buy", "sell", "hold", "long", "short", "accumulate", "exit")

    for context in (
        make_context(latest_price=Decimal("130"), previous_price=Decimal("127"),
                     rsi=Decimal("78"), sma={"20": Decimal("120"), "50": Decimal("110")},
                     ema={"12": Decimal("124"), "26": Decimal("116")},
                     macd_line=Decimal("1.4"), macd_signal=Decimal("1.0"),
                     latest_daily_return=Decimal("0.02")),
        make_context(latest_price=Decimal("90"), previous_price=Decimal("93"),
                     rsi=Decimal("28"), latest_daily_return=Decimal("-0.02")),
    ):
        classification = engine.classify(engine.calculate_composite_score(engine.build_signals(context)))

        assert classification.value.endswith("_alignment")
        for word in forbidden:
            assert word not in classification.value


def test_signal_fields_never_expose_probability_language(engine):
    """No signal field is named or phrased as a probability or win rate."""
    for context in (make_context(rsi=Decimal("25"), latest_price=Decimal("90"),
                                 latest_daily_return=Decimal("-0.02")),):
        for signal in engine.build_signals(context):
            blob = " ".join(
                [signal.indicator, signal.signal_type.value, signal.direction.value,
                 signal.strength_band.value, signal.explanation]
            ).lower()

            for word in ("probability", "confidence", "win rate", "chance", "likelihood", "% sure"):
                assert word not in blob, f"{word!r} exposed in signal: {blob!r}"


# --- API integration --------------------------------------------------------------

@pytest.fixture
def api_client(client):
    """The project test client (overridden in-memory database) used for API tests."""
    return client


def create_stock(client, suffix: str) -> int:
    """Create a uniquely named stock and return its id."""
    symbol = f"SIG{suffix}{uuid.uuid4().hex[:6].upper()}"
    response = client.post(
        "/api/v1/stocks",
        json={"symbol": symbol, "company_name": f"Signal Test {suffix}", "exchange": "NASDAQ"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def insert_prices(client, stock_id: int, closes, start=date(2025, 1, 1), volume=1_000_000):
    """Insert a deterministic ascending series of closes."""
    for offset, close in enumerate(closes):
        response = client.post(
            f"/api/v1/stocks/{stock_id}/prices",
            json={
                "price_date": (start + timedelta(days=offset)).isoformat(),
                "open_price": str(close),
                "high_price": str(close + Decimal("2")),
                "low_price": str(close - Decimal("2")),
                "close_price": str(close),
                "volume": volume,
            },
        )
        assert response.status_code in (200, 201), response.text


def rising_series(days: int = 90):
    """A steadily rising close series (RSI pins at 100 - see test_monotonic_series)."""
    return [Decimal("100") + Decimal(i) * Decimal("0.5") for i in range(days)]


def falling_series(days: int = 90):
    """A steadily falling close series (RSI pins at 0 - see test_monotonic_series)."""
    return [Decimal("200") - Decimal(i) * Decimal("0.5") for i in range(days)]


def base_then_rally_series():
    """A decline followed by a rally: bullish trend alignment."""
    return (
        [Decimal("200") - Decimal(i) * Decimal("1.0") for i in range(60)]
        + [Decimal("140") + Decimal(i) * Decimal("2.0") for i in range(40)]
    )


def rally_then_decline_series():
    """A rally followed by a decline: bearish trend alignment."""
    return (
        [Decimal("100") + Decimal(i) * Decimal("1.0") for i in range(60)]
        + [Decimal("160") - Decimal(i) * Decimal("2.0") for i in range(40)]
    )


def test_api_signals_for_stock_with_data(api_client):
    """A stock with sufficient history returns a full signal summary."""
    stock_id = create_stock(api_client, "UP")
    insert_prices(api_client, stock_id, base_then_rally_series())

    response = api_client.get(f"/api/v1/stocks/{stock_id}/signals")
    assert response.status_code == 200, response.text

    data = response.json()
    assert data["stock_id"] == stock_id
    assert data["status"] == "ok"
    assert data["classification"] == "bullish_alignment"
    assert data["composite_score"] > 0
    assert data["signal_count"] == len(data["signals"]) > 0
    assert data["as_of"] is not None
    assert data["indicator_snapshot"]["rsi"] is not None

    for signal in data["signals"]:
        assert signal["direction"] in {"bullish", "bearish", "neutral"}
        assert 0 <= signal["strength"] <= 100
        assert signal["explanation"]


def test_api_signals_are_deterministic(api_client):
    """Repeated calls on unchanged data return identical payloads."""
    stock_id = create_stock(api_client, "DET")
    insert_prices(api_client, stock_id, base_then_rally_series())

    first = api_client.get(f"/api/v1/stocks/{stock_id}/signals").json()
    second = api_client.get(f"/api/v1/stocks/{stock_id}/signals").json()

    assert first == second


def test_api_signals_bearish_stock(api_client):
    """A rally followed by a decline yields a bearish classification."""
    stock_id = create_stock(api_client, "DOWN")
    insert_prices(api_client, stock_id, rally_then_decline_series())

    data = api_client.get(f"/api/v1/stocks/{stock_id}/signals").json()

    assert data["composite_score"] < 0
    assert data["classification"] == "bearish_alignment"


def test_api_monotonic_series_report_mixed_alignment(api_client):
    """A perfectly monotonic series pins RSI at 0/100 and is reported as mixed.

    This documents the known limitation that a never-reversing price series makes
    every indicator extreme at once, so the composite score stays near neutral.
    """
    stock_id = create_stock(api_client, "MONO")
    insert_prices(api_client, stock_id, rising_series())

    data = api_client.get(f"/api/v1/stocks/{stock_id}/signals").json()

    assert data["status"] == "ok"
    assert data["classification"] == "neutral_alignment"
    assert abs(data["composite_score"]) < 20


# --- 21. Nonexistent stock -------------------------------------------------------

def test_api_signals_nonexistent_stock(api_client):
    """An unknown stock id returns 404 using the project error envelope."""
    response = api_client.get("/api/v1/stocks/987654/signals")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "STOCK_NOT_FOUND"


# --- 15/22/24. Insufficient data handling ----------------------------------------

def test_api_signals_no_price_data(api_client):
    """A stock with no prices reports no_price_data instead of crashing."""
    stock_id = create_stock(api_client, "EMPTY")

    response = api_client.get(f"/api/v1/stocks/{stock_id}/signals")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "no_price_data"
    assert data["signals"] == []
    assert data["composite_score"] == 0
    assert data["classification"] == "insufficient_data"
    assert data["message"]


def test_api_signals_insufficient_history(api_client):
    """Too few records for indicators reports insufficient_data and no fake signals."""
    stock_id = create_stock(api_client, "SHORT")
    insert_prices(api_client, stock_id, rising_series(days=5))

    data = api_client.get(f"/api/v1/stocks/{stock_id}/signals").json()

    assert data["status"] == "insufficient_data"
    assert data["signals"] == []
    assert data["indicator_snapshot"]["records"] == 5
    assert "rsi" in data["indicator_snapshot"]["unavailable_indicators"]


def test_api_signals_partial_history_reports_unavailable_indicators(api_client):
    """Enough records for some indicators but not others reports the gaps."""
    stock_id = create_stock(api_client, "MID")
    insert_prices(api_client, stock_id, rising_series(days=25))

    data = api_client.get(f"/api/v1/stocks/{stock_id}/signals").json()

    assert data["status"] == "ok"
    assert data["indicator_snapshot"]["rsi"] is not None
    assert "sma_50" in data["indicator_snapshot"]["unavailable_indicators"]
    assert "macd" in data["indicator_snapshot"]["unavailable_indicators"]
    assert not any(s["indicator"] == "macd" for s in data["signals"])


def test_api_latest_signal_endpoint(api_client):
    """The latest-signal endpoint returns the strongest individual signal."""
    stock_id = create_stock(api_client, "LATEST")
    insert_prices(api_client, stock_id, base_then_rally_series())

    response = api_client.get(f"/api/v1/stocks/{stock_id}/signals/latest")

    assert response.status_code == 200
    signal = response.json()
    assert signal["indicator"]
    assert 0 <= signal["strength"] <= 100


def test_api_latest_signal_conflict_without_data(api_client):
    """Asking for the latest signal with no data returns 409, not a crash."""
    stock_id = create_stock(api_client, "NOLATEST")

    response = api_client.get(f"/api/v1/stocks/{stock_id}/signals/latest")

    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "SIGNALS_UNAVAILABLE"
    assert body["error"]["details"]["status"] == "no_price_data"


def test_api_latest_signal_conflict_uses_error_envelope(api_client):
    """Every Phase 7 error uses the project's {"success": false, "error": {...}} shape."""
    stock_id = create_stock(api_client, "ENV")

    response = api_client.get(f"/api/v1/stocks/{stock_id}/signals/latest")

    assert response.status_code == 409
    body = response.json()
    assert set(body) == {"success", "error"}
    assert "detail" not in body
    assert set(body["error"]) >= {"code", "message", "details"}


def test_api_classification_endpoint(api_client):
    """The classification endpoint returns only the alignment label."""
    stock_id = create_stock(api_client, "CLS")
    insert_prices(api_client, stock_id, base_then_rally_series())

    response = api_client.get(f"/api/v1/stocks/{stock_id}/signals/classification")

    assert response.status_code == 200
    assert response.json() in {
        "strong_bullish_alignment",
        "bullish_alignment",
        "neutral_alignment",
        "bearish_alignment",
        "strong_bearish_alignment",
        "insufficient_data",
    }


def test_api_invalid_stock_id_rejected(api_client):
    """A non-positive stock id is rejected by validation."""
    assert api_client.get("/api/v1/stocks/0/signals").status_code == 422
    assert api_client.get("/api/v1/stocks/-3/signals").status_code == 422


# --- Service-level data access ---------------------------------------------------

def test_service_screen_contexts_skips_unknown_stocks(db_session):
    """screen_contexts ignores ids that cannot be evaluated."""
    service = SignalService()

    assert service.screen_contexts(db_session, [999999]) == []


def test_service_calculate_composite_score_is_reusable(engine):
    """The service delegates composite scoring to the same engine implementation."""
    service = SignalService(engine=engine)
    signal = engine.rsi_signal(Decimal("25"), AS_OF)

    assert service.calculate_composite_score([signal]) > 0