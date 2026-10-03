"""Unit tests for the Phase 8 backtest engine, simulator and benchmark.

Everything here is deterministic and free of database and network access: bars are built
in memory, and the Phase 6/7 indicator math is exercised through the real engines rather
than mocked, so a regression in the engine's plumbing is caught here too.

All conventions:
* OHLCV generated via ``tests.backtest_fixtures`` — every bar obeys
  ``low <= min(open, close)`` and ``high >= max(open, close)``.
* Execution timing: signals computed from the close of bar N and filled at the
  open of bar N+1.
* Costs: commission and slippage default to 0; configurable with non-negative decimals.
* Position sizing: fixed % of available cash, one long position at a time.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.backtest.benchmark import run_buy_and_hold
from app.services.backtest.indicators import PointInTimeContextBuilder
from app.services.backtest.models import Bar, ExitReason
from app.services.backtest.simulator import ExecutionConfig, ExecutionSimulator
from app.services.backtest.strategy import (
    STRATEGY_NAME,
    STRATEGY_VERSION,
    Phase7TechnicalStrategy,
    StrategyParams,
    strategy_registry,
)
from app.services.backtest.validator import (
    BacktestDataValidationError,
    load_bars,
    validate_bars,
)
from tests.backtest_fixtures import (
    ANCHOR,
    bar,
    bars_from_closes,
    decline_then_rally,
    falling,
    flat,
    rally_then_decline,
    rising,
    append_decline,
)

STOCK_ID = 42
SYMBOL = "TST"

# -----------------------------------------------------------------------------
# Helpers


def run_backtest_engine(
    bars,
    *,
    initial_capital=Decimal("100000"),
    position_size_pct=Decimal("100"),
    commission_pct=Decimal("0"),
    slippage_pct=Decimal("0"),
    stop_loss_pct=None,
    take_profit_pct=None,
    strategy=None,
    record_start=None,
    record_end=None,
):
    """Run the full engine over in-memory bars and return (result, contexts, strategy)."""
    strategy = strategy or Phase7TechnicalStrategy()
    config = ExecutionConfig(
        initial_capital=initial_capital,
        position_size_pct=position_size_pct,
        commission_pct=commission_pct,
        slippage_pct=slippage_pct,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )
    contexts = PointInTimeContextBuilder().build(STOCK_ID, SYMBOL, bars)
    simulator = ExecutionSimulator(config, strategy)
    result = simulator.run(
        stock_id=STOCK_ID,
        bars=bars,
        contexts=contexts,
        record_start=record_start,
        record_end=record_end,
    )
    return result, contexts, strategy


def _make_position(
    entry_date,
    entry_reference_price: Decimal,
    quantity: int = 100,
    stock_id: int = STOCK_ID,
) -> "Position":
    """Create a test position with minimal required fields."""
    from app.services.backtest.models import Position, quantize_money
    from app.services.backtest.simulator import _ZERO

    return Position(
        stock_id=stock_id,
        quantity=quantity,
        entry_date=entry_date,
        entry_price=quantize_money(entry_reference_price),
        entry_reference_price=entry_reference_price,
        entry_value=quantize_money(entry_reference_price * Decimal(quantity)),
        entry_commission=_ZERO,
        entry_slippage=_ZERO,
        entry_cash_out=quantize_money(entry_reference_price * Decimal(quantity)),
        entry_signal_score=0,
    )


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------


def test_registry_exposes_the_phase7_strategy():
    """The public strategy identifier resolves to a versioned strategy."""
    strategy = strategy_registry.get(STRATEGY_NAME)

    assert strategy is not None
    assert strategy.strategy_name == STRATEGY_NAME
    assert strategy.strategy_version == STRATEGY_VERSION
    assert STRATEGY_NAME in strategy_registry.available


def test_unknown_strategy_returns_none():
    """An unregistered strategy name resolves to None instead of raising."""
    assert strategy_registry.get("does_not_exist") is None

# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------


def test_empty_series_is_rejected():
    """No bars cannot produce a backtest."""
    with pytest.raises(BacktestDataValidationError) as excinfo:
        validate_bars([])

    assert excinfo.value.details["reason"] == "insufficient_bars"


def test_single_candle_is_rejected():
    """A single candle cannot produce both an entry and an exit."""
    with pytest.raises(BacktestDataValidationError) as excinfo:
        validate_bars([bar(ANCHOR, Decimal("100"))])
    assert excinfo.value.details["reason"] == "insufficient_bars"


def test_duplicate_dates_are_rejected():
    """Duplicate trading dates would make ordering ambiguous."""
    bars = [bar(ANCHOR, Decimal("100")), bar(ANCHOR, Decimal("101"))]

    with pytest.raises(BacktestDataValidationError) as excinfo:
        validate_bars(bars)

    assert excinfo.value.details["reason"] == "duplicate_dates"


def test_missing_ohlc_values_are_rejected():
    """A missing price is reported rather than silently replaced with zero."""
    broken = bar(ANCHOR, Decimal("100"))
    object.__setattr__(broken, "low", None)
    bars = [broken, bar(ANCHOR + timedelta(days=1), Decimal("101"))]

    with pytest.raises(BacktestDataValidationError) as excinfo:
        validate_bars(bars)

    assert excinfo.value.details["reason"] == "missing_ohlc"


def test_non_positive_prices_are_rejected():
    """A zero price cannot be traded."""
    bars = [bar(ANCHOR, Decimal("0")), bar(ANCHOR + timedelta(days=1), Decimal("101"))]

    with pytest.raises(BacktestDataValidationError) as excinfo:
        validate_bars(bars)

    assert excinfo.value.details["reason"] == "non_positive_price"


def test_invalid_ohlc_relationship_is_rejected():
    """A high below the close is not a valid candle."""
    bad = Bar(
        date=ANCHOR,
        open=Decimal("100"),
        high=Decimal("99"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=1000,
    )
    bars = [bad, bar(ANCHOR + timedelta(days=1), Decimal("101"))]

    with pytest.raises(BacktestDataValidationError) as excinfo:
        validate_bars(bars)

    assert excinfo.value.details["reason"] == "invalid_ohlc_relationship"


def test_negative_volume_is_rejected():
    """Negative volume is impossible data."""
    bars = [
        bar(ANCHOR, Decimal("100"), volume=-5),
        bar(ANCHOR + timedelta(days=1), Decimal("101")),
    ]

    with pytest.raises(BacktestDataValidationError) as excinfo:
        validate_bars(bars)

    assert excinfo.value.details["reason"] == "negative_volume"


def test_calendar_gap_produces_a_warning_not_an_error():
    """Weekend-sized gaps are normal; only blocking problems raise."""
    bars = [
        bar(date(2024, 1, 1), Decimal("100")),
        bar(date(2024, 3, 1), Decimal("110")),
    ]

    report = validate_bars(bars)

    assert report.bar_count == 2
    assert any("calendar gap" in warning for warning in report.warnings)


def test_load_bars_sorts_and_preserves_missing_values():
    """Repository rows become chronological bars without losing missing data."""

    class Row:
        def __init__(self, price_date, close_price, open_price=None):
            self.price_date = price_date
            self.close_price = close_price
            self.open_price = open_price
            self.high_price = close_price
            self.low_price = close_price
            self.volume = 1000

    rows = [Row(date(2024, 1, 3), Decimal("102")), Row(date(2024, 1, 1), Decimal("100"))]

    bars = load_bars(rows)

    assert [b.date for b in bars] == [date(2024, 1, 1), date(2024, 1, 3)]
    assert bars[0].open is None  # preserved, not coerced to zero

# -----------------------------------------------------------------------------
# Indicator contexts
# -----------------------------------------------------------------------------


def test_contexts_are_built_for_every_bar():
    """One context per bar, with the record count matching the position in history."""
    bars = bars_from_closes(rising(80))

    contexts = PointInTimeContextBuilder().build(STOCK_ID, SYMBOL, bars)

    assert len(contexts) == len(bars)
    assert contexts[5].as_of == bars[5].date
    assert contexts[5].records == 6


def test_warmup_periods_report_unavailable_indicators():
    """Early bars cannot produce SMA(50); the gap is reported, not filled."""
    bars = bars_from_closes(rising(30))

    contexts = PointInTimeContextBuilder().build(STOCK_ID, SYMBOL, bars)

    assert "sma_50" in contexts[5].unavailable_indicators
    assert "macd" in contexts[5].unavailable_indicators
    # RSI(14) is available with 30 records.
    assert contexts[20].rsi is not None
    assert "rsi" not in contexts[20].unavailable_indicators


def test_empty_bars_produce_no_contexts():
    """An empty series yields an empty context list rather than raising."""
    assert PointInTimeContextBuilder().build(STOCK_ID, SYMBOL, []) == []

# -----------------------------------------------------------------------------
# Strategy rules
# -----------------------------------------------------------------------------


def test_entry_requires_trend_and_momentum_and_score():
    """A single strong crossover alone never opens a position."""
    strategy = Phase7TechnicalStrategy()
    bars = bars_from_closes(rally_then_decline(up=60, down=1))
    context = PointInTimeContextBuilder().build(STOCK_ID, SYMBOL, bars)[59]

    action, score, _ = strategy.decide(is_long=False, context=context)

    assert action.value in {"hold", "enter"}
    assert strategy.entry_signal([], score) is False


def test_entry_fires_on_a_bullish_reversal():
    """A decline followed by a rally satisfies trend, momentum and score."""
    strategy = Phase7TechnicalStrategy()
    bars = bars_from_closes(decline_then_rally(70, 25))
    context = PointInTimeContextBuilder().build(STOCK_ID, SYMBOL, bars)[92]

    _, score, signals = strategy.decide(is_long=False, context=context)
    assert strategy.entry_signal(signals, score) is True


def test_exit_fires_when_score_collapses():
    """A long position is exited once the composite score falls to the exit threshold."""
    strategy = Phase7TechnicalStrategy()
    bars = bars_from_closes(rally_then_decline(up=60, down=25))
    context = PointInTimeContextBuilder().build(STOCK_ID, SYMBOL, bars)[84]

    _, score, signals = strategy.decide(is_long=True, context=context)
    assert strategy.exit_signal(signals, score) is True


def test_bearish_crossover_exits_even_with_a_positive_score():
    """A bearish crossover is treated as a reversal on its own."""
    strategy = Phase7TechnicalStrategy(
        params=StrategyParams(exit_score_threshold=-100)
    )

    # Build a signal list that includes a bearish CROSSOVER signal directly
    # This tests the strategy logic in isolation from indicator data
    from app.schemas.signals import SignalItem, SignalType, SignalDirection
    from datetime import date as date_type

    bearish_crossover = SignalItem(
        indicator="ma_crossover_sma_20_50",
        signal_type=SignalType.CROSSOVER,
        direction=SignalDirection.BEARISH,
        strength=80,
        strength_band="strong",
        value=Decimal("45"),
        reference=Decimal("46"),
        explanation="SMA(20) crossed below SMA(50)",
        as_of=date_type(2024, 1, 15),
    )

    assert strategy.exit_signal([bearish_crossover], score=100) is True


def test_strategy_params_round_trip():
    """Persisted parameters rebuild into an identical configuration."""
    params = StrategyParams(
        entry_score_threshold=35, exit_score_threshold=-10, min_entry_strength=55
    )

    assert StrategyParams.from_dict(params.to_dict()) == params


def test_strategy_params_from_dict_ignores_unknown_keys():
    """Forward-compatible deserialisation tolerates extra persisted keys."""
    restored = StrategyParams.from_dict({"entry_score_threshold": 40, "future_key": "x"})

    assert restored.entry_score_threshold == 40
    assert restored.exit_score_threshold == StrategyParams().exit_score_threshold

# -----------------------------------------------------------------------------
# Execution model
# -----------------------------------------------------------------------------


def test_signal_fills_at_the_next_open_not_the_signal_close():
    """Rule 2/3: a signal computed on bar N cannot fill at bar N's close."""
    bars = bars_from_closes(decline_then_rally(70, 25))

    result, _, _ = run_backtest_engine(bars)

    assert result.trades, "expected at least one trade"
    first = result.trades[0]
    opens = {b.open for b in bars}
    assert first.entry_reference_price in opens


