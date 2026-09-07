"""Lossless memory restore, with per-record atomicity and explicit conflicts.

Unverified backup vectors are rejected before any record writes. Re-embedding
is opt-in and never runs during dry-run; source backup files are never changed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import time
from typing import Any

from . import embedding as embedding_module
from .export import check_embedding_compatibility, _known_space
from .storage import Storage, VectorSpaceError, _content_hash


class ImportConflict(ValueError):
    """The destination already contains different data for this identity."""


def _parse_document(data: str | dict) -> dict:
    if isinstance(data, str):
        try:
            doc = json.loads(data)
        except json.JSONDecodeError:
            objects = [json.loads(line) for line in data.splitlines() if line.strip()]
            if not objects or not isinstance(objects[0], dict) or "__header__" not in objects[0]:
                raise ValueError("JSONL must start with a __header__ record")
            doc = {"header": objects[0]["__header__"], "memories": objects[1:]}
        else:
            if isinstance(doc, dict) and "__header__" in doc:
                doc = {"header": doc["__header__"], "memories": []}
    else:
        doc = data
    if not isinstance(doc, dict) or not isinstance(doc.get("header", {}), dict):
        raise ValueError("expected a backup document with an object header")
    if not isinstance(doc.get("memories"), list):
        raise ValueError("memories must be an array")
    version = str(doc.get("header", {}).get("schema_version", "1.0"))
    if version not in ("0.9", "1.0", "1.1"):
        raise ValueError(f"unsupported backup schema_version: {version}")
    return doc


def _columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _insert(conn, table: str, values: dict) -> None:
    # Column names originate only from PRAGMA after rejecting unknown fields.
    names = ",".join('"' + name.replace('"', '""') + '"' for name in values)
    conn.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in values)})',
                 tuple(values.values()))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


_WRAPPERS = {"layer", "storage_json", "embedding", "embedding_model", "embedding_created_at",
             "embedding_record", "embedding_indexed", "embedding_space_record"}


def _memory_values(conn, mem: dict) -> tuple[str, dict]:
    if not isinstance(mem, dict):
        raise ValueError("memory must be an object")
    layer = mem.get("layer", "cold")
    if layer not in ("hot", "cold"):
        raise ValueError(f"invalid layer: {layer}")
    if not isinstance(mem.get("content"), str) or not mem["content"]:
        raise ValueError("empty or non-string content")
    columns = _columns(conn, layer + "_memory")
    extra = set(mem) - columns - _WRAPPERS
    # v1.0 exported this synthetic field on hot rows (no storage column).
    if layer == "hot" and mem.get("metadata") == {}:
        extra.discard("metadata")
    if extra:
        raise ValueError(f"unsupported {layer} fields (not discarded): {sorted(extra)}")
    values = {k: v for k, v in mem.items() if k in columns}
    values.setdefault("target", "memory")
    if "id" not in values:
        # Old hand-authored files sometimes have no ID. Stable, full-record
        # identity makes their retries idempotent without content-only dedup.
        values["id"] = "import-" + hashlib.sha256(_canonical(mem).encode()).hexdigest()
    if not isinstance(values["id"], str) or not values["id"]:
        raise ValueError("id must be a nonempty string")
    raw = mem.get("storage_json", {})
    if not isinstance(raw, dict) or set(raw) - {"tags", "metadata"}:
        raise ValueError("invalid storage_json")
    for key in ("tags", "metadata"):
        if key not in values:
            if key in raw:
                raise ValueError(f"storage_json.{key} missing decoded field")
            continue
        if key in raw:
            if raw[key] is not None and not isinstance(raw[key], str):
                raise ValueError(f"storage_json.{key} must be text or null")
            decoded = json.loads(raw[key]) if raw[key] is not None else None
            if _canonical(decoded) != _canonical(values[key]):
                raise ValueError(f"storage_json.{key} conflicts with decoded value")
            values[key] = raw[key]
        elif values[key] is not None:
            values[key] = _canonical(values[key])
    # Reject NaN/Inf in all scalar/nested input fields, not just vectors.
    _canonical(mem)
    return layer, values


def _vector_values(conn, mem: dict, memory_id: str) -> dict | None:
    if "embedding" not in mem or mem["embedding"] is None:
        if any(k in mem for k in ("embedding_record", "embedding_model", "embedding_space_record", "embedding_created_at")):
            raise ValueError("embedding metadata without embedding")
        return None
    vec = mem["embedding"]
    if not isinstance(vec, list) or not vec:
        raise ValueError("embedding must be a nonempty float array")
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='cold_memory_vec'").fetchone()
    match = re.search(r"embedding\s+float\s*\[\s*(\d+)\s*\]", ddl[0] if ddl else "", re.I)
    if not match:
        raise ValueError("cannot verify destination vector dimension")
    dimension = int(match.group(1))
    if len(vec) != dimension:
        raise ValueError(f"embedding dimension {len(vec)} does not match destination {dimension}")
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in vec):
        raise ValueError("embedding contains non-numeric or non-finite values")
    blob = struct.pack(f"<{dimension}f", *vec)
    if any(not math.isfinite(x) for x in struct.unpack(f"<{dimension}f", blob)):
        raise ValueError("embedding overflows float32")
    record = mem.get("embedding_record", {})
    if not isinstance(record, dict):
        raise ValueError("embedding_record must be an object")
    record = dict(record)
    if set(record) - (_columns(conn, "cold_embeddings") - {"memory_id", "embedding"}):
        raise ValueError("unsupported embedding_record fields (not discarded)")
    for column, wrapper in (("model", "embedding_model"), ("created_at", "embedding_created_at")):
        if wrapper in mem:
            if column in record and record[column] != mem[wrapper]:
                raise ValueError(f"conflicting {wrapper}")
            record[column] = mem[wrapper]
    # Unknown stays unknown; do not invoke storage's historical nomic default.
    record.setdefault("model", "unknown")
    if not isinstance(record["model"], str) or not record["model"]:
        raise ValueError("embedding_model must be a nonempty string")
    record.update(memory_id=memory_id, embedding=blob)
    return record


def _same_memory(existing: dict, expected: dict, mem: dict) -> bool:
    for key, value in expected.items():
        actual = existing[key]
        if key in ("tags", "metadata") and key not in mem.get("storage_json", {}):
            actual = json.loads(actual) if actual is not None else None
            value = json.loads(value) if value is not None else None
        if actual != value:
            return False
    return True


def _space_values(mem: dict, vector: dict) -> dict:
    space = mem.get("embedding_space_record")
    if not isinstance(space, dict) or not _known_space(space.get("space_id")):
        raise ValueError("unknown embedding space: record rejected before writes; retain source backup or explicitly reembed")
    if set(space) - {"memory_id", "space_id", "model"}:
        raise ValueError("unsupported embedding_space_record fields (not discarded)")
    if space.get("memory_id", vector["memory_id"]) != vector["memory_id"] or space.get("model") != vector["model"]:
        raise ImportConflict("embedding space record conflicts with vector identity/model")
    if "embedding_indexed" in mem and mem["embedding_indexed"] is not True:
        raise ValueError("backup vector is not indexed; refusing a non-lossless partial index restore")
    return dict(space, memory_id=vector["memory_id"])


def _restore_record(storage, conn, layer: str, values: dict, vector: dict | None,
                    space: dict | None, mem: dict, *, reembed_retry=False) -> bool:
    """Restore all vector tables within the caller's record savepoint.

    Return False only for a verified idempotent retry. Even a matching content
    string cannot mask a different scope, lifecycle, timestamp or vector.
    """
    table, mid = layer + "_memory", values["id"]
    existing = conn.execute(f'SELECT * FROM "{table}" WHERE id=?', (mid,)).fetchone()
    if existing is not None:
        if not _same_memory(dict(existing), values, mem):
            raise ImportConflict(f"{layer} id {mid!r} already has different fields/content")
        if layer == "cold" and "embedding" in mem and mem["embedding"] is None:
            if conn.execute("SELECT 1 FROM cold_embeddings WHERE memory_id=?", (mid,)).fetchone():
                raise ImportConflict(f"cold id {mid!r} has an embedding absent from the backup")
        if vector is not None:
            old = conn.execute("SELECT * FROM cold_embeddings WHERE memory_id=?", (mid,)).fetchone()
            if old is None or any(old[k] != v for k, v in vector.items()):
                raise ImportConflict(f"cold id {mid!r} already has different/missing embedding")
            old_space = conn.execute("SELECT * FROM embedding_spaces WHERE memory_id=?", (mid,)).fetchone()
            old_index = conn.execute("SELECT embedding FROM cold_memory_vec WHERE memory_id=?", (mid,)).fetchone()
            if old_space is None or dict(old_space) != space or old_index is None or old_index[0] != vector["embedding"]:
                raise ImportConflict(f"cold id {mid!r} already has different/missing vector index or space")
        if reembed_retry:
            old = conn.execute("SELECT * FROM cold_embeddings WHERE memory_id=?", (mid,)).fetchone()
            old_space = conn.execute("SELECT * FROM embedding_spaces WHERE memory_id=?", (mid,)).fetchone()
            old_index = conn.execute("SELECT embedding FROM cold_memory_vec WHERE memory_id=?", (mid,)).fetchone()
            if (old is None or old_space is None or not _known_space(old_space["space_id"])
                    or old_index is None or old_index[0] != old["embedding"]):
                raise ImportConflict(f"cold id {mid!r} has no verified reembedded vector")
            storage._check_vector_space(conn, old["model"], old_space["space_id"])
        return False
    if vector is not None:
        # Check BEFORE creating either memory or blob: an orphan blob would
        # disable the entire shared index, even for previously healthy rows.
        storage._check_vector_space(conn, space["model"], space["space_id"])
        for vector_table in ("cold_embeddings", "cold_memory_vec", "embedding_spaces"):
            if conn.execute(f"SELECT 1 FROM {vector_table} WHERE memory_id=?", (mid,)).fetchone():
                raise ImportConflict(f"cold id {mid!r} already has orphan vector data")
    if layer == "cold" and "content_hash" not in values:
        values = dict(values, content_hash=_content_hash(values["content"]))
    _insert(conn, table, values)
    if vector is not None:
        vec = struct.unpack(f"<{len(vector['embedding']) // 4}f", vector["embedding"])
        storage.vec_store(mid, list(vec), model=space["model"], space_id=space["space_id"])
        # vec_store is savepoint-aware, but chooses a new timestamp. Restore
        # the source timestamp (and future supported metadata) before release.
        metadata = {k: v for k, v in vector.items() if k not in ("memory_id", "embedding", "model")}
        if metadata:
            assignments = ','.join('"' + k + '"=?' for k in metadata)
            conn.execute(f"UPDATE cold_embeddings SET {assignments} WHERE memory_id=?",
                         (*metadata.values(), mid))
    return True


def import_json(storage: Storage, data: str | dict, *,
                reembed: bool = False, dry_run: bool = False) -> dict:
    """Restore JSON/JSONL, including old 0.9/1.0 records.

    Failures are per-record, with no partial memory/FTS/blob writes. Same-ID
    differences are explicit conflicts, never upserts. Unknown vector spaces
    are rejected atomically, leaving the source backup intact. This
    conservative mode never calls a remote embedding
    provider unless reembed=True. It is not a whole-database/audit-log backup.
    """
    doc = _parse_document(data)
    compat = check_embedding_compatibility(doc.get("header", {}))
    result = {"imported_hot": 0, "imported_cold": 0, "skipped_dup": 0,
              "reembedded": 0, "errors": [], "conflicts": [], "dry_run": dry_run,
              "vectors_preserved": 0, "vectors_unindexed": [],
              "reembed_warning": None if compat["compatible"] else
              "Embedding space is unverified or different; no automatic re-embedding. "
              "Only per-record attested spaces can be restored; unknown vector records are rejected."}
    conn = storage._get_conn()
    # Dry-run performs the same constraints/trigger/blob writes, then rolls them
    # all back, allowing duplicates within the preview to be detected as well.
    if dry_run:
        conn.execute("SAVEPOINT import_preview")
    try:
        for i, mem in enumerate(doc["memories"]):
            savepoint = False
            try:
                layer, values = _memory_values(conn, mem)
                vector = _vector_values(conn, mem, values["id"])
                if layer == "hot" and vector is not None:
                    raise ValueError("hot memory cannot store embeddings")
                generated = False
                space = None
                # Check identity before opt-in remote work. Never modify an
                # existing record just because reembed was requested.
                conn.execute("SAVEPOINT import_record")
                savepoint = True
                exists = conn.execute(f'SELECT 1 FROM "{layer}_memory" WHERE id=?',
                                      (values["id"],)).fetchone() is not None
                if reembed and layer == "cold" and not dry_run and not exists:
                    # Explicit opt-in only. Bypass legacy cross-provider cache.
                    provider = embedding_module.get_provider()
                    model, space_id = embedding_module.provider_identity(provider)
                    storage._check_vector_space(conn, model, space_id)
                    vec = provider.embed(values["content"])
                    if embedding_module.provider_identity(provider) != (model, space_id):
                        raise ValueError("provider configuration changed during re-embedding; record not imported")
                    if vec is None:
                        raise ValueError("explicit re-embedding failed; memory not imported")
                    # Capture identity BEFORE inference, not from mutable global
                    # configuration or a provider switched during the request.
                    vec = embedding_module.EmbeddingVector(vec, model=model, space_id=space_id)
                    fresh = {"embedding": vec, "embedding_model": model,
                             "embedding_created_at": time.time(),
                             "embedding_space_record": {"memory_id": values["id"], "model": model, "space_id": space_id}}
                    vector = _vector_values(conn, fresh, values["id"])
                    space = _space_values(fresh, vector)
                    generated = True
                # With explicit reembed, source vectors are intentionally not
                # the desired output. A retry compares memory fields only and
                # never overwrites an existing destination embedding.
                comparison_vector = None if reembed and exists else vector
                comparison_mem = {k: v for k, v in mem.items() if k != "embedding"} if reembed and exists else mem
                if comparison_vector is not None and space is None:
                    space = _space_values(mem, comparison_vector)
                created = _restore_record(storage, conn, layer, values, comparison_vector, space,
                                          comparison_mem, reembed_retry=reembed and exists and layer == "cold")
                conn.execute("RELEASE SAVEPOINT import_record")
                savepoint = False
                if created:
                    result["imported_" + layer] += 1
                    result["reembedded"] += int(generated)
                    result["vectors_preserved"] += int(vector is not None)
                else:
                    result["skipped_dup"] += 1

            except Exception as exc:
                if savepoint:
                    conn.execute("ROLLBACK TO SAVEPOINT import_record")
                    conn.execute("RELEASE SAVEPOINT import_record")
                error = {"index": i, "id": mem.get("id") if isinstance(mem, dict) else None,
                         "reason": str(exc), "type": "conflict" if isinstance(exc, (ImportConflict, VectorSpaceError)) else "invalid_record"}
                result["errors"].append(error)
                if isinstance(exc, (ImportConflict, VectorSpaceError)):
                    result["conflicts"].append(error)
    finally:
        if dry_run:
            conn.execute("ROLLBACK TO SAVEPOINT import_preview")
            conn.execute("RELEASE SAVEPOINT import_preview")
    return result
