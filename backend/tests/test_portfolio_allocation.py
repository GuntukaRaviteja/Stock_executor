"""Pure-unit tests for the Phase 11 money math — no database, no FastAPI.

These cover the arithmetic the whole phase rests on, and they assert the *boundary* behaviour
explicitly (inclusive stops, exclusive loss limits, reserved cash) because that is where an
off-by-one would silently change a position size.

Amounts are written as exact ``Decimal`` strings and every expected figure is derived from the
rule under test rather than copied from a run, so a failure points at the code, not at noise.
"""

from decimal import Decimal

import pytest

from app.services.portfolio.allocation import (
    allocate_equal_weights,
    budget_for_weight,
    capped_target_pct,
    equal_weight_target_pct,
    gain_pct,
    loss_pct,
    normalise_weights,
    position_loss_breached,
    risk_amount,
    risk_based_quantity,
    risk_budget,
    stop_loss_breached,
    stop_price,
    take_profit_price,
    take_profit_triggered,
    target_quantity_for_weight,
    weight_difference_pct,
)
from app.services.portfolio.calculator import (
    apply_deposit,
    apply_withdrawal,
    cash_after_deposit_or_withdrawal,
)
from app.services.portfolio.models import TradingCostConfig

D = Decimal

#: Zero-cost model, so a test asserts the arithmetic and not the fee schedule.
NO_FEES = TradingCostConfig(
    commission_rate=D("0"), min_fee=D("0"), slippage_rate=D("0")
)


# --- Cash movements ---------------------------------------------------------


class TestCashMovements:
    def test_a_deposit_adds_exactly_the_amount(self):
        assert apply_deposit(D("1000.00"), D("250.50")).net_cash_impact == D("250.50")

    def test_a_withdrawal_removes_exactly_the_amount(self):
        assert apply_withdrawal(D("1000.00"), D("250.50")).net_cash_impact == D("-250.50")

    def test_cash_after_a_deposit_is_the_sum(self):
        assert apply_deposit(D("1000.00"), D("250.50")).cash_after == D("1250.50")

    def test_cash_after_a_withdrawal_is_the_difference(self):
        assert apply_withdrawal(D("1000.00"), D("250.50")).cash_after == D("749.50")

    def test_a_withdrawal_beyond_the_balance_is_refused(self):
        with pytest.raises(ValueError, match="exceeds the available cash"):
            apply_withdrawal(D("100.00"), D("100.01"))

    def test_withdrawing_the_whole_balance_is_allowed(self):
        assert apply_withdrawal(D("100.00"), D("100.00")).cash_after == D("0.00")

    def test_the_direction_is_reported(self):
        assert apply_deposit(D("10"), D("5")).movement_type == "DEPOSIT"
        assert apply_withdrawal(D("10"), D("5")).movement_type == "WITHDRAWAL"

    def test_a_movement_carries_no_position_and_no_pnl(self):
        # A cash movement is not a trade. The dataclass has no stock, quantity, price or
        # realised-P&L field at all, which is the strongest form of that guarantee: there is
        # nowhere for a cash movement to record a position or a profit it did not earn.
        movement = apply_deposit(D("10"), D("5"))
        assert not hasattr(movement, "realized_pnl")
        assert not hasattr(movement, "stock_id")
        assert not hasattr(movement, "quantity")

    @pytest.mark.parametrize("amount", ["0", "-1"])
    def test_a_non_positive_amount_is_refused(self, amount):
        for movement in (apply_deposit, apply_withdrawal):
            with pytest.raises(ValueError):
                movement(D("100"), D(amount))

    def test_the_helper_agrees_with_the_movements(self):
        assert cash_after_deposit_or_withdrawal(D("100"), D("40")) == D("140.00")
        assert cash_after_deposit_or_withdrawal(D("100"), D("-40")) == D("60.00")


# --- Equal-weight allocation ------------------------------------------------


