"""Centralised defaults for the Phase 9 paper-trading engine.

There are no magic numbers in the engine: every fee and risk figure comes either from
application settings (via these factories) or from the per-portfolio configuration persisted
with the portfolio. Changing a default here changes it for every new portfolio, and an
existing portfolio keeps the configuration it was created with.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.core.config import settings
from app.schemas.portfolio import PositionSizingMethod
from app.services.portfolio.models import (
    AllocationConfig,
    PortfolioRiskPolicy,
    RiskLimits,
    TradingCostConfig,
)


def default_trading_cost_config() -> TradingCostConfig:
    """Simulated cost model from application settings."""
    return TradingCostConfig(
        commission_rate=Decimal(str(settings.PAPER_TRADING_COMMISSION_RATE)),
        min_fee=Decimal(str(settings.PAPER_TRADING_MIN_FEE)),
        slippage_rate=Decimal(str(settings.PAPER_TRADING_SLIPPAGE_RATE)),
    )


def default_risk_limits() -> RiskLimits:
    """Risk limits from application settings. Rules left unset stay disabled."""
    return RiskLimits(
        max_position_quantity=settings.PAPER_TRADING_MAX_POSITION_QUANTITY,
        max_position_value=_optional(settings.PAPER_TRADING_MAX_POSITION_VALUE),
        max_position_allocation_pct=_optional(settings.PAPER_TRADING_MAX_POSITION_ALLOCATION_PCT),
        max_drawdown_pct=_optional(settings.PAPER_TRADING_MAX_DRAWDOWN_PCT),
        max_daily_loss_pct=_optional(settings.PAPER_TRADING_MAX_DAILY_LOSS_PCT),
        min_trade_value=_optional(settings.PAPER_TRADING_MIN_TRADE_VALUE),
        max_exposure_pct=_optional(settings.PAPER_TRADING_MAX_EXPOSURE_PCT),
        max_trade_allocation_pct=_optional(settings.PAPER_TRADING_MAX_TRADE_ALLOCATION_PCT),
        min_cash_reserve_pct=_optional(settings.PAPER_TRADING_MIN_CASH_RESERVE_PCT),
        max_positions=settings.PAPER_TRADING_MAX_POSITIONS,
    )


def _optional(value) -> Optional[Decimal]:
    """``None`` for an unset setting, ``Decimal`` otherwise."""
    return None if value is None else Decimal(str(value))


def default_position_size_pct() -> Decimal:
    """Entry budget per strategy order, as a percentage of available cash."""
    return Decimal(str(settings.PAPER_TRADING_POSITION_SIZE_PCT))


def default_allocation_config() -> AllocationConfig:
    """Portfolio construction policy from application settings (Phase 11).

    Every allocation constant lives here or in the per-portfolio
    ``allocation_config_json``, never in the engine: changing a default here changes it for every
    new portfolio, and an existing portfolio keeps the configuration it was created with. That is
    what lets a future strategy supply a different policy without a code change.
    """
    return AllocationConfig(
        sizing_method=PositionSizingMethod(settings.PORTFOLIO_ALLOCATION_SIZING_METHOD),
        max_position_weight_pct=_decimal(settings.PORTFOLIO_MAX_POSITION_WEIGHT_PCT),
        max_sector_weight_pct=_optional(settings.PORTFOLIO_MAX_SECTOR_WEIGHT_PCT),
        max_positions=settings.PORTFOLIO_MAX_POSITIONS,
        max_exposure_pct=_optional(settings.PORTFOLIO_MAX_EXPOSURE_PCT),
        max_position_loss_pct=_optional(settings.PORTFOLIO_MAX_POSITION_LOSS_PCT),
        stop_loss_pct=_optional(settings.PORTFOLIO_STOP_LOSS_PCT),
        take_profit_pct=_optional(settings.PORTFOLIO_TAKE_PROFIT_PCT),
        rebalance_threshold_pct=_decimal(settings.PORTFOLIO_REBALANCE_THRESHOLD_PCT),
        min_position_value=_optional(settings.PORTFOLIO_MIN_POSITION_VALUE),
        risk_per_trade_pct=_decimal(settings.PORTFOLIO_RISK_PER_TRADE_PCT),
        cash_reserve_pct=_decimal(settings.PORTFOLIO_CASH_RESERVE_PCT),
        target_position_count=None,
    )


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def default_portfolio_risk_policy() -> PortfolioRiskPolicy:
    """The Phase 13 portfolio & risk policy, assembled from application settings.

    One object carrying every limit the Phase 13 engine reads, built from the same settings the
    Phase 11 and Phase 12 defaults are built from — plus the three limits Phase 13 added. There
    is still exactly one place a default lives, so changing a setting here changes it for every
    new policy and nothing else.
    """
    policy = PortfolioRiskPolicy(
        max_position_weight_pct=_decimal(settings.PORTFOLIO_MAX_POSITION_WEIGHT_PCT),
        max_exposure_pct=_optional(settings.PORTFOLIO_MAX_EXPOSURE_PCT),
        max_risk_per_trade_pct=_decimal(settings.PORTFOLIO_RISK_PER_TRADE_PCT),
        max_drawdown_pct=_optional(settings.PORTFOLIO_MAX_DRAWDOWN_PCT),
        max_positions=settings.PORTFOLIO_MAX_POSITIONS,
        min_cash_reserve_pct=_decimal(settings.PORTFOLIO_CASH_RESERVE_PCT),
        max_sector_weight_pct=_optional(settings.PORTFOLIO_MAX_SECTOR_WEIGHT_PCT),
        max_total_risk_pct=_optional(settings.PORTFOLIO_MAX_TOTAL_RISK_PCT),
        stop_loss_pct=_optional(settings.PORTFOLIO_STOP_LOSS_PCT),
        min_trade_value=_optional(settings.PORTFOLIO_MIN_TRADE_VALUE),
        commission_rate=_decimal(settings.PAPER_TRADING_COMMISSION_RATE),
        min_fee=_decimal(settings.PAPER_TRADING_MIN_FEE),
        slippage_rate=_decimal(settings.PAPER_TRADING_SLIPPAGE_RATE),
    )
    policy.validate()
    return policy