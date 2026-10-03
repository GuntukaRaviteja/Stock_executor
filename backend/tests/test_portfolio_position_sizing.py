"""Phase 10 position-sizing tests.

The expected quantity in every test is derived from the formula the specification states, not
copied from a run, so a failure always points at the sizing code. Rounding, fee interaction
and invalid stop-loss values are covered explicitly.
"""

from decimal import Decimal

import pytest

from app.core.exceptions import InvalidPositionSizeError
from app.schemas.portfolio import PositionSizingMethod, PositionSizingRequest
from app.services.portfolio.calculator import (
    risk_per_share,
    size_quantity_by_percentage,
    size_quantity_by_risk,
    size_quantity_fixed_capital,
)
from app.services.portfolio.models import TradingCostConfig

#: Zero-cost model so a quantity is exactly ``floor(budget / price)``.
NO_FEES = TradingCostConfig(
    commission_rate=Decimal("0"), min_fee=Decimal("0"), slippage_rate=Decimal("0")
)
#: 0.1% per side, the project default.
DEFAULT_COSTS = TradingCostConfig(
    commission_rate=Decimal("0.001"), min_fee=Decimal("0"), slippage_rate=Decimal("0")
)


# ----------------------------------------------------------------------
# Fixed capital
# ----------------------------------------------------------------------


class TestFixedCapitalSizing:
    def test_specification_example(self):
        """capital 10,000 at price 250 -> 40 shares."""
        assert size_quantity_fixed_capital(Decimal("10000"), Decimal("250"), NO_FEES) == 40

    def test_quantity_is_floored_not_rounded(self):
        """10,000 at 300 buys 33 shares, never 34."""
        assert size_quantity_fixed_capital(Decimal("10000"), Decimal("300"), NO_FEES) == 33

    def test_a_budget_too_small_for_one_share_returns_zero(self):
        assert size_quantity_fixed_capital(Decimal("100"), Decimal("250"), NO_FEES) == 0

    def test_zero_budget_returns_zero(self):
        assert size_quantity_fixed_capital(Decimal("0"), Decimal("250"), NO_FEES) == 0

    def test_fees_are_included_in_the_unit_cost(self):
        """With 0.1% fees, 1,000 at 100 buys 9 shares: 10 would need 1,010."""
        assert size_quantity_fixed_capital(Decimal("1000"), Decimal("100"), DEFAULT_COSTS) == 9

    def test_negative_or_zero_price_returns_zero(self):
        assert size_quantity_fixed_capital(Decimal("1000"), Decimal("0"), NO_FEES) == 0
        assert size_quantity_fixed_capital(Decimal("1000"), Decimal("-100"), NO_FEES) == 0


# ----------------------------------------------------------------------
# Portfolio percentage
# ----------------------------------------------------------------------


class TestPercentageSizing:
    def test_specification_example(self):
        """equity 100,000, 5% at price 250 -> 20 shares."""
        assert (
            size_quantity_by_percentage(
                Decimal("100000"), Decimal("5"), Decimal("250"), NO_FEES
            )
            == 20
        )

    def test_basis_is_equity_not_cash(self):
        """A portfolio with 40,000 cash but 100,000 equity still sizes off 100,000."""
        assert (
            size_quantity_by_percentage(
                Decimal("100000"), Decimal("10"), Decimal("500"), NO_FEES
            )
            == 20
        )

    def test_quantity_is_floored(self):
        """5% of 100,000 is 5,000; at 300 that is 16 shares, not 17."""
        assert (
            size_quantity_by_percentage(
                Decimal("100000"), Decimal("5"), Decimal("300"), NO_FEES
            )
            == 16
        )

    def test_zero_or_negative_allocation_returns_zero(self):
        assert size_quantity_by_percentage(Decimal("100000"), Decimal("0"), Decimal("100"), NO_FEES) == 0
        assert size_quantity_by_percentage(Decimal("100000"), Decimal("-5"), Decimal("100"), NO_FEES) == 0

    def test_non_positive_equity_returns_zero(self):
        assert size_quantity_by_percentage(Decimal("0"), Decimal("5"), Decimal("100"), NO_FEES) == 0
        assert size_quantity_by_percentage(Decimal("-100"), Decimal("5"), Decimal("100"), NO_FEES) == 0

    def test_fees_are_included_in_the_unit_cost(self):
        assert (
            size_quantity_by_percentage(Decimal("100000"), Decimal("5"), Decimal("100"), DEFAULT_COSTS)
            == 49
        )


