"""Phase 10 valuation and performance-metric tests.

Two groups:

* **pure calculations** — exposure, invested capital, signed drawdown, win rate, profit
  factor — stated from their formulas;
* **portfolio-level reporting** — a multi-position portfolio, the deterministic drawdown
  sequence from the specification, and the "insufficient observations" rule that makes a metric
  ``None`` instead of a misleading ``0``.
"""

from decimal import Decimal

from datetime import datetime, timedelta

import pytest

from app.services.portfolio.calculator import (
    allocation_pct,
    drawdown_pct,
    equity,
    exposure_pct,
    invested_capital,
    return_pct,
    signed_drawdown_pct,
    total_pnl,
)
from app.services.portfolio.models import PositionValuation
from app.services.portfolio.performance_service import (
    average,
    drawdown_extremes,
    profit_factor,
    win_rate_pct,
)
from app.services.portfolio.models import DrawdownPoint


def valued(stock_id: int, market_value: Decimal, cost_basis: Decimal) -> PositionValuation:
    """A valued position with only the fields these tests need."""
    return PositionValuation(
        stock_id=stock_id,
        quantity=1,
        average_entry_price=Decimal("1"),
        current_price=Decimal("1"),
        market_value=market_value,
        unrealized_pnl=market_value - cost_basis,
        realized_pnl=Decimal("0"),
        entry_fees_total=Decimal("0"),
        cost_basis=cost_basis,
    )


# ----------------------------------------------------------------------
# Pure valuation calculations
# ----------------------------------------------------------------------


class TestValuationCalculations:
    def test_equity_is_cash_plus_market_value(self):
        assert equity(Decimal("90000"), Decimal("20000")) == Decimal("110000.00")

    def test_total_pnl_is_equity_minus_initial_capital(self):
        assert total_pnl(Decimal("110000"), Decimal("100000")) == Decimal("10000.00")

    def test_return_percentage(self):
        assert return_pct(Decimal("1000"), Decimal("100000")) == Decimal("1.0000")

    def test_return_percentage_is_zero_when_capital_is_zero(self):
        """A zero-capital portfolio has no defined return; reporting 0 is the safe answer."""
        assert return_pct(Decimal("1000"), Decimal("0")) == Decimal("0.0000")

    def test_invested_capital_is_the_sum_of_cost_bases(self):
        positions = [valued(1, Decimal("12000"), Decimal("10000")), valued(2, Decimal("11000"), Decimal("10000"))]
        assert invested_capital(positions) == Decimal("20000.00")

    def test_position_weight_is_market_value_over_equity(self):
        assert allocation_pct(Decimal("10000"), Decimal("100000")) == Decimal("10.0000")

    def test_exposure_is_all_positions_over_equity(self):
        assert exposure_pct(Decimal("20000"), Decimal("100000")) == Decimal("20.0000")

    def test_exposure_is_undefined_when_equity_is_not_positive(self):
        assert exposure_pct(Decimal("20000"), Decimal("0")) is None
        assert exposure_pct(Decimal("20000"), Decimal("-1")) is None


# ----------------------------------------------------------------------
# Multi-position portfolio (specification scenario)
# ----------------------------------------------------------------------


class TestMultiPositionPortfolio:
    """Two positions of 10,000 each against 100,000 of capital == 20% exposure."""

    def setup_method(self):
        self.cash = Decimal("80000")
        self.positions = [
            valued(1, Decimal("10000"), Decimal("10000")),
            valued(2, Decimal("10000"), Decimal("10000")),
        ]
        self.market_value = sum(p.market_value for p in self.positions)

    def test_total_market_value(self):
        assert self.market_value == Decimal("20000")

    def test_exposure_is_twenty_percent(self):
        total_equity = equity(self.cash, self.market_value)
        assert total_equity == Decimal("100000.00")
        assert exposure_pct(self.market_value, total_equity) == Decimal("20.0000")

    def test_position_weights_sum_to_the_exposure(self):
        total_equity = equity(self.cash, self.market_value)
        weights = [allocation_pct(position.market_value, total_equity) for position in self.positions]
        assert weights == [Decimal("10.0000"), Decimal("10.0000")]
        assert sum(weights) == exposure_pct(self.market_value, total_equity)

    def test_raising_prices_raises_market_value_unrealised_pnl_and_equity(self):
        revalued = [
            valued(1, Decimal("12000"), Decimal("10000")),
            valued(2, Decimal("10000"), Decimal("10000")),
        ]
        new_market_value = sum(p.market_value for p in revalued)
        assert new_market_value == Decimal("22000")
        unrealized = sum(p.unrealized_pnl for p in revalued)
        assert unrealized == Decimal("2000")
        new_equity = equity(self.cash, new_market_value)
        assert new_equity == Decimal("102000.00")
        assert total_pnl(new_equity, Decimal("100000")) == Decimal("2000.00")

    def test_lowering_prices_lowers_equity_and_pnl(self):
        revalued = [
            valued(1, Decimal("8000"), Decimal("10000")),
            valued(2, Decimal("10000"), Decimal("10000")),
        ]
        new_equity = equity(self.cash, sum(p.market_value for p in revalued))
        assert new_equity == Decimal("98000.00")
        assert total_pnl(new_equity, Decimal("100000")) == Decimal("-2000.00")
        assert drawdown_pct(Decimal("100000"), new_equity) == Decimal("2.0000")


