"""Phase 10 paper-trading engine tests.

The engine is tested through a **recording strategy** rather than the Phase 7 technical
strategy. That is deliberate: the look-ahead guarantee is about *timing*, so the tests control
exactly when a decision is taken and assert the fill price came from the correct bar. A
separate test then runs the real Phase 7 strategy end-to-end to prove the wiring works.

Groups:

* exact sequential BUY / SELL execution with hand-computed cash and P&L;
* the two execution-price policies;
* position sizing inside a run;
* risk refusals recorded as rejected orders, not exceptions;
* equity-curve and snapshot persistence;
* look-ahead prevention — the mandatory regression test;
* atomicity.
"""

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.schemas.analytics import IndicatorContext
from app.schemas.portfolio import ExecutionPricePolicy, OrderSide, OrderStatus, StrategyAction
from app.services.backtest.models import Bar
from app.services.backtest.strategy import STRATEGY_NAME, strategy_registry
from app.services.portfolio.models import PositionSizingMethod
from app.services.portfolio.paper_engine import INDICATOR_WARMUP_CALENDAR_DAYS, PaperTradingEngine
from tests.phase10_helpers import create_portfolio, create_stock, insert_prices


# ----------------------------------------------------------------------
# Test doubles: a strategy whose decisions we control exactly
# ----------------------------------------------------------------------


class ScriptedStrategy:
    """A strategy that enters on one bar and exits on another, by date.

    Recording the decisions here rather than hand-rolling an engine stub keeps the timing
    assertions in the test honest: the only thing the engine controls is *when* it asks.
    """

    strategy_name = "scripted_test_strategy"
    strategy_version = "1.0.0"

    def __init__(self, enter_on=(), exit_on=()):
        self.enter_on = set(enter_on)
        self.exit_on = set(exit_on)
        self.seen_bars = []

    def decide(self, is_long: bool, context: IndicatorContext):
        bar_date = context.as_of
        self.seen_bars.append(bar_date)
        if is_long:
            if bar_date in self.exit_on:
                return StrategyAction.EXIT, 80, []
            return StrategyAction.HOLD, 0, []
        if bar_date in self.enter_on:
            return StrategyAction.ENTER, 75, []
        return StrategyAction.HOLD, 0, []


class AlwaysEnterStrategy(ScriptedStrategy):
    """Enters on every bar while flat — exercises repeated entry refusal."""

    def decide(self, is_long: bool, context: IndicatorContext):
        self.seen_bars.append(context.as_of)
        if is_long:
            return StrategyAction.HOLD, 0, []
        return StrategyAction.ENTER, 75, []


class SingleRegistry:
    """Registry stub serving one strategy under any requested name.

    The scripted strategies have their own names, but ``PaperTradingEngine.run`` defaults to
    the production strategy name; resolving any name to the scripted instance keeps these
    tests focused on execution timing. Unknown-strategy refusal is covered by a separate test
    that uses the real registry.
    """

    def __init__(self, strategy):
        self._strategy = strategy

    def get(self, name):
        return self._strategy

    @property
    def available(self):
        return [self._strategy.strategy_name]


class NoWarmupBuilder:
    """Passes bars through as contexts, so short test windows can drive decisions.

    The real engine uses the Phase 8 point-in-time builder, which needs a 50-bar warm-up;
    these tests are about execution timing, not indicator maths, and the look-ahead test
    covers the real builder explicitly.
    """

    def build(self, stock_id, symbol, bars):
        return [
            IndicatorContext(
                as_of=bar.date,
                stock_id=stock_id,
                symbol=symbol,
                latest_price=Decimal(str(bar.close)),
                records=index + 1,
            )
            for index, bar in enumerate(bars)
        ]


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def bar(day: int, open_price, high, low, close) -> "tuple":
    return (date(2026, 1, day), open_price, high, low, close)


#: ``db_session`` and ``client`` come from tests/conftest.py.
#: Every OHLCV bar here is strictly rising with distinct prices, so a fill drawn from the
#: wrong bar is detectable rather than merely improbable.


