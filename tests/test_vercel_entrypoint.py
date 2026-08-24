"""Tests for the serverless entry point.

These exist because a deployment bug of exactly this shape is invisible locally:
`vercel.json` rewrites every path to `/api/index`, Vercel hands the function the
rewritten path, and the app answers every page with `{"detail":"Not Found"}`
while `vc-audit serve` works perfectly on the same code.

The whole point of the wrapper is to make that failure reproducible in the test
suite rather than only in production.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.index import app  # noqa: E402  (the entry point is not an installed module)


@pytest.fixture
def client():
    return TestClient(app)


class TestRewrittenPaths:
    """What Vercel actually delivers after its catch-all rewrite."""

    def test_the_rewritten_root_serves_the_web_ui(self, client):
        """The regression this file exists for: `/` arriving as `/api/index`."""
        response = client.get("/api/index")

        assert response.status_code == 200
        assert "VC Audit Tool" in response.text

    @pytest.mark.parametrize(
        ("rewritten", "expected_status"),
        [
            ("/api/index/healthz", 200),
            ("/api/index/api/capabilities", 200),
            ("/api/index/api/methods", 200),
            ("/api/index/openapi.json", 200),
        ],
    )
    def test_rewritten_subpaths_reach_their_route(self, client, rewritten, expected_status):
        assert client.get(rewritten).status_code == expected_status

    def test_a_genuinely_unknown_path_still_404s(self, client):
        """The shim must not turn every miss into a 200."""
        assert client.get("/api/index/no/such/route").status_code == 404


class TestUnrewrittenPaths:
    """Hosts that pass the original path through must be unaffected."""

    def test_the_real_root_still_works(self, client):
        response = client.get("/")

        assert response.status_code == 200
        assert "VC Audit Tool" in response.text

    def test_healthz_is_untouched(self, client):
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_real_api_routes_are_not_mistaken_for_the_function_path(self, client):
        """`/api/examples` starts with `/api/` but must never be stripped."""
        assert client.get("/api/capabilities").status_code == 200
        assert client.get("/api/methods").status_code == 200

    def test_a_path_merely_prefixed_like_the_function_is_not_stripped(self, client):
        """`/api/indexes` is not `/api/index`, so it must 404 rather than match."""
        assert client.get("/api/indexes").status_code == 404


class TestParity:
    """The entry point must add no behaviour of its own."""

    def test_it_wraps_the_same_app_the_cli_serves(self):
        from vc_audit.api.main import app as served

        assert app._app is served

    def test_a_valuation_works_through_the_rewritten_path(self, client, company):
        response = client.post(
            "/api/index/api/valuations",
            json={
                "company": company.model_dump(mode="json"),
                "as_of": "2026-08-22",
                "data_mode": "fixtures",
                "persist": False,
            },
        )

        assert response.status_code == 200
        assert response.json()["concluded_value_usd"] > 0
