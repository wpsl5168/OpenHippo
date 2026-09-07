"""Real export -> real importer -> every persisted column; no live services."""
from __future__ import annotations

import copy
import io
import json
import socket
import struct

import pytest

from openhippo.core import embedding
from openhippo.core.export import check_embedding_compatibility, export_json
from openhippo.core.importer import import_json
from openhippo.core.storage import Storage


class SyntheticProvider:
    model = "synthetic-test-model"
    dimension = 768


MODEL, SPACE = embedding.provider_identity(SyntheticProvider())
TABLES = ("hot_memory", "cold_memory", "cold_embeddings", "cold_memory_vec", "embedding_spaces")


def vector_record(mid="atomic"):
    return {"id": mid, "content": "atomic sentinel", "embedding": [0.125] + [0.0] * 767,
            "embedding_model": MODEL, "embedding_created_at": 1234.125,
            "embedding_space_record": {"memory_id": mid, "model": MODEL, "space_id": SPACE}}


@pytest.fixture(scope="session", autouse=True)
def _install_global_client():
    """Override conftest's API startup for these storage-only regressions."""
    yield


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network/provider initialization forbidden during backup")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(embedding, "_provider", None)
    monkeypatch.setattr(embedding, "get_provider", forbidden)


@pytest.fixture
def stores(tmp_path):
    source = Storage(tmp_path / "source.db")
    dest = Storage(tmp_path / "dest.db")
    yield source, dest
    source.close()
    dest.close()


def rows(storage, table):
    conn = storage._get_conn()
    order = "id" if table in ("hot_memory", "cold_memory") else "memory_id"
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def snapshot(storage):
    return {t: rows(storage, t) for t in TABLES}


def seed(storage):
    conn = storage._get_conn()
    for i in range(2):
        mid = storage.hot_add("user" if i else "memory", f"hot record {i}",
                               agent_id=f"agent-hot-{i}", scope="global" if i else "session",
                               session_id=f"session-hot-{i}")["id"]
        conn.execute("UPDATE hot_memory SET id=?, sort_order=?, created_at=?, updated_at=? WHERE id=?",
                     (f"hot-original-{i}", 73 - i, 1200.125 + i, 1600.875 + i, mid))
        conn.commit()
    for i, status in enumerate(("active", "dormant", "consolidated")):
        mid = storage.cold_add("user" if i else "memory", f"cold recovery record {i}",
            source="session_summary", tags=["非默认", f"tag{i}"], metadata={"z": [1, True, None]},
            archived_from=f"historical-hot-{i}", agent_id=f"agent-cold-{i}", scope="session",
            session_id=f"cold-session-{i}", originator=f"source-agent-{i}", channel="feishu")["id"]
        conn.execute("""UPDATE cold_memory SET id=?, access_count=?, created_at=?, updated_at=?,
            last_accessed=?, importance=?, dream_status=?, last_dream_at=?, consolidated_into=?,
            merged_from=?, tags=?, metadata=? WHERE id=?""",
            (f"cold-original-{i}", 82 + i, 2100.125 + i, 2900.625 + i, 3100.25 + i,
             0.83, status, "2026-07-03T05:04:01Z", "seed-old" if i == 2 else None,
             '[ "old-a", "old-b" ]', '["非默认", "tag' + str(i) + '"]',
             '{ "z" : [1, true, null], "note": "保留空白" }', mid))
        conn.commit()
        if i != 2:
            storage.vec_store(f"cold-original-{i}", [0.125, -0.25] + [0.0] * 766,
                              model=MODEL, space_id=SPACE)
            conn.execute("UPDATE cold_embeddings SET created_at=? WHERE memory_id=?",
                         (4000.375 + i, f"cold-original-{i}"))
            conn.commit()


