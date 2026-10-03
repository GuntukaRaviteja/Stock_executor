"""Unit tests for the Phase 9 paper-trading engine.

These tests exercise the pure layers — the calculator, the risk manager and the order
executor — with no FastAPI, no session and no database. Every business rule of Phase 9 has a
test here; the API-level behaviour is covered in ``test_portfolio_api.py``.
"""

from decimal import Decimal

import pytest

from app.schemas.portfolio import OrderStatus, PortfolioStatus, RejectionCode
from app.services.portfolio.models import RiskLimits
from app.services.portfolio.calculator import (
    allocation_pct,
    apply_buy,
    apply_sell,
    apply_slippage,
    calculate_fee,
    cost_basis,
    drawdown_pct,
    equity,
    gross_value,
    market_value,
    quantize_price,
    return_pct,
    size_quantity_from_cash,
    total_pnl,
    unrealized_pnl,
    value_position,
    weighted_average_entry_price,
)
from app.services.portfolio.executor import OrderExecutor
from app.services.portfolio.models import TradingCostConfig
from tests.portfolio_fixtures import (
    DEFAULT_COSTS,
    NO_FEES,
    account,
    buy_intent,
    expected_fee,
    position_state,
    price_series,
    quant,
    sell_intent,
)


# ======================================================================
# Trading cost model
# ======================================================================


class TestTradingCosts:
    def test_default_commission_is_the_configured_percentage(self):
        assert DEFAULT_COSTS.commission_rate == Decimal("0.001")

    def test_fee_is_percentage_of_gross_value(self):
        # 10 shares at 1000 => gross 10000 => 0.1% => 10.00
        assert calculate_fee(Decimal("10000"), DEFAULT_COSTS) == Decimal("10.00")

    def test_fee_is_rounded_half_up_once(self):
        # 3333.33 * 0.001 = 3.33333 => 3.33
        assert calculate_fee(Decimal("3333.33"), DEFAULT_COSTS) == Decimal("3.33")

    def test_minimum_fee_floor_applies_to_small_orders(self):
        costs = TradingCostConfig(commission_rate=Decimal("0.001"), min_fee=Decimal("5"))
        # 100 * 0.001 = 0.10, below the 5.00 floor
        assert calculate_fee(Decimal("100"), costs) == Decimal("5.00")

    def test_zero_gross_charges_no_fee(self):
        assert calculate_fee(Decimal("0"), DEFAULT_COSTS) == Decimal("0.00")

    def test_slippage_is_adverse_on_both_sides(self):
        costs = TradingCostConfig(slippage_rate=Decimal("0.01"))
        assert apply_slippage(Decimal("100"), True, costs) == Decimal("101.0000")
        assert apply_slippage(Decimal("100"), False, costs) == Decimal("99.0000")

    def test_no_slippage_fills_at_the_requested_price(self):
        assert apply_slippage(Decimal("123.45"), True, DEFAULT_COSTS) == Decimal("123.4500")

    def test_negative_cost_configuration_is_rejected(self):
        with pytest.raises(ValueError):
            TradingCostConfig(commission_rate=Decimal("-0.1")).validate()


# ======================================================================
# Portfolio creation arithmetic
# ======================================================================


class TestPortfolioInitialState:
    def test_cash_equity_and_pnl_start_at_initial_capital(self):
        """A new portfolio with 100,000 has 100,000 cash, equity and no P&L."""
        fresh = account(initial_capital=Decimal("100000"), cash=Decimal("100000"))

        assert fresh.cash == Decimal("100000")
        assert fresh.equity == Decimal("100000")
        assert total_pnl(fresh.equity, fresh.initial_capital) == Decimal("0.00")
        assert return_pct(Decimal("0"), fresh.initial_capital) == Decimal("0.0000")
        assert fresh.position is None

    def test_equity_is_cash_plus_position_market_value(self):
        state = account(cash=Decimal("50000"), position=position_state(quantity=10))
        assert state.equity == quant(Decimal("50000") + Decimal("1000"))

    def test_return_percentage_handles_zero_initial_capital(self):
        assert return_pct(Decimal("500"), Decimal("0")) == Decimal("0.0000")
        assert return_pct(Decimal("0"), Decimal("0")) == Decimal("0.0000")

    def test_return_percentage_of_a_ten_percent_gain(self):
        assert return_pct(Decimal("10000"), Decimal("100000")) == Decimal("10.0000")


