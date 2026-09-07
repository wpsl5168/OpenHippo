"""Retrieval closeout: synthetic data, real SQLite/FTS5/sqlite-vec, no TCP."""
from __future__ import annotations

import socket
import sqlite3
import time

import pytest
import sqlite_vec

from openhippo.core import engine as engine_module
from openhippo.core.embedding import EmbeddingVector
from openhippo.core.engine import HippoEngine
from openhippo.core.storage import Storage, atomic


@pytest.fixture(scope="session", autouse=True)
def _install_global_client():
    """Storage-only suite: override conftest's REST startup."""
    yield


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network forbidden in retrieval regressions")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(engine_module, "get_embedding", lambda text: vector())


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "retrieval.db")
    yield s
    c = s._get_conn()
    if c.in_transaction:
        c.rollback()
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not c.execute("PRAGMA foreign_key_check").fetchall()
    s.close()


def vector(distance=0.0):
    return EmbeddingVector([distance] + [0.0] * 767,
                           model="retrieval-synthetic", space_id="retrieval-synthetic-space")


def original_filtered_search(s, query_embedding, target=None, limit=20,
                             include_dormant=False, origin=None, *, scope=None,
                             agent_id=None, include_consolidated=False):
    """Frozen pre-optimization scalar SQL oracle (not the lossy KNN path)."""
    if limit <= 0:
        return []
    clauses = ["vec_distance <= ?"]
    params = [s._serialize_vec(query_embedding), s.VEC_DISTANCE_THRESHOLD]
    for column, value in (("target", target), ("source", origin), ("scope", scope)):
        if value:
            clauses.append(f"cm.{column} = ?")
            params.append(value)
    if agent_id == "__local__":
        clauses.append("cm.agent_id IS NULL")
    elif agent_id:
        clauses.append("cm.agent_id = ?")
        params.append(agent_id)
    if not include_dormant:
        clauses.append("COALESCE(cm.dream_status, 'active') != 'dormant'")
    if not include_consolidated:
        clauses.append("COALESCE(cm.dream_status, 'active') != 'consolidated'")
    params.append(limit)
    return [dict(r) for r in s._get_conn().execute(
        "SELECT cm.*, vec_distance_l2(v.embedding, ?) AS vec_distance "
        "FROM cold_memory_vec v CROSS JOIN cold_memory cm ON cm.id = v.memory_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY vec_distance, cm.id LIMIT ?", params)]