class TestEqualWeights:
    def test_the_weight_splits_across_the_candidate_count(self):
        assert equal_weight_target_pct(4) == D("25.0000")

    def test_one_name_is_the_whole_portfolio(self):
        assert equal_weight_target_pct(1) == D("100.0000")

    def test_the_split_truncates_rather_than_rounds(self):
        # 100/3 = 33.3333..., truncated: the engine must never promise a fraction of a
        # percentage the allocator cannot deliver.
        assert equal_weight_target_pct(3) == D("33.3333")

    def test_no_candidates_allocates_nothing(self):
        assert equal_weight_target_pct(0) == D("0.0000")

    def test_a_cap_below_the_equal_weight_truncates_it(self):
        assert capped_target_pct(D("25"), D("20")) == D("20.0000")

    def test_a_cap_above_the_equal_weight_leaves_it_alone(self):
        assert capped_target_pct(D("25"), D("40")) == D("25.0000")

    def test_the_cap_is_applied_to_the_split_not_before_it(self):
        # Two candidates would be 50% each; the 20% cap trims both. The surplus is left
        # uninvested rather than redistributed, so the caps cannot be evaded by pairing a big
        # target with a small one.
        weights = allocate_equal_weights([1, 2], max_position_weight_pct=D("20"))
        assert weights == {1: D("20.0000"), 2: D("20.0000")}

    def test_allocation_is_keyed_by_stock_id_and_ordered(self):
        assert list(allocate_equal_weights([3, 1, 2], max_position_weight_pct=D("100"))) == [1, 2, 3]

    def test_duplicate_candidates_are_counted_once(self):
        weights = allocate_equal_weights([1, 1, 2], max_position_weight_pct=D("100"))
        assert weights == {1: D("50.0000"), 2: D("50.0000")}

    def test_a_target_count_selects_the_first_n(self):
        weights = allocate_equal_weights(
            [1, 2, 3], max_position_weight_pct=D("100"), target_count=2
        )
        assert weights == {1: D("50.0000"), 2: D("50.0000")}

    def test_no_candidates_allocate_nothing(self):
        assert allocate_equal_weights([], max_position_weight_pct=D("20")) == {}

    def test_a_zero_target_count_allocates_nothing(self):
        assert allocate_equal_weights([1, 2], max_position_weight_pct=D("20"), target_count=0) == {}


class TestNormalisedWeights:
    def test_weights_are_accepted_in_percent(self):
        assert normalise_weights({1: D("30"), 2: D("70")}) == {1: D("30.0000"), 2: D("70.0000")}

    def test_nothing_to_normalise_gives_nothing(self):
        assert normalise_weights({}) == {}

    def test_a_negative_weight_is_refused(self):
        with pytest.raises(ValueError, match="must not be negative"):
            normalise_weights({1: D("-1")})

    def test_a_weight_above_one_hundred_is_refused(self):
        with pytest.raises(ValueError, match="at most 100"):
            normalise_weights({1: D("101")})

    def test_over_allocation_is_refused_rather_than_scaled_down(self):
        # Scaling would make the executed portfolio differ from the requested one, which is the
        # one thing a caller cannot detect afterwards.
        with pytest.raises(ValueError, match="above the 90% allocation limit"):
            normalise_weights({1: D("50"), 2: D("50")}, max_total_pct=D("90"))

    def test_exactly_the_limit_is_accepted(self):
        assert normalise_weights({1: D("90")}, max_total_pct=D("90")) == {1: D("90.0000")}

    def test_stock_ids_are_coerced_to_integers(self):
        assert normalise_weights({"1": D("10")}) == {1: D("10.0000")}


class TestDrift:
    def test_drift_is_current_minus_target(self):
        assert weight_difference_pct(D("30"), D("25")) == D("5.0000")

    def test_an_underweight_position_has_negative_drift(self):
        assert weight_difference_pct(D("10"), D("25")) == D("-15.0000")

    def test_drift_is_none_without_a_measurable_current_weight(self):
        assert weight_difference_pct(None, D("25")) is None


