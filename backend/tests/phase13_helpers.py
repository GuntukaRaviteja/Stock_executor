"""Shared helpers for the Phase 13 portfolio & risk-management tests.

Everything here is deterministic and offline: no provider is contacted, no network call is
made, and every expected amount is derived from the rule under test rather than copied from a
run, so a failure always points at the engine and never at the fixture.

Bar series are built from explicit price paths rather than generated, because the look-ahead
tests need two paths that are *identical up to an entry and then diverge* — which a generator
cannot promise.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from app.models.stock import Stock
from app.schemas.portfolio import (
    PortfolioCreate,
    RiskLimitSettings,
    TradingCostSettings,
)
from app.services.backtest.indicators import PointInTimeContextBuilder
from app.services.backtest.models import Bar
from app.services.backtest.validator import load_bars, validate_bars
from app.services.portfolio.models import (
    AllocationCandidate,
    PortfolioRiskPolicy,
    PortfolioValuation,
    PositionValuation,
)
from app.services.portfolio.service import paper_trading_service

BASE = "/api/v1/portfolios"

#: Zero-cost model, so a test can assert exact arithmetic without fees in the way.
NO_FEES = {
    "commission_rate": Decimal("0"),
    "min_fee": Decimal("0"),
    "slippage_rate": Decimal("0"),
}


# ----------------------------------------------------------------------
# Policies
# ----------------------------------------------------------------------


def policy(**overrides) -> PortfolioRiskPolicy:
    """A Phase 13 policy with sensible test defaults, overridable per field.

    Defaults chosen so a *typical* allocation succeeds: a 30% position cap, a 1% risk budget,
    a 5% stop, no exposure ceiling, no position cap, no cash reserve. A test turns a limit on
    precisely because it is testing that limit.
    """
    fields = {
        "max_position_weight_pct": Decimal("30"),
        "max_risk_per_trade_pct": Decimal("1"),
        "min_cash_reserve_pct": Decimal("0"),
        "commission_rate": Decimal("0"),
        "min_fee": Decimal("0"),
        "slippage_rate": Decimal("0"),
    }
    fields.update(overrides)
    resolved = PortfolioRiskPolicy(**fields)
    resolved.validate()
    return resolved


def policy_body(**overrides) -> dict:
    """The same policy as a JSON body for the API tests."""
    resolved = policy(**overrides)
    return {
        "max_position_weight_pct": str(resolved.max_position_weight_pct),
        "max_exposure_pct": None
        if resolved.max_exposure_pct is None
        else str(resolved.max_exposure_pct),
        "max_risk_per_trade_pct": str(resolved.max_risk_per_trade_pct),
        "max_drawdown_pct": None
        if resolved.max_drawdown_pct is None
        else str(resolved.max_drawdown_pct),
        "max_positions": resolved.max_positions,
        "min_cash_reserve_pct": str(resolved.min_cash_reserve_pct),
        "max_sector_weight_pct": None
        if resolved.max_sector_weight_pct is None
        else str(resolved.max_sector_weight_pct),
        "max_total_risk_pct": None
        if resolved.max_total_risk_pct is None
        else str(resolved.max_total_risk_pct),
        "stop_loss_pct": None
        if resolved.stop_loss_pct is None
        else str(resolved.stop_loss_pct),
        "min_trade_value": None
        if resolved.min_trade_value is None
        else str(resolved.min_trade_value),
        "commission_rate": str(resolved.commission_rate),
        "min_fee": str(resolved.min_fee),
        "slippage_rate": str(resolved.slippage_rate),
    }


# ----------------------------------------------------------------------
# Candidates
# ----------------------------------------------------------------------


def candidate(
    stock_id: int,
    entry_price,
    *,
    stop_price=None,
    score: int = 0,
    symbol: Optional[str] = None,
    sector: Optional[str] = None,
    eligible: bool = True,
    ineligible_reason: Optional[str] = None,
) -> AllocationCandidate:
    """One engine candidate."""
    return AllocationCandidate(
        stock_id=stock_id,
        entry_price=Decimal(entry_price),
        stop_price=None if stop_price is None else Decimal(stop_price),
        composite_score=score,
        symbol=symbol,
        sector=sector,
        eligible=eligible,
        ineligible_reason=ineligible_reason,
    )


def candidate_body(stock_id: int, entry_price=None, score=None, **extra) -> dict:
    """One candidate as a JSON body for the API tests."""
    body = {"stock_id": stock_id}
    if entry_price is not None:
        body["entry_price"] = str(entry_price)
    if score is not None:
        body["composite_score"] = score
    body.update(extra)
    return body


def persist_bars(db_session, stock_id: int, bars) -> None:
    """Write every bar to the price table, so the endpoints read real stored history."""
    from app.models.price import StockPrice

    for item in bars:
        db_session.add(
            StockPrice(
                stock_id=stock_id,
                price_date=item.date,
                open_price=item.open,
                high_price=item.high,
                low_price=item.low,
                close_price=item.close,
                volume=item.volume or 0,
            )
        )
    db_session.commit()


# ----------------------------------------------------------------------
# Valuations
# ----------------------------------------------------------------------


def position_valuation(
    stock_id: int,
    quantity: int,
    entry_price,
    current_price=None,
) -> PositionValuation:
    """One valued position with a consistent cost basis and unrealised P&L."""
    entry = Decimal(entry_price)
    current = Decimal(entry if current_price is None else current_price)
    value = (current * Decimal(quantity)).quantize(Decimal("0.01"))
    cost = (entry * Decimal(quantity)).quantize(Decimal("0.01"))
    return PositionValuation(
        stock_id=stock_id,
        quantity=quantity,
        average_entry_price=entry,
        current_price=current,
        market_value=value,
        unrealized_pnl=((current - entry) * Decimal(quantity)).quantize(Decimal("0.01")),
        realized_pnl=Decimal("0"),
        entry_fees_total=Decimal("0"),
        cost_basis=cost,
    )


def valuation(
    *,
    initial_capital="100000",
    cash="100000",
    positions: Sequence[PositionValuation] = (),
    realized_pnl="0",
    portfolio_id: int = 1,
) -> PortfolioValuation:
    """A complete mark-to-market valuation whose totals are derived from its positions."""
    market_value = sum((position.market_value for position in positions), Decimal("0"))
    invested = sum((position.cost_basis for position in positions), Decimal("0"))
    unrealized = sum((position.unrealized_pnl for position in positions), Decimal("0"))
    equity = (Decimal(cash) + market_value).quantize(Decimal("0.01"))
    exposure = (
        (market_value / equity * Decimal("100")).quantize(Decimal("0.0001"))
        if equity > 0
        else None
    )
    return PortfolioValuation(
        portfolio_id=portfolio_id,
        initial_capital=Decimal(initial_capital),
        cash=Decimal(cash),
        market_value=market_value,
        equity=equity,
        realized_pnl=Decimal(realized_pnl),
        unrealized_pnl=unrealized,
        total_pnl=(equity - Decimal(initial_capital)).quantize(Decimal("0.01")),
        total_fees=Decimal("0"),
        open_entry_fees=Decimal("0"),
        return_pct=((equity - Decimal(initial_capital)) / Decimal(initial_capital) * 100).quantize(
            Decimal("0.0001")
        ),
        invested_capital=invested,
        exposure_pct=exposure,
        positions=tuple(positions),
    )


# ----------------------------------------------------------------------
# Bar series
# ----------------------------------------------------------------------


def bar(trading_date: date, o, h, low, c, volume: int = 1000) -> Bar:
    """One OHLCV bar with ``Decimal`` prices."""
    return Bar(
        date=trading_date,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=volume,
    )


def flat_bars(
    start: date, count: int, price, *, warmup: int = 0, step: Decimal = Decimal("0")
) -> List[Bar]:
    """``count`` consecutive bars at ``price`` (plus ``warmup`` earlier ones).

    A flat path is the neutral input for a look-ahead test: any future variation has to be
    introduced deliberately, so a decision that changes can only be caused by that variation.
    """
    prices = [
        Decimal(price) + step * Decimal(index) for index in range(count + warmup)
    ]
    first = start - timedelta(days=warmup) if warmup else start
    return [
        bar(first + timedelta(days=index), value, value, value, value)
        for index, value in enumerate(prices)
    ]


def replace_tail(bars: Sequence[Bar], from_index: int, values: Sequence[Decimal]) -> List[Bar]:
    """The same bars with their tail replaced by ``values``, leaving OHLC flat per bar.

    This is how the "two paths identical up to entry, divergent afterwards" fixture is built:
    every bar before ``from_index`` is untouched, so anything the engine decides before that
    point cannot have been influenced by the tail.
    """
    result = list(bars)
    for offset, value in enumerate(values):
        index = from_index + offset
        if index >= len(result):
            break
        result[index] = bar(result[index].date, value, value, value, value)
    return result


def persisted_series(
    db_session,
    stock_id: int,
    symbol: str,
    bars: Sequence[Bar],
) -> "tuple":
    """Write ``bars`` to the price table, then load, validate and build contexts for them.

    Going through the database rather than handing the engine the list directly is what makes
    the backtest tests exercise the real load-and-validate path, including the ordering
    guarantee :func:`load_bars` provides.
    """
    from app.models.price import StockPrice
    from app.repositories.price_repository import price_repository

    for item in bars:
        db_session.add(
            StockPrice(
                stock_id=stock_id,
                price_date=item.date,
                open_price=item.open,
                high_price=item.high,
                low_price=item.low,
                close_price=item.close,
                volume=item.volume or 0,
            )
        )
    db_session.commit()
    validated = validate_bars(load_bars(price_repository.get_history_asc(db_session, stock_id))).bars
    contexts = PointInTimeContextBuilder().build(stock_id, symbol, validated)
    return validated, contexts


# ----------------------------------------------------------------------
# Database-backed fixtures
# ----------------------------------------------------------------------


def create_stock(db_session, symbol: str = "TEST", *, sector: Optional[str] = None) -> int:
    """Insert a stock master row (and an optional company profile) and return its id."""
    from app.models.company import Company

    stock = Stock(
        symbol=symbol, company_name=f"{symbol} Limited", exchange="NSE", market="INDIA"
    )
    db_session.add(stock)
    db_session.flush()
    if sector is not None:
        db_session.add(Company(stock_id=stock.id, sector=sector, industry="Generic"))
    db_session.commit()
    db_session.refresh(stock)
    return stock.id


def store_price(db_session, stock_id: int, trading_date: date, close) -> None:
    """Store one OHLCV row so the latest-close lookup has something to find."""
    from app.models.price import StockPrice

    value = Decimal(str(close))
    db_session.add(
        StockPrice(
            stock_id=stock_id,
            price_date=trading_date,
            open_price=value,
            high_price=value,
            low_price=value,
            close_price=value,
            volume=1000,
        )
    )
    db_session.commit()


def create_portfolio(
    db_session,
    name="P13",
    initial_capital="100000",
    *,
    fees=None,
    risk_limits: Optional[dict] = None,
    allocation_config: Optional[dict] = None,
) -> int:
    """Create a portfolio through the service layer and return its id."""
    payload = PortfolioCreate(
        name=name,
        initial_capital=Decimal(initial_capital),
        trading_costs=TradingCostSettings(**(fees if fees is not None else NO_FEES)),
        risk_limits=RiskLimitSettings(**(risk_limits or {})),
        allocation_config=allocation_config,
    )
    return paper_trading_service.create_portfolio(db_session, payload).id


def create_via_api(client, name="P13", capital="100000", **extra) -> dict:
    """Create a portfolio through the API and return the parsed response."""
    body = {
        "name": name,
        "initial_capital": capital,
        "trading_costs": {key: str(value) for key, value in NO_FEES.items()},
    }
    body.update(extra)
    response = client.post(BASE, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def allocate(client, portfolio_id: int, body: dict):
    """POST the allocation endpoint and return the raw response."""
    return client.post(f"{BASE}/{portfolio_id}/allocate", json=body)


def risk_state(client, portfolio_id: int, *, prices: Optional[str] = None):
    """GET the Phase 13 risk state and return the raw response."""
    return client.get(
        f"{BASE}/{portfolio_id}/risk/state", params={"prices": prices} if prices else None
    )


def position_size(client, portfolio_id: int, body: dict):
    """POST the Phase 13 sizing endpoint and return the raw response."""
    return client.post(f"{BASE}/{portfolio_id}/risk/position-size", json=body)


def backtest(client, portfolio_id: int, body: dict):
    """POST the portfolio backtest endpoint and return the raw response."""
    return client.post(f"{BASE}/{portfolio_id}/backtest", json=body)


def buy_via_api(client, portfolio_id: int, stock_id: int, quantity: int, price) -> dict:
    """Fill a BUY through the ordinary order path, so a test starts from real state."""
    response = client.post(
        f"{BASE}/{portfolio_id}/orders",
        json={
            "stock_id": stock_id,
            "side": "BUY",
            "quantity": quantity,
            "requested_price": str(price),
            "order_type": "MARKET",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ----------------------------------------------------------------------
# Assertions
# ----------------------------------------------------------------------


def strip_volatile(value):
    """Strip primary keys and wall-clock timestamps so two runs can be compared for equality.

    Determinism is asserted structurally rather than by eyeballing: anything other than an
    identity or a timestamp that differs between two runs would fail here.
    """
    if isinstance(value, dict):
        return {
            key: strip_volatile(item)
            for key, item in value.items()
            if key not in {"portfolio_id", "as_of", "order_id", "trade_id", "id", "currency"}
        }
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


def sectors_of(stocks: Dict[int, str]) -> Dict[int, Optional[str]]:
    """``{stock_id: sector}`` in the shape the engines expect."""
    return dict(stocks)


__all__ = [
    "BASE",
    "NO_FEES",
    "allocate",
    "backtest",
    "bar",
    "candidate",
    "candidate_body",
    "create_portfolio",
    "create_stock",
    "create_via_api",
    "flat_bars",
    "persisted_series",
    "persist_bars",
    "policy",
    "policy_body",
    "position_size",
    "position_valuation",
    "replace_tail",
    "risk_state",
    "store_price",
    "strip_volatile",
    "valuation",
    "buy_via_api",
]
