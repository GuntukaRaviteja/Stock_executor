"""Phase 10 risk-engine unit tests.

Every rule is exercised **in isolation**: each test configures exactly one limit so a failure
points at that rule and not at an interaction with another. Boundary cases are stated as
explicit percentages rather than copied from a run, and the tolerance handling for the
percentage comparisons is asserted as well.

The engine is pure here — no FastAPI, no database — so the money rules can be checked in
isolation.
"""

from decimal import Decimal

import pytest

from app.schemas.portfolio import OrderSide, PortfolioStatus
from app.services.portfolio.models import (
    PortfolioAccount,
    PositionState,
    RiskLimits,
    TradingCostConfig,
)
from app.services.portfolio.risk import RiskManager

#: Zero-cost model, so every percentage below is the arithmetic the test states and not a fee
#: effect. (Fees are covered by the Phase 9 test suite.)
NO_FEES = TradingCostConfig(
    commission_rate=Decimal("0"), min_fee=Decimal("0"), slippage_rate=Decimal("0")
)


def position(stock_id: int = 1, quantity: int = 10, price: Decimal = Decimal("100")) -> PositionState:
    """An open long position with a consistent cost basis."""
    return PositionState(
        stock_id=stock_id,
        quantity=quantity,
        average_entry_price=price,
        cost_basis=price * Decimal(quantity),
        current_price=price,
    )


def account(
    *,
    cash: Decimal = Decimal("100000"),
    market_value: Decimal = Decimal("0"),
    position_state: PositionState = None,
    position_count: int = 0,
    status: PortfolioStatus = PortfolioStatus.ACTIVE,
) -> PortfolioAccount:
    """A runtime account for the risk rules."""
    return PortfolioAccount(
        portfolio_id=1,
        name="Risk Test",
        status=status,
        initial_capital=Decimal("100000"),
        cash=cash,
        realized_pnl=Decimal("0"),
        total_market_value=market_value,
        position=position_state,
        position_count=position_count,
    )


def buy_resultion(
    *,
    quantity: int,
    fill_price: Decimal,
    previous_value: Decimal = Decimal("0"),
    total_market_value: Decimal = Decimal("0"),
    cash: Decimal = Decimal("100000"),
) -> dict:
    """The post-fill projection a buy of ``quantity`` at ``fill_price`` produces.

    Spelled out from the projection the executor uses, so a test states its own arithmetic.
    """
    trade_value = (fill_price * Decimal(quantity)).quantize(Decimal("0.01"))
    resulting_position_value = (
        fill_price * Decimal(quantity)
    ).quantize(Decimal("0.01")) + previous_value
    resulting_total = total_market_value + trade_value
    cash_after = cash - trade_value
    return {
        "resulting_quantity": quantity,
        "resulting_position_value": resulting_position_value,
        "resulting_total_market_value": resulting_total,
        "cash_after": cash_after,
        "projected_equity": cash_after + resulting_total,
        "trade_value": trade_value,
        "fees": Decimal("0"),
        "opening_new_position": previous_value == Decimal("0"),
    }


# ----------------------------------------------------------------------
# A. Maximum position size (share of equity)
# ----------------------------------------------------------------------


class TestMaxPositionSize:
    """``max_position_allocation_pct`` — one position's share of portfolio equity."""

    # A flat portfolio whose equity equals its cash: 10 shares at 999 / 1000 / 1001 give
    # position values of 9,990 / 10,000 / 10,010 against 100,000 of equity, i.e. exactly
    # 9.99% / 10.00% / 10.01%.
    def test_9_99_percent_is_accepted(self):
        manager = RiskManager(RiskLimits(max_position_allocation_pct=Decimal("10")))
        projection = _with_equity(
            buy_resultion(quantity=10, fill_price=Decimal("999"), cash=Decimal("100000")),
            Decimal("100000"),
        )
        assert projection["resulting_position_value"] == Decimal("9990.00")
        assert projection["projected_allocation_pct"] == Decimal("9.9900")
        assert manager.validate_buy(account(), **_rule_args(projection, Decimal("100000"))) is None

    def test_exactly_10_percent_is_accepted(self):
        manager = RiskManager(RiskLimits(max_position_allocation_pct=Decimal("10")))
        projection = _with_equity(
            buy_resultion(quantity=10, fill_price=Decimal("1000"), cash=Decimal("100000")),
            Decimal("100000"),
        )
        assert projection["resulting_position_value"] == Decimal("10000.00")
        assert projection["projected_allocation_pct"] == Decimal("10.0000")
        assert manager.validate_buy(account(), **_rule_args(projection, Decimal("100000"))) is None

    def test_10_01_percent_is_rejected(self):
        manager = RiskManager(RiskLimits(max_position_allocation_pct=Decimal("10")))
        projection = _with_equity(
            buy_resultion(quantity=10, fill_price=Decimal("1001"), cash=Decimal("100000")),
            Decimal("100000"),
        )
        assert projection["resulting_position_value"] == Decimal("10010.00")
        assert projection["projected_allocation_pct"] == Decimal("10.0100")
        rejection = manager.validate_buy(account(), **_rule_args(projection, Decimal("100000")))
        assert rejection is not None
        assert rejection.details["rule"] == "max_position_allocation_pct"
        assert "allocation" in rejection.reason.lower()

    def test_disabled_limit_allows_any_size(self):
        manager = RiskManager(RiskLimits())
        acc = account()
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=10_000,
            resulting_position_value=Decimal("1000000"),
            projected_equity=Decimal("100000"),
            peak_equity=None,
        )
        assert rejection is None


