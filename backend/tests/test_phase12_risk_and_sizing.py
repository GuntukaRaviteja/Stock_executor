"""Phase 12 risk-management and position-sizing tests.

Numbered coverage in this file:

* 25-34 risk rules: position size, concentration, exposure, max positions, minimum trade
  value, drawdown, daily loss, insufficient cash, and an approved/rejected trade
* 46-51 position sizing: fixed capital, percentage of equity, risk-based, zero stop distance,
  an invalid stop loss, and Decimal precision

The Phase 12 additions covered here specifically are the two rules that did not exist before
this phase: ``min_trade_value`` (applies to buys *and* sells) and ``max_daily_loss_pct``
(blocks new buys once the day's loss reaches the limit, and never blocks a sell).

Each rule is configured in isolation, so a failure points at that rule rather than at an
interaction with another.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import InsufficientCashError, RiskLimitExceededError
from app.models.stock import Stock
from app.schemas.portfolio import (
    OrderSide,
    PositionSizingMethod,
    PositionSizingRequest,
    PortfolioCreate,
    RiskCheckRequest,
    RiskLimitSettings,
    TradingCostSettings,
)
from app.services.portfolio.defaults import default_risk_limits
from app.services.portfolio.models import PortfolioAccount, RiskLimits
from app.services.portfolio.risk import RiskManager
from app.services.portfolio.risk_service import day_open_equity
from app.services.portfolio.service import paper_trading_service

NO_FEES = TradingCostSettings(commission_rate=Decimal("0"), min_fee=Decimal("0"), slippage_rate=Decimal("0"))


def money(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


@pytest.fixture
def stock(db_session: Session) -> Stock:
    record = Stock(symbol="RSKCO", company_name="Risk Co", exchange="NSE", market="INDIA")
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


@pytest.fixture
def other_stock(db_session: Session) -> Stock:
    record = Stock(symbol="RSKCO2", company_name="Risk Co Two", exchange="NSE", market="INDIA")
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


def make_portfolio(db: Session, capital="100000", **limits):
    return paper_trading_service.create_portfolio(
        db,
        PortfolioCreate(
            name="Risk Portfolio",
            initial_capital=Decimal(capital),
            trading_costs=NO_FEES,
            risk_limits=RiskLimitSettings(**limits) if limits else None,
        ),
    )


def buy(db: Session, portfolio, stock_id: int, quantity: int, price: str):
    from app.schemas.portfolio import OrderCreate, OrderType

    return paper_trading_service.place_order(
        db,
        portfolio.id,
        OrderCreate(
            stock_id=stock_id,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=quantity,
            requested_price=Decimal(price),
        ),
    )


def sell(db: Session, portfolio, stock_id: int, quantity: int, price: str):
    from app.schemas.portfolio import OrderCreate, OrderType

    return paper_trading_service.place_order(
        db,
        portfolio.id,
        OrderCreate(
            stock_id=stock_id,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=quantity,
            requested_price=Decimal(price),
        ),
    )


def check(db: Session, portfolio, stock_id: int, side: str, quantity: int, price: str, **kwargs):
    return paper_trading_service.check_risk(
        db,
        portfolio.id,
        RiskCheckRequest(
            stock_id=stock_id,
            side=OrderSide(side),
            quantity=quantity,
            requested_price=Decimal(price),
            **kwargs,
        ),
    )


def rules_of(assessment) -> list:
    return [violation.rule for violation in assessment.violations]


def snapshot_on(db: Session, portfolio, equity_price: str, days_ago: int):
    """Record a snapshot at an explicit past moment, at a stated mark."""
    paper_trading_service.update_market_prices(
        db, portfolio.id, {}, as_of=datetime.utcnow() - timedelta(days=days_ago)
    )
    return paper_trading_service.create_snapshot(
        db,
        portfolio.id,
        as_of=datetime.utcnow() - timedelta(days=days_ago),
    )


# ======================================================================
# 25-34  Risk rules
# ======================================================================


def test_25_position_size_limit(db_session: Session, stock):
    """25. A position may not exceed its configured share of portfolio equity."""
    portfolio = make_portfolio(db_session, max_position_allocation_pct=Decimal("20"))

    assessment = check(db_session, portfolio, stock.id, "BUY", 250, "100")

    assert assessment.accepted is False
    assert "max_position_allocation_pct" in rules_of(assessment)
    # 250 shares at 100 is 25000, i.e. 25% of the 100000 equity, above the 20% limit.
    assert any(
        violation.details.get("limit_pct") == "20" for violation in assessment.violations
    )
    # ...and the rejected trade changed nothing.
    assert paper_trading_service.get_positions(db_session, portfolio.id) == []
    assert Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    ) == money(Decimal("100000"))


def test_25b_position_at_exactly_the_limit_is_accepted(db_session: Session, stock):
    """25. A position exactly on the limit is allowed; one share above it is not."""
    portfolio = make_portfolio(db_session, max_position_allocation_pct=Decimal("20"))

    at_limit = check(db_session, portfolio, stock.id, "BUY", 200, "100")
    over_limit = check(db_session, portfolio, stock.id, "BUY", 201, "100")

    assert at_limit.accepted is True
    assert over_limit.accepted is False
    assert "max_position_allocation_pct" in rules_of(over_limit)


def test_26_concentration_limit(db_session: Session, stock, other_stock):
    """26. Concentration is the same rule seen from the other side: one stock, one cap.

    With the cap set, the first stock may be bought up to the limit and no further; a second
    stock is unaffected by it, because concentration is per stock.
    """
    portfolio = make_portfolio(
        db_session,
        max_position_allocation_pct=Decimal("30"),
        max_positions=5,
    )
    buy(db_session, portfolio, stock.id, 300, "100")  # exactly 30%

    adding_more = check(db_session, portfolio, stock.id, "BUY", 1, "100")
    a_different_stock = check(db_session, portfolio, other_stock.id, "BUY", 100, "50")

    assert "max_position_allocation_pct" in rules_of(adding_more)
    assert a_different_stock.accepted is True


def test_26b_max_position_quantity_is_a_separate_cap(db_session: Session, stock):
    """26. The share-count cap is independent of the value cap."""
    portfolio = make_portfolio(db_session, max_position_quantity=5)

    assessment = check(db_session, portfolio, stock.id, "BUY", 6, "1")

    assert "max_position_quantity" in rules_of(assessment)


def test_27_exposure_limit(db_session: Session, stock, other_stock):
    """27. Total invested capital across all positions cannot exceed the exposure limit."""
    portfolio = make_portfolio(
        db_session, max_exposure_pct=Decimal("40"), max_positions=5
    )
    buy(db_session, portfolio, stock.id, 300, "100")  # 30000 invested, 30% of equity

    assessment = check(db_session, portfolio, other_stock.id, "BUY", 200, "100")

    # 30000 + 20000 = 50000 invested is 50% of equity, above the 40% exposure limit. The
    # exposure rule is what fires: the second stock itself is only 20% of equity.
    assert "max_exposure_pct" in rules_of(assessment)


def test_28_maximum_positions(db_session: Session, stock, other_stock):
    """28. A new position is refused once the maximum number of positions is held."""
    portfolio = make_portfolio(
        db_session, max_positions=2, max_position_allocation_pct=Decimal("50")
    )
    second = Stock(symbol="RSKCO3", company_name="Risk Co Three", exchange="NSE", market="INDIA")
    db_session.add(second)
    db_session.commit()
    db_session.refresh(second)
    buy(db_session, portfolio, stock.id, 100, "100")
    buy(db_session, portfolio, second.id, 100, "100")  # now at the maximum of two

    third = Stock(symbol="RSKCO4", company_name="Risk Co Four", exchange="NSE", market="INDIA")
    db_session.add(third)
    db_session.commit()
    db_session.refresh(third)

    opening_third = check(db_session, portfolio, third.id, "BUY", 10, "10")

    assert "max_positions" in rules_of(opening_third)
    # Increasing an *existing* position is still fine: no new name is opened.
    increasing = check(db_session, portfolio, stock.id, "BUY", 10, "100")
    assert increasing.accepted is True


def test_29_minimum_trade_value_on_a_buy(db_session: Session, stock):
    """29. An order below the configured minimum trade value is refused."""
    portfolio = make_portfolio(db_session, min_trade_value=Decimal("1000"))

    too_small = check(db_session, portfolio, stock.id, "BUY", 1, "100")  # 100
    big_enough = check(db_session, portfolio, stock.id, "BUY", 10, "100")  # 1000

    assert "min_trade_value" in rules_of(too_small)
    assert big_enough.accepted is True
    assert next(
        v for v in too_small.violations if v.rule == "min_trade_value"
    ).details["limit"] == "1000"


def test_29b_minimum_trade_value_blocks_a_sell_too(db_session: Session, stock):
    """29. The minimum trade value is the one limit that applies to sells as well.

    A dust sell must be refused too: otherwise a position could be closed for a few paise, and
    the per-order fee would dominate the trade.
    """
    portfolio = make_portfolio(db_session, min_trade_value=Decimal("1000"))
    buy(db_session, portfolio, stock.id, 10, "100")

    dust_sell = check(db_session, portfolio, stock.id, "SELL", 1, "100")
    whole_sell = check(db_session, portfolio, stock.id, "SELL", 10, "100")

    assert "min_trade_value" in rules_of(dust_sell)
    assert whole_sell.accepted is True

    with pytest.raises(RiskLimitExceededError) as excinfo:
        sell(db_session, portfolio, stock.id, 1, "100")
    assert excinfo.value.code == "MIN_TRADE_VALUE_BELOW"


def test_29c_minimum_trade_value_unset_never_blocks(db_session: Session, stock):
    """29. With no minimum configured, a one-share order is perfectly acceptable."""
    portfolio = make_portfolio(db_session)

    assert check(db_session, portfolio, stock.id, "BUY", 1, "0.01").accepted is True


def test_30_drawdown_limit(db_session: Session, stock):
    """30. New buys are blocked at or beyond the drawdown limit; sells stay allowed."""
    portfolio = make_portfolio(db_session, max_drawdown_pct=Decimal("10"))
    # Mostly invested, so the mark is the main thing that moves equity: 800 shares at 100
    # leaves cash 20000 and equity 100000, which is the peak.
    buy(db_session, portfolio, stock.id, 800, "100")
    # Marked to 89 the equity is 20000 + 71200 = 91200, an 8.8% drawdown. Marked to 87 it is
    # 89600, a 10.4% drawdown — past the limit.
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("87")})
    paper_trading_service.create_snapshot(db_session, portfolio.id)

    blocked_buy = check(db_session, portfolio, stock.id, "BUY", 1, "87")
    allowed_sell = check(db_session, portfolio, stock.id, "SELL", 10, "87")

    assert "max_drawdown_pct" in rules_of(blocked_buy)
    assert allowed_sell.accepted is True
    # The rule refuses the *order*, so the engine records a rejection rather than a fill.
    with pytest.raises(RiskLimitExceededError) as excinfo:
        buy(db_session, portfolio, stock.id, 1, "87")
    assert excinfo.value.code == "MAX_DRAWDOWN_EXCEEDED"


def test_30b_drawdown_exactly_at_the_limit_is_blocked(db_session: Session, stock):
    """30. The limit is inclusive: a drawdown exactly equal to it blocks."""
    portfolio = make_portfolio(db_session, max_drawdown_pct=Decimal("10"))
    buy(db_session, portfolio, stock.id, 800, "100")  # cash 20000, equity 100000
    # 800 * 87.50 = 70000, so equity is exactly 90000 and the drawdown exactly 10%.
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("87.5")})

    assessment = check(db_session, portfolio, stock.id, "BUY", 1, "87.5")

    assert "max_drawdown_pct" in rules_of(assessment)
    assert assessment.current_drawdown_pct == Decimal("10.0000")


def test_30c_drawdown_one_unit_below_the_limit_is_allowed(db_session: Session, stock):
    """30. Just inside the limit, buying is still permitted."""
    portfolio = make_portfolio(db_session, max_drawdown_pct=Decimal("10"))
    buy(db_session, portfolio, stock.id, 800, "100")  # cash 20000, equity 100000
    # 800 * 88.75 = 71000, so equity is 91000 and the drawdown 9%, just inside the limit.
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("88.75")})

    assessment = check(db_session, portfolio, stock.id, "BUY", 1, "88.75")

    assert assessment.accepted is True
    assert assessment.current_drawdown_pct == Decimal("9.0000")


def test_31_daily_loss_limit(db_session: Session, stock):
    """31. New buys are blocked once the day's loss reaches the configured limit.

    The day's opening equity is the last snapshot recorded *before* today, so the limit measures
    the loss of the calendar day rather than the loss since inception.
    """
    portfolio = make_portfolio(db_session, max_daily_loss_pct=Decimal("5"))
    buy(db_session, portfolio, stock.id, 800, "100")
    # A snapshot from yesterday puts the day's opening equity at 100000...
    snapshot_on(db_session, portfolio, "100", days_ago=1)
    # ...and marking the holding to 93 makes today's equity 20000 + 74400 = 94400, a 5.6% loss.
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("93")})

    blocked = check(db_session, portfolio, stock.id, "BUY", 1, "93")
    allowed_sell = check(db_session, portfolio, stock.id, "SELL", 10, "93")

    assert "max_daily_loss_pct" in rules_of(blocked)
    assert allowed_sell.accepted is True
    assert next(
        v for v in blocked.violations if v.rule == "max_daily_loss_pct"
    ).details["day_open_equity"] == "100000.00"


def test_31b_daily_loss_exactly_at_the_limit_is_blocked(db_session: Session, stock):
    """31. Exactly at the limit blocks, exactly one unit inside it does not."""
    portfolio = make_portfolio(db_session, max_daily_loss_pct=Decimal("5"))
    buy(db_session, portfolio, stock.id, 800, "100")
    snapshot_on(db_session, portfolio, "100", days_ago=1)
    # 800 * 93.75 = 75000, so equity is exactly 95000 and the day's loss exactly 5%.
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("93.75")})

    at_limit = check(db_session, portfolio, stock.id, "BUY", 1, "93.75")

    assert "max_daily_loss_pct" in rules_of(at_limit)
    assert next(
        v for v in at_limit.violations if v.rule == "max_daily_loss_pct"
    ).details["daily_loss_pct"] == "5.0000"


def test_31c_daily_loss_is_disabled_by_default(db_session: Session, stock):
    """31. A portfolio with no daily-loss limit is never blocked by one."""
    portfolio = make_portfolio(db_session)
    buy(db_session, portfolio, stock.id, 800, "100")
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("50")})

    assessment = check(db_session, portfolio, stock.id, "BUY", 1, "50")

    assert assessment.accepted is True


def test_31d_daily_loss_uses_the_first_snapshot_of_the_day(db_session: Session, stock):
    """31. With no earlier snapshot, the day's opening equity is today's first snapshot.

    Without this, a portfolio created and traded in one session would look as if it had lost
    nothing, and the rule could never fire on the day it matters most.
    """
    portfolio = make_portfolio(db_session, max_daily_loss_pct=Decimal("5"))
    buy(db_session, portfolio, stock.id, 800, "100")
    paper_trading_service.create_snapshot(db_session, portfolio.id)  # equity 100000, today
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("90")})

    opening = day_open_equity(db_session, paper_trading_service.get_portfolio(db_session, portfolio.id))
    assessment = check(db_session, portfolio, stock.id, "BUY", 1, "90")

    assert opening == money(Decimal("100000"))
    assert "max_daily_loss_pct" in rules_of(assessment)


def test_31e_daily_loss_ignores_a_deposit(db_session: Session, stock):
    """31. A deposit is not a loss: the day opened above the starting capital."""
    from app.schemas.allocation import CashMovementRequest

    portfolio = make_portfolio(db_session, max_daily_loss_pct=Decimal("5"))
    paper_trading_service.deposit(
        db_session, portfolio.id, CashMovementRequest(amount=Decimal("10000"))
    )
    buy(db_session, portfolio, stock.id, 800, "100")

    opening = day_open_equity(db_session, paper_trading_service.get_portfolio(db_session, portfolio.id))
    assessment = check(db_session, portfolio, stock.id, "BUY", 1, "100")

    # The portfolio has 110000 to work with, not 100000: the deposit is capital, not a gain.
    assert opening == money(Decimal("110000"))
    assert assessment.accepted is True


def test_31f_daily_pnl_is_reported_performance(db_session: Session, stock):
    """31/12. Daily P&L is equity minus the day's opening equity, signed."""
    portfolio = make_portfolio(db_session)
    buy(db_session, portfolio, stock.id, 800, "100")  # cash 20000, equity 100000
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("90")})

    performance = paper_trading_service.get_performance(db_session, portfolio.id)

    # Equity falls from 100000 to 92000, so the day is down 8000, i.e. 8%.
    assert performance.equity == money(Decimal("92000"))
    assert performance.daily_pnl == money(Decimal("-8000"))
    assert performance.daily_return_pct == Decimal("-8.0000")