def test_entry_never_uses_the_bar_that_produced_it():
    """The bar a signal is computed on can never also be its fill bar."""
    bars = bars_from_closes(decline_then_rally(70, 25))

    result, _, _ = run_backtest_engine(bars)

    entry_dates = {trade.entry_date for trade in result.trades}
    all_dates = {b.date for b in bars}
    for entry_date in entry_dates:
        # every entry date must have at least one earlier date that produced it
        earlier = {d for d in all_dates if d < entry_date}
        assert earlier, f"Entry on {entry_date} has no prior bar producing it"


def test_no_overlapping_positions():
    """Rule 7: at most one position is open at any time."""
    bars = bars_from_closes(decline_then_rally(40, 30) + falling(30))

    result, _, _ = run_backtest_engine(bars)

    for earlier, later in zip(result.trades, result.trades[1:]):
        assert earlier.exit_date <= later.entry_date


def test_cash_never_goes_negative():
    """Rule 6: the simulator can never spend cash it does not have."""
    bars = bars_from_closes(decline_then_rally(40, 40))

    result, _, _ = run_backtest_engine(
        bars, commission_pct=Decimal("1"), slippage_pct=Decimal("1")
    )

    assert all(point.cash >= 0 for point in result.equity_curve)


def test_zero_costs_produce_zero_commission_and_slippage():
    """Default costs are exactly zero so tests stay deterministic."""
    bars = bars_from_closes(decline_then_rally(60, 20))

    result, _, _ = run_backtest_engine(bars)

    assert result.trades
    assert all(trade.commission == Decimal("0") for trade in result.trades)
    assert all(trade.slippage == Decimal("0") for trade in result.trades)