@pytest.mark.parametrize("jsonlines", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_real_backup_roundtrip_all_columns_and_retry(stores, jsonlines, stream):
    source, dest = stores
    seed(source)
    before = snapshot(source)
    output = io.StringIO() if stream else None
    data = export_json(source, output=output, jsonlines=jsonlines, agent_id="annotation-only")
    if stream:
        assert data is None
        data = output.getvalue()
    report = import_json(dest, data)
    assert report["errors"] == []
    assert (report["imported_hot"], report["imported_cold"], report["vectors_preserved"]) == (2, 3, 2)
    assert snapshot(dest) == before  # IDs, every column, exact JSON strings, BLOB bytes/model/time
    assert report["vectors_unindexed"] == []
    assert report["reembed_warning"]
    assert dest._get_conn().execute("SELECT COUNT(*) FROM cold_memory_vec").fetchone()[0] == 2
    hits = dest.vec_search([0.125, -0.25] + [0.0] * 766, model=MODEL, space_id=SPACE)
    assert any(hit["id"] == "cold-original-0" and hit["vec_distance"] == 0 for hit in hits)
    assert snapshot(source) == before  # export is read-only, including access counters
    again = import_json(dest, data)
    assert again["errors"] == []
    assert (again["imported_hot"], again["imported_cold"], again["skipped_dup"]) == (0, 0, 5)
    assert snapshot(dest) == before
    # Check committed persistence via a fresh connection, not just own transaction.
    reopened = Storage(dest.db_path)
    assert snapshot(reopened) == before
    reopened.close()
    fts = dest._get_conn().execute(
        "SELECT COUNT(*) FROM cold_memory_fts WHERE cold_memory_fts MATCH 'recovery'").fetchone()[0]
    assert fts == 3


@pytest.mark.parametrize("jsonlines", [False, True])
def test_filtered_counts_and_zero_time_boundary(stores, jsonlines):
    source, _ = stores
    seed(source)
    data = export_json(source, target="user", jsonlines=jsonlines)
    if jsonlines:
        objects = [json.loads(s) for s in data.splitlines()]
        header, memories = objects[0]["__header__"], objects[1:]
    else:
        doc = json.loads(data)
        header, memories = doc["header"], doc["memories"]
    assert header["total_count"] == len(memories) == 3
    assert (header["total_hot"], header["total_cold"]) == (1, 2)
    assert all(m["target"] == "user" for m in memories)
    assert json.loads(export_json(source, until=0))["memories"] == []


@pytest.mark.parametrize("version", ["0.9", "1.0"])
@pytest.mark.parametrize("with_ids", [False, True])
def test_legacy_schema_defaults_and_stable_missing_ids(stores, version, with_ids):
    _, dest = stores
    hot = {"layer": "hot", "content": "legacy hot", "metadata": {}}
    cold = {"content": "legacy cold", "tags": ["old"], "metadata": {"x": 4}}
    if with_ids:
        hot["id"], cold["id"] = "legacy/hot:01", "legacy/cold:01"
    doc = {"header": {"schema_version": version}, "memories": [hot, cold]}
    result = import_json(dest, doc)
    assert not result["errors"]
    first = snapshot(dest)
    assert import_json(dest, doc)["skipped_dup"] == 2
    assert snapshot(dest) == first
    if with_ids:
        assert first["hot_memory"][0]["id"] == hot["id"]
        assert first["cold_memory"][0]["id"] == cold["id"]


@pytest.mark.parametrize("layer", ["hot", "cold"])
@pytest.mark.parametrize("field,value", [("content", "different"), ("scope", "global"),
                                         ("agent_id", "other"), ("updated_at", 1.25)])
def test_same_id_conflicts_without_overwrite(stores, layer, field, value):
    source, dest = stores
    seed(source)
    doc = json.loads(export_json(source))
    assert not import_json(dest, doc)["errors"]
    before = snapshot(dest)
    record = next(m for m in doc["memories"] if m["layer"] == layer)
    record[field] = value
    result = import_json(dest, {"memories": [record]})
    assert len(result["conflicts"]) == len(result["errors"]) == 1
    assert result["skipped_dup"] == 0
    assert snapshot(dest) == before


@pytest.mark.parametrize("mutate", ["vector", "model", "time"])
def test_same_id_vector_conflicts(stores, mutate):
    source, dest = stores
    seed(source)
    doc = json.loads(export_json(source))
    assert not import_json(dest, doc)["errors"]
    before = snapshot(dest)
    mem = next(m for m in doc["memories"] if "embedding" in m)
    if mutate == "vector":
        mem["embedding"][0] = 1.0
    elif mutate == "model":
        mem["embedding_model"] = mem["embedding_record"]["model"] = "wrong"
    else:
        mem["embedding_created_at"] = mem["embedding_record"]["created_at"] = 34.5
    result = import_json(dest, {"memories": [mem]})
    assert len(result["conflicts"]) == 1
    assert snapshot(dest) == before


@pytest.mark.parametrize("vec", [[0.1] * 3, [], [float("nan")] * 768,
                                  [float("inf")] * 768, [1e39] * 768, [True] * 768])
def test_invalid_vectors_never_leave_partial_commits(stores, vec):
    _, dest = stores
    doc = {"memories": [{"id": "bad-vector", "content": "rollback sentinel", "embedding": vec}]}
    for _ in range(2):
        report = import_json(dest, doc)
        assert len(report["errors"]) == 1
        assert report["imported_cold"] == 0
        dest._get_conn().commit()  # simulate a later unrelated commit
        assert snapshot(dest) == {t: [] for t in TABLES}
        assert not dest._get_conn().execute(
            "SELECT rowid FROM cold_memory_fts WHERE cold_memory_fts MATCH 'sentinel'").fetchall()


def test_late_blob_failure_rolls_back_memory_fts_then_retry(stores):
    _, dest = stores
    conn = dest._get_conn()
    conn.execute("""CREATE TRIGGER fail_restore AFTER INSERT ON cold_embeddings
                    BEGIN SELECT RAISE(ABORT, 'injected blob failure'); END""")
    conn.commit()
    doc = {"memories": [vector_record()]}
    result = import_json(dest, doc)
    assert len(result["errors"]) == 1
    assert "injected blob failure" in result["errors"][0]["reason"]
    conn.commit()
    assert not rows(dest, "cold_memory") and not rows(dest, "cold_embeddings")
    assert not conn.execute("SELECT rowid FROM cold_memory_fts WHERE cold_memory_fts MATCH 'sentinel'").fetchall()
    conn.execute("DROP TRIGGER fail_restore")
    conn.commit()
    assert import_json(dest, doc)["imported_cold"] == 1
    assert import_json(dest, doc)["skipped_dup"] == 1


def test_independent_record_failure_and_dry_run(stores):
    source, dest = stores
    seed(source)
    doc = json.loads(export_json(source))
    doc["memories"].insert(1, {"id": "bad", "content": "bad", "embedding": [0.0]})
    doc["memories"].append(copy.deepcopy(doc["memories"][0]))
    preview = import_json(dest, doc, dry_run=True)
    assert (preview["imported_hot"], preview["imported_cold"], preview["skipped_dup"]) == (2, 3, 1)
    assert len(preview["errors"]) == 1
    assert snapshot(dest) == {t: [] for t in TABLES}
    actual = import_json(dest, doc)
    assert (actual["imported_hot"], actual["imported_cold"], actual["skipped_dup"]) == (2, 3, 1)
    assert len(actual["errors"]) == 1
    assert snapshot(dest) == snapshot(source)


def test_caller_transaction_is_not_committed_or_rolled_back(stores):
    _, dest = stores
    conn = dest._get_conn()
    conn.execute("INSERT INTO hot_memory(id,target,content) VALUES ('caller','memory','caller')")
    doc = {"memories": [{"id": "new", "content": "new memory"},
                         {"id": "bad", "content": "bad", "embedding": [1.0]}]}
    result = import_json(dest, doc)
    assert result["imported_cold"] == 1 and len(result["errors"]) == 1
    assert conn.in_transaction
    conn.rollback()
    assert not rows(dest, "hot_memory") and not rows(dest, "cold_memory")


def test_equal_content_different_id_is_not_discarded(stores):
    _, dest = stores
    records = [{"id": f"separate-{i}", "content": "same text", "agent_id": f"agent-{i}"} for i in range(2)]
    result = import_json(dest, {"memories": records})
    assert result["imported_cold"] == 2 and not result["errors"]
    assert import_json(dest, {"memories": records})["skipped_dup"] == 2


def test_no_vectors_and_jsonl_whitespace(stores):
    source, dest = stores
    seed(source)
    data = export_json(source, include_embeddings=False, jsonlines=True)
    data = "\n  " + data.replace('"__header__":', '"__header__" :') + "\n"
    result = import_json(dest, data)
    assert not result["errors"] and result["vectors_preserved"] == 0
    assert rows(dest, "cold_memory") == rows(source, "cold_memory")
    assert not rows(dest, "cold_embeddings")


def test_unknown_vector_rejected_without_poisoning_existing_index(stores):
    _, dest = stores
    assert not import_json(dest, {"memories": [vector_record()]})["errors"]
    before = snapshot(dest)
    result = import_json(dest, {"memories": [{"id": "old-vector", "content": "old vector",
                                            "embedding": [0.0] * 768}]})
    assert len(result["errors"]) == 1 and "unknown embedding space" in result["errors"][0]["reason"]
    dest._get_conn().commit()
    assert snapshot(dest) == before
    assert dest.vec_search(vector_record()["embedding"], model=MODEL, space_id=SPACE)


@pytest.mark.parametrize("extra", [{"mystery": 5}, {"layer": "wrong"}, {"id": None},
                                    {"storage_json": {"tags": "[1]"}, "tags": [2]}])
def test_unrestorable_fields_are_errors_not_silently_dropped(stores, extra):
    _, dest = stores
    mem = {"id": "unsupported", "content": "keep safe", **extra}
    result = import_json(dest, {"memories": [mem]})
    assert len(result["errors"]) == 1 and not rows(dest, "cold_memory")


def test_compatibility_requires_core_space_tag(monkeypatch):
    for backend in ("unknown", "unknown/CopilotProvider", "copilot/text-embedding-3-small"):
        assert check_embedding_compatibility({"embedding_backend": backend})["compatible"] is False
    monkeypatch.setattr(embedding, "_provider", SyntheticProvider())
    assert check_embedding_compatibility({"embedding_space": SPACE})["compatible"]
    for invalid in (None, "unknown", "v1:abc", SPACE.upper(), {"space_id": SPACE}, "v1:" + "0" * 64):
        assert not check_embedding_compatibility({"embedding_space": invalid})["compatible"]


def test_explicit_reembed_only_and_retry(monkeypatch, stores):
    _, dest = stores
    class Fake:
        model = "new-synthetic-model"
        dimension = 768
        calls = 0
        def embed(self, content):
            self.calls += 1
            return [0.5] * 768
    fake = Fake()
    monkeypatch.setattr(embedding, "get_provider", lambda: fake)
    doc = {"memories": [{"id": "opt-in", "content": "explicit remote opt in"}]}
    preview = import_json(dest, doc, reembed=True, dry_run=True)
    assert not preview["errors"] and fake.calls == 0
    result = import_json(dest, doc, reembed=True)
    assert not result["errors"] and result["reembedded"] == 1 and fake.calls == 1
    row = rows(dest, "cold_embeddings")[0]
    assert row["model"] == fake.model and row["embedding"] == struct.pack("<768f", *([0.5] * 768))
    assert rows(dest, "embedding_spaces")[0]["space_id"] == embedding.provider_identity(fake)[1]
    assert rows(dest, "cold_memory_vec")[0]["embedding"] == row["embedding"]
    assert import_json(dest, doc, reembed=True)["skipped_dup"] == 1
    assert fake.calls == 1


def test_failed_explicit_reembed_is_atomic(monkeypatch, stores):
    _, dest = stores
    class Fake:
        def embed(self, content):
            return None
        model = MODEL
        dimension = 768
    monkeypatch.setattr(embedding, "get_provider", Fake)
    report = import_json(dest, {"memories": [{"id": "failed", "content": "failure"}]}, reembed=True)
    assert len(report["errors"]) == 1 and not rows(dest, "cold_memory")


def test_null_json_columns_roundtrip(stores):
    source, dest = stores
    source._get_conn().execute("""INSERT INTO cold_memory(id,target,content,tags,metadata,source,scope)
                               VALUES ('null-json','user','null values',NULL,NULL,NULL,'global')""")
    source._get_conn().commit()
    assert not import_json(dest, export_json(source))["errors"]
    assert snapshot(dest) == snapshot(source)
    assert import_json(dest, export_json(source))["skipped_dup"] == 1


def test_explicit_vector_absence_conflicts_but_text_only_backup_does_not(stores):
    source, dest = stores
    source.cold_add("memory", "no source vector")
    data = export_json(source)
    assert not import_json(dest, data)["errors"]
    mid = rows(dest, "cold_memory")[0]["id"]
    dest.vec_store(mid, [0.0] * 768, model=MODEL, space_id=SPACE)
    before = snapshot(dest)
    assert len(import_json(dest, data)["conflicts"]) == 1
    assert snapshot(dest) == before
    assert import_json(dest, export_json(source, include_embeddings=False))["skipped_dup"] == 1


def test_export_snapshot_survives_concurrent_writer(stores):
    source, _ = stores
    seed(source)
    other = Storage(source.db_path)
    class Output(io.StringIO):
        changed = False
        def write(self, text):
            if not self.changed:
                self.changed = True
                other.hot_add("memory", "concurrent after snapshot")
            return super().write(text)
    output = Output()
    try:
        export_json(source, output=output, jsonlines=True)
    finally:
        other.close()
    objects = [json.loads(s) for s in output.getvalue().splitlines()]
    assert objects[0]["__header__"]["total_count"] == len(objects[1:]) == 5
    assert all(m["content"] != "concurrent after snapshot" for m in objects[1:])
    assert len(rows(source, "hot_memory")) == 3
    assert not source._get_conn().in_transaction


def test_export_output_failure_releases_read_snapshot(stores):
    source, _ = stores
    seed(source)
    class BrokenOutput:
        def write(self, text):
            raise OSError("injected output error")
    before = snapshot(source)
    with pytest.raises(OSError, match="injected"):
        export_json(source, output=BrokenOutput(), jsonlines=True)
    assert not source._get_conn().in_transaction
    assert snapshot(source) == before


@pytest.mark.parametrize("stage", ["space_insert", "timestamp_restore"])
def test_post_index_sql_failure_rolls_back_all_tables(stores, stage):
    _, dest = stores
    conn = dest._get_conn()
    event = "AFTER INSERT ON embedding_spaces" if stage == "space_insert" else "AFTER UPDATE OF created_at ON cold_embeddings"
    conn.execute(f"CREATE TRIGGER late_failure {event} BEGIN SELECT RAISE(ABORT, 'after vec0 insertion'); END")
    conn.commit()
    before = snapshot(dest)
    doc = {"memories": [vector_record()]}
    result = import_json(dest, doc)
    assert len(result["errors"]) == 1 and "after vec0 insertion" in result["errors"][0]["reason"]
    conn.commit()
    assert snapshot(dest) == before
    assert not conn.execute("SELECT rowid FROM cold_memory_fts WHERE cold_memory_fts MATCH 'sentinel'").fetchall()
    conn.execute("DROP TRIGGER late_failure")
    conn.commit()
    assert import_json(dest, doc)["imported_cold"] == 1
    assert import_json(dest, doc)["skipped_dup"] == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_other_space_atomically_rejected_before_any_record_write(stores, dry_run):
    _, dest = stores
    assert not import_json(dest, {"memories": [vector_record()]})["errors"]
    before = snapshot(dest)
    other = SyntheticProvider()
    other.space_revision = "different-preprocessing"
    mem = vector_record("incompatible")
    mem["embedding_space_record"]["space_id"] = embedding.provider_identity(other)[1]
    statements = []
    dest._get_conn().set_trace_callback(statements.append)
    result = import_json(dest, {"memories": [mem]}, dry_run=dry_run)
    dest._get_conn().set_trace_callback(None)
    assert len(result["conflicts"]) == 1 and result["imported_cold"] == 0
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)
    dest._get_conn().commit()
    assert snapshot(dest) == before
    assert dest.vec_search(vector_record()["embedding"], model=MODEL, space_id=SPACE)


