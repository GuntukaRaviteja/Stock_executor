"""Performance and risk metrics for Phase 8 backtests.

Every function here is a pure, deterministic calculation over already-simulated results.
The central design rule is: **a metric that cannot be meaningfully computed is ``None``,
never a fabricated or zero-substituted number.** Division by zero, an empty trade list or
a single observation all resolve to ``None`` for the affected metric while leaving the
rest of the report intact.

Annualisation convention
------------------------
Daily equity returns are annualised with :data:`TRADING_DAYS_PER_YEAR` (252). This is the
same constant used by the Phase 6 volatility calculation, so metrics stay consistent
across phases. A risk-free rate is expressed as an annual percentage in the API and
converted to a daily rate here by dividing by 252 — the default is exactly ``0`` and is
never assumed to be non-zero.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from math import sqrt
from typing import List, Optional, Sequence

from app.services.backtest.models import (
    EquityPoint,
    PerformanceMetrics,
    Trade,
    quantize_money,
    quantize_count,
    quantize_percent,
    quantize_ratio,
)

#: Number of trading days used to annualise volatility, Sharpe and Sortino.
#: Matches the Phase 6 ``analytics_service`` volatility annualisation constant.
TRADING_DAYS_PER_YEAR = Decimal("252")

#: Minimum calendar days required before a CAGR is reported. Below this the annualisation
#: exponent explodes and the number would be dominated by rounding, so it is ``None``.
MIN_DAYS_FOR_CAGR = 30

#: Minimum number of daily returns required before volatility, Sharpe or Sortino are
#: reported. A sample standard deviation needs at least two observations.
MIN_OBSERVATIONS_FOR_RISK = 2

#: Basis-point scale for the daily risk-free rate derived from an annual percentage.
DAILY_RISK_FREE_DIVISOR = TRADING_DAYS_PER_YEAR


def _zero() -> Decimal:
    return Decimal("0")


def equity_returns(equity_curve: Sequence[EquityPoint]) -> List[Decimal]:
    """Simple period-over-period returns of the equity curve.

    Returns one value fewer than the curve: ``returns[i]`` is the return from
    ``equity_curve[i]`` to ``equity_curve[i + 1]``. An empty curve, or any bar where the
    previous equity is not strictly positive, contributes no observation rather than an
    undefined one.
    """
    returns: List[Decimal] = []
    for previous, current in zip(equity_curve, equity_curve[1:]):
        if previous.equity <= 0:
            continue
        returns.append((current.equity - previous.equity) / previous.equity)
    return returns


def total_return_pct(initial_capital: Decimal, final_equity: Decimal) -> Decimal:
    """``(final_equity / initial_capital - 1) * 100``.

    Returns ``0`` when the initial capital is not strictly positive; a backtest can never
    start from a non-positive capital (the API rejects it), so this is a defensive guard.
    """
    if initial_capital <= 0:
        return _zero()
    return quantize_percent((final_equity / initial_capital - Decimal("1")) * Decimal("100"))


def cagr_pct(initial_capital: Decimal, final_equity: Decimal, days: int) -> Optional[Decimal]:
    """Compound annual growth rate as a percentage.

    ``((final / initial) ** (365 / days) - 1) * 100``

    Returns ``None`` when the window is shorter than :data:`MIN_DAYS_FOR_CAGR`, when the
    capital is not positive, or when the final equity is not positive (a total loss makes
    the geometric growth rate undefined).
    """
    if days < MIN_DAYS_FOR_CAGR or initial_capital <= 0 or final_equity <= 0:
        return None
    years = Decimal(days) / Decimal("365")
    try:
        growth = (final_equity / initial_capital) ** (Decimal("1") / years)
    except (ArithmeticError, ValueError):
        return None
    return quantize_percent((growth - Decimal("1")) * Decimal("100"))


def drawdown_series(equity_curve: Sequence[EquityPoint]) -> List[Decimal]:
    """Peak-to-trough drawdown of each bar, as a positive fraction of the running peak."""
    if not equity_curve:
        return []
    series: List[Decimal] = []
    peak = equity_curve[0].equity
    for point in equity_curve:
        if point.equity > peak:
            peak = point.equity
        series.append(_zero() if peak <= 0 else (peak - point.equity) / peak)
    return series


def max_drawdown(equity_curve: Sequence[EquityPoint]) -> "tuple[Decimal, Decimal, Decimal, Decimal]":
    """Return ``(peak_equity, trough_equity, max_drawdown, max_drawdown_pct)``.

    ``max_drawdown`` is the absolute currency amount of the largest peak-to-trough
    decline and ``max_drawdown_pct`` is that amount expressed as a positive percentage of
    the peak. An empty or single-bar curve reports all zeros rather than ``None``: a flat
    curve genuinely has no drawdown, so ``0`` is a real measurement, not a substitute.
    """
    if not equity_curve:
        return _zero(), _zero(), _zero(), _zero()

    peak = equity_curve[0].equity
    current_peak_equity = peak
    worst_amount = _zero()
    worst_pct = _zero()
    trough = equity_curve[0].equity

    for point in equity_curve:
        if point.equity > peak:
            peak = point.equity
            current_peak_equity = point.equity
        amount = peak - point.equity
        if amount > worst_amount:
            worst_amount = amount
            trough = point.equity
            worst_pct = _zero() if peak <= 0 else amount / peak

    return current_peak_equity, trough, quantize_money(worst_amount), quantize_percent(worst_pct * Decimal("100"))


def _winning_and_losing(trades: Sequence[Trade]) -> "tuple[List[Trade], List[Trade]]":
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl <= 0]
    return winners, losers


def win_rate_pct(trades: Sequence[Trade]) -> Optional[Decimal]:
    """Percentage of closed trades with positive net P&L. ``None`` when there are no trades."""
    if not trades:
        return None
    winners, _ = _winning_and_losing(trades)
    return quantize_percent(Decimal(len(winners)) / Decimal(len(trades)) * Decimal("100"))


def profit_factor(trades: Sequence[Trade]) -> Optional[Decimal]:
    """Gross profit divided by gross loss.

    Returns ``None`` when there are no trades, or when there are no losing trades at all
    (the denominator is zero and the ratio is genuinely undefined — reporting ``inf``
    would imply a certainty the data does not support).
    """
    if not trades:
        return None
    gross_profit = sum((trade.net_pnl for trade in trades if trade.net_pnl > 0), _zero())
    gross_loss = abs(sum((trade.net_pnl for trade in trades if trade.net_pnl < 0), _zero()))
    if gross_loss == 0:
        return None
    return quantize_ratio(gross_profit / gross_loss)


def _mean(values: Sequence[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    return sum(values, _zero()) / Decimal(len(values))


def _stdev(values: Sequence[Decimal]) -> Optional[Decimal]:
    """Sample standard deviation. ``None`` for fewer than two observations."""
    if len(values) < 2:
        return None
    mean = _mean(values)
    if mean is None:
        return None
    variance = sum(((value - mean) ** 2 for value in values), _zero()) / Decimal(len(values) - 1)
    return Decimal(str(sqrt(float(variance))))


def annualized_volatility_pct(returns: Sequence[Decimal]) -> Optional[Decimal]:
    """Annualised volatility of periodic returns as a percentage.

    ``None`` when fewer than :data:`MIN_OBSERVATIONS_FOR_RISK` returns exist. A genuinely
    zero-voltatility series legitimately reports ``0``.
    """
    stdev = _stdev(returns)
    if stdev is None:
        return None
    return quantize_percent(stdev * Decimal(str(sqrt(float(TRADING_DAYS_PER_YEAR)))) * Decimal("100"))


def sharpe_ratio(returns: Sequence[Decimal], risk_free_rate_pct: Decimal) -> Optional[Decimal]:
    """Annualised Sharpe ratio.

    The annual risk-free rate is converted to a daily rate before subtracting, so a
    ``risk_free_rate_pct`` of ``0`` (the default) leaves returns untouched. Returns
    ``None`` when there are too few observations, or when the standard deviation of
    excess returns is zero — a flat equity curve has no defined risk-adjusted return.
    """
    if len(returns) < MIN_OBSERVATIONS_FOR_RISK:
        return None
    daily_rf = risk_free_rate_pct / Decimal("100") / DAILY_RISK_FREE_DIVISOR
    excess = [value - daily_rf for value in returns]
    stdev = _stdev(excess)
    mean_excess = _mean(excess)
    if stdev is None or mean_excess is None or stdev == 0:
        return None
    return quantize_ratio(mean_excess / stdev * Decimal(str(sqrt(float(TRADING_DAYS_PER_YEAR)))))


def sortino_ratio(returns: Sequence[Decimal], risk_free_rate_pct: Decimal) -> Optional[Decimal]:
    """Annualised Sortino ratio.

    Downside deviation uses only negative excess returns, so upside volatility is not
    penalised. Returns ``None`` when there are too few observations, when there is no
    downside at all, or when a single downside observation cannot define a deviation.
    """
    if len(returns) < MIN_OBSERVATIONS_FOR_RISK:
        return None
    daily_rf = risk_free_rate_pct / Decimal("100") / DAILY_RISK_FREE_DIVISOR
    excess = [value - daily_rf for value in returns]
    downside = [value for value in excess if value < 0]
    if len(downside) < MIN_OBSERVATIONS_FOR_RISK:
        return None
    downside_deviation = _stdev(downside)
    mean_excess = _mean(excess)
    if downside_deviation is None or mean_excess is None or downside_deviation == 0:
        return None
    return quantize_ratio(
        mean_excess / downside_deviation * Decimal(str(sqrt(float(TRADING_DAYS_PER_YEAR))))
    )


def build_performance_metrics(
    *,
    initial_capital: Decimal,
    final_equity: Decimal,
    equity_curve: Sequence[EquityPoint],
    trades: Sequence[Trade],
    risk_free_rate_pct: Decimal,
    start_date: date,
    end_date: date,
) -> PerformanceMetrics:
    """Assemble the full metrics report for a completed backtest."""
    returns = equity_returns(equity_curve)
    peak, trough, drawdown_amount, drawdown_pct = max_drawdown(equity_curve)
    winners, losers = _winning_and_losing(trades)

    trade_returns = [trade.return_pct for trade in trades]
    holding_periods = [Decimal(trade.holding_period) for trade in trades]
    calendar_days = (end_date - start_date).days

    return PerformanceMetrics(
        initial_capital=quantize_money(initial_capital),
        final_equity=quantize_money(final_equity),
        total_return_pct=total_return_pct(initial_capital, final_equity),
        cagr_pct=cagr_pct(initial_capital, final_equity, calendar_days),
        peak_equity=quantize_money(peak),
        trough_equity=quantize_money(trough),
        max_drawdown=drawdown_amount,
        max_drawdown_pct=drawdown_pct,
        win_rate_pct=win_rate_pct(trades),
        profit_factor=profit_factor(trades),
        number_of_trades=len(trades),
        winning_trades=len(winners),
        losing_trades=len(losers),
        average_trade_return_pct=(
            quantize_percent(value) if (value := _mean(trade_returns)) is not None else None
        ),
        average_winning_trade_pct=(
            quantize_percent(value)
            if (value := _mean([trade.return_pct for trade in winners])) is not None
            else None
        ),
        average_losing_trade_pct=(
            quantize_percent(value)
            if (value := _mean([trade.return_pct for trade in losers])) is not None
            else None
        ),
        average_holding_period=(
            quantize_count(value) if (value := _mean(holding_periods)) is not None else None
        ),
        best_trade_pct=max(trade_returns) if trade_returns else None,
        worst_trade_pct=min(trade_returns) if trade_returns else None,
        annualized_volatility_pct=annualized_volatility_pct(returns),
        sharpe_ratio=sharpe_ratio(returns, risk_free_rate_pct),
        sortino_ratio=sortino_ratio(returns, risk_free_rate_pct),
    )