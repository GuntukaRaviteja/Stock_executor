"""Pydantic schemas for Phase 11 — portfolio construction, rebalancing and cash movements.

**Simulation only.** Everything here describes decisions taken by the portfolio engine about a
virtual portfolio. No schema carries a brokerage identifier, a credential or a real-money
endpoint, and nothing in this module can place a real order.

Responsibility split, which this module makes explicit in its field names:

======================  ==========================================================
screener (``Phase 7``)   *which* stocks qualify
portfolio (this module) *how much should I hold* in each of them
======================  ==========================================================

Monetary and percentage fields are ``Decimal`` end to end, including across the HTTP
boundary, so no floating-point value is ever parsed into or serialised out of a financial
figure. Percentages are in percent (``20`` means 20%), never fractions.
"""

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.portfolio import (
    PAPER_TRADING_DISCLAIMER,
    DecisionAction,
    OrderSide,
    PortfolioAllocationConfig,
    PortfolioPriceInput,
    PortfolioResponse,
    PortfolioStatus,
    PositionSizingMethod,
    TransactionType,
)
from app.schemas.screeners import TechnicalScreenerRequest

#: Upper bound on a position target weight a caller may request through the rebalance
#: endpoint. A target above this is rejected rather than clamped, because a silently clamped
#: target would make the response disagree with the request.
MAX_TARGET_WEIGHT_PCT = Decimal("100")


# --- Cash movements ---------------------------------------------------------


class CashMovementRequest(BaseModel):
    """Add capital to, or take capital out of, a virtual portfolio.

    A cash movement is not a trade: it creates no order, no position and no realised P&L. It
    does change ``current_cash`` — and therefore equity — and it is recorded in the immutable
    transaction ledger, so the cash history stays auditable independently of the trade history.
    """

    amount: Decimal = Field(..., gt=0, description="Capital moved; must be strictly positive")
    reason: Optional[str] = Field(
        None,
        max_length=255,
        description="Why the capital moved; stored verbatim on the ledger entry",
    )
    as_of: Optional[datetime] = Field(
        None, description="Timestamp of the movement; defaults to now (UTC)"
    )