@pytest.mark.parametrize("damage", ["space", "index", "index_bytes"])
@pytest.mark.parametrize("reembed", [False, True])
def test_idempotence_checks_index_and_space_without_repair(stores, damage, reembed):
    _, dest = stores
    doc = {"memories": [vector_record()]}
    assert not import_json(dest, doc)["errors"]
    conn = dest._get_conn()
    if damage == "space":
        conn.execute("UPDATE embedding_spaces SET space_id='unknown'")
    else:
        conn.execute("DELETE FROM cold_memory_vec")
        if damage == "index_bytes":
            conn.execute("INSERT INTO cold_memory_vec(memory_id,embedding) VALUES (?,?)",
                         ("atomic", struct.pack("<768f", *([0.5] * 768))))
    conn.commit()
    before = snapshot(dest)
    result = import_json(dest, doc, reembed=reembed)
    assert len(result["conflicts"]) == 1 and result["skipped_dup"] == 0
    assert snapshot(dest) == before


def test_export_provenance_is_stored_not_current_provider(stores, monkeypatch):
    source, _ = stores
    seed(source)
    provider = SyntheticProvider()
    provider.model = "new-provider-not-the-source"
    monkeypatch.setattr(embedding, "_provider", provider)
    doc = json.loads(export_json(source))
    for mem in doc["memories"]:
        if mem.get("embedding"):
            expected = source._get_conn().execute("SELECT * FROM embedding_spaces WHERE memory_id=?", (mem["id"],)).fetchone()
            assert mem["embedding_space_record"] == dict(expected)
            assert mem["embedding_space_record"]["space_id"] == SPACE


