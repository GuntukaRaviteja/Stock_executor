"""Phase 11 portfolio construction — the pure decision engine.

Given a portfolio, some eligible candidates and a configuration, this module answers one
question per stock: *what should the portfolio do about it?* It is a pure function of its
arguments — no clock, no database, no network, no randomness — so the same inputs always produce
identical decisions.

The decision ladder, in evaluation order
----------------------------------------
For a stock the portfolio does **not** hold:

1. no usable price → ``HOLD``, ineligible, ``no_price``;
2. target weight is zero → ``HOLD``, ``no_target``;
3. target value below the minimum position value → ``HOLD``, ineligible,
   ``below_min_position_value``;
4. the configured position count is already used up → ``HOLD``, blocked, ``max_positions``;
5. a limit refuses the buy (cash, cash reserve, or an injected pre-trade risk check) → ``BUY``,
   blocked, with every offending rule named;
6. otherwise → ``BUY``.

For a stock the portfolio **does** hold:

1. the risk engine recommends an action → ``REDUCE`` (trim) or ``SELL`` (close), naming the rule;
2. the position is above the single-position cap and past the rebalancing threshold → ``REDUCE``;
3. the weight is below target by at least the threshold → ``BUY``;
4. otherwise → ``HOLD``.

Ordering
--------
Decisions come back sorted by **composite score descending, then stock id ascending** — the key
:meth:`~app.services.portfolio.models.Candidate.sort_key` defines. Stock ids are unique, so the
ordering is total: two runs over the same candidates cannot swap, and equal scores never depend
on dictionary or insertion order.

Explainability
--------------
Every decision carries ``reasons`` (stable machine-readable codes) and ``explanation`` (the same
rules in prose, including the numbers). Nothing is scored opaquely: there is no weighted blend,
no hidden threshold and no tie-break by insertion order, because an unexplainable decision is
indistinguishable from a guess.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from app.schemas.portfolio import DecisionAction, PortfolioStatus, PositionSizingMethod

from app.services.backtest.models import quantize_money
from app.services.portfolio.allocation import (
    affordable_quantity,
    allocate_equal_weights,
    budget_for_weight,
    capped_target_pct,
    risk_amount,
    risk_based_quantity,
    risk_budget,
    target_quantity_for_weight,
    weight_difference_pct,
)
from app.services.portfolio.calculator import allocation_pct, gross_value
from app.services.portfolio.models import (
    ZERO,
    AllocationConfig,
    Candidate,
    PortfolioDecision,
    PortfolioDecisions,
    PortfolioRiskReport,
    PositionValuation,
    TradingCostConfig,
)

_ONE_HUNDRED = Decimal("100")

#: Stable, machine-readable reason codes. A caller branches on these, never on the prose.
REASON_NO_PRICE = "no_price"
REASON_NO_TARGET = "no_target"
REASON_ELIGIBLE = "eligible_candidate"
REASON_BELOW_MIN_VALUE = "below_min_position_value"
REASON_MAX_POSITIONS = "max_positions"
REASON_RISK_BLOCKED = "risk_limit_blocked"
REASON_CASH_INSUFFICIENT = "insufficient_cash"
REASON_CASH_RESERVE = "cash_reserve_reached"
REASON_AT_TARGET = "at_target_weight"
REASON_BELOW_THRESHOLD = "below_rebalance_threshold"
REASON_UNDERWEIGHT = "underweight"
REASON_OVERWEIGHT = "overweight"
REASON_POSITION_OVER_CAP = "position_over_cap"
REASON_EQUAL_WEIGHT = "equal_weight_allocation"
REASON_RISK_BASED = "risk_based_sizing"
REASON_POSITION_CAP = "position_cap_applied"


def build_target_weights(
    candidates: Sequence[Candidate],
    config: AllocationConfig,
) -> Dict[int, Decimal]:
    """Target weight per candidate, derived with the configured allocation formula.

    ``EQUAL_WEIGHT`` splits the weight across the eligible candidates and caps each share;
    ``MAX_POSITION_CAP`` and ``RISK_BASED`` target the capped weight for each. For ``RISK_BASED``
    that is deliberate: the *risk* budget sizes the quantity, so the target weight is the
    ceiling rather than the instruction, and the per-share risk only becomes meaningful once a
    price and a stop are known.

    Ineligible candidates get no target at all, which keeps an unpriceable stock from diluting
    the split across the others.
    """
    eligible = [candidate for candidate in candidates if candidate.eligible]
    if not eligible:
        return {}

    ordered = sorted(eligible, key=Candidate.sort_key)
    limit = effective_position_count(config, len(ordered))
    ids = [candidate.stock_id for candidate in ordered]

    if config.sizing_method is PositionSizingMethod.EQUAL_WEIGHT:
        return allocate_equal_weights(
            ids,
            max_position_weight_pct=config.max_position_weight_pct,
            target_count=limit,
        )
    capped = capped_target_pct(_ONE_HUNDRED, config.max_position_weight_pct)
    return {stock_id: capped for stock_id in ids[:limit]}


def effective_position_count(config: AllocationConfig, candidate_count: int) -> int:
    """How many names this allocation may target.

    ``target_position_count`` wins when configured; otherwise the candidate list itself sets the
    count, capped by ``max_positions``. There is no hidden default number: when neither is
    configured the answer is simply "every eligible candidate".
    """
    count = config.target_position_count if config.target_position_count is not None else candidate_count
    if config.max_positions is not None:
        count = min(count, config.max_positions)
    return max(count, 0)


def build_decisions(
    *,
    portfolio_id: int,
    status: PortfolioStatus,
    as_of: datetime,
    equity: Decimal,
    cash: Decimal,
    invested_value: Decimal,
    candidates: Sequence[Candidate],
    positions: Sequence[PositionValuation],
    config: AllocationConfig,
    costs: TradingCostConfig,
    risk_report: PortfolioRiskReport,
    risk_check=None,
) -> PortfolioDecisions:
    """The full decision set for one construction run.

    ``risk_check`` is an optional callable ``(stock_id, quantity, price) -> Sequence[str]``
    returning the machine-readable rules that would refuse a buy of that size at that price. It
    is injected rather than imported so this module stays free of the database, while a caller
    that *can* fund the check gets decisions that already account for cash, exposure and the
    cash reserve. Omitting it still yields a complete decision set — the risk report's own
    recommendations are always honoured.
    """
    reserve_required = quantize_money(equity * config.cash_reserve_pct / _ONE_HUNDRED)
    targets = build_target_weights(candidates, config)
    held = {position.stock_id: position for position in positions}

    # Names already held do not consume a *new* position slot, so the budget for new names is the
    # configured limit minus the positions that already exist.
    openings_left = (
        None if config.max_positions is None else max(config.max_positions - len(held), 0)
    )

    investable_cash = quantize_money(max(Decimal(cash) - reserve_required, ZERO))
    # Equity that is not yet invested. Buying more than this would leave less cash than the
    # reserve requires, since equity is cash plus holdings.
    uninvested_equity = quantize_money(max(Decimal(equity) - Decimal(invested_value), ZERO))

    ordered = sorted(candidates, key=Candidate.sort_key)
    decisions: List[PortfolioDecision] = []
    skipped: List[PortfolioDecision] = []
    cash_left = investable_cash
    uninvested_left = uninvested_equity
    opened = 0

    for candidate in ordered:
        target_weight = targets.get(candidate.stock_id, quantize_money(ZERO))
        position = held.get(candidate.stock_id)

        if not candidate.eligible:
            skipped.append(
                _skipped_decision(
                    candidate,
                    reason=candidate.ineligible_reason or REASON_NO_PRICE,
                    explanation=(
                        candidate.ineligible_reason
                        or f"Stock {candidate.stock_id} is not eligible for allocation."
                    ),
                )
            )
            continue

        if position is None and openings_left is not None and opened >= openings_left:
            skipped.append(
                _skipped_decision(
                    candidate,
                    target_weight=target_weight,
                    reason=REASON_MAX_POSITIONS,
                    explanation=(
                        f"Stock {candidate.stock_id} targets {target_weight}% of equity, but the "
                        f"portfolio already holds {len(held)} position(s) against a configured "
                        f"maximum of {config.max_positions}, so no new position was opened."
                    ),
                )
            )
            continue

        decision = _decide_for_candidate(
            candidate=candidate,
            position=position,
            target_weight_pct=target_weight,
            equity=equity,
            investable_cash=cash_left,
            reserve_required=reserve_required,
            config=config,
            costs=costs,
            risk_report=risk_report,
            risk_check=risk_check,
            uninvested_equity=uninvested_left,
        )
        decisions.append(decision)

        if decision.action is DecisionAction.BUY and not decision.blocked:
            bought = decision.target_quantity - decision.current_quantity
            if bought > 0:
                opened += 1
                cash_left = quantize_money(max(cash_left - decision.estimated_trade_value, ZERO))
                uninvested_left = quantize_money(
                    max(uninvested_left - decision.estimated_trade_value, ZERO)
                )

    return PortfolioDecisions(
        portfolio_id=portfolio_id,
        status=status,
        as_of=as_of,
        sizing_method=config.sizing_method,
        equity=equity,
        cash=cash,
        invested_value=invested_value,
        cash_reserve_required=reserve_required,
        candidate_count=len(ordered),
        eligible_count=len([candidate for candidate in ordered if candidate.eligible]),
        decisions=tuple(decisions),
        skipped=tuple(skipped),
        risk_violations=tuple(violation.rule for violation in risk_report.violations),
        configuration=config,
    )


def _decide_for_candidate(
    *,
    candidate: Candidate,
    position: Optional[PositionValuation],
    target_weight_pct: Decimal,
    equity: Decimal,
    investable_cash: Decimal,
    reserve_required: Decimal,
    config: AllocationConfig,
    costs: TradingCostConfig,
    risk_report: PortfolioRiskReport,
    risk_check,
    uninvested_equity: Decimal,
) -> PortfolioDecision:
    """The decision for one stock.

    ``investable_cash`` is the cash still available *after* holding back the configured reserve,
    and is decremented by the caller as buys are proposed, so a run can never commit the same
    money twice or commit the reserve at all.
    """
    stock_id = candidate.stock_id
    price = candidate.price
    current_quantity = position.quantity if position else 0
    current_weight = allocation_pct(position.market_value, equity) if position else None
    difference = weight_difference_pct(current_weight, target_weight_pct)

    if price is None or price <= ZERO:
        return PortfolioDecision(
            stock_id=stock_id,
            action=DecisionAction.HOLD,
            target_weight_pct=target_weight_pct,
            current_quantity=current_quantity,
            current_weight_pct=current_weight,
            weight_difference_pct=difference,
            symbol=candidate.symbol,
            sector=candidate.sector,
            reasons=(REASON_NO_PRICE,),
            explanation=(
                f"Stock {stock_id} has no usable market price, so no quantity can be sized. The "
                "portfolio is left unchanged.",
            ),
        )

    # A held position answers to the risk engine before it answers to the allocation.
    if position is not None:
        recommended = risk_report.recommendation_for(stock_id)
        if recommended is not None:
            action = (
                DecisionAction.REDUCE
                if recommended.action.value == DecisionAction.REDUCE.value
                else DecisionAction.SELL
            )
            return PortfolioDecision(
                stock_id=stock_id,
                action=action,
                target_weight_pct=target_weight_pct,
                target_quantity=max(current_quantity - recommended.quantity, 0),
                current_quantity=current_quantity,
                current_weight_pct=current_weight,
                weight_difference_pct=difference,
                price=price,
                estimated_trade_value=gross_value(recommended.quantity, price),
                reasons=(recommended.rule,),
                explanation=(recommended.reason,),
                symbol=candidate.symbol or recommended.symbol,
                sector=candidate.sector,
            )

        if (
            current_weight is not None
            and current_weight > config.max_position_weight_pct
            and difference is not None
            and difference >= config.rebalance_threshold_pct
        ):
            return _reduction_decision(
                stock_id=stock_id,
                symbol=candidate.symbol,
                sector=candidate.sector,
                target_weight_pct=target_weight_pct,
                current_quantity=current_quantity,
                current_weight=current_weight,
                difference=difference,
                price=price,
                target_quantity=target_quantity_for_weight(
                    equity, target_weight_pct, price, costs
                ),
                reasons=(REASON_POSITION_OVER_CAP, REASON_OVERWEIGHT),
                explanation=(
                    f"Stock {stock_id} is {current_weight}% of {equity} equity, above the "
                    f"{config.max_position_weight_pct}% single-position cap, and its "
                    f"{difference}% drift is at or beyond the "
                    f"{config.rebalance_threshold_pct}% threshold.",
                    f"Reducing stock {stock_id} towards its {target_weight_pct}% target reallocates "
                    "capital instead of adding to it, and keeps the position rather than "
                    "closing it.",
                ),
            )

    if target_weight_pct <= ZERO:
        return PortfolioDecision(
            stock_id=stock_id,
            action=DecisionAction.HOLD,
            target_weight_pct=target_weight_pct,
            current_quantity=current_quantity,
            target_quantity=current_quantity,
            current_weight_pct=current_weight,
            weight_difference_pct=difference,
            price=price,
            symbol=candidate.symbol,
            sector=candidate.sector,
            reasons=(REASON_NO_TARGET,),
            explanation=(
                f"Stock {stock_id} is not part of this allocation, so it has no target weight. "
                "No trade is proposed."
            ),
        )

    target_budget = budget_for_weight(equity, target_weight_pct)
    if position is None and config.min_position_value is not None and target_budget < config.min_position_value:
        return PortfolioDecision(
            stock_id=stock_id,
            action=DecisionAction.HOLD,
            eligible=False,
            target_weight_pct=target_weight_pct,
            current_quantity=current_quantity,
            current_weight_pct=current_weight,
            weight_difference_pct=difference,
            price=price,
            symbol=candidate.symbol,
            sector=candidate.sector,
            reasons=(REASON_BELOW_MIN_VALUE,),
            explanation=(
                f"Stock {stock_id} targets {target_weight_pct}% of {equity} equity, which is "
                f"{target_budget} — below the configured minimum position value of "
                f"{config.min_position_value}. No position is opened rather than opening one "
                "too small to be meaningful."
            ),
        )

    target_quantity = _target_quantity(
        equity=equity,
        target_weight_pct=target_weight_pct,
        price=price,
        config=config,
        costs=costs,
        investable_cash=investable_cash,
    )
    required = target_quantity - current_quantity

    if difference is not None and abs(difference) < config.rebalance_threshold_pct:
        if abs(difference) == ZERO:
            reason = REASON_AT_TARGET
            text = (
                f"Stock {stock_id} is already at its {target_weight_pct}% target "
                f"({current_weight}% of equity). No trade is needed."
            )
        else:
            reason = REASON_BELOW_THRESHOLD
            text = (
                f"Stock {stock_id} is {current_weight}% of equity against a "
                f"{target_weight_pct}% target: a drift of {difference}%, inside the "
                f"{config.rebalance_threshold_pct}% threshold, so no trade is proposed."
            )
        return PortfolioDecision(
            stock_id=stock_id,
            action=DecisionAction.HOLD,
            target_weight_pct=target_weight_pct,
            current_quantity=current_quantity,
            target_quantity=current_quantity,
            current_weight_pct=current_weight,
            weight_difference_pct=difference,
            price=price,
            reasons=(reason,),
            explanation=(text,),
            symbol=candidate.symbol,
            sector=candidate.sector,
        )

    if required == 0:
        return PortfolioDecision(
            stock_id=stock_id,
            action=DecisionAction.HOLD,
            target_weight_pct=target_weight_pct,
            current_quantity=current_quantity,
            target_quantity=current_quantity,
            current_weight_pct=current_weight,
            weight_difference_pct=difference,
            price=price,
            reasons=(REASON_AT_TARGET,),
            explanation=(
                f"Stock {stock_id} already holds the {target_quantity} share(s) its "
                f"{target_weight_pct}% target works out to. No trade is needed."
            ),
            symbol=candidate.symbol,
            sector=candidate.sector,
        )

    if required > 0:
        return _buy_decision(
            candidate=candidate,
            target_weight_pct=target_weight_pct,
            target_quantity=target_quantity,
            current_quantity=current_quantity,
            current_weight=current_weight,
            difference=difference,
            required=required,
            price=price,
            equity=equity,
            investable_cash=investable_cash,
            reserve_required=reserve_required,
            config=config,
            risk_check=risk_check,
            uninvested_equity=uninvested_equity,
        )

    return _reduction_decision(
        stock_id=stock_id,
        symbol=candidate.symbol,
        sector=candidate.sector,
        target_weight_pct=target_weight_pct,
        current_quantity=current_quantity,
        current_weight=current_weight,
        difference=difference,
        price=price,
        target_quantity=target_quantity,
        reasons=(REASON_OVERWEIGHT,),
        explanation=(
            f"Stock {stock_id} is {current_weight}% of equity against a "
            f"{target_weight_pct}% target: a drift of {difference}%, at or beyond the "
            f"{config.rebalance_threshold_pct}% threshold.",
            _reduce_explanation(stock_id, current_quantity, target_quantity),
        ),
    )


def _buy_decision(
    *,
    candidate: Candidate,
    target_weight_pct: Decimal,
    target_quantity: int,
    current_quantity: int,
    current_weight: Optional[Decimal],
    difference: Optional[Decimal],
    required: int,
    price: Decimal,
    equity: Decimal,
    investable_cash: Decimal,
    reserve_required: Decimal,
    config: AllocationConfig,
    risk_check,
    uninvested_equity: Decimal,
) -> PortfolioDecision:
    """A ``BUY``: open or add to a position, including every reason it may be blocked.

    ``investable_cash`` is what the run has left to spend, so the size proposed here is already
    fundable; the cash checks below therefore only fire when the *weight target* could not be
    reached, which is a different thing from the trade being unaffordable.
    """
    stock_id = candidate.stock_id
    trade_value = gross_value(required, price)
    budget = risk_budget(equity, config.risk_per_trade_pct)
    exposure = risk_amount(required, price, config.stop_loss_pct)
    target_budget = budget_for_weight(equity, target_weight_pct)

    reasons: List[str] = [
        REASON_RISK_BASED
        if config.sizing_method is PositionSizingMethod.RISK_BASED
        else REASON_EQUAL_WEIGHT
        if config.sizing_method is PositionSizingMethod.EQUAL_WEIGHT
        else REASON_POSITION_CAP
    ]
    explanation: List[str] = []

    if config.sizing_method is PositionSizingMethod.EQUAL_WEIGHT:
        explanation.append(
            f"An equal-weight split targets {target_weight_pct}% of equity for each eligible "
            f"name, so {required} share(s) must be bought to reach it."
        )
    elif config.sizing_method is PositionSizingMethod.RISK_BASED:
        risk_amount_allowed = risk_budget(equity, config.risk_per_trade_pct)
        per_share_risk = quantize_money(price * config.stop_loss_pct / Decimal("100"))
        explanation.append(
            f"Risk-based sizing: {config.risk_per_trade_pct}% of {equity} equity is "
            f"{risk_amount_allowed}, and {per_share_risk} of risk per share at a "
            f"{config.stop_loss_pct}% stop gives {required} share(s) to buy."
        )

    if target_weight_pct >= config.max_position_weight_pct:
        if REASON_POSITION_CAP not in reasons:
            reasons.append(REASON_POSITION_CAP)
        explanation.append(
            f"The single-position cap of {config.max_position_weight_pct}% applies, so the "
            f"target is capped at {target_weight_pct}%."
        )
    reasons.append(REASON_ELIGIBLE)
    explanation.append(
        f"Stock {stock_id} is an eligible candidate with a target weight of "
        f"{target_weight_pct}% of {equity} equity ({target_budget}), which is "
        f"{target_quantity} share(s) at {price}."
    )

    if current_quantity == 0:
        reasons.append(REASON_UNDERWEIGHT)
        explanation.append(
            f"The portfolio holds no position in stock {stock_id} (0% of equity), below the "
            f"{target_weight_pct}% target."
        )
    else:
        reasons.append(REASON_UNDERWEIGHT)
        explanation.append(
            f"Stock {stock_id} is {current_weight}% of equity against a {target_weight_pct}% "
            f"target: a drift of {difference}%, at or beyond the "
            f"{config.rebalance_threshold_pct}% threshold."
        )

    blocked: List[str] = []
    if target_quantity <= 0:
        blocked.append(REASON_CASH_INSUFFICIENT)
        explanation.append(
            f"Blocked: the target works out to no shares, because only {investable_cash} of the "
            f"portfolio's cash is investable after holding back the {reserve_required} cash "
            "reserve."
        )
    if trade_value > uninvested_equity:
        blocked.append(REASON_CASH_RESERVE)
        explanation.append(
            f"Blocked: the buy needs {trade_value} but only {uninvested_equity} of portfolio "
            f"equity is not already invested, so committing it would leave cash below the "
            f"{reserve_required} reserve."
        )
    if risk_check is not None:
        for rule in risk_check(stock_id, required, price):
            blocked.append(rule)
            explanation.append(
                f"Blocked: the pre-trade risk engine refused this buy on rule '{rule}'."
            )

    if blocked and REASON_RISK_BLOCKED not in reasons:
        reasons.append(REASON_RISK_BLOCKED)
    reasons.extend(blocked)

    return PortfolioDecision(
        stock_id=stock_id,
        action=DecisionAction.BUY,
        target_weight_pct=target_weight_pct,
        target_quantity=target_quantity,
        current_quantity=current_quantity,
        current_weight_pct=current_weight,
        weight_difference_pct=difference,
        price=price,
        estimated_trade_value=trade_value,
        risk_budget=budget,
        risk_exposure=exposure,
        blocked=bool(blocked),
        reasons=tuple(reasons),
        explanation=tuple(explanation),
        symbol=candidate.symbol,
        sector=candidate.sector,
    )


def _reduction_decision(
    *,
    stock_id: int,
    symbol: Optional[str],
    sector: Optional[str],
    target_weight_pct: Decimal,
    current_quantity: int,
    current_weight: Optional[Decimal],
    difference: Optional[Decimal],
    price: Decimal,
    target_quantity: int,
    reasons: Sequence[str],
    explanation: Sequence[str],
) -> PortfolioDecision:
    """A ``REDUCE``: trim an overweight position back to its target weight.

    Exactly the surplus is sold — the difference between what is held and what the target weight
    works out to at this price — and the remainder of the position stays alive. A full exit is a
    ``SELL``, which only a risk rule or an explicit zero target produces.

    ``target_quantity`` is the pure weight target, deliberately *not* the cash-bounded figure
    :func:`_target_quantity` returns: trimming does not need cash, so there is nothing to fund, and
    sizing the sale down to the investable cash would strand the surplus in the position.
    """
    sellable = max(current_quantity - target_quantity, 0)
    return PortfolioDecision(
        stock_id=stock_id,
        action=DecisionAction.REDUCE,
        target_weight_pct=target_weight_pct,
        target_quantity=current_quantity - sellable,
        current_quantity=current_quantity,
        current_weight_pct=current_weight,
        weight_difference_pct=difference,
        price=price,
        estimated_trade_value=gross_value(sellable, price),
        reasons=tuple(reasons),
        explanation=tuple(explanation),
        symbol=symbol,
        sector=sector,
    )


def _reduce_explanation(stock_id: int, current_quantity: int, target_quantity: int) -> str:
    """Prose for what a reduction does."""
    return (
        f"Stock {stock_id} is trimmed from {current_quantity} to "
        f"{max(current_quantity - target_quantity, 0) + target_quantity} share(s), bringing it back "
        f"to its target weight: capital is reallocated rather than added, and the position is "
        "kept rather than closed."
    )


def _target_quantity(
    *,
    equity: Decimal,
    target_weight_pct: Decimal,
    price: Decimal,
    config: AllocationConfig,
    costs: TradingCostConfig,
    investable_cash: Decimal,
) -> int:
    """The share count a target weight works out to, under the configured formula.

    Every formula is bounded by ``investable_cash`` — the cash left once the reserve is held
    back. Sizing down to what is fundable rather than refusing the buy is deliberate: the
    reserve is a self-imposed budget, not a prohibition, and "hold 50 shares because 5000 is
    investable" is a better answer than "you may not buy the 80 shares the target wanted". A
    risk *limit* is the opposite case and does refuse, further down, via ``risk_check``.

    ``RISK_BASED`` is bounded by that cash as well as by the risk budget, because a risk budget
    is not a cash budget: risking 1% of 100,000 through a 5% stop is 200 shares, which costs far
    more than the risk budget figure.
    """
    if config.sizing_method is PositionSizingMethod.RISK_BASED:
        return risk_based_quantity(
            equity=equity,
            risk_per_trade_pct=config.risk_per_trade_pct,
            entry_price=price,
            stop_loss_pct=config.stop_loss_pct,
            costs=costs,
            max_capital=investable_cash,
        )
    return min(
        target_quantity_for_weight(equity, target_weight_pct, price, costs),
        affordable_quantity(investable_cash, price, costs),
    )


def _skipped_decision(
    candidate: Candidate,
    *,
    reason: str,
    explanation: str,
    target_weight: Decimal = quantize_money(ZERO),
) -> PortfolioDecision:
    """A candidate that was considered and could not be held at all."""
    return PortfolioDecision(
        stock_id=candidate.stock_id,
        action=DecisionAction.HOLD,
        eligible=False,
        blocked=True,
        target_weight_pct=target_weight,
        reasons=(reason,),
        explanation=(explanation,),
        symbol=candidate.symbol,
        sector=candidate.sector,
    )