@pytest.fixture
def rising_bars():
    """Six consecutive rising bars. Every open is exactly one below the close."""
    return [bar(d, 100 + d, 105 + d, 99 + d, 101 + d) for d in range(1, 7)]


def engine_for(strategy, builder=None) -> PaperTradingEngine:
    return PaperTradingEngine(
        registry=SingleRegistry(strategy),
        context_builder=builder or NoWarmupBuilder(),
    )


def run(engine, db, portfolio, stock_id, **kwargs):
    return engine.run(db, portfolio, stock_id, **kwargs)


# ----------------------------------------------------------------------
# Exact sequential execution
# ----------------------------------------------------------------------


class TestSequentialExecution:
    def test_a_buy_fills_at_the_next_bars_open(self, db_session, rising_bars):
        """Signal at bar 1's close fills at bar 2's open — not bar 1's open or close."""
        stock_id = create_stock(db_session, "SEQ")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Sequential", "100000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(enter_on={date(2026, 1, 1)})
        result = run(engine_for(strategy), db_session, portfolio, stock_id)

        assert result.bars_processed == 6
        assert len(result.orders) == 1
        order = result.orders[0]
        assert order.status is OrderStatus.FILLED
        assert order.side is OrderSide.BUY
        # Bar 2's open is 100 + 2 = 102.
        assert order.execution_price == Decimal("102.00")
        assert order.signal_date == datetime(2026, 1, 1)
        assert order.executed_at == datetime(2026, 1, 2)
        assert order.rejection_code is None

    def test_cash_moves_by_exactly_the_fill_value(self, db_session, rising_bars):
        """A 20-share buy at 102 leaves 100,000 - 2,040 = 97,960 with zero fees."""
        from app.schemas.portfolio import PositionSizingRequest

        stock_id = create_stock(db_session, "CASHC")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Cash", "100000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(enter_on={date(2026, 1, 1)})
        sizing = PositionSizingRequest(
            method=PositionSizingMethod.FIXED_CAPITAL,
            price=Decimal("102"),
            capital=Decimal("2040"),
        )
        result = run(engine_for(strategy), db_session, portfolio, stock_id, sizing=sizing)

        order = result.orders[0]
        assert order.quantity == 20
        assert order.execution_price == Decimal("102.00")
        assert order.cash_after == Decimal("97960.00")

    def test_a_sell_closes_the_position_at_the_next_bars_open(self, db_session, rising_bars):
        stock_id = create_stock(db_session, "EXIT")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Exit", "100000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(
            enter_on={date(2026, 1, 1)}, exit_on={date(2026, 1, 3)}
        )
        result = run(engine_for(strategy), db_session, portfolio, stock_id)

        assert len(result.orders) == 2
        entry, exit_order = result.orders
        assert entry.side is OrderSide.BUY and entry.execution_price == Decimal("102.00")
        assert exit_order.side is OrderSide.SELL
        # Bar 4's open is 100 + 4 = 104.
        assert exit_order.execution_price == Decimal("104.00")
        assert exit_order.quantity == entry.quantity

    def test_realised_pnl_is_the_price_difference_times_quantity(self, db_session, rising_bars):
        stock_id = create_stock(db_session, "PNL")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "PnL", "100000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(
            enter_on={date(2026, 1, 1)}, exit_on={date(2026, 1, 3)}
        )
        result = run(engine_for(strategy), db_session, portfolio, stock_id)

        entry, exit_order = result.orders
        expected = (exit_order.execution_price - entry.execution_price) * exit_order.quantity
        assert entry.execution_price == Decimal("102.00")
        assert exit_order.execution_price == Decimal("104.00")
        assert exit_order.realized_pnl == expected == Decimal("196.00")

    def test_the_final_bar_never_opens_a_new_position(self, db_session, rising_bars):
        """A signal on the last bar has no next bar to fill on, so it is not an order."""
        stock_id = create_stock(db_session, "TAIL")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Tail", "100000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(enter_on={date(2026, 1, 6)})
        result = run(engine_for(strategy), db_session, portfolio, stock_id)

        assert result.orders == ()

    def test_an_open_position_remains_after_the_run(self, db_session, rising_bars):
        from app.models.portfolio import PortfolioPosition
        from app.repositories.portfolio_repository import portfolio_repository
        from app.schemas.portfolio import PositionSizingRequest

        stock_id = create_stock(db_session, "OPEN")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Open", "100000")

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(enter_on={date(2026, 1, 1)})
        sizing = PositionSizingRequest(
            method=PositionSizingMethod.FIXED_CAPITAL,
            price=Decimal("102"),
            capital=Decimal("10000"),
        )
        run(engine_for(strategy), db_session, portfolio, stock_id, sizing=sizing)

        positions = (
            db_session.query(PortfolioPosition).filter_by(portfolio_id=portfolio_id).all()
        )
        assert len(positions) == 1
        assert positions[0].quantity == 98
        assert Decimal(positions[0].average_entry_price) == Decimal("102.00")


