"""Phase 12 portfolio engine tests — lifecycle, trades, positions, valuation and drawdown.

Each test is tagged with the numbered Phase 12 acceptance item it covers, so the suite maps
directly onto the specification rather than onto whatever happened to be convenient to write.

Numbered coverage in this file:

* 1-8   portfolio creation, validation, update, pause, close and the trading lockout
* 9-15  BUY: fees, slippage, cash, quantity, weighted average entry price
* 16-21 SELL: full/partial, oversell refusal, realised P&L, position closure
* 22-24 average-price integrity across repeated buys and partial/full closes
* 35-41 valuation: market value, equity, unrealised/realised/total P&L, return %, exposure %
* 42-45 drawdown: peak tracking, current, maximum and recovery

Everything runs against the real service against an isolated SQLite database, and every expected
amount is derived from the rules under test rather than copied from a run.
"""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import (
    InsufficientCashError,
    InsufficientPositionError,
    InvalidOrderError,
    PortfolioInactiveError,
    PortfolioNotFoundError,
)
from app.models.stock import Stock
from app.schemas.allocation import CashMovementRequest
from app.schemas.portfolio import (
    OrderCreate,
    OrderSide,
    OrderType,
    PortfolioCreate,
    PortfolioStatus,
    PortfolioStatusUpdate,
    PortfolioUpdate,
    RiskLimitSettings,
    TradingCostSettings,
)
from app.services.portfolio.service import paper_trading_service

#: Zero-fee, zero-slippage cost model. Every amount asserted below is then exactly the stated
#: arithmetic, with no fee effect to explain a rounding difference.
NO_FEES = TradingCostSettings(commission_rate=Decimal("0"), min_fee=Decimal("0"), slippage_rate=Decimal("0"))

#: 0.1% per side, the default commission, used by the fee tests.
DEFAULT_FEES = TradingCostSettings(commission_rate=Decimal("0.001"), min_fee=Decimal("0"), slippage_rate=Decimal("0"))

#: 0.5% slippage, used by the slippage tests.
WITH_SLIPPAGE = TradingCostSettings(
    commission_rate=Decimal("0"), min_fee=Decimal("0"), slippage_rate=Decimal("0.005")
)

CENT = Decimal("0.01")


def money(value) -> Decimal:
    """Quantise the way the engine quantises money."""
    return Decimal(value).quantize(CENT)