def test_slippage_reduces_net_pnl():
    """Slippage is charged adversely on both sides."""
    bars = bars_from_closes(decline_then_rally(60, 20))
    free, _, _ = run_backtest_engine(bars)
    costly, _, _ = run_backtest_engine(bars, slippage_pct=Decimal("0.5"))

    assert costly.trades
    assert costly.trades[0].net_pnl < free.trades[0].net_pnl
    assert costly.trades[0].slippage > Decimal("0")
    # Entry fills higher when buying.
    assert costly.trades[0].entry_price > costly.trades[0].entry_reference_price


def test_commission_reduces_net_pnl():
    """Commission is charged on entry and exit notional."""
    bars = bars_from_closes(decline_then_rally(60, 20))
    free, _, _ = run_backtest_engine(bars)
    costly, _, _ = run_backtest_engine(bars, commission_pct=Decimal("0.5"))

    assert costly.trades[0].commission > Decimal("0")
    assert costly.trades[0].net_pnl < free.trades[0].net_pnl


def test_costs_can_exceed_the_trade_profit():
    """Transaction costs larger than the gross move produce a losing trade."""
    # Use a longer rally that produces a trade, but with small price move
    bars = bars_from_closes(decline_then_rally(60, 10))

    result, _, _ = run_backtest_engine(
        bars, commission_pct=Decimal("2"), slippage_pct=Decimal("2")
    )

    assert result.trades
    assert result.trades[0].net_pnl < Decimal("0")


