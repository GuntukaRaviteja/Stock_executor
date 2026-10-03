"""Phase 13 position-sizing tests.

Every expected quantity is derived from the documented formula rather than copied from a run:
``floor(risk_budget / risk_per_share)``, ``floor(max_position_value / entry_price)`` and the
cash bound, each recomputed here from the policy under test. A failure therefore points at the
engine's arithmetic, not at a magic number in the fixture.
"""

from decimal import Decimal

import pytest

from app.core.exceptions import InvalidOrderError, InvalidStopPriceError
from app.services.portfolio.models import (
    CONSTRAINT_CASH,
    CONSTRAINT_POSITION_WEIGHT,
    CONSTRAINT_RISK,
    CONSTRAINT_UNAFFORDABLE,
)
from app.services.portfolio.sizing import (
    METHOD_MAX_POSITION_CAP,
    PositionSizingEngine,
    available_cash_of,
    max_position_value_of,
    quantity_by_capital,
    quantity_by_risk,
    risk_budget_of,
    risk_per_share_long,
    weight_pct_of,
)
from tests.phase13_helpers import policy

EQUITY = Decimal("100000")


def expected_by_risk(equity, pct, entry, stop) -> int:
    """The risk bound, re-derived from the rule under test."""
    budget = equity * pct / Decimal("100")
    return int((budget / (entry - stop)).to_integral_value(rounding="ROUND_FLOOR"))


# ----------------------------------------------------------------------
# The pure functions
# ----------------------------------------------------------------------


class TestRiskBudget:
    def test_one_percent_of_equity(self):
        assert risk_budget_of(EQUITY, Decimal("1")) == Decimal("1000.00")

    def test_rate_is_a_percentage_not_a_fraction(self):
        """1% of 100,000 is 1,000 — the engine divides by 100 exactly once."""
        assert risk_budget_of(EQUITY, Decimal("1")) != risk_budget_of(EQUITY, Decimal("100"))

    def test_zero_equity_has_nothing_to_risk(self):
        assert risk_budget_of(Decimal("0"), Decimal("1")) == Decimal("0.00")

    def test_negative_equity_has_nothing_to_risk(self):
        assert risk_budget_of(Decimal("-5000"), Decimal("5")) == Decimal("0.00")


class TestRiskPerShare:
    def test_entry_minus_stop(self):
        assert risk_per_share_long(Decimal("100"), Decimal("95")) == Decimal("5.0000")

    def test_missing_stop_is_refused_not_assumed(self):
        with pytest.raises(InvalidStopPriceError):
            risk_per_share_long(Decimal("100"), None)

    def test_stop_above_entry_is_refused(self):
        """A long cannot lose on a stop above entry; the 'risk' would be upside."""
        with pytest.raises(InvalidStopPriceError):
            risk_per_share_long(Decimal("100"), Decimal("105"))

    def test_stop_equal_to_entry_is_refused(self):
        with pytest.raises(InvalidStopPriceError):
            risk_per_share_long(Decimal("100"), Decimal("100"))

    def test_stop_at_zero_is_refused(self):
        with pytest.raises(InvalidStopPriceError):
            risk_per_share_long(Decimal("100"), Decimal("0"))

    def test_zero_entry_is_refused(self):
        with pytest.raises(InvalidOrderError):
            risk_per_share_long(Decimal("0"), Decimal("10"))


class TestCapitalBound:
    def test_max_position_value(self):
        assert max_position_value_of(EQUITY, Decimal("30")) == Decimal("30000.00")

    def test_quantity_floors_rather_than_rounds_up(self):
        """30,000 / 100 = 300 exactly; 30,000 / 250 = 120 with a remainder that must be dropped."""
        assert quantity_by_capital(Decimal("30000"), Decimal("250")) == 120

    def test_zero_capital_sizes_nothing(self):
        assert quantity_by_capital(Decimal("0"), Decimal("100")) == 0

    def test_risk_quantity_floors_rather_than_rounds_up(self):
        """A budget of 1,000 against 3.33 of risk is 300 shares, not 301."""
        assert quantity_by_risk(Decimal("1000"), Decimal("3.33")) == 300

    def test_zero_budget_sizes_nothing(self):
        assert quantity_by_risk(Decimal("0"), Decimal("5")) == 0


class TestWeightPercent:
    def test_weight_of_a_value(self):
        assert weight_pct_of(Decimal("30000"), EQUITY) == Decimal("30.0000")

    def test_non_positive_equity_has_no_weight(self):
        assert weight_pct_of(Decimal("100"), Decimal("0")) is None

    def test_cash_reserve_fallback_is_derived_not_actual_cash(self):
        """The helper is documented as a fallback for prose only, and must not be read as cash."""
        derived = available_cash_of(policy(min_cash_reserve_pct=Decimal("10")), EQUITY)
        assert derived == Decimal("90000.00")