# ----------------------------------------------------------------------
# Execution-price policies
# ----------------------------------------------------------------------


class TestExecutionPricePolicy:
    @pytest.mark.parametrize(
        "policy,expected",
        [
            # OPEN fills the bar *after* the signal bar: bar 2's open.
            (ExecutionPricePolicy.OPEN, "102.00"),
            # CLOSE fills the signal bar's own close, which is already known when the decision
            # is taken: bar 1's close.
            (ExecutionPricePolicy.CLOSE, "101.00"),
        ],
    )
    def test_policies_are_honoured(self, db_session, policy, expected):
        """Bar 1 closes at 101 and bar 2 opens at 102, so the policies are distinguishable."""
        stock_id = create_stock(db_session, f"POL{policy.value}")
        bars = [bar(1, 100, 105, 99, 101), bar(2, 102, 110, 101, 103), bar(3, 104, 110, 103, 106)]
        insert_prices(db_session, stock_id, bars)
        portfolio_id = create_portfolio(db_session, f"Policy {policy.value}", "100000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(enter_on={date(2026, 1, 1)})
        result = run(engine_for(strategy), db_session, portfolio, stock_id, policy=policy)
        assert result.execution_price_policy is policy
        assert result.orders[0].execution_price == Decimal(expected)

    def test_open_and_close_policies_fill_at_different_prices(self, db_session):
        stock_id = create_stock(db_session, "BOTH")
        bars = [bar(1, 100, 105, 99, 101), bar(2, 102, 110, 101, 107), bar(3, 104, 110, 103, 106)]
        insert_prices(db_session, stock_id, bars)

        from app.repositories.portfolio_repository import portfolio_repository

        prices = {}
        for policy in (ExecutionPricePolicy.OPEN, ExecutionPricePolicy.CLOSE):
            portfolio_id = create_portfolio(db_session, f"Both {policy.value}", "100000")
            portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
            strategy = ScriptedStrategy(enter_on={date(2026, 1, 1)})
            result = run(engine_for(strategy), db_session, portfolio, stock_id, policy=policy)
            prices[policy] = result.orders[0].execution_price

        assert prices[ExecutionPricePolicy.OPEN] == Decimal("102.00")
        assert prices[ExecutionPricePolicy.CLOSE] == Decimal("101.00")
        assert prices[ExecutionPricePolicy.OPEN] != prices[ExecutionPricePolicy.CLOSE]


# ----------------------------------------------------------------------
# Sizing inside a run
# ----------------------------------------------------------------------


class TestSizingInsideARun:
    def test_a_fixed_capital_request_sizes_the_simulated_buy(self, db_session, rising_bars):
        """10,000 of a 100,000 portfolio at a 102 fill -> 98 shares."""
        from app.schemas.portfolio import PositionSizingRequest

        stock_id = create_stock(db_session, "SIZE")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Sized", "100000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(enter_on={date(2026, 1, 1)})
        sizing = PositionSizingRequest(
            method=PositionSizingMethod.FIXED_CAPITAL,
            price=Decimal("102"),
            capital=Decimal("10000"),
        )
        result = run(engine_for(strategy), db_session, portfolio, stock_id, sizing=sizing)

        assert result.orders[0].quantity == 98
        assert result.orders[0].execution_price == Decimal("102.00")

    def test_the_default_sizing_uses_a_percentage_of_cash(self, db_session, rising_bars):
        """With no explicit request the engine falls back to the configured default percentage."""
        stock_id = create_stock(db_session, "DEF")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Default", "100000")

        from app.repositories.portfolio_repository import portfolio_repository
        from app.services.portfolio.defaults import default_position_size_pct

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(enter_on={date(2026, 1, 1)})
        result = run(engine_for(strategy), db_session, portfolio, stock_id)

        expected = int(
            (Decimal("100000") * default_position_size_pct() / Decimal("100")) / Decimal("102")
        )
        assert result.orders[0].quantity == expected
        assert result.orders[0].quantity > 0


# ----------------------------------------------------------------------
# Risk refusals inside a run
# ----------------------------------------------------------------------


class TestRiskRefusalsInsideARun:
    def test_a_refused_trade_is_recorded_and_the_run_continues(self, db_session, rising_bars):
        """A 1% max exposure cap refuses the first buy; the engine keeps stepping."""
        stock_id = create_stock(db_session, "CAPPED")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(
            db_session,
            "Capped",
            "100000",
            risk_limits={"max_exposure_pct": "1"},
        )

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = AlwaysEnterStrategy()
        result = run(engine_for(strategy), db_session, portfolio, stock_id)

        assert len(result.orders) >= 1
        assert all(order.status is OrderStatus.REJECTED for order in result.orders)
        assert result.orders[0].rejection_code == "MAX_EXPOSURE_EXCEEDED"
        assert result.orders[0].order_id is not None, "a refusal must still be auditable"
        assert result.orders[0].execution_price == Decimal("0")

    def test_a_refused_run_leaves_the_portfolio_untouched(self, db_session, rising_bars):
        stock_id = create_stock(db_session, "NOCHANGE")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(
            db_session, "Untouched", "100000", risk_limits={"max_exposure_pct": "1"}
        )

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = AlwaysEnterStrategy()
        run(engine_for(strategy), db_session, portfolio, stock_id)

        db_session.refresh(portfolio)
        assert Decimal(portfolio.current_cash) == Decimal("100000.00")
        # Every bar except the last is offered a decision; the last has no next bar to fill on.
        assert len(strategy.seen_bars) == 5, "every bar is still processed after a refusal"


# ----------------------------------------------------------------------
# Equity curve persistence
# ----------------------------------------------------------------------


class TestEquityCurvePersistence:
    def test_one_snapshot_per_recorded_bar(self, db_session, rising_bars):
        stock_id = create_stock(db_session, "CURVE")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Curve", "100000")

        from app.repositories.portfolio_repository import portfolio_repository
        from app.services.portfolio.service import paper_trading_service

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(enter_on={date(2026, 1, 1)})
        result = run(engine_for(strategy), db_session, portfolio, stock_id)

        assert result.snapshots == 6
        # Committed even though only one trade occurred.
        assert len(paper_trading_service.get_snapshots(db_session, portfolio_id)) == 6

    def test_snapshots_are_persisted_with_no_trades_at_all(self, db_session, rising_bars):
        stock_id = create_stock(db_session, "NOTRADE")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "NoTrade", "100000")

        from app.repositories.portfolio_repository import portfolio_repository
        from app.services.portfolio.service import paper_trading_service

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        result = run(engine_for(ScriptedStrategy()), db_session, portfolio, stock_id)

        assert result.orders == ()
        assert result.snapshots == 6
        assert len(paper_trading_service.get_snapshots(db_session, portfolio_id)) == 6

    def test_the_curve_records_exposure_and_drawdown(self, db_session, rising_bars):
        stock_id = create_stock(db_session, "FIELDS")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Fields", "100000")

        from app.repositories.portfolio_repository import portfolio_repository
        from app.services.portfolio.service import paper_trading_service

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(enter_on={date(2026, 1, 1)})
        run(engine_for(strategy), db_session, portfolio, stock_id)

        snapshots = paper_trading_service.get_snapshots(db_session, portfolio_id)
        # After the buy at bar 2 the position exists, so exposure is non-zero.
        assert snapshots[-1].exposure_pct is not None
        assert Decimal(snapshots[-1].exposure_pct) > 0
        assert snapshots[-1].peak_equity is not None
        assert Decimal(snapshots[-1].drawdown_pct) == Decimal("0")


