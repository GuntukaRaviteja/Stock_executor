from typing import Any, Optional


class AppException(Exception):
    """Base application exception."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: Optional[Any] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class NotFoundError(AppException):
    def __init__(self, resource: str, identifier: Any):
        super().__init__(
            code=f"{resource.upper()}_NOT_FOUND",
            message=f"{resource} '{identifier}' was not found",
            status_code=404,
        )


class StockNotFoundError(NotFoundError):
    def __init__(self, identifier: Any):
        super().__init__(resource="Stock", identifier=identifier)


class CompanyNotFoundError(NotFoundError):
    def __init__(self, identifier: Any):
        super().__init__(resource="Company", identifier=identifier)


class PriceNotFoundError(NotFoundError):
    def __init__(self, identifier: Any):
        super().__init__(resource="Price", identifier=identifier)


class EventNotFoundError(NotFoundError):
    def __init__(self, identifier: Any):
        super().__init__(resource="Event", identifier=identifier)


class DuplicateResourceError(AppException):
    def __init__(self, resource: str, identifier: Any):
        super().__init__(
            code=f"DUPLICATE_{resource.upper()}",
            message=f"{resource} '{identifier}' already exists",
            status_code=409,
        )


class DuplicateStockError(DuplicateResourceError):
    def __init__(self, symbol: str):
        super().__init__(resource="Stock", identifier=symbol)


class DuplicatePriceError(DuplicateResourceError):
    def __init__(self, stock_id: int, date: str):
        super().__init__(resource="Price", identifier=f"stock_id={stock_id}, date={date}")


class DuplicateEventError(DuplicateResourceError):
    def __init__(self, detail: str):
        super().__init__(resource="Event", identifier=detail)


class AppValidationError(AppException):
    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(
            code="VALIDATION_ERROR",
            message=message,
            status_code=422,
            details=details,
        )


class SignalsUnavailableError(AppException):
    """Raised when a requested signal cannot be produced from the available data.

    Signals are never fabricated or substituted with placeholders, so a request that
    needs an individual signal returns 409 instead of an invented value.
    """

    def __init__(self, code: str, message: str, details: Optional[Any] = None):
        super().__init__(code=code, message=message, status_code=409, details=details)


class BacktestNotFoundError(NotFoundError):
    """Raised when a persisted backtest id does not exist."""

    def __init__(self, identifier: Any):
        super().__init__(resource="Backtest", identifier=identifier)


class UnknownStrategyError(AppValidationError):
    """Raised when a backtest requests a strategy that is not registered."""

    def __init__(self, strategy: str, available: Any = None):
        super().__init__(
            f"Unknown strategy '{strategy}'",
            details={"strategy": strategy, "available": available},
        )


class ProviderError(AppException):
    def __init__(self, provider: str, message: str):
        super().__init__(
            code=f"{provider.upper()}_PROVIDER_ERROR",
            message=f"Provider '{provider}' error: {message}",
            status_code=502,
        )


class DatabaseError(AppException):
    def __init__(self, message: str = "A database error occurred"):
        super().__init__(
            code="DATABASE_ERROR",
            message=message,
            status_code=500,
        )


# --- Phase 9: paper trading / portfolio domain ---


class PortfolioNotFoundError(NotFoundError):
    """Raised when a persisted portfolio id does not exist."""

    def __init__(self, identifier: Any):
        super().__init__(resource="Portfolio", identifier=identifier)


class OrderNotFoundError(NotFoundError):
    """Raised when a persisted paper order id does not exist."""

    def __init__(self, identifier: Any):
        super().__init__(resource="Order", identifier=identifier)


class PortfolioInactiveError(AppException):
    """Raised when an order targets a portfolio that is not ACTIVE.

    ``PAUSED`` and ``CLOSED`` portfolios reject every new order without mutating cash,
    positions or realised P&L.
    """

    def __init__(self, portfolio_id: Any, status: Any):
        super().__init__(
            code="PORTFOLIO_INACTIVE",
            message=(
                f"Portfolio '{portfolio_id}' is '{status}' and cannot accept new orders. "
                "Only ACTIVE portfolios execute orders."
            ),
            status_code=409,
            details={"portfolio_id": portfolio_id, "status": str(status)},
        )


class InvalidOrderError(AppValidationError):
    """Raised when an order request is structurally invalid (quantity, price, type)."""

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message, details=details)


class OrderNotExecutableError(AppException):
    """Raised when an order cannot be executed because its state forbids it.

    Distinct from :class:`InvalidOrderError`: the request is well formed, but the order is
    already settled. That is a conflict with stored state, so it is reported as 409 rather
    than as a 422 validation failure.
    """

    def __init__(self, order_id: Any, status: Any, details: Optional[Any] = None):
        super().__init__(
            code="ORDER_NOT_EXECUTABLE",
            message=(
                f"Order {order_id} is {status}, so it cannot be executed. Only a PENDING "
                "order can be executed."
            ),
            status_code=409,
            details={"order_id": order_id, "status": str(status), **(details or {})},
        )


class InsufficientCashError(AppException):
    """Raised when a BUY cannot be funded by the portfolio's available cash."""

    def __init__(self, required: Any, available: Any, details: Optional[Any] = None):
        payload = {
            "required_cash": str(required),
            "available_cash": str(available),
        }
        if details:
            payload.update(details)
        super().__init__(
            code="INSUFFICIENT_CASH",
            message=(
                f"Insufficient cash: the order requires {required} but the portfolio has "
                f"{available} available."
            ),
            status_code=409,
            details=payload,
        )


