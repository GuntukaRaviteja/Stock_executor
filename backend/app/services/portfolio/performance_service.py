"""Portfolio performance metrics — everything Phase 10 reports about a simulation.

Design rule, inherited from the Phase 8 metrics module and deliberately not relaxed here:

    **a metric that cannot be meaningfully computed is ``None``, never a fabricated or
    zero-substituted number.**

So ``win_rate_pct``, ``profit_factor``, ``annualized_volatility_pct`` and ``sharpe_ratio``
are ``None`` when their sample is empty, has fewer observations than the statistic needs, or
has a zero denominator. A genuinely flat curve reports ``0`` for a drawdown (that *is* the
measurement) and ``None`` for a Sharpe ratio (a flat curve has no defined risk-adjusted
return).

What is measured
----------------
* **Activity** — orders, fills, rejections, executions, open positions.
* **Realised result** — realised and unrealised P&L, total P&L, return %, fees.
* **Closed round trips** — wins, losses, win rate, average winning/losing trade, profit
  factor. A "closed round trip" here is a **sell**: a buy books no realised P&L, so counting
  buys as non-winning trades would understate the result.
* **Equity-curve risk** — peak equity, current drawdown, maximum drawdown (reusing the Phase 8
  :func:`~app.services.backtest.metrics.max_drawdown` so both phases measure a peak the same
  way), exposure, and — only when enough snapshots exist — daily returns, annualised
  volatility and the Sharpe ratio.

Return-based metrics need at least two equity observations to define one return, and at least
two returns to define a standard deviation; below that they are ``None``.
"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.portfolio import Portfolio, PortfolioSnapshot
from app.repositories.order_repository import order_repository
from app.repositories.portfolio_snapshot_repository import portfolio_snapshot_repository
from app.repositories.portfolio_transaction_repository import portfolio_transaction_repository
from app.repositories.trade_repository import trade_repository
from app.schemas.portfolio import OrderSide, OrderStatus, PortfolioStatus
from app.services.backtest.metrics import (
    equity_returns,
    max_drawdown,
    sharpe_ratio,
    annualized_volatility_pct,
)
from app.services.backtest.models import EquityPoint, quantize_money, quantize_percent, quantize_ratio
from app.services.portfolio.models import (
    ZERO,
    DrawdownPoint,
    PortfolioPerformance,
    PortfolioValuation,
)
from app.services.portfolio.risk import peak_equity
from app.services.portfolio.risk_service import day_open_equity
from app.services.portfolio.calculator import (
    allocation_pct,
    daily_pnl,
    drawdown_pct,
    signed_drawdown_pct,
)

#: The equity-curve history read when building the report. Phase 9 already caps history reads
#: at this value; keeping one constant means the performance report, the drawdown curve and
#: the drawdown risk rule all see exactly the same window.
MAX_HISTORY = 1000

#: Trading days per year, the same annualisation factor the Phase 8 metrics module uses, so a
#: portfolio Sharpe ratio and a backtest Sharpe ratio are scaled identically.
_TRADING_DAYS_PER_YEAR = Decimal("252")
_SQRT_TRADING_DAYS = Decimal(252).sqrt()

#: A CAGR over a window shorter than this is not reported: annualising it would extrapolate a
#: trend from too little data to be meaningful.
MIN_CAGR_WINDOW_DAYS = 365


def _annual_mean_return(returns: Sequence[Decimal]) -> Optional[Decimal]:
    """Arithmetic mean of the period returns, scaled to a year."""
    if not returns:
        return None
    mean = sum(returns, ZERO) / Decimal(len(returns))
    return mean * _TRADING_DAYS_PER_YEAR


def round_trip_pnl(trade) -> Decimal:
    """Realised P&L booked by an execution. Buys book zero by construction."""
    return Decimal(trade.realized_pnl)


def win_rate_pct(winners: int, total: int) -> Optional[Decimal]:
    """Percentage of closed round trips that were profitable; ``None`` without any."""
    if total <= 0:
        return None
    return quantize_percent(Decimal(winners) / Decimal(total) * Decimal("100"))


def average(values: Sequence[Decimal]) -> Optional[Decimal]:
    """Arithmetic mean of monetary values, or ``None`` when there are none."""
    if not values:
        return None
    return quantize_money(sum(values, ZERO) / Decimal(len(values)))


def profit_factor(wins: Sequence[Decimal], losses: Sequence[Decimal]) -> Optional[Decimal]:
    """Gross profit divided by gross loss over closed round trips.

    ``None`` when there are no losing trades, because the ratio is then undefined — reporting
    ``inf`` would imply a certainty the data does not support.
    """
    gross_profit = sum((value for value in wins if value > ZERO), ZERO)
    gross_loss = abs(sum((value for value in losses if value < ZERO), ZERO))
    if gross_loss == ZERO:
        return None
    return quantize_ratio(gross_profit / gross_loss)


def downside_returns(returns: Sequence[Decimal]) -> List[Decimal]:
    """The negative part of each return, as a non-negative loss magnitude.

    The empty list when no return is negative, which is what makes the Sortino ratio ``None``
    rather than an infinite one: a curve that never fell has no downside deviation.
    """
    return [abs(value) for value in returns if value < ZERO]


def downside_deviation(returns: Sequence[Decimal]) -> Optional[Decimal]:
    """Root-mean-square of the negative returns; ``None`` when there are none."""
    losses = downside_returns(returns)
    if not losses:
        return None
    total = sum((value * value for value in losses), ZERO)
    return (total / Decimal(len(returns))).sqrt()


def sortino_ratio(
    returns: Sequence[Decimal], risk_free_rate_pct: Decimal
) -> Optional[Decimal]:
    """Annualised return per unit of **downside** volatility.

    ``None`` — never a fabricated number — when there are fewer than two returns or no negative
    return at all, because the denominator is then zero and the ratio undefined.
    """
    if len(returns) < 2:
        return None
    deviation = downside_deviation(returns)
    if deviation is None or deviation == ZERO:
        return None
    annual_mean = _annual_mean_return(returns)
    if annual_mean is None:
        return None
    target = Decimal(risk_free_rate_pct) / _TRADING_DAYS_PER_YEAR
    excess = annual_mean - target
    if excess <= ZERO:
        # A negative excess return is a real, meaningful Sortino ratio, not a missing value.
        return quantize_ratio(excess / (deviation * _SQRT_TRADING_DAYS))
    return quantize_ratio(excess / (deviation * _SQRT_TRADING_DAYS))


def cagr_pct(starting_equity: Decimal, ending_equity: Decimal, days: int) -> Optional[Decimal]:
    """Compound annual growth rate over ``days``, in percent.

    ``None`` when the window is shorter than a year, when the starting equity is not positive, or
    when the ending equity is not positive. A CAGR annualises a window that did not cover a year,
    so a curve six weeks old would report a rate extrapolated from a fortnight; refusing to
    report is the honest answer and the caller receives the reason instead.
    """
    if days < MIN_CAGR_WINDOW_DAYS:
        return None
    if Decimal(starting_equity) <= ZERO or Decimal(ending_equity) <= ZERO:
        return None
    years = Decimal(days) / Decimal(365)
    growth = (Decimal(ending_equity) / Decimal(starting_equity)) ** (Decimal(1) / years)
    return quantize_percent((growth - Decimal(1)) * Decimal("100"))


def insufficient_data_reasons(
    *,
    closed_trades: int,
    return_observations: int,
    snapshots: int,
    days_observed: Optional[int],
    has_losses: bool,
    positive_equity: bool,
    volatility_pct: Optional[Decimal],
    sharpe: Optional[Decimal],
    sortino: Optional[Decimal],
    cagr: Optional[Decimal],
) -> List[str]:
    """The metrics left null because the portfolio has too little history.

    An explicit list is the "insufficient data" state: it distinguishes "this portfolio has not
    done enough yet" from "this metric is zero", which are very different things to a caller.
    """
    reasons: List[str] = []
    if closed_trades == 0:
        reasons.append("win_rate_pct/average gain/average loss/profit_factor: no closed round trips yet")
    elif not has_losses:
        reasons.append("profit_factor: no losing closed round trip, so the ratio is undefined")
    if return_observations < 2:
        reasons.append(
            f"annualized_volatility_pct/sharpe_ratio/sortino_ratio: need at least 2 period "
            f"returns, have {return_observations}"
        )
    elif volatility_pct is None:
        reasons.append("annualized_volatility_pct: every recorded return is identical, so there is no dispersion")
    elif sharpe is None:
        reasons.append("sharpe_ratio: the standard deviation of the returns is zero")
    if sortino is None and return_observations >= 2:
        reasons.append("sortino_ratio: no negative returns, so the downside deviation is zero")
    if cagr is None:
        reasons.append(
            "cagr_pct: the equity curve spans "
            + (f"{days_observed} day(s)" if days_observed is not None else "no recorded window")
            + ", which is shorter than the 365 days a CAGR needs"
        )
    if not positive_equity:
        reasons.append("exposure/weight percentages: portfolio equity is not positive")
    if snapshots < 2:
        reasons.append("drawdown metrics: need at least 2 snapshots, have " f"{snapshots}")
    return reasons


def daily_returns(snapshots: Sequence[PortfolioSnapshot]) -> List[Decimal]:
    """Period-over-period equity returns of the recorded equity curve.

    Only *consecutive* observations produce a return: two snapshots recorded on the same day
    are two samples of one day, not two days, and treating them as separate observations
    would inflate the observation count and understate the variance.
    """
    returns: List[Decimal] = []
    for previous, current in zip(snapshots, snapshots[1:]):
        if previous.timestamp.date() == current.timestamp.date():
            continue
        previous_equity = Decimal(previous.equity)
        if previous_equity <= ZERO:
            continue
        returns.append((Decimal(current.equity) - previous_equity) / previous_equity)
    return returns


class PortfolioPerformanceService:
    """Builds the portfolio performance report and the drawdown curve."""

    def __init__(
        self,
        order_repo=order_repository,
        trade_repo=trade_repository,
        snapshot_repo=portfolio_snapshot_repository,
        transaction_repo=portfolio_transaction_repository,
    ):
        self.order_repo = order_repo
        self.trade_repo = trade_repo
        self.snapshot_repo = snapshot_repo
        self.transaction_repo = transaction_repo

    # --- Public API ---

    def performance(
        self,
        db: Session,
        portfolio: Portfolio,
        valuation: PortfolioValuation,
        risk_free_rate_pct: Decimal = ZERO,
    ) -> PortfolioPerformance:
        """Full performance report for ``portfolio`` at its current valuation.

        ``valuation`` is passed in rather than recomputed so a single request values the
        portfolio once and reuses that reading for every figure below.
        """
        orders = self.order_repo.list_history(db, portfolio.id, skip=0, limit=MAX_HISTORY)
        trades = self.trade_repo.list_history(db, portfolio.id, skip=0, limit=MAX_HISTORY)
        snapshots = self.snapshot_repo.list_curve(db, portfolio.id, skip=0, limit=MAX_HISTORY)

        curve = equity_curve_for_metrics(portfolio, snapshots)
        _, _, worst_amount, worst_pct = max_drawdown(curve)
        peak = peak_equity(
            [Decimal(portfolio.initial_capital), valuation.equity]
            + [Decimal(point.equity) for point in curve]
        )

        sells = [trade for trade in trades if trade.side == OrderSide.SELL.value]
        pnls = [round_trip_pnl(trade) for trade in sells]
        wins = [value for value in pnls if value > ZERO]
        losses = [value for value in pnls if value <= ZERO]
        returns = daily_returns(snapshots)

        volatility = annualized_volatility_pct(returns)
        sharpe = sharpe_ratio(returns, Decimal(risk_free_rate_pct))
        sortino = sortino_ratio(returns, Decimal(risk_free_rate_pct))

        # The CAGR window is measured between the first and last *recorded* equity observation,
        # and the drawdown window between the first and last snapshot — both stated explicitly so
        # a caller can see how much history a figure is based on.
        window_days, first_equity, last_equity = _equity_window(portfolio, valuation, snapshots)
        cagr = cagr_pct(first_equity, last_equity, window_days) if window_days is not None else None

        gross_exposure = valuation.exposure_pct
        cash_pct = allocation_pct(valuation.cash, valuation.equity)
        invested_pct = allocation_pct(valuation.invested_capital, valuation.equity)

        day_open = day_open_equity(
            db, portfolio, snapshot_repo=self.snapshot_repo, transaction_repo=self.transaction_repo
        )
        todays_pnl = daily_pnl(day_open, valuation.equity)
        todays_return_pct = (
            None
            if day_open <= ZERO
            else quantize_percent(todays_pnl / day_open * Decimal("100"))
        )

        reasons = insufficient_data_reasons(
            closed_trades=len(sells),
            return_observations=len(returns),
            snapshots=len(snapshots),
            days_observed=window_days,
            has_losses=bool([value for value in losses if value < ZERO]),
            positive_equity=valuation.equity > ZERO,
            volatility_pct=volatility,
            sharpe=sharpe,
            sortino=sortino,
            cagr=cagr,
        )

        performance = PortfolioPerformance(
            portfolio_id=portfolio.id,
            name=portfolio.name,
            status=PortfolioStatus(portfolio.status),
            initial_capital=valuation.initial_capital,
            cash=valuation.cash,
            market_value=valuation.market_value,
            equity=valuation.equity,
            realized_pnl=valuation.realized_pnl,
            unrealized_pnl=valuation.unrealized_pnl,
            total_pnl=valuation.total_pnl,
            total_fees=valuation.total_fees,
            open_entry_fees=valuation.open_entry_fees,
            return_pct=valuation.return_pct,
            position_count=len(valuation.positions),
            order_count=len(orders),
            filled_order_count=sum(
                1 for order in orders if order.status == OrderStatus.FILLED.value
            ),
            rejected_order_count=sum(
                1 for order in orders if order.status == OrderStatus.REJECTED.value
            ),
            trade_count=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            snapshot_count=len(snapshots),
            peak_equity=peak,
            current_drawdown_pct=drawdown_pct(peak, valuation.equity),
            max_drawdown=worst_amount,
            max_drawdown_pct=worst_pct,
            exposure_pct=valuation.exposure_pct,
            invested_capital=valuation.invested_capital,
            win_rate_pct=win_rate_pct(len(wins), len(sells)),
            closed_trades=len(sells),
            average_realized_pnl=average(pnls),
            average_winning_trade=average(wins),
            average_losing_trade=average(losses),
            profit_factor=profit_factor(wins, losses),
            return_observations=len(returns),
            annualized_volatility_pct=volatility,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            cagr_pct=cagr,
            cagr_window_days=window_days,
            max_drawdown_pct_observed_over_days=window_days,
            gross_exposure_pct=gross_exposure,
            net_exposure_pct=gross_exposure,
            cash_pct=cash_pct,
            invested_pct=invested_pct,
            day_open_equity=day_open,
            daily_pnl=todays_pnl,
            daily_return_pct=todays_return_pct,
            insufficient_data=reasons,
            risk_free_rate_pct=Decimal(risk_free_rate_pct),
            positions=valuation.positions,
        )

        logger.info(
            "Performance for portfolio %s: equity=%s total_pnl=%s trades=%s closed=%s "
            "win_rate=%s max_drawdown_pct=%s",
            portfolio.id,
            performance.equity,
            performance.total_pnl,
            performance.trade_count,
            len(sells),
            performance.win_rate_pct,
            performance.max_drawdown_pct,
        )
        return performance

    def drawdown_curve(
        self, db: Session, portfolio: Portfolio, skip: int = 0, limit: int = MAX_HISTORY
    ) -> List[DrawdownPoint]:
        """The drawdown curve, oldest first, from the recorded equity curve.

        The initial capital is prepended as a synthetic first point so the very first
        snapshot is measured against the capital the portfolio started with rather than
        against itself (which would report a drawdown of zero for a portfolio that opened
        below its starting capital).
        """
        snapshots = self.snapshot_repo.list_curve(db, portfolio.id, skip=skip, limit=limit)
        initial = Decimal(portfolio.initial_capital)

        points: List[DrawdownPoint] = [
            DrawdownPoint(
                timestamp=snapshots[0].timestamp if snapshots else datetime.now(timezone.utc).replace(tzinfo=None),
                equity=initial,
                peak_equity=initial,
                drawdown_pct=signed_drawdown_pct(initial, initial),
                exposure_pct=None,
            )
        ]
        running_peak = initial
        for snapshot in snapshots:
            equity_value = Decimal(snapshot.equity)
            running_peak = max(running_peak, equity_value)
            points.append(
                DrawdownPoint(
                    timestamp=snapshot.timestamp,
                    equity=equity_value,
                    peak_equity=running_peak,
                    drawdown_pct=signed_drawdown_pct(running_peak, equity_value),
                    exposure_pct=(
                        Decimal(snapshot.exposure_pct)
                        if snapshot.exposure_pct is not None
                        else None
                    ),
                )
            )
        return points

    def drawdown_extremes(self, points: Sequence[DrawdownPoint]) -> "tuple":
        """``(peak_equity, max_drawdown, max_drawdown_pct, peak_point, trough_point)``."""
        return drawdown_extremes(points)


def drawdown_extremes(points: Sequence[DrawdownPoint]) -> "tuple":
    """Peak, largest decline and where it happened, from a drawdown curve.

    ``max_drawdown_pct`` is the positive magnitude, matching the Phase 8 metric and the
    ``max_drawdown_pct`` performance field; the curve itself carries the signed value for
    every point.
    """
    if not points:
        return ZERO, ZERO, ZERO, None, None
    peak = points[0].peak_equity
    worst_amount = ZERO
    worst_pct = ZERO
    peak_point = points[0]
    trough_point = points[0]
    for point in points:
        if point.peak_equity > peak:
            peak = point.peak_equity
            peak_point = point
        amount = point.peak_equity - point.equity
        if amount > worst_amount:
            worst_amount = amount
            trough_point = point
            worst_pct = (
                ZERO
                if point.peak_equity <= ZERO
                else quantize_percent(amount / point.peak_equity * Decimal("100"))
            )
    return (
        quantize_money(peak),
        quantize_money(worst_amount),
        worst_pct,
        peak_point,
        trough_point,
    )


def equity_curve_for_metrics(
    portfolio: Portfolio, snapshots: Sequence[PortfolioSnapshot]
) -> List[EquityPoint]:
    """Adapt portfolio snapshots onto the Phase 8 equity-curve type.

    Reusing the Phase 8 type (and therefore the Phase 8 ``max_drawdown``) is what guarantees
    a portfolio drawdown and a backtest drawdown are computed by the same code.
    """
    points = [
        EquityPoint(
            date=snapshot.timestamp.date(),
            cash=Decimal(snapshot.cash),
            position_value=Decimal(snapshot.market_value),
            equity=Decimal(snapshot.equity),
        )
        for snapshot in snapshots
    ]
    initial = Decimal(portfolio.initial_capital)
    first_date = points[0].date if points else None
    return [
        EquityPoint(
            date=first_date if first_date is not None else _fallback_date(),
            cash=initial,
            position_value=ZERO,
            equity=initial,
        ),
        *points,
    ]


def portfolio_return_series(points: Sequence[DrawdownPoint]) -> List[Decimal]:
    """Period-over-period returns of a drawdown curve (same convention as Phase 8)."""
    return equity_returns(
        [EquityPoint(date=point.timestamp.date(), cash=ZERO, position_value=ZERO, equity=point.equity) for point in points]
    )


def _equity_window(
    portfolio: Portfolio, valuation: PortfolioValuation, snapshots: Sequence[PortfolioSnapshot]
) -> tuple:
    """``(days, first_equity, last_equity)`` for the CAGR window.

    The first observation is the portfolio's initial capital at the first snapshot's date, which
    is the same synthetic starting point the drawdown curve uses; the last observation is the
    current equity at the most recent snapshot's date, or at ``as_of`` when no snapshot exists.
    Without two dated observations there is no window, and the caller reports no CAGR.
    """
    if not snapshots:
        return None, Decimal(portfolio.initial_capital), valuation.equity

    first_date = snapshots[0].timestamp.date()
    last_date = snapshots[-1].timestamp.date()
    days = (last_date - first_date).days
    first_equity = Decimal(portfolio.initial_capital)
    last_equity = Decimal(snapshots[-1].equity)
    return days, first_equity, last_equity


def _fallback_date():
    return datetime.now(timezone.utc).date()