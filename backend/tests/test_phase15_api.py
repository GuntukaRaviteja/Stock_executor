"""Phase 15 — HTTP surface: the three risk routes and their validation.

Every response is asserted as a raw string, because the API contract is decimal strings; an
assertion that passed through ``float()`` would hide a precision regression. The 422 cases matter
as much as the 200 ones: a request the engine cannot honestly describe must be refused before it
produces a report.
"""

from decimal import Decimal

import pytest

from tests.phase15_helpers import (
    LONG_SERIES,
    body_json,
    cyclic,
    diversified_snapshot,
    position,
    request_body,
)

BASE = "/api/v1/portfolios"

#: Limits no realistic portfolio breaches, so a test can assert about something other than
#: the adjustment loop.
WIDE = {
    "max_single_asset_weight": "100",
    "max_sector_weight": "100",
    "max_strategy_weight": "100",
    "max_top_n_concentration": "100",
    "max_portfolio_volatility": "100",
    "max_portfolio_beta": "100",
    "max_drawdown": "100",
    "max_correlation_concentration": "100",
    "max_stress_loss": "100",
}


def _data(response):
    """The report payload, without the server-generated timestamp that differs per call."""
    body = response.json()["data"]
    return {key: value for key, value in body.items() if key != "timestamp"}


def _without_meta(payload):
    return {key: value for key, value in payload.items() if key != "meta"}


def _body(**overrides):
    """A valid request body for the diversified fixture portfolio."""
    snap = diversified_snapshot()
    return body_json(
        request_body(
            snap.positions,
            cash="4000",
            asset_returns=snap.asset_returns,
            portfolio_returns=snap.portfolio_returns,
            benchmark_symbol=snap.benchmark_symbol,
            benchmark_returns=snap.benchmark_returns,
            **overrides,
        )
    )


def _concentrated_body(quantity="1000"):
    """One oversized position, with the data needed to measure beta so the decision is about
    concentration rather than about a metric that could not be evaluated at all."""
    return body_json(
        request_body(
            [position("AAA", int(quantity), Decimal("10"), beta="1.2")],
            cash="0",
            asset_returns={"AAA": cyclic("0.01", 7, LONG_SERIES)},
            portfolio_returns=cyclic("0.002", 9, LONG_SERIES),
            benchmark_symbol="SPY",
            benchmark_returns=cyclic("0.001", 7, LONG_SERIES),
        )
    )