# ----------------------------------------------------------------------
# Window handling
# ----------------------------------------------------------------------


class TestWindowHandling:
    def test_a_date_window_bounds_the_recorded_bars(self, db_session, rising_bars):
        stock_id = create_stock(db_session, "WINDOW")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Window", "100000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(enter_on={date(2026, 1, 2), date(2026, 1, 3)})
        result = run(
            engine_for(strategy),
            db_session,
            portfolio,
            stock_id,
            start_date=date(2026, 1, 3),
        )

        # Bars 3..6 only; the signal on bar 3 fills on bar 4.
        assert result.bars_processed == 4
        assert [order.signal_date for order in result.orders] == [datetime(2026, 1, 3)]

    def test_an_end_date_bounds_the_run(self, db_session, rising_bars):
        stock_id = create_stock(db_session, "ENDDATE")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "EndDate", "100000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = ScriptedStrategy(enter_on={date(2026, 1, 1)})
        result = run(
            engine_for(strategy), db_session, portfolio, stock_id, end_date=date(2026, 1, 4)
        )
        assert result.bars_processed == 4

    def test_a_window_with_no_bars_reports_zero_rather_than_failing(self, db_session, rising_bars):
        stock_id = create_stock(db_session, "EMPTY")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Empty", "100000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        result = run(
            engine_for(ScriptedStrategy()),
            db_session,
            portfolio,
            stock_id,
            start_date=date(2027, 1, 1),
        )
        assert result.bars_processed == 0
        assert result.orders == ()