# ----------------------------------------------------------------------
# Drawdown
# ----------------------------------------------------------------------


class TestDrawdown:
    """The deterministic equity sequence from the specification."""

    SEQUENCE = [100000, 110000, 105000, 90000, 95000]

    def _points(self, sequence=None):
        values = sequence if sequence is not None else self.SEQUENCE
        base = datetime(2026, 1, 1)
        points = []
        running_peak = Decimal("0")
        for index, value in enumerate(values):
            equity_value = Decimal(value)
            running_peak = max(running_peak, equity_value)
            points.append(
                DrawdownPoint(
                    timestamp=base + timedelta(days=index),
                    equity=equity_value,
                    peak_equity=running_peak,
                    drawdown_pct=signed_drawdown_pct(running_peak, equity_value),
                    exposure_pct=None,
                )
            )
        return points

    def test_peak_is_110000(self):
        peak, _, _, peak_point, _ = drawdown_extremes(self._points())
        assert peak == Decimal("110000.00")
        assert peak_point.equity == Decimal("110000")

    def test_maximum_drawdown_amount_is_20000(self):
        _, amount, _, _, trough = drawdown_extremes(self._points())
        assert amount == Decimal("20000.00")
        assert trough.equity == Decimal("90000")

    def test_maximum_drawdown_percentage(self):
        """(90,000 - 110,000) / 110,000 = -18.1818...%."""
        _, _, pct, _, _ = drawdown_extremes(self._points())
        assert pct == Decimal("18.1818")
        assert pct == ((Decimal("110000") - Decimal("90000")) / Decimal("110000") * 100).quantize(
            Decimal("0.0001")
        )

    def test_signed_drawdown_at_the_trough_matches_the_specification(self):
        points = self._points()
        trough = points[3]
        assert trough.peak_equity == Decimal("110000")
        assert trough.drawdown_pct == Decimal("-18.1818")

    def test_drawdown_is_zero_at_a_peak(self):
        points = self._points()
        assert points[0].drawdown_pct == Decimal("0.0000")
        assert points[1].drawdown_pct == Decimal("0.0000")

    def test_drawdown_recovers_after_the_trough(self):
        points = self._points()
        assert points[4].drawdown_pct == signed_drawdown_pct(
            Decimal("110000"), Decimal("95000")
        )
        assert points[4].drawdown_pct > points[3].drawdown_pct

    def test_monotonic_rise_has_no_drawdown(self):
        _, amount, pct, _, _ = drawdown_extremes(self._points([100000, 110000, 120000]))
        assert amount == Decimal("0.00")
        assert pct == Decimal("0.0000")

    def test_empty_curve_reports_zeros_rather_than_failing(self):
        peak, amount, pct, peak_point, trough_point = drawdown_extremes([])
        assert peak == Decimal("0")
        assert amount == Decimal("0")
        assert pct == Decimal("0")
        assert peak_point is None and trough_point is None


# ----------------------------------------------------------------------
# Trade statistics
# ----------------------------------------------------------------------