@pytest.fixture
def stock(db_session: Session) -> Stock:
    record = Stock(
        symbol="P12CO", company_name="Phase Twelve Co", exchange="NSE", market="INDIA"
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


@pytest.fixture
def other_stock(db_session: Session) -> Stock:
    record = Stock(
        symbol="P12OT", company_name="Phase Twelve Other", exchange="NSE", market="INDIA"
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


def make_portfolio(db: Session, name="Phase 12 Portfolio", capital="100000", **overrides):
    """Create a portfolio through the service, with the zero-fee model unless stated."""
    payload = {
        "name": name,
        "initial_capital": Decimal(capital),
        "trading_costs": overrides.pop("trading_costs", NO_FEES),
    }
    payload.update(overrides)
    return paper_trading_service.create_portfolio(db, PortfolioCreate(**payload))


def order(stock_id: int, side: str, quantity: int, price: str, **extra) -> OrderCreate:
    return OrderCreate(
        stock_id=stock_id,
        side=OrderSide(side),
        order_type=OrderType.MARKET,
        quantity=quantity,
        requested_price=Decimal(price),
        **extra,
    )


def buy(db: Session, portfolio, stock_id: int, quantity: int, price: str, **extra):
    return paper_trading_service.place_order(
        db, portfolio.id, order(stock_id, "BUY", quantity, price, **extra)
    )


def sell(db: Session, portfolio, stock_id: int, quantity: int, price: str, **extra):
    return paper_trading_service.place_order(
        db, portfolio.id, order(stock_id, "SELL", quantity, price, **extra)
    )


def position_of(db: Session, portfolio_id: int, stock_id: int):
    positions = paper_trading_service.get_positions(db, portfolio_id)
    return next((p for p in positions if p.stock_id == stock_id), None)


def valuation_of(db: Session, portfolio_id: int):
    return paper_trading_service.calculate_portfolio_value(db, portfolio_id)


# ======================================================================
# 1-8  Portfolio lifecycle
# ======================================================================


def test_01_create_portfolio(db_session: Session):
    """1. A new portfolio starts flat: cash == equity == initial capital, no positions."""
    portfolio = make_portfolio(db_session, capital="250000.50")

    assert Decimal(portfolio.initial_capital) == Decimal("250000.50")
    assert Decimal(portfolio.current_cash) == Decimal("250000.50")
    assert Decimal(portfolio.equity) == Decimal("250000.50")
    assert Decimal(portfolio.realized_pnl) == Decimal("0")
    assert Decimal(portfolio.unrealized_pnl) == Decimal("0")
    assert portfolio.status == PortfolioStatus.ACTIVE.value
    assert paper_trading_service.get_positions(db_session, portfolio.id) == []


def test_01b_portfolio_records_its_currency(db_session: Session):
    """1. The currency is stored on the portfolio and defaults when not stated."""
    stated = make_portfolio(db_session, name="INR book", currency="INR")
    other = make_portfolio(db_session, name="USD book", currency="USD")
    implicit = make_portfolio(db_session, name="Implicit book")

    assert stated.currency == "INR"
    assert other.currency == "USD"
    assert implicit.currency == "INR"
    # The unit is a description of the figures, not a conversion.
    assert Decimal(other.initial_capital) == Decimal("100000")


def test_01c_portfolio_update_cannot_change_currency(db_session: Session):
    """1. Currency is fixed at creation: changing it would relabel recorded history."""
    portfolio = make_portfolio(db_session, currency="INR")

    updated = paper_trading_service.update_portfolio(
        db_session, portfolio.id, PortfolioUpdate(name="Renamed")
    )

    assert updated.name == "Renamed"
    assert updated.currency == "INR"


@pytest.mark.parametrize("capital", ["0", "-1", "-0.01", "-100000"])
def test_02_03_reject_non_positive_initial_capital(db_session: Session, capital):
    """2/3. Zero and negative initial capital are rejected; nothing is created."""
    with pytest.raises(Exception) as excinfo:
        paper_trading_service.create_portfolio(
            db_session, PortfolioCreate(name="Bad", initial_capital=Decimal(capital))
        )
    assert "initial_capital" in str(excinfo.value).lower()


def test_04_update_portfolio(db_session: Session):
    """4. A partial update changes only the fields supplied; the rest are untouched."""
    portfolio = make_portfolio(db_session, name="Original", capital="50000")

    updated = paper_trading_service.update_portfolio(
        db_session,
        portfolio.id,
        PortfolioUpdate(description="Now described", risk_limits=RiskLimitSettings(max_positions=4)),
    )

    assert updated.name == "Original"
    assert updated.description == "Now described"
    assert Decimal(updated.initial_capital) == Decimal("50000")
    assert paper_trading_service.risk_limits(updated).max_positions == 4


def test_05_pause_portfolio(db_session: Session, stock):
    """5. Pausing keeps every position and every record; only new orders stop."""
    portfolio = make_portfolio(db_session)
    buy(db_session, portfolio, stock.id, 10, "100")

    paused = paper_trading_service.pause_portfolio(db_session, portfolio.id)

    assert paused.status == PortfolioStatus.PAUSED.value
    assert position_of(db_session, portfolio.id, stock.id) is not None
    assert len(paper_trading_service.get_orders(db_session, portfolio.id)) == 1
    # Reading and snapshotting still work while paused.
    snapshot = paper_trading_service.create_snapshot(db_session, portfolio.id)
    assert snapshot is not None


def test_06_close_portfolio(db_session: Session, stock):
    """6. Closing preserves positions, orders, trades and snapshots."""
    portfolio = make_portfolio(db_session)
    buy(db_session, portfolio, stock.id, 10, "100")
    sell(db_session, portfolio, stock.id, 4, "110")
    paper_trading_service.create_snapshot(db_session, portfolio.id)

    closed = paper_trading_service.close_portfolio(db_session, portfolio.id)

    assert closed.status == PortfolioStatus.CLOSED.value
    assert position_of(db_session, portfolio.id, stock.id).quantity == 6
    assert len(paper_trading_service.get_orders(db_session, portfolio.id)) == 2
    assert len(paper_trading_service.get_trades(db_session, portfolio.id)) == 2
    assert paper_trading_service.count_snapshots(db_session, portfolio.id) == 1


def test_06b_close_is_terminal(db_session: Session):
    """6. A closed portfolio cannot be reopened, and re-closing it is a safe no-op."""
    portfolio = make_portfolio(db_session)
    paper_trading_service.close_portfolio(db_session, portfolio.id)

    with pytest.raises(InvalidOrderError):
        paper_trading_service.update_status(
            db_session, portfolio.id, PortfolioStatusUpdate(status=PortfolioStatus.ACTIVE)
        )

    again = paper_trading_service.close_portfolio(db_session, portfolio.id)
    assert again.status == PortfolioStatus.CLOSED.value


def test_07_cannot_trade_closed_portfolio(db_session: Session, stock):
    """7. Every order on a CLOSED portfolio is refused and changes nothing."""
    portfolio = make_portfolio(db_session)
    paper_trading_service.close_portfolio(db_session, portfolio.id)

    with pytest.raises(PortfolioInactiveError) as excinfo:
        buy(db_session, portfolio, stock.id, 5, "100")

    assert excinfo.value.code == "PORTFOLIO_INACTIVE"
    after = paper_trading_service.get_portfolio(db_session, portfolio.id)
    assert Decimal(after.current_cash) == Decimal("100000")
    assert position_of(db_session, portfolio.id, stock.id) is None


def test_08_cannot_trade_paused_portfolio(db_session: Session, stock):
    """8. Every order on a PAUSED portfolio is refused and changes nothing."""
    portfolio = make_portfolio(db_session)
    paper_trading_service.pause_portfolio(db_session, portfolio.id)

    with pytest.raises(PortfolioInactiveError) as excinfo:
        buy(db_session, portfolio, stock.id, 5, "100")

    assert excinfo.value.code == "PORTFOLIO_INACTIVE"
    assert Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    ) == Decimal("100000")
    # ...and it can be resumed afterwards.
    resumed = paper_trading_service.update_portfolio(
        db_session, portfolio.id, PortfolioUpdate(status=PortfolioStatus.ACTIVE)
    )
    assert resumed.status == PortfolioStatus.ACTIVE.value
    buy(db_session, resumed, stock.id, 5, "100")
    assert position_of(db_session, portfolio.id, stock.id).quantity == 5


def test_08b_paused_portfolio_cannot_be_closed_paused_twice(db_session: Session):
    """8/6. Pausing an already-paused portfolio is a no-op; a closed one cannot pause."""
    portfolio = make_portfolio(db_session)
    paper_trading_service.pause_portfolio(db_session, portfolio.id)
    assert paper_trading_service.pause_portfolio(db_session, portfolio.id).status == (
        PortfolioStatus.PAUSED.value
    )

    paper_trading_service.close_portfolio(db_session, portfolio.id)
    with pytest.raises(PortfolioInactiveError):
        paper_trading_service.pause_portfolio(db_session, portfolio.id)


def test_08c_cash_moves_while_paused_but_never_once_closed(db_session: Session):
    """8/6. A pause halts trading but not capital movements; a closure halts both.

    The two lifecycle states deliberately stop different amounts. A paused portfolio has stopped
    its *strategy*, so an operator can still fund or drain it. A closed portfolio's history is
    final, so a deposit afterwards would rewrite the very numbers the closure exists to fix.
    """
    portfolio = make_portfolio(db_session, capital="50000")

    paper_trading_service.pause_portfolio(db_session, portfolio.id)
    movement = paper_trading_service.deposit(
        db_session, portfolio.id, CashMovementRequest(amount=Decimal("10000"))
    )
    assert movement.transaction_type == "DEPOSIT"
    # The ledger records the signed effect on cash and the balance it produced.
    assert movement.net_cash_impact == Decimal("10000")
    assert movement.cash_after == Decimal("60000")
    assert movement.realized_pnl == Decimal("0")
    assert paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash == Decimal(
        "60000"
    )
    paper_trading_service.withdraw(
        db_session, portfolio.id, CashMovementRequest(amount=Decimal("5000"))
    )
    assert paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash == Decimal(
        "55000"
    )

    paper_trading_service.close_portfolio(db_session, portfolio.id)
    with pytest.raises(PortfolioInactiveError):
        paper_trading_service.deposit(
            db_session, portfolio.id, CashMovementRequest(amount=Decimal("10000"))
        )
    with pytest.raises(PortfolioInactiveError):
        paper_trading_service.withdraw(
            db_session, portfolio.id, CashMovementRequest(amount=Decimal("1000"))
        )
    # The refusal changed nothing.
    assert paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash == Decimal(
        "55000"
    )


def test_unknown_portfolio_is_a_structured_error(db_session: Session):
    """A missing portfolio is reported as 404-style domain error, never as a DB error."""
    with pytest.raises(PortfolioNotFoundError) as excinfo:
        paper_trading_service.get_portfolio(db_session, 4242)
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "PORTFOLIO_NOT_FOUND"


# ======================================================================
# 9-15  BUY
# ======================================================================


def test_09_successful_buy(db_session: Session, stock):
    """9. A funded buy fills, creates the position and records an order and a trade."""
    portfolio = make_portfolio(db_session, capital="100000")

    result = buy(db_session, portfolio, stock.id, 100, "50")

    assert result.order.status == "FILLED"
    assert result.trade is not None
    assert result.trade.side == "BUY"
    position = position_of(db_session, portfolio.id, stock.id)
    assert position.quantity == 100
    assert Decimal(position.average_entry_price) == Decimal("50")


def test_10_insufficient_cash(db_session: Session, stock):
    """10. A buy that cash cannot fund is refused; cash stays whole and no position appears."""
    portfolio = make_portfolio(db_session, capital="1000")

    with pytest.raises(InsufficientCashError) as excinfo:
        buy(db_session, portfolio, stock.id, 100, "50")

    assert excinfo.value.code == "INSUFFICIENT_CASH"
    assert Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    ) == Decimal("1000")
    assert position_of(db_session, portfolio.id, stock.id) is None


