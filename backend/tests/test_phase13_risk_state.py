"""Phase 13 risk-state tests.

Expected exposures, P&L, drawdowns and total risk are each recomputed here from the valuation the
engine is given, so a failure points at the calculation rather than at a recorded number. Flags
are asserted by identity (:class:`PortfolioRiskFlag`), so a test cannot pass by raising a
different breach than the one under test.
"""

from datetime import datetime
from decimal import Decimal

from app.schemas.risk import PortfolioRiskFlag
from app.services.portfolio.risk_state import (
    PortfolioRiskStateEngine,
    portfolio_return_pct,
    signed_drawdown,
    worst_drawdown,
)
from tests.phase13_helpers import policy, position_valuation, valuation

AS_OF = datetime(2024, 6, 3, 10, 30)


def state_of(val, engine_policy=None, **kwargs):
    """Evaluate the risk state for a valuation."""
    engine = PortfolioRiskStateEngine()
    return engine.evaluate(val, engine_policy or policy(), **kwargs)


def flags_of(result):
    """The flags raised, as a set."""
    return set(result.flags)


# ----------------------------------------------------------------------
# Exposure
# ----------------------------------------------------------------------


class TestExposure:
    def test_exposure_is_market_value_over_equity(self):
        result = state_of(
            valuation(cash="20000", positions=[position_valuation(1, 200, 100)])
        )
        assert result.equity == Decimal("40000.00")
        assert result.market_value == Decimal("20000.00")
        assert result.exposure_pct == Decimal("50.0000")

    def test_cash_weight_complements_exposure_when_nothing_is_owed(self):
        result = state_of(
            valuation(cash="20000", positions=[position_valuation(1, 200, 100)])
        )
        assert result.cash_weight_pct == Decimal("50.0000")

    def test_exposure_uses_market_value_not_cost_basis(self):
        """A position up 50% is 50% of equity by market value, not by what it cost."""
        result = state_of(
            valuation(cash="0", positions=[position_valuation(1, 100, 100, 150)])
        )
        assert result.market_value == Decimal("15000.00")
        assert result.exposure_pct == Decimal("100.0000")

    def test_invested_weight_uses_cost_basis(self):
        result = state_of(
            valuation(cash="0", positions=[position_valuation(1, 100, 100, 150)])
        )
        assert result.invested_capital == Decimal("10000.00")
        assert result.invested_weight_pct == Decimal("66.6667")

    def test_a_flat_portfolio_has_no_exposure(self):
        result = state_of(valuation(cash="100000"))
        assert result.exposure_pct == Decimal("0.0000")
        assert result.position_count == 0

    def test_a_wiped_out_portfolio_has_no_weights(self):
        """Weight None means 'no portfolio to be a fraction of', which is not weight zero."""
        result = state_of(valuation(cash="0"))
        assert result.equity == Decimal("0.00")
        assert result.exposure_pct is None
        assert result.cash_weight_pct is None
        assert result.largest_position_weight_pct is None


class TestPositionWeights:
    def test_each_weight_is_its_share_of_equity(self):
        result = state_of(
            valuation(
                cash="0",
                positions=[
                    position_valuation(1, 100, 100),
                    position_valuation(2, 200, 100),
                ],
            )
        )
        weights = {item.stock_id: item.portfolio_weight_pct for item in result.positions}
        assert weights[1] == Decimal("33.3333")
        assert weights[2] == Decimal("66.6667")

    def test_the_largest_position_is_reported(self):
        result = state_of(
            valuation(
                cash="0",
                positions=[
                    position_valuation(1, 100, 100),
                    position_valuation(2, 300, 100),
                ],
            )
        )
        assert result.largest_position_stock_id == 2
        assert result.largest_position_weight_pct == Decimal("75.0000")

    def test_positions_are_ordered_by_weight_then_id(self):
        """A stable order, so two reads of the same portfolio can be compared."""
        result = state_of(
            valuation(
                cash="0",
                positions=[
                    position_valuation(9, 100, 100),
                    position_valuation(4, 100, 100),
                    position_valuation(7, 300, 100),
                ],
            )
        )
        assert [item.stock_id for item in result.positions] == [7, 4, 9]


# ----------------------------------------------------------------------
# P&L
# ----------------------------------------------------------------------


