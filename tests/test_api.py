"""Tests for the HTTP service.

The API is a transport layer, so these assert the contract rather than the
valuation: correct status codes, an audit trail present in the payload, and the
guarantee that the API and the engine cannot drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AS_OF
from vc_audit.api import main as api_main
from vc_audit.data.mock_provider import MockMarketDataProvider
from vc_audit.engine import value_company


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client whose evidence packs land in a temp dir, not the repo."""
    monkeypatch.setattr(api_main, "OUTPUT_DIR", tmp_path)
    return TestClient(api_main.app)


@pytest.fixture
def payload(company):
    return {
        "company": company.model_dump(mode="json"),
        "as_of": AS_OF.isoformat(),
        "data_mode": "fixtures",
    }


class TestOps:
    def test_healthz(self, client):
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_index_serves_the_front_end(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "VC Audit Tool" in response.text

    def test_openapi_schema_is_generated(self, client):
        schema = client.get("/openapi.json").json()
        assert "/api/valuations" in schema["paths"]


class TestMethodsEndpoint:
    def test_lists_every_registered_method(self, client):
        methods = client.get("/api/methods").json()
        assert {m["id"] for m in methods} == {"comps", "dcf", "last_round"}

    def test_each_method_declares_its_inputs_and_drivers(self, client):
        for method in client.get("/api/methods").json():
            assert method["required_inputs"]
            assert method["drivers"]
            assert method["weight_rationale"]


class TestPeersEndpoint:
    def test_returns_the_screened_universe_with_multiples(self, client):
        body = client.get("/api/peers", params={"sector": "saas", "data": "fixtures"}).json()

        assert len(body["peers"]) == 8
        assert all(p["ev_to_revenue"] > 0 for p in body["peers"])
        assert "yahoo_finance_mock" in body["citation"]

    def test_an_unknown_sector_is_a_404_listing_what_exists(self, client):
        response = client.get("/api/peers", params={"sector": "widgets", "data": "fixtures"})

        assert response.status_code == 404
        assert "saas" in response.json()["detail"]

    def test_a_size_band_narrows_the_screen(self, client):
        narrow = client.get(
            "/api/peers",
            params={
                "sector": "saas",
                "revenue": 10_000_000,
                "size_band": 40,
                "data": "fixtures",
            },
        ).json()
        assert len(narrow["peers"]) < 8


class TestValuationsEndpoint:
    def test_returns_the_conclusion_with_its_trail(self, client, payload):
        body = client.post("/api/valuations", json=payload).json()

        assert body["company_name"] == "Basis AI"
        assert body["concluded_value_usd"] > 0
        assert len(body["method_results"]) == 3
        for result in body["method_results"]:
            assert result["trail"]["steps"], "a result without a trail is not auditable"
            assert result["trail"]["assumptions"]

    def test_matches_the_engine_exactly(self, client, payload, company):
        """The API must never become a second, divergent implementation."""
        direct = value_company(
            company, provider=MockMarketDataProvider(), as_of=AS_OF
        )
        served = client.post("/api/valuations", json=payload).json()

        assert served["fingerprint"] == direct.fingerprint
        assert served["run_id"] == direct.run_id

    def test_overrides_are_applied(self, client, payload):
        body = client.post(
            "/api/valuations", json={**payload, "overrides": {"wacc": 0.25}}
        ).json()
        dcf = next(m for m in body["method_results"] if m["method_id"] == "dcf")
        wacc = next(a for a in dcf["trail"]["assumptions"] if a["key"] == "wacc")

        assert wacc["value"] == 0.25
        assert wacc["origin"] == "user_provided"

    def test_a_method_restriction_is_honoured(self, client, payload):
        body = client.post("/api/valuations", json={**payload, "methods": ["dcf"]}).json()
        assert [m["method_id"] for m in body["method_results"]] == ["dcf"]

    def test_an_unknown_method_is_a_400(self, client, payload):
        response = client.post("/api/valuations", json={**payload, "methods": ["nope"]})

        assert response.status_code == 400
        assert "unknown method" in response.json()["detail"]

    def test_a_company_no_method_supports_is_a_422_not_a_500(self, client):
        response = client.post(
            "/api/valuations",
            json={
                "company": {"name": "Stealth Co", "sector": "saas"},
                "as_of": "2026-08-22",
                "data_mode": "fixtures",
            },
        )

        assert response.status_code == 422
        assert "no valuation method could be applied" in response.json()["detail"]

    def test_a_malformed_record_is_rejected_by_schema_validation(self, client):
        response = client.post(
            "/api/valuations",
            json={
                "company": {"name": "X", "sector": "saas", "ltm_revenue_usd": -1},
                "data_mode": "fixtures",
            },
        )

        assert response.status_code == 422
        assert "ltm_revenue_usd" in json.dumps(response.json())


class TestArchive:
    def test_a_run_can_be_reopened_by_id(self, client, payload):
        created = client.post("/api/valuations", json=payload).json()
        fetched = client.get(f"/api/valuations/{created['run_id']}").json()

        assert fetched["fingerprint"] == created["fingerprint"]

    def test_the_memo_is_served_as_markdown(self, client, payload):
        created = client.post("/api/valuations", json=payload).json()
        response = client.get(f"/api/valuations/{created['run_id']}/memo")

        assert response.status_code == 200
        assert response.text.startswith("# Fair Value Memorandum")
        assert created["fingerprint"] in response.text

    def test_runs_are_listed(self, client, payload):
        created = client.post("/api/valuations", json=payload).json()
        listed = client.get("/api/valuations").json()

        assert [r["run_id"] for r in listed] == [created["run_id"]]

    def test_persist_false_leaves_no_archive(self, client, payload):
        client.post("/api/valuations", json={**payload, "persist": False})
        assert client.get("/api/valuations").json() == []

    def test_an_unknown_run_id_is_a_404(self, client):
        assert client.get("/api/valuations/nope").status_code == 404


class TestExamplesEndpoint:
    def test_serves_the_bundled_records(self, client):
        examples = client.get("/api/examples").json()
        assert {e["file"] for e in examples} == {
            "basis_ai.json",
            "inflo.json",
            "northwind_labs.json",
        }

    def test_returns_empty_when_the_source_tree_is_absent(self, client, monkeypatch):
        monkeypatch.setattr(api_main, "EXAMPLES_DIR", Path("does/not/exist"))
        assert client.get("/api/examples").json() == []