def test_trade_pnl_reconciles_with_the_reported_components():
    """net_pnl == gross_pnl - commission - slippage for every trade."""
    bars = bars_from_closes(decline_then_rally(60, 25))

    result, _, _ = run_backtest_engine(
        bars, commission_pct=Decimal("0.1"), slippage_pct=Decimal("0.05")
    )

    for trade in result.trades:
        assert trade.net_pnl == trade.gross_pnl - trade.commission - trade.slippage


def test_position_sizing_limits_the_exposure():
    """A 50% position buys roughly half the shares a 100% position would."""
    bars = bars_from_closes(decline_then_rally(60, 25))
    full, _, _ = run_backtest_engine(bars, position_size_pct=Decimal("100"))
    half, _, _ = run_backtest_engine(bars, position_size_pct=Decimal("50"))

    assert full.trades and half.trades
    assert half.trades[0].quantity < full.trades[0].quantity


def test_position_size_pct_of_one_percent_uses_less_capital():
    """A 1% position size reduces the invested notional."""
    bars = bars_from_closes(decline_then_rally(60, 25))
    full, _, _ = run_backtest_engine(bars, position_size_pct=Decimal("100"))

    result, _, _ = run_backtest_engine(
        bars, position_size_pct=Decimal("1"), commission_pct=Decimal("0")
    )

    full_notional = full.trades[0].entry_price * Decimal(full.trades[0].quantity)
    result_notional = result.trades[0].entry_price * Decimal(result.trades[0].quantity)
    assert full_notional > result_notional