# --- Quantity from a weight -------------------------------------------------


class TestTargetQuantity:
    def test_a_weight_becomes_a_whole_share_count(self):
        assert target_quantity_for_weight(D("40000"), D("25"), D("100"), NO_FEES) == 100

    def test_quantities_are_floored_never_rounded_up(self):
        # 3333 of a 100% weight at 100 a share is 33.33 shares -> 33. Rounding up would buy more
        # than the target and silently breach the cap it came from.
        assert target_quantity_for_weight(D("3333"), D("100"), D("100"), NO_FEES) == 33

    def test_a_budget_too_small_for_one_share_is_zero(self):
        assert target_quantity_for_weight(D("50"), D("100"), D("100"), NO_FEES) == 0

    @pytest.mark.parametrize("price", ["0", "-1"])
    def test_a_non_positive_price_buys_nothing(self, price):
        assert target_quantity_for_weight(D("1000"), D("50"), D(price), NO_FEES) == 0

    @pytest.mark.parametrize(
        "equity,weight",
        [("0", "50"), ("-1", "50"), ("1000", "0"), ("1000", "-5")],
    )
    def test_a_non_positive_equity_or_weight_buys_nothing(self, equity, weight):
        assert target_quantity_for_weight(D(equity), D(weight), D("100"), NO_FEES) == 0

    def test_the_budget_of_a_weight_is_its_share_of_equity(self):
        assert budget_for_weight(D("100000"), D("25")) == D("25000.00")

    def test_the_budget_of_no_equity_is_nothing(self):
        assert budget_for_weight(D("0"), D("25")) == D("0.00")


# --- Stops ------------------------------------------------------------------


class TestStops:
    def test_a_stop_is_the_configured_percentage_below_the_entry(self):
        assert stop_price(D("200"), D("10")) == D("180.0000")

    def test_a_take_profit_is_the_configured_percentage_above_the_entry(self):
        assert take_profit_price(D("200"), D("25")) == D("250.0000")

    def test_a_price_at_the_stop_triggers_it(self):
        # Inclusive: the price that reaches the stop *is* the stop, so an exclusive comparison
        # would only fire on a price the position had already passed through.
        assert stop_loss_breached(D("200"), D("180"), D("10")) is True

    def test_a_price_below_the_stop_triggers_it(self):
        assert stop_loss_breached(D("200"), D("170"), D("10")) is True

    def test_a_price_above_the_stop_does_not(self):
        assert stop_loss_breached(D("200"), D("180.01"), D("10")) is False

    def test_a_price_at_the_take_profit_triggers_it(self):
        assert take_profit_triggered(D("200"), D("250"), D("25")) is True

    def test_a_price_below_the_take_profit_does_not(self):
        assert take_profit_triggered(D("200"), D("249.99"), D("25")) is False

    def test_no_configured_stop_never_triggers(self):
        assert stop_loss_breached(D("200"), D("1"), None) is False

    def test_no_configured_take_profit_never_triggers(self):
        assert take_profit_triggered(D("200"), D("9999"), None) is False

    def test_no_configured_stop_has_no_price(self):
        assert stop_price(D("200"), None) is None

    def test_no_configured_take_profit_has_no_price(self):
        assert take_profit_price(D("200"), None) is None


# --- Loss and gain percentages ----------------------------------------------