class TestAnalyzeRoute:
    def test_a_within_policy_submission_is_approved(self, client):
        body = _body()
        body["risk_config"] = dict(WIDE)
        response = client.post(f"{BASE}/risk/analyze", json=body)
        assert response.status_code == 200, response.text
        payload = _data(response)
        assert payload["decision"] == "APPROVED"
        assert payload["portfolio_value"] == "20000"
        assert payload["exposure"]["summary"]["total_positions"] == 4
        assert len(payload["stress_tests"]) == 7
        assert payload["decision_explanation"]

    def test_decimal_figures_are_returned_as_strings(self, client):
        body = _body()
        body["risk_config"] = dict(WIDE)
        payload = _data(client.post(f"{BASE}/risk/analyze", json=body))
        assert isinstance(payload["exposure"]["assets"][0]["weight_pct"], str)
        assert isinstance(payload["risk_metrics"]["volatility"]["value"], str)
        assert isinstance(payload["risk_metrics"]["var_95"]["value"], str)

    def test_a_concentrated_submission_is_reduced_and_reported(self, client):
        response = client.post(f"{BASE}/risk/analyze", json=_concentrated_body())
        assert response.status_code == 200, response.text
        payload = _data(response)
        assert payload["decision"] in ("ADJUSTED", "REJECTED")
        assert payload["violations"]
        assert payload["adjustments"]
        assert payload["final_portfolio"]["cash"] != "0"

    def test_missing_market_data_is_reported_as_missing(self, client):
        body = body_json(
            request_body(
                [position("AAA", 100, Decimal("10"))],
                cash="0",
                asset_returns={},
                risk_config=None,
            )
        )
        body["risk_config"] = dict(WIDE)
        response = client.post(f"{BASE}/risk/analyze", json=body)
        assert response.status_code == 200, response.text
        payload = _data(response)
        assert payload["correlation"]["data_quality"] == "INSUFFICIENT"
        assert payload["correlation"]["annualised_volatility_pct"] is None
        volatility = payload["risk_metrics"]["volatility"]
        assert volatility["value"] is None
        assert volatility["status"] == "DATA_INSUFFICIENT"

    def test_the_report_explains_itself(self, client):
        body = _body()
        body["risk_config"] = dict(WIDE)
        payload = _data(client.post(f"{BASE}/risk/analyze", json=body))
        assert payload["explanation"]
        assert payload["data_quality"]
        assert payload["config_applied"]["max_sector_weight"]

    def test_a_partial_risk_config_keeps_the_other_defaults(self, client):
        """Overriding one limit must not silently clear every limit the caller did not mention."""
        body = _body()
        body["risk_config"] = {"max_single_asset_weight": "100"}
        response = client.post(f"{BASE}/risk/analyze", json=body)
        assert response.status_code == 200, response.text
        applied = _data(response)["config_applied"]
        assert Decimal(applied["max_single_asset_weight"]) == Decimal("100")
        assert Decimal(applied["market_crash_shock"]) > 0

    def test_an_explicit_null_limit_disables_that_rule(self, client):
        body = _body()
        body["risk_config"] = dict(WIDE, max_single_asset_weight=None)
        response = client.post(f"{BASE}/risk/analyze", json=body)
        assert response.status_code == 200, response.text
        payload = _data(response)
        applied = payload["config_applied"]
        assert applied["max_single_asset_weight"] is None
        assert "single_asset_weight" not in {item["rule"] for item in payload["violations"]}

    def test_analysis_is_deterministic_over_http(self, client):
        body = _body()
        body["risk_config"] = dict(WIDE)
        first = _data(client.post(f"{BASE}/risk/analyze", json=body))
        second = _data(client.post(f"{BASE}/risk/analyze", json=body))
        assert first == second


class TestAnalyzeValidation:
    """Every refusal below is a request the engine cannot describe honestly."""

    def _refused(self, client, body):
        response = client.post(f"{BASE}/risk/analyze", json=body)
        assert response.status_code == 422, response.text
        return response.json()

    def _raw(self, positions, cash="0", **extra):
        return {"cash": cash, "positions": positions, **extra}

    def test_a_portfolio_with_no_positions_and_no_cash(self, client):
        assert self._refused(client, {"portfolio_value": "0", "cash": "0", "positions": []})[
            "success"
        ] is False

    def test_unreconciled_equity(self, client):
        body = _body()
        body["portfolio_value"] = "999999"
        self._refused(client, body)

    def test_negative_cash(self, client):
        self._refused(
            client,
            self._raw([{"symbol": "AAA", "quantity": 10, "price": "100"}], cash="-50", portfolio_value="1000"),
        )

    def test_a_negative_price(self, client):
        self._refused(
            client,
            self._raw([{"symbol": "AAA", "quantity": 10, "price": "-5"}], portfolio_value="100"),
        )

    def test_a_negative_quantity(self, client):
        self._refused(
            client,
            self._raw(
                [
                    {"symbol": "AAA", "quantity": -10, "price": "100"},
                    {"symbol": "BBB", "quantity": 10, "price": "100"},
                ],
                portfolio_value="1000",
            ),
        )

    def test_a_non_numeric_price(self, client):
        self._refused(
            client,
            self._raw([{"symbol": "AAA", "quantity": 10, "price": "not-a-number"}]),
        )

    def test_a_missing_symbol(self, client):
        body = _body()
        del body["positions"][0]["symbol"]
        self._refused(client, body)

    def test_an_empty_symbol(self, client):
        body = _body()
        body["positions"][0]["symbol"] = ""
        self._refused(client, body)

    def test_a_whitespace_only_symbol(self, client):
        body = _body()
        body["positions"][0]["symbol"] = "   "
        self._refused(client, body)

    def test_a_limit_above_one_hundred_percent(self, client):
        body = _body()
        body["risk_config"] = {"max_single_asset_weight": "150"}
        self._refused(client, body)

    def test_a_zero_limit(self, client):
        body = _body()
        body["risk_config"] = {"max_single_asset_weight": "0"}
        self._refused(client, body)

    def test_a_confidence_outside_zero_and_one(self, client):
        body = _body()
        body["risk_config"] = {"var_confidence": "1.4"}
        self._refused(client, body)

    def test_an_unknown_limit_name(self, client):
        body = _body()
        body["risk_config"] = {"not_a_real_limit": "10"}
        self._refused(client, body)

    def test_an_unknown_position_field(self, client):
        body = _body()
        body["positions"][0]["sentiment_score"] = "0.8"
        self._refused(client, body)

    def test_a_return_series_that_is_not_numeric(self, client):
        body = _body()
        body["asset_returns"]["AAA"][0] = "up a bit"
        self._refused(client, body)

    def test_a_portfolio_id_that_is_not_an_integer(self, client):
        body = _body()
        body["portfolio_id"] = "one"
        self._refused(client, body)

    def test_an_unknown_top_level_field(self, client):
        body = _body()
        body["risk_tolerance"] = "high"
        self._refused(client, body)