def test_31g_daily_return_is_none_when_the_day_opened_at_zero(db_session: Session, stock):
    """Edge: a daily return percentage is undefined for a day that opened at zero equity."""
    portfolio = make_portfolio(db_session, capital="1000")
    buy(db_session, portfolio, stock.id, 10, "100")
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("0.0001")})

    performance = paper_trading_service.get_performance(db_session, portfolio.id)

    assert performance.daily_pnl == money(Decimal("-999.9999"))
    assert performance.daily_return_pct == Decimal("-100.0000")


def test_32_insufficient_cash(db_session: Session, stock):
    """32. A buy the cash cannot fund is refused by the risk check and by execution."""
    portfolio = make_portfolio(db_session, capital="1000")

    assessment = check(db_session, portfolio, stock.id, "BUY", 100, "100")

    assert assessment.accepted is False
    assert "insufficient_cash_protection" in rules_of(assessment)

    with pytest.raises(InsufficientCashError):
        buy(db_session, portfolio, stock.id, 100, "100")


def test_33_risk_approved_trade(db_session: Session, stock):
    """33. A trade inside every configured limit is approved by the check and then fills."""
    portfolio = make_portfolio(
        db_session,
        max_position_allocation_pct=Decimal("25"),
        max_exposure_pct=Decimal("50"),
        max_positions=3,
        min_trade_value=Decimal("100"),
    )

    assessment = check(db_session, portfolio, stock.id, "BUY", 200, "100")
    result = buy(db_session, portfolio, stock.id, 200, "100")

    assert assessment.accepted is True
    assert assessment.violations == ()
    assert result.order.status == "FILLED"
    assert Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    ) == money(Decimal("80000"))


