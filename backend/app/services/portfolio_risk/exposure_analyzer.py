"""Phase 15 — portfolio exposure analysis.

Answers the first three questions the risk engine asks: *what does this portfolio hold*, *how is
that exposure split by strategy, sector and asset class*, and *where is it concentrated*.

Symbol aggregation
------------------
A symbol held by several strategies is **one** portfolio-level exposure. Phase 14 deliberately
hands the same instrument to more than one strategy, and reporting each strategy's copy as an
independent asset would understate concentration exactly where the book is most concentrated —
a 15% position held by three strategies would read as three 15% positions instead of one 45%
one. Lots are therefore summed per symbol, and the contributing strategies are listed on the
aggregated exposure.

Unclassified groups
-------------------
A lot submitted without a sector is grouped under ``UNCLASSIFIED`` and reported, but the sector
limit is not applied to it: the engine does not know which sector the lot belongs to, and
applying a sector cap to a bucket of unknown membership would be a claim it has no data for.
The same rule applies to a missing asset class. Strategy exposure has no such exemption — an
unnamed strategy is still a real bucket of capital — so it *is* measured against the limit.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.exceptions import InvalidPortfolioStateError
from app.schemas.portfolio_risk import RiskStatus
from app.services.backtest.models import quantize_money, quantize_percent
from app.services.portfolio_risk.config import EPSILON, ZERO, _HUNDRED, PortfolioRiskConfig
from app.services.portfolio_risk.evaluation import assess, status_for, utilization_pct
from app.services.portfolio_risk.models import (
    UNCLASSIFIED_ASSET_CLASS,
    UNCLASSIFIED_SECTOR,
    AssetExposure,
    ConcentrationMetric,
    ExposureSummary,
    GroupExposure,
    PortfolioExposure,
    RiskPosition,
)

#: Number of leading positions reported by name in the exposure summary.
_SUMMARY_TOP_COUNTS = (3, 5, 10)
#: Relative tolerance when reconciling equity against cash plus market value. A hundredth of a
#: percent is far tighter than any rounding the price or money quantisation can introduce, and far
#: looser than a genuine mistake in the caller's figures.
_EQUITY_TOLERANCE = Decimal("0.0001")


def validate_equity(
    positions: Sequence[RiskPosition],
    cash: Decimal,
    portfolio_value: Decimal,
) -> None:
    """Require portfolio equity to equal cash plus the market value of the holdings.

    Every weight in the report is a share of ``portfolio_value``, so an equity that disagrees with
    the positions makes every exposure, concentration and limit figure wrong by a constant factor
    while still looking precise. The check is a reconciliation rather than a rejection of small
    differences: quantisation to cents cannot be allowed to fail a correct portfolio, but a real
    discrepancy must not be silently absorbed.
    """
    if portfolio_value <= ZERO:
        raise InvalidPortfolioStateError(
            f"Portfolio equity must be positive: every exposure, weight and limit in this report "
            f"is a share of it, and the value supplied was {portfolio_value}."
        )

    market_value = quantize_money(sum((position.market_value for position in positions), ZERO))
    expected = quantize_money(market_value + cash)
    difference = abs(expected - portfolio_value)
    if difference > _EQUITY_TOLERANCE * portfolio_value:
        raise InvalidPortfolioStateError(
            f"Portfolio equity does not reconcile: cash {cash} plus market value "
            f"{market_value} is {expected}, but portfolio_value is {portfolio_value} "
            f"(difference {difference}). Every weight in this report is a share of "
            "portfolio_value, so the two must agree.",
            details={
                "cash": str(cash),
                "market_value": str(market_value),
                "expected_equity": str(expected),
                "portfolio_value": str(portfolio_value),
                "difference": str(difference),
            },
        )


def validate_positions(
    positions: Sequence[RiskPosition],
    portfolio_value: Decimal,
    cash: Decimal,
) -> None:
    """Reject a position set the exposure maths cannot honestly describe.

    Checked here, once, rather than silently absorbed downstream: a negative quantity, a
    non-positive price, a repeated ``(symbol, strategy)`` lot, or a symbol carrying two
    different sectors would all produce exposures that look precise and are not. Equities and
    every weight derived from them are ratios, so nonsense in makes nonsense out.
    """
    if portfolio_value <= ZERO:
        raise InvalidPortfolioStateError(
            "Portfolio equity must be positive: every exposure, weight and limit in this "
            f"report is a share of it, and the value supplied was {portfolio_value}."
        )
    if cash < ZERO:
        raise InvalidPortfolioStateError(
            f"Cash cannot be negative; the value supplied was {cash}."
        )

    seen_lots: Dict[Tuple[str, str], int] = {}
    sectors: Dict[str, set] = {}
    asset_classes: Dict[str, set] = {}

    for index, position in enumerate(positions):
        if not position.symbol or not position.symbol.strip():
            raise InvalidPortfolioStateError(
                f"Position {index} has no symbol; a holding without an instrument cannot be "
                "exposed, valued or stressed."
            )
        if position.quantity < 0:
            raise InvalidPortfolioStateError(
                f"Position {position.symbol} has negative quantity {position.quantity}. This "
                "engine is long-only; a short is not a portfolio exposure it can weight."
            )
        if Decimal(str(position.price)) <= ZERO:
            raise InvalidPortfolioStateError(
                f"Position {position.symbol} has price {position.price}; a position priced at "
                "or below zero contributes no value while still consuming a risk slot."
            )

        key = (position.symbol, position.strategy_name)
        if key in seen_lots:
            raise InvalidPortfolioStateError(
                f"Duplicate position: '{position.symbol}' appears twice for strategy "
                f"'{position.strategy_name}'. A symbol may appear at most once per strategy."
            )
        seen_lots[key] = index

        if position.sector:
            sectors.setdefault(position.symbol, set()).add(position.sector)
        if position.asset_class:
            asset_classes.setdefault(position.symbol, set()).add(position.asset_class)

    for symbol, values in sorted(sectors.items()):
        if len(values) > 1:
            raise InvalidPortfolioStateError(
                f"Symbol '{symbol}' is submitted with conflicting sectors "
                f"({', '.join(sorted(values))}); the portfolio-level sector exposure of that "
                "holding is undefined until one classification is chosen."
            )
    for symbol, values in sorted(asset_classes.items()):
        if len(values) > 1:
            raise InvalidPortfolioStateError(
                f"Symbol '{symbol}' is submitted with conflicting asset classes "
                f"({', '.join(sorted(values))}); the portfolio-level asset-class exposure of "
                "that holding is undefined until one classification is chosen."
            )

    # Last, so a defect in a position is reported as itself rather than as the equity mismatch it
    # also causes.
    validate_equity(positions, cash, portfolio_value)


class ExposureAnalyzer:
    """Deterministic exposure and concentration measurement.

    Pure: it reads one :class:`~app.services.portfolio_risk.models.RiskSnapshot` and one policy
    and returns the exposure breakdown. No clock, no database, no iteration-order dependence —
    every list it returns is sorted, so two runs over the same positions produce equal output.
    """

    def __init__(self, config: Optional[PortfolioRiskConfig] = None):
        self.config = config or PortfolioRiskConfig().validate()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, snapshot) -> PortfolioExposure:
        """Build the full exposure breakdown for ``snapshot``."""
        positions = snapshot.positions
        validate_positions(positions, snapshot.portfolio_value, snapshot.cash)

        assets = self._asset_exposures(positions, snapshot.portfolio_value)
        strategies = self._group_exposure(
            positions,
            key=lambda p: p.strategy_name,
            limit=self.config.max_strategy_weight,
            unlabelled_group=None,
            portfolio_value=snapshot.portfolio_value,
        )
        sectors = self._group_exposure(
            positions,
            key=lambda p: p.sector_name,
            limit=self.config.max_sector_weight,
            unlabelled_group=UNCLASSIFIED_SECTOR,
            portfolio_value=snapshot.portfolio_value,
        )
        asset_classes = self._group_exposure(
            positions,
            key=lambda p: p.asset_class_name,
            limit=self.config.max_asset_class_weight,
            unlabelled_group=UNCLASSIFIED_ASSET_CLASS,
            portfolio_value=snapshot.portfolio_value,
        )
        summary = self._summary(assets, positions, snapshot.portfolio_value, snapshot.cash, sectors)

        return PortfolioExposure(
            assets=tuple(assets),
            strategies=tuple(strategies),
            sectors=tuple(sectors),
            asset_classes=tuple(asset_classes),
            summary=summary,
        )

    def concentration_metrics(self, exposure: PortfolioExposure) -> Tuple[ConcentrationMetric, ...]:
        """Every concentration rule, measured — the input the controller turns into violations."""
        metrics: List[ConcentrationMetric] = []
        top_n = exposure.summary.top_n_count
        top_symbols = tuple(
            asset.symbol for asset in self._by_weight(exposure.assets)[:top_n]
        )
        metrics.append(
            ConcentrationMetric(
                metric=f"top_{top_n}_concentration",
                subject=None,
                value_pct=exposure.summary.top_n_concentration_pct,
                limit_pct=self.config.max_top_n_concentration,
                utilization_pct=utilization_pct(
                    exposure.summary.top_n_concentration_pct, self.config.max_top_n_concentration
                ),
                status=status_for(
                    exposure.summary.top_n_concentration_pct,
                    self.config.max_top_n_concentration,
                    self.config.warning_utilization_pct,
                ),
                details={"symbols": top_symbols, "position_count": len(top_symbols)},
            )
        )
        for asset in self._by_weight(exposure.assets):
            metrics.append(
                ConcentrationMetric(
                    metric="single_asset_weight",
                    subject=asset.symbol,
                    value_pct=asset.weight_pct,
                    limit_pct=self.config.max_single_asset_weight,
                    utilization_pct=asset.utilization_pct,
                    status=asset.status,
                    details={
                        "market_value": str(asset.market_value),
                        "strategies": list(asset.strategies),
                        "sector": asset.sector,
                    },
                )
            )
        for group, label in (
            (exposure.sectors, "sector"),
            (exposure.strategies, "strategy"),
            (exposure.asset_classes, "asset_class"),
        ):
            for item in group:
                metrics.append(
                    ConcentrationMetric(
                        metric=f"{label}_weight",
                        subject=item.name,
                        value_pct=item.weight_pct,
                        limit_pct=item.limit_pct,
                        utilization_pct=item.utilization_pct,
                        status=item.status,
                        details={
                            "symbols": list(item.symbols),
                            "position_count": item.position_count,
                            "limit_applied": item.limit_applied,
                        },
                    )
                )
        return tuple(metrics)

    def position_count_status(self, exposure: PortfolioExposure) -> Optional[ConcentrationMetric]:
        """Diversification count check, or ``None`` when no count rule is configured."""
        count = exposure.summary.total_positions
        if self.config.minimum_position_count is None and self.config.maximum_position_count is None:
            return None
        if self.config.maximum_position_count is not None and count > self.config.maximum_position_count:
            status, utilization = assess(
                Decimal(str(count)),
                Decimal(str(self.config.maximum_position_count)),
                self.config.warning_utilization_pct,
            )
            return ConcentrationMetric(
                metric="maximum_position_count",
                subject=None,
                value_pct=quantize_percent(Decimal(str(count))),
                limit_pct=Decimal(str(self.config.maximum_position_count)),
                utilization_pct=utilization,
                status=status,
                details={"position_count": count},
            )
        if self.config.minimum_position_count is not None and count < self.config.minimum_position_count:
            status, utilization = assess(
                Decimal(str(count)),
                Decimal(str(self.config.minimum_position_count)),
                self.config.warning_utilization_pct,
            )
            return ConcentrationMetric(
                metric="minimum_position_count",
                subject=None,
                value_pct=quantize_percent(Decimal(str(count))),
                limit_pct=Decimal(str(self.config.minimum_position_count)),
                utilization_pct=utilization,
                status=status,
                details={"position_count": count},
            )
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _by_weight(assets: Sequence[AssetExposure]) -> Tuple[AssetExposure, ...]:
        """Assets heaviest first, ties broken by symbol so the order never varies."""
        return tuple(sorted(assets, key=lambda a: (-a.weight_pct, a.symbol)))

    def _asset_exposures(
        self,
        positions: Sequence[RiskPosition],
        portfolio_value: Decimal,
    ) -> List[AssetExposure]:
        """Aggregate lots per symbol and measure each against the single-position limit."""
        grouped: Dict[str, List[RiskPosition]] = {}
        for position in positions:
            grouped.setdefault(position.symbol, []).append(position)

        exposures: List[AssetExposure] = []
        for symbol in sorted(grouped):
            lots = grouped[symbol]
            quantity = sum(lot.quantity for lot in lots)
            market_value = quantize_money(sum((lot.market_value for lot in lots), ZERO))
            weight = quantize_percent(market_value / portfolio_value * _HUNDRED)
            limit = self.config.max_single_asset_weight
            status, utilization = assess(weight, limit, self.config.warning_utilization_pct)

            beta = _first_decimal(lots, lambda lot: lot.beta)
            adtv = _first_decimal(lots, lambda lot: lot.avg_daily_traded_value)
            participation = None
            if adtv is not None and adtv > ZERO:
                participation = quantize_percent(market_value / adtv)

            exposures.append(
                AssetExposure(
                    symbol=symbol,
                    quantity=quantity,
                    market_value=market_value,
                    weight_pct=weight,
                    limit_pct=limit,
                    utilization_pct=utilization,
                    status=status,
                    strategies=tuple(sorted({lot.strategy_name for lot in lots})),
                    sector=_first_str(lots, lambda lot: lot.sector),
                    industry=_first_str(lots, lambda lot: lot.industry),
                    asset_class=_first_str(lots, lambda lot: lot.asset_class),
                    beta=beta,
                    avg_daily_traded_value=adtv,
                    participation_ratio=participation,
                    liquidity_score=_first_decimal(lots, lambda lot: lot.liquidity_score),
                )
            )
        return exposures

    def _group_exposure(
        self,
        positions: Sequence[RiskPosition],
        key,
        limit: Optional[Decimal],
        unlabelled_group: Optional[str],
        portfolio_value: Decimal,
    ) -> List[GroupExposure]:
        """Aggregate lots into named groups and measure each against its limit.

        ``unlabelled_group`` names the bucket the caller never classified — ``UNCLASSIFIED`` for
        sectors. That group is reported in full but never measured against the limit: the engine
        does not know its membership, so a cap applied to it would be a claim rather than a
        measurement.
        """
        grouped: Dict[str, List[RiskPosition]] = {}
        for position in positions:
            grouped.setdefault(key(position), []).append(position)

        groups: List[GroupExposure] = []
        for name in sorted(grouped):
            lots = grouped[name]
            market_value = quantize_money(sum((lot.market_value for lot in lots), ZERO))
            weight = quantize_percent(market_value / portfolio_value * _HUNDRED)
            limit_applied = not (unlabelled_group is not None and name == unlabelled_group)
            effective_limit = limit if limit_applied else None
            status, utilization = assess(weight, effective_limit, self.config.warning_utilization_pct)
            groups.append(
                GroupExposure(
                    name=name,
                    weight_pct=weight,
                    limit_pct=effective_limit,
                    utilization_pct=utilization,
                    status=status,
                    market_value=market_value,
                    position_count=len(lots),
                    symbols=tuple(sorted({lot.symbol for lot in lots})),
                    limit_applied=limit_applied,
                )
            )
        return groups

    def _summary(
        self,
        assets: Sequence[AssetExposure],
        positions: Sequence[RiskPosition],
        portfolio_value: Decimal,
        cash: Decimal,
        sectors: Sequence[GroupExposure],
    ) -> ExposureSummary:
        """Headline figures: cash share, largest position and the top-N concentration ladder."""
        ordered = self._by_weight(assets)
        total_market_value = quantize_money(sum((asset.market_value for asset in assets), ZERO))
        top_n_count = self.config.top_n_count

        tops: Dict[int, Decimal] = {}
        for count in _SUMMARY_TOP_COUNTS:
            tops[count] = quantize_percent(
                sum((asset.weight_pct for asset in ordered[:count]), ZERO)
            )
        top_n = quantize_percent(sum((asset.weight_pct for asset in ordered[:top_n_count]), ZERO))

        unclassified = next(
            (group.weight_pct for group in sectors if group.name == UNCLASSIFIED_SECTOR), None
        )

        largest = ordered[0] if ordered else None
        return ExposureSummary(
            total_positions=len(assets),
            total_lots=len(positions),
            total_market_value=total_market_value,
            cash=quantize_money(cash),
            cash_pct=quantize_percent(cash / portfolio_value * _HUNDRED),
            invested_pct=quantize_percent(total_market_value / portfolio_value * _HUNDRED),
            largest_position_pct=largest.weight_pct if largest else None,
            largest_position_symbol=largest.symbol if largest else None,
            top_3_concentration_pct=tops[3],
            top_5_concentration_pct=tops[5],
            top_10_concentration_pct=tops[10],
            top_n_concentration_pct=top_n,
            top_n_count=top_n_count,
            unclassified_sector_pct=unclassified or ZERO,
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _first_decimal(lots: Sequence[RiskPosition], getter):
    """The first non-null value across a symbol's lots, in submitted order.

    Lots are sorted by strategy first so the answer does not depend on the order the caller
    listed its positions in.
    """
    for lot in sorted(lots, key=lambda item: item.strategy_name):
        value = getter(lot)
        if value is not None:
            return Decimal(str(value))
    return None


def _first_str(lots: Sequence[RiskPosition], getter) -> Optional[str]:
    """The first non-empty string across a symbol's lots, in deterministic order."""
    for lot in sorted(lots, key=lambda item: item.strategy_name):
        value = getter(lot)
        if value:
            return value
    return None


__all__ = [
    "ExposureAnalyzer",
    "validate_equity",
    "validate_positions",
    "UNCLASSIFIED_SECTOR",
    "UNCLASSIFIED_ASSET_CLASS",
    "EPSILON",
]