# ----------------------------------------------------------------------
# Look-ahead prevention — the mandatory regression test
# ----------------------------------------------------------------------


class TestLookAheadPrevention:
    def test_no_fill_ever_uses_a_price_from_a_later_bar(self, db_session):
        """Every recorded fill must equal the open or close of its own execution bar.

        Prices are distinct per bar, so a fill drawn from any other bar is detectable.
        """
        # 20 bars with strictly increasing, unique prices.
        bars = [bar(d + 1, 100 + 10 * d, 105 + 10 * d, 95 + 10 * d, 103 + 10 * d) for d in range(20)]
        stock_id = create_stock(db_session, "LOOK")
        insert_prices(db_session, stock_id, bars)
        portfolio_id = create_portfolio(db_session, "LookAhead", "1000000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = AlwaysEnterStrategy()
        result = run(
            engine_for(strategy), db_session, portfolio, stock_id, policy=ExecutionPricePolicy.OPEN
        )

        prices_of = {
            bar_date: (open_price, close_price)
            for bar_date, open_price, _h, _l, close_price in bars
        }
        assert result.orders, "the scripted strategy must trade for this test to mean anything"
        for order in result.orders:
            open_price, close_price = prices_of[order.executed_at.date()]
            allowed = {Decimal(str(open_price)), Decimal(str(close_price))}
            assert order.execution_price in allowed, (
                f"fill {order.execution_price} on {order.executed_at} is not a price of that bar"
            )

    def test_the_signal_bar_never_post_dates_the_execution_bar(self, db_session):
        bars = [bar(d + 1, 100 + 10 * d, 105 + 10 * d, 95 + 10 * d, 103 + 10 * d) for d in range(20)]
        stock_id = create_stock(db_session, "SIGNAL")
        insert_prices(db_session, stock_id, bars)
        portfolio_id = create_portfolio(db_session, "SignalOrder", "1000000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        strategy = AlwaysEnterStrategy()
        result = run(engine_for(strategy), db_session, portfolio, stock_id)

        for order in result.orders:
            assert order.signal_date is not None
            assert order.executed_at is not None
            assert order.signal_date <= order.executed_at

    def test_a_fill_is_always_strictly_after_its_signal_bar(self, db_session):
        """Same-bar fills would mean the decision already knew the fill price."""
        bars = [bar(d + 1, 100 + 10 * d, 105 + 10 * d, 95 + 10 * d, 103 + 10 * d) for d in range(20)]
        stock_id = create_stock(db_session, "STRICT")
        insert_prices(db_session, stock_id, bars)
        portfolio_id = create_portfolio(db_session, "Strict", "1000000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        result = run(
            engine_for(AlwaysEnterStrategy()), db_session, portfolio, stock_id
        )
        for order in result.orders:
            assert order.signal_date < order.executed_at

    def test_the_real_point_in_time_builder_is_used_by_default(self):
        """The default engine must not fall back to a whole-series indicator call."""
        from app.services.backtest.indicators import PointInTimeContextBuilder

        assert isinstance(PaperTradingEngine().context_builder, PointInTimeContextBuilder)

    def test_point_in_time_contexts_contain_no_future_data(self):
        """The Phase 8 builder is the look-ahead guard, so assert its invariant directly."""
        from app.services.backtest.indicators import PointInTimeContextBuilder

        closes = [Decimal(100 + d) for d in range(60)]
        bars = [
            Bar(
                date=date(2026, 1, 1) + __import__("datetime").timedelta(days=index),
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1000,
            )
            for index, close in enumerate(closes)
        ]
        contexts = PointInTimeContextBuilder().build(1, "TEST", bars)

        assert len(contexts) == len(bars)
        # Truncating the series must not change any earlier context.
        for index in range(len(bars)):
            partial = PointInTimeContextBuilder().build(1, "TEST", bars[: index + 1])
            earlier = partial[index]
            assert earlier.as_of == contexts[index].as_of
            if getattr(earlier, "sma_50", None) is not None:
                assert earlier.sma_50 == contexts[index].sma_50
            if getattr(earlier, "macd", None) is not None:
                assert earlier.macd == contexts[index].macd

    def test_the_real_phase7_strategy_runs_end_to_end(self, db_session):
        """The production strategy and the production registry are wired together."""
        stock_id = create_stock(db_session, "PHASE7")
        # 200 bars of a steady uptrend so the real indicators are warm and produce signals.
        bars = [
            bar(
                (d % 28) + 1,
                100 + d,
                102 + d,
                98 + d,
                101 + d,
            )
            for d in range(200)
        ]
        # De-duplicate trading days by extending into later months.
        bars = []
        start = date(2025, 1, 1)
        for index in range(200):
            current = start + __import__("datetime").timedelta(days=index)
            bars.append((current, 100 + index, 103 + index, 97 + index, 101 + index))
        insert_prices(db_session, stock_id, bars)
        portfolio_id = create_portfolio(db_session, "Phase7", "1000000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        engine = PaperTradingEngine()
        result = engine.run(db_session, portfolio, stock_id, strategy_name=STRATEGY_NAME)

        assert result.strategy == STRATEGY_NAME
        assert result.bars_processed == 200
        # Look-ahead invariants hold for the real strategy too.
        for order in result.orders:
            assert order.signal_date < order.executed_at

    def test_an_unknown_strategy_is_refused(self, db_session, rising_bars):
        from app.core.exceptions import UnknownStrategyError
        from app.repositories.portfolio_repository import portfolio_repository

        stock_id = create_stock(db_session, "UNKNOWN")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Unknown", "100000")
        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)

        with pytest.raises(UnknownStrategyError):
            PaperTradingEngine().run(
                db_session, portfolio, stock_id, strategy_name="not_a_strategy"
            )

    def test_an_unknown_stock_is_refused(self, db_session, rising_bars):
        from app.core.exceptions import StockNotFoundError
        from app.repositories.portfolio_repository import portfolio_repository

        portfolio_id = create_portfolio(db_session, "NoStock", "100000")
        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)

        with pytest.raises(StockNotFoundError):
            PaperTradingEngine().run(db_session, portfolio, 999999)


# ----------------------------------------------------------------------
# Warm-up handling
# ----------------------------------------------------------------------


class TestIndicatorWarmup:
    def test_bars_before_the_window_are_used_only_for_indicators(self, db_session):
        """A mid-history window still gets a warm indicator context, not a cold start."""
        stock_id = create_stock(db_session, "WARM")
        bars = []
        start = date(2025, 1, 1)
        for index in range(400):
            current = start + __import__("datetime").timedelta(days=index)
            bars.append((current, 100 + index, 103 + index, 97 + index, 101 + index))
        insert_prices(db_session, stock_id, bars)
        portfolio_id = create_portfolio(db_session, "Warm", "1000000")

        from app.repositories.portfolio_repository import portfolio_repository

        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)
        seen = []

        class RecordingBuilder:
            def build(self, stock_id, symbol, bars_in):
                seen.append(len(bars_in))
                return NoWarmupBuilder().build(stock_id, symbol, bars_in)

        window_start = date(2026, 1, 1)
        result = run(
            engine_for(ScriptedStrategy(), builder=RecordingBuilder()),
            db_session,
            portfolio,
            stock_id,
            start_date=window_start,
        )

        # 400 daily bars from 2025-01-01 reach into February 2026; the window from 2026-01-01
        # therefore holds the remaining 35 of them.
        assert result.bars_processed == 35
        # Warm-up bars were loaded: the fetch reaches back before the window start.
        assert seen[0] == 400
        assert INDICATOR_WARMUP_CALENDAR_DAYS == 400


