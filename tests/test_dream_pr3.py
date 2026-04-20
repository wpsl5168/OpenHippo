"""Tests for F5 Dream — PR-3 (forget + restore + autoloop)."""

from __future__ import annotations

import asyncio
import time

import pytest
from openhippo import HippoEngine
from openhippo.core.dream import DreamConfig, DreamEngine


@pytest.fixture
def engine(tmp_path):
    e = HippoEngine(db_path=tmp_path / "dream3.db")
    yield e
    e.close()


@pytest.fixture
def dream(engine):
    return DreamEngine(engine.storage)


def _backdate(engine, memory_id: str, days_ago: float) -> None:
    """Force a row's created_at into the past so age-based gates trip."""
    conn = engine.storage._get_conn()
    conn.execute(
        "UPDATE cold_memory SET created_at = ? WHERE id = ?",
        (time.time() - days_ago * 86400.0, memory_id),
    )
    conn.commit()


# ── Forget stage ──

class TestForget:
    def test_disabled_by_default(self, dream, engine):
        """consolidate() with default config never marks anything dormant."""
        r = engine.storage.cold_add("memory", "ancient idle low-importance row")
        _backdate(engine, r["id"], days_ago=365)
        result = dream.consolidate()  # default cfg has enable_forget=False
        assert result.forgotten_count == 0
        assert engine.storage.cold_get(r["id"])["dream_status"] == "active"

    def test_old_low_importance_row_gets_forgotten(self, dream, engine):
        r = engine.storage.cold_add("memory", "stale row to forget")
        _backdate(engine, r["id"], days_ago=120)  # decay = 120/30 - 0 - 1.0 = 3.0 > 1.0
        cfg = DreamConfig(enable_forget=True, forget_threshold=1.0)
        result = dream.consolidate(cfg)
        assert result.forgotten_count == 1
        row = engine.storage.cold_get(r["id"])
        assert row["dream_status"] == "dormant"
        assert row["last_dream_at"] is not None

    def test_recent_row_protected_by_min_age(self, dream, engine):
        r = engine.storage.cold_add("memory", "fresh row stays even if low importance")
        _backdate(engine, r["id"], days_ago=3)
        cfg = DreamConfig(enable_forget=True, forget_threshold=0.0,
                          forget_min_age_days=7)
        result = dream.consolidate(cfg)
        assert result.forgotten_count == 0
        assert engine.storage.cold_get(r["id"])["dream_status"] == "active"

    def test_high_importance_row_protected(self, dream, engine):
        r = engine.storage.cold_add("memory", "important row that should survive")
        conn = engine.storage._get_conn()
        conn.execute("UPDATE cold_memory SET importance = 1.0 WHERE id = ?", (r["id"],))
        conn.commit()
        _backdate(engine, r["id"], days_ago=120)
        # decay = 4.0 - 0 - 2.0 = 2.0 — over threshold 1.0... raise threshold
        cfg = DreamConfig(enable_forget=True, forget_threshold=2.5)
        result = dream.consolidate(cfg)
        assert result.forgotten_count == 0
        assert engine.storage.cold_get(r["id"])["dream_status"] == "active"

    def test_forget_audited(self, dream, engine):
        r = engine.storage.cold_add("memory", "row to be forgotten with audit")
        _backdate(engine, r["id"], days_ago=200)
        cfg = DreamConfig(enable_forget=True, forget_threshold=1.0)
        result = dream.consolidate(cfg)
        conn = engine.storage._get_conn()
        actions = conn.execute(
            "SELECT action, details FROM dream_actions WHERE dream_run_id = ?",
            (result.run_id,),
        ).fetchall()
        kinds = [a[0] for a in actions]
        assert "forget" in kinds

    def test_dormant_hidden_from_default_search(self, dream, engine):
        r = engine.storage.cold_add("memory", "specialword12345 dormant test")
        _backdate(engine, r["id"], days_ago=200)
        cfg = DreamConfig(enable_forget=True, forget_threshold=1.0)
        dream.consolidate(cfg)

        results = engine.storage.cold_search("specialword12345", limit=10)
        assert all(x["id"] != r["id"] for x in results)

        # include_dormant returns it
        all_results = engine.storage.cold_search(
            "specialword12345", limit=10, include_dormant=True,
        )
        assert any(x["id"] == r["id"] for x in all_results)


