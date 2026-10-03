"""Shared helpers for the Phase 10 tests.

Everything here is deterministic and offline: no provider is contacted, no network call is
made, and amounts are written as exact ``Decimal`` strings so a failure points at the code
under test rather than at floating-point noise.
"""

from decimal import Decimal
from typing import Optional

from app.models.stock import Stock
from app.schemas.portfolio import (
    OrderSide,
    PortfolioCreate,
    RiskLimitSettings,
    TradingCostSettings,
)
from app.services.portfolio.service import paper_trading_service

#: Zero-cost model, so a test can assert exact arithmetic without fees in the way.
NO_FEES = {
    "commission_rate": Decimal("0"),
    "min_fee": Decimal("0"),
    "slippage_rate": Decimal("0"),
}


def create_stock(db_session, symbol: str = "TEST", stock_id: Optional[int] = None) -> int:
    """Insert a stock master row and return its id."""
    stock = Stock(
        symbol=symbol,
        company_name=f"{symbol} Limited",
        exchange="NSE",
        market="INDIA",
    )
    db_session.add(stock)
    db_session.commit()
    db_session.refresh(stock)
    return stock.id


def create_portfolio(
    db_session,
    name: str = "Test Portfolio",
    initial_capital="100000",
    *,
    fees=None,
    risk_limits: Optional[dict] = None,
    description: Optional[str] = None,
) -> int:
    """Create a portfolio through the service layer and return its id."""
    payload = PortfolioCreate(
        name=name,
        description=description,
        initial_capital=Decimal(initial_capital),
        trading_costs=TradingCostSettings(**(fees if fees is not None else NO_FEES)),
        risk_limits=RiskLimitSettings(**(risk_limits or {})),
    )
    portfolio = paper_trading_service.create_portfolio(db_session, payload)
    return portfolio.id


def order_payload(
    stock_id: int,
    side: OrderSide,
    quantity: int,
    price,
    **extra,
) -> dict:
    """A JSON order body for the API tests."""
    return {
        "stock_id": stock_id,
        "side": side.value if isinstance(side, OrderSide) else side,
        "quantity": quantity,
        "requested_price": str(price),
        **extra,
    }


def buy(client, portfolio_id: int, stock_id: int, quantity: int, price, **extra):
    """Place a BUY and return the parsed response body."""
    response = client.post(
        f"/api/v1/portfolios/{portfolio_id}/orders",
        json=order_payload(stock_id, OrderSide.BUY, quantity, price, **extra),
    )
    assert response.status_code == 201, response.text
    return response.json()


def sell(client, portfolio_id: int, stock_id: int, quantity: int, price, **extra):
    """Place a SELL and return the parsed response body."""
    response = client.post(
        f"/api/v1/portfolios/{portfolio_id}/orders",
        json=order_payload(stock_id, OrderSide.SELL, quantity, price, **extra),
    )
    assert response.status_code == 201, response.text
    return response.json()


def portfolio(client, portfolio_id: int) -> dict:
    """The portfolio summary as returned by the API."""
    response = client.get(f"/api/v1/portfolios/{portfolio_id}")
    assert response.status_code == 200, response.text
    return response.json()


def performance(client, portfolio_id: int) -> dict:
    """The performance report as returned by the API."""
    response = client.get(f"/api/v1/portfolios/{portfolio_id}/performance")
    assert response.status_code == 200, response.text
    return response.json()


def positions(client, portfolio_id: int) -> list:
    """The open positions as returned by the API."""
    response = client.get(f"/api/v1/portfolios/{portfolio_id}/positions")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def insert_prices(db_session, stock_id: int, bars):
    """Insert stored OHLCV rows.

    ``bars`` is a sequence of ``(date, open, high, low, close)`` tuples. Prices are written
    as exact ``Numeric`` values so the stored data is exactly what a test states.
    """
    from app.models.price import StockPrice

    for bar_date, open_price, high, low, close in bars:
        db_session.add(
            StockPrice(
                stock_id=stock_id,
                price_date=bar_date,
                open_price=Decimal(str(open_price)),
                high_price=Decimal(str(high)),
                low_price=Decimal(str(low)),
                close_price=Decimal(str(close)),
                volume=1000,
            )
        )
    db_session.commit()