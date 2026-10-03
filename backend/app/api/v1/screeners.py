"""Phase 7 API endpoints for the technical screener."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, get_screener_service
from app.schemas.screeners import TechnicalScreenerRequest, TechnicalScreenerResponse
from app.services.screener_service import ScreenerService

router = APIRouter(prefix="/screeners", tags=["Technical Screeners"])


@router.post(
    "/technical",
    response_model=TechnicalScreenerResponse,
    summary="Run the technical screener",
    description=(
        "Filters stocks using the deterministic technical conditions produced by the "
        "signal engine.\n\n"
        "All supplied filters are combined with AND logic. Supported filters: "
        "`direction`, `min_score`, `max_score`, `min_rsi`, `max_rsi`, "
        "`price_above_sma`, `price_below_sma`, `macd_direction`, `min_volume`, and an "
        "optional `symbols` whitelist.\n\n"
        "Stocks without enough price history are excluded and counted in "
        "`skipped_insufficient_data`. Results are ordered by composite score "
        "(descending) and paginated with `skip` and `limit`.\n\n"
        "Matching stocks describes **technical alignment only** — it is not financial "
        "advice and does not guarantee any future price movement."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Matching stocks, possibly an empty result set"},
        422: {"description": "Invalid filter combination (e.g. min_score > max_score)"},
    },
)
def run_technical_screener(
    request: TechnicalScreenerRequest,
    db: Session = Depends(get_db),
    service: ScreenerService = Depends(get_screener_service),
) -> TechnicalScreenerResponse:
    """Run the technical screener with the supplied filter criteria."""
    return service.screen(db, request)