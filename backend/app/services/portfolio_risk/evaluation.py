"""Phase 15 — shared limit evaluation.

One place decides what "within the limit" means. Every analyzer, the stress tester and the
controller ask these helpers, so a utilization is computed the same way and a status is assigned
from the same rule everywhere in the package.

Two rules are deliberate and load-bearing:

* **A missing value is never zero.** When the metric could not be computed the status is
  ``DATA_INSUFFICIENT`` and the value stays ``None``. Reporting ``0`` would report *no* risk for
  a metric nobody measured.
* **A missing limit is not a pass.** ``NOT_APPLICABLE`` means the deployment did not configure
  that rule. Collapsing it into ``PASS`` would claim a check ran when none did.

Status is decided against the configured warning utilization rather than a hard-coded 80%, so a
deployment can tighten or loosen how loudly the engine speaks.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Tuple

from app.schemas.portfolio_risk import RiskStatus
from app.services.backtest.models import quantize_percent

#: Absolute zero, reused for sign comparisons.
ZERO = Decimal("0")
#: One hundred — the scale of every percentage in this package.
_HUNDRED = Decimal("100")
#: Numerical tolerance for floating-point comparisons.
EPSILON = Decimal("1e-8")


def utilization_pct(value: Optional[Decimal], limit: Optional[Decimal]) -> Optional[Decimal]:
    """``value / limit`` as a percent, or ``None`` when it cannot be computed.

    ``None`` for a missing value or a missing limit: an unmeasured metric has no utilization,
    and a rule that is switched off has no denominator.
    """
    if value is None or limit is None or limit <= ZERO:
        return None
    return quantize_percent(Decimal(str(value)) / Decimal(str(limit)) * _HUNDRED)


def status_for(
    value: Optional[Decimal],
    limit: Optional[Decimal],
    warning_utilization_pct: Decimal = Decimal("80"),
) -> RiskStatus:
    """Classify one measurement against its limit.

    ``PASS`` within the limit, ``WARNING`` at or above ``warning_utilization_pct`` of it,
    ``BREACH`` above the limit, ``DATA_INSUFFICIENT`` when there is no value,
    ``NOT_APPLICABLE`` when there is no limit.
    """
    if value is None:
        return RiskStatus.DATA_INSUFFICIENT
    if limit is None:
        return RiskStatus.NOT_APPLICABLE
    if value > limit:
        return RiskStatus.BREACH
    utilization = utilization_pct(value, limit)
    if utilization is not None and utilization >= warning_utilization_pct:
        return RiskStatus.WARNING
    return RiskStatus.PASS


def assess(
    value: Optional[Decimal],
    limit: Optional[Decimal],
    warning_utilization_pct: Decimal = Decimal("80"),
) -> Tuple[RiskStatus, Optional[Decimal]]:
    """``(status, utilization_pct)`` for one measurement — the pair most callers need."""
    return status_for(value, limit, warning_utilization_pct), utilization_pct(value, limit)


def exceeds(value: Optional[Decimal], limit: Optional[Decimal]) -> bool:
    """True only when both figures exist and the value is strictly above the limit.

    Used by the limit checks so that "at the limit" is not a breach: the comparison carries the
    project tolerance, because a value that only appears over the limit because of a
    quantisation step is not a real breach.
    """
    if value is None or limit is None:
        return False
    return Decimal(str(value)) > Decimal(str(limit)) + EPSILON


def excess_pct(value: Optional[Decimal], limit: Optional[Decimal]) -> Optional[Decimal]:
    """How far past the limit an observation sits, in percentage points (``None`` when compliant)."""
    if not exceeds(value, limit):
        return None
    return quantize_percent(Decimal(str(value)) - Decimal(str(limit)))


def fmt(value: Optional[Decimal], places: int = 2) -> str:
    """Format a percentage for an explanation sentence, e.g. ``42.30``."""
    if value is None:
        return "n/a"
    exponent = Decimal(1).scaleb(-places)
    return str(Decimal(str(value)).quantize(exponent))


def pct(value: Optional[Decimal], places: int = 2) -> str:
    """Format a percentage for an explanation sentence, e.g. ``42.30%``."""
    return f"{fmt(value, places)}%"