# ======================================================================
# BUY execution
# ======================================================================


class TestBuyExecution:
    def test_successful_buy_reduces_cash_by_value_plus_fees(self):
        plan = OrderExecutor(DEFAULT_COSTS).execute(account(), buy_intent(quantity=10, price=Decimal("1000")))

        assert plan.accepted is True
        assert plan.order_status is OrderStatus.FILLED
        assert plan.gross_value == Decimal("10000.00")
        assert plan.fees == expected_fee(Decimal("10000"))
        assert plan.net_cash_impact == quant(-Decimal("10000") - plan.fees)
        assert plan.cash_after == quant(Decimal("100000") - Decimal("10000") - plan.fees)

    def test_successful_buy_creates_the_position(self):
        plan = OrderExecutor(NO_FEES).execute(account(), buy_intent(quantity=10, price=Decimal("1000")))

        assert plan.position_before is None
        assert plan.position_after.quantity == 10
        assert plan.position_after.average_entry_price == Decimal("1000.0000")
        assert plan.position_after.cost_basis == Decimal("10000.00")

    def test_buy_realized_pnl_is_zero(self):
        plan = OrderExecutor(DEFAULT_COSTS).execute(account(), buy_intent())
        assert plan.realized_pnl == Decimal("0")

    def test_cash_never_goes_negative(self):
        executor = OrderExecutor(NO_FEES)
        plan = executor.execute(
            account(cash=Decimal("500")), buy_intent(quantity=5, price=Decimal("1000"))
        )
        assert plan.accepted is False
        assert plan.cash_after == Decimal("500")


# ======================================================================
# Multiple buys / weighted average price
# ======================================================================


class TestWeightedAverageEntryPrice:
    def test_two_buys_average_to_their_weighted_mean(self):
        """Buy 10 @ 100 then 10 @ 120 => average 110."""
        executor = OrderExecutor(NO_FEES)
        state = account()
        first = executor.execute(state, buy_intent(quantity=10, price=Decimal("100")))
        state = account(cash=first.cash_after, position=first.position_after)
        second = executor.execute(state, buy_intent(quantity=10, price=Decimal("120")))

        assert second.position_after.quantity == 20
        assert second.position_after.average_entry_price == Decimal("110.0000")
        assert second.position_after.cost_basis == Decimal("2200.00")

    def test_average_is_quantity_weighted_not_arithmetic(self):
        """1 share at 100 and 3 shares at 200 => 175, not 150."""
        executor = OrderExecutor(NO_FEES)
        first = executor.execute(account(), buy_intent(quantity=1, price=Decimal("100")))
        state = account(cash=first.cash_after, position=first.position_after)
        second = executor.execute(state, buy_intent(quantity=3, price=Decimal("200")))

        assert second.position_after.average_entry_price == Decimal("175.0000")

    def test_weighted_average_helper_is_exact_for_the_documented_example(self):
        assert weighted_average_entry_price(
            10, Decimal("100"), 10, Decimal("120")
        ) == Decimal("110.0000")

    def test_average_of_a_flat_position_is_the_execution_price(self):
        assert weighted_average_entry_price(0, Decimal("0"), 10, Decimal("123.45")) == Decimal(
            "123.4500"
        )

    def test_entry_fees_accumulate_without_changing_the_average(self):
        executor = OrderExecutor(DEFAULT_COSTS)
        plan = executor.execute(account(), buy_intent(quantity=10, price=Decimal("100")))
        assert plan.position_after.entry_fees_total == plan.fees
        assert plan.position_after.average_entry_price == Decimal("100.0000")