def populate(s, records):
    """Batch synthetic fixtures into all existing index/provenance tables."""
    c = s._get_conn()
    with atomic(c):
        for row in records:
            mid = row["id"]
            c.execute("""INSERT INTO cold_memory
                (id, target, content, source, scope, agent_id, dream_status)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                      (mid, row.get("target", "memory"), row.get("content", "synthetic needle " + mid),
                       row.get("source", "manual"), row.get("scope", "agent"),
                       row.get("agent_id"), row.get("dream_status", "active")))
            v = vector(row.get("distance", 0.0))
            blob = s._serialize_vec(v)
            c.execute("INSERT INTO cold_embeddings (memory_id, embedding, model) VALUES (?,?,?)",
                      (mid, blob, v.model))
            c.execute("INSERT INTO cold_memory_vec (memory_id, embedding) VALUES (?,?)", (mid, blob))
            c.execute("INSERT INTO embedding_spaces VALUES (?,?,?)", (mid, v.space_id, v.model))


def fts_shadow(c):
    return {name: [tuple(r) for r in c.execute(f'SELECT * FROM "{name}" ORDER BY 1')]
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                     "AND name GLOB 'cold_memory_fts_*'")}


def counts(s):
    return {r["id"]: r["access_count"] for r in s._get_conn().execute("SELECT * FROM cold_memory")}


def test_legacy_trigger_migrates_without_reindex_or_data_changes(store):
    populate(store, [{"id": "old"}])
    c = store._get_conn()
    c.executescript("""
        DROP TRIGGER cold_memory_au;
        CREATE TRIGGER cold_memory_au AFTER UPDATE ON cold_memory BEGIN
          INSERT INTO cold_memory_fts(cold_memory_fts,rowid,content,tags)
            VALUES('delete',old.rowid,old.content,old.tags);
          INSERT INTO cold_memory_fts(rowid,content,tags) VALUES(new.rowid,new.content,new.tags);
        END;
    """)
    before = fts_shadow(c)
    data = [tuple(r) for r in c.execute("SELECT * FROM cold_memory")]
    reopened = Storage(store.db_path)
    try:
        c2 = reopened._get_conn()
        trigger = c2.execute("SELECT sql FROM sqlite_master WHERE name='cold_memory_au'").fetchone()[0]
        assert "UPDATE OF content, tags" in trigger
        assert fts_shadow(c2) == before
        assert [tuple(r) for r in c2.execute("SELECT * FROM cold_memory")] == data
        assert reopened.cold_search("needle")[0]["id"] == "old"
        assert counts(reopened) == {"old": 1}
        assert fts_shadow(c2) == before
    finally:
        reopened.close()


def test_statistics_batch_dedup_and_no_fts_shadow_writes(store):
    populate(store, [{"id": f"id-{i:04}"} for i in range(1103)])
    c = store._get_conn()
    before = fts_shadow(c)
    writes = c.total_changes
    authorized = []
    def authorize(action, table, column, db, source):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_UPDATE):
            authorized.append((action, table, column))
        return sqlite3.SQLITE_OK
    c.set_authorizer(authorize)
    try:
        store.record_access([*counts(store), *counts(store)])
    finally:
        c.set_authorizer(None)
    assert set(counts(store).values()) == {1}
    assert c.total_changes - writes == 1103
    assert fts_shadow(c) == before
    assert not any(table.startswith("cold_memory_fts") for _, table, _ in authorized)
    assert sum(table == "cold_memory" and col == "access_count" for _, table, col in authorized) <= 3
    assert c.execute("SELECT count(DISTINCT last_accessed) FROM cold_memory").fetchone()[0] == 1


def test_content_and_tags_still_update_fts(store):
    populate(store, [{"id": "editable", "content": "oldword"}])
    c = store._get_conn()
    c.execute("UPDATE cold_memory SET content='newword', tags='[\"oldtag\"]' WHERE id='editable'")
    c.commit()
    assert store.cold_search("oldword", record_access=False) == []
    assert store.cold_search("newword", record_access=False)
    assert store.cold_search("oldtag", record_access=False)
    c.execute("UPDATE cold_memory SET tags='[\"newtag\"]' WHERE id='editable'")
    c.commit()
    assert store.cold_search("oldtag", record_access=False) == []
    assert store.cold_search("newtag", record_access=False)
    shadow = fts_shadow(c)
    c.execute("UPDATE cold_memory SET content=content, tags=tags, updated_at=123 WHERE id='editable'")
    c.commit()
    assert fts_shadow(c) == shadow
    c.execute("INSERT INTO cold_memory_fts(cold_memory_fts, rank) VALUES('integrity-check', 1)")
    c.commit()


@pytest.mark.parametrize("mode", ["fts", "hybrid", "vector"])
def test_contended_statistics_do_not_fail_or_delay_read(store, mode):
    populate(store, [{"id": "visible"}])
    e = HippoEngine.__new__(HippoEngine)
    e.storage = store
    c = store._get_conn()
    timeout = c.execute("PRAGMA busy_timeout").fetchone()[0]
    writer = sqlite3.connect(str(store.db_path))
    writer.execute("BEGIN IMMEDIATE")
    try:
        start = time.monotonic()
        result = e.search("needle", source="cold", mode=mode)
        elapsed = time.monotonic() - start
        assert result["cold"][0]["id"] == "visible"
        assert elapsed < 1.0  # default busy timeout is 5 seconds
        assert counts(store) == {"visible": 0}
        assert not c.in_transaction
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == timeout
    finally:
        writer.rollback()
        writer.close()
    assert e.search("needle", source="cold", mode=mode)["cold"]
    assert counts(store) == {"visible": 1}


@pytest.mark.parametrize("mode", ["fts", "hybrid", "vector"])
def test_read_does_not_commit_or_rollback_caller_transaction(store, mode):
    populate(store, [{"id": "visible"}])
    c = store._get_conn()
    c.execute("UPDATE cold_memory SET metadata='{\"uncommitted\":true}' WHERE id='visible'")
    e = HippoEngine.__new__(HippoEngine)
    e.storage = store
    assert e.search("needle", source="cold", mode=mode)["cold"]
    assert c.in_transaction
    assert counts(store) == {"visible": 0}
    observer = sqlite3.connect(str(store.db_path))
    try:
        assert observer.execute("SELECT metadata FROM cold_memory").fetchone()[0] == "{}"
    finally:
        observer.close()
    c.rollback()
    assert store.cold_get("visible")["metadata"] == "{}"


def test_failed_batch_rolls_back_all_statistics(store):
    populate(store, [{"id": f"id-{i:04}"} for i in range(501)])
    c = store._get_conn()
    c.execute("""CREATE TRIGGER synthetic_stats_failure BEFORE UPDATE OF access_count ON cold_memory
              WHEN new.id = 'id-0500' BEGIN SELECT RAISE(ABORT, 'synthetic telemetry failure'); END""")
    before = fts_shadow(c)
    assert len(store.cold_search("needle", limit=501)) == 501
    assert set(counts(store).values()) == {0}
    assert fts_shadow(c) == before
    assert not c.in_transaction


def test_hybrid_counts_only_final_results_once(store):
    populate(store, [
        {"id": "a", "content": "needle needle needle", "distance": 0.75},
        {"id": "b", "content": "needle", "distance": 0.05},
        {"id": "c", "content": "haystack", "distance": 0.0},
    ])
    assert [r["id"] for r in store.cold_search("needle", limit=2, record_access=False)] == ["a", "b"]
    assert [r["id"] for r in store.vec_search(vector(), limit=2)] == ["c", "b"]
    e = HippoEngine.__new__(HippoEngine)
    e.storage = store
    result = e.search("needle", source="cold", mode="hybrid", limit=2)["cold"]
    assert [r["id"] for r in result] == ["b", "a"]
    assert result[0]["rrf_score"] > result[1]["rrf_score"]
    assert counts(store) == {"a": 1, "b": 1, "c": 0}


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_fallback_counts_once(store, monkeypatch, mode):
    populate(store, [{"id": "fallback"}])
    monkeypatch.setattr(engine_module, "get_embedding", lambda text: None)
    e = HippoEngine.__new__(HippoEngine)
    e.storage = store
    assert e.search("needle", source="cold", mode=mode)["cold"]
    assert counts(store) == {"fallback": 1}


@pytest.mark.parametrize("field,blocker,wanted,filter_arg", [
    ("dream_status", "dormant", "active", {}),
    ("dream_status", "consolidated", "active", {}),
    ("target", "user", "memory", {"target": "memory"}),
    ("source", "manual", "session_summary", {"origin": "session_summary"}),
    ("scope", "global", "session", {"scope": "session"}),
    ("agent_id", "other", "wanted", {"agent_id": "wanted"}),
    ("agent_id", "other", None, {"agent_id": "__local__"}),
])
def test_filters_precede_limit_when_nearest_candidates_are_blocked(store, field, blocker, wanted, filter_arg):
    populate(store, [{"id": f"blocked-{i:03}", field: blocker} for i in range(96)] +
             [{"id": "wanted", field: wanted, "distance": 0.5}])
    result = store.vec_search(vector(), limit=1, **filter_arg)
    assert [r["id"] for r in result] == ["wanted"]
    assert result == original_filtered_search(store, vector(), limit=1, **filter_arg)


def test_scope_is_optional_exact_filter_not_new_visibility_policy(store):
    populate(store, [
        {"id": "a", "scope": "agent", "agent_id": "mine"},
        {"id": "b", "scope": "session", "agent_id": "other"},
        {"id": "c", "scope": "global", "agent_id": None},
    ])
    assert [r["id"] for r in store.vec_search(vector())] == ["a", "b", "c"]
    assert [r["id"] for r in store.vec_search(vector(), agent_id="mine")] == ["a"]
    assert [r["id"] for r in store.vec_search(vector(), scope="global")] == ["c"]
    assert store.vec_search(vector(), scope="global", agent_id="mine") == []
    assert store.vec_search(vector(), scope="global' OR 1=1 --") == []


def test_real_sqlite_vec_knn_ceiling_and_join_postfilter(store):
    populate(store, [{"id": "blocked", "dream_status": "dormant"},
                     {"id": "active", "distance": 0.5}])
    c = store._get_conn()
    blob = store._serialize_vec(vector())
    assert len(c.execute("SELECT memory_id FROM cold_memory_vec WHERE embedding MATCH ? AND k=4096",
                         (blob,)).fetchall()) == 2
    with pytest.raises(sqlite3.OperationalError, match="k value.*too large.*4096"):
        c.execute("SELECT memory_id FROM cold_memory_vec WHERE embedding MATCH ? AND k=4097", (blob,)).fetchall()
    assert c.execute("""SELECT cm.id FROM cold_memory_vec v JOIN cold_memory cm ON cm.id=v.memory_id
        WHERE v.embedding MATCH ? AND k=1 AND cm.dream_status='active'""", (blob,)).fetchall() == []
    assert [r["id"] for r in store.vec_search(vector(), limit=1)] == ["active"]
    print(f"\nREAL_SQLITE_VEC version={sqlite_vec.__version__} knn_k_max=4096 join_postfilter_reproduced=True")


def test_beyond_knn_ceiling_no_silent_omission_and_large_limit(store):
    n = 4105
    populate(store, [{"id": f"blocked-{i:05}", "dream_status": "dormant", "agent_id": "other"}
                     for i in range(n)] +
             [{"id": f"wanted-{i}", "distance": 0.5 + i / 10, "agent_id": "wanted"}
              for i in range(3)])
    assert [r["id"] for r in store.vec_search(vector(), limit=3, agent_id="wanted")] == [
        "wanted-0", "wanted-1", "wanted-2"]
    c = store._get_conn()
    # Python 3.11 exposes setlimit; keep this regression runnable on supported
    # Python 3.10 too, and check batch sizes through the SQL trace on both.
    old_limit = c.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999) if hasattr(c, "setlimit") else None
    statements = []
    c.set_trace_callback(statements.append)
    try:
        result = store.vec_search(vector(), limit=n + 3, include_dormant=True)
        assert len(result) == n + 3
        assert result == original_filtered_search(store, vector(), limit=n + 3, include_dormant=True)
        assert store.vec_search(vector(), limit=n + 100, include_dormant=True) == result
        batches = [sql for sql in statements if sql.startswith("SELECT * FROM cold_memory WHERE id IN")]
        assert batches and all(sql.count(",") < 500 for sql in batches)
    finally:
        c.set_trace_callback(None)
        if old_limit is not None:
            c.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)


def test_distance_threshold_limit_and_stable_tie_order(store):
    assert store.vec_search(vector()) == []
    populate(store, [
        {"id": "z", "distance": 0.25}, {"id": "a", "distance": 0.25},
        {"id": "boundary", "distance": 1.0}, {"id": "far", "distance": 1.0001},
        {"id": "null-status", "distance": 0.5, "dream_status": None},
    ])
    result = store.vec_search(vector(), limit=20)
    assert [r["id"] for r in result] == ["a", "z", "null-status", "boundary"]
    assert [r["vec_distance"] for r in result] == [0.25, 0.25, 0.5, 1.0]
    assert [r["id"] for r in store.vec_search(vector(), limit=1)] == ["a"]
    assert store.vec_search(vector(), limit=0) == []
    assert store.vec_search(vector(), limit=-1) == []


def test_status_audit_opt_ins_are_independent(store):
    populate(store, [{"id": "dormant", "dream_status": "dormant"},
                     {"id": "consolidated", "dream_status": "consolidated"}, {"id": "active"}])
    assert [r["id"] for r in store.vec_search(vector())] == ["active"]
    assert [r["id"] for r in store.vec_search(vector(), include_dormant=True)] == ["active", "dormant"]
    assert [r["id"] for r in store.vec_search(vector(), include_consolidated=True)] == ["active", "consolidated"]


def test_vec_plan_scans_virtual_table_once_then_metadata_primary_key(store):
    populate(store, [{"id": "plan"}])
    c = store._get_conn()
    statements = []
    c.set_trace_callback(statements.append)
    try:
        assert store.vec_search(vector(), scope="agent", limit=4097)
    finally:
        c.set_trace_callback(None)
    sql = next(s for s in statements if s.startswith("SELECT cm.id, vec_distance_l2"))
    assert "cm.*" not in sql
    assert "vec_distance <=" not in sql
    bytecode = c.execute("EXPLAIN " + sql).fetchall()
    assert sum("vec_distance_l2" in str(r[5]) for r in bytecode) == 1
    plan = [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql)]
    assert "SCAN v VIRTUAL TABLE" in plan[0]
    assert any("SEARCH cm USING INDEX sqlite_autoindex_cold_memory_1" in p for p in plan)
    assert not any("SEARCH v" in p for p in plan)
    assert set(counts(store).values()) == {0}  # raw vector API is still read-only


@pytest.mark.parametrize("threshold", [-1.0, 0.0, 0.5, 1.0, 2.0])
@pytest.mark.parametrize("filters", [
    {},
    {"include_dormant": True, "include_consolidated": True},
    {"target": "memory", "origin": "session_summary", "scope": "session", "agent_id": "wanted"},
    {"target": "user", "scope": "global", "agent_id": "__local__", "include_dormant": True},
])
def test_optimized_full_rows_equal_original_with_threshold_and_filter_combinations(store, threshold, filters):
    # Deterministic cross-product, including NULL status, ties and rejected rows
    # that are closer than legal hits. Equality includes every field and order.
    import itertools
    records = []
    combinations = itertools.product(
        [None, "active", "dormant", "consolidated"], ["memory", "user"],
        ["manual", "session_summary"], ["session", "global"], [None, "wanted"])
    for i, (status, target, source, scope, agent) in enumerate(combinations):
        for j, distance in enumerate([0.0, 0.5, 1.0, 1.0001, 2.0]):
            records.append({"id": f"row-{i:03}-{j}", "dream_status": status,
                            "target": target, "source": source, "scope": scope,
                            "agent_id": agent, "distance": distance,
                            "content": "synthetic long body " * 128})
    populate(store, reversed(records))
    store.VEC_DISTANCE_THRESHOLD = threshold
    for limit in (0, 1, 7, 1000, 4097):
        assert store.vec_search(vector(), limit=limit, **filters) == original_filtered_search(
            store, vector(), limit=limit, **filters)


def test_distance_evaluated_once_only_after_metadata_filters(store):
    import struct
    populate(store, [
        {"id": "d", "dream_status": "dormant"},
        {"id": "c", "dream_status": "consolidated"},
        {"id": "t", "target": "user"}, {"id": "o", "source": "other"},
        {"id": "s", "scope": "global"}, {"id": "a", "agent_id": "other"},
        *[{"id": f"hit-{i}", "agent_id": "wanted", "distance": i / 2}
          for i in range(5)],
    ])
    # Make the other blocker fields eligible to isolate each rejection reason.
    c = store._get_conn()
    c.execute("UPDATE cold_memory SET agent_id='wanted' WHERE id IN ('d','c','t','o','s')")
    c.commit()
    calls = []
    def counted_distance(blob, query):
        calls.append(1)
        return abs(struct.unpack_from('<f', blob)[0] - struct.unpack_from('<f', query)[0])
    # Counting scalar substitutes only for this synthetic single-axis test;
    # equivalence and EXPLAIN tests above execute the native sqlite-vec scalar.
    c.create_function("vec_distance_l2", 2, counted_distance)
    result = store.vec_search(vector(), limit=4097, target="memory", origin="manual",
                              scope="agent", agent_id="wanted")
    assert [r["id"] for r in result] == ["hit-0", "hit-1", "hit-2"]
    assert len(calls) == 5


def test_threshold_rejected_rows_are_not_hydrated(store):
    populate(store, [{"id": "near", "distance": 0.5}, {"id": "far", "distance": 2.0}])
    c = store._get_conn()
    statements = []
    c.set_trace_callback(statements.append)
    try:
        assert [r["id"] for r in store.vec_search(vector(), limit=20)] == ["near"]
    finally:
        c.set_trace_callback(None)
    reads = [sql for sql in statements if sql.startswith("SELECT * FROM cold_memory WHERE id IN")]
    assert len(reads) == 1
    assert "'near'" in reads[0] and "'far'" not in reads[0]
    assert any(sql.startswith("BEGIN") for sql in statements)
    assert statements.index(reads[0]) < next(i for i, sql in enumerate(statements) if sql == "COMMIT")


def test_hydration_keeps_distance_scan_snapshot_during_concurrent_update(store):
    populate(store, [{"id": "snapshot", "content": "old synthetic body"}])
    c = store._get_conn()
    writer = sqlite3.connect(str(store.db_path))
    events = []
    def before_hydration(sql):
        if sql.startswith("SELECT * FROM cold_memory WHERE id IN"):
            writer.execute("UPDATE cold_memory SET content='new synthetic body' WHERE id='snapshot'")
            writer.commit()
            events.append("committed")
    c.set_trace_callback(before_hydration)
    try:
        result = store.vec_search(vector(), limit=1)
        assert events == ["committed"]
        assert result[0]["content"] == "old synthetic body"
    finally:
        c.set_trace_callback(None)
        writer.close()
    assert store.cold_get("snapshot")["content"] == "new synthetic body"


@pytest.mark.parametrize("size", [97, 4105])
def test_knn_tied_boundary_expands_or_falls_back_without_losing_stable_order(store, size):
    # Reverse insertion makes native KNN's arbitrary tied prefix unsuitable for
    # ID tie-breaking. At >4096 we MUST fall back, even though limit=1 is filled.
    populate(store, [{"id": f"tied-{i:05}", "distance": 0.5} for i in reversed(range(size))])
    c = store._get_conn()
    statements = []
    c.set_trace_callback(statements.append)
    try:
        result = store.vec_search(vector(), limit=1)
    finally:
        c.set_trace_callback(None)
    assert result == original_filtered_search(store, vector(), limit=1)
    assert result[0]["id"] == "tied-00000"
    knn = [sql for sql in statements if sql.startswith("SELECT memory_id, distance FROM cold_memory_vec")]
    assert len(knn) > 1
    scalar = [sql for sql in statements if sql.startswith("SELECT cm.id, vec_distance_l2")]
    assert bool(scalar) == (size > 4096)


def test_knn_fast_path_stops_only_with_proven_boundary_and_skips_scalar(store):
    populate(store, [{"id": f"rank-{i:03}", "distance": i / 100} for i in range(100)])
    c = store._get_conn()
    statements = []
    c.set_trace_callback(statements.append)
    try:
        result = store.vec_search(vector(), limit=10)
    finally:
        c.set_trace_callback(None)
    assert result == original_filtered_search(store, vector(), limit=10)
    assert len([sql for sql in statements if sql.startswith("SELECT memory_id, distance FROM cold_memory_vec")]) == 1
    assert not any(sql.startswith("SELECT cm.id, vec_distance_l2") for sql in statements)


def test_knn_threshold_boundary_does_not_stop_at_an_equal_distance(store):
    populate(store, [{"id": f"edge-{i:03}", "distance": 1.0} for i in reversed(range(97))] +
             [{"id": "outside", "distance": 1.01}])
    result = store.vec_search(vector(), limit=60)
    assert len(result) == 60
    assert result == original_filtered_search(store, vector(), limit=60)
