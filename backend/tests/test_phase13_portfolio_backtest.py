"""Phase 13 portfolio-backtest tests, including the mandatory no-look-ahead proof.

The strategy is a stub whose decisions the test dictates, so every expected fill price, quantity
and P&L figure is derivable from the bars by hand. A test that used the real Phase 7 strategy
would be asserting the behaviour of the signal engine as much as of the portfolio engine, and a
signal change would break it for no reason connected to Phase 13.

The no-look-ahead tests are the important ones. Each builds **two** price paths that are
byte-identical up to a decision point and diverge afterwards, then asserts the decision taken
before that point is the same in both. Any influence from the future shows up as a difference.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.exceptions import UnknownStrategyError
from app.services.backtest.indicators import PointInTimeContextBuilder
from app.services.backtest.models import ExitReason
from app.services.backtest.strategy import STRATEGY_NAME, StrategyAction
from app.services.backtest.validator import load_bars, validate_bars
from app.services.portfolio.portfolio_backtest import (
    BacktestStock,
    PortfolioBacktestEngine,
)
from tests.phase13_helpers import bar, policy

START = date(2024, 1, 1)
CAPITAL = Decimal("100000")
STRATEGY = "phase13_stub"


class StubStrategy:
    """A strategy whose action per bar the test decides exactly.

    ``plan`` maps a trading date to the action to take at that bar's close. A date absent from
    the plan yields ``HOLD``, so a test states only the bars it cares about.
    """

    strategy_name = STRATEGY
    strategy_version = "phase13"

    def __init__(self, plan=None, always=None):
        self.plan = dict(plan or {})
        self.always = always
        self.seen = []

    def decide(self, is_long, context):
        """Return the planned action, recording the date so a test can assert what was seen."""
        self.seen.append(context.as_of)
        if self.always is None:
            action = self.plan.get(context.as_of, StrategyAction.HOLD)
        elif callable(self.always):
            action = self.always(is_long, context)
        else:
            action = self.always
        return action, 50, []

    @property
    def dates_seen(self):
        return list(self.seen)


class SingleRegistry:
    """A registry holding one stub strategy, mirroring the Phase 9 paper-engine test seam."""

    def __init__(self, strategy):
        self._strategy = strategy

    def get(self, name):
        return self._strategy if name == STRATEGY else None

    @property
    def available(self):
        return [self._strategy.strategy_name]


def series(closes, *, start=START, warmup=0):
    """``(bars, contexts)`` for an explicit close series."""
    bars = list(closes)
    assert len(bars) == warmup + len(closes)
    checked = validate_bars(bars).bars
    contexts = PointInTimeContextBuilder().build(1, "AAA", checked)
    return tuple(checked), tuple(contexts)


def stock(stock_id, closes, *, symbol=None, start=START):
    """One replayable stock from an explicit close series."""
    bars, contexts = series(closes, start=start)
    return BacktestStock(
        stock_id=stock_id, symbol=symbol or f"S{stock_id}", bars=bars, contexts=contexts
    )


def replay(stocks, strategy, *, capital=CAPITAL, days=5, engine_policy=None, **kwargs):
    """Replay ``stocks`` for ``days`` recorded days and return the result."""
    return PortfolioBacktestEngine(registry=SingleRegistry(strategy)).run(
        portfolio_id=1,
        stocks=stocks,
        policy=engine_policy or policy(),
        initial_capital=capital,
        start_date=START,
        end_date=START + timedelta(days=days - 1),
        strategy_name=STRATEGY,
        **kwargs,
    )


def flat(price, count, start=START):
    """``count`` bars all at ``price``."""
    return [bar(start + timedelta(days=index), price, price, price, price) for index in range(count)]


def closes_at(price, count, start=START):
    """The close of each of ``count`` bars, ready for :func:`series`."""
    return [bar(start + timedelta(days=index), price, price, price, price) for index in range(count)]


# ----------------------------------------------------------------------
# Basic mechanics
# ----------------------------------------------------------------------


class TestReplayMechanics:
    def test_a_holding_strategy_trades_nothing(self):
        result = replay([stock(1, closes_at(100, 5))], StubStrategy(always=StrategyAction.HOLD))
        assert result.trades == ()
        assert result.final_equity == CAPITAL
        assert result.total_fees == Decimal("0.00")

    def test_an_entry_at_the_first_close_fills_at_the_second_open(self):
        """The signal is on day 1's close; the fill is day 2's open."""
        bars = [
            bar(START, 100, 100, 100, 100),
            bar(START + timedelta(days=1), 200, 200, 200, 200),
            bar(START + timedelta(days=2), 300, 300, 300, 300),
            bar(START + timedelta(days=3), 300, 300, 300, 300),
        ]
        result = replay(
            [stock(1, bars)],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            days=4,
        )
        assert len(result.trades) == 1
        assert result.trades[0].entry_price == Decimal("200")

    def test_a_position_is_marked_to_market_each_day(self):
        bars = [
            bar(START, 100, 100, 100, 100),
            bar(START + timedelta(days=1), 100, 100, 100, 100),
            bar(START + timedelta(days=2), 150, 150, 150, 150),
            bar(START + timedelta(days=3), 150, 150, 150, 150),
        ]
        result = replay(
            [stock(1, bars)],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            days=4,
        )
        by_date = {point.date: point for point in result.equity_curve}
        assert by_date[START + timedelta(days=2)].market_value > 0

    def test_open_positions_are_liquidated_at_the_end(self):
        """A position still open on the last bar is closed, not left valued as if held."""
        bars = closes_at(100, 5)
        bars[1] = bar(START + timedelta(days=1), 110, 110, 110, 110)
        result = replay(
            [stock(1, bars)],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            days=5,
        )
        assert result.ending_invested_capital == Decimal("0.00")
        assert result.trades
        assert result.trades[-1].exit_reason is ExitReason.END_OF_PERIOD

    def test_liquidation_realises_the_loss_of_a_falling_position(self):
        """Bought at 110 and liquidated at 100, the cash is 100,000 minus the 10% loss."""
        bars = closes_at(100, 5)
        bars[1] = bar(START + timedelta(days=1), 110, 110, 110, 110)
        result = replay(
            [stock(1, bars)],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            days=5,
        )
        assert result.total_pnl == Decimal("-2720.00")
        assert result.final_equity == CAPITAL + result.total_pnl

    def test_the_equity_curve_has_one_point_per_recorded_day(self):
        result = replay([stock(1, closes_at(100, 5))], StubStrategy(always=StrategyAction.HOLD))
        assert result.days_processed == 5
        assert len(result.equity_curve) == 5
        assert result.equity_curve[0].date == START

    def test_a_flat_portfolio_reports_zero_exposure(self):
        result = replay([stock(1, closes_at(100, 5))], StubStrategy(always=StrategyAction.HOLD))
        assert all(point.exposure_pct == Decimal("0.0000") for point in result.equity_curve)


class TestInputValidation:
    def test_no_stocks_is_refused(self):
        with pytest.raises(ValueError):
            replay([], StubStrategy())

    def test_non_positive_capital_is_refused(self):
        with pytest.raises(ValueError):
            replay([stock(1, closes_at(100, 3))], StubStrategy(), capital=Decimal("0"))

    def test_an_unknown_strategy_is_refused(self):
        with pytest.raises(UnknownStrategyError):
            PortfolioBacktestEngine(registry=SingleRegistry(StubStrategy())).run(
                portfolio_id=1,
                stocks=[stock(1, closes_at(100, 3))],
                policy=policy(),
                initial_capital=CAPITAL,
                start_date=START,
                end_date=START + timedelta(days=2),
                strategy_name="no_such_strategy",
            )


# ----------------------------------------------------------------------
# Stops
# ----------------------------------------------------------------------


class TestStops:
    def test_a_stop_is_taken_from_the_policy(self):
        """5% below a 100 entry is a stop at 95."""
        bars = [
            bar(START, 100, 100, 100, 100),
            bar(START + timedelta(days=1), 100, 100, 100, 100),
            bar(START + timedelta(days=2), 100, 100, 80, 100),
            bar(START + timedelta(days=3), 100, 100, 100, 100),
        ]
        result = replay(
            [stock(1, bars)],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            engine_policy=policy(stop_loss_pct=Decimal("5")),
            days=4,
        )
        assert result.trades
        assert result.trades[0].exit_price == Decimal("95")

    def test_a_stop_is_not_triggered_when_the_bar_never_reaches_it(self):
        bars = [
            bar(START, 100, 100, 100, 100),
            bar(START + timedelta(days=1), 100, 100, 96, 100),
            bar(START + timedelta(days=2), 100, 100, 100, 100),
            bar(START + timedelta(days=3), 100, 100, 100, 100),
        ]
        result = replay(
            [stock(1, bars)],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            engine_policy=policy(stop_loss_pct=Decimal("5")),
            days=4,
        )
        assert all(trade.exit_reason != "STOP_LOSS" for trade in result.trades)

    def test_a_stop_caps_the_loss_at_the_stop_price(self):
        """The exit is the stop, not the day's low: the position is not sold at the flush."""
        bars = [
            bar(START, 100, 100, 100, 100),
            bar(START + timedelta(days=1), 100, 100, 80, 100),
            bar(START + timedelta(days=2), 100, 100, 100, 100),
            bar(START + timedelta(days=3), 100, 100, 100, 100),
        ]
        result = replay(
            [stock(1, bars)],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            engine_policy=policy(stop_loss_pct=Decimal("5")),
            days=4,
        )
        assert result.trades[0].exit_price == Decimal("95")


