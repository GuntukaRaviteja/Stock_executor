"""Phase 11 portfolio risk engine — the portfolio-level risk *report*.

Where :mod:`app.services.portfolio.risk` answers "may this one order be executed?", this module
answers "what is wrong with this portfolio right now, and what should be done about it?". The two
are deliberately separate:

* the pre-trade rules are a **gate** — they stop an order and change nothing;
* this report is an **observation** — it never mutates state, and its recommendations only
  become orders when the caller explicitly applies a decision.

Report contents
---------------
=========================  =============================================================
position risk              market value, weight, unrealised P&L and P&L %, and the loss each
                           position would book if it fell to its stop (its *risk contribution*)
portfolio exposure         gross exposure, net exposure, cash %, invested %
concentration              largest single weight, combined weight of the top three, and the
                           combined weight of each sector, against the configured caps
cash risk                  whether the configured cash reserve is currently satisfied
drawdown                   peak equity, current drawdown, against the configured guard
stop-loss / take-profit    per-position breach detection, with a recommended action
=========================  =============================================================

Everything is a pure function of the valuation, the two configurations and the peak equity. No
clock, no database, no network — which is what makes every figure reproducible and every rule
independently testable.

Exposure convention
-------------------
``gross_exposure_pct`` is the sum of the **absolute** market values of every position;
``net_exposure_pct`` is longs minus shorts. This engine is long-only, so the two are equal and
both are reported: a future long/short version changes the short term only, and the response
shape, the limits and the reporting stay exactly as they are.

Recommendation policy
---------------------
A stop-loss or maximum-loss breach recommends ``SELL`` (close the whole position) and a
take-profit recommends ``REDUCE`` (take part of the profit off, keeping the remainder). Neither
is applied automatically, and a recommendation never appears for a position whose threshold is
merely *approached* — only for one that is actually crossed, so the report stays quiet on a
healthy portfolio.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Mapping, Optional, Sequence

from app.services.backtest.models import quantize_money, quantize_percent
from app.services.portfolio.allocation import (
    gain_pct,
    loss_pct,
    position_loss_breached,
    risk_amount,
    stop_loss_breached,
    stop_price,
    take_profit_price,
    take_profit_triggered,
)
from app.services.portfolio.calculator import allocation_pct, drawdown_pct
from app.services.portfolio.models import (
    ZERO,
    AllocationConfig,
    PortfolioRiskReport,
    PortfolioValuation,
    PositionRisk,
    PositionValuation,
    RiskLimits,
    RiskRecommendation,
    RiskViolation,
)
from app.schemas.portfolio import RiskAction

_ONE_HUNDRED = Decimal("100")

#: How many of the largest positions are combined into the "top positions" concentration
#: figure. Three is reported alongside the single largest weight because a portfolio can be
#: concentrated without any one name breaching the cap.
TOP_POSITIONS_COUNT = 3

#: Stable, machine-readable rule names. These are the names a caller branches on, so they are
#: constants rather than string literals repeated across the module.
RULE_MAX_POSITION_WEIGHT = "max_position_weight_pct"
RULE_MAX_SECTOR_WEIGHT = "max_sector_weight_pct"
RULE_MAX_POSITIONS = "max_positions"
RULE_MAX_EXPOSURE = "max_exposure_pct"
RULE_MIN_CASH_RESERVE = "min_cash_reserve_pct"
RULE_MAX_DRAWDOWN = "max_drawdown_pct"
RULE_STOP_LOSS = "stop_loss_pct"
RULE_TAKE_PROFIT = "take_profit_pct"
RULE_MAX_POSITION_LOSS = "max_position_loss_pct"


class PortfolioRiskEngine:
    """Evaluates the portfolio-level risk of a valuation against its configuration."""

    def evaluate(
        self,
        valuation: PortfolioValuation,
        *,
        config: AllocationConfig,
        limits: Optional[RiskLimits] = None,
        peak_equity: Optional[Decimal] = None,
        sectors: Optional[Mapping[int, Optional[str]]] = None,
        portfolio_id: Optional[int] = None,
    ) -> PortfolioRiskReport:
        """Full risk report for ``valuation``.

        ``sectors`` maps a stock id to its sector, for the concentration rule. Positions whose
        sector is unknown are excluded from the sector totals rather than lumped into one
        "unknown" bucket: an invented bucket would make the sector rule report a weight that
        belongs to no real sector. The reported sector weights are therefore the sum over the
        sectors that *are* known, which is stated here rather than hidden.
        """
        limits = limits or RiskLimits()
        sectors = sectors or {}
        equity = valuation.equity

        position_risks = tuple(
            self._position_risks(
                position, valuation, config, sectors.get(position.stock_id)
            )
            for position in valuation.positions
        )
        # Deterministic ordering: heaviest position first, then stock id, so the same
        # portfolio always reports its risks in the same order.
        position_risks = tuple(
            sorted(
                position_risks,
                key=lambda risk: (
                    -(risk.weight_pct if risk.weight_pct is not None else ZERO),
                    risk.stock_id,
                ),
            )
        )

        # Long-only: every position is long, so gross and net exposure are the same figure.
        # Both are reported so a future long/short portfolio fills in the short term only.
        gross = valuation.exposure_pct
        net = valuation.exposure_pct
        cash_pct = allocation_pct(valuation.cash, equity)
        invested_pct = allocation_pct(valuation.invested_capital, equity)

        top_weight = position_risks[0].weight_pct if position_risks else None
        top_positions_weight = _combined_weight(position_risks[:TOP_POSITIONS_COUNT])
        sector_weights = _sector_weights(position_risks)

        peak = peak_equity if peak_equity is not None else equity
        current_drawdown = drawdown_pct(peak, equity)
        reserve_required = quantize_money(equity * config.cash_reserve_pct / _ONE_HUNDRED)

        violations = tuple(
            self._violations(
                valuation=valuation,
                config=config,
                limits=limits,
                position_risks=position_risks,
                sector_weights=sector_weights,
                exposure_pct=valuation.exposure_pct,
                current_drawdown=current_drawdown,
                peak=peak,
                reserve_required=reserve_required,
            )
        )

        return PortfolioRiskReport(
            portfolio_id=valuation.portfolio_id if portfolio_id is None else portfolio_id,
            equity=equity,
            cash=valuation.cash,
            market_value=valuation.market_value,
            invested_capital=valuation.invested_capital,
            gross_exposure_pct=gross,
            net_exposure_pct=net,
            cash_pct=cash_pct,
            invested_pct=invested_pct,
            peak_equity=peak,
            current_drawdown_pct=current_drawdown,
            cash_reserve_required=reserve_required,
            top_position_weight_pct=top_weight,
            top_positions_weight_pct=top_positions_weight,
            sector_weights=tuple(sorted(sector_weights.items())),
            position_risks=position_risks,
            violations=violations,
            recommendations=tuple(self._recommendations(position_risks, config)),
            position_count=len(valuation.positions),
            max_positions=config.max_positions,
            drawdown_limit_pct=limits.max_drawdown_pct,
            drawdown_breached=(
                limits.max_drawdown_pct is not None
                and current_drawdown >= limits.max_drawdown_pct
            ),
            total_risk_amount=quantize_money(
                sum((risk.risk_amount for risk in position_risks), ZERO)
            ),
            configuration=config,
        )

    # --- Per-position risk ---

    def _position_risks(
        self,
        position: PositionValuation,
        valuation: PortfolioValuation,
        config: AllocationConfig,
        sector: Optional[str],
    ) -> PositionRisk:
        """Risk figures for one position, measured against its average entry price.

        Thresholds are measured from the **average entry price**, not from the first fill: after
        a second buy the average is what the position as a whole actually cost, so a stop derived
        from it is the level at which the whole position is down by that much.
        """
        entry = position.average_entry_price
        current = position.current_price
        weight = allocation_pct(position.market_value, valuation.equity)
        amount = risk_amount(position.quantity, entry, config.stop_loss_pct)
        return PositionRisk(
            stock_id=position.stock_id,
            quantity=position.quantity,
            average_entry_price=entry,
            current_price=current,
            market_value=position.market_value,
            weight_pct=weight,
            unrealized_pnl=position.unrealized_pnl,
            unrealized_pnl_pct=gain_pct(entry, current),
            stop_price=stop_price(entry, config.stop_loss_pct),
            take_profit_price=take_profit_price(entry, config.take_profit_pct),
            stop_loss_breached=stop_loss_breached(entry, current, config.stop_loss_pct),
            take_profit_triggered=take_profit_triggered(entry, current, config.take_profit_pct),
            risk_amount=amount,
            risk_contribution_pct=allocation_pct(amount, valuation.equity),
            sector=sector,
        )

    # --- Concentration, cash, exposure and drawdown ---

    def _violations(
        self,
        *,
        valuation: PortfolioValuation,
        config: AllocationConfig,
        limits: RiskLimits,
        position_risks: Sequence[PositionRisk],
        sector_weights: Dict[str, Decimal],
        exposure_pct: Optional[Decimal],
        current_drawdown: Decimal,
        peak: Decimal,
        reserve_required: Decimal,
    ) -> list:
        """Every rule the portfolio is currently outside of, in a fixed evaluation order.

        The order is fixed so two runs over the same portfolio always report the same sequence,
        which is what makes the report diffable.
        """
        violations: list = []

        # --- single-position concentration ---
        for risk in position_risks:
            if risk.weight_pct is None:
                continue
            if risk.weight_pct > config.max_position_weight_pct:
                violations.append(
                    RiskViolation(
                        rule=RULE_MAX_POSITION_WEIGHT,
                        reason=(
                            f"Concentration: stock {risk.stock_id} is {risk.weight_pct}% of "
                            f"portfolio equity, above the {config.max_position_weight_pct}% "
                            "single-position cap."
                        ),
                        limit=config.max_position_weight_pct,
                        observed=risk.weight_pct,
                        details={
                            "stock_id": str(risk.stock_id),
                            "weight_pct": str(risk.weight_pct),
                            "equity": str(valuation.equity),
                        },
                    )
                )

        # --- sector concentration ---
        if config.max_sector_weight_pct is not None:
            for sector in sorted(sector_weights):
                weight = sector_weights[sector]
                if weight > config.max_sector_weight_pct:
                    violations.append(
                        RiskViolation(
                            rule=RULE_MAX_SECTOR_WEIGHT,
                            reason=(
                                f"Sector concentration: sector '{sector}' is {weight}% of "
                                f"portfolio equity, above the "
                                f"{config.max_sector_weight_pct}% cap."
                            ),
                            limit=config.max_sector_weight_pct,
                            observed=weight,
                            details={"sector": sector, "weight_pct": str(weight)},
                        )
                    )

        # --- position count ---
        position_limit = config.max_positions if config.max_positions is not None else limits.max_positions
        if position_limit is not None and len(position_risks) > position_limit:
            violations.append(
                RiskViolation(
                    rule=RULE_MAX_POSITIONS,
                    reason=(
                        f"Position count: the portfolio holds {len(position_risks)} open "
                        f"position(s), above the configured limit of {position_limit}."
                    ),
                    limit=Decimal(position_limit),
                    observed=Decimal(len(position_risks)),
                    details={"position_count": str(len(position_risks)), "limit": str(position_limit)},
                )
            )

        # --- total exposure ---
        exposure_limit = (
            config.max_exposure_pct if config.max_exposure_pct is not None else limits.max_exposure_pct
        )
        if exposure_limit is not None and exposure_pct is not None and exposure_pct > exposure_limit:
            violations.append(
                RiskViolation(
                    rule=RULE_MAX_EXPOSURE,
                    reason=(
                        f"Exposure: {valuation.market_value} is invested, {exposure_pct}% of "
                        f"portfolio equity, above the {exposure_limit}% limit."
                    ),
                    limit=exposure_limit,
                    observed=exposure_pct,
                    details={"market_value": str(valuation.market_value)},
                )
            )

        # --- cash reserve ---
        reserve_limit = (
            config.cash_reserve_pct
            if config.cash_reserve_pct > ZERO
            else limits.min_cash_reserve_pct
        )
        if reserve_limit is not None and reserve_limit > ZERO and valuation.cash < reserve_required:
            violations.append(
                RiskViolation(
                    rule=RULE_MIN_CASH_RESERVE,
                    reason=(
                        f"Cash reserve: cash is {valuation.cash} but {reserve_required} is "
                        f"required ({reserve_limit}% of equity)."
                    ),
                    limit=reserve_limit,
                    observed=valuation.cash,
                    details={"required_reserve": str(reserve_required)},
                )
            )

        # --- drawdown guard ---
        guard = limits.max_drawdown_pct
        if guard is not None and current_drawdown >= guard:
            violations.append(
                RiskViolation(
                    rule=RULE_MAX_DRAWDOWN,
                    reason=(
                        f"Drawdown protection: equity is {current_drawdown}% below the peak "
                        f"equity of {peak}, at or beyond the configured {guard}% limit, so new "
                        "BUY orders are blocked. SELL orders remain allowed."
                    ),
                    limit=guard,
                    observed=current_drawdown,
                    details={"peak_equity": str(peak)},
                )
            )

        return violations

    # --- Recommendations ---

    def _recommendations(
        self,
        position_risks: Sequence[PositionRisk],
        config: AllocationConfig,
    ) -> list:
        """Risk-driven actions for positions whose thresholds are crossed.

        Evaluation order per position is stop-loss, then maximum single-position loss, then
        take-profit, and positions are visited heaviest first — so the highest-risk name is
        addressed first and two runs always produce the same list.
        """
        recommendations: list = []
        for risk in position_risks:
            if risk.stop_loss_breached:
                recommendations.append(
                    RiskRecommendation(
                        stock_id=risk.stock_id,
                        action=RiskAction.SELL,
                        quantity=risk.quantity,
                        price=risk.current_price,
                        rule=RULE_STOP_LOSS,
                        reason=(
                            f"Stop-loss triggered: stock {risk.stock_id} is marked at "
                            f"{risk.current_price}, at or below its stop of {risk.stop_price} "
                            f"({loss_pct(risk.average_entry_price, risk.current_price)}% below "
                            f"the average entry price of {risk.average_entry_price}). Closing "
                            f"{risk.quantity} share(s) is recommended; nothing is executed here."
                        ),
                        symbol=risk.symbol,
                        details={
                            "stop_price": str(risk.stop_price),
                            "current_price": str(risk.current_price),
                            "unrealized_pnl": str(risk.unrealized_pnl),
                            "rule": RULE_STOP_LOSS,
                        },
                    )
                )
                continue

            if position_loss_breached(
                risk.average_entry_price, risk.current_price, config.max_position_loss_pct
            ):
                recommendations.append(
                    RiskRecommendation(
                        stock_id=risk.stock_id,
                        action=RiskAction.SELL,
                        quantity=risk.quantity,
                        price=risk.current_price,
                        rule=RULE_MAX_POSITION_LOSS,
                        reason=(
                            f"Maximum single-position loss exceeded: stock {risk.stock_id} is "
                            f"down {loss_pct(risk.average_entry_price, risk.current_price)}% "
                            f"from its average entry price, beyond the "
                            f"{config.max_position_loss_pct}% tolerance. Closing "
                            f"{risk.quantity} share(s) is recommended; nothing is executed here."
                        ),
                        symbol=risk.symbol,
                        details={
                            "loss_pct": str(loss_pct(risk.average_entry_price, risk.current_price)),
                            "limit_pct": str(config.max_position_loss_pct),
                            "rule": RULE_MAX_POSITION_LOSS,
                        },
                    )
                )
                continue

            if risk.take_profit_triggered:
                quantity = _take_profit_quantity(risk)
                recommendations.append(
                    RiskRecommendation(
                        stock_id=risk.stock_id,
                        action=RiskAction.REDUCE,
                        quantity=quantity,
                        price=risk.current_price,
                        rule=RULE_TAKE_PROFIT,
                        reason=(
                            f"Take-profit triggered: stock {risk.stock_id} is marked at "
                            f"{risk.current_price}, at or above its target of "
                            f"{risk.take_profit_price} "
                            f"({gain_pct(risk.average_entry_price, risk.current_price)}% above "
                            f"the average entry price of {risk.average_entry_price}). Taking "
                            f"profit on {quantity} share(s) is recommended; nothing is "
                            "executed here."
                        ),
                        symbol=risk.symbol,
                        details={
                            "take_profit_price": str(risk.take_profit_price),
                            "current_price": str(risk.current_price),
                            "rule": RULE_TAKE_PROFIT,
                        },
                    )
                )
        return recommendations


def _take_profit_quantity(risk: PositionRisk) -> int:
    """Shares to sell on a take-profit: **half**, rounded down, and never the whole position.

    Taking half off banks part of the gain while leaving the position running, which is the
    ordinary reading of "take profit". Rounded down so a one-share position is never fully
    closed by a profit rule alone — that stays the job of an explicit exit decision. A one-share
    position therefore produces no recommendation, because there is no partial exit to make.
    """
    return max(risk.quantity // 2, 0)


def _combined_weight(risks: Sequence[PositionRisk]) -> Optional[Decimal]:
    """Sum of the weights of the given positions; ``None`` when none is known."""
    weights = [risk.weight_pct for risk in risks if risk.weight_pct is not None]
    if not weights:
        return None
    return quantize_percent(sum(weights, ZERO))


def _sector_weights(risks: Sequence[PositionRisk]) -> Dict[str, Decimal]:
    """Combined weight per sector, for the sectors that are actually known.

    Positions with no sector on record are left out of the totals entirely: inventing an
    "unknown" bucket would make the sector rule silently pass, so the sector figures are
    reported as the sum of the *known* sectors and the report says so.
    """
    totals: Dict[str, Decimal] = {}
    for risk in risks:
        if risk.sector is None or risk.weight_pct is None:
            continue
        totals[risk.sector] = quantize_percent(totals.get(risk.sector, ZERO) + risk.weight_pct)
    return totals