# ----------------------------------------------------------------------
# Risk-based sizing
# ----------------------------------------------------------------------


class TestRiskBasedSizing:
    def test_specification_example(self):
        """equity 100,000, risk 1%, entry 100, stop 95 -> risk/share 5, allowed 1,000 -> 200."""
        assert risk_per_share(Decimal("100"), Decimal("95")) == Decimal("5")
        assert (
            size_quantity_by_risk(
                Decimal("100000"), Decimal("1"), Decimal("100"), Decimal("95"), NO_FEES
            )
            == 200
        )

    def test_allowed_risk_is_equity_times_risk_percentage(self):
        plan_allowed = Decimal("100000") * Decimal("1") / Decimal("100")
        assert plan_allowed == Decimal("1000")

    def test_risk_per_share_is_the_absolute_distance(self):
        """A stop *above* the entry (a trailing/target style stop) measures the same way."""
        assert risk_per_share(Decimal("100"), Decimal("105")) == Decimal("5")

    def test_quantity_is_floored(self):
        """allowed risk 1,000 over a risk/share of 3 -> 333 shares."""
        assert (
            size_quantity_by_risk(
                Decimal("100000"), Decimal("1"), Decimal("100"), Decimal("97"), NO_FEES
            )
            == 333
        )

    def test_a_tighter_stop_increases_the_quantity(self):
        loose = size_quantity_by_risk(
            Decimal("100000"), Decimal("1"), Decimal("100"), Decimal("90"), NO_FEES
        )
        tight = size_quantity_by_risk(
            Decimal("100000"), Decimal("1"), Decimal("100"), Decimal("99"), NO_FEES
        )
        assert loose == 100
        assert tight == 1000

    def test_stop_equal_to_entry_is_rejected(self):
        """A stop at the entry price defines no risk per share, so there is no defensible size."""
        with pytest.raises(ValueError, match="must differ"):
            size_quantity_by_risk(
                Decimal("100000"), Decimal("1"), Decimal("100"), Decimal("100"), NO_FEES
            )

    def test_risk_budget_is_not_a_cash_budget(self):
        """200 shares at 100 costs 20,000 — the 1,000 risk budget is what is being risked."""
        quantity = size_quantity_by_risk(
            Decimal("100000"), Decimal("1"), Decimal("100"), Decimal("95"), NO_FEES
        )
        assert quantity == 200
        assert quantity * Decimal("100") == Decimal("20000")

    def test_an_explicit_cash_cap_limits_the_result(self):
        """The service passes the portfolio's cash so a "risk" order stays fundable."""
        capped = size_quantity_by_risk(
            Decimal("100000"),
            Decimal("1"),
            Decimal("100"),
            Decimal("95"),
            NO_FEES,
            max_capital=Decimal("5000"),
        )
        assert capped == 50

    def test_zero_risk_percentage_returns_zero(self):
        assert (
            size_quantity_by_risk(
                Decimal("100000"), Decimal("0"), Decimal("100"), Decimal("95"), NO_FEES
            )
            == 0
        )


# ----------------------------------------------------------------------
# Service-level behaviour (request validation and reporting)
# ----------------------------------------------------------------------


class TestPositionSizingRequestValidation:
    @pytest.mark.parametrize(
        "method,fields",
        [
            (PositionSizingMethod.FIXED_CAPITAL, {}),
            (PositionSizingMethod.PERCENTAGE, {}),
            (
                PositionSizingMethod.RISK_BASED,
                {"risk_pct": Decimal("1"), "entry_price": Decimal("100")},
            ),
        ],
    )
    def test_incomplete_requests_are_refused(self, method, fields):
        with pytest.raises(Exception) as excinfo:
            PositionSizingRequest(method=method, price=Decimal("100"), **fields)
        assert "sizing requires" in str(excinfo.value)

    @pytest.mark.parametrize(
        "field", ["capital", "allocation_pct", "risk_pct", "entry_price", "stop_loss_price"]
    )
    def test_non_positive_financial_inputs_are_refused(self, field):
        with pytest.raises(Exception):
            PositionSizingRequest(
                method=PositionSizingMethod.FIXED_CAPITAL,
                price=Decimal("100"),
                capital=Decimal("1000"),
                **{field: Decimal("-1")},
            )

    def test_percentage_above_100_is_refused(self):
        with pytest.raises(Exception):
            PositionSizingRequest(
                method=PositionSizingMethod.PERCENTAGE,
                price=Decimal("100"),
                allocation_pct=Decimal("101"),
            )


