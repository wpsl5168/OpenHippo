"""Pytest fixtures — in-process FastAPI TestClient with isolated tmp DB.

Eliminates the `:8200` external server dependency that previously caused
~42/54 REST tests to fail when no uvicorn was running.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


# ── Env setup MUST happen before openhippo imports ──
# Use a session-scoped tmp directory; clean up at end.
_TMP_DIR = tempfile.mkdtemp(prefix="openhippo_test_")
_DB_PATH = str(Path(_TMP_DIR) / "memory.db")

os.environ["HIPPO_DB_PATH"] = _DB_PATH
os.environ["OPENHIPPO_DREAM_AUTO"] = "0"  # don't run background loop in tests
# Disable auth so existing tests (which don't set bearer tokens) keep passing.
os.environ.pop("HIPPO_AUTH_TOKEN", None)
os.environ.pop("HIPPO_AUTH_ENABLED", None)


@pytest.fixture(scope="session")
def client():
    """In-process FastAPI client. `with` triggers lifespan startup/shutdown."""
    from fastapi.testclient import TestClient
    from openhippo.api.rest import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def _install_global_client(client):
    """Inject the live client into the rest-api test module's helpers."""
    try:
        from tests import test_rest_api as _t
        _t._client = client
    except ImportError:
        pass
    yield
    # Cleanup tmp DB at session end
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
