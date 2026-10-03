"""Portfolio valuation — a pure reading of a portfolio's state at one moment.

Answers, deterministically and without mutating anything:

======================  ==========================================================
cash                    ``current_cash`` as recorded on the portfolio
position market value   ``quantity * current_price`` per position
total market value      sum of every open position's market value
equity                  ``cash + total_market_value``
invested capital        sum of every open position's **cost basis**
unrealised P&L          ``market_value - cost_basis``
realised P&L            accumulated by sells, net of the fees of the shares sold
invested basis           ``initial_capital`` plus every deposit and withdrawal
total P&L               ``equity - invested basis`` (authoritative, net of all fees)
return %                ``total P&L / invested basis * 100``
position weight         ``position market value / equity * 100``
exposure                ``total market value / equity * 100``
======================  ==========================================================

Accounting conventions
----------------------
* **Invested capital** is what the open positions *cost* (``sum(cost_basis)``), not what they
  are worth. The difference between it and the market value is exactly the unrealised P&L of
  those positions, so the two figures always reconcile.
* **Capital in is not performance.** A deposit raises equity without raising the return, so the
  P&L basis is the opening capital plus the net of every deposit and withdrawal, read from the
  transaction ledger. Without that adjustment a deposit would be reported as a profit and the
  ``return_pct`` would rise without a single share having been traded. For a portfolio that has
  only ever traded, the ledger holds no such entries and the basis is exactly the opening
  capital — the same figures as before the ledger existed.
* ``total_pnl`` is always derived as ``equity - invested basis`` and is never accumulated
  independently, so it can never drift from cash plus market value.
* An unpriced position (no mark yet) is valued at its own average entry price, so an
  unpriced holding reports a market value equal to its cost and an unrealised P&L of zero
  rather than an invented price move.
* Every figure is ``Decimal`` and rounded once, through the shared Phase 8 helpers.

The service only reads. Persisting a valuation is
:meth:`~app.services.portfolio.service.PaperTradingService.update_market_prices`, which is
the single place that writes marks.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional, Sequence

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.portfolio import Portfolio, PortfolioPosition
from app.repositories.position_repository import position_repository
from app.repositories.portfolio_transaction_repository import portfolio_transaction_repository
from app.repositories.trade_repository import trade_repository
from app.services.backtest.models import quantize_money
from app.services.portfolio.calculator import (
    equity,
    exposure_pct,
    invested_basis,
    invested_capital,
    return_pct,
    sum_market_value,
    sum_open_entry_fees,
    sum_unrealized_pnl,
    total_pnl,
    value_position,
)
from app.services.portfolio.models import ZERO, PortfolioValuation, PositionState


class PortfolioValuationService:
    """Computes a portfolio's mark-to-market view from its persisted state."""

    def __init__(
        self,
        position_repo=position_repository,
        trade_repo=trade_repository,
        transaction_repo=portfolio_transaction_repository,
    ):
        self.position_repo = position_repo
        self.trade_repo = trade_repo
        self.transaction_repo = transaction_repo

    # --- Public API ---

    def value(
        self,
        db: Session,
        portfolio: Portfolio,
        prices: Optional[Dict[int, Decimal]] = None,
    ) -> PortfolioValuation:
        """Value ``portfolio``, optionally overriding the mark of specific stocks.

        ``prices`` affects only this reading: nothing is written. Loading the positions once
        and deriving every figure from that single load is what keeps a portfolio response
        from issuing one query per position.
        """
        positions = self.position_repo.list_open(db, portfolio.id)
        valued = self.value_positions(positions, prices)

        cash = Decimal(portfolio.current_cash)
        value_total = sum_market_value(valued)
        equity_total = equity(cash, value_total)
        basis = invested_basis(
            Decimal(portfolio.initial_capital),
            self.transaction_repo.external_flow(db, portfolio.id),
        )
        pnl = total_pnl(equity_total, basis)
        exposure = exposure_pct(value_total, equity_total)

        logger.debug(
            "Valued portfolio %s: cash=%s market_value=%s equity=%s exposure=%s basis=%s pnl=%s",
            portfolio.id,
            cash,
            value_total,
            equity_total,
            exposure,
            basis,
            pnl,
        )
        return PortfolioValuation(
            portfolio_id=portfolio.id,
            initial_capital=Decimal(portfolio.initial_capital),
            cash=cash,
            market_value=value_total,
            equity=equity_total,
            realized_pnl=Decimal(portfolio.realized_pnl),
            unrealized_pnl=sum_unrealized_pnl(valued),
            total_pnl=pnl,
            total_fees=Decimal(str(self.trade_repo.total_fees(db, portfolio.id) or 0)),
            open_entry_fees=sum_open_entry_fees(valued),
            return_pct=return_pct(pnl, basis),
            invested_capital=invested_capital(valued),
            exposure_pct=exposure,
            positions=valued,
        )

    def value_positions(
        self,
        positions: Sequence[PortfolioPosition],
        prices: Optional[Dict[int, Decimal]] = None,
    ) -> tuple:
        """Mark a set of persisted positions, using ``prices`` for the given stock ids."""
        return tuple(
            value_position(position_state(position), (prices or {}).get(position.stock_id))
            for position in positions
        )

    def exposure_of(self, valuation: PortfolioValuation) -> Optional[Decimal]:
        """Exposure of an existing valuation: market value as a percentage of equity."""
        return exposure_pct(valuation.market_value, valuation.equity)


def position_state(position: PortfolioPosition) -> PositionState:
    """Adapt a persisted position row onto the engine's runtime state.

    Reading a portfolio never needs a re-derivation: the persisted columns are already the
    authoritative values, and the adapter only normalises them to ``Decimal``.
    """
    return PositionState(
        stock_id=position.stock_id,
        quantity=position.quantity,
        average_entry_price=Decimal(position.average_entry_price),
        cost_basis=Decimal(position.cost_basis),
        entry_fees_total=Decimal(position.entry_fees_total),
        realized_pnl=Decimal(position.realized_pnl),
        current_price=(
            Decimal(position.current_price) if position.current_price is not None else None
        ),
    )


def cash_floor(valuation: PortfolioValuation, reserve_pct: Optional[Decimal]) -> Decimal:
    """Cash that must remain available under a ``min_cash_reserve_pct`` rule.

    Returns ``0`` when the rule is disabled, so a caller can always subtract it.
    """
    if reserve_pct is None:
        return quantize_money(ZERO)
    return quantize_money(Decimal(valuation.equity) * Decimal(reserve_pct) / Decimal("100"))