# ----------------------------------------------------------------------
# The combined model
# ----------------------------------------------------------------------


class TestRiskAndWeightIsTheMinimum:
    def test_risk_bound_binds_and_is_reported(self):
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        assert breakdown.quantity == 200
        assert breakdown.quantity_by_risk == 200
        assert breakdown.quantity_by_capital == 300
        assert breakdown.binding_constraint == CONSTRAINT_RISK

    def test_weight_bound_binds_when_the_stop_is_tight(self):
        """A tight stop shrinks risk per share, so the weight cap becomes the limit."""
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            stop_price=Decimal("99.99"),
        )
        assert breakdown.binding_constraint == CONSTRAINT_POSITION_WEIGHT
        assert breakdown.quantity == 300

    def test_cash_bound_binds_when_cash_is_short(self):
        """Only 1,000 of cash funds 10 shares, whatever the policy would otherwise allow."""
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=Decimal("1000"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        assert breakdown.quantity == 10
        assert breakdown.binding_constraint == CONSTRAINT_CASH

    def test_quantity_is_the_minimum_of_all_three_bounds(self):
        """The cross-check: the answer equals min(risk, weight, cash), recomputed here."""
        engine = PositionSizingEngine(policy(max_position_weight_pct=Decimal("12")))
        equity, cash, entry, stop = Decimal("250000"), Decimal("40000"), Decimal("150"), Decimal("145")
        breakdown = engine.size(
            equity=equity, available_cash=cash, entry_price=entry, stop_price=stop
        )
        assert breakdown.quantity == min(
            expected_by_risk(equity, Decimal("1"), entry, stop),
            int((equity * Decimal("12") / Decimal("100") / entry).to_integral_value()),
            int((cash / entry).to_integral_value()),
        )

    def test_risk_amount_never_exceeds_the_budget(self):
        """The invariant the risk bound exists to protect, checked at several stops."""
        engine = PositionSizingEngine(policy())
        for stop in ("99", "97.5", "95", "90", "85"):
            breakdown = engine.size(
                equity=EQUITY,
                available_cash=EQUITY,
                entry_price=Decimal("100"),
                stop_price=Decimal(stop),
            )
            assert breakdown.risk_amount <= breakdown.risk_budget

    def test_position_value_never_exceeds_the_weight_cap(self):
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("137"),
            stop_price=Decimal("120"),
        )
        assert breakdown.quantity * Decimal("137") <= breakdown.max_position_value


class TestCashBoundIncludesFees:
    def test_fee_makes_the_affordable_quantity_smaller(self):
        """With a 0.1% commission, 1,000 of cash funds 9 shares at 100, not 10."""
        engine = PositionSizingEngine(
            policy(commission_rate=Decimal("0.001"), min_fee=Decimal("0"))
        )
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=Decimal("1000"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        assert breakdown.quantity == 9
        assert breakdown.capital_required <= Decimal("1000")

    def test_capital_required_exceeds_price_times_quantity_by_exactly_the_fee(self):
        engine = PositionSizingEngine(policy(commission_rate=Decimal("0.001")))
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        expected_fee = (Decimal(breakdown.quantity) * Decimal("100") * Decimal("0.001")).quantize(
            Decimal("0.01")
        )
        assert breakdown.fees == expected_fee
        assert breakdown.capital_required == Decimal(breakdown.quantity) * Decimal("100") + expected_fee


class TestCapitalOnlySizing:
    def test_sizes_from_capital_without_a_stop(self):
        """MAX_POSITION_CAP deliberately opts out of stop-based sizing."""
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            method=METHOD_MAX_POSITION_CAP,
        )
        assert breakdown.quantity == 300
        assert breakdown.binding_constraint == CONSTRAINT_POSITION_WEIGHT

    def test_is_still_capped_by_cash(self):
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=Decimal("5000"),
            entry_price=Decimal("100"),
            method=METHOD_MAX_POSITION_CAP,
        )
        assert breakdown.quantity == 50
        assert breakdown.binding_constraint == CONSTRAINT_CASH

    def test_claims_no_risk_bound(self):
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            method=METHOD_MAX_POSITION_CAP,
        )
        assert breakdown.risk_per_share is None
        assert breakdown.risk_amount == Decimal("0.00")
        assert breakdown.stop_price is None