# ── Restore ──

class TestRestore:
    def test_restore_dormant(self, dream, engine):
        r = engine.storage.cold_add("memory", "to-be-forgotten-then-restored")
        _backdate(engine, r["id"], days_ago=200)
        dream.consolidate(DreamConfig(enable_forget=True, forget_threshold=1.0))
        assert engine.storage.cold_get(r["id"])["dream_status"] == "dormant"

        result = dream.restore(r["id"])
        assert result["status"] == "restored"
        assert result["from"] == "dormant"
        row = engine.storage.cold_get(r["id"])
        assert row["dream_status"] == "active"

    def test_restore_consolidated_member(self, dream, engine):
        # Build a consolidatable cluster
        ids = []
        for i in range(2):
            r = engine.storage.cold_add("memory", f"cluster {i}")
            ids.append(r["id"])
            engine.storage.vec_store(r["id"], [1.0 + i * 0.001] + [0.0] * 767)
        dream.consolidate(DreamConfig(l2_threshold=0.5))

        # ids[1] should be consolidated into ids[0]
        member = engine.storage.cold_get(ids[1])
        assert member["dream_status"] == "consolidated"
        result = dream.restore(ids[1])
        assert result["status"] == "restored"
        assert result["from"] == "consolidated"
        row = engine.storage.cold_get(ids[1])
        assert row["dream_status"] == "active"
        assert row["consolidated_into"] is None

    def test_restore_active_is_noop(self, dream, engine):
        r = engine.storage.cold_add("memory", "always active row")
        result = dream.restore(r["id"])
        assert result["status"] == "noop"

    def test_restore_unknown_id(self, dream):
        result = dream.restore("nonexistent-id-xyz")
        assert "error" in result

    def test_restore_writes_audit(self, dream, engine):
        r = engine.storage.cold_add("memory", "restore audit test")
        _backdate(engine, r["id"], days_ago=200)
        dream.consolidate(DreamConfig(enable_forget=True, forget_threshold=1.0))
        result = dream.restore(r["id"])
        conn = engine.storage._get_conn()
        actions = conn.execute(
            "SELECT action FROM dream_actions WHERE dream_run_id = ?",
            (result["run_id"],),
        ).fetchall()
        assert [a[0] for a in actions] == ["restore"]


# ── Background autoloop ──

class TestAutoloop:
    @pytest.mark.asyncio
    async def test_autoloop_runs_consolidate(self, monkeypatch, tmp_path):
        """Spin the autoloop with a tiny interval; ensure it triggers at least once."""
        # Inject our engine into the rest module's globals
        from openhippo.api import rest as rest_mod

        e = HippoEngine(db_path=tmp_path / "loop.db")
        monkeypatch.setattr(rest_mod, "engine", e)

        # Seed a cluster so consolidate has something to do
        ids = []
        for i in range(2):
            r = e.storage.cold_add("memory", f"loop cluster {i}")
            ids.append(r["id"])
            e.storage.vec_store(r["id"], [1.0 + i * 0.001] + [0.0] * 767)

        # Run loop with sub-second interval; cancel after enough time for one tick
        task = asyncio.create_task(rest_mod._dream_autoloop(0.2))
        try:
            await asyncio.sleep(0.6)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # At least one consolidate run should have completed
        runs = DreamEngine(e.storage).list_runs(limit=5)
        completed = [r for r in runs if r["status"] == "completed"]
        assert len(completed) >= 1
        e.close()