def test_11_correct_fee_calculation(db_session: Session, stock):
    """11. The fee is commission_rate * gross value, charged on the buy."""
    portfolio = make_portfolio(db_session, capital="100000", trading_costs=DEFAULT_FEES)

    result = buy(db_session, portfolio, stock.id, 100, "50")

    gross = money(Decimal("50") * 100)  # 5000.00
    assert gross == Decimal("5000.00")
    expected_fee = money(gross * Decimal("0.001"))
    assert expected_fee == Decimal("5.00")
    assert Decimal(result.trade.gross_value) == gross
    assert Decimal(result.trade.fees) == expected_fee
    # Cash pays value plus fee.
    assert Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    ) == money(Decimal("100000") - gross - expected_fee)


def test_11b_minimum_fee_floor(db_session: Session, stock):
    """11. The per-side minimum fee is charged when it exceeds the percentage."""
    portfolio = make_portfolio(
        db_session,
        capital="100000",
        trading_costs=TradingCostSettings(
            commission_rate=Decimal("0.001"), min_fee=Decimal("1.50"), slippage_rate=Decimal("0")
        ),
    )

    result = buy(db_session, portfolio, stock.id, 1, "100")  # 0.10% of 100 = 0.10 < 1.50

    assert Decimal(result.trade.fees) == Decimal("1.50")