class InsufficientPositionError(AppException):
    """Raised when a SELL is larger than the position actually held (or none is held)."""

    def __init__(self, requested: Any, owned: Any, details: Optional[Any] = None):
        payload = {"requested_quantity": str(requested), "owned_quantity": str(owned)}
        if details:
            payload.update(details)
        super().__init__(
            code="INSUFFICIENT_POSITION",
            message=(
                f"Insufficient position: cannot sell {requested} share(s) while holding "
                f"{owned}."
            ),
            status_code=409,
            details=payload,
        )


class RiskLimitExceededError(AppException):
    """Raised when an order violates a configured portfolio risk limit.

    ``code`` is the specific limit that fired (``MAX_EXPOSURE_EXCEEDED``,
    ``MIN_CASH_RESERVE_BREACHED``, …) so a client can branch on the rule without parsing text.
    The HTTP status stays 409 for every risk refusal.
    """

    def __init__(
        self,
        rule: str,
        message: str,
        details: Optional[Any] = None,
        code: str = "RISK_LIMIT_EXCEEDED",
    ):
        super().__init__(
            code=code,
            message=message,
            status_code=409,
            details={"rule": rule, **(details or {})},
        )


class DuplicateExecutionError(AppException):
    """Raised when an identical strategy event is submitted twice.

    Phase 9 protects against accidental double execution with a deterministic
    application-level key, not with distributed locking.
    """

    def __init__(self, key: str, details: Optional[Any] = None):
        super().__init__(
            code="DUPLICATE_EXECUTION",
            message=f"An order for event/key '{key}' has already been processed",
            status_code=409,
            details={"key": key, **(details or {})},
        )


# --- Phase 10: risk management, sizing and paper-trading engine ---


class PositionNotFoundError(NotFoundError):
    """Raised when a portfolio does not hold an open position in the requested stock."""

    def __init__(self, portfolio_id: Any, stock_id: Any):
        super().__init__(resource="Position", identifier=f"portfolio={portfolio_id}, stock={stock_id}")
        self.portfolio_id = portfolio_id
        self.stock_id = stock_id


class InvalidPositionSizeError(AppValidationError):
    """Raised when a position-sizing request cannot produce a defensible quantity.

    Examples: no price to size against, a stop-loss equal to the entry price (which defines
    no risk per share), or a budget too small for a single share. The engine reports these
    rather than rounding a quantity into a trade it cannot justify.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message, details=details)


class ExecutionPriceUnavailableError(AppValidationError):
    """Raised when no execution price can be resolved for a decision.

    Either the bar needed has no usable price, or the requested policy would have required a
    price from a bar that did not exist yet at decision time. This is the engine's explicit
    look-ahead guard, so a caller can never be handed a price it could not have known.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message, details=details)


# --- Phase 11: portfolio construction, rebalancing and cash management ---


class InvalidAllocationConfigError(AppValidationError):
    """Raised when a portfolio's allocation configuration cannot be acted on.

    For example a position cap outside ``(0, 100]``, a negative cash reserve, or risk-based
    sizing with no stop-loss — the last of which means there is no per-share risk, and therefore
    no defensible position size.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message, details=details)


class InvalidCashMovementError(AppValidationError):
    """Raised when a deposit or withdrawal is not a valid capital movement.

    Non-positive amounts are rejected rather than treated as no-ops: a "deposit" of nothing is a
    caller error, not a harmless request.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message, details=details)


class InsufficientCashForWithdrawalError(AppException):
    """Raised when a withdrawal is larger than the portfolio's available cash.

    Refused rather than allowed to overdraw: a portfolio whose cash balance went negative would
    make equity, exposure and every weight measured against it meaningless. ``409`` because the
    request is well formed — it conflicts with the portfolio's stored state.
    """

    def __init__(self, requested: Any, available: Any, details: Optional[Any] = None):
        super().__init__(
            code="INSUFFICIENT_CASH_FOR_WITHDRAWAL",
            message=(
                f"Insufficient cash: the withdrawal requests {requested} but the portfolio only "
                f"has {available} available. Nothing was withdrawn."
            ),
            status_code=409,
            details={
                "requested_amount": str(requested),
                "available_cash": str(available),
                **(details or {}),
            },
        )


