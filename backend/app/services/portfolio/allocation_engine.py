"""Phase 13 allocation — the deterministic portfolio allocation workflow.

A **pure** module. It reads a portfolio snapshot and a list of candidate signals and returns a
decision for each; it never touches a session, a clock or the network, and it never mutates its
arguments. The same portfolio, the same candidates and the same policy always produce the same
decisions, the same quantities and the same rejection codes.

The workflow
------------
Exactly the six steps the portfolio engine has to answer, in order:

1. **collect** — the candidates, in whatever order the caller sent them;
2. **validate** — side supported, price present and positive, stop usable;
3. **rank** — composite score descending, then stock id ascending. That is
   :meth:`~app.services.portfolio.models.AllocationCandidate.sort_key`, the *same* key the Phase 11
   construction engine uses, so the two can never disagree about which candidate is better and
   neither invents a score;
4. **size** — the risk-and-weight formula from
   :mod:`app.services.portfolio.sizing`, bounded by the weight capacity still available;
5. **allocate** — apply the position, then re-check every portfolio limit against the *new*
   state, so the second candidate is sized against what the first one actually left behind;
6. **stop** — when capital is exhausted, the exposure ceiling is reached, the position count is
   reached, or no remaining candidate could be accepted.

Why allocation is sequential
----------------------------
Candidates compete for one pool of cash and one exposure ceiling, so they cannot be sized
independently. Sizing them in parallel would let three candidates each be approved for a third of
the portfolio and then all three fill it. One at a time, with the constraints re-checked after
each, is what makes the outcome a portfolio rather than a list of individually reasonable trades.

Every refused candidate is still reported, and carries **every** reason it was refused rather than
only the first. One call therefore answers "what happened to this stock" completely, which is what
an explainable allocation decision has to do. A refusal changes nothing: the working state is only
advanced by an approval.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from app.services.backtest.models import quantize_money, quantize_percent
from app.services.portfolio.allocation import side_value, stop_price
from app.services.portfolio.calculator import allocation_pct, gross_value, quantize_price
from app.services.portfolio.models import (
    ZERO,
    AllocationCandidate,
    AllocationDecision,
    AllocationResult,
    PortfolioRiskPolicy,
)
from app.services.portfolio.sizing import (
    METHOD_MAX_POSITION_CAP,
    METHOD_RISK_AND_WEIGHT,
    PositionSizingEngine,
)
from app.schemas.portfolio import RejectionCode
from app.schemas.risk import BindingConstraint

#: The two sizing models the allocation engine supports.
ALLOCATION_METHODS = (METHOD_RISK_AND_WEIGHT, METHOD_MAX_POSITION_CAP)

#: Why a run stopped before every candidate had been considered. Named rather than described, so
#: a caller can branch on it. ``None`` means every candidate was considered.
STOP_CAPITAL_EXHAUSTED = "capital_exhausted"
STOP_EXPOSURE_REACHED = "max_exposure_reached"
STOP_POSITION_LIMIT_REACHED = "max_positions_reached"
STOP_NOTHING_LEFT_TO_ACCEPT = "no_further_candidate_acceptable"

#: Rejection code reported for a candidate the run never reached, per stop reason.
_STOP_CODES = {
    STOP_CAPITAL_EXHAUSTED: RejectionCode.INSUFFICIENT_CAPITAL,
    STOP_EXPOSURE_REACHED: RejectionCode.MAX_PORTFOLIO_EXPOSURE_EXCEEDED,
    STOP_POSITION_LIMIT_REACHED: RejectionCode.MAX_POSITIONS_REACHED,
    STOP_NOTHING_LEFT_TO_ACCEPT: RejectionCode.INSUFFICIENT_CAPITAL,
}


class PortfolioAllocationEngine:
    """Turns a portfolio snapshot and a list of signals into a deterministic allocation."""

    def __init__(
        self,
        policy: Optional[PortfolioRiskPolicy] = None,
        sizer: Optional[PositionSizingEngine] = None,
    ):
        self.policy = policy or PortfolioRiskPolicy()
        self.sizer = sizer or PositionSizingEngine()

    # --- Public API ---

    def allocate(
        self,
        *,
        portfolio_id: int,
        as_of: datetime,
        cash: Decimal,
        equity: Decimal,
        invested_value: Decimal,
        position_count: int,
        candidates: Sequence[AllocationCandidate] = (),
        held: Optional[Dict[int, int]] = None,
        held_value: Optional[Dict[int, Decimal]] = None,
        sector_weights: Optional[Dict[str, Decimal]] = None,
        existing_total_risk: Optional[Decimal] = None,
        policy: Optional[PortfolioRiskPolicy] = None,
        method: str = METHOD_RISK_AND_WEIGHT,
        open_side: str = "BUY",
    ) -> AllocationResult:
        """Allocate capital across ``candidates``, one at a time, in ranking order.

        ``held`` maps a stock id to the shares already held and ``held_value`` to their current
        market value. Together they let the engine tell *adding to* a position from *opening* one
        — the position-count limit only binds when a new position is opened, and the weight limit
        is measured on the resulting total rather than on the increment. ``sector_weights`` seeds
        the sector totals from the portfolio's *existing* holdings, so the sector cap is measured
        against the whole portfolio rather than only the candidates being considered.
        ``existing_total_risk`` does the same for ``max_total_risk_pct``: the risk open positions
        already carry, entry price to stop. Without it that limit would measure only the risk
        this run creates, and a portfolio already at its risk ceiling could keep adding to it.

        Raises:
            ValueError: the portfolio's own state is not usable (negative equity or cash), which
                would make every weight and exposure figure meaningless. Reported rather than
                tolerated.
        """
        policy = policy or self.policy
        held = dict(held or {})
        held_value = dict(held_value or {})

        cash = Decimal(cash)
        equity = Decimal(equity)
        invested_value = Decimal(invested_value)

        if equity < ZERO:
            raise ValueError("portfolio equity must not be negative")
        if cash < ZERO:
            raise ValueError("portfolio cash must not be negative")

        ordered = sorted(candidates, key=AllocationCandidate.sort_key)
        cash_before = quantize_money(cash)
        invested_before = quantize_money(invested_value)
        count_before = int(position_count)

        working = _WorkingState(
            cash=cash_before,
            invested=invested_before,
            position_count=count_before,
            sector_weights=dict(sector_weights or {}),
            total_risk=quantize_money(ZERO if existing_total_risk is None else existing_total_risk),
        )
        held = dict(held)

        decisions: List[AllocationDecision] = []
        stop_reason: Optional[str] = None

        for index, candidate in enumerate(ordered):
            if stop_reason is not None:
                decisions.append(self._not_considered(candidate, stop_reason, working.cash, equity))
                continue

            decision, stop_reason = self._allocate_one(
                candidate=candidate,
                policy=policy,
                method=method,
                open_side=open_side,
                equity=equity,
                working=working,
                held=held,
                held_value=held_value,
                remaining=tuple(item.stock_id for item in ordered[index + 1 :]),
            )
            decisions.append(decision)
            if decision.approved and decision.quantity:
                held[candidate.stock_id] = held.get(candidate.stock_id, 0) + decision.quantity
                held_value[candidate.stock_id] = quantize_money(
                    held_value.get(candidate.stock_id, ZERO)
                    + gross_value(decision.quantity, decision.entry_price)
                )

        return AllocationResult(
            portfolio_id=portfolio_id,
            as_of=as_of,
            equity=quantize_money(equity),
            cash_before=cash_before,
            cash_after=working.cash,
            invested_before=invested_before,
            invested_after=working.invested,
            exposure_before_pct=allocation_pct(invested_before, equity),
            exposure_after_pct=allocation_pct(working.invested, equity),
            position_count_before=count_before,
            position_count_after=working.position_count,
            policy=policy,
            candidates_considered=len(ordered),
            decisions=tuple(decisions),
            stop_reason=stop_reason,
        )

    # --- One candidate ---

    def _allocate_one(
        self,
        *,
        candidate: AllocationCandidate,
        policy: PortfolioRiskPolicy,
        method: str,
        open_side: str,
        equity: Decimal,
        working: "_WorkingState",
        held: Dict[int, int],
        held_value: Dict[int, Decimal],
        remaining: Sequence[int],
    ) -> Tuple[AllocationDecision, Optional[str]]:
        """Validate, size, allocate and re-check one candidate.

        ``working`` is advanced **in place** and only ever by an approval, which is what keeps a
        refusal a complete no-op.
        """
        if side_value(open_side) != "BUY":
            return self._reject(
                candidate,
                equity=equity,
                working=working,
                codes=(RejectionCode.UNSUPPORTED_SIDE,),
                reasons=(
                    (
                        RejectionCode.UNSUPPORTED_SIDE,
                        f"Unsupported side '{side_value(open_side)}': this engine is long-only "
                    "and sizes BUY "
                        "positions. SELL reduces or closes an existing long.",
                    ),
                ),
            ), None

        if not candidate.eligible:
            return self._reject(
                candidate,
                equity=equity,
                working=working,
                codes=(RejectionCode.INVALID_PORTFOLIO_STATE,),
                reasons=(
                    (
                        RejectionCode.INVALID_PORTFOLIO_STATE,
                        candidate.ineligible_reason
                        or "The candidate was supplied as ineligible.",
                    ),
                ),
            ), None

        entry = quantize_price(Decimal(candidate.entry_price))
        if entry <= ZERO:
            return self._reject(
                candidate,
                equity=equity,
                working=working,
                codes=(RejectionCode.INVALID_PRICE,),
                reasons=(
                    (
                        RejectionCode.INVALID_PRICE,
                        f"Invalid entry price {entry}: an entry price must be greater than zero.",
                    ),
                ),
            ), None

        existing = int(held.get(candidate.stock_id, candidate.held_quantity) or 0)
        existing_value = quantize_money(
            held_value.get(candidate.stock_id, gross_value(existing, entry))
        )

        stop, stop_problem = _resolve_stop(candidate, policy, entry, method)
        if stop_problem is not None:
            code, reason = stop_problem
            return self._reject(
                candidate,
                equity=equity,
                working=working,
                codes=(code,),
                reasons=((code, reason),),
                entry=entry,
            ), None

        # --- Weight capacity for this position: the cap, minus what is already in it ---
        capacity = quantize_money(policy.max_position_value(equity) - existing_value)

        breakdown = self.sizer.size(
            equity=equity,
            available_cash=working.cash,
            entry_price=entry,
            stop_price=stop,
            policy=policy,
            method=method,
            existing_quantity=existing,
            remaining_weight_capacity=capacity,
        )
        quantity = breakdown.quantity
        capital_required = breakdown.capital_required

        if quantity <= 0:
            return self._reject(
                candidate,
                equity=equity,
                working=working,
                codes=(RejectionCode.INSUFFICIENT_CAPITAL,),
                reasons=(
                    (
                        RejectionCode.INSUFFICIENT_CAPITAL,
                        f"No whole share of stock {candidate.stock_id} fits inside the risk "
                        f"budget of {breakdown.risk_budget}, the "
                        f"{policy.max_position_weight_pct}% position cap "
                        f"({breakdown.max_position_value}) and the {working.cash} of cash "
                        "available. No position is opened: a size the policy cannot afford is no "
                        "position, not a smaller one.",
                    ),
                ),
                entry=entry,
                stop=stop,
                breakdown=breakdown,
            ), None

        # --- Re-check every portfolio limit against the *resulting* state ---
        trade_value = gross_value(quantity, entry)
        resulting_position_value = quantize_money(existing_value + trade_value)
        resulting_invested = quantize_money(working.invested + trade_value)
        cash_after = quantize_money(working.cash - capital_required)
        opening_new_position = existing <= 0
        resulting_count = working.position_count + (1 if opening_new_position else 0)
        risk_amount = breakdown.risk_amount
        risk_budget = policy.risk_budget(equity)

        codes, reasons = self._constraint_breaches(
            candidate=candidate,
            policy=policy,
            equity=equity,
            working=working,
            trade_value=trade_value,
            capital_required=capital_required,
            resulting_position_value=resulting_position_value,
            resulting_invested=resulting_invested,
            cash_after=cash_after,
            opening_new_position=opening_new_position,
            risk_amount=risk_amount,
            risk_budget=risk_budget,
        )

        if codes:
            return self._reject(
                candidate,
                equity=equity,
                working=working,
                codes=codes,
                reasons=reasons,
                entry=entry,
                stop=stop,
                breakdown=breakdown,
                resulting_exposure_pct=allocation_pct(resulting_invested, equity),
            ), None

        position_weight = allocation_pct(resulting_position_value, equity)
        resulting_exposure = allocation_pct(resulting_invested, equity)

        decision = AllocationDecision(
            stock_id=candidate.stock_id,
            approved=True,
            quantity=quantity,
            entry_price=entry,
            stop_price=stop,
            capital_required=capital_required,
            risk_amount=risk_amount,
            portfolio_weight_pct=position_weight,
            resulting_exposure_pct=resulting_exposure,
            remaining_cash=cash_after,
            rejection_reasons=(),
            explanation=(
                f"Approved {quantity} share(s) of stock {candidate.stock_id} at {entry}"
                + (f" with a stop at {stop}" if stop is not None else "")
                + f": capital required {capital_required} including {breakdown.fees} of fees.",
                f"Resulting position weight {position_weight}% of equity against the "
                f"{policy.max_position_weight_pct}% cap; resulting portfolio exposure "
                f"{resulting_exposure}%; cash left {cash_after}.",
                f"Risk at the stop is {risk_amount}, against a budget of {risk_budget} "
                f"({policy.max_risk_per_trade_pct}% of equity).",
                breakdown.explanation,
            ),
            binding_constraint=BindingConstraint(breakdown.binding_constraint),
            composite_score=candidate.composite_score,
            symbol=candidate.symbol,
            sector=candidate.sector,
            sizing=breakdown,
        )

        # --- Accepted: advance the working state ---
        working.cash = cash_after
        working.invested = resulting_invested
        working.position_count = resulting_count
        working.total_risk = quantize_money(working.total_risk + risk_amount)
        increment = allocation_pct(trade_value, equity) or ZERO
        if candidate.sector is not None:
            working.sector_weights[candidate.sector] = quantize_percent(
                working.sector_weights.get(candidate.sector, ZERO) + increment
            )

        stop_reason = _stop_reason(
            policy=policy,
            cash=cash_after,
            invested=resulting_invested,
            equity=equity,
            position_count=resulting_count,
            remaining=remaining,
            held=held,
        )
        return decision, stop_reason

    def _constraint_breaches(
        self,
        *,
        candidate: AllocationCandidate,
        policy: PortfolioRiskPolicy,
        equity: Decimal,
        working: "_WorkingState",
        trade_value: Decimal,
        capital_required: Decimal,
        resulting_position_value: Decimal,
        resulting_invested: Decimal,
        cash_after: Decimal,
        opening_new_position: bool,
        risk_amount: Decimal,
        risk_budget: Decimal,
    ) -> tuple:
        """Every portfolio limit this trade would break, in a fixed evaluation order.

        Fixed order so a request that breaks several rules always reports them in the same
        sequence. Every breach is reported, never only the first: a client that had to call three
        times to discover three problems with one proposal was not told anything useful.
        """
        codes: List[RejectionCode] = []
        reasons: List[tuple] = []

        weight = allocation_pct(resulting_position_value, equity)
        if weight is not None and weight > policy.max_position_weight_pct:
            codes.append(RejectionCode.MAX_POSITION_WEIGHT_EXCEEDED)
            reasons.append(
                (
                    RejectionCode.MAX_POSITION_WEIGHT_EXCEEDED,
                    f"This trade would make stock {candidate.stock_id} {weight}% of portfolio "
                    f"equity, above the {policy.max_position_weight_pct}% single-position cap.",
                )
            )

        exposure = allocation_pct(resulting_invested, equity)
        if policy.max_exposure_pct is not None and exposure is not None:
            if exposure > policy.max_exposure_pct:
                codes.append(RejectionCode.MAX_PORTFOLIO_EXPOSURE_EXCEEDED)
                reasons.append(
                    (
                        RejectionCode.MAX_PORTFOLIO_EXPOSURE_EXCEEDED,
                        f"This trade would leave {resulting_invested} invested, {exposure}% of "
                        f"portfolio equity, above the {policy.max_exposure_pct}% limit.",
                    )
                )

        if opening_new_position and policy.max_positions is not None:
            if working.position_count >= policy.max_positions:
                codes.append(RejectionCode.MAX_POSITIONS_REACHED)
                reasons.append(
                    (
                        RejectionCode.MAX_POSITIONS_REACHED,
                        f"The portfolio already holds {working.position_count} open position(s) "
                        f"and the limit is {policy.max_positions}.",
                    )
                )

        reserve_required = policy.cash_reserve_required(equity)
        if policy.min_cash_reserve_pct > ZERO and cash_after < reserve_required:
            codes.append(RejectionCode.MIN_CASH_RESERVE_BREACHED)
            reasons.append(
                (
                    RejectionCode.MIN_CASH_RESERVE_BREACHED,
                    f"This trade would leave {cash_after} in cash but {reserve_required} is "
                    f"required ({policy.min_cash_reserve_pct}% of equity).",
                )
            )

        if capital_required > working.cash:
            codes.append(RejectionCode.INSUFFICIENT_CAPITAL)
            reasons.append(
                (
                    RejectionCode.INSUFFICIENT_CAPITAL,
                    f"This trade needs {capital_required} but only {working.cash} is available.",
                )
            )

        if risk_amount > risk_budget:
            codes.append(RejectionCode.MAX_RISK_PER_TRADE_EXCEEDED)
            reasons.append(
                (
                    RejectionCode.MAX_RISK_PER_TRADE_EXCEEDED,
                    f"This trade would risk {risk_amount} at its stop, above the {risk_budget} "
                    f"budget ({policy.max_risk_per_trade_pct}% of equity).",
                )
            )

        risk_limit = policy.max_total_risk(equity)
        if risk_limit is not None:
            new_total_risk = quantize_money(working.total_risk + risk_amount)
            if new_total_risk > risk_limit:
                codes.append(RejectionCode.MAX_TOTAL_RISK_EXCEEDED)
                reasons.append(
                    (
                        RejectionCode.MAX_TOTAL_RISK_EXCEEDED,
                        f"The open positions would carry {new_total_risk} of risk to their "
                        f"stops, above the {risk_limit} allowed "
                        f"({policy.max_total_risk_pct}% of equity).",
                    )
                )

        if policy.max_sector_weight_pct is not None and candidate.sector is not None:
            sector_after = quantize_percent(
                working.sector_weights.get(candidate.sector, ZERO)
                + (allocation_pct(trade_value, equity) or ZERO)
            )
            if sector_after > policy.max_sector_weight_pct:
                codes.append(RejectionCode.MAX_SECTOR_WEIGHT_EXCEEDED)
                reasons.append(
                    (
                        RejectionCode.MAX_SECTOR_WEIGHT_EXCEEDED,
                        f"This trade would make sector '{candidate.sector}' {sector_after}% of "
                        f"portfolio equity, above the {policy.max_sector_weight_pct}% cap.",
                    )
                )

        return codes, reasons

    # --- Builders ---

    def _reject(
        self,
        candidate: AllocationCandidate,
        *,
        equity: Decimal,
        working: "_WorkingState",
        codes: Sequence[RejectionCode],
        reasons: Sequence[tuple],
        entry: Optional[Decimal] = None,
        stop: Optional[Decimal] = None,
        breakdown=None,
        resulting_exposure_pct: Optional[Decimal] = None,
    ) -> AllocationDecision:
        """A refused candidate, keeping every reason it was refused.

        The working state is untouched, so a refusal leaves the portfolio exactly as it was —
        which is what makes the whole run reproducible.
        """
        return AllocationDecision(
            stock_id=candidate.stock_id,
            approved=False,
            quantity=0,
            entry_price=(
                entry if entry is not None else quantize_price(Decimal(candidate.entry_price))
            ),
            stop_price=stop,
            capital_required=ZERO,
            risk_amount=ZERO,
            portfolio_weight_pct=None,
            resulting_exposure_pct=(
                resulting_exposure_pct
                if resulting_exposure_pct is not None
                else allocation_pct(working.invested, equity)
            ),
            remaining_cash=working.cash,
            rejection_reasons=tuple(codes),
            explanation=tuple(reason for _, reason in reasons)
            + ((breakdown.explanation,) if breakdown is not None else ()),
            binding_constraint=(
                BindingConstraint(breakdown.binding_constraint) if breakdown is not None else None
            ),
            composite_score=candidate.composite_score,
            symbol=candidate.symbol,
            sector=candidate.sector,
            sizing=breakdown,
        )

    def _not_considered(
        self,
        candidate: AllocationCandidate,
        stop_reason: str,
        cash: Decimal,
        equity: Decimal,
    ) -> AllocationDecision:
        """A candidate the run never reached, because the run stopped earlier.

        Reported rather than dropped: "this stock was on the list and was not reached" is a
        result, and a caller planning a rebalance needs to know the difference between *refused*
        and *never looked at*.
        """
        code = _STOP_CODES.get(stop_reason, RejectionCode.INSUFFICIENT_CAPITAL)
        return AllocationDecision(
            stock_id=candidate.stock_id,
            approved=False,
            quantity=0,
            entry_price=quantize_price(Decimal(candidate.entry_price)),
            stop_price=candidate.stop_price,
            capital_required=ZERO,
            risk_amount=ZERO,
            portfolio_weight_pct=None,
            resulting_exposure_pct=None,
            remaining_cash=cash,
            rejection_reasons=(code,),
            explanation=(
                f"Not considered: the allocation stopped earlier because "
                f"{stop_reason.replace('_', ' ')}. Equity at that point was {equity} and "
                f"{cash} of cash remained.",
            ),
            composite_score=candidate.composite_score,
            symbol=candidate.symbol,
            sector=candidate.sector,
        )


class _WorkingState:
    """The portfolio state as allocation proceeds. Advanced only by an accepted allocation."""

    __slots__ = ("cash", "invested", "position_count", "sector_weights", "total_risk")

    def __init__(
        self,
        *,
        cash: Decimal,
        invested: Decimal,
        position_count: int,
        sector_weights: Dict[str, Decimal],
        total_risk: Decimal,
    ):
        self.cash = cash
        self.invested = invested
        self.position_count = position_count
        self.sector_weights = sector_weights
        self.total_risk = total_risk


def _resolve_stop(
    candidate: AllocationCandidate, policy: PortfolioRiskPolicy, entry: Decimal, method: str
) -> tuple:
    """The stop to size against, and a ``(code, reason)`` problem when there isn't one.

    An explicit ``stop_price`` on the candidate wins; otherwise the policy's ``stop_loss_pct``
    derives one from the entry. When neither exists and the method needs one, the candidate is
    refused with ``INVALID_STOP_PRICE`` — reported rather than silently degrading to a
    capital-only size, because "sized without knowing the risk" is not a risk-based size.
    """
    if candidate.stop_price is not None:
        stop = quantize_price(Decimal(candidate.stop_price))
        if stop <= ZERO:
            return None, (
                RejectionCode.INVALID_STOP_PRICE,
                f"Invalid stop price {stop}: a long position's stop must be greater than zero.",
            )
        if stop >= entry:
            return None, (
                RejectionCode.INVALID_STOP_PRICE,
                f"Invalid stop price {stop}: a long position's stop must be strictly below its "
                f"entry price of {entry}. A stop at or above the entry defines no per-share risk.",
            )
        return stop, None

    if policy.stop_loss_pct is not None:
        derived = stop_price(entry, policy.stop_loss_pct)
        if derived is None or derived <= ZERO:
            return None, (
                RejectionCode.INVALID_STOP_PRICE,
                f"The policy's stop_loss_pct of {policy.stop_loss_pct}% produces a stop of "
                f"{derived} at an entry of {entry}, which is not a usable stop for a long "
                "position.",
            )
        return derived, None

    if method == METHOD_MAX_POSITION_CAP:
        return None, None

    return None, (
        RejectionCode.INVALID_STOP_PRICE,
        "Risk-based sizing needs a stop: entry_price - stop_price is the per-share risk, so "
        "without one no position size is defensible. Supply 'stop_price' on the candidate or "
        "'stop_loss_pct' in the policy, or size with MAX_POSITION_CAP.",
    )


def _stop_reason(
    *,
    policy: PortfolioRiskPolicy,
    cash: Decimal,
    invested: Decimal,
    equity: Decimal,
    position_count: int,
    remaining: Sequence[int],
    held: Dict[int, int],
) -> Optional[str]:
    """Why no further candidate can be accepted, or ``None`` when one still can.

    Checked in a fixed order so two runs over the same portfolio name the same reason. A limit is
    only treated as reached when it would actually stop the **remaining** candidates: reaching the
    position count does not end a run whose next candidate adds to a position already held.
    """
    if policy.max_positions is not None and position_count >= policy.max_positions:
        if any(stock_id not in held for stock_id in remaining):
            return STOP_POSITION_LIMIT_REACHED
    if (
        policy.max_exposure_pct is not None
        and equity > ZERO
        and allocation_pct(invested, equity) >= policy.max_exposure_pct
    ):
        return STOP_EXPOSURE_REACHED
    if cash <= policy.cash_reserve_required(equity):
        return STOP_CAPITAL_EXHAUSTED
    return None