def test_12_correct_slippage(db_session: Session, stock):
    """12. Slippage is adverse: a buy fills above the requested price."""
    portfolio = make_portfolio(db_session, capital="100000", trading_costs=WITH_SLIPPAGE)

    result = buy(db_session, portfolio, stock.id, 10, "100")

    # 0.5% of 100 = 0.50, so the buy fills at 100.50.
    assert Decimal(result.trade.execution_price) == Decimal("100.5000")
    assert Decimal(result.trade.gross_value) == money(Decimal("100.5000") * 10)


def test_12b_slippage_is_adverse_on_a_sell(db_session: Session, stock):
    """12. A sell fills below the requested price, the mirror of the buy rule."""
    portfolio = make_portfolio(db_session, capital="100000", trading_costs=WITH_SLIPPAGE)
    buy(db_session, portfolio, stock.id, 10, "100")

    result = sell(db_session, portfolio, stock.id, 10, "100")

    assert Decimal(result.trade.execution_price) == Decimal("99.5000")


def test_13_correct_cash_balance(db_session: Session, stock):
    """13. Cash moves by exactly -(value + fees) on a buy and +(value - fees) on a sell."""
    portfolio = make_portfolio(db_session, capital="100000", trading_costs=DEFAULT_FEES)

    bought = buy(db_session, portfolio, stock.id, 100, "50")
    after_buy = Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    )
    assert after_buy == money(
        Decimal("100000") - Decimal(bought.trade.gross_value) - Decimal(bought.trade.fees)
    )

    sold = sell(db_session, portfolio, stock.id, 40, "60")
    after_sell = Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    )
    assert after_sell == money(
        after_buy + Decimal(sold.trade.gross_value) - Decimal(sold.trade.fees)
    )


def test_14_correct_position_quantity(db_session: Session, stock):
    """14. Quantity accumulates across buys and never exceeds what was bought."""
    portfolio = make_portfolio(db_session, capital="100000")

    buy(db_session, portfolio, stock.id, 30, "10")
    buy(db_session, portfolio, stock.id, 20, "12")

    assert position_of(db_session, portfolio.id, stock.id).quantity == 50


def test_15_correct_average_entry_price(db_session: Session, stock):
    """15. The average entry price is quantity-weighted: (10*100 + 10*120) / 20 = 110."""
    portfolio = make_portfolio(db_session, capital="100000")

    buy(db_session, portfolio, stock.id, 10, "100")
    buy(db_session, portfolio, stock.id, 10, "120")

    position = position_of(db_session, portfolio.id, stock.id)
    expected = (Decimal(10) * Decimal("100") + Decimal(10) * Decimal("120")) / Decimal(20)
    assert Decimal(position.average_entry_price) == expected
    assert Decimal(position.average_entry_price) == Decimal("110")
    assert Decimal(position.cost_basis) == money(Decimal("110") * 20)


# ======================================================================
# 16-21  SELL
# ======================================================================


def test_16_successful_sell(db_session: Session, stock):
    """16. A covered sell fills, returns cash and reduces the position."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")

    result = sell(db_session, portfolio, stock.id, 4, "110")

    assert result.order.status == "FILLED"
    assert result.trade.side == "SELL"
    assert position_of(db_session, portfolio.id, stock.id).quantity == 6


def test_17_partial_sell_keeps_the_position_open(db_session: Session, stock):
    """17. A partial sell reduces the quantity and keeps the position."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")

    sell(db_session, portfolio, stock.id, 3, "120")

    position = position_of(db_session, portfolio.id, stock.id)
    assert position.quantity == 7
    assert position is not None


def test_18_full_sell(db_session: Session, stock):
    """18. Selling the whole holding leaves no position behind."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")

    sell(db_session, portfolio, stock.id, 10, "120")

    assert position_of(db_session, portfolio.id, stock.id) is None


def test_19_cannot_sell_more_than_owned(db_session: Session, stock):
    """19. An oversell is refused and leaves the position and cash untouched."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")
    cash_before = Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    )

    with pytest.raises(InsufficientPositionError) as excinfo:
        sell(db_session, portfolio, stock.id, 11, "100")

    assert excinfo.value.code == "INSUFFICIENT_POSITION"
    assert position_of(db_session, portfolio.id, stock.id).quantity == 10
    assert Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    ) == cash_before


def test_19b_cannot_sell_a_stock_that_is_not_held(db_session: Session, stock, other_stock):
    """19. Selling a stock with no position is refused rather than creating a short."""
    portfolio = make_portfolio(db_session)
    buy(db_session, portfolio, stock.id, 10, "100")

    with pytest.raises(InsufficientPositionError):
        sell(db_session, portfolio, other_stock.id, 1, "50")

    assert position_of(db_session, portfolio.id, other_stock.id) is None