# ----------------------------------------------------------------------
# Audit trail
# ----------------------------------------------------------------------


class TestAuditTrail:
    def test_every_simulated_order_records_its_strategy_and_signal(self, db_session, rising_bars):
        from app.models.portfolio import PortfolioOrder
        from app.repositories.portfolio_repository import portfolio_repository

        stock_id = create_stock(db_session, "AUDIT")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Audit", "100000")
        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)

        strategy = ScriptedStrategy(
            enter_on={date(2026, 1, 1)}, exit_on={date(2026, 1, 3)}
        )
        run(engine_for(strategy), db_session, portfolio, stock_id)

        orders = (
            db_session.query(PortfolioOrder)
            .filter_by(portfolio_id=portfolio_id)
            .order_by(PortfolioOrder.id)
            .all()
        )
        assert len(orders) == 2
        for order in orders:
            assert order.source == "STRATEGY"
            assert order.signal_as_of is not None
            assert order.executed_at is not None
            assert order.execution_price_policy == "OPEN"
            assert order.strategy_event_id is not None
            metadata = json.loads(order.metadata_json)
            assert metadata["strategy"] == strategy.strategy_name
            assert metadata["signal_date"] is not None
            assert metadata["action"] in {"enter", "exit"}

    def test_replaying_the_same_window_does_not_execute_twice(self, db_session, rising_bars):
        """Re-running a window over an already-filled portfolio adds no second order.

        The strategy-event key makes each signal a one-time event; service-level idempotency
        for a repeated key is covered by the Phase 9 API tests.
        """
        from app.repositories.portfolio_repository import portfolio_repository
        from app.models.portfolio import PortfolioOrder

        stock_id = create_stock(db_session, "IDEMP")
        insert_prices(db_session, stock_id, rising_bars)
        portfolio_id = create_portfolio(db_session, "Idempotent", "100000")
        portfolio = portfolio_repository.get_by_id(db_session, portfolio_id)

        engine = engine_for(ScriptedStrategy(enter_on={date(2026, 1, 1)}))
        first = run(engine, db_session, portfolio, stock_id)
        assert len(first.orders) == 1
        db_session.refresh(portfolio)
        cash_after_first = Decimal(portfolio.current_cash)

        second = run(engine, db_session, portfolio, stock_id)
        assert len(second.orders) == 0
        db_session.refresh(portfolio)
        assert Decimal(portfolio.current_cash) == cash_after_first
        assert (
            db_session.query(PortfolioOrder).filter_by(portfolio_id=portfolio_id).count() == 1
        )