# ======================================================================
# SELL execution
# ======================================================================


class TestSellExecution:
    def test_successful_sell_increases_cash_by_value_minus_fees(self):
        executor = OrderExecutor(DEFAULT_COSTS)
        position = position_state(quantity=10, average_entry_price=Decimal("1000"))
        state = account(cash=Decimal("0"), position=position)

        plan = executor.execute(state, sell_intent(quantity=10, price=Decimal("1100")))

        assert plan.accepted is True
        assert plan.gross_value == Decimal("11000.00")
        assert plan.fees == expected_fee(Decimal("11000"))
        assert plan.cash_after == quant(Decimal("11000") - plan.fees)

    def test_full_exit_removes_the_position(self):
        plan = OrderExecutor(NO_FEES).execute(
            account(cash=Decimal("0"), position=position_state(quantity=10)),
            sell_intent(quantity=10, price=Decimal("1100")),
        )
        assert plan.position_after is None

    def test_partial_exit_keeps_the_remaining_quantity(self):
        plan = OrderExecutor(NO_FEES).execute(
            account(cash=Decimal("0"), position=position_state(quantity=20)),
            sell_intent(quantity=5, price=Decimal("1100")),
        )
        assert plan.position_after.quantity == 15

    def test_partial_exit_does_not_reset_the_average_price(self):
        """The remaining shares keep the average cost of the original position."""
        plan = OrderExecutor(NO_FEES).execute(
            account(cash=Decimal("0"), position=position_state(quantity=20, average_entry_price=Decimal("100"))),
            sell_intent(quantity=5, price=Decimal("130")),
        )
        assert plan.position_after.average_entry_price == Decimal("100.0000")
        assert plan.position_after.cost_basis == Decimal("1500.00")

    def test_realized_pnl_is_calculated_only_for_the_sold_quantity(self):
        """Sell 5 of 15 shares bought at 100, at 130 => 5 * 30 = 150."""
        plan = OrderExecutor(NO_FEES).execute(
            account(cash=Decimal("0"), position=position_state(quantity=15, average_entry_price=Decimal("100"))),
            sell_intent(quantity=5, price=Decimal("130")),
        )
        assert plan.realized_pnl == Decimal("150.00")

    def test_realized_pnl_excludes_entry_fees_of_the_shares_being_sold(self):
        """A position bought with 1.00 of entry fees realises 0.99 less on a full exit."""
        position = position_state(
            quantity=10, average_entry_price=Decimal("100"), entry_fees_total=Decimal("1.00")
        )
        plan = OrderExecutor(NO_FEES).execute(
            account(cash=Decimal("0"), position=position), sell_intent(quantity=10, price=Decimal("110"))
        )
        # gross 100.00 - 1.00 of entry fees allocated to the sold shares
        assert plan.realized_pnl == Decimal("99.00")

    def test_entry_fees_are_allocated_pro_rata_on_a_partial_exit(self):
        position = position_state(
            quantity=10, average_entry_price=Decimal("100"), entry_fees_total=Decimal("10.00")
        )
        plan = OrderExecutor(NO_FEES).execute(
            account(cash=Decimal("0"), position=position), sell_intent(quantity=5, price=Decimal("110"))
        )
        assert plan.realized_pnl == Decimal("50.00") - Decimal("5.00")
        assert plan.position_after.entry_fees_total == Decimal("5.00")

    def test_selling_more_than_owned_is_rejected(self):
        plan = OrderExecutor(NO_FEES).execute(
            account(cash=Decimal("0"), position=position_state(quantity=5)),
            sell_intent(quantity=6),
        )
        assert plan.accepted is False
        assert plan.rejection.code is RejectionCode.INSUFFICIENT_POSITION

    def test_selling_from_a_flat_portfolio_is_rejected(self):
        plan = OrderExecutor(NO_FEES).execute(account(), sell_intent(quantity=1))
        assert plan.accepted is False
        assert plan.rejection.code is RejectionCode.INSUFFICIENT_POSITION

    def test_quantity_can_never_go_negative(self):
        position = position_state(quantity=5)
        _, realized, _ = apply_sell(position, 3, Decimal("100"), Decimal("0"))
        assert position.quantity == 5
        assert realized == Decimal("0.00")

    def test_selling_beyond_the_position_raises_in_the_calculator(self):
        with pytest.raises(ValueError):
            apply_sell(position_state(quantity=5), 6, Decimal("100"), Decimal("0"))


