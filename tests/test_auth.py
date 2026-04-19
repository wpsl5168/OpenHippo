"""Tests for Bearer token authentication."""

import pytest
from fastapi.testclient import TestClient

import os
import importlib


def _make_client(token: str | None = None):
    """Create a fresh test client with optional auth."""
    from openhippo.core.config import reset_config
    reset_config()
    
    if token:
        os.environ["HIPPO_AUTH_TOKEN"] = token
    else:
        os.environ.pop("HIPPO_AUTH_TOKEN", None)
        os.environ.pop("HIPPO_AUTH_ENABLED", None)
    
    import openhippo.api.rest as rest_mod
    importlib.reload(rest_mod)
    client = TestClient(rest_mod.app)
    client.__enter__()  # trigger startup
    return client


def _cleanup():
    os.environ.pop("HIPPO_AUTH_TOKEN", None)
    os.environ.pop("HIPPO_AUTH_ENABLED", None)
    from openhippo.core.config import reset_config
    reset_config()


class TestAuthDisabled:
    """When auth is disabled, all endpoints are open."""

    def setup_method(self):
        self.client = _make_client(token=None)

    def teardown_method(self):
        self.client.__exit__(None, None, None)
        _cleanup()

    def test_health_no_auth(self):
        r = self.client.get("/health")
        assert r.status_code == 200

    def test_api_no_auth(self):
        r = self.client.get("/v1/stats")
        assert r.status_code == 200


class TestAuthEnabled:
    """When auth is enabled, /v1/* requires valid Bearer token."""

    TOKEN = "test-secret-token-12345"

    def setup_method(self):
        self.client = _make_client(token=self.TOKEN)

    def teardown_method(self):
        self.client.__exit__(None, None, None)
        _cleanup()

    def test_health_bypasses_auth(self):
        r = self.client.get("/health")
        assert r.status_code == 200

    def test_no_token_rejected(self):
        r = self.client.get("/v1/stats")
        assert r.status_code == 401

    def test_wrong_token_rejected(self):
        r = self.client.get("/v1/stats", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_valid_token_accepted(self):
        r = self.client.get("/v1/stats", headers={"Authorization": f"Bearer {self.TOKEN}"})
        assert r.status_code == 200

    def test_add_memory_with_token(self):
        r = self.client.post(
            "/v1/memories",
            json={"target": "memory", "content": "auth test entry"},
            headers={"Authorization": f"Bearer {self.TOKEN}"},
        )
        assert r.status_code == 200
        assert "data" in r.json()
