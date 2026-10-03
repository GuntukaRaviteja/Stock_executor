"""Shared helpers for the Phase 14 strategy capital-allocation tests.

Deterministic and offline: no provider is contacted, no network call is made, and every
expected amount is derived from the rule under test rather than copied from a run.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional, Sequence

from app.models.backtest import Backtest, BacktestEquityPoint
from app.models.stock import Stock
from app.schemas.optimization import (
    AllocationMethod,
    OptimizeRequest,
    StrategyPerformanceSchema,
    StrategyWeightSchema,
)
from app.services.backtest.models import EquityPoint
from app.services.portfolio.optimization import (
    AllocationMethod as EngineAllocationMethod,
    StrategyPerformance,
)

BASE = "/api/v1/portfolios"

#: A round-trip capital figure used in most tests.
CAPITAL = Decimal("1_000_000")
ANCHOR = date(2024, 1, 1)


def strategy_perf(
    strategy_id: str,
    *,
    total_return_pct="0",
    sharpe_ratio=None,
    max_drawdown_pct="10",
    annualized_volatility_pct="20",
    number_of_trades=10,
    win_rate_pct=None,
    profit_factor=None,
    window_start: Optional[date] = None,
    window_end: Optional[date] = None,
) -> StrategyPerformance:
    """Build a :class:`StrategyPerformance` with explicit, deterministic fields."""
    return StrategyPerformance(
        strategy_id=strategy_id,
        strategy_name=strategy_id,
        total_return_pct=Decimal(str(total_return_pct)),
        sharpe_ratio=Decimal(str(sharpe_ratio)) if sharpe_ratio is not None else None,
        max_drawdown_pct=Decimal(str(max_drawdown_pct)),
        annualized_volatility_pct=Decimal(str(annualized_volatility_pct)) if annualized_volatility_pct is not None else None,
        number_of_trades=number_of_trades,
        win_rate_pct=Decimal(str(win_rate_pct)) if win_rate_pct is not None else None,
        profit_factor=Decimal(str(profit_factor)) if profit_factor is not None else None,
        window_start=window_start,
        window_end=window_end,
    )


def strategy_perf_schema(
    strategy_id: str,
    *,
    total_return_pct="0",
    sharpe_ratio=None,
    max_drawdown_pct="10",
    annualized_volatility_pct="20",
    number_of_trades=10,
    win_rate_pct=None,
    profit_factor=None,
    window_start: Optional[date] = None,
    window_end: Optional[date] = None,
) -> StrategyPerformanceSchema:
    """Build a :class:`StrategyPerformanceSchema` for API request bodies."""
    return StrategyPerformanceSchema(
        strategy_id=strategy_id,
        strategy_name=strategy_id,
        total_return_pct=Decimal(str(total_return_pct)),
        sharpe_ratio=Decimal(str(sharpe_ratio)) if sharpe_ratio is not None else None,
        max_drawdown_pct=Decimal(str(max_drawdown_pct)),
        annualized_volatility_pct=Decimal(str(annualized_volatility_pct)) if annualized_volatility_pct is not None else None,
        number_of_trades=number_of_trades,
        win_rate_pct=Decimal(str(win_rate_pct)) if win_rate_pct is not None else None,
        profit_factor=Decimal(str(profit_factor)) if profit_factor is not None else None,
        window_start=window_start,
        window_end=window_end,
    )


def optimize_request(
    total_capital=CAPITAL,
    method=AllocationMethod.EQUAL_WEIGHT,
    strategies: Optional[Sequence] = None,
    user_weights: Optional[Sequence] = None,
    min_weight_pct="0",
    max_weight_pct="100",
) -> dict:
    """Build a JSON-serializable request body dict for the optimization endpoint."""
    body = {
        "total_capital": str(total_capital),
        "method": method.value,
        "min_weight_pct": str(min_weight_pct),
        "max_weight_pct": str(max_weight_pct),
    }
    if strategies is not None:
        body["strategies"] = [
            {
                "strategy_id": s.strategy_id,
                "strategy_name": s.strategy_name,
                "total_return_pct": str(s.total_return_pct),
                "sharpe_ratio": None if s.sharpe_ratio is None else str(s.sharpe_ratio),
                "max_drawdown_pct": str(s.max_drawdown_pct),
                "annualized_volatility_pct": None if s.annualized_volatility_pct is None else str(s.annualized_volatility_pct),
                "number_of_trades": s.number_of_trades,
                "win_rate_pct": None if s.win_rate_pct is None else str(s.win_rate_pct),
                "profit_factor": None if s.profit_factor is None else str(s.profit_factor),
                "window_start": s.window_start.isoformat() if s.window_start else None,
                "window_end": s.window_end.isoformat() if s.window_end else None,
            }
            for s in strategies
        ]
    if user_weights is not None:
        body["user_weights"] = [
            {"strategy_id": w.strategy_id, "weight_pct": str(w.weight_pct)}
            for w in user_weights
        ]
    return body


def create_strategy(
    db_session, name: str, initial_capital=Decimal("100000"),
    start_date=ANCHOR, end_date=None,
) -> int:
    """Insert a Stock + a persisted Backtest for a named strategy and return the stock id."""
    if end_date is None:
        end_date = start_date + timedelta(days=30)

    stock = Stock(
        symbol=name[:4].upper(), company_name=f"{name} Ltd", exchange="NSE", market="INDIA"
    )
    db_session.add(stock)
    db_session.flush()

    backtest = Backtest(
        stock_id=stock.id,
        strategy_name=name,
        strategy_version="1.0",
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        final_equity=initial_capital * Decimal("1.1"),
        total_return_pct=Decimal("10.0000"),
        max_drawdown_pct=Decimal("5.0000"),
        annualized_volatility_pct=Decimal("15.0000"),
        sharpe_ratio=Decimal("1.2"),
        number_of_trades=5,
    )
    db_session.add(backtest)
    db_session.commit()
    return stock.id


#: Zero used by persist_equity_curve
ZERO = Decimal("0")


def persist_equity_curve(db_session, backtest_id: int, closes: Sequence[Decimal], start: date) -> None:
    """Write equity points derived from a close-series for one backtest."""
    equity: Decimal = Decimal("1")
    points = [EquityPoint(date=start, cash=equity, position_value=ZERO, equity=equity)]
    for index, close in enumerate(closes):
        d = start + timedelta(days=index + 1)
        prev = equity
        equity = Decimal(str(close)) / Decimal(str(closes[0])) if closes else Decimal("1")
        ret = (equity - prev) / prev if prev > 0 else ZERO
        equity = prev * (Decimal("1") + ret)
        points.append(EquityPoint(date=d, cash=equity, position_value=ZERO, equity=equity))
    for p in points:
        db_session.add(
            BacktestEquityPoint(backtest_id=backtest_id, date=p.date, cash=p.cash,
                                position_value=p.position_value, equity=p.equity)
        )
    db_session.commit()


def create_portfolio_via_api(client, name="P14", capital="1000000") -> dict:
    """Create a portfolio through the API and return the parsed response."""
    body = {
        "name": name,
        "initial_capital": capital,
        "trading_costs": {"commission_rate": "0", "min_fee": "0", "slippage_rate": "0"},
    }
    response = client.post(BASE, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def optimize_via_api(client, portfolio_id: int, body: dict):
    """POST the optimization endpoint and return the raw response."""
    return client.post(f"{BASE}/{portfolio_id}/optimize", json=body)


def strip_volatile(value):
    """Strip primary keys and wall-clock timestamps so two runs can be compared for equality."""
    if isinstance(value, dict):
        return {
            key: strip_volatile(item)
            for key, item in value.items()
            if key not in {"portfolio_id", "as_of", "order_id", "trade_id", "id", "currency"}
        }
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


__all__ = [
    "BASE",
    "CAPITAL",
    "ANCHOR",
    "strategy_perf",
    "strategy_perf_schema",
    "optimize_request",
    "create_strategy",
    "persist_equity_curve",
    "create_portfolio_via_api",
    "optimize_via_api",
    "strip_volatile",
]
