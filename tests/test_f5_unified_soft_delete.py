"""F5 v0.3 PR-1: unified soft-delete pipeline tests.

Cases D11–D18 from the design doc (~/obsidian-vault/.../F5-...-v0.3.md §5.2).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

import pytest

# Each test gets a fresh DB so triggers/migrations don't accumulate.
@pytest.fixture
def storage(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="f5_test_")
    db_path = str(Path(tmp) / "memory.db")
    monkeypatch.setenv("HIPPO_DB_PATH", db_path)
    # Force a fresh Storage instance (singleton-like behavior across tests
    # would otherwise reuse a different path).
    from openhippo.core import storage as storage_mod
    s = storage_mod.Storage(db_path=db_path)
    yield s


def _audit_for(storage, mid, action="mark_dormant"):
    return storage._get_conn().execute(
        "SELECT details FROM dream_actions WHERE memory_id=? AND action=? "
        "ORDER BY created_at DESC LIMIT 1",
        (mid, action),
    ).fetchone()


# ── D11: cold_delete is now a soft-delete ──
def test_d11_cold_delete_is_soft(storage):
    res = storage.cold_add(target="memory", content="d11 sample")
    mid = res["id"]
    out = storage.cold_delete(mid)
    assert out["status"] == "dormant", out
    row = storage._get_conn().execute(
        "SELECT dream_status FROM cold_memory WHERE id=?", (mid,)
    ).fetchone()
    assert row is not None  # NOT physically deleted
    assert row["dream_status"] == "dormant"
    audit = _audit_for(storage, mid)
    assert audit is not None
    details = json.loads(audit["details"])
    assert details["reason"] == "cold_delete"
    assert details["actor"] == "api"
    assert details["source_tier"] == "cold"


# ── D12: hot_remove archives to cold + marks dormant ──
def test_d12_hot_remove_archives_then_dormant(storage):
    storage.hot_add("memory", "d12 sample content")
    out = storage.hot_remove("memory", "d12 sample")
    assert out["status"] == "removed"
    cold_id = out["cold_id"]
    row = storage._get_conn().execute(
        "SELECT dream_status, content FROM cold_memory WHERE id=?", (cold_id,)
    ).fetchone()
    assert row["dream_status"] == "dormant"
    assert "d12 sample" in row["content"]
    audit = _audit_for(storage, cold_id)
    details = json.loads(audit["details"])
    assert details["reason"] == "hot_remove"
    assert details["source_tier"] == "hot"
    assert details["original_id"] == out["id"]


# ── D13: promote leaves cold side dormant w/ consolidate_promoted reason ──
def test_d13_promote_marks_cold_dormant(storage):
    res = storage.cold_add(target="memory", content="d13 promote test")
    mid = res["id"]
    out = storage.promote(mid)
    assert out["status"] == "promoted"
    row = storage._get_conn().execute(
        "SELECT dream_status FROM cold_memory WHERE id=?", (mid,)
    ).fetchone()
    assert row["dream_status"] == "dormant"
    audit = _audit_for(storage, mid)
    details = json.loads(audit["details"])
    assert details["reason"] == "consolidate_promoted"


# ── D14: vec_search hides dormant by default, opt-in with include_dormant ──
def test_d14_vec_search_filters_dormant(storage):
    # Use stub embedding (zeroes) — vec_search just needs vec rows to exist.
    a = storage.cold_add(target="memory", content="d14 alpha")
    storage.vec_store(a["id"], [0.0] * 768)
    b = storage.cold_add(target="memory", content="d14 beta")
    storage.vec_store(b["id"], [0.0] * 768)
    storage.cold_delete(b["id"])  # soft delete beta

    q = [0.0] * 768
    default = storage.vec_search(q, limit=10)
    ids_default = {r["id"] for r in default}
    assert b["id"] not in ids_default, "dormant must be filtered by default"

    audit_view = storage.vec_search(q, limit=10, include_dormant=True)
    ids_audit = {r["id"] for r in audit_view}
    assert b["id"] in ids_audit, "include_dormant=True must surface dormant rows"


# ── D14b: cold_search / cold_timeline / unified_search filter too ──
def test_d14b_all_recall_paths_filter_dormant(storage):
    a = storage.cold_add(target="memory", content="d14b unique-token apple")
    b = storage.cold_add(target="memory", content="d14b unique-token banana")
    storage.cold_delete(b["id"])

    # cold_search default
    rows = storage.cold_search("unique-token", limit=10)
    ids = {r["id"] for r in rows}
    assert a["id"] in ids and b["id"] not in ids
    # opt-in
    rows_audit = storage.cold_search("unique-token", limit=10, include_dormant=True)
    assert b["id"] in {r["id"] for r in rows_audit}

    # cold_timeline default
    tl = storage.cold_timeline(target="memory", limit=50)
    ids_tl = {r["id"] for r in tl}
    assert a["id"] in ids_tl and b["id"] not in ids_tl
    # opt-in
    tl_audit = storage.cold_timeline(target="memory", limit=50, include_dormant=True)
    assert b["id"] in {r["id"] for r in tl_audit}

    # unified_timeline default (hot+cold combined; cold dormant must be hidden)
    u = storage.unified_timeline(target="memory", limit=50)
    ids_u = {r["id"] for r in u}
    assert b["id"] not in ids_u


# ── D15: vec rows survive mark_dormant; restore makes them findable ──
def test_d15_vec_survives_dormant_and_restore(storage):
    res = storage.cold_add(target="memory", content="d15 sample")
    mid = res["id"]
    storage.vec_store(mid, [0.0] * 768)
    storage.cold_delete(mid)
    # vec row should still exist
    cnt = storage._get_conn().execute(
        "SELECT COUNT(*) FROM cold_memory_vec WHERE memory_id=?", (mid,)
    ).fetchone()[0]
    assert cnt == 1, "vec row must NOT be deleted on soft-delete"
    # restore
    from openhippo.core.dream import DreamEngine
    eng = DreamEngine(storage)
    eng.restore(mid)
    # findable in default vec_search now
    res2 = storage.vec_search([0.0] * 768, limit=10)
    assert mid in {r["id"] for r in res2}


# ── D16: triggers reject invalid dream_status / dream_action ──
def test_d16_triggers_reject_invalid_values(storage):
    res = storage.cold_add(target="memory", content="d16 sample")
    mid = res["id"]
    conn = storage._get_conn()
    # invalid dream_status on UPDATE
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE cold_memory SET dream_status='garbage' WHERE id=?", (mid,)
        )
    # invalid dream_action on INSERT
    # First create a synthetic dream_run to satisfy FK
    rid = f"test:{uuid.uuid4().hex}"
    conn.execute(
        """INSERT INTO dream_runs (id, started_at, finished_at, status,
           config_snapshot, candidates_count, clusters_count,
           consolidated_count, forgotten_count)
           VALUES (?, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z',
                   'manual','{}',0,0,0,0)""",
        (rid,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO dream_actions (dream_run_id, action, memory_id, details, created_at)
               VALUES (?, 'nope', ?, '{}', '2026-01-01T00:00:00.000Z')""",
            (rid, mid),
        )