def test_legacy_unknown_export_keeps_source_and_rejects_before_writes(stores):
    source, dest = stores
    seed(source)
    source._get_conn().execute("DELETE FROM embedding_spaces")
    source._get_conn().commit()
    before = snapshot(source)
    data = export_json(source)
    doc = json.loads(data)
    statements = []
    dest._get_conn().set_trace_callback(statements.append)
    vectors = [m for m in doc["memories"] if m.get("embedding")]
    result = import_json(dest, {"header": doc["header"], "memories": vectors})
    dest._get_conn().set_trace_callback(None)
    assert len(result["errors"]) == len(vectors) == 2
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)
    assert snapshot(source) == before and snapshot(dest) == {t: [] for t in TABLES}
    assert json.loads(data) == doc


def test_reembed_rejects_mutated_provider_without_changing_config(stores, monkeypatch):
    _, dest = stores
    class SwitchingProvider(SyntheticProvider):
        def embed(self, content):
            self.model = "mutated-during-inference"
            return [0.125] + [0.0] * 767
    provider = SwitchingProvider()
    start_model, start_space = embedding.provider_identity(provider)
    configured = SyntheticProvider()
    monkeypatch.setattr(embedding, "_provider", configured)
    monkeypatch.setattr(embedding, "get_provider", lambda: provider)
    mem = vector_record()
    del mem["embedding_space_record"]
    result = import_json(dest, {"memories": [mem]}, reembed=True)
    assert len(result["errors"]) == 1 and result["reembedded"] == 0
    assert "configuration changed" in result["errors"][0]["reason"]
    assert embedding._provider is configured and configured.model == MODEL
    for table in ("cold_memory", "cold_embeddings", "cold_memory_vec", "embedding_spaces"):
        assert rows(dest, table) == []


def test_verified_vector_restore_respects_caller_transaction(stores):
    _, dest = stores
    conn = dest._get_conn()
    conn.execute("INSERT INTO hot_memory(id,target,content) VALUES ('caller','memory','caller')")
    assert import_json(dest, {"memories": [vector_record()]})["imported_cold"] == 1
    assert len(rows(dest, "cold_memory_vec")) == len(rows(dest, "embedding_spaces")) == 1
    assert conn.in_transaction
    conn.rollback()
    assert snapshot(dest) == {t: [] for t in TABLES}