# ----------------------------------------------------------------------
# B. Maximum portfolio exposure
# ----------------------------------------------------------------------


class TestMaxExposure:
    """``max_exposure_pct`` — every position combined, as a share of equity."""

    def test_exposure_within_the_limit_is_accepted(self):
        manager = RiskManager(RiskLimits(max_exposure_pct=Decimal("100")))
        acc = account(market_value=Decimal("90000"))
        projection = _with_equity(
            buy_resultion(
                quantity=50,
                fill_price=Decimal("100"),
                total_market_value=Decimal("90000"),
                cash=Decimal("10000"),
            ),
            Decimal("100000"),
        )
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=projection["resulting_quantity"],
            resulting_position_value=projection["resulting_position_value"],
            projected_equity=projection["projected_equity"],
            peak_equity=None,
            resulting_total_market_value=projection["resulting_total_market_value"],
        )
        assert rejection is None

    def test_exposure_above_the_limit_is_rejected(self):
        manager = RiskManager(RiskLimits(max_exposure_pct=Decimal("80")))
        acc = account(market_value=Decimal("80000"))
        # 80,000 already invested plus a 20,000 buy leaves the portfolio fully invested.
        projection = _with_equity(
            buy_resultion(
                quantity=200,
                fill_price=Decimal("100"),
                total_market_value=Decimal("80000"),
                cash=Decimal("20000"),
            ),
            Decimal("100000"),
        )
        assert projection["projected_exposure_pct"] == Decimal("100.0000")
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=projection["resulting_quantity"],
            resulting_position_value=projection["resulting_position_value"],
            projected_equity=projection["projected_equity"],
            peak_equity=None,
            resulting_total_market_value=projection["resulting_total_market_value"],
        )
        assert rejection is not None
        assert rejection.details["rule"] == "max_exposure_pct"
        assert rejection.details["limit_pct"] == "80"

    def test_a_single_large_position_can_pass_exposure_while_failing_position_size(self):
        """The two rules are independent: one position may be legal but the total illegal."""
        manager = RiskManager(
            RiskLimits(max_position_allocation_pct=Decimal("90"), max_exposure_pct=Decimal("50"))
        )
        acc = account(market_value=Decimal("30000"))
        projection = _with_equity(
            buy_resultion(
                quantity=400,
                fill_price=Decimal("100"),
                total_market_value=Decimal("30000"),
                cash=Decimal("70000"),
            ),
            Decimal("100000"),
        )
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=projection["resulting_quantity"],
            resulting_position_value=projection["resulting_position_value"],
            projected_equity=projection["projected_equity"],
            peak_equity=None,
            resulting_total_market_value=projection["resulting_total_market_value"],
        )
        assert rejection is not None
        assert rejection.details["rule"] == "max_exposure_pct"


# ----------------------------------------------------------------------
# C. Maximum single-trade allocation
# ----------------------------------------------------------------------