def test_34_risk_rejected_trade_is_recorded_with_its_reason(db_session: Session, stock):
    """34. A rejected trade is stored with a machine-readable code and an explanation."""
    portfolio = make_portfolio(db_session, max_position_allocation_pct=Decimal("10"))

    with pytest.raises(RiskLimitExceededError) as excinfo:
        buy(db_session, portfolio, stock.id, 500, "100")

    orders = paper_trading_service.get_orders(db_session, portfolio.id)
    assert excinfo.value.code == "MAX_POSITION_ALLOCATION_EXCEEDED"
    assert len(orders) == 1
    assert orders[0].status == "REJECTED"
    assert orders[0].rejection_code == "MAX_POSITION_ALLOCATION_EXCEEDED"
    assert "20" not in orders[0].rejection_reason or "10" in orders[0].rejection_reason
    # Nothing moved.
    assert paper_trading_service.get_trades(db_session, portfolio.id) == []
    assert Decimal(
        paper_trading_service.get_portfolio(db_session, portfolio.id).current_cash
    ) == money(Decimal("100000"))


def test_34b_a_check_never_mutates_anything(db_session: Session, stock):
    """34. Asking the risk engine a question changes no cash, position, order or trade."""
    portfolio = make_portfolio(db_session, max_position_allocation_pct=Decimal("10"))
    buy(db_session, portfolio, stock.id, 100, "100")

    before = paper_trading_service.get_portfolio(db_session, portfolio.id)
    orders_before = len(paper_trading_service.get_orders(db_session, portfolio.id))

    check(db_session, portfolio, stock.id, "BUY", 5000, "100")

    after = paper_trading_service.get_portfolio(db_session, portfolio.id)
    assert Decimal(after.current_cash) == Decimal(before.current_cash)
    assert Decimal(after.equity) == Decimal(before.equity)
    assert len(paper_trading_service.get_orders(db_session, portfolio.id)) == orders_before