class TestProfitAndLoss:
    def test_unrealized_pnl_is_the_move_from_entry(self):
        result = state_of(
            valuation(cash="0", positions=[position_valuation(1, 100, 100, 120)])
        )
        assert result.unrealized_pnl == Decimal("2000.00")

    def test_realized_and_unrealized_sum_to_total(self):
        result = state_of(
            valuation(
                cash="50000",
                positions=[position_valuation(1, 100, 100, 120)],
                realized_pnl="1500",
            )
        )
        assert result.realized_pnl == Decimal("1500")
        assert result.unrealized_pnl == Decimal("2000.00")
        assert result.total_pnl == Decimal("3500.00")

    def test_return_is_on_initial_capital(self):
        """20,000 invested at 100 and now worth 22,000: equity 102,000 on 100,000 of capital."""
        result = state_of(
            valuation(
                initial_capital="100000",
                cash="80000",
                positions=[position_valuation(1, 200, 100, 110)],
            )
        )
        assert result.equity == Decimal("102000.00")
        assert result.total_pnl == Decimal("2000.00")
        assert result.portfolio_return_pct == Decimal("2.0000")

    def test_return_helper(self):
        assert portfolio_return_pct(Decimal("110000"), Decimal("100000")) == Decimal("10.0000")

    def test_a_loss_is_a_negative_return(self):
        """Fully invested, and the holding is down 10%: equity 90,000 on 100,000."""
        result = state_of(
            valuation(initial_capital="100000", cash="0", positions=[
                position_valuation(1, 1000, 100, 90)
            ])
        )
        assert result.equity == Decimal("90000.00")
        assert result.portfolio_return_pct == Decimal("-10.0000")


# ----------------------------------------------------------------------
# Drawdown
# ----------------------------------------------------------------------


class TestDrawdown:
    def test_current_drawdown_is_measured_from_the_peak(self):
        """Peak 110,000 against equity 100,000 is 10% down, not 0%."""
        result = state_of(
            valuation(initial_capital="100000", cash="100000"), peak_equity=Decimal("110000")
        )
        assert result.peak_equity == Decimal("110000.00")
        assert result.current_drawdown_pct == Decimal("9.0909")

    def test_the_peak_never_falls_below_the_current_equity(self):
        """Measuring a drawdown against today's equity would report every day as a fresh peak."""
        result = state_of(valuation(initial_capital="100000", cash="50000"))
        assert result.peak_equity == Decimal("100000.00")
        assert result.current_drawdown_pct == Decimal("50.0000")

    def test_the_initial_capital_counts_toward_the_peak(self):
        """A portfolio that has only ever fallen is at its drawdown limit from day one."""
        result = state_of(valuation(initial_capital="100000", cash="80000"))
        assert result.peak_equity == Decimal("100000.00")
        assert result.current_drawdown_pct == Decimal("20.0000")

    def test_worst_drawdown_comes_from_the_history(self):
        """What turns the limit into 'the worst drawdown this portfolio has been in'."""
        amount, pct = worst_drawdown(
            [Decimal("100000"), Decimal("120000"), Decimal("90000")], Decimal("100000")
        )
        assert amount == Decimal("30000.00")
        assert pct == Decimal("25.0000")

    def test_worst_drawdown_of_a_rising_portfolio_is_zero(self):
        amount, pct = worst_drawdown(
            [Decimal("100000"), Decimal("110000"), Decimal("120000")], Decimal("100000")
        )
        assert amount == Decimal("0.00")
        assert pct == Decimal("0.0000")

    def test_worst_drawdown_is_reported_alongside_the_current_one(self):
        result = state_of(
            valuation(initial_capital="100000", cash="100000"),
            equity_history=[Decimal("100000"), Decimal("110000"), Decimal("100000")],
        )
        assert result.current_drawdown_pct == Decimal("9.0909")
        assert result.max_drawdown_pct == Decimal("9.0909")

    def test_signed_drawdown_is_negative_below_the_peak(self):
        assert signed_drawdown(Decimal("110000"), Decimal("100000")) < 0


# ----------------------------------------------------------------------
# Total risk
# ----------------------------------------------------------------------