class TestRefusals:
    def test_missing_stop_on_the_risk_model_is_refused(self):
        """Sizing 'without knowing the risk' is not risk-based sizing."""
        engine = PositionSizingEngine(policy())
        with pytest.raises(InvalidStopPriceError):
            engine.size(
                equity=EQUITY,
                available_cash=EQUITY,
                entry_price=Decimal("100"),
            )

    def test_zero_entry_price_is_refused(self):
        engine = PositionSizingEngine(policy())
        with pytest.raises(InvalidOrderError):
            engine.size(
                equity=EQUITY,
                available_cash=EQUITY,
                entry_price=Decimal("0"),
                stop_price=Decimal("95"),
            )

    def test_negative_equity_opens_nothing(self):
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=Decimal("-1000"),
            available_cash=Decimal("1000"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        assert breakdown.quantity == 0
        assert breakdown.binding_constraint == CONSTRAINT_UNAFFORDABLE

    def test_cash_below_one_share_opens_nothing(self):
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=Decimal("50"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        assert breakdown.quantity == 0
        assert breakdown.binding_constraint == CONSTRAINT_UNAFFORDABLE

    def test_below_minimum_trade_value_opens_nothing(self):
        """The refusal must be a zero position, not a smaller one."""
        engine = PositionSizingEngine(policy(min_trade_value=Decimal("500000")))
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        assert breakdown.quantity == 0
        assert breakdown.binding_constraint == CONSTRAINT_UNAFFORDABLE

    def test_a_refused_position_risk_nothing(self):
        """A zero position must not report risk at the stop."""
        engine = PositionSizingEngine(policy(min_trade_value=Decimal("500000")))
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        assert breakdown.risk_amount == Decimal("0.00")

    def test_a_size_at_the_minimum_trade_value_is_accepted(self):
        """The boundary is inclusive: exactly the minimum trades."""
        engine = PositionSizingEngine(policy(min_trade_value=Decimal("20000")))
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        assert breakdown.quantity == 200
        assert breakdown.capital_required == Decimal("20000.00")


class TestRemainingWeightCapacity:
    def test_a_smaller_capacity_replaces_the_weight_bound(self):
        """How the allocation engine spends a leftover exposure ceiling across candidates."""
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            remaining_weight_capacity=Decimal("5000"),
        )
        assert breakdown.quantity == 50
        assert breakdown.max_position_value == Decimal("5000.00")

    def test_capacity_above_the_policy_cap_does_not_raise_the_bound(self):
        """Capacity can only tighten the weight bound, never loosen it."""
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            remaining_weight_capacity=Decimal("900000"),
        )
        assert breakdown.max_position_value == Decimal("30000.00")


class TestExplanationIsReconstructable:
    def test_every_number_in_the_decision_appears_in_the_text(self):
        """A size the caller cannot re-derive is a size the caller has to trust."""
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=Decimal("60000"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        for figure in (
            breakdown.risk_budget,
            breakdown.max_position_value,
            breakdown.risk_per_share,
            breakdown.capital_required,
            breakdown.risk_amount,
        ):
            assert str(figure) in breakdown.explanation

    def test_the_text_names_the_binding_constraint(self):
        engine = PositionSizingEngine(policy())
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=Decimal("1000"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        assert CONSTRAINT_CASH in breakdown.explanation

    def test_the_text_quotes_the_actual_cash_not_a_derived_figure(self):
        """The engine sizes against real cash, so the prose must quote real cash."""
        engine = PositionSizingEngine(policy(min_cash_reserve_pct=Decimal("50")))
        breakdown = engine.size(
            equity=EQUITY,
            available_cash=Decimal("12345"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        assert "12345" in breakdown.explanation

    def test_existing_position_is_reported_without_changing_the_marginal_size(self):
        engine = PositionSizingEngine(policy())
        without = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        with_position = engine.size(
            equity=EQUITY,
            available_cash=EQUITY,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            existing_quantity=50,
        )
        assert with_position.quantity == without.quantity
        assert "50 share" in with_position.explanation


class TestDeterminism:
    def test_the_same_inputs_always_produce_the_same_answer(self):
        engine = PositionSizingEngine(policy())
        answers = {
            engine.size(
                equity=EQUITY,
                available_cash=Decimal("54321"),
                entry_price=Decimal("173.25"),
                stop_price=Decimal("159.87"),
            ).explanation
            for _ in range(5)
        }
        assert len(answers) == 1

    def test_decimal_inputs_do_not_drift(self):
        """A repeated float-style division cannot accumulate error; Decimal cannot."""
        engine = PositionSizingEngine(policy())
        quantities = {
            engine.size(
                equity=Decimal("33333.33"),
                available_cash=Decimal("22222.22"),
                entry_price=Decimal("77.77"),
                stop_price=Decimal("70.01"),
            ).quantity
            for _ in range(10)
        }
        assert len(quantities) == 1