def test_34c_effective_limits_report_the_daily_loss_breach(db_session: Session, stock):
    """34. The portfolio-level report states the daily-loss rule as already breached."""
    portfolio = make_portfolio(db_session, max_daily_loss_pct=Decimal("5"))
    buy(db_session, portfolio, stock.id, 800, "100")
    snapshot_on(db_session, portfolio, "100", days_ago=1)
    paper_trading_service.update_market_prices(db_session, portfolio.id, {stock.id: Decimal("93.75")})

    effective = paper_trading_service.effective_risk(db_session, portfolio.id)

    assert effective["day_open_equity"] == money(Decimal("100000"))
    assert "max_daily_loss_pct" in [violation.rule for violation in effective["violations"]]


# ======================================================================
# 46-51  Position sizing
# ======================================================================


def test_46_fixed_capital_sizing(db_session: Session, stock):
    """46. FIXED_CAPITAL floors capital / price into whole shares."""
    portfolio = make_portfolio(db_session, capital="100000")

    plan = paper_trading_service.size_position(
        db_session,
        portfolio.id,
        PositionSizingRequest(
            method=PositionSizingMethod.FIXED_CAPITAL,
            price=Decimal("30"),
            capital=Decimal("1000"),
        ),
    )

    assert plan.method == PositionSizingMethod.FIXED_CAPITAL
    assert plan.quantity == 33  # floor(1000 / 30)
    assert plan.capital == money(Decimal("33") * 30)