class TestTotalRisk:
    def test_risk_is_the_loss_at_each_position_stop(self):
        """The policy's stop, not a percentage of the position: 5/share over 200 shares."""
        result = state_of(
            valuation(cash="0", positions=[position_valuation(1, 200, 100)]),
            policy(stop_loss_pct=Decimal("5")),
        )
        assert result.positions[0].stop_price == Decimal("95.0000")
        assert result.positions[0].risk_amount == Decimal("1000.00")
        assert result.total_risk_amount == Decimal("1000.00")

    def test_total_risk_sums_across_positions(self):
        result = state_of(
            valuation(
                cash="0",
                positions=[
                    position_valuation(1, 200, 100),
                    position_valuation(2, 100, 50),
                ],
            ),
            policy(stop_loss_pct=Decimal("5")),
        )
        assert result.positions[0].risk_amount == Decimal("1000.00")
        assert result.positions[1].risk_amount == Decimal("250.00")
        assert result.total_risk_amount == Decimal("1250.00")

    def test_risk_is_zero_without_a_stop(self):
        result = state_of(
            valuation(cash="0", positions=[position_valuation(1, 200, 100)]),
            policy(),
        )
        assert result.total_risk_amount == Decimal("0.00")

    def test_total_risk_is_a_percentage_of_equity(self):
        result = state_of(
            valuation(cash="0", positions=[position_valuation(1, 200, 100)]),
            policy(stop_loss_pct=Decimal("5")),
        )
        assert result.total_risk_pct == Decimal("5.0000")


# ----------------------------------------------------------------------
# Flags
# ----------------------------------------------------------------------