class InvalidRebalanceRequestError(AppValidationError):
    """Raised when a rebalancing request cannot produce a set of targets.

    No target weights and no candidate list means there is nothing to trade towards; and a
    target set summing above the configured allocation limit is refused rather than scaled down,
    because silently rescaling would make the executed portfolio differ from the requested one.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message, details=details)


# --- Phase 13: portfolio & risk management engine ---


class InvalidRiskPolicyError(AppValidationError):
    """Raised when a portfolio risk policy cannot be acted on.

    A limit outside its legal range, a negative percentage, a stop-loss of 100% (which would put
    the stop at zero and make the position worth nothing before it is sold), or a risk budget of
    zero or less. Reported rather than silently clamped: a risk policy that is repaired behind
    the caller's back produces a portfolio nobody designed.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message, details=details)


class InvalidStopPriceError(AppValidationError):
    """Raised when a stop loss cannot define a per-share risk for a long position.

    A stop at or above the entry price is not a stop for a long-only book: it defines no
    downside, so there is no per-share risk and therefore no defensible position size. Reported
    as a 422 because the request itself is inconsistent.
    """

    def __init__(
        self,
        entry_price: Any,
        stop_price: Any,
        message: Optional[str] = None,
        details: Optional[Any] = None,
    ):
        payload = {
            "entry_price": str(entry_price),
            "stop_loss_price": str(stop_price),
            "code": "INVALID_STOP_PRICE",
            **(details or {}),
        }
        super().__init__(
            message
            or (
                f"Invalid stop price: a long position's stop ({stop_price}) must be strictly "
                f"below its entry price ({entry_price}). A stop at or above the entry defines no "
                "per-share risk, so no position size can be derived from it."
            ),
            details=payload,
        )


class UnsupportedOrderSideError(AppValidationError):
    """Raised when a request asks for a side this engine cannot simulate.

    The engine is **long-only**. A short is refused explicitly rather than being quietly
    treated as a buy, because a silent buy in place of a requested short is the one failure
    mode that would make a result look correct while describing the opposite trade.
    """

    def __init__(self, side: Any, details: Optional[Any] = None):
        super().__init__(
            f"Unsupported order side '{side}': this engine is long-only and can only size "
            "BUY positions. SELL reduces or closes an existing long position.",
            details={"side": str(side), **(details or {})},
        )


class InvalidPortfolioStateError(AppValidationError):
    """Raised when a portfolio's stored state cannot support a risk calculation.

    A negative cash balance, a negative quantity, a position priced at or below zero, or an
    equity that is not positive where a percentage of equity is required. These are reported
    rather than tolerated, because every weight, exposure and drawdown figure is a ratio against
    equity and a nonsensical equity makes all of them meaningless.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message, details=details)


# --- Phase 15: portfolio-level risk, stress testing and exposure control ---


class InvalidRiskConfigurationError(AppValidationError):
    """Raised when a portfolio risk policy cannot be acted on.

    A limit outside ``(0, 100]``, a VaR confidence outside ``(0, 1)``, an observation floor
    below two, a cluster threshold outside ``[0, 1]``, or a mandatory-limit name that does not
    exist. Reported rather than silently clamped: a risk policy repaired behind the caller's
    back produces decisions nobody designed, and a decision is exactly what this engine exists
    to make trustworthy.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message, details=details)


class InsufficientMarketDataError(AppValidationError):
    """Raised when the market data supplied for a risk run cannot be used as given.

    Return series of inconsistent length, a benchmark shorter than the data it is meant to
    align with, or a covariance block whose shape does not match its symbol list. Missing data
    is normally **not** an error — it is reported as ``DATA_INSUFFICIENT`` on the affected
    metric — so this exception is reserved for input that is internally contradictory, where
    there is no honest way to continue.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message, details=details)


class CovarianceCalculationError(AppValidationError):
    """Raised when a covariance or correlation matrix cannot be trusted.

    A matrix that is not square, is not symmetric, contains non-finite entries, or is not
    positive semi-definite cannot produce a meaningful portfolio variance. Silently projecting
    it to the nearest valid matrix would hide the defect behind a plausible-looking number, so
    the run is refused instead and the caller is told which symbol ordering failed.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message, details=details)


class RiskAnalysisError(AppException):
    """Raised when a portfolio risk analysis cannot be completed.

    The request itself was valid; something in the run went wrong that the caller cannot fix by
    changing the request — an empty position set where positions are required, or a limit set so
    tight that no compliant portfolio exists. ``500`` because it reflects engine state, not a
    malformed request, and the internal exception is never echoed to the client.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(
            code="RISK_ANALYSIS_FAILED",
            message=message,
            status_code=500,
            details=details,
        )


class RiskAdjustmentError(AppException):
    """Raised when a deterministic risk adjustment cannot be produced.

    The portfolio breaches a limit but no legal reduction exists: the position cannot be trimmed
    below its limit without breaching a minimum position count, or the requested action would
    require creating leverage the portfolio does not have. The portfolio is rejected rather than
    adjusted into something that only looks compliant.
    """

    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(
            code="RISK_ADJUSTMENT_FAILED",
            message=message,
            status_code=409,
            details=details,
        )