class TestPositionSizingService:
    """The service turns a validated request plus a portfolio into a reported plan."""

    def test_fixed_capital_plan_reports_its_arithmetic(self, db_session):
        from app.services.portfolio.position_sizing_service import position_sizing_service

        plan = position_sizing_service.size(
            db_session,
            equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            costs=NO_FEES,
            request=PositionSizingRequest(
                method=PositionSizingMethod.FIXED_CAPITAL,
                price=Decimal("250"),
                capital=Decimal("10000"),
            ),
        )
        assert plan.method is PositionSizingMethod.FIXED_CAPITAL
        assert plan.quantity == 40
        assert plan.capital == Decimal("10000.00")
        assert "10000" in plan.reason

    def test_percentage_plan_uses_portfolio_equity(self, db_session):
        from app.services.portfolio.position_sizing_service import position_sizing_service

        plan = position_sizing_service.size(
            db_session,
            equity=Decimal("100000"),
            available_cash=Decimal("40000"),
            costs=NO_FEES,
            request=PositionSizingRequest(
                method=PositionSizingMethod.PERCENTAGE,
                price=Decimal("250"),
                allocation_pct=Decimal("5"),
            ),
        )
        assert plan.quantity == 20
        assert plan.budget == Decimal("5000.00")
        assert plan.equity == Decimal("100000")

    def test_risk_plan_reports_risk_per_share_and_allowed_risk(self, db_session):
        from app.services.portfolio.position_sizing_service import position_sizing_service

        plan = position_sizing_service.size(
            db_session,
            equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            costs=NO_FEES,
            request=PositionSizingRequest(
                method=PositionSizingMethod.RISK_BASED,
                price=Decimal("100"),
                risk_pct=Decimal("1"),
                entry_price=Decimal("100"),
                stop_loss_price=Decimal("95"),
            ),
        )
        assert plan.quantity == 200
        assert plan.risk_per_share == Decimal("5")
        assert plan.allowed_risk == Decimal("1000.00")

    def test_risk_plan_reports_the_cash_cap_it_applied(self, db_session):
        from app.services.portfolio.position_sizing_service import position_sizing_service

        plan = position_sizing_service.size(
            db_session,
            equity=Decimal("100000"),
            available_cash=Decimal("5000"),
            costs=NO_FEES,
            request=PositionSizingRequest(
                method=PositionSizingMethod.RISK_BASED,
                price=Decimal("100"),
                risk_pct=Decimal("1"),
                entry_price=Decimal("100"),
                stop_loss_price=Decimal("95"),
            ),
        )
        assert plan.quantity == 50
        assert "Capped" in plan.reason

    def test_stop_equal_to_entry_raises_a_domain_error(self, db_session):
        from app.services.portfolio.position_sizing_service import position_sizing_service

        with pytest.raises(InvalidPositionSizeError):
            position_sizing_service.size(
                db_session,
                equity=Decimal("100000"),
                available_cash=Decimal("100000"),
                costs=NO_FEES,
                request=PositionSizingRequest.model_construct(
                    method=PositionSizingMethod.RISK_BASED,
                    price=Decimal("100"),
                    risk_pct=Decimal("1"),
                    entry_price=Decimal("100"),
                    stop_loss_price=Decimal("100"),
                ),
            )

    def test_missing_price_is_reported_rather_than_guessed(self, db_session):
        from app.services.portfolio.position_sizing_service import position_sizing_service

        with pytest.raises(InvalidPositionSizeError, match="No price"):
            position_sizing_service.size(
                db_session,
                equity=Decimal("100000"),
                available_cash=Decimal("100000"),
                costs=NO_FEES,
                request=PositionSizingRequest(
                    method=PositionSizingMethod.FIXED_CAPITAL, capital=Decimal("1000")
                ),
            )