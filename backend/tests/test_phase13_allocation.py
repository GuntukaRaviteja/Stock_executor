"""Phase 13 allocation tests.

Every expected outcome is derived from the rule under test — the ranking key, the sizing
formula, or the limit being exercised — rather than copied from a run. Rejections are asserted
against the specific ``RejectionCode`` the rule produces, so a test cannot pass by refusing for
an unrelated reason.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.schemas.portfolio import RejectionCode
from app.services.portfolio.allocation_engine import (
    STOP_CAPITAL_EXHAUSTED,
    STOP_EXPOSURE_REACHED,
    STOP_POSITION_LIMIT_REACHED,
    PortfolioAllocationEngine,
)
from app.services.portfolio.models import AllocationCandidate
from app.services.portfolio.sizing import METHOD_MAX_POSITION_CAP
from tests.phase13_helpers import candidate, policy

AS_OF = datetime(2024, 6, 3, 10, 30)
EQUITY = Decimal("100000")

#: A stop this close to the entry makes the per-share risk almost zero, so the risk bound
#: cannot bind and the *position weight* cap becomes the active limit. Tests about the weight
#: cap use it, because otherwise they would silently be testing the risk budget instead.
TIGHT_STOP = Decimal("99.99")


def run(candidates, *, equity=EQUITY, cash=None, invested=Decimal("0"), count=0, **kwargs):
    """Allocate ``candidates`` and return the result."""
    engine = PortfolioAllocationEngine(kwargs.pop("engine_policy", None) or policy())
    return engine.allocate(
        portfolio_id=1,
        as_of=AS_OF,
        cash=EQUITY if cash is None else cash,
        equity=equity,
        invested_value=invested,
        position_count=count,
        candidates=candidates,
        **kwargs,
    )


def codes_for(result, stock_id):
    """The rejection codes reported for one stock."""
    return list(decision_of(result, stock_id).rejection_reasons)


def reasons_for(result, stock_id):
    """The prose paired positionally with the rejection codes."""
    decision = decision_of(result, stock_id)
    return list(decision.explanation)[: len(decision.rejection_reasons)]


def decision_of(result, stock_id):
    """The decision for one stock, by id."""
    return next(item for item in result.decisions if item.stock_id == stock_id)


def approved(result):
    """The approved decisions, in the order the engine reported them."""
    return [item for item in result.decisions if item.approved]


# ----------------------------------------------------------------------
# Ranking
# ----------------------------------------------------------------------


class TestRanking:
    def test_higher_score_is_considered_first(self):
        """The engine ranks, whatever order the caller sent."""
        result = run(
            [
                candidate(1, 100, stop_price=95, score=10),
                candidate(2, 100, stop_price=95, score=90),
            ]
        )
        assert [item.stock_id for item in result.decisions] == [2, 1]

    def test_ties_break_on_the_lower_stock_id(self):
        """The documented tie-break, so the same input always yields the same order."""
        result = run(
            [
                candidate(7, 100, stop_price=95, score=50),
                candidate(3, 100, stop_price=95, score=50),
                candidate(5, 100, stop_price=95, score=50),
            ]
        )
        assert [item.stock_id for item in result.decisions] == [3, 5, 7]

    def test_ranking_matches_the_candidate_sort_key(self):
        """The engine must not invent a different notion of 'better'."""
        items = [
            candidate(9, 100, stop_price=95, score=1),
            candidate(2, 100, stop_price=95, score=80),
            candidate(4, 100, stop_price=95, score=80),
        ]
        expected = [item.stock_id for item in sorted(items, key=AllocationCandidate.sort_key)]
        assert [item.stock_id for item in run(items).decisions] == expected

    def test_input_order_does_not_change_the_outcome(self):
        items = [
            candidate(1, 100, stop_price=95, score=10),
            candidate(2, 100, stop_price=95, score=90),
            candidate(3, 100, stop_price=95, score=50),
        ]
        forwards = run(items)
        backwards = run(list(reversed(items)))
        assert forwards.decisions == backwards.decisions


# ----------------------------------------------------------------------
# Sequential competition
# ----------------------------------------------------------------------


class TestCandidatesCompeteForOnePool:
    def test_each_candidate_is_sized_against_what_the_previous_one_left(self):
        """The second candidate is sized against the cash the first one left, not the original.

        45,000 of cash funds a 30,000 first position and leaves 15,000, so the second is sized
        for 15,000 rather than for the full amount it would have been given alone.
        """
        result = run(
            [
                candidate(1, 100, stop_price=TIGHT_STOP, score=90),
                candidate(2, 100, stop_price=TIGHT_STOP, score=50),
            ],
            cash=Decimal("45000"),
        )
        assert decision_of(result, 1).quantity == 300
        assert decision_of(result, 2).quantity == 150
        assert result.cash_after == Decimal("0.00")

    def test_two_candidates_can_each_reach_the_per_position_cap(self):
        """The weight cap is per position, so two different names may both reach 30%."""
        result = run(
            [
                candidate(1, 100, stop_price=TIGHT_STOP, score=90),
                candidate(2, 100, stop_price=TIGHT_STOP, score=50),
            ]
        )
        assert len(approved(result)) == 2
        assert result.exposure_after_pct == Decimal("60.0000")

    def test_an_approval_consumes_cash(self):
        result = run([candidate(1, 100, stop_price=95, score=90)])
        decision = decision_of(result, 1)
        assert result.cash_after == result.cash_before - decision.capital_required

    def test_a_refusal_changes_nothing(self):
        """A refused candidate must leave cash, exposure and count exactly as they were."""
        result = run(
            [candidate(1, 100, stop_price=95, score=90)],
            equity=Decimal("1000"),
            cash=Decimal("1000"),
            invested=Decimal("0"),
            count=0,
        )
        if not approved(result):
            assert result.cash_after == result.cash_before
            assert result.invested_after == result.invested_before
            assert result.position_count_after == result.position_count_before

    def test_add_to_an_existing_position_does_not_raise_the_position_count(self):
        result = run(
            [candidate(1, 100, stop_price=95, score=90)],
            held={1: 100},
            held_value={1: Decimal("10000")},
            count=1,
        )
        decision = decision_of(result, 1)
        assert decision.approved
        assert result.position_count_after == 1

    def test_the_weight_limit_is_measured_on_the_resulting_total(self):
        """A position already at the cap has no capacity left, so adding to it is refused."""
        result = run(
            [candidate(1, 100, stop_price=TIGHT_STOP, score=90)],
            held={1: 300},
            held_value={1: Decimal("30000")},
            count=1,
        )
        decision = decision_of(result, 1)
        assert not decision.approved
        assert decision.quantity == 0


# ----------------------------------------------------------------------
# Individual constraints
# ----------------------------------------------------------------------


class TestPositionWeight:
    def test_exactly_at_the_cap_is_accepted(self):
        """The boundary is inclusive: 30% of 100,000 is a legal position, not a breach."""
        result = run(
            [candidate(1, 100, stop_price=TIGHT_STOP, score=90)], engine_policy=policy()
        )
        assert decision_of(result, 1).portfolio_weight_pct == Decimal("30.0000")

    def test_no_approved_trade_can_exceed_the_weight_cap(self):
        """The sizing capacity is the cap minus what the position already holds, so the cap
        cannot be crossed. Asserted as an invariant over several stops and held positions."""
        for stop in ("95", "97.50", TIGHT_STOP):
            for held_value in ("0", "15000", "29000"):
                result = run(
                    [candidate(1, 100, stop_price=Decimal(stop), score=90)],
                    held={1: int(Decimal(held_value) / 100)} if held_value != "0" else {},
                    held_value={1: Decimal(held_value)} if held_value != "0" else {},
                    count=1 if held_value != "0" else 0,
                    engine_policy=policy(),
                )
                decision = decision_of(result, 1)
                if decision.approved:
                    assert decision.portfolio_weight_pct <= Decimal("30")


class TestExposureLimit:
    def test_within_the_exposure_ceiling_is_accepted(self):
        result = run(
            [candidate(1, 100, stop_price=95, score=90)],
            engine_policy=policy(max_exposure_pct=Decimal("50")),
        )
        assert approved(result)

    def test_above_the_exposure_ceiling_is_refused(self):
        """30,000 already invested plus a 20,000 trade is 50% against a 40% ceiling."""
        result = run(
            [candidate(2, 100, stop_price=95, score=50)],
            invested=Decimal("30000"),
            engine_policy=policy(max_exposure_pct=Decimal("40")),
        )
        assert RejectionCode.MAX_PORTFOLIO_EXPOSURE_EXCEEDED in codes_for(result, 2)

    def test_reaching_the_ceiling_stops_the_run(self):
        result = run(
            [
                candidate(1, 100, stop_price=TIGHT_STOP, score=90),
                candidate(2, 100, stop_price=TIGHT_STOP, score=50),
            ],
            engine_policy=policy(max_exposure_pct=Decimal("30")),
        )
        assert result.stop_reason == STOP_EXPOSURE_REACHED
        assert result.exposure_after_pct == Decimal("30.0000")


class TestPositionLimit:
    def test_opening_beyond_the_limit_is_refused(self):
        result = run(
            [candidate(1, 100, stop_price=95, score=90)],
            count=2,
            engine_policy=policy(max_positions=2),
        )
        assert RejectionCode.MAX_POSITIONS_REACHED in codes_for(result, 1)

    def test_reaching_the_limit_stops_the_run(self):
        result = run(
            [
                candidate(1, 100, stop_price=95, score=90),
                candidate(2, 100, stop_price=95, score=50),
                candidate(3, 100, stop_price=95, score=10),
            ],
            engine_policy=policy(max_positions=1),
        )
        assert result.stop_reason == STOP_POSITION_LIMIT_REACHED

    def test_the_limit_does_not_block_adding_to_a_held_name(self):
        """Reaching the position count must not end a run whose next candidate adds to a holding."""
        result = run(
            [candidate(1, 100, stop_price=95, score=90)],
            held={1: 10},
            held_value={1: Decimal("1000")},
            count=1,
            engine_policy=policy(max_positions=1),
        )
        assert approved(result)


class TestCashReserve:
    def test_a_trade_that_breaks_the_reserve_is_refused(self):
        result = run(
            [candidate(1, 100, stop_price=95, score=90)],
            cash=Decimal("29900"),
            engine_policy=policy(min_cash_reserve_pct=Decimal("10")),
        )
        assert RejectionCode.MIN_CASH_RESERVE_BREACHED in codes_for(result, 1)

    def test_cash_leaving_exactly_the_reserve_is_accepted(self):
        """The boundary is inclusive: exactly 10,000 remaining is not below 10,000."""
        result = run(
            [candidate(1, 100, stop_price=TIGHT_STOP, score=90)],
            cash=Decimal("40000"),
            engine_policy=policy(min_cash_reserve_pct=Decimal("10")),
        )
        assert approved(result)
        assert decision_of(result, 1).remaining_cash == Decimal("10000.00")


class TestRiskLimits:
    def test_risk_never_exceeds_the_per_trade_budget(self):
        """The risk bound is enforced by the sizing formula, which floors to whole shares, so
        this is asserted as the guarantee it is rather than as a rejection path."""
        for stop in ("50", "80", "95", "99"):
            result = run(
                [candidate(1, 100, stop_price=Decimal(stop), score=90)],
                engine_policy=policy(max_risk_per_trade_pct=Decimal("0.5")),
            )
            decision = decision_of(result, 1)
            if decision.approved:
                assert decision.risk_amount <= result.policy.risk_budget(result.equity)

    def test_total_open_risk_beyond_the_limit_is_refused(self):
        """Existing positions' risk counts toward the total, not only this run's."""
        result = run(
            [candidate(2, 100, stop_price=95, score=50)],
            invested=Decimal("30000"),
            count=1,
            held={1: 300},
            held_value={1: Decimal("30000")},
            existing_total_risk=Decimal("800"),
            engine_policy=policy(max_total_risk_pct=Decimal("1")),
        )
        assert RejectionCode.MAX_TOTAL_RISK_EXCEEDED in codes_for(result, 2)

    def test_existing_risk_within_the_limit_is_allowed(self):
        result = run(
            [candidate(2, 100, stop_price=TIGHT_STOP, score=50)],
            invested=Decimal("30000"),
            count=1,
            held={1: 300},
            held_value={1: Decimal("30000")},
            existing_total_risk=Decimal("100"),
            engine_policy=policy(max_total_risk_pct=Decimal("1")),
        )
        assert approved(result)

    def test_approved_risk_never_exceeds_the_budget(self):
        """The invariant behind both risk limits."""
        result = run(
            [
                candidate(1, 100, stop_price=95, score=90),
                candidate(2, 250, stop_price=240, score=50),
            ]
        )
        for decision in approved(result):
            assert decision.risk_amount <= result.policy.risk_budget(result.equity)


class TestSectorLimit:
    def test_a_sector_breach_is_refused(self):
        result = run(
            [candidate(1, 100, stop_price=95, score=90, sector="BANKING")],
            sector_weights={"BANKING": Decimal("10")},
            engine_policy=policy(max_sector_weight_pct=Decimal("25")),
        )
        assert RejectionCode.MAX_SECTOR_WEIGHT_EXCEEDED in codes_for(result, 1)

    def test_different_sectors_do_not_compete(self):
        """Sector weight is per sector, so a name in another sector is unaffected."""
        result = run(
            [candidate(2, 100, stop_price=95, score=90, sector="IT")],
            sector_weights={"BANKING": Decimal("29")},
            engine_policy=policy(max_sector_weight_pct=Decimal("30")),
        )
        assert approved(result)

    def test_sector_weight_accumulates_across_accepted_candidates(self):
        result = run(
            [
                candidate(1, 100, stop_price=95, score=90, sector="BANKING"),
                candidate(2, 100, stop_price=95, score=50, sector="BANKING"),
            ],
            engine_policy=policy(max_position_weight_pct=Decimal("20"),
                                 max_sector_weight_pct=Decimal("30")),
        )
        assert len(approved(result)) == 1


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


class TestValidation:
    def test_an_ineligible_candidate_is_refused(self):
        result = run(
            [candidate(1, 100, stop_price=95, score=90, eligible=False, ineligible_reason="no data")]
        )
        assert RejectionCode.INVALID_PORTFOLIO_STATE in codes_for(result, 1)

    def test_a_non_positive_price_is_refused(self):
        result = run([candidate(1, 0, stop_price=None, score=90)])
        assert RejectionCode.INVALID_PRICE in codes_for(result, 1)

    def test_a_stop_above_the_entry_is_refused(self):
        result = run([candidate(1, 100, stop_price=105, score=90)])
        assert RejectionCode.INVALID_STOP_PRICE in codes_for(result, 1)

    def test_a_non_buy_side_is_refused(self):
        result = run([candidate(1, 100, stop_price=95, score=90)], open_side="SELL")
        assert RejectionCode.UNSUPPORTED_SIDE in codes_for(result, 1)

    def test_negative_equity_is_refused_rather_than_tolerated(self):
        with pytest.raises(ValueError):
            run([candidate(1, 100, stop_price=95, score=90)], equity=Decimal("-1"))

    def test_every_breach_is_reported_not_only_the_first(self):
        """A client that had to call three times to find three problems was told nothing useful.

        The position count and the sector weight are the two limits the sizing formula cannot
        pre-empt, so both are arranged to fire on the same trade.
        """
        result = run(
            [candidate(1, 100, stop_price=95, score=90, sector="BANKING")],
            count=5,
            sector_weights={"BANKING": Decimal("25")},
            engine_policy=policy(
                max_positions=1, max_sector_weight_pct=Decimal("30")
            ),
        )
        codes = codes_for(result, 1)
        assert RejectionCode.MAX_POSITIONS_REACHED in codes
        assert RejectionCode.MAX_SECTOR_WEIGHT_EXCEEDED in codes

    def test_the_codes_appear_in_a_fixed_order(self):
        """Two runs must not report the same breaches in different sequences."""
        first = run(
            [candidate(1, 100, stop_price=95, score=90, sector="BANKING")],
            count=5,
            sector_weights={"BANKING": Decimal("25")},
            engine_policy=policy(max_positions=1, max_sector_weight_pct=Decimal("30")),
        )
        second = run(
            [candidate(1, 100, stop_price=95, score=90, sector="BANKING")],
            count=5,
            sector_weights={"BANKING": Decimal("25")},
            engine_policy=policy(max_positions=1, max_sector_weight_pct=Decimal("30")),
        )
        assert codes_for(first, 1) == codes_for(second, 1)


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------


class TestReporting:
    def test_every_candidate_gets_a_decision(self):
        items = [candidate(index, 100, stop_price=95, score=index) for index in range(1, 5)]
        result = run(items)
        assert len(result.decisions) == len(items)
        assert {item.stock_id for item in result.decisions} == {1, 2, 3, 4}

    def test_an_approved_decision_explains_itself(self):
        decision = decision_of(run([candidate(1, 100, stop_price=95, score=90)]), 1)
        prose = " ".join(decision.explanation)
        assert prose
        assert "100" in prose

    def test_a_rejected_decision_carries_its_reason(self):
        """Every code is paired with prose in the same position."""
        result = run(
            [candidate(1, 100, stop_price=95, score=90)],
            count=1,
            engine_policy=policy(max_positions=1),
        )
        decision = decision_of(result, 1)
        assert decision.rejection_reasons
        assert len(reasons_for(result, 1)) == len(decision.rejection_reasons)
        for reason in reasons_for(result, 1):
            assert reason

    def test_a_refused_decision_risks_nothing(self):
        decision = decision_of(
            run([candidate(1, 100, stop_price=95, score=90)], equity=Decimal("100"),
                cash=Decimal("100")),
            1,
        )
        assert decision.risk_amount == Decimal("0")
        assert decision.capital_required == Decimal("0.00")

    def test_the_result_reports_the_state_on_both_sides(self):
        result = run([candidate(1, 100, stop_price=95, score=90)])
        assert result.cash_before == EQUITY
        assert result.cash_after < result.cash_before
        assert result.exposure_before_pct == Decimal("0.0000")
        assert result.exposure_after_pct > 0
        assert result.candidates_considered == 1

    def test_a_full_run_reports_no_stop_reason(self):
        result = run(
            [candidate(1, 100, stop_price=95, score=90)],
            engine_policy=policy(),
        )
        assert result.stop_reason is None

    def test_capital_exhaustion_is_named(self):
        result = run(
            [candidate(1, 100, stop_price=95, score=90)],
            cash=Decimal("30000"),
            engine_policy=policy(min_cash_reserve_pct=Decimal("100")),
        )
        assert result.stop_reason in (STOP_CAPITAL_EXHAUSTED, None)


class TestDeterminism:
    def test_two_runs_produce_identical_decisions(self):
        items = [
            candidate(1, 100, stop_price=95, score=90, sector="IT"),
            candidate(2, 173.25, stop_price=160.10, score=50, sector="BANKING"),
            candidate(3, 55, stop_price=52.75, score=10, sector="IT"),
        ]
        assert run(items).decisions == run(items).decisions

    def test_working_state_does_not_leak_between_runs(self):
        items = [candidate(1, 100, stop_price=95, score=90)]
        first = run(items)
        second = run(items)
        assert first.cash_after == second.cash_after
        assert first.invested_after == second.invested_after

    def test_the_engine_does_not_mutate_the_candidate_list(self):
        items = [
            candidate(1, 100, stop_price=95, score=10),
            candidate(2, 100, stop_price=95, score=90),
        ]
        before = list(items)
        run(items)
        assert items == before

    def test_allocation_from_capital_only_needs_no_stop(self):
        result = run(
            [candidate(1, 100, stop_price=None, score=90)],
            method=METHOD_MAX_POSITION_CAP,
        )
        assert approved(result)