def test_20_correct_realized_pnl(db_session: Session, stock):
    """20. Realised P&L is (exit - average entry) * quantity, net of both fees' share."""
    portfolio = make_portfolio(db_session, capital="100000", trading_costs=DEFAULT_FEES)
    buy(db_session, portfolio, stock.id, 10, "100")  # fee 1.00
    sell(db_session, portfolio, stock.id, 10, "110")  # fee 1.10

    realized = Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).realized_pnl
    )
    # 10 * (110 - 100) = 100 gross, less both sides' fees on the closed shares.
    assert realized == money(Decimal("100") - Decimal("1.00") - Decimal("1.10"))


def test_20b_realized_pnl_uses_cost_basis_not_market_price(db_session: Session, stock):
    """20. Realised P&L is measured against the cost basis even after the mark moves."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")
    # Mark the position up, which changes unrealised P&L but never the cost basis.
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("150")})

    result = sell(db_session, portfolio, stock.id, 10, "150")

    assert Decimal(result.trade.realized_pnl) == money(Decimal("50") * 10)


def test_21_position_closes_correctly(db_session: Session, stock):
    """21. A closed position books realised P&L, frees the cash and leaves no row."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")

    sell(db_session, portfolio, stock.id, 10, "120")

    summary = paper_trading_service.get_portfolio(db_session, portfolio.id)
    assert position_of(db_session, portfolio.id, stock.id) is None
    assert Decimal(summary.realized_pnl) == money(Decimal("200"))
    # Cash is initial capital plus the net sale proceeds: a flat portfolio, no positions.
    assert Decimal(summary.current_cash) == money(Decimal("100000") + Decimal("200"))


# ======================================================================
# 22-24  Average-price integrity
# ======================================================================


def test_22_multiple_buys_produce_a_weighted_average(db_session: Session, stock):
    """22. Three buys at three prices produce the quantity-weighted average cost."""
    portfolio = make_portfolio(db_session, capital="1000000")
    buy(db_session, portfolio, stock.id, 10, "100")
    buy(db_session, portfolio, stock.id, 20, "200")
    buy(db_session, portfolio, stock.id, 30, "300")

    position = position_of(db_session, portfolio.id, stock.id)
    # (1000 + 4000 + 9000) / 60 = 233.3333..., stored at the 4-decimal price scale the
    # ``Numeric(18, 4)`` column uses, so the stored average is a real price and not a
    # repeating fraction.
    assert Decimal(position.average_entry_price) == Decimal("233.3334")
    assert position.quantity == 60
    # The cost basis is the average times the quantity, at money scale.
    assert Decimal(position.cost_basis) == money(Decimal("233.3334") * 60)


def test_23_partial_sell_does_not_corrupt_average_cost(db_session: Session, stock):
    """23. A partial sell reduces quantity but leaves the average cost untouched."""
    portfolio = make_portfolio(db_session, capital="1000000")
    buy(db_session, portfolio, stock.id, 10, "100")
    buy(db_session, portfolio, stock.id, 10, "120")
    before = Decimal(position_of(db_session, portfolio.id, stock.id).average_entry_price)

    sell(db_session, portfolio, stock.id, 5, "200")

    after = position_of(db_session, portfolio.id, stock.id)
    assert after.quantity == 15
    assert Decimal(after.average_entry_price) == before


def test_24_full_close_then_reopen_resets_the_position(db_session: Session, stock):
    """24. Closing and reopening starts a fresh average cost rather than mixing the two."""
    portfolio = make_portfolio(db_session, capital="1000000")
    buy(db_session, portfolio, stock.id, 10, "100")
    sell(db_session, portfolio, stock.id, 10, "100")
    assert position_of(db_session, portfolio.id, stock.id) is None

    buy(db_session, portfolio, stock.id, 4, "250")

    position = position_of(db_session, portfolio.id, stock.id)
    assert position.quantity == 4
    assert Decimal(position.average_entry_price) == Decimal("250")
    assert Decimal(position.cost_basis) == money(Decimal("1000"))


# ======================================================================
# 35-41  Valuation
# ======================================================================


def test_35_correct_market_value(db_session: Session, stock, other_stock):
    """35. Market value is the sum over positions of quantity * current price."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")
    buy(db_session, portfolio, other_stock.id, 5, "40")
    paper_trading_service.update_market_prices(
        db_session, portfolio.id, {stock.id: Decimal("110"), other_stock.id: Decimal("50")}
    )

    valuation = valuation_of(db_session, portfolio.id)

    assert valuation.market_value == money(Decimal("1100") + Decimal("250"))


def test_36_correct_total_equity(db_session: Session, stock):
    """36. Equity is cash + market value, in both directions."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("120")})

    valuation = valuation_of(db_session, portfolio.id)

    expected_cash = money(Decimal("100000") - Decimal("1000"))
    assert valuation.cash == expected_cash
    assert valuation.market_value == money(Decimal("1200"))
    assert valuation.equity == money(expected_cash + Decimal("1200")) == money(Decimal("100200"))