class TestTradeStatistics:
    def test_win_rate_percentage(self):
        assert win_rate_pct(3, 4) == Decimal("75.0000")

    def test_win_rate_is_none_without_closed_round_trips(self):
        """No sells means no win rate — a 0% would be a fabricated measurement."""
        assert win_rate_pct(0, 0) is None

    def test_average_of_realised_pnl(self):
        assert average([Decimal("100"), Decimal("300")]) == Decimal("200.00")

    def test_average_is_none_without_values(self):
        assert average([]) is None

    def test_profit_factor(self):
        assert profit_factor([Decimal("300"), Decimal("100")], [Decimal("-200")]) == Decimal("2.0000")

    def test_profit_factor_is_none_without_losses(self):
        """The denominator is zero, so the ratio is undefined rather than infinite."""
        assert profit_factor([Decimal("300")], []) is None

    def test_profit_factor_is_none_without_any_trades(self):
        assert profit_factor([], []) is None


# ----------------------------------------------------------------------
# Insufficient observations
# ----------------------------------------------------------------------


class TestInsufficientObservations:
    """A metric that cannot be computed is ``None``, never a misleading zero."""

    def test_volatility_needs_two_returns(self):
        from app.services.backtest.metrics import annualized_volatility_pct

        assert annualized_volatility_pct([]) is None
        assert annualized_volatility_pct([Decimal("0.01")]) is None
        assert annualized_volatility_pct([Decimal("0.01"), Decimal("-0.02")]) is not None

    def test_sharpe_needs_two_returns(self):
        from app.services.backtest.metrics import sharpe_ratio

        assert sharpe_ratio([], Decimal("0")) is None
        assert sharpe_ratio([Decimal("0.01")], Decimal("0")) is None

    def test_sharpe_of_a_flat_curve_is_none(self):
        """Zero standard deviation has no defined risk-adjusted return."""
        from app.services.backtest.metrics import sharpe_ratio

        assert sharpe_ratio([Decimal("0.01"), Decimal("0.01"), Decimal("0.01")], Decimal("0")) is None


# ----------------------------------------------------------------------
# Daily returns from the equity curve
# ----------------------------------------------------------------------


class TestDailyReturns:
    def _snapshot(self, day: int, equity: Decimal):
        class Snapshot:
            def __init__(self):
                self.timestamp = datetime(2026, 1, 1) + timedelta(days=day)
                self.cash = equity
                self.market_value = Decimal("0")
                self.equity = equity
                self.exposure_pct = Decimal("0")

        return Snapshot()

    def test_returns_are_computed_between_consecutive_days(self):
        from app.services.portfolio.performance_service import daily_returns

        snapshots = [self._snapshot(0, Decimal("100000")), self._snapshot(1, Decimal("110000"))]
        assert daily_returns(snapshots) == [Decimal("0.1")]

    def test_two_snapshots_on_the_same_day_are_one_observation(self):
        """Intraday marks are not extra days; treating them as such would understate volatility."""
        from app.services.portfolio.performance_service import daily_returns

        snapshots = [self._snapshot(0, Decimal("100000")), self._snapshot(0, Decimal("101000"))]
        assert daily_returns(snapshots) == []

    def test_a_single_snapshot_has_no_returns(self):
        from app.services.portfolio.performance_service import daily_returns

        assert daily_returns([self._snapshot(0, Decimal("100000"))]) == []

    def test_a_non_positive_previous_equity_is_skipped(self):
        from app.services.portfolio.performance_service import daily_returns

        snapshots = [self._snapshot(0, Decimal("0")), self._snapshot(1, Decimal("100"))]
        assert daily_returns(snapshots) == []


# ----------------------------------------------------------------------
# Portfolio-level reporting through the service
# ----------------------------------------------------------------------


@pytest.mark.usefixtures("db_session")
class TestPortfolioReporting:
    def test_exposure_and_invested_capital_are_reported_for_a_real_portfolio(
        self, client, db_session
    ):
        from tests.phase10_helpers import create_portfolio, create_stock, buy

        stock_a = create_stock(db_session, "AAA")
        stock_b = create_stock(db_session, "BBB")
        portfolio_id = create_portfolio(db_session, "Reporting", "100000")

        buy(client, portfolio_id, stock_a, 100, 100)
        buy(client, portfolio_id, stock_b, 50, 200)

        response = client.get(f"/api/v1/portfolios/{portfolio_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["market_value"] == "20000.00"
        assert body["equity"] == "100000.00"
        assert body["exposure_pct"] == "20.0000"
        assert body["invested_capital"] == "20000.00"
        assert body["position_count"] == 2