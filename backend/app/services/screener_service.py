"""Phase 7 technical screener service.

Filters stocks by the technical conditions produced by the signal engine.

The screener never calculates indicators itself. It asks
:class:`app.services.signal_service.SignalService` for the composite score and the
indicator snapshot of each candidate stock, then applies the requested filters to
those values.
"""

from typing import List

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.schemas.screeners import (
    TechnicalScreenerRequest,
    TechnicalScreenerResponse,
    TechnicalScreenerResult,
)
from app.schemas.signals import CompositeSignalResponse, SignalAlignment, SignalDirection, SignalStatus
from app.services.signal_service import SignalService, signal_service

#: Alignment classes treated as bullish / bearish / neutral by the ``direction`` filter.
BULLISH_ALIGNMENTS = (
    SignalAlignment.BULLISH_ALIGNMENT,
    SignalAlignment.STRONG_BULLISH_ALIGNMENT,
)
BEARISH_ALIGNMENTS = (
    SignalAlignment.BEARISH_ALIGNMENT,
    SignalAlignment.STRONG_BEARISH_ALIGNMENT,
)
NEUTRAL_ALIGNMENTS = (SignalAlignment.NEUTRAL_ALIGNMENT,)


class ScreenerService:
    """Applies technical filter criteria to stocks with computed signals."""

    def __init__(self, signal_service: SignalService = signal_service):
        self.signal_service = signal_service

    # --- Filter evaluation ---

    def matches(self, summary: CompositeSignalResponse, request: TechnicalScreenerRequest) -> bool:
        """Return ``True`` when a signal summary satisfies every requested filter."""
        snapshot = summary.indicator_snapshot

        # Stocks without evaluable signals are never returned by the screener.
        if summary.status is not SignalStatus.OK:
            return False

        if not (request.min_score <= summary.composite_score <= request.max_score):
            return False

        if request.direction is not None:
            alignments = {
                SignalDirection.BULLISH: BULLISH_ALIGNMENTS,
                SignalDirection.BEARISH: BEARISH_ALIGNMENTS,
                SignalDirection.NEUTRAL: NEUTRAL_ALIGNMENTS,
            }[request.direction]
            if summary.classification not in alignments:
                return False

        if request.min_rsi is not None or request.max_rsi is not None:
            if snapshot.rsi is None:
                return False
            rsi = float(snapshot.rsi)
            if request.min_rsi is not None and rsi < request.min_rsi:
                return False
            if request.max_rsi is not None and rsi > request.max_rsi:
                return False

        if request.price_above_sma is not None:
            average = snapshot.sma.get(str(request.price_above_sma))
            if average is None or snapshot.latest_price is None:
                return False
            if float(snapshot.latest_price) <= float(average):
                return False

        if request.price_below_sma is not None:
            average = snapshot.sma.get(str(request.price_below_sma))
            if average is None or snapshot.latest_price is None:
                return False
            if float(snapshot.latest_price) >= float(average):
                return False

        if request.macd_direction is not None:
            if snapshot.macd_line is None or snapshot.macd_signal is None:
                return False
            histogram = float(snapshot.macd_line) - float(snapshot.macd_signal)
            if request.macd_direction is SignalDirection.BULLISH and histogram <= 0:
                return False
            if request.macd_direction is SignalDirection.BEARISH and histogram >= 0:
                return False

        if request.min_volume is not None:
            if snapshot.volume is None or snapshot.volume < request.min_volume:
                return False

        return True

    @staticmethod
    def to_result(summary: CompositeSignalResponse) -> TechnicalScreenerResult:
        """Project a signal summary into a screener result row."""
        snapshot = summary.indicator_snapshot
        return TechnicalScreenerResult(
            stock_id=summary.stock_id,
            symbol=summary.symbol,
            composite_score=summary.composite_score,
            classification=summary.classification,
            as_of=summary.as_of,
            latest_price=snapshot.latest_price,
            rsi=snapshot.rsi,
            volume=snapshot.volume,
            signal_count=summary.signal_count,
        )

    # --- Public API ---

    def screen(self, db: Session, request: TechnicalScreenerRequest) -> TechnicalScreenerResponse:
        """Run the technical screener.

        Candidates are read in pages from the stock repository; each candidate is
        evaluated by the signal service. One analytics pass per candidate is required
        because indicators are computed from stored price history — this is documented
        as a known cost in ``docs/phase-7.md``.

        Results are ordered by composite score (descending) then symbol, and paginated
        with ``skip`` / ``limit``.
        """
        summaries = self.signal_service.screen_contexts(db, self._candidate_ids(db, request))

        skipped = sum(1 for s in summaries if s.status is not SignalStatus.OK)

        matches = [self.to_result(s) for s in summaries if self.matches(s, request)]
        matches.sort(key=lambda r: (-r.composite_score, r.symbol))

        total = len(matches)
        page = matches[request.skip : request.skip + request.limit]

        logger.info(
            f"ScreenerService: evaluated {len(summaries)} stocks, "
            f"{total} matched, {skipped} skipped for insufficient data"
        )

        return TechnicalScreenerResponse(
            count=total,
            total=len(summaries),
            skipped_insufficient_data=skipped,
            skip=request.skip,
            limit=request.limit,
            results=page,
        )

    def _candidate_ids(self, db: Session, request: TechnicalScreenerRequest) -> List[int]:
        """Return the stock IDs to evaluate for this request."""
        if request.symbols:
            stocks = [self.signal_service.stock_repo.get_by_symbol(db, symbol) for symbol in request.symbols]
            return [stock.id for stock in stocks if stock is not None]

        # Screen every stock in the catalogue; the page size keeps the query bounded.
        page_size = 500
        stock_ids: List[int] = []
        offset = 0
        while True:
            batch = self.signal_service.stock_repo.get_all(db, skip=offset, limit=page_size)
            if not batch:
                break
            stock_ids.extend(stock.id for stock in batch)
            if len(batch) < page_size:
                break
            offset += page_size

        return stock_ids


screener_service = ScreenerService()