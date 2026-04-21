"""Tests for the async embedding job queue (PR-perf-async-embed).

Covers: enqueue/claim atomicity, worker drain, fallback when async disabled,
crash recovery, retry cap, and end-to-end via cold_add.
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

from openhippo.core import embed_queue
from openhippo.core.engine import HippoEngine
from openhippo.core.embedding import get_embedding


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # Ensure ASYNC mode for these tests regardless of env.
    monkeypatch.setenv("OPENHIPPO_ASYNC_EMBED", "1")
    e = HippoEngine(db_path=tmp_path / "test.db")
    yield e
    e.close()


@pytest.fixture
def sync_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHIPPO_ASYNC_EMBED", "0")
    e = HippoEngine(db_path=tmp_path / "test.db")
    yield e
    e.close()


# ── Schema ──

def test_migration_creates_table(engine):
    conn = engine.storage._get_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='embedding_jobs'"
    ).fetchone()
    assert row is not None


def test_partial_index_exists(engine):
    conn = engine.storage._get_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_embedding_jobs_pending'"
    ).fetchone()
    assert row is not None


# ── Queue ops ──

def test_enqueue_and_claim(engine):
    conn = engine.storage._get_conn()
    job_id = embed_queue.enqueue(conn, "cold_memory", "abc123", "hello world")
    assert job_id > 0

    job = embed_queue.fetch_one_pending(conn)
    assert job is not None
    assert job["target_id"] == "abc123"
    assert job["content"] == "hello world"

    # Once claimed, it's no longer 'pending'
    second = embed_queue.fetch_one_pending(conn)
    assert second is None


def test_mark_done_and_failed(engine):
    conn = engine.storage._get_conn()
    embed_queue.enqueue(conn, "cold_memory", "x1", "content")
    job = embed_queue.fetch_one_pending(conn)
    embed_queue.mark_done(conn, job["id"])
    stats = embed_queue.queue_stats(conn)
    assert stats.get("done") == 1

    embed_queue.enqueue(conn, "cold_memory", "x2", "fail-me")
    job2 = embed_queue.fetch_one_pending(conn)
    embed_queue.mark_failed(conn, job2["id"], "boom")
    stats2 = embed_queue.queue_stats(conn)
    assert stats2.get("failed") == 1


def test_failed_job_retried_until_cap(engine):
    conn = engine.storage._get_conn()
    embed_queue.enqueue(conn, "cold_memory", "retry1", "content")

    for attempt in range(embed_queue.MAX_ATTEMPTS):
        job = embed_queue.fetch_one_pending(conn)
        assert job is not None, f"should still be claimable on attempt {attempt}"
        embed_queue.mark_failed(conn, job["id"], f"err{attempt}")

    # Now attempts == MAX_ATTEMPTS → no longer claimable
    job = embed_queue.fetch_one_pending(conn)
    assert job is None


def test_reset_running_on_startup(engine):
    conn = engine.storage._get_conn()
    embed_queue.enqueue(conn, "cold_memory", "stuck", "content")
    job = embed_queue.fetch_one_pending(conn)
    assert job is not None
    # Simulate worker crash mid-job: row stays 'running'
    reset = embed_queue.reset_running_on_startup(conn)
    assert reset == 1
    # Now claimable again
    rejob = embed_queue.fetch_one_pending(conn)
    assert rejob is not None
    assert rejob["id"] == job["id"]


def test_cleanup_done(engine):
    conn = engine.storage._get_conn()
    embed_queue.enqueue(conn, "cold_memory", "old", "content")
    job = embed_queue.fetch_one_pending(conn)
    embed_queue.mark_done(conn, job["id"])
    # Force timestamp into the past
    conn.execute(
        "UPDATE embedding_jobs SET updated_at=datetime('now','-10 days') WHERE id=?",
        (job["id"],),
    )
    conn.commit()
    deleted = embed_queue.cleanup_done(conn, older_than_days=7)
    assert deleted == 1


# ── Engine integration ──

def test_cold_add_async_returns_pending(engine):
    res = engine.cold_add("memory", "async test content one")
    assert res["embedding_status"] == "pending"
    # Vector NOT yet in cold_embeddings
    conn = engine.storage._get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM cold_embeddings WHERE memory_id=?", (res["id"],)
    ).fetchone()[0]
    assert n == 0
    # Job sits in queue
    stats = embed_queue.queue_stats(conn)
    assert stats.get("pending", 0) >= 1


def test_cold_add_sync_when_disabled(sync_engine):
    res = sync_engine.cold_add("memory", "sync test content one")
    assert res["embedding_status"] == "sync"
    conn = sync_engine.storage._get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM cold_embeddings WHERE memory_id=?", (res["id"],)
    ).fetchone()[0]
    assert n == 1


def test_drain_now_completes_pending(engine):
    res = engine.cold_add("memory", "drain me content please")
    assert res["embedding_status"] == "pending"
    # Skip if no embedding backend available in test env
    if not get_embedding("probe"):
        pytest.skip("no embedding backend configured")
    counts = engine.embed_drain_now()
    assert counts["done"] >= 1
    conn = engine.storage._get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM cold_embeddings WHERE memory_id=?", (res["id"],)
    ).fetchone()[0]
    assert n == 1


def test_is_async_enabled_env_toggle(monkeypatch):
    monkeypatch.setenv("OPENHIPPO_ASYNC_EMBED", "0")
    assert embed_queue.is_async_enabled() is False
    monkeypatch.setenv("OPENHIPPO_ASYNC_EMBED", "1")
    assert embed_queue.is_async_enabled() is True
    monkeypatch.delenv("OPENHIPPO_ASYNC_EMBED", raising=False)
    assert embed_queue.is_async_enabled() is True  # default ON
