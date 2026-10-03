"""Shared helpers for the Phase 11 construction, rebalancing and cash-ledger tests.

Everything here is deterministic and offline: no provider is contacted, no network call is
made, and amounts are written as exact ``Decimal`` strings so a failure points at the code
under test rather than at floating-point noise.
"""

from decimal import Decimal
from typing import Optional

from app.models.company import Company
from app.models.stock import Stock
from app.schemas.portfolio import (
    PortfolioCreate,
    RiskLimitSettings,
    TradingCostSettings,
)
from app.services.portfolio.service import paper_trading_service
from tests.phase10_helpers import NO_FEES, buy, sell

BASE = "/api/v1/portfolios"


def create_stock(
    db_session, symbol: str = "TEST", *, sector: Optional[str] = None
) -> int:
    """Insert a stock master row and return its id.

    ``sector`` is attached through a company profile, because ``Stock`` itself has no sector —
    the concentration rules read ``Company.sector``, so a test that wants sector behaviour must
    create the profile too.
    """
    stock = Stock(
        symbol=symbol,
        company_name=f"{symbol} Limited",
        exchange="NSE",
        market="INDIA",
    )
    db_session.add(stock)
    db_session.flush()
    if sector is not None:
        db_session.add(Company(stock_id=stock.id, sector=sector, industry="Generic"))
    db_session.commit()
    db_session.refresh(stock)
    return stock.id


def allocation(**overrides) -> dict:
    """An allocation-config body. Values are ``Decimal`` and serialised as exact strings."""
    payload = {
        "sizing_method": "EQUAL_WEIGHT",
        "max_position_weight_pct": Decimal("40"),
        "cash_reserve_pct": Decimal("10"),
        "stop_loss_pct": Decimal("10"),
        "take_profit_pct": Decimal("20"),
        "rebalance_threshold_pct": Decimal("1"),
    }
    payload.update(overrides)
    return {key: str(value) for key, value in payload.items()}


def create(
    client,
    name="P11",
    capital="100000",
    *,
    fees=None,
    allocation_config: Optional[dict] = None,
    risk_limits: Optional[dict] = None,
) -> dict:
    """Create a portfolio through the API, defaulting to zero fees for exact arithmetic."""
    body = {
        "name": name,
        "initial_capital": capital,
        "trading_costs": {key: str(value) for key, value in (fees or NO_FEES).items()},
    }
    if allocation_config is not None:
        body["allocation_config"] = allocation_config
    if risk_limits is not None:
        body["risk_limits"] = risk_limits
    response = client.post(BASE, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def create_with(
    db_session,
    name="Test Portfolio",
    initial_capital="100000",
    *,
    allocation_config: Optional[dict] = None,
    risk_limits: Optional[dict] = None,
) -> int:
    """Create a portfolio through the service layer and return its id."""
    payload = PortfolioCreate(
        name=name,
        initial_capital=Decimal(initial_capital),
        trading_costs=TradingCostSettings(**NO_FEES),
        risk_limits=RiskLimitSettings(**(risk_limits or {})),
        allocation_config=allocation_config,
    )
    return paper_trading_service.create_portfolio(db_session, payload).id


def candidate(stock_id: int, price, **extra) -> dict:
    """A candidate entry for the decisions / rebalance endpoints."""
    return {"stock_id": stock_id, "price": str(price), **extra}


def prices(*pairs) -> list:
    """A ``prices`` body: ``[(stock_id, price), ...]``."""
    return [{"stock_id": stock_id, "price": str(price)} for stock_id, price in pairs]


def decisions(client, portfolio_id: int, body: dict):
    """POST the decisions endpoint and return the parsed response."""
    response = client.post(f"{BASE}/{portfolio_id}/decisions", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def rebalance(client, portfolio_id: int, body: dict):
    """POST the rebalance endpoint and return the parsed response."""
    response = client.post(f"{BASE}/{portfolio_id}/rebalance", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def transactions(client, portfolio_id: int, **params):
    """GET the transaction ledger and return the parsed response."""
    response = client.get(f"{BASE}/{portfolio_id}/transactions", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def risk_report(client, portfolio_id: int, *, prices=None) -> dict:
    """GET the portfolio risk report, optionally marking positions with ``stock_id=price``."""
    params = {"prices": prices} if prices else None
    response = client.get(f"{BASE}/{portfolio_id}/risk/report", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def portfolio(client, portfolio_id: int) -> dict:
    """The portfolio summary as returned by the API."""
    response = client.get(f"{BASE}/{portfolio_id}")
    assert response.status_code == 200, response.text
    return response.json()


__all__ = [
    "BASE",
    "NO_FEES",
    "allocation",
    "buy",
    "candidate",
    "create",
    "create_stock",
    "create_with",
    "decisions",
    "portfolio",
    "prices",
    "rebalance",
    "risk_report",
    "sell",
    "transactions",
]
