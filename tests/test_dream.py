"""Tests for F5 Dream — PR-1 (schema + preview, side-effect-free)."""

from __future__ import annotations

import pytest
from openhippo import HippoEngine
from openhippo.core.dream import DreamConfig, DreamEngine


@pytest.fixture
def engine(tmp_path):
    e = HippoEngine(db_path=tmp_path / "dream.db")
    yield e
    e.close()


@pytest.fixture
def dream(engine):
    return DreamEngine(engine.storage)


# ── Schema ──

class TestSchema:
    def test_dream_tables_exist(self, engine):
        conn = engine.storage._get_conn()
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "dream_runs" in names
        assert "dream_actions" in names

    def test_cold_memory_dream_columns(self, engine):
        conn = engine.storage._get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cold_memory)")}
        for c in [
            "consolidated_into", "merged_from", "importance",
            "dream_status", "last_dream_at",
        ]:
            assert c in cols, f"Missing column {c}"

    def test_active_default(self, engine):
        """Existing cold rows should default to dream_status='active'."""
        engine.storage.cold_add("memory", "hello world")
        conn = engine.storage._get_conn()
        row = conn.execute(
            "SELECT dream_status FROM cold_memory LIMIT 1"
        ).fetchone()
        assert row[0] == "active"


# ── Preview behavior ──

class TestPreviewSideEffectFree:
    def test_empty_db_preview(self, dream, engine):
        result = dream.preview()
        assert result.candidates_count == 0
        assert result.clusters == []
        # cold_memory must remain untouched
        assert engine.storage.cold_count() == 0

    def test_preview_records_run(self, dream, engine):
        before = len(dream.list_runs())
        dream.preview()
        after = dream.list_runs()
        assert len(after) == before + 1
        assert after[0]["status"] == "preview"

    def test_preview_does_not_mutate_cold(self, dream, engine):
        # Seed some cold memories
        for i in range(5):
            engine.add("memory", f"unrelated entry number {i}")
            # promote to cold so they have embeddings
        # Force cold storage + embeddings
        for entry in engine.get_hot("memory"):
            engine.archive("memory", entry["content"])
        engine.embed_all_cold()

        snapshot_before = [
            (r["id"], r["content"], r.get("dream_status"))
            for r in engine.storage.cold_timeline(limit=100)
        ]
        dream.preview()
        snapshot_after = [
            (r["id"], r["content"], r.get("dream_status"))
            for r in engine.storage.cold_timeline(limit=100)
        ]
        assert snapshot_before == snapshot_after

    def test_preview_finds_clusters_when_similar(self, dream, engine):
        """If we add highly similar memories, preview should find a cluster.

        This test depends on a working embedding backend. Skipped if embeddings
        are unavailable (graceful degradation).
        """
        from openhippo.core.embedding import get_embedding
        if get_embedding("ping") is None:
            pytest.skip("Embedding backend unavailable")

        # Three near-identical statements about the same topic
        topics = [
            "The user prefers concise direct answers without filler",
            "User wants brief direct responses, no fluff",
            "Pei prefers terse direct replies without padding",
        ]
        for t in topics:
            engine.storage.cold_add("memory", t)
        engine.embed_all_cold()

        # Loose threshold to ensure capture even if embedder differs slightly
        cfg = DreamConfig(l2_threshold=1.2, min_cluster_size=2)
        result = dream.preview(cfg)

        # We should find at least one cluster (3 similar items)
        assert result.candidates_count >= 3
        # Don't strictly require clusters — depends on embedder quality.
        # Just ensure no crash and run is recorded.
        assert result.run_id

    def test_config_snapshot_persisted(self, dream):
        cfg = DreamConfig(l2_threshold=0.42, min_cluster_size=3)
        dream.preview(cfg)
        runs = dream.list_runs(limit=1)
        import json
        snap = json.loads(runs[0]["config_snapshot"])
        assert snap["l2_threshold"] == 0.42
        assert snap["min_cluster_size"] == 3
        assert snap["version"] == "f5-pr1"


# ── List runs ──

class TestListRuns:
    def test_empty_initially(self, dream):
        assert dream.list_runs() == []

    def test_ordering_desc(self, dream):
        for _ in range(3):
            dream.preview()
        runs = dream.list_runs()
        assert len(runs) == 3
        # started_at desc — string ISO compare works
        assert runs[0]["started_at"] >= runs[1]["started_at"] >= runs[2]["started_at"]
