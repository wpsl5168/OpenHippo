"""Isolated vector safety regression suite (real SQLite + sqlite-vec).

Run with the audit venv: python tests/test_vector_safety.py [other test files]
The standalone runner disables conftest/autouse REST startup, redirects HOME,
HERMES_HOME and all temporary DBs, blocks TCP and installs only fake providers.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

if __name__ == "__main__":
    _sandbox = tempfile.TemporaryDirectory(prefix=".vector-safety-", dir=Path(__file__).resolve().parents[1])
    for key in list(os.environ):
        if key.startswith(("HIPPO_", "OPENHIPPO_", "COPILOT_")):
            os.environ.pop(key)
    os.environ.update(HOME=_sandbox.name, HERMES_HOME=_sandbox.name + "/hermes",
                      TMPDIR=_sandbox.name, HIPPO_DB_PATH=_sandbox.name + "/default.db",
                      OPENHIPPO_DREAM_AUTO="0", OPENHIPPO_ASYNC_EMBED="0")
    tempfile.tempdir = _sandbox.name
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex


def _no_tcp(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        raise RuntimeError("TCP disabled in isolated vector safety tests")
    return _original_connect(sock, address)


def _no_tcp_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        raise RuntimeError("TCP disabled in isolated vector safety tests")
    return _original_connect_ex(sock, address)


socket.socket.connect = _no_tcp
socket.socket.connect_ex = _no_tcp_ex

import pytest
from openhippo.core import embedding as emb, embed_queue as queue
from openhippo.core.engine import HippoEngine
from openhippo.core.storage import Storage, VectorSpaceError, atomic


class FakeProvider(emb.EmbeddingProvider):
    model = "FAKE-model-a"
    base_url = "https://fake-a.invalid"
    calls = 0

    @property
    def dimension(self):
        return 768

    def embed(self, text):
        self.calls += 1
        values = [0.0] * self.dimension
        values[sum(text.encode()) % self.dimension] = 1.0
        return values


@pytest.fixture(autouse=True)
def isolated_provider(monkeypatch):
    previous = emb._provider
    emb.set_provider(FakeProvider())
    emb.cache_clear()
    monkeypatch.setenv("OPENHIPPO_ASYNC_EMBED", "0")
    yield
    emb.set_provider(previous)
    emb.cache_clear()


@pytest.fixture
def engine(tmp_path):
    e = HippoEngine(tmp_path / "vectors.db")
    yield e
    assert e.storage._get_conn().execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not e.storage._get_conn().execute("PRAGMA foreign_key_check").fetchall()
    e.close()


def snapshot(storage):
    conn = storage._get_conn()
    return {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("cold_memory", "cold_embeddings", "cold_memory_vec", "embedding_spaces")}


def add_vector(engine, text="old"):
    result = engine.cold_add("memory", text)
    assert result["embedding_status"] == "sync"
    return result["id"]


BAD_VECTORS = [[], [0.0] * 767, [0.0] * 769,
               [float("nan")] * 768, [float("inf")] * 768,
               [-float("inf")] * 768, [1e100] * 768,
               ["1"] * 768, [None] * 768, [True] * 768]


@pytest.mark.parametrize("bad", BAD_VECTORS)
def test_bad_vector_rejected_before_write(engine, bad):
    mid = add_vector(engine)
    before = snapshot(engine.storage)
    with pytest.raises(ValueError):
        engine.storage.vec_store(mid, bad, model="fake", space_id="invalid")
    engine.storage._get_conn().commit()  # unrelated future commit cannot leak changes
    assert snapshot(engine.storage) == before


@pytest.mark.parametrize("bad", BAD_VECTORS)
def test_invalid_provider_outputs_never_cached(monkeypatch, bad):
    provider = FakeProvider()
    monkeypatch.setattr(provider, "embed", lambda text: bad)
    emb.set_provider(provider)
    assert emb.get_embedding("one") is None
    assert emb.get_embeddings_batch(["two", "three"]) == [None, None]
    assert emb.cache_stats()["size"] == 0


def test_cache_provider_model_endpoint_dimension_and_copy_isolation():
    first = emb.get_embedding("same")
    provider = emb.get_provider()
    again = emb.get_embedding("same")
    assert again == first and provider.calls == 1
    again[0] = 19
    assert emb.get_embedding("same") == first
    for attr, value in (("model", "FAKE-model-b"), ("base_url", "https://fake-b.invalid")):
        other = FakeProvider()
        setattr(other, attr, value)
        emb.set_provider(other)
        vector = emb.get_embedding("same")
        assert other.calls == 1 and vector.space_id != first.space_id
    class OtherProvider(FakeProvider):
        pass
    other = OtherProvider()
    emb.set_provider(other)
    assert emb.get_embedding("same").space_id != first.space_id
    class SmallProvider(FakeProvider):
        @property
        def dimension(self):
            return 3
    other = SmallProvider()
    emb.set_provider(other)
    assert len(emb.get_embedding("same")) == 3


def test_inflight_provider_switch_keeps_original_provenance(monkeypatch):
    original = emb.get_provider()
    other = FakeProvider()
    other.model = "FAKE-model-b"
    original_embed = original.embed
    def switching(text):
        emb.set_provider(other)
        return original_embed(text)
    monkeypatch.setattr(original, "embed", switching)
    vector = emb.get_embedding("race")
    assert vector.model == original.model
    assert vector.space_id == emb.provider_identity(original)[1]
    assert emb.get_embedding("race").model == other.model


def test_batch_switch_and_bad_cardinality(monkeypatch):
    original = emb.get_provider()
    other = FakeProvider()
    other.model = "FAKE-other"
    def batch(texts):
        emb.set_provider(other)
        return [original.embed(t) for t in texts]
    monkeypatch.setattr(original, "embed_batch", batch)
    vectors = emb.get_embeddings_batch(["a", "b"])
    assert all(v.model == original.model for v in vectors)
    emb.cache_clear()
    monkeypatch.setattr(other, "embed_batch", lambda texts: [[0.0] * 768])
    assert emb.get_embeddings_batch(["a", "b"]) == [None, None]
    assert emb.cache_stats()["size"] == 0


@pytest.mark.parametrize("change", ["model", "base_url", "provider", "legacy", "raw"])
def test_space_gate_blocks_mixing_and_wrong_queries(engine, change):
    mid = add_vector(engine)
    s = engine.storage
    first = emb.get_embedding("old")
    assert s.vec_search(first)[0]["id"] == mid
    if change == "legacy":
        s._get_conn().execute("DELETE FROM embedding_spaces")
        s._get_conn().commit()
    elif change == "raw":
        first = list(first)
    else:
        if change == "provider":
            class Another(FakeProvider):
                pass
            other = Another()
        else:
            other = FakeProvider()
            setattr(other, change, "different")
        emb.set_provider(other)
        first = emb.get_embedding("old")
    before = snapshot(s)
    assert s.vec_search(first) == []
    with pytest.raises(VectorSpaceError):
        s.vec_store(mid, first)
    assert snapshot(s) == before
    assert engine.search("old", mode="hybrid")["cold"][0]["id"] == mid


def test_explicit_wrong_model_cannot_relabel(engine):
    mid = add_vector(engine)
    before = snapshot(engine.storage)
    with pytest.raises(VectorSpaceError):
        engine.storage.vec_store(mid, emb.get_embedding("old"), model="invented")
    assert snapshot(engine.storage) == before


@pytest.mark.parametrize("stage", ["vec_insert", "provenance"])
def test_vec_store_rolls_back_after_partial_writes(engine, stage):
    mid = add_vector(engine)
    s = engine.storage
    conn = s._get_conn()
    before = snapshot(s)
    if stage == "vec_insert":
        def authorize(action, table, *args):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "cold_memory_vec" else sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
    else:
        conn.execute("CREATE TRIGGER fail_space BEFORE INSERT ON embedding_spaces BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.DatabaseError):
        s.vec_store(mid, emb.get_embedding("new"))
    conn.set_authorizer(None)
    assert not conn.in_transaction
    s.hot_add("memory", "unrelated commit")
    assert snapshot(s) == before


def test_savepoint_never_commits_caller_work(engine):
    mid = add_vector(engine)
    s = engine.storage
    before = snapshot(s)
    conn = s._get_conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE cold_memory SET source='uncommitted' WHERE id=?", (mid,))
    s.vec_store(mid, emb.get_embedding("new"))
    assert conn.in_transaction
    other = s._make_conn()
    assert other.execute("SELECT source FROM cold_memory WHERE id=?", (mid,)).fetchone()[0] == "manual"
    other.close()
    conn.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("failure", ["provider", "dimension", "space", "sql"])
def test_cold_update_failure_preserves_content_fts_vector_and_jobs(engine, monkeypatch, failure):
    mid = add_vector(engine)
    s = engine.storage
    conn = s._get_conn()
    job_id = queue.enqueue(conn, "cold_memory", mid, "old")
    before = snapshot(s)
    if failure == "provider":
        monkeypatch.setattr(emb.get_provider(), "embed", lambda text: None)
    elif failure == "dimension":
        monkeypatch.setattr(emb.get_provider(), "embed", lambda text: [1.0])
    elif failure == "space":
        other = FakeProvider()
        other.model = "other"
        emb.set_provider(other)
    else:
        conn.execute("CREATE TRIGGER fail_space BEFORE INSERT ON embedding_spaces BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        s.cold_update(mid, "new")
    conn.commit()
    assert snapshot(s) == before
    assert conn.execute("SELECT status FROM embedding_jobs WHERE id=?", (job_id,)).fetchone()[0] == "pending"
    assert conn.execute("SELECT count(*) FROM cold_memory_fts WHERE cold_memory_fts MATCH 'old'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM cold_memory_fts WHERE cold_memory_fts MATCH 'new'").fetchone()[0] == 0


@pytest.mark.parametrize("replacement", ["new", "old"])
def test_old_running_job_cannot_overwrite_cold_update_even_same_text(engine, replacement):
    mid = add_vector(engine)
    s = engine.storage
    queue.enqueue(s._get_conn(), "cold_memory", mid, "old")
    job = queue.fetch_one_pending(s._get_conn())
    stale_vec = emb.get_embedding("old")
    version = s.cold_get(mid)["updated_at"]
    s.cold_update(mid, replacement)
    assert s.cold_get(mid)["updated_at"] > version
    before = snapshot(s)
    assert queue.complete_job(s, job, stale_vec) is False
    queue.mark_failed(s._get_conn(), job["id"], "late failure")
    assert snapshot(s) == before
    assert s._get_conn().execute("SELECT status FROM embedding_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "done"


@pytest.mark.parametrize("mutation", ["content", "version", "delete", "legacy", "table", "newer"])
def test_job_rechecks_durable_target_before_write(engine, mutation):
    s = engine.storage
    mid = s.cold_add("memory", "old")["id"]
    conn = s._get_conn()
    queue.enqueue(conn, "cold_memory", mid, "old")
    job = queue.fetch_one_pending(conn)
    if mutation == "content":
        conn.execute("UPDATE cold_memory SET content='new' WHERE id=?", (mid,))
    elif mutation == "version":
        conn.execute("UPDATE cold_memory SET updated_at=updated_at+1 WHERE id=?", (mid,))
    elif mutation == "delete":
        conn.execute("DELETE FROM cold_memory WHERE id=?", (mid,))
    elif mutation == "legacy":
        conn.execute("DELETE FROM embedding_job_versions")
    elif mutation == "table":
        conn.execute("UPDATE embedding_jobs SET target_table='hot_memory' WHERE id=?", (job["id"],))
    else:
        queue.enqueue(conn, "cold_memory", mid, "old")
    conn.commit()
    assert queue.complete_job(s, job, emb.get_embedding("old")) is False
    assert s.vec_count() == 0


def test_job_vector_and_done_status_rollback_together(engine):
    s = engine.storage
    mid = s.cold_add("memory", "old")["id"]
    conn = s._get_conn()
    queue.enqueue(conn, "cold_memory", mid, "old")
    job = queue.fetch_one_pending(conn)
    conn.execute("CREATE TRIGGER fail_done BEFORE UPDATE ON embedding_jobs WHEN new.status='done' BEGIN SELECT RAISE(ABORT, 'done failed'); END")
    with pytest.raises(sqlite3.DatabaseError):
        queue.complete_job(s, job, emb.get_embedding("old"))
    queue.mark_failed(conn, job["id"], "injected")
    assert s.vec_count() == 0
    assert conn.execute("SELECT count(*) FROM cold_memory_vec").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM embedding_spaces").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM embedding_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "failed"


def test_job_check_and_write_hold_writer_lock(engine, monkeypatch):
    s = engine.storage
    mid = s.cold_add("memory", "old")["id"]
    queue.enqueue(s._get_conn(), "cold_memory", mid, "old")
    job = queue.fetch_one_pending(s._get_conn())
    original = s._entry_matches
    attempts = []
    def checked(memory_id, expected):
        assert s._get_conn().in_transaction
        result = original(memory_id, expected)
        def competitor():
            conn = s._make_conn()
            conn.execute("PRAGMA busy_timeout=1")
            try:
                conn.execute("UPDATE cold_memory SET content='raced' WHERE id=?", (mid,))
                conn.commit()
                attempts.append("wrote")
            except sqlite3.OperationalError as exc:
                attempts.append(str(exc))
            finally:
                conn.close()
        thread = threading.Thread(target=competitor)
        thread.start()
        thread.join(timeout=3)
        assert not thread.is_alive()
        return result
    monkeypatch.setattr(s, "_entry_matches", checked)
    assert queue.complete_job(s, job, emb.get_embedding("old"))
    assert attempts and all("locked" in x for x in attempts)
    assert s.cold_get(mid)["content"] == "old"


def test_query_gate_and_scan_share_snapshot(engine, monkeypatch):
    mid = add_vector(engine)
    s = engine.storage
    original = s._check_vector_space
    def check(conn, model, space):
        assert conn.in_transaction
        original(conn, model, space)
        other = s._make_conn()
        other.execute("DELETE FROM embedding_spaces")
        other.commit()
        other.close()
    monkeypatch.setattr(s, "_check_vector_space", check)
    # Same snapshot still has the verified corpus; next read must fail closed.
    assert s.vec_search(emb.get_embedding("old"))[0]["id"] == mid
    monkeypatch.setattr(s, "_check_vector_space", original)
    assert s.vec_search(emb.get_embedding("old")) == []


def test_sync_paths_and_queue_record_real_model(engine, monkeypatch):
    s = engine.storage
    ids = [add_vector(engine, "sync")]
    raw = s.cold_add("memory", "backfill")["id"]
    assert engine.embed_all_cold()["embedded"] == 1
    ids.append(raw)
    s.hot_add("memory", "archive")
    ids.append(engine.archive("memory", "archive")["cold_id"])
    engine.cold_update(raw, "updated")
    monkeypatch.setenv("OPENHIPPO_ASYNC_EMBED", "1")
    ids.append(engine.cold_add("memory", "drain")["id"])
    assert engine.embed_drain_now() == {"done": 1, "failed": 0, "stale": 0}
    conn = s._get_conn()
    for mid in ids:
        assert conn.execute("SELECT model FROM cold_embeddings WHERE memory_id=?", (mid,)).fetchone()[0] == FakeProvider.model
        assert conn.execute("SELECT space_id FROM embedding_spaces WHERE memory_id=?", (mid,)).fetchone()[0] == emb.provider_identity(emb.get_provider())[1]


def test_sync_snapshot_cannot_overwrite_concurrent_update(engine, monkeypatch):
    mid = add_vector(engine)
    s = engine.storage
    original_embed = emb.get_provider().embed
    emb.cache_clear()
    def changed(text):
        other = s._make_conn()
        other.execute("UPDATE cold_memory SET content='changed', updated_at=updated_at+1 WHERE id=?", (mid,))
        other.commit()
        other.close()
        return original_embed(text)
    before = s._get_conn().execute("SELECT embedding FROM cold_embeddings WHERE memory_id=?", (mid,)).fetchone()[0]
    monkeypatch.setattr(emb.get_provider(), "embed", changed)
    assert engine._embed_cold_entry(mid) is False
    assert s._get_conn().execute("SELECT embedding FROM cold_embeddings WHERE memory_id=?", (mid,)).fetchone()[0] == before


def test_async_worker_uses_same_atomic_completion(engine, monkeypatch):
    monkeypatch.setenv("OPENHIPPO_ASYNC_EMBED", "1")
    mid = engine.cold_add("memory", "worker")["id"]
    async def run():
        stop = asyncio.Event()
        original = queue.complete_job
        def complete(storage, job, vec):
            result = original(storage, job, vec)
            stop.set()
            return result
        monkeypatch.setattr(queue, "complete_job", complete)
        await asyncio.wait_for(queue.embedding_worker(engine, stop), timeout=3)
    asyncio.run(run())
    assert engine.storage._get_conn().execute("SELECT model FROM cold_embeddings WHERE memory_id=?", (mid,)).fetchone()[0] == FakeProvider.model


@pytest.mark.parametrize("bad", [[0.0] * 3, [float("nan")] * 768, [float("inf")] * 768])
def test_real_http_providers_reject_bad_response(monkeypatch, bad):
    import io
    import json
    def response(req, **kwargs):
        if req.full_url.endswith("/embeddings") and not req.full_url.endswith("/api/embeddings"):
            return io.BytesIO(json.dumps({"data": [{"index": 0, "embedding": bad}]}).encode())
        return io.BytesIO(json.dumps({"embedding": bad}).encode())
    monkeypatch.setattr(emb.urllib.request, "urlopen", response)
    assert emb.CopilotProvider(token="FAKE-NOT-A-CREDENTIAL").embed("text") is None
    assert emb.OllamaProvider().embed("text") is None


@pytest.mark.parametrize("indices", [[1], [0, 0], [0, 2]])
def test_copilot_batch_cannot_shift_content_alignment(monkeypatch, indices):
    import io
    import json
    monkeypatch.setattr(emb.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps({
        "data": [{"index": i, "embedding": [0.0] * 768} for i in indices]
    }).encode()))
    assert emb.CopilotProvider(token="FAKE-NOT-A-CREDENTIAL").embed_batch(["a", "b"]) == [None, None]


def test_local_provider_model_name_is_preserved(engine, monkeypatch):
    class Array(list):
        def tolist(self):
            return list(self)
    class Model:
        def encode(self, text, **kwargs):
            if isinstance(text, list):
                return [Array([0.0] * 768) for t in text]
            return Array([0.0] * 768)
    provider = emb.SentenceTransformerProvider(model_name="FAKE-local")
    monkeypatch.setattr(provider, "_get_model", lambda: Model())
    emb.set_provider(provider)
    mid = add_vector(engine)
    assert engine.storage._get_conn().execute("SELECT model FROM cold_embeddings WHERE memory_id=?", (mid,)).fetchone()[0] == "FAKE-local"
    assert emb.get_embeddings_batch(["a", "b"])[0].model == "FAKE-local"


def test_provider_switch_gates_every_sync_route_and_drain(engine, monkeypatch):
    old_id = add_vector(engine)
    s = engine.storage
    before_blob = s._get_conn().execute("SELECT embedding FROM cold_embeddings WHERE memory_id=?", (old_id,)).fetchone()[0]
    other = FakeProvider()
    other.model = "FAKE-new-space"
    emb.set_provider(other)
    assert engine.cold_add("memory", "new sync")["embedding_status"] == "failed"
    assert engine.embed_all_cold()["failed"] == 1
    s.hot_add("memory", "new archive")
    archived = engine.archive("memory", "new archive")["cold_id"]
    assert not s._get_conn().execute("SELECT 1 FROM cold_embeddings WHERE memory_id=?", (archived,)).fetchone()
    with pytest.raises(VectorSpaceError):
        engine.cold_update(old_id, "must not commit")
    assert engine.search("old", mode="vector")["cold"] == []
    # Hot dedup cannot compare an old-space query with new-space candidates.
    old_vector = emb.EmbeddingVector([0.0] * 768, model=FakeProvider.model, space_id="old-space")
    assert engine._semantic_match_hot("memory", old_vector) is None
    monkeypatch.setenv("OPENHIPPO_ASYNC_EMBED", "1")
    queued = engine.cold_add("memory", "new queue")["id"]
    counts = engine.embed_drain_now(max_jobs=1)
    assert counts == {"done": 0, "failed": 1, "stale": 0}
    assert not s._get_conn().execute("SELECT 1 FROM cold_embeddings WHERE memory_id=?", (queued,)).fetchone()
    assert s.vec_count() == 1
    assert s._get_conn().execute("SELECT embedding FROM cold_embeddings WHERE memory_id=?", (old_id,)).fetchone()[0] == before_blob


def test_nested_failure_rolls_back_only_vec_store(engine):
    mid = add_vector(engine)
    s = engine.storage
    conn = s._get_conn()
    before_blob = conn.execute("SELECT embedding FROM cold_embeddings WHERE memory_id=?", (mid,)).fetchone()[0]
    conn.execute("CREATE TRIGGER fail_space BEFORE INSERT ON embedding_spaces BEGIN SELECT RAISE(ABORT, 'injected'); END")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE cold_memory SET source='caller' WHERE id=?", (mid,))
    with pytest.raises(sqlite3.DatabaseError):
        s.vec_store(mid, emb.get_embedding("different"))
    assert conn.in_transaction
    conn.commit()
    assert s.cold_get(mid)["source"] == "caller"
    assert conn.execute("SELECT embedding FROM cold_embeddings WHERE memory_id=?", (mid,)).fetchone()[0] == before_blob
    assert conn.execute("SELECT embedding FROM cold_memory_vec WHERE memory_id=?", (mid,)).fetchone()[0] == before_blob


def test_enqueue_snapshot_failure_does_not_leave_unversioned_job(engine):
    s = engine.storage
    mid = s.cold_add("memory", "old")["id"]
    conn = s._get_conn()
    conn.execute("CREATE TRIGGER fail_version BEFORE INSERT ON embedding_job_versions BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.DatabaseError):
        queue.enqueue(conn, "cold_memory", mid, "old")
    conn.commit()
    assert not conn.execute("SELECT 1 FROM embedding_jobs").fetchone()


def test_reopen_never_adopts_legacy_labels_or_drops_vectors(engine):
    mid = add_vector(engine)
    s = engine.storage
    s._get_conn().execute("DELETE FROM embedding_spaces")
    s._get_conn().commit()
    before = snapshot(s)
    reopened = Storage(s.db_path)
    try:
        assert snapshot(reopened) == before
        assert reopened.vec_search(emb.get_embedding("old")) == []
        with pytest.raises(VectorSpaceError):
            reopened.vec_store(mid, emb.get_embedding("old"))
        assert snapshot(reopened) == before
    finally:
        reopened.close()


def test_async_worker_failure_cannot_commit_partial_vector(engine, monkeypatch):
    mid = add_vector(engine)
    s = engine.storage
    conn = s._get_conn()
    queue.enqueue(conn, "cold_memory", mid, "old")
    before = snapshot(s)
    conn.execute("CREATE TRIGGER fail_space BEFORE INSERT ON embedding_spaces BEGIN SELECT RAISE(ABORT, 'injected'); END")
    async def run():
        stop = asyncio.Event()
        original = queue.mark_failed
        def fail(conn, job_id, error):
            original(conn, job_id, error)
            stop.set()
        monkeypatch.setattr(queue, "mark_failed", fail)
        await asyncio.wait_for(queue.embedding_worker(engine, stop), timeout=3)
    asyncio.run(run())
    assert snapshot(s) == before
    assert queue.queue_stats(conn) == {"failed": 1}


def _all_rows(storage):
    """Include jobs, timestamps, FTS and vec shadow tables, not just counts."""
    conn = storage._get_conn()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
              if r[0] not in ("embedding_spaces", "embedding_space_adoptions", "sqlite_sequence")]
    return {name: sorted((tuple(r) for r in conn.execute(f'SELECT * FROM "{name}"')), key=repr)
            for name in tables}


def test_explicit_adoption_adds_only_missing_metadata_and_audit(engine):
    s = engine.storage
    ids = [add_vector(engine, text) for text in ("old", "second")]
    conn = s._get_conn()
    queue.enqueue(conn, "cold_memory", ids[0], "old")
    conn.execute("DELETE FROM embedding_spaces WHERE memory_id=?", (ids[0],))
    conn.commit()
    existing = tuple(conn.execute("SELECT * FROM embedding_spaces").fetchone())
    before = _all_rows(s)
    vector = emb.get_embedding("old")
    assert s.vec_search(vector) == []
    evidence = " Synthetic fixture generation recorded; not production proof. \n"
    statements = []
    conn.set_trace_callback(statements.append)
    result = s.adopt_legacy_space(vector.model, vector.space_id, evidence)
    conn.set_trace_callback(None)
    assert any(sql == "BEGIN IMMEDIATE" for sql in statements)
    assert result["validated_count"] == 2 and result["adopted_count"] == 1
    assert _all_rows(s) == before
    assert tuple(conn.execute("SELECT * FROM embedding_spaces WHERE memory_id=?", (ids[1],)).fetchone()) == existing
    audit = dict(conn.execute("SELECT * FROM embedding_space_adoptions WHERE id=?", (result["audit_id"],)).fetchone())
    assert audit == {"id": result["audit_id"], "model": vector.model, "space_id": vector.space_id,
                     "evidence": evidence, "created_at": result["created_at"], "validated_count": 2, "adopted_count": 1}
    assert s.vec_search(vector)[0]["id"] == ids[0]
    assert s.adopt_legacy_space(vector.model, vector.space_id, "Explicit repeat")['adopted_count'] == 0
    assert _all_rows(s) == before


@pytest.mark.parametrize("damage", ["mixed_model", "wrong_model", "dimension", "blob_mismatch",
                                    "vec_orphan", "missing_vec", "cold_orphan", "space_orphan",
                                    "space_conflict", "space_model_conflict", "nonfinite"])
def test_adoption_rejects_entire_corrupt_corpus_without_changes(tmp_path, damage):
    import struct
    s = Storage(tmp_path / "adopt.db")
    try:
        conn = s._get_conn()
        vector = emb.get_embedding("old")
        ids = [s.cold_add("memory", text)["id"] for text in ("one", "two")]
        for mid in ids:
            s.vec_store(mid, vector)
        conn.execute("DELETE FROM embedding_spaces WHERE memory_id=?", (ids[0],))
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")  # reproduce pre-existing corruption
        model, space = vector.model, vector.space_id
        if damage == "mixed_model":
            conn.execute("UPDATE cold_embeddings SET model='other' WHERE memory_id=?", (ids[1],))
        elif damage == "wrong_model":
            model = "not-the-stored-model"
        elif damage == "dimension":
            conn.execute("UPDATE cold_embeddings SET embedding=? WHERE memory_id=?", (b'\x00' * 4, ids[1]))
        elif damage == "blob_mismatch":
            conn.execute("UPDATE cold_embeddings SET embedding=? WHERE memory_id=?", (s._serialize_vec([0.0] * 768), ids[1]))
        elif damage == "vec_orphan":
            conn.execute("INSERT INTO cold_memory_vec VALUES (?,?)", ("orphan", s._serialize_vec(vector)))
        elif damage == "missing_vec":
            conn.execute("DELETE FROM cold_memory_vec WHERE memory_id=?", (ids[1],))
        elif damage == "cold_orphan":
            conn.execute("DELETE FROM cold_memory WHERE id=?", (ids[1],))
        elif damage == "space_orphan":
            conn.execute("INSERT INTO embedding_spaces VALUES (?,?,?)", ("orphan", space, model))
        elif damage == "space_conflict":
            conn.execute("UPDATE embedding_spaces SET space_id='other-space'")
        elif damage == "space_model_conflict":
            conn.execute("UPDATE embedding_spaces SET model='other-model'")
        elif damage == "nonfinite":
            blob = struct.pack('<768f', *([float('inf')] * 768))
            conn.execute("UPDATE cold_embeddings SET embedding=? WHERE memory_id=?", (blob, ids[1]))
            conn.execute("DELETE FROM cold_memory_vec WHERE memory_id=?", (ids[1],))
            conn.execute("INSERT INTO cold_memory_vec VALUES (?,?)", (ids[1], blob))
        conn.commit()
        before, provenance = _all_rows(s), snapshot(s)
        with pytest.raises(ValueError):
            s.adopt_legacy_space(model, space, "Synthetic corruption must be rejected")
        conn.commit()
        assert _all_rows(s) == before and snapshot(s) == provenance
        assert conn.execute("SELECT count(*) FROM embedding_space_adoptions").fetchone()[0] == 0
    finally:
        s.close()


@pytest.mark.parametrize("args", [("", "space", "evidence"), ("model", " ", "evidence"),
                                 ("model", "space", ""), ("model", "space", " \n"),
                                 ("model", "space", None), (1, "space", "evidence")])
def test_adoption_requires_explicit_nonempty_strings(engine, args):
    before = snapshot(engine.storage)
    with pytest.raises(VectorSpaceError):
        engine.storage.adopt_legacy_space(*args)
    assert snapshot(engine.storage) == before


def test_adoption_rejects_empty_corpus_and_caller_transaction(engine):
    s = engine.storage
    with pytest.raises(VectorSpaceError):
        s.adopt_legacy_space("model", "space", "evidence")
    mid = add_vector(engine)
    conn = s._get_conn()
    conn.execute("UPDATE cold_memory SET source='caller' WHERE id=?", (mid,))
    vector = emb.get_embedding("old")
    with pytest.raises(VectorSpaceError, match="BEGIN IMMEDIATE"):
        s.adopt_legacy_space(vector.model, vector.space_id, "evidence")
    assert conn.in_transaction
    assert conn.execute("SELECT source FROM cold_memory WHERE id=?", (mid,)).fetchone()[0] == "caller"
    conn.rollback()


@pytest.mark.parametrize("failed_table", ["embedding_spaces", "embedding_space_adoptions"])
def test_adoption_failure_rolls_back_metadata_and_audit(engine, failed_table):
    add_vector(engine)
    s = engine.storage
    conn = s._get_conn()
    conn.execute("DELETE FROM embedding_spaces")
    conn.commit()
    conn.execute(f"CREATE TRIGGER fail_adopt BEFORE INSERT ON {failed_table} BEGIN SELECT RAISE(ABORT, 'injected'); END")
    before = snapshot(s)
    vector = emb.get_embedding("old")
    with pytest.raises(sqlite3.DatabaseError):
        s.adopt_legacy_space(vector.model, vector.space_id, "evidence")
    conn.commit()
    assert snapshot(s) == before
    assert conn.execute("SELECT count(*) FROM embedding_space_adoptions").fetchone()[0] == 0


def test_adoption_locks_writers_before_validation(engine, monkeypatch):
    mid = add_vector(engine)
    s = engine.storage
    original = s._validate_legacy_vectors
    def validate(conn, model, space):
        assert conn.in_transaction
        other = s._make_conn()
        try:
            other.execute("PRAGMA busy_timeout=1")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE cold_embeddings SET model='race' WHERE memory_id=?", (mid,))
        finally:
            other.close()
        return original(conn, model, space)
    monkeypatch.setattr(s, "_validate_legacy_vectors", validate)
    vector = emb.get_embedding("old")
    assert s.adopt_legacy_space(vector.model, vector.space_id, "evidence")["validated_count"] == 1


def test_dream_rehydrates_stored_space_not_current_provider(engine):
    from openhippo.core.dream import DreamEngine, DreamConfig
    s = engine.storage
    ids = [s.cold_add("memory", text)["id"] for text in ("dream one", "dream two")]
    vector = emb.EmbeddingVector([1.0] + [0.0] * 767,
                                 model="synthetic-dream", space_id="synthetic-dream-768-v1")
    for mid in ids:
        s.vec_store(mid, vector)
    dream = DreamEngine(s)
    restored = dream._get_embedding(ids[0])
    assert isinstance(restored, emb.EmbeddingVector)
    assert restored.model == vector.model and restored.space_id == vector.space_id
    assert restored == vector
    assert len(dream._cluster([s.cold_get(mid) for mid in ids], DreamConfig())) == 1
    conn = s._get_conn()
    conn.execute("DELETE FROM embedding_spaces")
    conn.commit()
    assert dream._get_embedding(ids[0]) is None
    assert dream._cluster([s.cold_get(mid) for mid in ids], DreamConfig()) == []
    s.adopt_legacy_space(vector.model, vector.space_id, "Synthetic fixture provenance")
    assert len(dream._cluster([s.cold_get(mid) for mid in ids], DreamConfig())) == 1
    conn.execute("UPDATE embedding_spaces SET model='conflict' WHERE memory_id=?", (ids[0],))
    conn.commit()
    assert dream._get_embedding(ids[0]) is None


def test_26k_query_gate_uses_indexed_metadata_not_blob_audit(tmp_path):
    import time
    s = Storage(tmp_path / "scale.db")
    try:
        conn = s._get_conn()
        n = 26238
        vector = emb.get_embedding("scale")
        blob = s._serialize_vec(vector)
        with atomic(conn):
            conn.executemany("INSERT INTO cold_memory (id, target, content) VALUES (?, 'memory', ?)",
                             ((f"scale-{i:05}", f"synthetic scale {i}") for i in range(n)))
            conn.executemany("INSERT INTO cold_embeddings (memory_id, embedding, model) VALUES (?,?,?)",
                             ((f"scale-{i:05}", blob, vector.model) for i in range(n)))
            conn.executemany("INSERT INTO cold_memory_vec (memory_id, embedding) VALUES (?,?)",
                             ((f"scale-{i:05}", blob) for i in range(n)))
            conn.executemany("INSERT INTO embedding_spaces VALUES (?,?,?)",
                             ((f"scale-{i:05}", vector.space_id, vector.model) for i in range(n)))
        reads, statements = [], []
        def authorize(action, table, column, db, source):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
                if table.startswith("cold_memory_vec") or (table == "cold_embeddings" and column == "embedding"):
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        conn.set_trace_callback(statements.append)
        start = time.perf_counter()
        s._check_vector_space(conn, vector.model, vector.space_id)
        gate_seconds = time.perf_counter() - start
        conn.set_authorizer(None)
        conn.set_trace_callback(None)
        assert gate_seconds < 2.0  # prior vec0/BLOB join measured >10s at this size
        assert reads and all(table in ("cold_embeddings", "embedding_spaces") for table, _ in reads)
        sql = next(sql for sql in statements if "SELECT e.memory_id" in sql)
        plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        assert any("COVERING INDEX idx_cold_embeddings_identity" in line for line in plan)
        assert any("SEARCH s USING INDEX" in line for line in plan)
        writes = conn.total_changes
        query_seconds = []
        for _ in range(3):
            start = time.perf_counter()
            result = s.vec_search(vector, limit=3)
            query_seconds.append(time.perf_counter() - start)
            assert len(result) == 3 and all(r["vec_distance"] == 0 for r in result)
        assert max(query_seconds) < 2.0
        assert conn.total_changes == writes
        # A single missing declaration must still close the whole corpus.
        conn.execute("DELETE FROM embedding_spaces WHERE memory_id='scale-26237'")
        conn.commit()
        assert s.vec_search(vector) == []
        start = time.perf_counter()
        adoption = s.adopt_legacy_space(vector.model, vector.space_id, "26238 synthetic fixtures")
        audit_seconds = time.perf_counter() - start
        assert adoption["validated_count"] == n and adoption["adopted_count"] == 1
        assert len(s.vec_search(vector, limit=3)) == 3
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        print(f"\nSYNTHETIC_PERF n={n} gate_s={gate_seconds:.6f} query_s={query_seconds} full_adopt_s={audit_seconds:.6f}")
    finally:
        s.close()


if __name__ == "__main__":
    assert Path(emb.__file__).resolve() == Path(__file__).resolve().parents[1] / "src/openhippo/core/embedding.py"
    emb.set_provider(FakeProvider())
    try:
        code = pytest.main(["--noconftest", "-p", "no:cacheprovider", "-q", "--basetemp", _sandbox.name + "/pytest",
                            *([str(Path(__file__))] if len(sys.argv) == 1 else sys.argv[1:])])
    finally:
        _sandbox.cleanup()
    raise SystemExit(code)