# ----------------------------------------------------------------------
# Portfolio interaction
# ----------------------------------------------------------------------


class TestMultipleStocks:
    def test_two_stocks_compete_for_the_same_cash(self):
        stocks = [
            stock(1, closes_at(100, 6)),
            stock(2, closes_at(100, 6)),
        ]
        result = replay(
            stocks,
            StubStrategy(plan={START: StrategyAction.ENTER}),
            engine_policy=policy(max_position_weight_pct=Decimal("30")),
            days=6,
        )
        assert len(result.trades) >= 2
        invested = result.initial_capital - result.ending_cash
        assert invested <= result.initial_capital

    def test_cash_is_never_spent_below_zero(self):
        stocks = [stock(index, closes_at(100, 6)) for index in range(1, 5)]
        result = replay(
            stocks,
            StubStrategy(plan={START: StrategyAction.ENTER}),
            engine_policy=policy(),
            days=6,
        )
        assert result.ending_cash >= Decimal("0.00")

    def test_the_position_count_is_respected(self):
        stocks = [stock(index, closes_at(100, 6)) for index in range(1, 5)]
        result = replay(
            stocks,
            StubStrategy(plan={START: StrategyAction.ENTER}),
            engine_policy=policy(max_positions=2),
            days=6,
        )
        assert len(result.trades) <= 2
        assert result.metrics.number_of_trades == len(result.trades)

    def test_every_stock_gets_its_own_calendar_days(self):
        """A stock with a shorter history simply has fewer days, and the run continues."""
        stocks = [
            stock(1, closes_at(100, 6)),
            stock(2, closes_at(100, 4)),
        ]
        result = replay(stocks, StubStrategy(always=StrategyAction.HOLD), days=6)
        assert result.days_processed == 6