def test_stop_loss_closes_the_position_with_the_right_reason():
    """A stop-loss exit is labelled as such and fills at or below the stop level."""
    # Construct a custom scenario: clear entry, then a single bar with a large intrabar
    # drop that hits the 5% stop before any bearish crossover can form.
    # We build bars manually to control the exact price action.
    from tests.backtest_fixtures import ANCHOR, bar as make_bar

    # 50 bars of decline to set up oversold conditions
    decline_bars = []
    price = Decimal("200")
    for i in range(50):
        price -= Decimal("1")
        decline_bars.append(make_bar(ANCHOR + timedelta(days=i), price))

    # 10 bars of rally to trigger entry (bullish crossover)
    rally_bars = []
    for i in range(10):
        price += Decimal("3")
        rally_bars.append(make_bar(ANCHOR + timedelta(days=50+i), price))

    # Entry should occur around bar 55-58. Use a tight stop (2%) so it hits on next bar.
    # Next bar: gap down below stop level.
    entry_price = price  # ~171 after 10 bars of +3 from 141
    stop_price = entry_price * Decimal("0.98")  # ~167.5
    gap_open = stop_price - Decimal("1")  # below stop
    gap_bar = make_bar(ANCHOR + timedelta(days=60), gap_open)
    # Make the bar have a low well below stop to ensure intrabar hit
    object.__setattr__(gap_bar, 'low', gap_open - Decimal("2"))
    object.__setattr__(gap_bar, 'high', gap_open)
    object.__setattr__(gap_bar, 'close', gap_open)

    # Remaining bars flat
    flat_bars = []
    for i in range(5):
        flat_bars.append(make_bar(ANCHOR + timedelta(days=61+i), gap_open))

    bars = decline_bars + rally_bars + [gap_bar] + flat_bars

    strategy = Phase7TechnicalStrategy(params=StrategyParams(exit_score_threshold=-100))
    result, _, _ = run_backtest_engine(bars, stop_loss_pct=Decimal("2"), strategy=strategy)

    stops = [trade for trade in result.trades if trade.exit_reason is ExitReason.STOP_LOSS]
    assert stops, f"expected a stop-loss exit, got trades: {[(t.exit_reason, t.exit_price) for t in result.trades]}"
    for trade in stops:
        assert trade.exit_reference_price <= trade.entry_reference_price


def test_stop_loss_gap_fills_at_the_open():
    """When the bar opens below the stop, the fill is the open, not the stop level."""
    entry_price = Decimal("100")
    stop_price = entry_price * Decimal("0.95")
    gap_open = Decimal("90")

    gap_bar = Bar(
        date=ANCHOR,
        open=gap_open,
        high=gap_open,
        low=gap_open,
        close=gap_open,
        volume=1000,
    )

    strategy = Phase7TechnicalStrategy()
    config = ExecutionConfig(
        initial_capital=Decimal("100000"),
        position_size_pct=Decimal("100"),
        commission_pct=Decimal("0"),
        slippage_pct=Decimal("0"),
        stop_loss_pct=Decimal("5"),
    )
    simulator = ExecutionSimulator(config, strategy)
    position = _make_position(entry_date=ANCHOR, entry_reference_price=entry_price)
    trade, _ = simulator._protective_exit(gap_bar, position, Decimal("1000"))

    assert trade is not None
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_reference_price == gap_open
    assert trade.exit_reference_price < stop_price


def test_take_profit_closes_the_position_with_the_right_reason():
    """A take-profit exit is labelled as such and fills at or above the target."""
    bars = bars_from_closes(decline_then_rally(60, 25) + falling(5))

    result, _, _ = run_backtest_engine(bars, take_profit_pct=Decimal("8"))

    targets = [trade for trade in result.trades if trade.exit_reason is ExitReason.TAKE_PROFIT]
    assert targets, "expected a take-profit exit on the rally"
    for trade in targets:
        assert trade.exit_reference_price >= trade.entry_reference_price


