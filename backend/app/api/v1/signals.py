"""Phase 7 API endpoints for technical signals."""

from typing import List

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, get_signal_service
from app.core.exceptions import SignalsUnavailableError
from app.schemas.signals import (
    CompositeSignalResponse,
    SignalAlignment,
    SignalItem,
    SignalStatus,
)
from app.services.signal_service import MIN_SIGNAL_RECORDS, SignalService

router = APIRouter(prefix="/stocks", tags=["Technical Signals"])


@router.get(
    "/{stock_id}/signals",
    response_model=CompositeSignalResponse,
    summary="Get the current technical signal summary for a stock",
    description=(
        "Evaluates deterministic technical rules against the indicators calculated by "
        "the analytics layer (RSI, SMA, EMA, MACD, Bollinger Bands, volume, momentum).\n\n"
        "Signals describe *current technical conditions only*. They are not financial "
        "advice, not predictions, and not guaranteed buy/sell recommendations.\n\n"
        "When data is missing the response reports a status instead of fabricating "
        "signals: `no_price_data` when the stock has no stored prices, and "
        "`insufficient_data` when fewer than "
        f"{MIN_SIGNAL_RECORDS} usable records exist. Indicators that could not be "
        "computed for the available history are listed in "
        "`data.indicator_snapshot.unavailable_indicators`."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Signal summary, or a status describing why signals are unavailable"},
        404: {"description": "Stock not found"},
        422: {"description": "Validation error"},
    },
)
def get_stock_signals(
    stock_id: int = Path(..., ge=1, description="Internal stock ID"),
    db: Session = Depends(get_db),
    service: SignalService = Depends(get_signal_service),
) -> CompositeSignalResponse:
    """Return the current technical signal summary for a stock."""
    return service.get_stock_signals(db, stock_id)


@router.get(
    "/{stock_id}/signals/latest",
    response_model=SignalItem,
    summary="Get the strongest individual signal for a stock",
    description=(
        "Returns the single signal with the highest numeric strength for the most "
        "recent price record. `strength` is a rule-derived score between 0 and 100 "
        "and is **not** a probability or expected return.\n\n"
        "Signals are never fabricated, so a stock with no price data or with too "
        "little history returns 409 with code `SIGNALS_UNAVAILABLE` and the reason "
        "in `error.details.status`."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Strongest signal for the latest data"},
        404: {"description": "Stock not found"},
        409: {"description": "No signals could be generated from the available data"},
    },
)
def get_latest_signal(
    stock_id: int = Path(..., ge=1, description="Internal stock ID"),
    db: Session = Depends(get_db),
    service: SignalService = Depends(get_signal_service),
) -> SignalItem:
    """Return the strongest individual signal, or 409 when none can be produced.

    Raises:
        SignalsUnavailableError: When the stored data cannot support any signal.
    """
    summary = service.get_stock_signals(db, stock_id)
    if summary.status is not SignalStatus.OK:
        raise SignalsUnavailableError(
            code="SIGNALS_UNAVAILABLE",
            message=summary.message or "Signals are unavailable for this stock",
            details={"status": summary.status.value},
        )

    signal = service.get_latest_signal(db, stock_id)
    if signal is None:
        raise SignalsUnavailableError(
            code="NO_SIGNALS",
            message="No individual signals could be derived from the available indicators",
        )
    return signal


@router.get(
    "/{stock_id}/signals/classification",
    response_model=SignalAlignment,
    summary="Get only the composite score classification for a stock",
    description=(
        "Returns the descriptive alignment classification (`strong_bullish_alignment`, "
        "`bullish_alignment`, `neutral_alignment`, `bearish_alignment`, "
        "`strong_bearish_alignment`) for the latest price record.\n\n"
        "`insufficient_data` is returned instead of a classification when there is "
        "not enough price history, so missing data is never reported as neutral."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Alignment classification, or `insufficient_data`"},
        404: {"description": "Stock not found"},
    },
)
def get_signal_classification(
    stock_id: int = Path(..., ge=1, description="Internal stock ID"),
    db: Session = Depends(get_db),
    service: SignalService = Depends(get_signal_service),
) -> SignalAlignment:
    """Return only the classification label for a stock."""
    return service.get_stock_signals(db, stock_id).classification


__all__: List[str] = ["router"]