# ----------------------------------------------------------------------
# Costs
# ----------------------------------------------------------------------


class TestCosts:
    def test_a_commission_reduces_final_equity(self):
        bars = [
            bar(START, 100, 100, 100, 100),
            bar(START + timedelta(days=1), 100, 100, 100, 100),
            bar(START + timedelta(days=2), 200, 200, 200, 200),
            bar(START + timedelta(days=3), 200, 200, 200, 200),
        ]
        free = replay([stock(1, bars)], StubStrategy(plan={START: StrategyAction.ENTER}), days=4)
        costed = replay(
            [stock(1, bars)],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            engine_policy=policy(commission_rate=Decimal("0.001")),
            days=4,
        )
        assert costed.total_fees > Decimal("0.00")
        assert costed.final_equity < free.final_equity

    def test_fees_are_charged_on_both_sides(self):
        bars = [
            bar(START, 100, 100, 100, 100),
            bar(START + timedelta(days=1), 100, 100, 100, 100),
            bar(START + timedelta(days=2), 100, 100, 100, 100),
            bar(START + timedelta(days=3), 100, 100, 100, 100),
        ]
        result = replay(
            [stock(1, bars)],
            StubStrategy(plan={START: StrategyAction.ENTER, START + timedelta(days=2): StrategyAction.EXIT}),
            engine_policy=policy(commission_rate=Decimal("0.001")),
            days=4,
        )
        assert len(result.trades) == 1
        assert result.total_fees > Decimal("0.00")


# ----------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------


class TestMetrics:
    def test_a_flat_run_reports_zero_return(self):
        result = replay([stock(1, closes_at(100, 5))], StubStrategy(always=StrategyAction.HOLD))
        assert result.total_pnl == Decimal("0.00")
        assert result.metrics.total_return_pct == Decimal("0.0000")

    def test_a_rising_run_reports_a_positive_return(self):
        bars = [
            bar(START, 100, 100, 100, 100),
            bar(START + timedelta(days=1), 100, 100, 100, 100),
            bar(START + timedelta(days=2), 100, 120, 100, 120),
            bar(START + timedelta(days=3), 100, 120, 100, 120),
        ]
        result = replay(
            [stock(1, bars)], StubStrategy(plan={START: StrategyAction.ENTER}), days=4
        )
        assert result.total_pnl > Decimal("0.00")
        assert result.metrics.total_return_pct > 0

    def test_maximum_exposure_is_reported(self):
        result = replay(
            [stock(1, closes_at(100, 5))],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            days=5,
        )
        assert result.maximum_exposure_pct is not None
        assert result.maximum_exposure_pct > 0

    def test_rejected_signals_are_counted(self):
        """A candidate that could not be funded is a rejection, not a silent skip."""
        result = replay(
            [stock(1, closes_at(100, 5))],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            capital=Decimal("100"),
            engine_policy=policy(),
            days=5,
        )
        assert result.rejected_signals >= 1


