"""Unit and API tests for the Phase 7 technical screener.

All fixtures are deterministic and no external provider is contacted.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from pydantic import ValidationError


from app.schemas.screeners import TechnicalScreenerRequest
from app.schemas.signals import CompositeSignalResponse, SignalAlignment, SignalStatus, SignalDirection
from app.services.signal_service import SignalService
from app.services.screener_service import ScreenerService


# --- Deterministic helpers -------------------------------------------------------

def make_summary(
    stock_id: int = 1,
    symbol: str = "TEST",
    score: int = 0,
    classification=SignalAlignment.NEUTRAL_ALIGNMENT,
    status=SignalStatus.OK,
    price=None,
    rsi=None,
    sma=None,
    ema=None,
    macd_line=None,
    macd_signal=None,
    volume=None,
) -> CompositeSignalResponse:
    """Build a signal summary for filter-level unit tests."""
    from app.schemas.signals import IndicatorSnapshot

    return CompositeSignalResponse(
        stock_id=stock_id,
        symbol=symbol,
        as_of=date(2026, 1, 31),
        status=status,
        composite_score=score,
        classification=classification,
        signals=[],
        indicator_snapshot=IndicatorSnapshot(
            latest_price=price,
            rsi=rsi,
            sma=sma or {},
            ema=ema or {},
            macd_line=macd_line,
            macd_signal=macd_signal,
            volume=volume,
            records=90,
        ),
    )


@pytest.fixture
def screener() -> ScreenerService:
    """A screener instance using the default signal service."""
    return ScreenerService()


# --- 20. Screener parameter validation --------------------------------------------

@pytest.mark.parametrize("score", [-101, 101, 500, -1000])
def test_min_score_out_of_range_rejected(score):
    """min_score must stay inside -100..+100."""
    with pytest.raises(ValidationError):
        TechnicalScreenerRequest(min_score=score)


@pytest.mark.parametrize("score", [-101, 101])
def test_max_score_out_of_range_rejected(score):
    """max_score must stay inside -100..+100."""
    with pytest.raises(ValidationError):
        TechnicalScreenerRequest(max_score=score)


@pytest.mark.parametrize("rsi", [-0.1, 100.1, 250])
def test_rsi_bounds_rejected(rsi):
    """RSI filters must stay inside 0..100."""
    with pytest.raises(ValidationError):
        TechnicalScreenerRequest(min_rsi=rsi)
    with pytest.raises(ValidationError):
        TechnicalScreenerRequest(max_rsi=rsi)


def test_min_score_greater_than_max_score_rejected():
    """min_score above max_score is an invalid filter combination."""
    with pytest.raises(ValidationError, match="min_score must be less than or equal to max_score"):
        TechnicalScreenerRequest(min_score=50, max_score=-50)


def test_min_rsi_greater_than_max_rsi_rejected():
    """min_rsi above max_rsi is an invalid filter combination."""
    with pytest.raises(ValidationError, match="min_rsi must be less than or equal to max_rsi"):
        TechnicalScreenerRequest(min_rsi=70, max_rsi=30)


def test_above_and_below_sma_are_mutually_exclusive():
    """A price cannot simultaneously be above and below the same average."""
    with pytest.raises(ValidationError, match="mutually exclusive"):
        TechnicalScreenerRequest(price_above_sma=20, price_below_sma=20)


def test_invalid_direction_rejected():
    """Only bullish, bearish, and neutral are accepted directions."""
    with pytest.raises(ValidationError):
        TechnicalScreenerRequest(direction="sideways")


@pytest.mark.parametrize("limit", [0, -5, 501])
def test_invalid_limit_rejected(limit):
    """Pagination limits follow the project convention of 1..500."""
    with pytest.raises(ValidationError):
        TechnicalScreenerRequest(limit=limit)


def test_invalid_offset_rejected():
    """Offsets cannot be negative."""
    with pytest.raises(ValidationError):
        TechnicalScreenerRequest(skip=-1)


def test_symbol_whitelist_is_normalised_and_bounded():
    """Symbols are upper-cased and the whitelist is size limited."""
    request = TechnicalScreenerRequest(symbols=["reliance ", "tcs"])
    assert request.symbols == ["RELIANCE", "TCS"]

    with pytest.raises(ValidationError, match="at most 200"):
        TechnicalScreenerRequest(symbols=["AAA"] * 201)


# --- Filter behaviour --------------------------------------------------------------

def test_score_range_filter(screener):
    """min_score and max_score bound the composite score."""
    request = TechnicalScreenerRequest(min_score=20, max_score=80)

    assert screener.matches(make_summary(score=50), request) is True
    assert screener.matches(make_summary(score=10), request) is False
    assert screener.matches(make_summary(score=90), request) is False


@pytest.mark.parametrize(
    "direction,classification,expected",
    [
        (SignalDirection.BULLISH, SignalAlignment.BULLISH_ALIGNMENT, True),
        (SignalDirection.BULLISH, SignalAlignment.STRONG_BULLISH_ALIGNMENT, True),
        (SignalDirection.BULLISH, SignalAlignment.NEUTRAL_ALIGNMENT, False),
        (SignalDirection.BEARISH, SignalAlignment.STRONG_BEARISH_ALIGNMENT, True),
        (SignalDirection.BEARISH, SignalAlignment.BULLISH_ALIGNMENT, False),
        (SignalDirection.NEUTRAL, SignalAlignment.NEUTRAL_ALIGNMENT, True),
        (SignalDirection.NEUTRAL, SignalAlignment.BEARISH_ALIGNMENT, False),
    ],
)
def test_direction_filter(screener, direction, classification, expected):
    """The direction filter maps classifications onto bullish/bearish/neutral."""
    request = TechnicalScreenerRequest(direction=direction)
    summary = make_summary(classification=classification)

    assert screener.matches(summary, request) is expected


def test_rsi_filters(screener):
    """RSI bounds are inclusive at both ends."""
    assert screener.matches(make_summary(rsi=Decimal("50")), TechnicalScreenerRequest(min_rsi=50)) is True
    assert screener.matches(make_summary(rsi=Decimal("50")), TechnicalScreenerRequest(max_rsi=50)) is True
    assert screener.matches(make_summary(rsi=Decimal("40")), TechnicalScreenerRequest(min_rsi=50)) is False
    assert screener.matches(make_summary(rsi=Decimal("60")), TechnicalScreenerRequest(max_rsi=50)) is False


def test_rsi_filter_rejects_missing_rsi(screener):
    """A stock without RSI cannot satisfy an RSI filter."""
    assert screener.matches(make_summary(rsi=None), TechnicalScreenerRequest(min_rsi=30)) is False


def test_price_above_sma_filter(screener):
    """price_above_sma keeps only stocks trading above that average."""
    request = TechnicalScreenerRequest(price_above_sma=20)
    above = make_summary(price=Decimal("110"), sma={"20": Decimal("100")})
    below = make_summary(price=Decimal("90"), sma={"20": Decimal("100")})

    assert screener.matches(above, request) is True
    assert screener.matches(below, request) is False


def test_price_below_sma_filter(screener):
    """price_below_sma keeps only stocks trading below that average."""
    request = TechnicalScreenerRequest(price_below_sma=50)

    assert screener.matches(make_summary(price=Decimal("90"), sma={"50": Decimal("100")}), request) is True
    assert screener.matches(make_summary(price=Decimal("110"), sma={"50": Decimal("100")}), request) is False


def test_sma_filter_rejects_missing_sma(screener):
    """A stock without the requested average cannot satisfy the filter."""
    request = TechnicalScreenerRequest(price_above_sma=20)

    assert screener.matches(make_summary(price=Decimal("110"), sma={}), request) is False


def test_macd_direction_filter(screener):
    """macd_direction compares the MACD line against its signal line."""
    bullish = TechnicalScreenerRequest(macd_direction=SignalDirection.BULLISH)
    bearish = TechnicalScreenerRequest(macd_direction=SignalDirection.BEARISH)

    above = make_summary(macd_line=Decimal("1.5"), macd_signal=Decimal("1.0"))
    below = make_summary(macd_line=Decimal("0.5"), macd_signal=Decimal("1.0"))

    assert screener.matches(above, bullish) is True
    assert screener.matches(above, bearish) is False
    assert screener.matches(below, bearish) is True
    assert screener.matches(below, bullish) is False


def test_min_volume_filter(screener):
    """min_volume keeps only stocks at or above the requested volume."""
    request = TechnicalScreenerRequest(min_volume=1_000_000)

    assert screener.matches(make_summary(volume=1_000_000), request) is True
    assert screener.matches(make_summary(volume=999_999), request) is False
    assert screener.matches(make_summary(volume=None), request) is False


def test_stocks_without_signals_never_match(screener):
    """A stock with no evaluable signals is excluded regardless of other filters."""
    summary = make_summary(
        score=100,
        classification=SignalAlignment.STRONG_BULLISH_ALIGNMENT,
        status=SignalStatus.NO_PRICE_DATA,
    )

    assert screener.matches(summary, TechnicalScreenerRequest()) is False


def test_empty_request_matches_every_evaluable_stock(screener):
    """With no filters, any stock that has signals is returned."""
    assert screener.matches(make_summary(score=0), TechnicalScreenerRequest()) is True


# --- Service behaviour -------------------------------------------------------------

def test_to_result_projects_snapshot_fields(screener):
    """The screener result carries the snapshot values needed for display."""
    summary = make_summary(
        stock_id=7,
        symbol="abc",
        score=42,
        classification=SignalAlignment.BULLISH_ALIGNMENT,
        price=Decimal("123.45"),
        rsi=Decimal("55.5"),
        volume=1234,
    )

    result = screener.to_result(summary)

    assert result.stock_id == 7
    assert result.symbol == "abc"
    assert result.composite_score == 42
    assert result.classification is SignalAlignment.BULLISH_ALIGNMENT
    assert result.latest_price == Decimal("123.45")
    assert result.rsi == Decimal("55.5")
    assert result.volume == 1234


def test_candidate_ids_empty_when_no_stocks(db_session):
    """With an empty catalogue there is nothing to screen."""
    assert ScreenerService()._candidate_ids(db_session, TechnicalScreenerRequest()) == []


def test_candidate_ids_respects_symbol_whitelist(db_session, client):
    """A symbol whitelist narrows the candidate set."""
    symbol = f"W{uuid.uuid4().hex[:6].upper()}"
    created = client.post(
        "/api/v1/stocks",
        json={"symbol": symbol, "company_name": "Whitelist Test", "exchange": "NASDAQ"},
    )
    assert created.status_code == 201, created.text
    stock_id = created.json()["id"]

    ids = ScreenerService()._candidate_ids(
        db_session, TechnicalScreenerRequest(symbols=[symbol])
    )
    assert ids == [stock_id]


# --- API integration ---------------------------------------------------------------

@pytest.fixture
def api_client(client):
    """The project test client (overridden in-memory database) used for API tests."""
    return client


def create_stock(client, suffix: str) -> int:
    """Create a uniquely named stock and return its id."""
    symbol = f"SCR{suffix}{uuid.uuid4().hex[:6].upper()}"
    response = client.post(
        "/api/v1/stocks",
        json={"symbol": symbol, "company_name": f"Screener Test {suffix}", "exchange": "NASDAQ"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def insert_prices(client, stock_id: int, closes, start=date(2025, 1, 1), volume=1_000_000):
    """Insert a deterministic ascending series of closes."""
    for offset, close in enumerate(closes):
        response = client.post(
            f"/api/v1/stocks/{stock_id}/prices",
            json={
                "price_date": (start + timedelta(days=offset)).isoformat(),
                "open_price": str(close),
                "high_price": str(close + Decimal("2")),
                "low_price": str(close - Decimal("2")),
                "close_price": str(close),
                "volume": volume,
            },
        )
        assert response.status_code in (200, 201), response.text


def base_then_rally_series():
    """Decline followed by a rally: bullish alignment."""
    return (
        [Decimal("200") - Decimal(i) * Decimal("1.0") for i in range(60)]
        + [Decimal("140") + Decimal(i) * Decimal("2.0") for i in range(40)]
    )


def rally_then_decline_series():
    """Rally followed by a decline: bearish alignment."""
    return (
        [Decimal("100") + Decimal(i) * Decimal("1.0") for i in range(60)]
        + [Decimal("160") - Decimal(i) * Decimal("2.0") for i in range(40)]
    )


def test_api_screener_returns_matches(api_client):
    """The screener endpoint returns matching stocks with their scores."""
    stock_id = create_stock(api_client, "A")
    insert_prices(api_client, stock_id, base_then_rally_series())

    response = api_client.post("/api/v1/screeners/technical", json={"min_score": -100})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["count"] >= 1
    assert body["count"] == len(body["results"])
    assert body["disclaimer"]
    assert {r["stock_id"] for r in body["results"]} >= {stock_id}

    row = next(r for r in body["results"] if r["stock_id"] == stock_id)
    assert row["classification"] == "bullish_alignment"
    assert row["composite_score"] > 0
    assert row["symbol"]
    assert row["rsi"] is not None


def test_api_screener_direction_filter(api_client):
    """A bullish direction filter returns only bullish alignments."""
    insert_prices(api_client, create_stock(api_client, "BULL"), base_then_rally_series())
    insert_prices(api_client, create_stock(api_client, "BEAR"), rally_then_decline_series())

    response = api_client.post(
        "/api/v1/screeners/technical", json={"direction": "bullish", "min_score": 20}
    )
    assert response.status_code == 200, response.text

    for row in response.json()["results"]:
        assert row["classification"] in {"bullish_alignment", "strong_bullish_alignment"}
        assert row["composite_score"] >= 20


def test_api_screener_bearish_filter(api_client):
    """A bearish direction filter returns only bearish alignments."""
    insert_prices(api_client, create_stock(api_client, "DOWNB"), rally_then_decline_series())

    response = api_client.post(
        "/api/v1/screeners/technical", json={"direction": "bearish", "min_score": -100, "max_score": -20}
    )
    assert response.status_code == 200, response.text

    for row in response.json()["results"]:
        assert row["classification"] in {"bearish_alignment", "strong_bearish_alignment"}


def test_api_screener_neutral_filter(api_client):
    """A neutral direction filter returns only neutral alignments."""
    insert_prices(api_client, create_stock(api_client, "NEUT"), base_then_rally_series())

    response = api_client.post("/api/v1/screeners/technical", json={"direction": "neutral"})
    assert response.status_code == 200, response.text

    for row in response.json()["results"]:
        assert row["classification"] == "neutral_alignment"


def test_api_screener_multiple_matching_stocks(api_client):
    """Several matching stocks are all returned and correctly ordered."""
    for suffix in ("M1", "M2", "M3"):
        insert_prices(api_client, create_stock(api_client, suffix), base_then_rally_series())

    response = api_client.post(
        "/api/v1/screeners/technical", json={"direction": "bullish", "min_score": 20}
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["count"] >= 3
    scores = [r["composite_score"] for r in body["results"]]
    assert scores == sorted(scores, reverse=True)


def test_api_screener_skips_insufficient_data(api_client):
    """Stocks without enough history are excluded and counted separately."""
    create_stock(api_client, "NODATA")

    response = api_client.post("/api/v1/screeners/technical", json={})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["skipped_insufficient_data"] >= 1
    assert all(r["stock_id"] is not None for r in body["results"])


def test_api_screener_empty_result(api_client):
    """A filter that matches nothing returns an empty result set, not an error."""
    insert_prices(api_client, create_stock(api_client, "EMPTY"), base_then_rally_series())

    response = api_client.post("/api/v1/screeners/technical", json={"min_score": 100, "max_score": 100})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["count"] == 0
    assert body["results"] == []


def test_api_screener_min_volume_filter(api_client):
    """min_volume excludes stocks trading below the requested volume."""
    stock_id = create_stock(api_client, "VOL")
    insert_prices(api_client, stock_id, base_then_rally_series(), volume=500_000)

    response = api_client.post("/api/v1/screeners/technical", json={"min_volume": 900_000})
    assert response.status_code == 200, response.text
    assert all(r["stock_id"] != stock_id for r in response.json()["results"])

    response = api_client.post("/api/v1/screeners/technical", json={"min_volume": 100_000})
    assert response.status_code == 200, response.text
    assert any(r["stock_id"] == stock_id for r in response.json()["results"])


def test_api_screener_rsi_filter(api_client):
    """RSI bounds are applied to the latest indicator snapshot."""
    stock_id = create_stock(api_client, "RSI")
    insert_prices(api_client, stock_id, base_then_rally_series())

    response = api_client.post("/api/v1/screeners/technical", json={"min_rsi": 100})
    assert response.status_code == 200, response.text
    assert all(r["stock_id"] != stock_id for r in response.json()["results"])


def test_api_screener_price_above_sma_filter(api_client):
    """price_above_sma keeps only stocks trading above that average."""
    stock_id = create_stock(api_client, "SMA")
    insert_prices(api_client, stock_id, base_then_rally_series())

    response = api_client.post("/api/v1/screeners/technical", json={"price_above_sma": 20})
    assert response.status_code == 200, response.text
    assert any(r["stock_id"] == stock_id for r in response.json()["results"])


def test_api_screener_macd_filter(api_client):
    """macd_direction is applied to the MACD line and signal line."""
    stock_id = create_stock(api_client, "MACD")
    insert_prices(api_client, stock_id, base_then_rally_series())

    bullish = api_client.post("/api/v1/screeners/technical", json={"macd_direction": "bullish"})
    assert bullish.status_code == 200, bullish.text
    assert any(r["stock_id"] == stock_id for r in bullish.json()["results"])


def test_api_screener_pagination(api_client):
    """skip and limit slice the ordered result set."""
    for suffix in ("P1", "P2", "P3"):
        insert_prices(api_client, create_stock(api_client, suffix), base_then_rally_series())

    everything = api_client.post("/api/v1/screeners/technical", json={"limit": 500}).json()
    assert everything["count"] >= 3

    first_page = api_client.post("/api/v1/screeners/technical", json={"limit": 1, "skip": 0}).json()
    second_page = api_client.post("/api/v1/screeners/technical", json={"limit": 1, "skip": 1}).json()

    assert len(first_page["results"]) == 1
    assert first_page["count"] == second_page["count"] == everything["count"]
    assert first_page["results"][0]["symbol"] != second_page["results"][0]["symbol"]


def test_api_screener_invalid_score_rejected(api_client):
    """A score outside -100..+100 is rejected with the project error envelope."""
    response = api_client.post("/api/v1/screeners/technical", json={"min_score": 500})

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_api_screener_min_above_max_rejected(api_client):
    """min_score above max_score is rejected."""
    response = api_client.post("/api/v1/screeners/technical", json={"min_score": 80, "max_score": 20})

    assert response.status_code == 422
    assert response.json()["success"] is False


def test_api_screener_invalid_direction_rejected(api_client):
    """An unknown direction value is rejected."""
    response = api_client.post("/api/v1/screeners/technical", json={"direction": "sideways"})

    assert response.status_code == 422
    assert response.json()["success"] is False


def test_api_screener_invalid_rsi_rejected(api_client):
    """RSI outside 0..100 is rejected."""
    response = api_client.post("/api/v1/screeners/technical", json={"min_rsi": 150})

    assert response.status_code == 422
    assert response.json()["success"] is False


def test_api_screener_mutually_exclusive_sma_rejected(api_client):
    """price_above_sma and price_below_sma cannot be combined."""
    response = api_client.post(
        "/api/v1/screeners/technical", json={"price_above_sma": 20, "price_below_sma": 20}
    )

    assert response.status_code == 422


def test_api_screener_unknown_symbol_whitelist_returns_empty(api_client):
    """A whitelist of unknown symbols yields an empty result rather than an error."""
    response = api_client.post("/api/v1/screeners/technical", json={"symbols": ["NOSUCHSTOCK"]})

    assert response.status_code == 200
    assert response.json()["results"] == []


def test_api_screener_is_deterministic(api_client):
    """Running the same screener twice on unchanged data gives identical results."""
    insert_prices(api_client, create_stock(api_client, "DET"), base_then_rally_series())

    payload = {"direction": "bullish", "min_score": 0}
    first = api_client.post("/api/v1/screeners/technical", json=payload).json()
    second = api_client.post("/api/v1/screeners/technical", json=payload).json()

    assert first == second


def test_screener_uses_injected_signal_service():
    """The screener depends on the signal service rather than computing indicators."""
    service = ScreenerService(signal_service=SignalService())

    assert service.signal_service is not None
    assert hasattr(service.signal_service, "get_stock_signals")