# ======================================================================
# Rejections: cash, state, inputs
# ======================================================================


class TestRejections:
    def test_insufficient_cash_is_rejected_without_mutating_state(self):
        state = account(cash=Decimal("1000"), position=position_state(quantity=1))
        plan = OrderExecutor(NO_FEES).execute(state, buy_intent(quantity=10, price=Decimal("1000")))

        assert plan.accepted is False
        assert plan.rejection.code is RejectionCode.INSUFFICIENT_CASH
        assert plan.cash_after == state.cash
        assert plan.position_after == state.position
        assert plan.fees == Decimal("0")
        assert plan.gross_value == Decimal("0")

    def test_insufficient_cash_accounts_for_fees(self):
        """Exactly enough cash for the value but not the fee must still be rejected."""
        plan = OrderExecutor(DEFAULT_COSTS).execute(
            account(cash=Decimal("10000")), buy_intent(quantity=10, price=Decimal("1000"))
        )
        assert plan.accepted is False
        assert plan.rejection.code is RejectionCode.INSUFFICIENT_CASH

    def test_cash_exactly_covering_value_plus_fees_is_accepted(self):
        executor = OrderExecutor(NO_FEES)
        total = quant(Decimal("1000") * Decimal("10"))
        plan = executor.execute(account(cash=total), buy_intent(quantity=10, price=Decimal("1000")))
        assert plan.accepted is True
        assert plan.cash_after == Decimal("0.00")

    def test_paused_portfolio_rejects_orders(self):
        plan = OrderExecutor(NO_FEES).execute(
            account(status=PortfolioStatus.PAUSED), buy_intent()
        )
        assert plan.accepted is False
        assert plan.rejection.code is RejectionCode.PORTFOLIO_INACTIVE

    def test_closed_portfolio_rejects_orders(self):
        plan = OrderExecutor(NO_FEES).execute(
            account(status=PortfolioStatus.CLOSED), buy_intent()
        )
        assert plan.accepted is False
        assert plan.rejection.code is RejectionCode.PORTFOLIO_INACTIVE

    def test_non_positive_quantity_is_rejected(self):
        plan = OrderExecutor(NO_FEES).execute(account(), buy_intent(quantity=0))
        assert plan.accepted is False
        assert plan.rejection.code is RejectionCode.INVALID_QUANTITY

    def test_non_positive_price_is_rejected(self):
        plan = OrderExecutor(NO_FEES).execute(account(), buy_intent(price=Decimal("0")))
        assert plan.accepted is False
        assert plan.rejection.code is RejectionCode.INVALID_PRICE

    def test_every_rejection_carries_a_reason_and_rule(self):
        plan = OrderExecutor(NO_FEES).execute(account(cash=Decimal("1")), buy_intent())
        assert plan.rejection.reason
        assert plan.rejection.details["rule"]


# ======================================================================
# Risk controls
# ======================================================================