class TestFlags:
    def test_a_portfolio_inside_its_policy_raises_nothing(self):
        result = state_of(
            valuation(cash="40000", positions=[position_valuation(1, 200, 100)]),
            policy(max_position_weight_pct=Decimal("60"), max_exposure_pct=Decimal("70")),
        )
        assert result.flags == ()

    def test_position_concentration_is_flagged(self):
        result = state_of(
            valuation(cash="0", positions=[position_valuation(1, 800, 100)]),
            policy(max_position_weight_pct=Decimal("30")),
        )
        assert PortfolioRiskFlag.POSITION_CONCENTRATION_BREACHED in flags_of(result)

    def test_a_position_exactly_on_the_weight_cap_is_not_flagged(self):
        """Exclusive, matching the Phase 12 pre-trade rule: exactly 30% is inside the policy."""
        result = state_of(
            valuation(cash="70000", positions=[position_valuation(1, 300, 100)]),
            policy(max_position_weight_pct=Decimal("30")),
        )
        assert PortfolioRiskFlag.POSITION_CONCENTRATION_BREACHED not in flags_of(result)

    def test_sector_concentration_is_flagged(self):
        result = state_of(
            valuation(
                cash="0",
                positions=[
                    position_valuation(1, 300, 100),
                    position_valuation(2, 300, 100),
                ],
            ),
            policy(max_position_weight_pct=Decimal("40"), max_sector_weight_pct=Decimal("50")),
            sectors={1: "BANKING", 2: "BANKING"},
        )
        assert PortfolioRiskFlag.SECTOR_CONCENTRATION_BREACHED in flags_of(result)

    def test_a_sector_exactly_on_its_cap_is_not_flagged(self):
        result = state_of(
            valuation(
                cash="25000",
                positions=[position_valuation(1, 250, 100)],
            ),
            policy(max_position_weight_pct=Decimal("60"), max_sector_weight_pct=Decimal("50")),
            sectors={1: "BANKING"},
        )
        assert result.sector_weights == (("BANKING", Decimal("50.0000")),)
        assert PortfolioRiskFlag.SECTOR_CONCENTRATION_BREACHED not in flags_of(result)

    def test_positions_with_an_unknown_sector_are_left_out_of_sector_totals(self):
        """An invented 'unknown' bucket could let a concentrated portfolio pass the check."""
        result = state_of(
            valuation(
                cash="0",
                positions=[
                    position_valuation(1, 300, 100),
                    position_valuation(2, 200, 100),
                ],
            ),
            policy(max_position_weight_pct=Decimal("40"), max_sector_weight_pct=Decimal("50")),
            sectors={},
        )
        assert result.sector_weights == ()

    def test_position_count_is_flagged(self):
        result = state_of(
            valuation(
                cash="0",
                positions=[
                    position_valuation(1, 100, 100),
                    position_valuation(2, 100, 100),
                    position_valuation(3, 100, 100),
                ],
            ),
            policy(max_position_weight_pct=Decimal("40"), max_positions=2),
        )
        assert PortfolioRiskFlag.POSITION_LIMIT_BREACHED in flags_of(result)

    def test_exposure_above_the_ceiling_is_flagged(self):
        result = state_of(
            valuation(cash="10000", positions=[position_valuation(1, 900, 100)]),
            policy(max_position_weight_pct=Decimal("95"), max_exposure_pct=Decimal("80")),
        )
        assert PortfolioRiskFlag.EXPOSURE_LIMIT_BREACHED in flags_of(result)

    def test_exposure_exactly_on_the_ceiling_is_not_flagged(self):
        """The boundary is exclusive: exactly 80% invested is inside the policy."""
        result = state_of(
            valuation(cash="20000", positions=[position_valuation(1, 800, 100)]),
            policy(max_position_weight_pct=Decimal("90"), max_exposure_pct=Decimal("80")),
        )
        assert result.exposure_pct == Decimal("80.0000")
        assert PortfolioRiskFlag.EXPOSURE_LIMIT_BREACHED not in flags_of(result)

    def test_cash_below_the_reserve_is_flagged(self):
        result = state_of(
            valuation(cash="5000", positions=[position_valuation(1, 900, 100)]),
            policy(max_position_weight_pct=Decimal("95"), min_cash_reserve_pct=Decimal("10")),
        )
        assert PortfolioRiskFlag.CASH_RESERVE_BREACHED in flags_of(result)

    def test_cash_exactly_at_the_reserve_is_not_flagged(self):
        """The boundary is exclusive: exactly 10,000 of cash is inside the policy."""
        result = state_of(
            valuation(cash="10000", positions=[position_valuation(1, 900, 100)]),
            policy(max_position_weight_pct=Decimal("95"), min_cash_reserve_pct=Decimal("10")),
        )
        assert result.cash == Decimal("10000.00")
        assert PortfolioRiskFlag.CASH_RESERVE_BREACHED not in flags_of(result)

    def test_total_risk_above_the_limit_is_flagged(self):
        result = state_of(
            valuation(cash="0", positions=[position_valuation(1, 2000, 100)]),
            policy(stop_loss_pct=Decimal("5"), max_total_risk_pct=Decimal("1")),
        )
        assert PortfolioRiskFlag.TOTAL_RISK_LIMIT_BREACHED in flags_of(result)

    def test_drawdown_at_the_limit_is_flagged(self):
        """Inclusive, matching the Phase 12 drawdown guard: exactly at the limit blocks."""
        result = state_of(
            valuation(initial_capital="100000", cash="90000"),
            policy(max_drawdown_pct=Decimal("10")),
        )
        assert result.current_drawdown_pct == Decimal("10.0000")
        assert PortfolioRiskFlag.DRAWDOWN_LIMIT_BREACHED in flags_of(result)

    def test_drawdown_inside_the_limit_is_not_flagged(self):
        result = state_of(
            valuation(initial_capital="100000", cash="95000"),
            policy(max_drawdown_pct=Decimal("10")),
        )
        assert PortfolioRiskFlag.DRAWDOWN_LIMIT_BREACHED not in flags_of(result)

    def test_several_breaches_are_reported_together(self):
        result = state_of(
            valuation(
                cash="0",
                positions=[
                    position_valuation(1, 800, 100),
                    position_valuation(2, 800, 100),
                    position_valuation(3, 800, 100),
                ],
            ),
            policy(
                max_position_weight_pct=Decimal("10"),
                max_positions=1,
                max_exposure_pct=Decimal("50"),
            ),
        )
        assert {
            PortfolioRiskFlag.POSITION_CONCENTRATION_BREACHED,
            PortfolioRiskFlag.POSITION_LIMIT_BREACHED,
            PortfolioRiskFlag.EXPOSURE_LIMIT_BREACHED,
        } <= flags_of(result)

    def test_flags_appear_in_the_documented_order(self):
        """A flag list that reorders itself between two reads cannot be compared."""
        result = state_of(
            valuation(
                cash="0",
                positions=[
                    position_valuation(1, 800, 100),
                    position_valuation(2, 800, 100),
                    position_valuation(3, 800, 100),
                ],
            ),
            policy(
                max_position_weight_pct=Decimal("10"),
                max_sector_weight_pct=Decimal("10"),
                max_positions=1,
                max_exposure_pct=Decimal("50"),
            ),
            sectors={1: "IT", 2: "IT", 3: "IT"},
        )
        # Concentration is reported once per offending position, so the assertion is on the
        # sequence of distinct rules: every concentration flag, then sector, then the rest.
        rules_in_order = []
        for flag in result.flags:
            if not rules_in_order or rules_in_order[-1] != flag:
                rules_in_order.append(flag)
        assert rules_in_order == [
            PortfolioRiskFlag.POSITION_CONCENTRATION_BREACHED,
            PortfolioRiskFlag.SECTOR_CONCENTRATION_BREACHED,
            PortfolioRiskFlag.POSITION_LIMIT_BREACHED,
            PortfolioRiskFlag.EXPOSURE_LIMIT_BREACHED,
        ]

    def test_each_offending_position_gets_its_own_flag(self):
        """Which stock breached is part of the answer, so each one is named."""
        result = state_of(
            valuation(
                cash="0",
                positions=[
                    position_valuation(1, 800, 100),
                    position_valuation(2, 700, 100),
                    position_valuation(3, 100, 100),
                ],
            ),
            policy(max_position_weight_pct=Decimal("30")),
        )
        offending = [
            item.details["stock_id"]
            for item in result.flag_details
            if item.flag == PortfolioRiskFlag.POSITION_CONCENTRATION_BREACHED.value
        ]
        assert offending == ["1", "2"]

    def test_a_zero_reserve_never_flags(self):
        """A 0% reserve is a disabled reserve, not an impossible one."""
        result = state_of(
            valuation(cash="0", positions=[position_valuation(1, 100, 100)]),
            policy(min_cash_reserve_pct=Decimal("0")),
        )
        assert PortfolioRiskFlag.CASH_RESERVE_BREACHED not in flags_of(result)


