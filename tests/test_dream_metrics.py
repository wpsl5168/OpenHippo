"""Tests for /v1/dream/metrics endpoint and DreamEngine.metrics()."""

from __future__ import annotations

import pytest


def test_metrics_endpoint_shape(client):
    r = client.get("/v1/dream/metrics")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "data" in body
    data = body["data"]
    assert "persistent" in data
    assert "scheduler" in data

    persistent = data["persistent"]
    for key in ("total_runs", "by_status", "last_run", "totals", "actions_total"):
        assert key in persistent, f"missing persistent.{key}"

    scheduler = data["scheduler"]
    for key in (
        "auto_enabled", "interval_seconds", "iterations_total",
        "iterations_succeeded", "iterations_failed",
        "last_iteration_at", "last_status", "last_error",
        "last_duration_ms", "next_run_at",
    ):
        assert key in scheduler, f"missing scheduler.{key}"


def test_metrics_increments_after_run(client):
    """A successful /v1/dream/run should bump persistent totals + last_run."""
    before = client.get("/v1/dream/metrics").json()["data"]["persistent"]
    before_total = before["total_runs"]

    # Trigger a dream cycle (no data needed — empty DB still records a run row)
    r = client.post("/v1/dream/run", json={})
    assert r.status_code == 200, r.text

    after = client.get("/v1/dream/metrics").json()["data"]["persistent"]
    assert after["total_runs"] == before_total + 1
    assert after["last_run"] is not None
    assert after["last_run"]["status"] in ("completed", "failed")


def test_metrics_engine_direct():
    """DreamEngine.metrics() works without HTTP layer."""
    from openhippo.core.engine import HippoEngine
    from openhippo.core.dream import DreamEngine

    eng = HippoEngine()
    try:
        m = DreamEngine(eng.storage).metrics()
        assert isinstance(m["total_runs"], int)
        assert isinstance(m["by_status"], dict)
        assert isinstance(m["actions_total"], int)
    finally:
        eng.close()


def test_scheduler_runtime_disabled_in_tests(client):
    """conftest sets OPENHIPPO_DREAM_AUTO=0 so scheduler must be inactive."""
    sched = client.get("/v1/dream/metrics").json()["data"]["scheduler"]
    assert sched["auto_enabled"] is False
    assert sched["iterations_total"] == 0