# ── D17: grep guard — only one writer for hard-delete and dormant-mark ──
def test_d17_grep_guard_single_writer():
    """Static grep over storage.py + dream.py:
    - 'DELETE FROM cold_memory' (without _vec/_embeddings) only in purge_overdue_dormant
    - "dream_status = 'dormant'" only in _mark_dormant (storage) and one trigger SQL
    """
    src = Path(__file__).parent.parent / "src" / "openhippo" / "core"
    storage_py = (src / "storage.py").read_text()
    dream_py = (src / "dream.py").read_text()

    # 1. Hard delete on cold_memory (NOT cold_memory_vec / cold_embeddings)
    pat_hard = re.compile(r"DELETE\s+FROM\s+cold_memory\b(?!_)")
    storage_hits = pat_hard.findall(storage_py)
    dream_hits = pat_hard.findall(dream_py)
    # storage.py: zero hard-deletes after F5 PR-1
    assert len(storage_hits) == 0, f"storage.py must not hard-delete cold_memory anymore (got {len(storage_hits)})"
    # dream.py: exactly one — the purge_overdue_dormant body
    assert len(dream_hits) == 1, f"dream.py should have exactly one DELETE FROM cold_memory (purge), got {len(dream_hits)}"

    # 2. dream_status WRITES to dormant (SET dream_status='dormant' only;
    # filter expressions like NOT IN ('dormant') don't count as writes)
    pat_dorm_write = re.compile(r"SET\s+dream_status\s*=\s*'dormant'", re.IGNORECASE)
    storage_dorm = pat_dorm_write.findall(storage_py)
    dream_dorm = pat_dorm_write.findall(dream_py)
    # storage.py: only inside _mark_dormant (one UPDATE)
    assert len(storage_dorm) == 1, f"storage.py: only _mark_dormant should write dormant (got {len(storage_dorm)})"
    # dream.py: zero writes — forget_decay routes through _mark_dormant now
    assert len(dream_dorm) == 0, f"dream.py: must route through _mark_dormant (got {len(dream_dorm)} direct writes)"