class PortfolioTransactionResponse(BaseModel):
    """One immutable entry of the portfolio transaction ledger.

    The ledger is append-only: entries are never edited or deleted, so the recorded cash
    history always explains how the current balance was reached. A ``BUY`` / ``SELL`` entry
    carries the ``order_id`` / ``trade_id`` of the simulated execution it belongs to;
    ``stock_id``, ``quantity`` and ``price`` are ``null`` for ``DEPOSIT`` / ``WITHDRAWAL``,
    which have no stock and no price.
    """

    model_config = ConfigDict(from_attributes=True)

    transaction_id: int = Field(..., description="Transaction id")
    portfolio_id: int = Field(..., description="Owning portfolio id")
    stock_id: Optional[int] = Field(None, description="Internal stock ID; null for cash movements")
    symbol: Optional[str] = Field(None, description="Stock symbol, when known")
    transaction_type: TransactionType = Field(
        ..., description="BUY, SELL, DEPOSIT or WITHDRAWAL"
    )
    quantity: Optional[int] = Field(
        None, description="Shares moved; null for a cash movement"
    )
    price: Optional[Decimal] = Field(
        None, description="Execution price; null for a cash movement"
    )
    fees: Decimal = Field(
        Decimal("0"), description="Simulated fees charged; 0 for a cash movement"
    )
    gross_value: Decimal = Field(
        Decimal("0"), description="quantity * price; 0 for a cash movement"
    )
    net_cash_impact: Decimal = Field(
        ..., description="Signed effect on cash: negative for a buy or a withdrawal"
    )
    realized_pnl: Decimal = Field(
        Decimal("0"), description="P&L booked by this entry; 0 for a buy, a deposit or a withdrawal"
    )
    cash_after: Decimal = Field(..., description="Cash balance immediately after this entry")
    order_id: Optional[int] = Field(
        None, description="Simulated order this entry came from; null for a cash movement"
    )
    trade_id: Optional[int] = Field(
        None, description="Execution record this entry came from; null for a cash movement"
    )
    timestamp: datetime = Field(..., description="When the entry was recorded")
    reason: Optional[str] = Field(None, description="Why the entry was recorded")
    metadata: Optional[Dict[str, object]] = Field(
        None, description="Free-form audit metadata stored with the entry"
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class CashMovementResponse(BaseModel):
    """The result of a deposit or withdrawal: the ledger entry *and* the portfolio it changed.

    Both halves are returned because neither is sufficient alone — the entry is the audit record
    of what happened to cash, while the refreshed summary is what the caller actually wants next
    (the new cash, equity and position count, all derived from the same committed write).
    """

    portfolio: PortfolioResponse = Field(..., description="The portfolio after the movement")
    transaction: PortfolioTransactionResponse = Field(
        ..., description="The ledger entry this movement appended"
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class PortfolioTransactionsResponse(BaseModel):
    """Envelope listing the transaction ledger, newest first."""

    success: bool = True
    data: List[PortfolioTransactionResponse]
    count: int
    total: int
    portfolio_id: int
    balance: Decimal = Field(
        ...,
        description=(
            "Cash implied by replaying the whole ledger from the initial capital. Equals "
            "'initial_capital + sum(net_cash_impact)' and, for an intact ledger, the portfolio's "
            "own current cash balance."
        ),
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


# --- Candidate selection ----------------------------------------------------


class PortfolioCandidateInput(BaseModel):
    """One stock offered to the portfolio construction engine.

    ``price`` is optional: when it is omitted the engine resolves the latest stored close for
    the stock. A candidate that has no price at all is reported as ineligible with a reason
    rather than being silently dropped, because "which stocks were considered and why was each
    one skipped" is part of an explainable decision.
    """

    stock_id: int = Field(..., ge=1, description="Internal stock ID")
    price: Optional[Decimal] = Field(
        None, gt=0, description="Current market price; omitted resolves the latest stored close"
    )
    composite_score: Optional[int] = Field(
        None,
        ge=-100,
        le=100,
        description=(
            "Phase 7 composite alignment score. Used only for the deterministic ordering of "
            "candidates and reported in the decision; it never changes a position size."
        ),
    )
    symbol: Optional[str] = Field(
        None, max_length=20, description="Symbol hint; resolved from the stock master when omitted"
    )
    sector: Optional[str] = Field(
        None,
        max_length=100,
        description=(
            "Sector hint for concentration limits; resolved from the company profile when "
            "omitted, and left unknown when no profile exists."
        ),
    )


# --- Construction decisions -------------------------------------------------


class PortfolioDecisionRequest(BaseModel):
    """Ask the construction engine what the portfolio *should* do, and optionally do it.

    Candidates come from exactly one of three sources, and each choice keeps the existing rules
    in their own layer:

    ``candidates``
        explicit stocks with explicit prices — the caller has already screened;
    ``screener``
        a Phase 7 :class:`~app.schemas.screeners.TechnicalScreenerRequest`, evaluated by the
        existing ``ScreenerService``. No screening rule is duplicated here;
    neither
        the engine re-evaluates only the positions the portfolio already holds, which is how
        risk-driven exits (stop-loss, take-profit, drawdown) are discovered.

    With ``apply=true`` the returned decisions are submitted as simulated orders. The default
    is ``false``: a decision is a recommendation until the caller asks for it.
    """

    candidates: List[PortfolioCandidateInput] = Field(
        default_factory=list, description="Explicitly eligible stocks"
    )
    screener: Optional[TechnicalScreenerRequest] = Field(
        None,
        description="Phase 7 screener filters used to select candidates when 'candidates' is empty",
    )
    prices: List[PortfolioPriceInput] = Field(
        default_factory=list,
        description="Prices to mark held positions to before deciding; default marks to the stored closes",
    )
    sizing_method: Optional[PositionSizingMethod] = Field(
        None, description="Overrides the portfolio's configured allocation formula for this call"
    )
    max_position_weight_pct: Optional[Decimal] = Field(
        None, gt=0, le=100, description="Overrides the configured single-position cap for this call"
    )
    risk_per_trade_pct: Optional[Decimal] = Field(
        None, gt=0, le=100, description="Overrides the configured per-trade risk budget"
    )
    cash_reserve_pct: Optional[Decimal] = Field(
        None, ge=0, le=100, description="Overrides the configured minimum cash reserve"
    )
    stop_loss_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Overrides the configured stop-loss for this call; null cannot clear a set stop",
    )
    take_profit_pct: Optional[Decimal] = Field(
        None,
        gt=0,
        le=100,
        description="Overrides the configured take-profit for this call; null cannot clear a set target",
    )
    rebalance_threshold_pct: Optional[Decimal] = Field(
        None, ge=0, le=100, description="Overrides the configured rebalancing threshold"
    )
    max_positions: Optional[int] = Field(
        None, ge=1, description="Caps how many names this call may open, on top of the configured limit"
    )
    apply: bool = Field(
        False,
        description=(
            "Submit the recommended decisions as simulated paper orders. False (the default) "
            "returns the decisions without changing the portfolio."
        ),
    )
    as_of: Optional[datetime] = Field(
        None, description="Decision timestamp; defaults to now (UTC) and is echoed back"
    )

    @model_validator(mode="after")
    def validate_source(self) -> "PortfolioDecisionRequest":
        if self.candidates and self.screener is not None:
            raise ValueError(
                "supply either 'candidates' or 'screener', not both: explicit candidates and "
                "screener filters are two different sources of the same list"
            )
        return self


class PortfolioDecisionResponse(BaseModel):
    """One explainable decision.

    ``reasons`` holds stable machine-readable codes and ``explanation`` holds the prose that
    carries the numbers behind them. Both are always populated, so no decision is ever opaque:
    a ``HOLD`` reports why the portfolio is already where it should be, and a blocked ``BUY``
    reports which limit refused it.
    """

    stock_id: int = Field(..., description="Internal stock ID")
    symbol: Optional[str] = Field(None, description="Stock symbol, when known")
    sector: Optional[str] = Field(None, description="Sector, when a company profile exists")
    action: DecisionAction = Field(..., description="BUY, SELL, HOLD, REDUCE or REBALANCE")
    eligible: bool = Field(
        True, description="False when the candidate was considered but could not be held"
    )
    current_quantity: int = Field(
        0, description="Shares held right now; 0 when the portfolio is flat in this stock"
    )
    target_quantity: int = Field(
        0, description="Shares the decision would hold after it is applied"
    )
    current_weight_pct: Optional[Decimal] = Field(
        None, description="Position weight right now, as a percentage of equity"
    )
    target_weight_pct: Decimal = Field(
        ..., description="Weight the portfolio is targeting for this stock, as a percentage of equity"
    )
    weight_difference_pct: Optional[Decimal] = Field(
        None, description="current_weight_pct - target_weight_pct; negative means underweight"
    )
    price: Optional[Decimal] = Field(
        None, description="Price the decision was computed at; null when the stock has no price"
    )
    estimated_trade_value: Decimal = Field(
        Decimal("0"), description="Market value of the trade the decision would make"
    )
    risk_budget: Optional[Decimal] = Field(
        None, description="Risk budget available to this stock under risk-based sizing"
    )
    risk_exposure: Optional[Decimal] = Field(
        None, description="Risk the recommended quantity would carry to its stop"
    )
    blocked: bool = Field(
        False,
        description=(
            "True when a risk limit refused the trade. The decision is still returned, with "
            "the offending rule named in 'reasons'."
        ),
    )
    reasons: List[str] = Field(
        default_factory=list, description="Stable machine-readable reason codes, in evaluation order"
    )
    explanation: List[str] = Field(
        default_factory=list, description="Human-readable explanation, one entry per reason"
    )
    applied: bool = Field(
        False, description="True when the decision was actually submitted as a simulated order"
    )
    order_id: Optional[int] = Field(
        None, description="The order created when the decision was applied; null otherwise"
    )
    trade_id: Optional[int] = Field(
        None, description="The execution record created when the decision was applied"
    )


class PortfolioDecisionsResponse(BaseModel):
    """The full, deterministic result of one construction run."""

    portfolio_id: int = Field(..., description="Portfolio the decisions were built for")
    status: PortfolioStatus = Field(..., description="Portfolio status at decision time")
    as_of: datetime = Field(..., description="Decision timestamp")
    sizing_method: PositionSizingMethod = Field(..., description="Allocation formula applied")
    equity: Decimal = Field(..., description="Portfolio equity the targets are percentages of")
    cash: Decimal = Field(..., description="Available cash")
    invested_value: Decimal = Field(..., description="Sum of open position market values")
    cash_reserve_required: Decimal = Field(
        ..., description="Cash that must remain available under the configured reserve"
    )
    candidate_count: int = Field(..., description="Candidates supplied or screened")
    eligible_count: int = Field(..., description="Candidates that could be held")
    skipped: List[PortfolioDecisionResponse] = Field(
        default_factory=list,
        description=(
            "Candidates that could not be held at all (no price, below the minimum position "
            "value, position count exhausted), each with the reason recorded"
        ),
    )
    decisions: List[PortfolioDecisionResponse] = Field(
        default_factory=list,
        description=(
            "One decision per stock, ordered deterministically: composite score descending, "
            "then stock id ascending"
        ),
    )
    applied: bool = Field(
        False, description="True when the decisions were submitted as simulated orders"
    )
    configuration: PortfolioAllocationConfig = Field(
        ..., description="Allocation configuration the decisions were built from"
    )
    risk_violations: List[str] = Field(
        default_factory=list,
        description="Names of the risk rules currently breached by the portfolio",
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


# --- Rebalancing ------------------------------------------------------------


class RebalanceRequest(BaseModel):
    """Ask which trades would bring the portfolio to its target weights.

    Targets come from exactly one source:

    ``target_weights``
        explicit ``{stock_id: weight_pct}`` — the caller already knows the allocation;
    ``candidates``
        the eligible stock list, from which targets are derived with the configured allocation
        formula (equal weight by default).

    ``rebalance_threshold_pct`` suppresses trades whose weight drift is smaller than the
    threshold, so rounding noise never produces a transaction.
    """

    target_weights: Optional[Dict[int, Decimal]] = Field(
        None,
        description=(
            "Explicit target weight per stock id, in percent. Must be non-empty when "
            "'candidates' is empty."
        ),
    )
    candidates: List[PortfolioCandidateInput] = Field(
        default_factory=list, description="Eligible stocks used to derive target weights"
    )
    screener: Optional[TechnicalScreenerRequest] = Field(
        None,
        description="Phase 7 screener filters used to derive target weights when 'candidates' is empty",
    )
    prices: List[PortfolioPriceInput] = Field(
        default_factory=list,
        description="Prices to mark positions to before measuring drift; default marks to the stored closes",
    )
    sizing_method: Optional[PositionSizingMethod] = Field(
        None, description="Overrides the configured allocation formula for the derived targets"
    )
    rebalance_threshold_pct: Optional[Decimal] = Field(
        None, ge=0, le=100, description="Overrides the configured rebalancing threshold"
    )
    drop_unlisted: bool = Field(
        False,
        description=(
            "Treat a held stock that has no target weight as a target of zero, which proposes "
            "closing it. False (the default) leaves unlisted holdings alone."
        ),
    )
    apply: bool = Field(
        False,
        description="Submit the proposed trades as simulated paper orders. Default: propose only.",
    )
    as_of: Optional[datetime] = Field(
        None, description="Decision timestamp; defaults to now (UTC) and is echoed back"
    )

    @model_validator(mode="after")
    def validate_targets(self) -> "RebalanceRequest":
        if self.candidates and self.screener is not None:
            raise ValueError(
                "supply either 'candidates' or 'screener', not both: explicit candidates and "
                "screener filters are two different sources of the same list"
            )
        if self.target_weights is not None:
            if not self.target_weights:
                raise ValueError("'target_weights' must not be empty; omit it to derive targets")
            for stock_id, weight in self.target_weights.items():
                if weight < 0:
                    raise ValueError(
                        f"target weight for stock {stock_id} must not be negative; use "
                        "'drop_unlisted' to close a holding instead of a negative target"
                    )
                if weight > MAX_TARGET_WEIGHT_PCT:
                    raise ValueError(
                        f"target weight for stock {stock_id} must be at most "
                        f"{MAX_TARGET_WEIGHT_PCT}%, got {weight}%"
                    )
        elif not self.candidates and self.screener is None:
            raise ValueError(
                "supply 'target_weights', 'candidates' or 'screener': a rebalance with no "
                "targets has nothing to trade towards"
            )
        return self


class RebalanceItemResponse(BaseModel):
    """One stock's rebalancing verdict, including the ones that need no trade."""

    stock_id: int = Field(..., description="Internal stock ID")
    symbol: Optional[str] = Field(None, description="Stock symbol, when known")
    action: DecisionAction = Field(
        ..., description="REBALANCE for a trade, HOLD when the drift is inside the threshold"
    )
    side: Optional[OrderSide] = Field(
        None, description="Direction of the proposed trade; null when no trade is needed"
    )
    current_quantity: int = Field(..., description="Shares held right now")
    target_quantity: int = Field(..., description="Shares the portfolio is targeting")
    required_quantity: int = Field(
        0,
        description="Signed shares to trade: positive to buy, negative to sell; 0 when no trade",
    )
    current_weight_pct: Optional[Decimal] = Field(
        None, description="Position weight right now, as a percentage of equity"
    )
    target_weight_pct: Decimal = Field(..., description="Target weight, as a percentage of equity")
    weight_difference_pct: Optional[Decimal] = Field(
        None, description="current_weight_pct - target_weight_pct; negative means underweight"
    )
    price: Optional[Decimal] = Field(
        None, description="Price the drift was measured at; null when the stock has no price"
    )
    estimated_trade_value: Decimal = Field(
        Decimal("0"), description="Market value of the proposed trade"
    )
    estimated_fees: Decimal = Field(
        Decimal("0"), description="Simulated fees the proposed trade would cost"
    )
    below_threshold: bool = Field(
        False,
        description="True when the drift is smaller than the threshold, so no trade is proposed",
    )
    blocked: bool = Field(
        False, description="True when a risk limit refuses the proposed trade"
    )
    reasons: List[str] = Field(
        default_factory=list, description="Stable machine-readable reason codes, in evaluation order"
    )
    explanation: List[str] = Field(
        default_factory=list, description="Human-readable explanation, one entry per reason"
    )
    applied: bool = Field(False, description="True when the trade was submitted as a paper order")
    order_id: Optional[int] = Field(None, description="The order created, when applied")
    trade_id: Optional[int] = Field(None, description="The execution record created, when applied")


class RebalanceResponse(BaseModel):
    """The full result of one rebalancing pass."""

    portfolio_id: int = Field(..., description="Portfolio that was rebalanced")
    status: PortfolioStatus = Field(..., description="Portfolio status at rebalance time")
    as_of: datetime = Field(..., description="Rebalance timestamp")
    equity: Decimal = Field(..., description="Portfolio equity the weights are percentages of")
    cash: Decimal = Field(..., description="Available cash")
    invested_value: Decimal = Field(..., description="Sum of open position market values")
    rebalance_threshold_pct: Decimal = Field(
        ..., description="Weight drift required before a trade is proposed"
    )
    target_weights: Dict[str, Decimal] = Field(
        default_factory=dict, description="Target weight per stock id, in percent"
    )
    items: List[RebalanceItemResponse] = Field(
        default_factory=list,
        description=(
            "One entry per stock considered, ordered deterministically: stock id ascending. "
            "Entries with 'below_threshold' are HOLDs and need no trade."
        ),
    )
    trade_count: int = Field(..., description="Number of trades actually proposed")
    estimated_total_value: Decimal = Field(
        Decimal("0"), description="Sum of every proposed trade's market value"
    )
    estimated_total_fees: Decimal = Field(
        Decimal("0"), description="Sum of every proposed trade's simulated fees"
    )
    applied: bool = Field(
        False, description="True when the proposed trades were submitted as simulated orders"
    )
    configuration: PortfolioAllocationConfig = Field(
        ..., description="Allocation configuration the targets were derived from"
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")


class PortfolioTransactionTypeResponse(BaseModel):
    """A count per transaction type, so a caller can summarise a ledger without paging it."""

    portfolio_id: int = Field(..., description="Portfolio id")
    counts: Dict[TransactionType, int] = Field(
        default_factory=dict, description="Number of ledger entries per transaction type"
    )
    disclaimer: str = Field(PAPER_TRADING_DISCLAIMER, description="Paper-trading notice")