class TestLossAndGain:
    def test_loss_is_the_percentage_below_the_entry(self):
        assert loss_pct(D("200"), D("180")) == D("10.0000")

    def test_gain_is_the_percentage_above_the_entry(self):
        assert gain_pct(D("200"), D("250")) == D("25.0000")

    def test_a_gain_reports_no_loss(self):
        assert loss_pct(D("200"), D("250")) == D("0.0000")

    def test_a_loss_reports_no_gain(self):
        assert gain_pct(D("200"), D("180")) == D("-10.0000")

    def test_a_position_at_its_entry_has_no_loss(self):
        assert loss_pct(D("200"), D("200")) == D("0.0000")

    def test_a_zero_entry_price_has_no_measurable_return(self):
        # The ratio is undefined rather than infinite, so the engine reports "unknown" instead
        # of a number it would then have to special-case everywhere.
        assert gain_pct(D("0"), D("180")) is None
        assert loss_pct(D("0"), D("180")) is None

    def test_a_loss_beyond_the_tolerance_breaches_it(self):
        # 200 -> 189 is 5.5%, beyond a 5% tolerance.
        assert position_loss_breached(D("200"), D("189"), D("5")) is True

    def test_a_loss_exactly_at_the_tolerance_is_inside_it(self):
        # Exclusive: this limit is a tolerance ("may lose no more than 10%"), so exactly 10% is
        # not more than 10%.
        assert position_loss_breached(D("200"), D("190"), D("10")) is False

    def test_no_tolerance_configured_means_no_breach(self):
        assert position_loss_breached(D("200"), D("100"), None) is False

    def test_an_unmeasurable_loss_cannot_breach(self):
        assert position_loss_breached(D("0"), D("100"), D("5")) is False


# --- Risk budget and risk-based sizing --------------------------------------


class TestRiskSizing:
    def test_the_budget_is_the_configured_share_of_equity(self):
        assert risk_budget(D("100000"), D("1")) == D("1000.00")

    def test_the_budget_of_no_equity_is_nothing(self):
        assert risk_budget(D("0"), D("1")) == D("0.00")

    def test_the_risk_of_a_position_is_its_distance_to_the_stop(self):
        # 50 shares from 200 down to 180 is 50 * 20 of loss.
        assert risk_amount(50, D("200"), D("10")) == D("1000.00")

    def test_a_position_above_its_stop_carries_no_risk(self):
        # Not negative risk: closing at a profit books a gain, and "risk to stop" is a loss
        # figure floored at zero.
        assert risk_amount(50, D("200"), D("-10")) == D("0.00")

    def test_no_stop_carries_no_risk(self):
        assert risk_amount(50, D("200"), None) == D("0.00")

    def test_no_shares_carry_no_risk(self):
        assert risk_amount(0, D("200"), D("10")) == D("0.00")

    def test_risk_based_sizing_divides_the_budget_by_the_risk_per_share(self):
        # 1% of 100000 = 1000 of risk; a 10% stop on a 200 entry is 20 per share -> 50 shares.
        assert risk_based_quantity(D("100000"), D("1"), D("200"), D("10"), NO_FEES) == 50

    def test_risk_based_sizing_is_capped_by_the_cash_available(self):
        # 50 shares at 200 would cost 10000; only 2000 is fundable -> 10 shares.
        assert (
            risk_based_quantity(
                D("100000"), D("1"), D("200"), D("10"), NO_FEES, max_capital=D("2000")
            )
            == 10
        )

    def test_the_cap_never_raises_the_risk_answer(self):
        # Cash that can fund more than the risk budget allows does not buy more: the risk budget
        # is a *risk* limit, not a target.
        assert (
            risk_based_quantity(
                D("100000"), D("1"), D("200"), D("10"), NO_FEES, max_capital=D("1000000")
            )
            == 50
        )

    def test_no_configured_stop_gives_no_size(self):
        # Without a stop there is no per-share risk, so no defensible size exists.
        assert risk_based_quantity(D("100000"), D("1"), D("200"), None, NO_FEES) == 0

    def test_a_stop_at_or_above_the_entry_gives_no_size(self):
        assert risk_based_quantity(D("100000"), D("1"), D("200"), D("0"), NO_FEES) == 0

    def test_no_budget_gives_no_size(self):
        assert risk_based_quantity(D("0"), D("1"), D("200"), D("10"), NO_FEES) == 0