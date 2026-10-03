"""Phase 13 portfolio risk state — where a portfolio stands against its policy, right now.

A **pure** module: no clock, no database, no FastAPI, no randomness. Everything is a function
of the supplied valuation, the supplied history and the supplied policy, so the same three always
produce the same state and the same flags — which is what makes the state auditable and the flags
diffable.

What it reports
---------------
======================  ==============================================================
exposure                ``sum(position market value) / equity * 100``
cash weight             ``cash / equity * 100``
invested weight         ``sum(position cost basis) / equity * 100``
position weights        per position: ``market value / equity * 100``
portfolio P&L           realised, unrealised, total, and return on the invested basis
drawdown                current from peak, and the worst drawdown over the history
total risk              sum over the open positions of their loss at their stops
======================  ==============================================================

Ratios against equity
---------------------
Every percentage here divides by **equity**, and every one of them is ``None`` when equity is
not positive rather than zero. A weight of ``0`` means "this is nothing of the portfolio"; a
weight of ``None`` means "there is no portfolio to be a fraction of", and reporting the first
when the second is true would let a wiped-out portfolio look like a cautious one.

Risk flags
----------
A :class:`~app.schemas.risk.PortfolioRiskFlag` is raised when a configured limit is **currently**
breached, in a fixed evaluation order — concentration, sector, position count, exposure, cash,
total risk, drawdown. Fixed order, because a flag list that reorders itself between two reads of
the same portfolio cannot be compared.

Comparisons follow the Phase 12 convention exactly, which is **not** a single rule:

* **Concentration, exposure and cash limits are exclusive.** A portfolio sitting exactly on its
  limit is still *inside* it, and only a value strictly beyond it breaches. This is the Phase 12
  pre-trade rule: a position at exactly 30% is allowed, one share above it is not.
* **The drawdown guard is inclusive.** A portfolio sitting exactly on its drawdown limit is
  breaching it, and new buys are blocked. This is the Phase 12 drawdown rule.

The distinction is deliberate rather than accidental. A concentration limit answers "how much of
this portfolio may one name be", and holding exactly that much is the policy working as intended.
A drawdown limit answers "when does this portfolio stop being allowed to add risk", and the level
where the guard fires is the level where adding is no longer permitted. Both are reported with the
same flag vocabulary so a client never has to know which rule it is reading in order to act on it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

from app.services.backtest.models import quantize_money, quantize_percent
from app.services.portfolio.allocation import gain_pct, stop_price
from app.services.portfolio.calculator import (
    allocation_pct,
    drawdown_pct,
    invested_capital as sum_cost_basis,
    signed_drawdown_pct,
    sum_market_value,
    sum_unrealized_pnl,
    unrealized_pnl,
)
from app.services.portfolio.models import (
    ZERO,
    PortfolioRiskPolicy,
    PortfolioRiskState,
    PortfolioValuation,
    PositionExposure,
    PositionValuation,
    RiskFlagDetail,
)
from app.schemas.risk import PortfolioRiskFlag

_ONE_HUNDRED = Decimal("100")

#: Stable configuration keys, so a client can branch on the rule rather than on the prose. They
#: match the Phase 11 ``risk_engine`` rule names for the rules both engines share.
RULE_POSITION_WEIGHT = "max_position_weight_pct"
RULE_SECTOR_WEIGHT = "max_sector_weight_pct"
RULE_MAX_POSITIONS = "max_positions"
RULE_MAX_EXPOSURE = "max_exposure_pct"
RULE_MIN_CASH_RESERVE = "min_cash_reserve_pct"
RULE_MAX_TOTAL_RISK = "max_total_risk_pct"
RULE_MAX_DRAWDOWN = "max_drawdown_pct"


class PortfolioRiskStateEngine:
    """Builds the portfolio-level risk state from a valuation, a history and a policy."""

    def evaluate(
        self,
        valuation: PortfolioValuation,
        policy: PortfolioRiskPolicy,
        *,
        peak_equity: Optional[Decimal] = None,
        equity_history: Sequence[Decimal] = (),
        sectors: Optional[dict] = None,
        portfolio_id: Optional[int] = None,
        as_of: Optional[datetime] = None,
        initial_capital: Optional[Decimal] = None,
    ) -> PortfolioRiskState:
        """Full risk state for ``valuation``.

        ``equity_history`` is the recorded equity curve, oldest first. It is what turns
        ``max_drawdown`` from "the worst drawdown we happen to be in" into "the worst drawdown
        this portfolio has been in", so a caller that has a curve should always pass it.

        ``sectors`` maps a stock id to its sector. Positions whose sector is unknown are left out
        of the sector totals rather than lumped into an "unknown" bucket: an invented bucket would
        make the sector rule report a weight belonging to no real sector, and would let a
        concentrated portfolio pass the check.

        The drawdown flags use **peak equity**, which is taken from the history and the current
        equity and the initial capital — never from the current equity alone, because measuring a
        drawdown against today's equity would report every day as a fresh peak.
        """
        sectors = sectors or {}
        equity = valuation.equity
        basis = (
            quantize_money(Decimal(initial_capital))
            if initial_capital is not None
            else valuation.initial_capital
        )

        positions = tuple(
            sorted(
                (
                    self._position_exposure(position, equity, policy, sectors)
                    for position in valuation.positions
                ),
                key=lambda item: (
                    -(item.portfolio_weight_pct if item.portfolio_weight_pct is not None else ZERO),
                    item.stock_id,
                ),
            )
        )

        peak = max(
            [equity, *equity_history, quantize_money(basis), quantize_money(peak_equity or ZERO)],
        )
        current_drawdown = drawdown_pct(peak, equity)
        worst_dd, worst_dd_pct = worst_drawdown(equity_history, basis)

        total_risk = quantize_money(sum((item.risk_amount for item in positions), ZERO))
        total_pnl = quantize_money(valuation.realized_pnl + valuation.unrealized_pnl)
        unrealized = sum_unrealized_pnl(valuation.positions)
        reserve_required = policy.cash_reserve_required(equity)

        flags, details = self._flags(
            positions=positions,
            policy=policy,
            equity=equity,
            cash=valuation.cash,
            market_value=valuation.market_value,
            exposure_pct=valuation.exposure_pct,
            current_drawdown=current_drawdown,
            total_risk=total_risk,
            reserve_required=reserve_required,
        )

        return PortfolioRiskState(
            portfolio_id=valuation.portfolio_id if portfolio_id is None else portfolio_id,
            as_of=as_of or _utcnow(),
            equity=equity,
            cash=valuation.cash,
            market_value=valuation.market_value,
            exposure_pct=valuation.exposure_pct,
            cash_weight_pct=allocation_pct(valuation.cash, equity),
            invested_capital=sum_cost_basis(valuation.positions),
            invested_weight_pct=allocation_pct(sum_cost_basis(valuation.positions), equity),
            position_count=len(positions),
            largest_position_weight_pct=(
                positions[0].portfolio_weight_pct if positions else None
            ),
            largest_position_stock_id=positions[0].stock_id if positions else None,
            total_risk_amount=total_risk,
            total_risk_pct=allocation_pct(total_risk, equity),
            realized_pnl=valuation.realized_pnl,
            unrealized_pnl=unrealized,
            total_pnl=total_pnl,
            portfolio_return_pct=_return_pct(equity, basis),
            peak_equity=peak,
            current_drawdown_pct=current_drawdown,
            max_drawdown_pct=worst_dd_pct,
            max_drawdown=worst_dd,
            sector_weights=tuple(sorted(self._sector_weights(positions).items())),
            flags=tuple(flags),
            flag_details=tuple(details),
            positions=positions,
            policy=policy,
        )

    # --- Per-position ---

    def _position_exposure(
        self,
        position: PositionValuation,
        equity: Decimal,
        policy: PortfolioRiskPolicy,
        sectors: dict,
    ) -> PositionExposure:
        """Market figures, portfolio weight and the loss the position carries to its stop.

        The stop is derived from the **average entry price**, not from the first fill: after a
        second buy the average is what the position as a whole cost, so a stop derived from it is
        the level at which the whole position is down by that much.
        """
        entry = position.average_entry_price
        stop = stop_price(entry, policy.stop_loss_pct)
        per_share = Decimal(entry) - stop if stop is not None else ZERO
        risk = (
            quantize_money(per_share * Decimal(position.quantity))
            if per_share > ZERO
            else quantize_money(ZERO)
        )
        return PositionExposure(
            stock_id=position.stock_id,
            quantity=position.quantity,
            average_entry_price=entry,
            current_price=position.current_price,
            market_value=position.market_value,
            cost_basis=position.cost_basis,
            unrealized_pnl=unrealized_pnl(
                position.quantity, entry, position.current_price
            ),
            unrealized_pnl_pct=gain_pct(entry, position.current_price),
            portfolio_weight_pct=allocation_pct(position.market_value, equity),
            risk_amount=risk,
            stop_price=stop,
            sector=sectors.get(position.stock_id),
        )

    @staticmethod
    def _sector_weights(positions: Sequence[PositionExposure]) -> dict:
        """Combined weight per known sector. Positions with no sector are excluded entirely."""
        totals: dict = {}
        for item in positions:
            if item.sector is None or item.portfolio_weight_pct is None:
                continue
            totals[item.sector] = quantize_percent(
                totals.get(item.sector, ZERO) + item.portfolio_weight_pct
            )
        return totals

    # --- Flags ---

    def _flags(
        self,
        *,
        positions: Sequence[PositionExposure],
        policy: PortfolioRiskPolicy,
        equity: Decimal,
        cash: Decimal,
        market_value: Decimal,
        exposure_pct: Optional[Decimal],
        current_drawdown: Decimal,
        total_risk: Decimal,
        reserve_required: Decimal,
    ) -> tuple:
        """Every currently-breached limit, in the fixed evaluation order.

        Each entry is ``(flag, detail)``, so the API response can present them as a flag list and a
        parallel detail list without either being re-derived from the other.
        """
        flags: list = []
        details: list = []

        def raise_flag(flag, rule, reason, limit=None, observed=None, extra=None) -> None:
            flags.append(flag)
            details.append(
                RiskFlagDetail(
                    flag=flag.value,
                    rule=rule,
                    reason=reason,
                    limit=limit,
                    observed=observed,
                    details=extra or {},
                )
            )

        # 1. Single-position concentration.
        for item in positions:
            weight = item.portfolio_weight_pct
            if weight is not None and weight > policy.max_position_weight_pct:
                raise_flag(
                    PortfolioRiskFlag.POSITION_CONCENTRATION_BREACHED,
                    RULE_POSITION_WEIGHT,
                    (
                        f"Stock {item.stock_id} is {weight}% of portfolio equity, above the "
                        f"{policy.max_position_weight_pct}% single-position cap."
                    ),
                    limit=policy.max_position_weight_pct,
                    observed=weight,
                    extra={"stock_id": str(item.stock_id), "equity": str(equity)},
                )

        # 2. Sector concentration.
        if policy.max_sector_weight_pct is not None:
            for sector, weight in sorted(self._sector_weights(positions).items()):
                if weight > policy.max_sector_weight_pct:
                    raise_flag(
                        PortfolioRiskFlag.SECTOR_CONCENTRATION_BREACHED,
                        RULE_SECTOR_WEIGHT,
                        (
                            f"Sector '{sector}' is {weight}% of portfolio equity, above the "
                            f"{policy.max_sector_weight_pct}% cap."
                        ),
                        limit=policy.max_sector_weight_pct,
                        observed=weight,
                        extra={"sector": sector},
                    )

        # 3. Position count.
        if policy.max_positions is not None and len(positions) > policy.max_positions:
            raise_flag(
                PortfolioRiskFlag.POSITION_LIMIT_BREACHED,
                RULE_MAX_POSITIONS,
                (
                    f"The portfolio holds {len(positions)} open position(s), above the configured "
                    f"limit of {policy.max_positions}."
                ),
                limit=Decimal(policy.max_positions),
                observed=Decimal(len(positions)),
            )

        # 4. Total exposure.
        if policy.max_exposure_pct is not None and exposure_pct is not None:
            if exposure_pct > policy.max_exposure_pct:
                raise_flag(
                    PortfolioRiskFlag.EXPOSURE_LIMIT_BREACHED,
                    RULE_MAX_EXPOSURE,
                    (
                        f"{market_value} is invested, {exposure_pct}% of portfolio equity, above "
                        f"the {policy.max_exposure_pct}% limit."
                    ),
                    limit=policy.max_exposure_pct,
                    observed=exposure_pct,
                )

        # 5. Cash reserve.
        if policy.min_cash_reserve_pct > ZERO and cash < reserve_required:
            raise_flag(
                PortfolioRiskFlag.CASH_RESERVE_BREACHED,
                RULE_MIN_CASH_RESERVE,
                (
                    f"Cash is {cash} but {reserve_required} is required "
                    f"({policy.min_cash_reserve_pct}% of equity)."
                ),
                limit=policy.min_cash_reserve_pct,
                observed=cash,
                extra={"required_reserve": str(reserve_required)},
            )

        # 6. Total risk to the stops of every open position.
        risk_limit = policy.max_total_risk(equity)
        if risk_limit is not None and total_risk > risk_limit:
            raise_flag(
                PortfolioRiskFlag.TOTAL_RISK_LIMIT_BREACHED,
                RULE_MAX_TOTAL_RISK,
                (
                    f"The open positions carry {total_risk} of risk to their stops, above the "
                    f"{risk_limit} allowed ({policy.max_total_risk_pct}% of equity)."
                ),
                limit=policy.max_total_risk_pct,
                observed=total_risk,
                extra={"risk_amount": str(total_risk), "risk_limit": str(risk_limit)},
            )

        # 7. Drawdown guard. Inclusive: sitting exactly on the limit is breaching it.
        if policy.max_drawdown_pct is not None and current_drawdown >= policy.max_drawdown_pct:
            raise_flag(
                PortfolioRiskFlag.DRAWDOWN_LIMIT_BREACHED,
                RULE_MAX_DRAWDOWN,
                (
                    f"Equity is {current_drawdown}% below the peak equity, at or beyond the "
                    f"configured {policy.max_drawdown_pct}% limit, so new buys are blocked. "
                    "Sells remain allowed."
                ),
                limit=policy.max_drawdown_pct,
                observed=current_drawdown,
            )

        return flags, details


def worst_drawdown(
    equity_history: Sequence[Decimal], basis: Optional[Decimal] = None
) -> tuple:
    """Worst drawdown over a recorded equity curve, as ``(money, percent)``.

    The peak is tracked **running**: at each observation the peak is the highest equity seen up
    to and including that observation, never the highest over the whole curve. Using the global
    peak would measure every point against an equity the portfolio had not reached yet, which is
    exactly the mistake a drawdown curve exists to avoid.

    ``basis`` is prepended to the series when supplied, so a curve that starts mid-life still
    measures against the capital the portfolio began with. Percentages are ``0`` when the peak
    is not positive, because a drawdown from zero is not a number.
    """
    series = list(equity_history)
    if basis is not None:
        series.insert(0, quantize_money(Decimal(basis)))

    worst_money = quantize_money(ZERO)
    worst_pct = quantize_money(ZERO)
    peak: Optional[Decimal] = None
    for equity in series:
        value = quantize_money(Decimal(equity))
        peak = value if peak is None else max(peak, value)
        decline = quantize_money(peak - value)
        if decline > worst_money:
            worst_money = decline
        percentage = drawdown_pct(peak, value)
        if percentage > worst_pct:
            worst_pct = percentage
    return worst_money, worst_pct


def portfolio_return_pct(current_equity: Decimal, initial_equity: Decimal) -> Decimal:
    """``(current_equity - initial_equity) / initial_equity * 100``.

    Zero when the basis is not positive, because a return on nothing is not a number. Sign is
    preserved: a losing portfolio reports a negative return rather than a zero.
    """
    return _return_pct(current_equity, initial_equity)


def _return_pct(current_equity: Decimal, initial_equity: Decimal) -> Decimal:
    if Decimal(initial_equity) <= ZERO:
        return quantize_percent(ZERO)
    return quantize_percent(
        (Decimal(current_equity) - Decimal(initial_equity)) / Decimal(initial_equity) * _ONE_HUNDRED
    )


def signed_drawdown(peak_equity: Decimal, current_equity: Decimal) -> Decimal:
    """Drawdown from a peak, signed: ``0`` at a peak and negative below it."""
    return signed_drawdown_pct(peak_equity, current_equity)


def market_value_of(positions: Sequence[PositionValuation]) -> Decimal:
    """Total market value of a set of valued positions."""
    return sum_market_value(positions)


def _utcnow() -> datetime:
    """Naive UTC now, from the project's single timestamp helper.

    Imported lazily because the helper lives in the Phase 9 service module, which imports this
    package's siblings; a module-level import would make the pure calculation layer depend on the
    persistence layer for one function. Every caller in the application passes ``as_of``
    explicitly, so this fallback exists only for direct use of the pure engine.
    """
    from app.services.portfolio.service import utcnow

    return utcnow()


#: Module-level engine for callers that do not need a subclass.
portfolio_risk_state_engine = PortfolioRiskStateEngine()