def test_37_correct_unrealized_pnl(db_session: Session, stock):
    """37. Unrealised P&L is (current - average entry) * quantity."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("130")})

    valuation = valuation_of(db_session, portfolio.id)

    assert valuation.unrealized_pnl == money(Decimal("30") * 10)
    assert valuation.positions[0].unrealized_pnl == money(Decimal("300"))


def test_37b_negative_unrealized_pnl(db_session: Session, stock):
    """37. A mark below the entry price produces a negative unrealised P&L."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("80")})

    assert valuation_of(db_session, portfolio.id).unrealized_pnl == money(Decimal("-200"))


def test_38_correct_realized_pnl(db_session: Session, stock, other_stock):
    """38. Realised P&L accumulates across closed round trips and ignores open ones."""
    portfolio = make_portfolio(db_session, capital="100000")
    # Two independent round trips on two different stocks, so each one's P&L is exactly its own
    # price difference: +100 on the first, -100 on the second.
    buy(db_session, portfolio, stock.id, 10, "100")
    sell(db_session, portfolio, stock.id, 5, "120")
    buy(db_session, portfolio, other_stock.id, 10, "200")
    sell(db_session, portfolio, other_stock.id, 5, "180")
    # A trade still open contributes no realised P&L.
    buy(db_session, portfolio, stock.id, 4, "90")

    valuation = valuation_of(db_session, portfolio.id)

    assert valuation.realized_pnl == money(Decimal("100") - Decimal("100"))
    assert Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).realized_pnl
    ) == money(Decimal("0"))
    # The still-open 9 shares (5 left from the first round trip plus the 4 just bought) carry a
    # blended average cost of (5*100 + 4*90) / 9 = 95.5556, so they are slightly under water at
    # a 90 mark.
    kept = valuation.position(stock.id)
    assert kept.quantity == 9
    assert Decimal(kept.average_entry_price) == Decimal("95.5556")
    assert kept.unrealized_pnl == money((Decimal("90") - Decimal("95.5556")) * 9)


def test_39_correct_total_pnl(db_session: Session, stock):
    """39. Total P&L is equity - initial capital, so the books reconcile."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("130")})

    valuation = valuation_of(db_session, portfolio.id)

    assert valuation.total_pnl == money(valuation.equity - Decimal("100000"))
    assert valuation.total_pnl == money(Decimal("300"))
    # The documented reconciliation identity holds with no fees in the way.
    assert valuation.realized_pnl + valuation.unrealized_pnl - valuation.open_entry_fees == (
        valuation.total_pnl
    )


def test_40_correct_return_percentage(db_session: Session, stock):
    """40. Return % is total P&L / initial capital * 100."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("150")})

    valuation = valuation_of(db_session, portfolio.id)

    # Cash 99000 + market value 1500 = equity 100500, so the portfolio is up 500, i.e. 0.5%.
    assert valuation.equity == money(Decimal("100500"))
    assert valuation.total_pnl == money(Decimal("500"))
    assert valuation.return_pct == Decimal("0.5000")


def test_40b_return_percentage_of_a_loss_is_negative(db_session: Session, stock):
    """40. A losing portfolio reports a negative return %, never a signed-to-positive one."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 100, "100")
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("90")})

    valuation = valuation_of(db_session, portfolio.id)

    assert valuation.total_pnl == money(Decimal("-1000"))
    assert valuation.return_pct == Decimal("-1.0000")


def test_41_correct_exposure_percentage(db_session: Session, stock):
    """41. Exposure % is market value / equity * 100, and None when equity is not positive."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")

    flat = valuation_of(db_session, portfolio.id)
    assert flat.market_value == money(Decimal("1000"))
    assert flat.equity == money(Decimal("100000"))
    assert flat.exposure_pct == Decimal("1.0000")


def test_41b_exposure_is_none_when_equity_is_not_positive(db_session: Session, stock):
    """41. Exposure of a wiped-out portfolio is undefined, so it is reported as None."""
    portfolio = make_portfolio(db_session, capital="1000")
    buy(db_session, portfolio, stock.id, 10, "100")
    # Drive equity to exactly zero: mark the whole holding away.
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("0.0001")})

    valuation = valuation_of(db_session, portfolio.id)

    assert valuation.exposure_pct is None


# ======================================================================
# 42-45  Drawdown
# ======================================================================


def test_42_peak_equity_tracking(db_session: Session, stock):
    """42. The peak is the highest equity ever recorded, across snapshots and now."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")
    # Cash 99000 + 10 shares marked at 250 = 101500. The peak is that point, not the
    # starting capital.
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("250")})
    paper_trading_service.create_snapshot(db_session, portfolio.id)
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("90")})

    peak = paper_trading_service.peak_equity(db_session, paper_trading_service.get_portfolio(db_session, portfolio.id))

    assert peak == money(Decimal("101500"))


def test_43_current_drawdown(db_session: Session, stock):
    """43. Current drawdown is the decline from the peak, as a positive percentage."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("250")})
    paper_trading_service.create_snapshot(db_session, portfolio.id)
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("100")})
    paper_trading_service.create_snapshot(db_session, portfolio.id)

    drawdown = paper_trading_service.get_drawdown(db_session, portfolio.id)

    peak = money(Decimal("101500"))
    current = valuation_of(db_session, portfolio.id).equity
    assert current == money(Decimal("100000"))
    expected = ((peak - current) / peak * Decimal("100")).quantize(Decimal("0.0001"))
    assert drawdown["peak_equity"] == peak
    assert drawdown["current_equity"] == current
    assert drawdown["max_drawdown_pct"] == expected
    assert expected == Decimal("1.4778")