class TestAdjustRoute:
    def test_a_breached_book_is_reduced(self, client):
        response = client.post(f"{BASE}/risk/adjust", json=_concentrated_body())
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["adjustments"]
        assert (
            payload["adjusted_portfolio"]["portfolio_value"]
            == payload["original_portfolio"]["portfolio_value"]
        )
        for adjustment in payload["adjustments"]:
            assert adjustment["quantity_sold"] > 0
            assert adjustment["reason"]
            assert adjustment["violated_constraint"]

    def test_equity_is_unchanged_by_the_adjustment(self, client):
        body = body_json(
            request_body(
                [
                    position("AAA", 100, Decimal("10")),
                    position("BBB", 100, Decimal("10")),
                ],
                cash="1000",
                asset_returns={
                    "AAA": cyclic("0.01", 7, LONG_SERIES),
                    "BBB": cyclic("0.008", 5, LONG_SERIES),
                },
            )
        )
        payload = client.post(f"{BASE}/risk/adjust", json=body).json()
        adjusted = payload["adjusted_portfolio"]
        positions_value = sum(
            (Decimal(item["quantity"]) * Decimal(item["price"]) for item in adjusted["positions"]),
            Decimal("0"),
        )
        assert positions_value + Decimal(adjusted["cash"]) == Decimal(adjusted["portfolio_value"])

    def test_a_within_policy_book_is_left_alone(self, client):
        body = _body()
        body["risk_config"] = dict(WIDE)
        response = client.post(f"{BASE}/risk/adjust", json=body)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["adjustments"] == []
        assert payload["decision"] == "APPROVED"

    def test_adjustment_is_deterministic_over_http(self, client):
        body = _concentrated_body()
        first = _without_meta(client.post(f"{BASE}/risk/adjust", json=body).json())
        second = _without_meta(client.post(f"{BASE}/risk/adjust", json=body).json())
        assert first == second

    def test_an_invalid_request_is_refused_here_too(self, client):
        body = _concentrated_body()
        body["cash"] = "-1"
        response = client.post(f"{BASE}/risk/adjust", json=body)
        assert response.status_code == 422, response.text