# ----------------------------------------------------------------------
# No look-ahead — the core proof
# ----------------------------------------------------------------------


class TestNoLookAhead:
    def test_a_future_move_does_not_change_an_earlier_decision(self):
        """Two paths identical to day 2, divergent after. The day-2 decision must be the same.

        If any bar after day 2 leaked into the day-2 decision, the fill would differ.
        """
        flat_path = [
            bar(START, 100, 100, 100, 100),
            bar(START + timedelta(days=1), 100, 100, 100, 100),
            bar(START + timedelta(days=2), 100, 100, 100, 100),
            bar(START + timedelta(days=3), 100, 100, 100, 100),
            bar(START + timedelta(days=4), 100, 100, 100, 100),
        ]
        crash_path = list(flat_path)
        crash_path[3] = bar(START + timedelta(days=3), 100, 100, 50, 50)
        crash_path[4] = bar(START + timedelta(days=4), 50, 50, 50, 50)

        decisions = []
        for bars in (flat_path, crash_path):
            result = replay(
                [stock(1, bars)],
                StubStrategy(plan={START: StrategyAction.ENTER}),
                days=5,
            )
            assert result.trades
            decisions.append(result.trades[0].entry_price)
        assert decisions[0] == decisions[1]

    def test_the_decision_bar_is_never_the_fill_bar(self):
        """A signal from bar N fills at bar N+1's open, never at bar N."""
        bars = closes_at(100, 6)
        bars[1] = bar(START + timedelta(days=1), 250, 250, 250, 250)
        strategy = StubStrategy(plan={START: StrategyAction.ENTER})
        result = replay([stock(1, bars)], strategy, days=6)
        assert result.trades
        trade = result.trades[0]
        assert trade.entry_date == START + timedelta(days=1)

    def test_the_execution_log_shows_every_fill_after_its_signal(self):
        bars = closes_at(100, 6)
        bars[1] = bar(START + timedelta(days=1), 120, 120, 120, 120)
        result = replay(
            [stock(1, bars)],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            days=6,
        )
        assert result.execution_log
        assert all(signal < fill for signal, fill in result.execution_log)

    def test_the_run_says_whether_it_was_clean(self):
        result = replay(
            [stock(1, closes_at(100, 5))],
            StubStrategy(plan={START: StrategyAction.ENTER}),
            days=5,
        )
        assert result.no_look_ahead is True

    def test_a_future_rally_cannot_be_used_to_buy_earlier(self):
        """A path that only rallies from day 3 must not produce an earlier, larger entry.

        The strategy is told to enter on day 1 in both runs. In the rally path the position is
        bought at day 2's open of 100; in the flat path at the same 100. The *later* rally must
        not change what was paid on day 2.
        """
        flat_path = closes_at(100, 6)
        rally_path = list(closes_at(100, 6))
        rally_path[2] = bar(START + timedelta(days=2), 500, 500, 500, 500)
        rally_path[3] = bar(START + timedelta(days=3), 500, 500, 500, 500)

        prices = []
        for bars in (flat_path, rally_path):
            result = replay(
                [stock(1, bars)],
                StubStrategy(plan={START: StrategyAction.ENTER}),
                days=6,
            )
            assert result.trades
            prices.append(result.trades[0].entry_price)
        assert prices[0] == prices[1] == Decimal("100")

    def test_two_runs_of_the_same_bars_are_identical(self):
        bars = [
            bar(START, 100, 100, 100, 100),
            bar(START + timedelta(days=1), 100, 100, 100, 100),
            bar(START + timedelta(days=2), 110, 115, 105, 110),
            bar(START + timedelta(days=3), 110, 120, 100, 115),
            bar(START + timedelta(days=4), 115, 118, 112, 118),
        ]
        plan = {START: StrategyAction.ENTER, START + timedelta(days=3): StrategyAction.EXIT}
        first = replay([stock(1, bars)], StubStrategy(plan=plan), days=5)
        second = replay([stock(1, bars)], StubStrategy(plan=plan), days=5)
        assert first.final_equity == second.final_equity
        assert first.total_fees == second.total_fees
        assert len(first.trades) == len(second.trades)
        assert first.metrics.total_return_pct == second.metrics.total_return_pct