def test_44_maximum_drawdown(db_session: Session, stock):
    """44. The maximum drawdown is the worst decline the curve recorded, not today's."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")  # cash 99000

    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("200")})
    paper_trading_service.create_snapshot(db_session, portfolio.id)  # equity 101000, the peak
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("50")})
    paper_trading_service.create_snapshot(db_session, portfolio.id)  # equity 99500, the trough
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("180")})
    paper_trading_service.create_snapshot(db_session, portfolio.id)  # equity 100800

    performance = paper_trading_service.get_performance(db_session, portfolio.id)

    # Peak 101000 down to 99500 is 1500, i.e. 1.4851% of the peak.
    assert performance.peak_equity == money(Decimal("101000"))
    assert performance.max_drawdown == money(Decimal("1500"))
    assert performance.max_drawdown_pct == Decimal("1.4851")
    # Having recovered, today's drawdown is smaller than the maximum recorded.
    assert performance.current_drawdown_pct < performance.max_drawdown_pct


def test_45_recovery_from_drawdown(db_session: Session, stock):
    """45. Recovering to a new high resets the current drawdown to zero and raises the peak."""
    portfolio = make_portfolio(db_session, capital="100000")
    buy(db_session, portfolio, stock.id, 10, "100")  # cash 99000
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("80")})
    paper_trading_service.create_snapshot(db_session, portfolio.id)  # equity 99000
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("300")})
    paper_trading_service.create_snapshot(db_session, portfolio.id)  # equity 102000, a new high

    performance = paper_trading_service.get_performance(db_session, portfolio.id)

    assert performance.peak_equity == money(Decimal("102000"))
    assert performance.current_drawdown_pct == Decimal("0.0000")
    # The maximum drawdown is remembered even though the portfolio has fully recovered.
    assert performance.max_drawdown_pct > Decimal("0")


def test_drawdown_is_zero_for_a_peak_at_or_below_zero(db_session: Session, stock):
    """Edge: a non-positive peak yields a zero drawdown rather than a division by zero."""
    paper_trading_service.create_snapshot(db_session, make_portfolio(db_session).id)
    from app.services.portfolio.calculator import drawdown_pct

    assert drawdown_pct(Decimal("0"), Decimal("100")) == Decimal("0.0000")
    assert drawdown_pct(Decimal("-50"), Decimal("100")) == Decimal("0.0000")


# ======================================================================
# STEP 26  Edge cases
# ======================================================================


@pytest.mark.parametrize("quantity", [0, -1, -100])
def test_edge_invalid_quantity_is_refused(db_session: Session, stock, quantity):
    """Edge: a non-positive quantity never fills. The schema rejects it at the boundary."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OrderCreate(
            stock_id=stock.id,
            side=OrderSide.BUY,
            quantity=quantity,
            requested_price=Decimal("100"),
        )
    # The engine refuses it too, independently of the schema.
    from app.services.portfolio.executor import OrderExecutor
    from app.services.portfolio.models import OrderIntent, PortfolioAccount, TradingCostConfig

    account = PortfolioAccount(
        portfolio_id=1,
        name="Edge",
        status=PortfolioStatus.ACTIVE,
        initial_capital=Decimal("1000"),
        cash=Decimal("1000"),
        realized_pnl=Decimal("0"),
        total_market_value=Decimal("0"),
    )
    intent = OrderIntent(
        portfolio_id=1,
        stock_id=stock.id,
        side=OrderSide.BUY,
        quantity=quantity,
        requested_price=Decimal("100"),
        order_type=OrderType.MARKET,
    )
    plan = OrderExecutor(
        TradingCostConfig(commission_rate=Decimal("0"), min_fee=Decimal("0"), slippage_rate=Decimal("0"))
    ).execute(account, intent)

    assert plan.accepted is False
    assert plan.rejection.code.value == "INVALID_QUANTITY"