class TestStoredPortfolioRoute:
    def test_a_missing_portfolio_is_a_404(self, client):
        response = client.post(f"{BASE}/999999/risk/analyze")
        assert response.status_code == 404, response.text

    def test_a_zero_id_is_refused(self, client):
        response = client.post(f"{BASE}/0/risk/analyze")
        assert response.status_code == 422, response.text

    def test_a_config_only_body_is_accepted(self, client):
        """The stored route reads the book from the database, so its body carries only policy."""
        created = client.post(
            BASE,
            json={
                "name": "P15 override",
                "initial_capital": "10000",
                "trading_costs": {"brokerage_rate": "0", "stt_rate": "0"},
            },
        )
        assert created.status_code == 201, created.text
        response = client.post(
            f"{BASE}/{created.json()['portfolio_id']}/risk/analyze",
            json={"risk_config": {"max_stress_loss": "5"}},
        )
        # The portfolio holds nothing, so the refusal is about the book, not about the body.
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        assert "risk_config" not in response.json()["error"]["message"]

    def test_a_data_field_in_the_body_is_refused(self, client):
        """A caller must not be able to declare a portfolio_value the engine will not measure."""
        response = client.post(
            f"{BASE}/1/risk/analyze",
            json={"portfolio_value": "1", "cash": "1", "positions": []},
        )
        assert response.status_code == 422, response.text
        assert "portfolio_value" in response.json()["error"]["message"] or response.json()[
            "error"
        ]["code"] == "VALIDATION_ERROR"

    def test_an_empty_paper_portfolio_is_refused(self, client):
        created = client.post(
            BASE,
            json={
                "name": "P15 empty",
                "initial_capital": "10000",
                "trading_costs": {"brokerage_rate": "0", "stt_rate": "0"},
            },
        )
        assert created.status_code == 201, created.text
        response = client.post(f"{BASE}/{created.json()['portfolio_id']}/risk/analyze")
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_a_stored_portfolio_is_analysed_from_stored_state(self, client, db_session):
        from app.models.stock import Stock

        stock = Stock(symbol="P15CO", company_name="Phase Fifteen Co", exchange="NSE", market="INDIA")
        db_session.add(stock)
        db_session.commit()
        db_session.refresh(stock)

        created = client.post(
            BASE,
            json={
                "name": "P15 held",
                "initial_capital": "100000",
                "trading_costs": {"brokerage_rate": "0", "stt_rate": "0"},
            },
        )
        assert created.status_code == 201, created.text
        portfolio_id = created.json()["portfolio_id"]

        accepted = client.post(
            f"{BASE}/{portfolio_id}/orders",
            json={
                "stock_id": stock.id,
                "side": "BUY",
                "quantity": 10,
                "requested_price": "100",
            },
        )
        assert accepted.status_code == 201, accepted.text

        response = client.post(f"{BASE}/{portfolio_id}/risk/analyze")
        assert response.status_code == 200, response.text
        payload = _data(response)
        assert payload["portfolio_id"] == portfolio_id
        assert payload["exposure"]["summary"]["total_positions"] == 1
        # No traded-volume data was stored, so liquidity is missing rather than assumed good.
        assert payload["liquidity"]["positions_with_data"] == 0


class TestRouteRegistration:
    def test_the_risk_routes_are_registered(self):
        """The OpenAPI document is what a client reads, so it is what the test inspects."""
        from app.main import app

        paths = app.openapi()["paths"]
        assert f"{BASE}/risk/analyze" in paths
        assert f"{BASE}/risk/adjust" in paths
        assert f"{BASE}/{{portfolio_id}}/risk/analyze" in paths
        assert "post" in paths[f"{BASE}/risk/analyze"]
        assert "post" in paths[f"{BASE}/risk/adjust"]

    def test_the_literal_routes_match_before_the_parameterised_ones(self, client):
        """``/risk/analyze`` must not be parsed as a portfolio id."""
        body = body_json(
            request_body(
                [position("AAA", 10, Decimal("10"))],
                cash="0",
                asset_returns={"AAA": cyclic("0.01", 7, LONG_SERIES)},
            )
        )
        response = client.post(f"{BASE}/risk/analyze", json=body)
        assert response.status_code == 200, response.text
        assert _data(response)["portfolio_id"] is None


def _position(symbol, quantity, price, **kwargs):
    from app.schemas.portfolio_risk import PortfolioRiskPositionSchema

    return PortfolioRiskPositionSchema(
        symbol=symbol,
        quantity=quantity,
        price=Decimal(str(price)) if str(price).replace("-", "").replace(".", "").isdigit() else price,
        **kwargs,
    )