class TestRiskControls:
    def test_max_position_quantity_blocks_an_oversized_buy(self):
        limits = RiskLimits(max_position_quantity=5)
        plan = OrderExecutor(NO_FEES, limits).execute(account(), buy_intent(quantity=6, price=Decimal("10")))

        assert plan.accepted is False
        assert plan.rejection.code is RejectionCode.MAX_POSITION_QUANTITY_EXCEEDED
        assert plan.rejection.details["rule"] == "max_position_quantity"
        assert plan.cash_after == Decimal("100000")

    def test_max_position_quantity_allows_exactly_the_limit(self):
        limits = RiskLimits(max_position_quantity=5)
        plan = OrderExecutor(NO_FEES, limits).execute(
            account(), buy_intent(quantity=5, price=Decimal("10"))
        )
        assert plan.accepted is True

    def test_max_position_value_blocks_an_oversized_position(self):
        limits = RiskLimits(max_position_value=Decimal("1000"))
        plan = OrderExecutor(NO_FEES, limits).execute(
            account(), buy_intent(quantity=20, price=Decimal("100"))
        )
        assert plan.accepted is False
        assert plan.rejection.details["rule"] == "max_position_value"

    def test_max_allocation_blocks_an_over_concentrated_buy(self):
        """20% of a 100,000 portfolio caps a position at 20,000."""
        limits = RiskLimits(max_position_allocation_pct=Decimal("20"))
        plan = OrderExecutor(NO_FEES, limits).execute(
            account(), buy_intent(quantity=300, price=Decimal("100"))
        )
        assert plan.accepted is False
        assert plan.rejection.details["rule"] == "max_position_allocation_pct"

    def test_max_allocation_allows_a_position_inside_the_limit(self):
        limits = RiskLimits(max_position_allocation_pct=Decimal("20"))
        plan = OrderExecutor(NO_FEES, limits).execute(
            account(), buy_intent(quantity=100, price=Decimal("100"))
        )
        assert plan.accepted is True

    def test_allocation_is_measured_against_post_fill_equity(self):
        limits = RiskLimits(max_position_allocation_pct=Decimal("50"))
        plan = OrderExecutor(NO_FEES, limits).execute(
            account(), buy_intent(quantity=500, price=Decimal("100"))
        )
        # 50,000 in a portfolio that still holds 100,000 of equity => exactly 50% is allowed
        assert plan.accepted is True
        assert plan.resulting_allocation_pct == Decimal("50.0000")

    def test_allocation_of_a_flat_portfolio_is_measured_against_total_equity(self):
        """Regression: the position being bought must not be counted twice in the basis."""
        plan = OrderExecutor(NO_FEES).execute(
            account(), buy_intent(quantity=500, price=Decimal("100"))
        )
        assert plan.resulting_allocation_pct == Decimal("50.0000")

    def test_allocation_accounts_for_the_existing_position_being_increased(self):
        held = account(
            cash=Decimal("90000"),
            position=position_state(quantity=100, average_entry_price=Decimal("100")),
        )
        plan = OrderExecutor(NO_FEES).execute(
            held, buy_intent(quantity=100, price=Decimal("100"))
        )
        # 200 shares worth 20,000 in an unchanged 100,000 of equity
        assert plan.resulting_allocation_pct == Decimal("20.0000")

    def test_drawdown_rule_blocks_new_buys(self):
        limits = RiskLimits(max_drawdown_pct=Decimal("10"))
        state = account(cash=Decimal("1000"), total_market_value=Decimal("80000"))
        plan = OrderExecutor(NO_FEES, limits).execute(
            state, buy_intent(quantity=1, price=Decimal("10")), peak_equity=Decimal("100000")
        )
        assert plan.accepted is False
        assert plan.rejection.details["rule"] == "max_drawdown_pct"

    def test_drawdown_rule_does_not_block_sells(self):
        """A losing portfolio must always be able to de-risk."""
        limits = RiskLimits(max_drawdown_pct=Decimal("10"))
        state = account(
            cash=Decimal("1000"),
            position=position_state(quantity=800),
            total_market_value=Decimal("80000"),
        )
        plan = OrderExecutor(NO_FEES, limits).execute(
            state, sell_intent(quantity=800, price=Decimal("100")), peak_equity=Decimal("100000")
        )
        assert plan.accepted is True

    def test_drawdown_rule_is_disabled_when_unset(self):
        state = account(cash=Decimal("1000"), total_market_value=Decimal("80000"))
        plan = OrderExecutor(NO_FEES).execute(
            state, buy_intent(quantity=1, price=Decimal("10")), peak_equity=Decimal("100000")
        )
        assert plan.accepted is True

    def test_sell_is_never_blocked_by_position_growth_limits(self):
        limits = RiskLimits(max_position_quantity=1, max_position_value=Decimal("1"))
        state = account(cash=Decimal("0"), position=position_state(quantity=50))
        plan = OrderExecutor(NO_FEES, limits).execute(state, sell_intent(quantity=10, price=Decimal("100")))
        assert plan.accepted is True

    def test_invalid_limit_configuration_is_rejected(self):
        with pytest.raises(ValueError):
            RiskLimits(max_position_quantity=0).validate()
        with pytest.raises(ValueError):
            RiskLimits(max_drawdown_pct=Decimal("0")).validate()


