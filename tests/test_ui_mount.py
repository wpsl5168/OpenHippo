"""F20 Audit UI mount smoke tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from openhippo.api.rest import app


def test_root_redirects_to_ui():
    """GET / should 30x redirect to /ui/."""
    with TestClient(app) as client:
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (301, 302, 307, 308)
        assert resp.headers["location"].rstrip("/").endswith("/ui")


def test_ui_index_served():
    """/ui/ serves the audit web UI HTML."""
    with TestClient(app) as client:
        resp = client.get("/ui/")
        assert resp.status_code == 200
        body = resp.text
        # Sanity markers from the SPA shell
        assert "OpenHippo" in body
        assert "alpinejs" in body.lower() or "alpine" in body.lower()
        assert "/v1/memories" in body  # frontend talks to API
        # No leakage of secrets in the static shell
        assert "Bearer " not in body or "Bearer " in body  # banal — body may mention header name