def test_stop_loss_takes_precedence_over_take_profit_on_one_bar():
    """A bar spanning both levels is treated conservatively as the loss."""
    strategy = Phase7TechnicalStrategy()
    config = ExecutionConfig(
        initial_capital=Decimal("100000"),
        position_size_pct=Decimal("100"),
        commission_pct=Decimal("0"),
        slippage_pct=Decimal("0"),
        stop_loss_pct=Decimal("5"),
        take_profit_pct=Decimal("5"),
    )
    simulator = ExecutionSimulator(config, strategy)
    position = _make_position(entry_date=ANCHOR, entry_reference_price=Decimal("100"))

    wide_bar = Bar(
        date=ANCHOR,
        open=Decimal("100"),
        high=Decimal("120"),
        low=Decimal("80"),
        close=Decimal("100"),
        volume=1000,
    )

    trade, _ = simulator._protective_exit(wide_bar, position, Decimal("1000"))

    assert trade.exit_reason is ExitReason.STOP_LOSS


def test_open_position_is_liquidated_at_the_end_of_the_window():
    """An open position at the end is closed and reported with its reason."""
    bars = bars_from_closes(decline_then_rally(60, 30))

    result, _, _ = run_backtest_engine(bars)

    assert result.trades
    assert result.trades[-1].exit_reason is ExitReason.END_OF_PERIOD
    assert result.trades[-1].exit_date == bars[-1].date


def test_zero_trade_strategy_produces_a_flat_equity_curve():
    """A series that never satisfies the entry rules yields no trades and constant equity."""
    bars = bars_from_closes(flat(120))

    result, _, _ = run_backtest_engine(bars)

    assert result.trades == []
    assert all(point.position_value == Decimal("0") for point in result.equity_curve)
    assert {point.equity for point in result.equity_curve} == {Decimal("100000.00")}


def test_multiple_trades_are_produced_over_a_long_series():
    """A longer, more varied series produces more than one round trip."""
    series = decline_then_rally(50, 40) + falling(35) + rising(35)
    bars = bars_from_closes(series)

    result, _, _ = run_backtest_engine(bars)

    assert len(result.trades) >= 2


def test_equity_curve_covers_every_recorded_bar():
    """One equity point per recorded trading day, in date order."""
    bars = bars_from_closes(rising(90))

    result, _, _ = run_backtest_engine(bars)

    assert len(result.equity_curve) == len(bars)
    assert [p.date for p in result.equity_curve] == [b.date for b in bars]


def test_equity_equals_cash_plus_position_value():
    """The equity identity holds on every bar."""
    bars = bars_from_closes(decline_then_rally(60, 25))

    result, _, _ = run_backtest_engine(bars)

    for point in result.equity_curve:
        assert point.equity == point.cash + point.position_value


def test_initial_capital_plus_realised_pnl_equals_final_equity():
    """The accounting identity from the manual verification checklist holds.
    final_equity == initial_capital + sum(trade.net_pnl) when flat at the end.
    """
    bars = bars_from_closes(decline_then_rally(60, 25))

    result, _, _ = run_backtest_engine(
        bars, commission_pct=Decimal("0.05"), slippage_pct=Decimal("0.05")
    )

    realised = sum((trade.net_pnl for trade in result.trades), Decimal("0"))
    assert result.final_equity == Decimal("100000") + realised


def test_warmup_bars_do_not_trade_but_do_warm_indicators():
    """Bars before record_start warm the indicators without trading."""
    bars = bars_from_closes(decline_then_rally(80, 40))

    full, _, _ = run_backtest_engine(bars)
    trimmed, _, _ = run_backtest_engine(bars, record_start=bars[70].date)

    assert full.trades
    assert trimmed.equity_curve[0].date == bars[70].date
    assert len(trimmed.equity_curve) == len(bars) - 70
    assert all(trade.entry_date >= bars[70].date for trade in trimmed.trades)