# ======================================================================
# Valuation, market value and P&L arithmetic
# ======================================================================


class TestValuation:
    def test_market_value_and_unrealized_pnl(self):
        """10 shares, average 100, marked at 115 => value 1150, unrealized 150."""
        value = market_value(10, Decimal("115"))
        assert value == Decimal("1150.00")
        assert unrealized_pnl(10, Decimal("100"), Decimal("115")) == Decimal("150.00")

    def test_unrealized_pnl_is_zero_at_the_average_price(self):
        assert unrealized_pnl(10, Decimal("100"), Decimal("100")) == Decimal("0.00")

    def test_unrealized_pnl_is_negative_for_a_losing_position(self):
        assert unrealized_pnl(10, Decimal("100"), Decimal("90")) == Decimal("-100.00")

    def test_allocation_percentage(self):
        assert allocation_pct(Decimal("25000"), Decimal("100000")) == Decimal("25.0000")

    def test_allocation_is_undefined_for_non_positive_equity(self):
        assert allocation_pct(Decimal("100"), Decimal("0")) is None

    def test_drawdown_percentage(self):
        assert drawdown_pct(Decimal("1000"), Decimal("900")) == Decimal("10.0000")
        assert drawdown_pct(Decimal("1000"), Decimal("1100")) == Decimal("0.0000")
        assert drawdown_pct(Decimal("0"), Decimal("100")) == Decimal("0.0000")

    def test_value_position_falls_back_to_the_last_known_price(self):
        valued = value_position(position_state(quantity=10, current_price=Decimal("100")), None)
        assert valued.current_price == Decimal("100.0000")
        assert valued.unrealized_pnl == Decimal("0.00")

    def test_unmarked_position_is_valued_at_cost_rather_than_an_invented_move(self):
        valued = value_position(position_state(quantity=10, current_price=None), None)
        assert valued.current_price == Decimal("100.0000")
        assert valued.unrealized_pnl == Decimal("0.00")

    def test_cost_basis_is_quantity_times_average(self):
        assert cost_basis(15, Decimal("110.5")) == Decimal("1657.50")


# ======================================================================
# Position sizing
# ======================================================================


class TestPositionSizing:
    def test_budget_is_floored_to_whole_shares(self):
        quantity = size_quantity_from_cash(
            Decimal("100000"), Decimal("10"), Decimal("300"), NO_FEES
        )
        assert quantity == 33  # 10,000 / 300 = 33.33

    def test_fees_are_included_in_the_unit_cost(self):
        quantity = size_quantity_from_cash(
            Decimal("100000"), Decimal("10"), Decimal("300"), DEFAULT_COSTS
        )
        assert quantity == 33  # 10,000 / 300.30 = 33.3

    def test_sizing_never_exceeds_the_available_cash(self):
        quantity = size_quantity_from_cash(
            Decimal("1000"), Decimal("100"), Decimal("300"), DEFAULT_COSTS
        )
        cost = (Decimal("300") * Decimal("300.30")).quantize(Decimal("0.01"))
        assert quantity * Decimal("300") + expected_fee(Decimal("300") * Decimal(quantity)) <= Decimal(
            "1000"
        )
        assert cost > Decimal("900")

    def test_no_affordable_shares_returns_zero(self):
        assert size_quantity_from_cash(Decimal("10"), Decimal("10"), Decimal("1000"), NO_FEES) == 0
        assert size_quantity_from_cash(Decimal("0"), Decimal("100"), Decimal("10"), NO_FEES) == 0

    def test_zero_price_returns_zero(self):
        assert size_quantity_from_cash(Decimal("1000"), Decimal("100"), Decimal("0"), NO_FEES) == 0


