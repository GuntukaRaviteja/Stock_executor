"""Phase 15 — portfolio liquidity analysis.

Turns traded-value data into one number a risk controller can act on: how many days would it
take to get out of this book?

Participation ratio
-------------------
For each position::

    participation = position market value / average daily traded value

That is the share of a normal day's volume the position represents. A ratio near zero is a
position that can be sold without moving the market; a ratio of one would mean selling the whole
holding takes a full day's volume — and doing it into a falling market takes more.

Score
-----
``overall_score`` is the value-weighted mean of the per-position scores, where a score of 100
means a participation ratio of zero and the score falls to 0 at the configured participation
limit (or at 1.0, whichever is smaller). It is a *scale for comparison*, not an exchange
liquidity metric: it says how hard this book is to unwind relative to normal volume.

Nothing is invented
-------------------
With no average daily traded value for any position there is no basis for a participation ratio
at all, and the analysis reports ``LIQUIDITY_DATA_UNAVAILABLE`` with a ``None`` score rather than
assuming the book is perfectly liquid. Partial data is partial coverage, and the coverage is
reported next to the score.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional, Sequence

from app.schemas.portfolio_risk import RiskStatus
from app.services.backtest.models import quantize_percent
from app.services.portfolio_risk.config import ZERO, PortfolioRiskConfig
from app.services.portfolio_risk.evaluation import pct, status_for, utilization_pct
from app.services.portfolio_risk.models import (
    LiquidityAnalysis,
    PortfolioExposure,
)

#: One hundred — the scale of every percentage in this package.
_HUNDRED = Decimal("100")
#: Participation ratio at which a position scores zero when no limit is configured.
_FALLBACK_PARTICIPATION = Decimal("1")
#: The status a liquidity analysis reports when no traded-value data exists at all.
UNAVAILABLE_STATUS = RiskStatus.DATA_INSUFFICIENT


class LiquidityAnalyzer:
    """Deterministic liquidity assessment from traded-value data."""

    def __init__(self, config: Optional[PortfolioRiskConfig] = None):
        self.config = config or PortfolioRiskConfig().validate()

    def analyze(self, exposure: PortfolioExposure) -> LiquidityAnalysis:
        """Assess how readily the book could be liquidated."""
        assets = exposure.assets
        with_data = [asset for asset in assets if asset.avg_daily_traded_value is not None]
        participation_limit = self.config.max_participation_ratio

        if not with_data:
            return LiquidityAnalysis(
                status=UNAVAILABLE_STATUS,
                positions_with_data=0,
                positions_analysed=len(assets),
                overall_score=None,
                limit_pct=self.config.minimum_liquidity_score,
                utilization_pct=None,
                max_participation_ratio=None,
                max_participation_limit=participation_limit,
                worst_participation_symbol=None,
                estimated_liquidation_days=None,
                flagged_positions=(),
                explanation=(
                    "LIQUIDITY_DATA_UNAVAILABLE: no average daily traded value was supplied for "
                    f"any of the {len(assets)} positions, so no participation ratio, liquidity "
                    "score or liquidation estimate can be produced. An unavailable liquidity "
                    "figure is not a good one — it means the book may be impossible to exit "
                    "quickly and nothing here measures it."
                ),
            )

        scores: List[Decimal] = []
        flagged: List[dict] = []
        worst_ratio: Optional[Decimal] = None
        worst_symbol: Optional[str] = None

        for asset in with_data:
            ratio = asset.participation_ratio or ZERO
            denominator = participation_limit or _FALLBACK_PARTICIPATION
            score = quantize_percent(
                max(ZERO, (Decimal("1") - ratio / denominator) * _HUNDRED)
            )
            scores.append(score)
            if worst_ratio is None or ratio > worst_ratio:
                worst_ratio = ratio
                worst_symbol = asset.symbol
            if participation_limit is not None and ratio > participation_limit:
                flagged.append(
                    {
                        "symbol": asset.symbol,
                        "market_value": str(asset.market_value),
                        "avg_daily_traded_value": str(asset.avg_daily_traded_value),
                        "participation_ratio": str(ratio),
                        "participation_limit": str(participation_limit),
                        "days_to_liquidate": str(
                            quantize_percent(ratio / participation_limit * _HUNDRED) / _HUNDRED
                        ),
                        "status": RiskStatus.BREACH.value,
                    }
                )

        # Coverage is measured against *invested* weight: cash is not a position and carries no
        # traded-value requirement, so including it would understate coverage for every portfolio
        # that holds cash.
        invested = sum((asset.weight_pct for asset in assets), ZERO)
        covered = sum((asset.weight_pct for asset in with_data), ZERO)
        coverage_pct = (
            quantize_percent(covered / invested * _HUNDRED) if invested > ZERO else Decimal("100")
        )
        if covered > ZERO:
            overall = quantize_percent(
                sum(
                    (
                        asset.weight_pct / _HUNDRED * (score or ZERO)
                        for asset, score in zip(with_data, scores)
                    ),
                    ZERO,
                )
            )
        else:
            overall = None

        # Status reports how far the *check* can be trusted, not merely whether it passed:
        # a breach is reported as a breach, partial coverage as insufficient data, and a fully
        # covered book with no configured limit as not applicable.
        if flagged:
            status = RiskStatus.BREACH
        elif coverage_pct < _HUNDRED - Decimal("0.0001"):
            status = RiskStatus.DATA_INSUFFICIENT
        elif self.config.minimum_liquidity_score is not None:
            status = status_for(
                overall, self.config.minimum_liquidity_score, self.config.warning_utilization_pct
            )
        else:
            status = RiskStatus.NOT_APPLICABLE

        explanation_parts = [
            f"{len(with_data)} of {len(assets)} positions carry average daily traded value, "
            f"covering {pct(coverage_pct)} of invested weight."
        ]
        if overall is not None:
            explanation_parts.append(f"The value-weighted liquidity score is {overall}.")
        if flagged:
            explanation_parts.append(
                f"{len(flagged)} position(s) exceed the configured participation limit of "
                f"{pct(participation_limit)} of daily volume: "
                + ", ".join(sorted(item["symbol"] for item in flagged))
                + "."
            )
        elif participation_limit is not None:
            explanation_parts.append(
                f"No position exceeds the {pct(participation_limit)} participation limit."
            )

        return LiquidityAnalysis(
            status=status,
            positions_with_data=len(with_data),
            positions_analysed=len(assets),
            overall_score=overall,
            limit_pct=self.config.minimum_liquidity_score,
            utilization_pct=utilization_pct(overall, self.config.minimum_liquidity_score),
            max_participation_ratio=worst_ratio,
            max_participation_limit=participation_limit,
            worst_participation_symbol=worst_symbol,
            estimated_liquidation_days=self._liquidation_days(exposure, participation_limit),
            flagged_positions=tuple(sorted(flagged, key=lambda item: item["symbol"])),
            explanation=" ".join(explanation_parts),
        )

    def _liquidation_days(
        self,
        exposure: PortfolioExposure,
        participation_limit: Optional[Decimal],
    ) -> Optional[Decimal]:
        """Trading days needed to unwind the book at the participation limit.

        Each position's days are rounded up to a whole day — a partial day is not a tradable
        unit — and the book takes as long as its slowest position, since they cannot all be sold
        at once.
        """
        if participation_limit is None or participation_limit <= ZERO:
            return None
        days: List[Decimal] = []
        for asset in exposure.assets:
            if asset.avg_daily_traded_value is None or asset.avg_daily_traded_value <= ZERO:
                return None
            ratio = asset.participation_ratio or ZERO
            required = ratio / participation_limit
            days.append(
                Decimal("1")
                if required <= Decimal("1")
                else required.to_integral_value(rounding="ROUND_CEILING")
            )
        return max(days) if days else None


__all__ = ["LiquidityAnalyzer", "UNAVAILABLE_STATUS"]