class TestMaxTradeAllocation:
    """``max_trade_allocation_pct`` — capital one order may commit."""

    def test_trade_within_the_limit_is_accepted(self):
        manager = RiskManager(RiskLimits(max_trade_allocation_pct=Decimal("5")))
        acc = account()
        projection = _with_equity(
            buy_resultion(quantity=50, fill_price=Decimal("100"), cash=Decimal("100000")),
            Decimal("100000"),
        )
        assert projection["projected_trade_allocation_pct"] == Decimal("5.0000")
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=projection["resulting_quantity"],
            resulting_position_value=projection["resulting_position_value"],
            projected_equity=projection["projected_equity"],
            peak_equity=None,
            trade_value=projection["trade_value"],
        )
        assert rejection is None

    def test_trade_above_the_limit_is_rejected(self):
        manager = RiskManager(RiskLimits(max_trade_allocation_pct=Decimal("5")))
        acc = account()
        projection = _with_equity(
            buy_resultion(quantity=51, fill_price=Decimal("100"), cash=Decimal("100000")),
            Decimal("100000"),
        )
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=projection["resulting_quantity"],
            resulting_position_value=projection["resulting_position_value"],
            projected_equity=projection["projected_equity"],
            peak_equity=None,
            trade_value=projection["trade_value"],
        )
        assert rejection is not None
        assert rejection.details["rule"] == "max_trade_allocation_pct"

    def test_trade_allocation_is_measured_against_equity_not_cash(self):
        """A half-invested portfolio spends less than half of its cash on a 50% trade."""
        manager = RiskManager(RiskLimits(max_trade_allocation_pct=Decimal("10")))
        acc = account(cash=Decimal("50000"), market_value=Decimal("50000"))
        projection = _with_equity(
            buy_resultion(
                quantity=100,
                fill_price=Decimal("100"),
                total_market_value=Decimal("50000"),
                cash=Decimal("50000"),
            ),
            Decimal("100000"),
        )
        assert projection["projected_trade_allocation_pct"] == Decimal("10.0000")
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=projection["resulting_quantity"],
            resulting_position_value=projection["resulting_position_value"],
            projected_equity=projection["projected_equity"],
            peak_equity=None,
            trade_value=projection["trade_value"],
        )
        assert rejection is None


# ----------------------------------------------------------------------
# D. Minimum cash reserve
# ----------------------------------------------------------------------


class TestMinCashReserve:
    """``min_cash_reserve_pct`` — equity that must remain in cash after a buy."""

    def test_cash_above_the_reserve_is_accepted(self):
        manager = RiskManager(RiskLimits(min_cash_reserve_pct=Decimal("10")))
        acc = account(cash=Decimal("95000"))
        projection = _with_equity(
            buy_resultion(quantity=100, fill_price=Decimal("100"), cash=Decimal("95000")),
            Decimal("95000"),
        )
        assert projection["cash_after"] == Decimal("85000")
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=projection["resulting_quantity"],
            resulting_position_value=projection["resulting_position_value"],
            projected_equity=projection["projected_equity"],
            peak_equity=None,
            cash_after=projection["cash_after"],
        )
        assert rejection is None

    def test_cash_exactly_at_the_reserve_is_accepted(self):
        manager = RiskManager(RiskLimits(min_cash_reserve_pct=Decimal("10")))
        acc = account(cash=Decimal("90000"))
        # 10% of 90,000 equity is a 9,000 reserve; an 81,000 buy leaves exactly 9,000.
        projection = _with_equity(
            buy_resultion(quantity=810, fill_price=Decimal("100"), cash=Decimal("90000")),
            Decimal("90000"),
        )
        assert projection["cash_after"] == Decimal("9000.00")
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=projection["resulting_quantity"],
            resulting_position_value=projection["resulting_position_value"],
            projected_equity=projection["projected_equity"],
            peak_equity=None,
            cash_after=projection["cash_after"],
        )
        assert rejection is None

    def test_cash_just_below_the_reserve_is_rejected(self):
        manager = RiskManager(RiskLimits(min_cash_reserve_pct=Decimal("10")))
        acc = account(cash=Decimal("90000"))
        # One share more than the exact-reserve case leaves 100 less than the reserve.
        projection = _with_equity(
            buy_resultion(quantity=811, fill_price=Decimal("100"), cash=Decimal("90000")),
            Decimal("90000"),
        )
        assert projection["cash_after"] == Decimal("8900.00")
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=projection["resulting_quantity"],
            resulting_position_value=projection["resulting_position_value"],
            projected_equity=projection["projected_equity"],
            peak_equity=None,
            cash_after=projection["cash_after"],
        )
        assert rejection is not None
        assert rejection.details["rule"] == "min_cash_reserve_pct"
        assert rejection.details["required_reserve"] == "9000.00"


# ----------------------------------------------------------------------
# E. Maximum number of positions
# ----------------------------------------------------------------------