# ======================================================================
# Decimal precision
# ======================================================================


class TestDecimalPrecision:
    def test_repeated_thirds_never_produce_a_float_artefact(self):
        """A float-based engine would drift here; Decimal must stay exact."""
        plan = OrderExecutor(NO_FEES).execute(
            account(), buy_intent(quantity=3, price=Decimal("333.33"))
        )
        assert plan.gross_value == Decimal("999.99")
        assert plan.cash_after == Decimal("99000.01")

    def test_large_amounts_stay_exact(self):
        """A large notional stays exact; a float engine would lose the trailing cents."""
        plan = OrderExecutor(NO_FEES).execute(
            account(cash=Decimal("2000000000")),
            buy_intent(quantity=1_000_000, price=Decimal("1234.5678")),
        )
        assert plan.accepted is True
        assert plan.gross_value == Decimal("1234567800.00")

    def test_pnl_identity_holds_for_every_price_in_a_series(self):
        """realized + unrealized - open entry fees == total_pnl, for every price in the sweep."""
        executor = OrderExecutor(DEFAULT_COSTS)
        for price in price_series(Decimal("100.01"), Decimal("7.77"), 12):
            first = executor.execute(account(), buy_intent(quantity=7, price=Decimal("100.11")))
            held = account(cash=first.cash_after, position=first.position_after)
            second = executor.execute(held, sell_intent(quantity=4, price=price))

            assert second.accepted is True
            equity_after = equity(
                second.cash_after,
                market_value(second.position_after.quantity, price),
            )
            unrealized = unrealized_pnl(
                second.position_after.quantity, second.position_after.average_entry_price, price
            )
            assert (
                second.realized_pnl + unrealized - second.position_after.entry_fees_total
                == total_pnl(equity_after, Decimal("100000"))
            )

    def test_full_round_trip_realized_pnl_equals_the_cash_change(self):
        """After a full exit, the reported realised P&L is exactly what cash gained."""
        executor = OrderExecutor(DEFAULT_COSTS)
        first = executor.execute(account(), buy_intent(quantity=10, price=Decimal("1000")))
        held = account(cash=first.cash_after, position=first.position_after)
        second = executor.execute(held, sell_intent(quantity=10, price=Decimal("1100")))

        assert second.position_after is None
        assert second.cash_after - Decimal("100000") == second.realized_pnl

    def test_realized_plus_unrealized_reconciles_to_total_pnl_for_a_partial_exit(self):
        executor = OrderExecutor(DEFAULT_COSTS)
        first = executor.execute(account(), buy_intent(quantity=10, price=Decimal("1000")))
        held = account(cash=first.cash_after, position=first.position_after)
        second = executor.execute(held, sell_intent(quantity=4, price=Decimal("1050")))

        equity_after = equity(
            second.cash_after,
            market_value(second.position_after.quantity, Decimal("1000")),
        )
        unrealized = unrealized_pnl(6, second.position_after.average_entry_price, Decimal("1000"))
        assert (
            second.realized_pnl + unrealized - second.position_after.entry_fees_total
            == total_pnl(equity_after, Decimal("100000"))
        )

    def test_no_float_is_produced_anywhere_in_the_money_path(self):
        plan = OrderExecutor(DEFAULT_COSTS).execute(
            account(), buy_intent(quantity=3, price=Decimal("10.10"))
        )
        for value in (plan.gross_value, plan.fees, plan.cash_after, plan.realized_pnl):
            assert isinstance(value, Decimal)

    def test_prices_are_quantised_to_the_stored_scale(self):
        assert quantize_price(Decimal("100.123456")) == Decimal("100.1235")