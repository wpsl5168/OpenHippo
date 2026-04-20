"""Tests for F5 Dream — PR-2 (consolidate, mutating)."""

from __future__ import annotations

import json

import pytest
from openhippo import HippoEngine
from openhippo.core.dream import DreamConfig, DreamEngine, Cluster


@pytest.fixture
def engine(tmp_path):
    e = HippoEngine(db_path=tmp_path / "dream2.db")
    yield e
    e.close()


@pytest.fixture
def dream(engine):
    return DreamEngine(engine.storage)


def _seed_three(engine) -> list[str]:
    """Insert three cold rows with synthetic embeddings clustered together."""
    s = engine.storage
    ids = []
    for i in range(3):
        r = s.cold_add("memory", f"row {i} about the same topic")
        ids.append(r["id"])
        # near-identical embeddings → tight L2 cluster
        emb = [1.0 + i * 0.001] + [0.0] * 767
        s.vec_store(r["id"], emb)
    return ids


# ── Cluster picking & mutation ──

class TestApplyConsolidation:
    def test_oldest_becomes_seed(self, dream, engine):
        ids = _seed_three(engine)
        # Re-elect deliberately: feed cluster with arbitrary seed; consolidate must pick oldest
        cluster = Cluster(seed_id=ids[2], member_ids=[ids[0], ids[1]], avg_distance=0.01)
        run_id = "test-run-1"
        # Open run row so FK on dream_actions is happy
        engine.storage._get_conn().execute(
            "INSERT INTO dream_runs (id, started_at, status) VALUES (?, ?, 'running')",
            (run_id, "2026-04-20T00:00:00+00:00"),
        )
        n = dream._apply_consolidation(run_id, cluster)
        assert n == 2  # two members merged

        rows = {r["id"]: r for r in engine.storage.cold_timeline(limit=10)}
        # ids[0] was the oldest insert → seed
        assert rows[ids[0]]["dream_status"] == "active"
        assert rows[ids[1]]["dream_status"] == "consolidated"
        assert rows[ids[2]]["dream_status"] == "consolidated"
        assert rows[ids[1]]["consolidated_into"] == ids[0]
        assert rows[ids[2]]["consolidated_into"] == ids[0]

        merged = json.loads(rows[ids[0]]["merged_from"])
        assert set(merged) == {ids[1], ids[2]}

    def test_seed_importance_increased(self, dream, engine):
        ids = _seed_three(engine)
        # Bump member importance so we can verify accumulation
        conn = engine.storage._get_conn()
        for mid in ids:
            conn.execute("UPDATE cold_memory SET importance = 0.5 WHERE id = ?", (mid,))
        conn.commit()

        cluster = Cluster(seed_id=ids[0], member_ids=[ids[1], ids[2]], avg_distance=0.01)
        conn.execute(
            "INSERT INTO dream_runs (id, started_at, status) VALUES (?, ?, 'running')",
            ("imp-run", "2026-04-20T00:00:00+00:00"),
        )
        dream._apply_consolidation("imp-run", cluster)

        seed = engine.storage.cold_get(ids[0])
        # 0.5 + (0.5 + 0.5) * 0.3 = 0.8
        assert abs(seed["importance"] - 0.8) < 1e-6

    def test_idempotent_on_already_consolidated(self, dream, engine):
        ids = _seed_three(engine)
        conn = engine.storage._get_conn()
        conn.execute(
            "INSERT INTO dream_runs (id, started_at, status) VALUES (?, ?, 'running')",
            ("r1", "2026-04-20T00:00:00+00:00"),
        )
        cluster = Cluster(seed_id=ids[0], member_ids=[ids[1], ids[2]], avg_distance=0.01)
        dream._apply_consolidation("r1", cluster)

        # Re-running with same cluster: members already consolidated → 0
        conn.execute(
            "INSERT INTO dream_runs (id, started_at, status) VALUES (?, ?, 'running')",
            ("r2", "2026-04-20T00:01:00+00:00"),
        )
        n = dream._apply_consolidation("r2", cluster)
        assert n == 0


# ── End-to-end consolidate() ──

class TestConsolidateE2E:
    def test_no_op_on_empty_db(self, dream, engine):
        result = dream.consolidate()
        assert result.candidates_count == 0
        assert result.consolidated_count == 0
        assert result.clusters_count == 0
        runs = dream.list_runs(limit=1)
        assert runs[0]["status"] == "completed"

    def test_consolidates_synthetic_cluster(self, dream, engine):
        ids = _seed_three(engine)
        result = dream.consolidate(DreamConfig(l2_threshold=0.5, min_cluster_size=2))

        # Two of three should be merged into the third
        assert result.consolidated_count == 2
        assert result.seeds_updated == 1
        assert result.clusters_count == 1

        # The seed (oldest = ids[0]) survives as active
        seed = engine.storage.cold_get(ids[0])
        assert seed["dream_status"] == "active"
        assert seed["merged_from"] is not None

    def test_audit_actions_written(self, dream, engine):
        _seed_three(engine)
        result = dream.consolidate(DreamConfig(l2_threshold=0.5))
        conn = engine.storage._get_conn()
        actions = conn.execute(
            "SELECT action FROM dream_actions WHERE dream_run_id = ? ORDER BY id",
            (result.run_id,),
        ).fetchall()
        kinds = [a[0] for a in actions]
        # 2 members + 1 seed = 3 actions
        assert kinds.count("consolidate_member") == 2
        assert kinds.count("consolidate_seed") == 1

    def test_consolidated_hidden_from_default_search(self, dream, engine):
        ids = _seed_three(engine)
        dream.consolidate(DreamConfig(l2_threshold=0.5))

        # Default search: only the seed should appear (LIKE fallback works regardless of FTS)
        results = engine.storage.cold_search("topic", limit=10)
        result_ids = {r["id"] for r in results}
        # Seed in, members out
        assert ids[0] in result_ids
        assert ids[1] not in result_ids
        assert ids[2] not in result_ids

        # With include_consolidated, all three should appear
        results_all = engine.storage.cold_search("topic", limit=10, include_consolidated=True)
        all_ids = {r["id"] for r in results_all}
        assert ids[0] in all_ids
        assert ids[1] in all_ids
        assert ids[2] in all_ids

    def test_run_recorded_with_metrics(self, dream, engine):
        _seed_three(engine)
        result = dream.consolidate(DreamConfig(l2_threshold=0.5))
        runs = dream.list_runs(limit=5)
        target = next(r for r in runs if r["id"] == result.run_id)
        assert target["status"] == "completed"
        assert target["consolidated_count"] == 2
        assert target["clusters_count"] == 1
        assert target["forgotten_count"] == 0
        assert target["finished_at"] is not None