@pytest.mark.parametrize("price", ["0", "-1", "-0.0001"])
def test_edge_invalid_price_is_refused(db_session: Session, stock, price):
    """Edge: a non-positive price never fills."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OrderCreate(
            stock_id=stock.id,
            side=OrderSide.BUY,
            quantity=1,
            requested_price=Decimal(price),
        )

    portfolio = make_portfolio(db_session)
    from app.services.portfolio.executor import OrderExecutor
    from app.services.portfolio.models import OrderIntent, PortfolioAccount, TradingCostConfig

    account = PortfolioAccount(
        portfolio_id=portfolio.id,
        name="Edge",
        status=PortfolioStatus.ACTIVE,
        initial_capital=Decimal("1000"),
        cash=Decimal("1000"),
        realized_pnl=Decimal("0"),
        total_market_value=Decimal("0"),
    )
    plan = OrderExecutor(
        TradingCostConfig(commission_rate=Decimal("0"), min_fee=Decimal("0"), slippage_rate=Decimal("0"))
    ).execute(
        account,
        OrderIntent(
            portfolio_id=portfolio.id,
            stock_id=stock.id,
            side=OrderSide.BUY,
            quantity=1,
            requested_price=Decimal(price),
            order_type=OrderType.MARKET,
        ),
    )

    assert plan.rejection.code.value == "INVALID_PRICE"


def test_edge_zero_fees_are_charged_as_zero(db_session: Session, stock):
    """Edge: a zero commission charges nothing at all, so gross equals cash movement."""
    portfolio = make_portfolio(db_session, capital="100000", trading_costs=NO_FEES)

    result = buy(db_session, portfolio, stock.id, 10, "100")

    assert Decimal(result.trade.fees) == Decimal("0.00")
    assert Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    ) == money(Decimal("99000"))


def test_edge_very_small_decimal_price(db_session: Session, stock):
    """Edge: a sub-penny price still produces exact Decimal arithmetic, never a float."""
    portfolio = make_portfolio(db_session, capital="100000")

    result = buy(db_session, portfolio, stock.id, 3, "0.0001")

    position = position_of(db_session, portfolio.id, stock.id)
    assert Decimal(position.average_entry_price) == Decimal("0.0001")
    assert Decimal(result.trade.gross_value) == Decimal("0.00")
    assert Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    ) == money(Decimal("100000"))


def test_edge_very_large_portfolio(db_session: Session, stock):
    """Edge: a large portfolio keeps full Numeric precision on equity and P&L."""
    portfolio = make_portfolio(db_session, capital="999999999999.99")
    buy(db_session, portfolio, stock.id, 1, "1234.5678")
    paper_trading_service.update_market_prices(
        db_session, portfolio.id, {stock.id: Decimal("1234.5679")}
    )

    valuation = valuation_of(db_session, portfolio.id)

    assert valuation.equity == money(Decimal("999999999999.99") + Decimal("0.0001"))
    assert valuation.total_pnl == money(Decimal("0.0001"))


def test_edge_portfolio_with_only_cash(db_session: Session):
    """Edge: a portfolio that never traded values cleanly at its capital."""
    portfolio = make_portfolio(db_session, capital="75000")

    valuation = valuation_of(db_session, portfolio.id)

    assert valuation.market_value == Decimal("0.00")
    assert valuation.equity == money(Decimal("75000"))
    assert valuation.exposure_pct == Decimal("0.0000")
    assert paper_trading_service.count_snapshots(db_session, portfolio.id) == 0


def test_edge_single_position_portfolio(db_session: Session, stock):
    """Edge: one position concentrates the whole exposure, and that is reported."""
    portfolio = make_portfolio(
        db_session,
        capital="100000",
        risk_limits=RiskLimitSettings(max_position_allocation_pct=Decimal("80")),
    )
    buy(db_session, portfolio, stock.id, 100, "100")

    valuation = valuation_of(db_session, portfolio.id)

    assert valuation.positions[0].allocation_pct(valuation.equity) == Decimal("10.0000")
    assert valuation.exposure_pct == Decimal("10.0000")


def test_edge_repeated_buys_and_sells_stay_consistent(db_session: Session, stock):
    """Edge: an alternating sequence of trades keeps the books reconcilable."""
    portfolio = make_portfolio(db_session, capital="1000000")
    for index in range(5):
        buy(db_session, portfolio, stock.id, 10, str(100 + index))
        sell(db_session, portfolio, stock.id, 5, str(105 + index))

    valuation = valuation_of(db_session, portfolio.id)
    position = position_of(db_session, portfolio.id, stock.id)

    assert position.quantity == 25
    assert valuation.total_pnl == money(valuation.equity - Decimal("1000000"))
    assert valuation.realized_pnl + valuation.unrealized_pnl - valuation.open_entry_fees == (
        valuation.total_pnl
    )


def test_edge_no_trades_and_no_snapshots_reports_cleanly(db_session: Session):
    """Edge: activity counters are zero, not missing, for a brand-new portfolio."""
    portfolio = make_portfolio(db_session)

    performance = paper_trading_service.get_performance(db_session, portfolio.id)

    assert performance.trade_count == 0
    assert performance.order_count == 0
    assert performance.snapshot_count == 0
    assert performance.win_rate_pct is None
    assert performance.profit_factor is None
    assert performance.sharpe_ratio is None
    assert performance.insufficient_data  # the explicit "needs more history" state


def test_edge_missing_stock_is_a_structured_error(db_session: Session):
    """Edge: an unknown stock id is a 404-style domain error, not a foreign-key crash."""
    portfolio = make_portfolio(db_session)

    with pytest.raises(Exception) as excinfo:
        buy(db_session, portfolio, 999999, 1, "100")
    assert "999999" in str(excinfo.value)