class TestFlagDetails:
    def test_every_flag_carries_a_detail(self):
        result = state_of(
            valuation(cash="0", positions=[position_valuation(1, 800, 100)]),
            policy(max_position_weight_pct=Decimal("30")),
        )
        assert len(result.flag_details) == len(result.flags) == 1

    def test_the_detail_names_the_rule_and_the_numbers(self):
        result = state_of(
            valuation(cash="20000", positions=[position_valuation(1, 800, 100)]),
            policy(max_position_weight_pct=Decimal("30")),
        )
        detail = result.flag_details[0]
        assert detail.rule == "max_position_weight_pct"
        assert detail.limit == Decimal("30")
        assert detail.observed == Decimal("80.0000")
        assert detail.reason

    def test_the_detail_identifies_the_offending_stock(self):
        result = state_of(
            valuation(cash="20000", positions=[position_valuation(7, 800, 100)]),
            policy(max_position_weight_pct=Decimal("30")),
        )
        assert result.flag_details[0].details["stock_id"] == "7"

    def test_an_approved_portfolio_has_no_details(self):
        result = state_of(valuation(cash="100000"))
        assert result.flag_details == ()


class TestDeterminism:
    def test_two_evaluations_produce_identical_states(self):
        val = valuation(
            cash="1000",
            positions=[
                position_valuation(1, 500, 100, 95),
                position_valuation(2, 300, 50, 60),
            ],
        )
        strict = policy(
            max_position_weight_pct=Decimal("20"),
            max_sector_weight_pct=Decimal("30"),
            max_exposure_pct=Decimal("70"),
            max_total_risk_pct=Decimal("1"),
            max_drawdown_pct=Decimal("5"),
        )
        first = state_of(val, strict, sectors={1: "IT", 2: "IT"})
        second = state_of(val, strict, sectors={1: "IT", 2: "IT"})
        assert first.flags == second.flags
        assert first.flag_details == second.flag_details
        assert first.total_risk_amount == second.total_risk_amount

    def test_the_same_breaches_are_always_reported_in_the_same_order(self):
        val = valuation(
            cash="0",
            positions=[
                position_valuation(1, 800, 100),
                position_valuation(2, 800, 100),
                position_valuation(3, 800, 100),
            ],
        )
        strict = policy(
            max_position_weight_pct=Decimal("10"),
            max_sector_weight_pct=Decimal("10"),
            max_positions=1,
            max_exposure_pct=Decimal("50"),
        )
        runs = [state_of(val, strict, sectors={1: "IT", 2: "IT", 3: "IT"}).flags for _ in range(5)]
        assert all(item == runs[0] for item in runs)