# ── D18: backfill migration marks [snapshot %] rows dormant; spares [2026- rows ──
def test_d18_backfill_scope_is_snapshot_only(storage):
    """Simulate residue and re-run the backfill SQL idempotently.

    We can't re-execute migration 011 cleanly (it's already applied), so we
    inject fixtures and apply just the backfill sub-statements.
    """
    # 1. Plant fixtures
    snap = storage.cold_add(target="memory",
                            content="[snapshot 2026-04-21 06:50:15 session=x] test residue")
    real = storage.cold_add(target="memory",
                            content="[2026-04-20 15:26] real user note that should NOT be touched")

    # 2. Run only the backfill UPDATE+INSERT (simulating 011 §3 idempotently)
    conn = storage._get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO dream_runs
          (id, started_at, finished_at, status, config_snapshot,
           candidates_count, clusters_count, consolidated_count, forgotten_count)
        VALUES ('test:d18:backfill',
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                'migration',
                '{"test":"d18"}', 0, 0, 0, 0)
    """)
    conn.execute("""
        UPDATE cold_memory
           SET dream_status = 'dormant',
               last_dream_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
         WHERE COALESCE(dream_status, 'active') = 'active'
           AND content LIKE '[snapshot %'
    """)
    conn.execute("""
        INSERT INTO dream_actions (dream_run_id, action, memory_id, details, created_at)
        SELECT 'test:d18:backfill', 'mark_dormant', cm.id,
               json_object('actor','migration','reason','legacy_test_residue',
                           'source_tier','cold','original_id', NULL),
               strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
          FROM cold_memory cm
         WHERE cm.dream_status = 'dormant'
           AND cm.content LIKE '[snapshot %'
           AND NOT EXISTS (
             SELECT 1 FROM dream_actions da
              WHERE da.dream_run_id = 'test:d18:backfill'
                AND da.memory_id    = cm.id
           )
    """)
    conn.commit()

    # 3. Assertions
    snap_row = conn.execute(
        "SELECT dream_status FROM cold_memory WHERE id=?", (snap["id"],)
    ).fetchone()
    real_row = conn.execute(
        "SELECT dream_status FROM cold_memory WHERE id=?", (real["id"],)
    ).fetchone()
    assert snap_row["dream_status"] == "dormant", "snapshot residue should be marked"
    assert real_row["dream_status"] == "active", \
        "[2026- prefix is real user content; must NOT be touched"

    audit = conn.execute(
        "SELECT details FROM dream_actions WHERE memory_id=? AND action='mark_dormant'",
        (snap["id"],)
    ).fetchone()
    assert audit is not None
    assert json.loads(audit["details"])["reason"] == "legacy_test_residue"


# ── D19: purge job retention enforcement ──
def test_d19_purge_respects_retention(storage):
    """fresh dormant rows must NOT be purged; legacy_test_residue (0d) IS."""
    from openhippo.core.dream import DreamEngine
    eng = DreamEngine(storage)

    # Row A: fresh cold_delete (retention=7d) → not eligible
    a = storage.cold_add(target="memory", content="d19 recent")
    storage.cold_delete(a["id"])

    # Row B: simulate legacy_test_residue (retention=0d) → eligible immediately
    b = storage.cold_add(target="memory", content="d19 legacy")
    conn = storage._get_conn()
    rid = "test:d19:backfill"
    conn.execute(
        """INSERT OR IGNORE INTO dream_runs (id, started_at, finished_at, status,
           config_snapshot, candidates_count, clusters_count, consolidated_count, forgotten_count)
           VALUES (?, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z', 'migration', '{}', 0,0,0,0)""",
        (rid,),
    )
    conn.execute(
        "UPDATE cold_memory SET dream_status='dormant', last_dream_at=? WHERE id=?",
        ("2026-01-01T00:00:00.000Z", b["id"]),
    )
    conn.execute(
        """INSERT INTO dream_actions (dream_run_id, action, memory_id, details, created_at)
           VALUES (?, 'mark_dormant', ?, ?, '2026-01-01T00:00:00.000Z')""",
        (rid, b["id"],
         json.dumps({"actor": "migration", "reason": "legacy_test_residue",
                     "source_tier": "cold"})),
    )
    conn.commit()

    # Dry run first
    dry = eng.purge_overdue_dormant(dry_run=True)
    assert dry["dry_run"] is True
    assert dry["eligible"] >= 1
    assert dry["purged"] == 0

    # Real run
    out = eng.purge_overdue_dormant()
    assert out["purged"] >= 1
    assert "legacy_test_residue" in out["by_reason"]

    # A still present, B gone
    assert conn.execute("SELECT 1 FROM cold_memory WHERE id=?", (a["id"],)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM cold_memory WHERE id=?", (b["id"],)).fetchone() is None
    # purge_dormant audit row exists
    assert conn.execute(
        "SELECT 1 FROM dream_actions WHERE memory_id=? AND action='purge_dormant'",
        (b["id"],),
    ).fetchone() is not None