class TestMaxPositions:
    """``max_positions`` — how many stocks may be held at once."""

    def test_opening_a_position_below_the_limit_is_accepted(self):
        manager = RiskManager(RiskLimits(max_positions=Decimal("2")))
        acc = account(position_count=1)
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=10,
            resulting_position_value=Decimal("1000"),
            projected_equity=Decimal("100000"),
            peak_equity=None,
            open_position_count=1,
            opening_new_position=True,
        )
        assert rejection is None

    def test_opening_a_position_at_the_limit_is_rejected(self):
        manager = RiskManager(RiskLimits(max_positions=2))
        acc = account(position_count=2)
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=10,
            resulting_position_value=Decimal("1000"),
            projected_equity=Decimal("100000"),
            peak_equity=None,
            open_position_count=2,
            opening_new_position=True,
        )
        assert rejection is not None
        assert rejection.details["rule"] == "max_positions"
        assert rejection.details["limit"] == "2"

    def test_increasing_an_existing_position_is_allowed_at_the_limit(self):
        """The rule limits how many *stocks* are held, not how large a holding may get."""
        manager = RiskManager(RiskLimits(max_positions=2))
        acc = account(position_count=2, position_state=position())
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=20,
            resulting_position_value=Decimal("2000"),
            projected_equity=Decimal("100000"),
            peak_equity=None,
            open_position_count=2,
            opening_new_position=False,
        )
        assert rejection is None


# ----------------------------------------------------------------------
# F. Drawdown guard
# ----------------------------------------------------------------------


class TestDrawdownGuard:
    """``max_drawdown_pct`` — blocks new buys once the portfolio is far below its peak."""

    def test_buy_is_allowed_below_the_threshold(self):
        manager = RiskManager(RiskLimits(max_drawdown_pct=Decimal("20")))
        acc = account()
        # 85,000 against a 100,000 peak is a 15% drawdown: still inside the 20% limit.
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=10,
            resulting_position_value=Decimal("1000"),
            projected_equity=Decimal("85000"),
            peak_equity=Decimal("100000"),
        )
        assert rejection is None

    def test_buy_is_blocked_at_the_threshold(self):
        manager = RiskManager(RiskLimits(max_drawdown_pct=Decimal("20")))
        acc = account()
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=10,
            resulting_position_value=Decimal("1000"),
            projected_equity=Decimal("80000"),
            peak_equity=Decimal("100000"),
        )
        assert rejection is not None
        assert rejection.details["rule"] == "max_drawdown_pct"
        assert rejection.details["current_drawdown_pct"] == "20.0000"

    def test_buy_is_blocked_beyond_the_threshold(self):
        manager = RiskManager(RiskLimits(max_drawdown_pct=Decimal("20")))
        acc = account()
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=10,
            resulting_position_value=Decimal("1000"),
            projected_equity=Decimal("79999"),
            peak_equity=Decimal("100000"),
        )
        assert rejection is not None
        assert rejection.details["rule"] == "max_drawdown_pct"

    def test_sells_are_never_blocked_by_the_drawdown_guard(self):
        """A losing portfolio must always be able to de-risk itself."""
        manager = RiskManager(RiskLimits(max_drawdown_pct=Decimal("5")))
        acc = account(position_state=position())
        assert manager.validate_sell(acc, _sell(1, 10)) is None


# ----------------------------------------------------------------------
# G. Oversell protection
# ----------------------------------------------------------------------


class TestOversellProtection:
    def test_selling_without_a_position_is_rejected(self):
        manager = RiskManager(RiskLimits())
        rejection = manager.validate_sell(account(), _sell(1, 10))
        assert rejection is not None
        assert rejection.details["rule"] == "oversell_protection"
        assert rejection.details["owned_quantity"] == "0"

    def test_selling_more_than_held_is_rejected(self):
        manager = RiskManager(RiskLimits())
        acc = account(position_state=position(quantity=10))
        rejection = manager.validate_sell(acc, _sell(1, 11))
        assert rejection is not None
        assert rejection.details["owned_quantity"] == "10"

    def test_selling_exactly_what_is_held_is_accepted(self):
        manager = RiskManager(RiskLimits())
        acc = account(position_state=position(quantity=10))
        assert manager.validate_sell(acc, _sell(1, 10)) is None


# ----------------------------------------------------------------------
# H. Combined rules
# ----------------------------------------------------------------------