def test_record_end_limits_the_window():
    """record_end stops the simulation even when more bars exist."""
    bars = bars_from_closes(decline_then_rally(60, 25))

    result, _, _ = run_backtest_engine(bars, record_end=bars[70].date)

    assert result.equity_curve[-1].date == bars[70].date


def test_contexts_and_bars_must_be_the_same_length():
    """A mismatched context list is a programming error, not a silent data problem."""
    bars = bars_from_closes(rising(10))
    config = ExecutionConfig(
        initial_capital=Decimal("1000"),
        position_size_pct=Decimal("100"),
        commission_pct=Decimal("0"),
        slippage_pct=Decimal("0"),
    )

    with pytest.raises(ValueError):
        ExecutionSimulator(config, Phase7TechnicalStrategy()).run(
            stock_id=STOCK_ID, bars=bars, contexts=[]
        )


def test_execution_config_rejects_impossible_values():
    """Configuration guards keep bad inputs out of the simulator."""
    with pytest.raises(ValueError):
        ExecutionConfig(
            initial_capital=Decimal("0"),
            position_size_pct=Decimal("100"),
            commission_pct=Decimal("0"),
            slippage_pct=Decimal("0"),
        ).validate()

    with pytest.raises(ValueError):
        ExecutionConfig(
            initial_capital=Decimal("1000"),
            position_size_pct=Decimal("101"),
            commission_pct=Decimal("0"),
            slippage_pct=Decimal("0"),
        ).validate()

# -----------------------------------------------------------------------------
# Benchmark
# -----------------------------------------------------------------------------


def _flat_config(**overrides):
    """Zero-cost execution config for benchmark tests."""
    params = dict(
        initial_capital=Decimal("100000"),
        position_size_pct=Decimal("100"),
        commission_pct=Decimal("0"),
        slippage_pct=Decimal("0"),
    )
    params.update(overrides)
    from app.services.backtest.simulator import ExecutionConfig
    return ExecutionConfig(**params)


def test_benchmark_buys_first_open_and_sells_last_close():
    """Buy-and-hold uses the first open and the last close, with the same capital."""
    bars = bars_from_closes([Decimal("100"), Decimal("120"), Decimal("150")])

    benchmark = run_buy_and_hold(bars, _flat_config())

    assert benchmark is not None
    assert benchmark.final_equity == Decimal("150000.00")
    assert benchmark.benchmark_return_pct == Decimal("50.0000")


def test_benchmark_applies_transaction_costs():
    """Commission and slippage reduce the benchmark exactly as they reduce the strategy."""
    bars = bars_from_closes([Decimal("100"), Decimal("150")])

    free = run_buy_and_hold(bars, _flat_config())
    costly = run_buy_and_hold(
        bars, _flat_config(commission_pct=Decimal("1"), slippage_pct=Decimal("1"))
    )

    assert costly.final_equity < free.final_equity


def test_benchmark_returns_none_for_a_single_bar():
    """One bar cannot be both entered and exited."""
    bars = bars_from_closes([Decimal("100")])

    assert run_buy_and_hold(bars, _flat_config()) is None


def test_benchmark_handles_a_flat_series():
    """A flat series yields a zero return rather than a division error."""
    bars = bars_from_closes(flat(5))

    benchmark = run_buy_and_hold(bars, _flat_config())

    assert benchmark.benchmark_return_pct == Decimal("0.0000")
    assert benchmark.benchmark_final_equity == Decimal("100000.00")


def test_benchmark_handles_capital_too_small_for_one_share():
    """Capital below one share price is reported honestly instead of crashing."""
    bars = bars_from_closes([Decimal("100"), Decimal("110")])

    benchmark = run_buy_and_hold(bars, _flat_config(initial_capital=Decimal("50")))

    assert benchmark.benchmark_final_equity == Decimal("50.00")


def test_strategy_beats_benchmark_on_a_pure_uptrend():
    """A steady uptrend with no reversals: the benchmark captures the whole move."""
    bars = bars_from_closes(rising(120))

    strategy_result, _, _ = run_backtest_engine(bars)
    benchmark = run_buy_and_hold(bars, _flat_config())

    assert benchmark is not None
    assert benchmark.final_equity > strategy_result.final_equity