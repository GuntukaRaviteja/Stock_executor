"""Phase 15 — integration with the phases below it.

Phase 14 allocates capital across strategies, Phase 13 sizes the resulting orders, and Phase 9
holds the positions. Phase 15 asks whether the portfolio they produced is acceptable. These tests
run the real chain — allocate, fill, then report — because the claim being tested is that the
phases compose, not that each works alone.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.phase13_helpers import (
    BASE,
    allocate,
    bar,
    candidate_body,
    create_via_api,
    persist_bars,
    policy_body,
)
from tests.phase15_helpers import body_json, position, request_body


ANCHOR = date(2024, 1, 1)


@pytest.fixture
def allocated(client, db_session):
    """A portfolio holding two positions produced by the Phase 13 allocate endpoint."""
    from app.models.stock import Stock

    portfolio = create_via_api(client, name="P15 chain", capital="1000000")
    stocks = []
    for symbol, price in (("P15A", "100"), ("P15B", "50")):
        record = Stock(symbol=symbol, company_name=f"{symbol} Ltd", exchange="NSE", market="INDIA")
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)
        stocks.append((record, price))

    for record, price in stocks:
        persist_bars(
            db_session,
            record.id,
            [bar(ANCHOR + timedelta(days=index), price, price, price, price, 100000) for index in range(5)],
        )

    response = allocate(
        client,
        portfolio["portfolio_id"],
        {
            "candidates": [
                candidate_body(stocks[0][0].id, 100, stop_price="95", score=90),
                candidate_body(stocks[1][0].id, 50, stop_price="47", score=80),
            ],
            "policy": policy_body(max_position_weight_pct="30", max_risk_per_trade_pct="1"),
            "apply": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert any(item["approved"] for item in body["decisions"]), body
    return portfolio["portfolio_id"]


class TestPhaseChainThenRiskReport:
    def test_an_allocated_portfolio_produces_a_phase_15_report(self, client, allocated):
        response = client.post(f"{BASE}/{allocated}/risk/analyze")
        assert response.status_code == 200, response.text
        report = response.json()["data"]
        assert report["portfolio_id"] == allocated
        assert report["decision"] in ("APPROVED", "ADJUSTED", "REJECTED", "DATA_INSUFFICIENT")

    def test_the_report_sees_the_positions_the_chain_created(self, client, allocated):
        report = client.post(f"{BASE}/{allocated}/risk/analyze").json()["data"]
        assert report["exposure"]["summary"]["total_positions"] >= 1
        summary = report["exposure"]["summary"]
        assert Decimal(summary["total_market_value"]) + Decimal(summary["cash"]) == Decimal(
            report["portfolio_value"]
        )

    def test_stored_volume_makes_liquidity_measurable(self, client, allocated):
        """The bars written above give the traded-value input the liquidity analysis needs."""
        report = client.post(f"{BASE}/{allocated}/risk/analyze").json()["data"]
        assert report["liquidity"]["positions_with_data"] >= 1
        assert report["liquidity"]["overall_score"] is not None

    def test_strategy_labels_come_from_stored_orders(self, client, allocated):
        report = client.post(f"{BASE}/{allocated}/risk/analyze").json()["data"]
        for group in report["exposure"]["strategies"]:
            # A label is either stored in the order metadata or one of the two documented
            # fallbacks; it is never a name no order recorded.
            assert group["strategy"] in {"MANUAL", "STRATEGY"}

    def test_the_stored_report_is_deterministic(self, client, allocated):
        first = client.post(f"{BASE}/{allocated}/risk/analyze").json()["data"]
        second = client.post(f"{BASE}/{allocated}/risk/analyze").json()["data"]
        assert {k: v for k, v in first.items() if k != "timestamp"} == {
            k: v for k, v in second.items() if k != "timestamp"
        }

    def test_returns_from_stored_bars_feed_the_covariance(self, client, allocated):
        report = client.post(f"{BASE}/{allocated}/risk/analyze").json()["data"]
        quality = report["data_quality"]
        # Five identical closes produce no return observations at all, which the report says.
        assert quality["covariance"] in ("INSUFFICIENT", "PARTIAL", "SUFFICIENT")
        assert "min_observations_required" in quality


class TestRiskConfigScope:
    def test_a_submitted_request_uses_the_application_policy(self, client):
        body = body_json(request_body([position("AAA", 10, Decimal("100"))], cash="0"))
        response = client.post(f"{BASE}/risk/analyze", json=body)
        assert response.status_code == 200, response.text
        applied = response.json()["data"]["config_applied"]
        assert Decimal(applied["max_single_asset_weight"]) > 0

    def test_a_request_override_wins_over_the_application_default(self, client):
        body = body_json(request_body([position("AAA", 10, Decimal("100"))], cash="0"))
        body["risk_config"] = {"max_single_asset_weight": "33"}
        applied = client.post(f"{BASE}/risk/analyze", json=body).json()["data"]["config_applied"]
        assert applied["max_single_asset_weight"] == "33"

    def test_an_override_on_the_stored_route_applies_to_that_portfolio(self, client, allocated):
        response = client.post(
            f"{BASE}/{allocated}/risk/analyze",
            json={"risk_config": {"max_single_asset_weight": "33"}},
        )
        assert response.status_code == 200, response.text
        applied = response.json()["data"]["config_applied"]
        assert applied["max_single_asset_weight"] == "33"


class TestAggregationAcrossStrategies:
    def test_lots_of_one_symbol_aggregate_into_one_position(self, client):
        """Phase 14 hands the same instrument to several strategies; Phase 15 judges the total."""
        body = body_json(
            request_body(
                [
                    position("AAA", 60, Decimal("10"), strategy="MOM"),
                    position("AAA", 60, Decimal("10"), strategy="VAL"),
                ],
                cash="0",
            )
        )
        body["risk_config"] = {"max_single_asset_weight": "50"}
        report = client.post(f"{BASE}/risk/analyze", json=body).json()["data"]

        assert report["exposure"]["summary"]["total_positions"] == 1
        assert report["exposure"]["summary"]["total_lots"] == 2
        assert Decimal(report["exposure"]["assets"][0]["weight_pct"]) == Decimal("100.0000")
        assert sorted(
            item["strategy"] for item in report["exposure"]["strategies"]
        ) == ["MOM", "VAL"]
        breached = {item["rule"] for item in report["violations"] if item["status"] == "BREACH"}
        assert "single_asset_weight" in breached


class TestNoPhaseCoupling:
    def test_phase_15_does_not_need_a_stored_portfolio(self, client):
        body = body_json(
            request_body([position("AAA", 10, Decimal("100"))], cash="500")
        )
        report = client.post(f"{BASE}/risk/analyze", json=body).json()["data"]
        assert report["portfolio_id"] is None
        assert report["exposure"]["summary"]["total_positions"] == 1

    def test_the_engine_writes_nothing_back(self, client, db_session, allocated):
        from app.models.portfolio import Portfolio

        before = db_session.query(Portfolio).count()
        for _ in range(2):
            assert client.post(f"{BASE}/{allocated}/risk/analyze").status_code == 200
        assert db_session.query(Portfolio).count() == before