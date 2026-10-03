"""Deterministic helpers for the Phase 9 paper-trading tests.

Values are fixed, no provider or network is contacted, and every expected amount is derived
from the rules under test rather than copied from a run: a failure always points at the
engine, never at the fixture.
"""

from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from app.schemas.portfolio import OrderSide, OrderType, PortfolioStatus
from app.services.portfolio.models import (
    OrderIntent,
    PortfolioAccount,
    PositionState,
    TradingCostConfig,
)

#: Default simulated cost model used across the tests: 0.1% per side, no minimum, no slippage.
DEFAULT_COSTS = TradingCostConfig(
    commission_rate=Decimal("0.001"), min_fee=Decimal("0"), slippage_rate=Decimal("0")
)

#: A zero-cost model, used where a test asserts exact arithmetic without fees in the way.
NO_FEES = TradingCostConfig(
    commission_rate=Decimal("0"), min_fee=Decimal("0"), slippage_rate=Decimal("0")
)


def position_state(
    stock_id: int = 1,
    quantity: int = 10,
    average_entry_price: Decimal = Decimal("100"),
    *,
    entry_fees_total: Decimal = Decimal("0"),
    realized_pnl: Decimal = Decimal("0"),
    current_price: Optional[Decimal] = Decimal("100"),
) -> PositionState:
    """Build a position state with a consistent cost basis."""
    return PositionState(
        stock_id=stock_id,
        quantity=quantity,
        average_entry_price=average_entry_price,
        cost_basis=average_entry_price * Decimal(quantity),
        entry_fees_total=entry_fees_total,
        realized_pnl=realized_pnl,
        current_price=current_price,
    )


def account(
    *,
    portfolio_id: int = 1,
    status: PortfolioStatus = PortfolioStatus.ACTIVE,
    initial_capital: Decimal = Decimal("100000"),
    cash: Decimal = Decimal("100000"),
    realized_pnl: Decimal = Decimal("0"),
    position: Optional[PositionState] = None,
    total_market_value: Optional[Decimal] = None,
) -> PortfolioAccount:
    """Build a runtime account. ``total_market_value`` defaults to the position's value."""
    if total_market_value is None:
        total_market_value = position.market_value() if position else Decimal("0")
    return PortfolioAccount(
        portfolio_id=portfolio_id,
        name="Test Portfolio",
        status=status,
        initial_capital=initial_capital,
        cash=cash,
        realized_pnl=realized_pnl,
        total_market_value=total_market_value,
        position=position,
    )


def buy_intent(
    stock_id: int = 1,
    quantity: int = 10,
    price: Decimal = Decimal("100"),
    portfolio_id: int = 1,
    **kwargs,
) -> OrderIntent:
    """A simulated BUY intent."""
    return OrderIntent(
        portfolio_id=portfolio_id,
        stock_id=stock_id,
        side=OrderSide.BUY,
        quantity=quantity,
        requested_price=Decimal(price),
        order_type=OrderType.MARKET,
        **kwargs,
    )


def sell_intent(
    stock_id: int = 1,
    quantity: int = 10,
    price: Decimal = Decimal("100"),
    portfolio_id: int = 1,
    **kwargs,
) -> OrderIntent:
    """A simulated SELL intent."""
    return OrderIntent(
        portfolio_id=portfolio_id,
        stock_id=stock_id,
        side=OrderSide.SELL,
        quantity=quantity,
        requested_price=Decimal(price),
        order_type=OrderType.MARKET,
        **kwargs,
    )


def price_series(start: Decimal, step: Decimal, count: int) -> List[Decimal]:
    """A deterministic ascending price series, e.g. for precision sweeps."""
    return [start + step * Decimal(index) for index in range(count)]


def expected_fee(gross_value: Decimal, rate: Decimal = Decimal("0.001")) -> Decimal:
    """The fee the engine must charge for ``gross_value``, stated independently."""
    return (gross_value * rate).quantize(Decimal("0.01"))


def round_trip_expectations(
    quantity: int, entry_price: Decimal, exit_price: Decimal, rate: Decimal = Decimal("0.001")
) -> Tuple[Decimal, Decimal]:
    """``(cash_delta, net_realized_pnl)`` for a zero-fee round trip at the given rate."""
    entry_gross = (entry_price * Decimal(quantity)).quantize(Decimal("0.01"))
    exit_gross = (exit_price * Decimal(quantity)).quantize(Decimal("0.01"))
    entry_fee = expected_fee(entry_gross, rate)
    exit_fee = expected_fee(exit_gross, rate)
    cash_delta = quant(exit_gross - exit_fee - entry_gross - entry_fee)
    realized = quant((exit_price - entry_price) * Decimal(quantity) - exit_fee - entry_fee)
    return cash_delta, realized


def quant(value: Decimal) -> Decimal:
    """Round a money amount the same way the engine does."""
    return Decimal(value).quantize(Decimal("0.01"))


def prices_by_stock(entries: Dict[int, Decimal]) -> Dict[int, Decimal]:
    """Normalise a price mapping for the service layer."""
    return {stock_id: Decimal(price) for stock_id, price in entries.items()}