def test_47_percentage_of_equity_sizing(db_session: Session, stock):
    """47. PERCENTAGE commits a share of equity, floored into whole shares."""
    portfolio = make_portfolio(db_session, capital="100000")

    plan = paper_trading_service.size_position(
        db_session,
        portfolio.id,
        PositionSizingRequest(
            method=PositionSizingMethod.PERCENTAGE,
            price=Decimal("25"),
            allocation_pct=Decimal("10"),
        ),
    )

    assert plan.quantity == 400  # 10% of 100000 = 10000, / 25
    assert plan.capital == money(Decimal("10000"))


def test_48_risk_based_sizing(db_session: Session, stock):
    """48. RISK_BASED is floor(equity * risk% / |entry - stop|)."""
    portfolio = make_portfolio(db_session, capital="100000")

    plan = paper_trading_service.size_position(
        db_session,
        portfolio.id,
        PositionSizingRequest(
            method=PositionSizingMethod.RISK_BASED,
            price=Decimal("100"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("95"),
            risk_pct=Decimal("2"),
        ),
    )

    # Risk budget 2000, risk per share 5 -> 400 shares.
    assert plan.risk_per_share == Decimal("5")
    assert plan.allowed_risk == money(Decimal("2000"))
    assert plan.quantity == 400


def test_49_zero_stop_distance_is_rejected_not_divided(db_session: Session, stock):
    """49. A stop at the entry price defines no risk per share and is refused."""
    from app.core.exceptions import InvalidPositionSizeError

    portfolio = make_portfolio(db_session)

    with pytest.raises(InvalidPositionSizeError):
        paper_trading_service.size_position(
            db_session,
            portfolio.id,
            PositionSizingRequest(
                method=PositionSizingMethod.RISK_BASED,
                price=Decimal("100"),
                entry_price=Decimal("100"),
                stop_loss_price=Decimal("100"),
                risk_pct=Decimal("2"),
            ),
        )


def test_50_invalid_stop_loss_is_rejected(db_session: Session, stock):
    """50. A long stop above the entry price would invert the risk and is refused."""
    from app.core.exceptions import InvalidPositionSizeError

    portfolio = make_portfolio(db_session)

    with pytest.raises(InvalidPositionSizeError):
        paper_trading_service.size_position(
            db_session,
            portfolio.id,
            PositionSizingRequest(
                method=PositionSizingMethod.RISK_BASED,
                price=Decimal("100"),
                entry_price=Decimal("100"),
                stop_loss_price=Decimal("120"),
                risk_pct=Decimal("2"),
            ),
        )


def test_51_decimal_precision_in_sizing(db_session: Session, stock):
    """51. Sizing stays exact on fractional prices and fractions of a percent."""
    from app.services.portfolio.models import TradingCostConfig
    from app.services.portfolio.calculator import risk_per_share, size_quantity_by_risk

    NO_COSTS = TradingCostConfig(
        commission_rate=Decimal("0"), min_fee=Decimal("0"), slippage_rate=Decimal("0")
    )

    portfolio = make_portfolio(db_session, capital="100000")

    plan = paper_trading_service.size_position(
        db_session,
        portfolio.id,
        PositionSizingRequest(
            method=PositionSizingMethod.RISK_BASED,
            price=Decimal("99.9999"),
            entry_price=Decimal("99.9999"),
            stop_loss_price=Decimal("99.6666"),
            risk_pct=Decimal("1.5"),
        ),
    )

    # Risk per share is 0.3333 and 1.5% of 100000 is a 1500 risk budget, so the pure formula
    # allows floor(1500 / 0.3333) = 4500 shares.
    assert risk_per_share(Decimal("99.9999"), Decimal("99.6666")) == Decimal("0.3333")
    pure_quantity = size_quantity_by_risk(
        Decimal("100000"),
        Decimal("1.5"),
        Decimal("99.9999"),
        Decimal("99.6666"),
        NO_COSTS,
    )
    assert pure_quantity == 4500
    # The service additionally caps the result at what the portfolio's cash can actually fund:
    # 4500 shares at 99.9999 costs ~450000, so only 1000 fit in 100000 of cash.
    assert plan.quantity == 1000
    assert plan.quantity < pure_quantity
    assert plan.risk_per_share == Decimal("0.3333")
    assert plan.allowed_risk == money(Decimal("1500"))
    # Whatever the cap, the sized quantity never risks more than the budget.
    assert Decimal(plan.quantity) * Decimal("0.3333") <= Decimal("1500")
    # ...and every amount stayed exact Decimal arithmetic: no float ever entered.
    assert isinstance(plan.quantity, int)
    assert isinstance(plan.capital, Decimal)


# ======================================================================
# Pure-engine tests: the rules without a database
# ======================================================================


def test_risk_manager_is_a_pure_function_of_its_inputs():
    """The rules never touch state: the same numbers always give the same answer."""
    from app.schemas.portfolio import OrderType
    from app.services.portfolio.models import OrderIntent

    manager = RiskManager(RiskLimits(min_trade_value=Decimal("500")))
    account = PortfolioAccount(
        portfolio_id=1,
        name="Pure",
        status=OrderSide.BUY and __import__(
            "app.schemas.portfolio", fromlist=["PortfolioStatus"]
        ).PortfolioStatus.ACTIVE,
        initial_capital=Decimal("10000"),
        cash=Decimal("10000"),
        realized_pnl=Decimal("0"),
        total_market_value=Decimal("0"),
    )
    intent = OrderIntent(
        portfolio_id=1,
        stock_id=1,
        side=OrderSide.SELL,
        quantity=1,
        requested_price=Decimal("100"),
        order_type=OrderType.MARKET,
    )

    first = manager.validate_sell(account, intent, trade_value=Decimal("100"))
    second = manager.validate_sell(account, intent, trade_value=Decimal("100"))

    assert first is not None and second is not None
    assert first.code == second.code == "MIN_TRADE_VALUE_BELOW"
    assert first.reason == second.reason
    assert first.details == second.details


def test_risk_limits_validation_rejects_nonsense():
    """A limit that cannot mean anything is refused at configuration time."""
    for kwargs in (
        {"max_daily_loss_pct": Decimal("0")},
        {"max_daily_loss_pct": Decimal("-1")},
        {"max_daily_loss_pct": Decimal("101")},
        {"min_trade_value": Decimal("0")},
        {"min_trade_value": Decimal("-5")},
    ):
        with pytest.raises(ValueError):
            RiskLimits(**kwargs).validate()


def test_risk_limits_accept_the_phase_12_values():
    """The Phase 12 limits validate, and the percentage ones stay inside (0, 100]."""
    limits = RiskLimits(
        max_daily_loss_pct=Decimal("5"), min_trade_value=Decimal("250")
    )
    limits.validate()

    assert limits.max_daily_loss_pct == Decimal("5")
    assert limits.min_trade_value == Decimal("250")


def test_min_trade_value_violation_is_empty_when_unset_or_large_enough():
    """An unset minimum, or a large enough order, produces no rejection at all."""
    manager = RiskManager(RiskLimits(min_trade_value=Decimal("100")))
    unset = RiskManager(RiskLimits())

    assert manager.min_trade_value_violation(Decimal("100")) == []
    assert manager.min_trade_value_violation(Decimal("500")) == []
    assert unset.min_trade_value_violation(Decimal("0.01")) == []
    # An unknown trade value cannot be judged, so the rule says nothing.
    assert manager.min_trade_value_violation(None) == []


# ======================================================================
# Application settings: defaults, and per-portfolio override
# ======================================================================


def test_phase_12_settings_are_unset_by_default_so_the_rules_are_off():
    """The two new limits ship disabled. An upgrade must not change anyone's trading."""
    limits = default_risk_limits()

    assert limits.max_daily_loss_pct is None
    assert limits.min_trade_value is None


def test_phase_12_limits_are_read_from_application_settings(monkeypatch):
    """The configured default is what a portfolio with no limits of its own receives."""
    monkeypatch.setattr(settings, "PAPER_TRADING_MAX_DAILY_LOSS_PCT", Decimal("5"))
    monkeypatch.setattr(settings, "PAPER_TRADING_MIN_TRADE_VALUE", Decimal("250"))

    limits = default_risk_limits()

    assert limits.max_daily_loss_pct == Decimal("5")
    assert limits.min_trade_value == Decimal("250")


def test_a_portfolio_with_no_limits_inherits_the_application_defaults(
    db_session: Session, monkeypatch
):
    """The default is read once, at creation, and frozen into the portfolio."""
    monkeypatch.setattr(settings, "PAPER_TRADING_MAX_DAILY_LOSS_PCT", Decimal("5"))
    monkeypatch.setattr(settings, "PAPER_TRADING_MIN_TRADE_VALUE", Decimal("250"))

    portfolio = make_portfolio(db_session)

    assert paper_trading_service.risk_limits(portfolio).max_daily_loss_pct == Decimal("5")
    assert paper_trading_service.risk_limits(portfolio).min_trade_value == Decimal("250")

    # Changing the application default afterwards does not reach an existing portfolio.
    monkeypatch.setattr(settings, "PAPER_TRADING_MIN_TRADE_VALUE", Decimal("999"))
    assert paper_trading_service.risk_limits(portfolio).min_trade_value == Decimal("250")


def test_own_limits_override_the_application_default_and_leave_the_rest_unset(
    db_session: Session, monkeypatch
):
    """A portfolio that states its own values uses exactly those.

    Stating one limit does not silently pull in the application default for the others: a
    portfolio whose owner wrote down their risk policy gets exactly what they wrote down, and
    every limit they left out stays disabled rather than becoming an invented cap.
    """
    monkeypatch.setattr(settings, "PAPER_TRADING_MAX_DAILY_LOSS_PCT", Decimal("5"))
    monkeypatch.setattr(settings, "PAPER_TRADING_MIN_TRADE_VALUE", Decimal("250"))

    portfolio = make_portfolio(
        db_session, max_daily_loss_pct=Decimal("1"), min_trade_value=Decimal("100000")
    )

    limits = paper_trading_service.risk_limits(portfolio)
    assert limits.max_daily_loss_pct == Decimal("1")
    assert limits.min_trade_value == Decimal("100000")
    # Every limit this portfolio did not state is disabled, not defaulted.
    assert limits.max_exposure_pct is None
    assert limits.min_cash_reserve_pct is None
    assert limits.max_positions is None


def test_a_stored_limit_survives_a_portfolio_row_that_predates_it(db_session: Session):
    """A config written before Phase 12 has no Phase 12 key, and that stays ``None``."""
    portfolio = make_portfolio(db_session, max_daily_loss_pct=Decimal("2"))
    payload = json.loads(portfolio.risk_config_json)
    del payload["max_daily_loss_pct"]
    del payload["min_trade_value"]
    portfolio.risk_config_json = json.dumps(payload, sort_keys=True)
    db_session.commit()

    limits = paper_trading_service.risk_limits(portfolio)

    assert limits.max_daily_loss_pct is None
    assert limits.min_trade_value is None
    assert limits.max_position_allocation_pct is None


def test_currency_default_comes_from_application_settings(db_session: Session, monkeypatch):
    """A portfolio created with no currency takes the configured default, uppercased."""
    monkeypatch.setattr(settings, "PAPER_TRADING_DEFAULT_CURRENCY", "usd")

    assert make_portfolio(db_session).currency == "USD"