class TestCombinedRules:
    def test_all_violated_rules_are_reported_not_just_the_first(self):
        limits = RiskLimits(
            max_positions=1,
            max_exposure_pct=Decimal("50"),
            max_trade_allocation_pct=Decimal("5"),
            min_cash_reserve_pct=Decimal("50"),
        )
        manager = RiskManager(limits)
        acc = account(cash=Decimal("100000"), position_count=1)
        violations = manager.violations_for_buy(
            resulting_quantity=900,
            resulting_position_value=Decimal("90000"),
            projected_equity=Decimal("100000"),
            peak_equity=None,
            resulting_total_market_value=Decimal("90000"),
            cash_after=Decimal("10000"),
            trade_value=Decimal("90000"),
            open_position_count=1,
            opening_new_position=True,
        )
        rules = [violation.details["rule"] for violation in violations]
        assert rules == [
            "max_positions",
            "max_exposure_pct",
            "max_trade_allocation_pct",
            "min_cash_reserve_pct",
        ]

    def test_validate_buy_returns_the_first_violated_rule_in_order(self):
        """The engine reports one deterministic reason, so history is explainable."""
        limits = RiskLimits(max_positions=1, max_exposure_pct=Decimal("50"))
        manager = RiskManager(limits)
        acc = account(cash=Decimal("100000"), position_count=1)
        rejection = manager.validate_buy(
            acc,
            resulting_quantity=900,
            resulting_position_value=Decimal("90000"),
            projected_equity=Decimal("100000"),
            peak_equity=None,
            resulting_total_market_value=Decimal("90000"),
            cash_after=Decimal("10000"),
            trade_value=Decimal("90000"),
            open_position_count=1,
            opening_new_position=True,
        )
        assert rejection.details["rule"] == "max_positions"

    def test_drawdown_outranks_the_growth_rules(self):
        limits = RiskLimits(max_drawdown_pct=Decimal("20"), max_exposure_pct=Decimal("50"))
        manager = RiskManager(limits)
        rejection = manager.validate_buy(
            account(),
            resulting_quantity=900,
            resulting_position_value=Decimal("90000"),
            projected_equity=Decimal("70000"),
            peak_equity=Decimal("100000"),
            resulting_total_market_value=Decimal("90000"),
        )
        assert rejection.details["rule"] == "max_drawdown_pct"


# ----------------------------------------------------------------------
# Configuration validation
# ----------------------------------------------------------------------


class TestRiskLimitsValidation:
    @pytest.mark.parametrize(
        "limits",
        [
            RiskLimits(max_positions=0),
            RiskLimits(max_position_quantity=0),
            RiskLimits(max_exposure_pct=Decimal("0")),
            RiskLimits(max_exposure_pct=Decimal("101")),
            RiskLimits(max_trade_allocation_pct=Decimal("-1")),
            RiskLimits(min_cash_reserve_pct=Decimal("0")),
        ],
    )
    def test_invalid_limits_are_refused(self, limits):
        with pytest.raises(ValueError):
            limits.validate()

    @pytest.mark.parametrize(
        "limits",
        [
            RiskLimits(),
            RiskLimits(max_exposure_pct=Decimal("100")),
            RiskLimits(min_cash_reserve_pct=Decimal("100")),
            RiskLimits(max_positions=1),
        ],
    )
    def test_valid_limits_are_accepted(self, limits):
        limits.validate()

    def test_a_limit_of_100_percent_is_allowed_for_percentages(self):
        """``max_exposure_pct = 100`` means fully invested is legal, not "no limit"."""
        RiskLimits(max_exposure_pct=Decimal("100")).validate()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _rule_args(projection: dict, peak_equity) -> dict:
    """The keyword arguments the rule under test needs, taken from a projection."""
    return {
        "resulting_quantity": projection["resulting_quantity"],
        "resulting_position_value": projection["resulting_position_value"],
        "projected_equity": peak_equity,
        "peak_equity": peak_equity,
        "resulting_total_market_value": projection["resulting_total_market_value"],
        "cash_after": projection["cash_after"],
        "trade_value": projection["trade_value"],
    }


def _with_equity(projection: dict, equity: Decimal) -> dict:
    """Attach the percentages the tests assert on, computed independently."""
    from app.services.portfolio.calculator import allocation_pct

    enriched = dict(projection)
    enriched["projected_allocation_pct"] = allocation_pct(
        projection["resulting_position_value"], equity
    )
    enriched["projected_exposure_pct"] = allocation_pct(
        projection["resulting_total_market_value"], equity
    )
    enriched["projected_trade_allocation_pct"] = allocation_pct(
        projection["trade_value"], equity
    )
    return enriched


def _sell(stock_id: int, quantity: int):
    from app.services.portfolio.models import OrderIntent

    return OrderIntent(
        portfolio_id=1,
        stock_id=stock_id,
        side=OrderSide.SELL,
        quantity=quantity,
        requested_price=Decimal("100"),
    )