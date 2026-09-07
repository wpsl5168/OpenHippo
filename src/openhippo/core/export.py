"""Versioned memory backups. JSON/JSONL retain all stored memory fields."""
from __future__ import annotations

import csv
import io
import json
import re
import struct
from datetime import datetime, timezone
from typing import Any, Iterator

from . import embedding as embedding_module
from .storage import Storage

EXPORT_SCHEMA_VERSION = "1.1"


def _get_embedding_backend_info() -> str:
    """Describe an already configured provider; never initialize/probe one.

    Provider initialization can read credentials or probe remote services. A
    backup must not do either. This is configuration, NOT stored-vector proof.
    """
    provider = embedding_module._provider
    if provider is None:
        return "unknown"
    names = {"OllamaProvider": "ollama", "CopilotProvider": "copilot",
             "SentenceTransformerProvider": "sentence-transformers"}
    name = names.get(type(provider).__name__)
    model = getattr(provider, "model", getattr(provider, "_model_name", None))
    return f"{name}/{model}" if name and model else f"unknown/{type(provider).__name__}"


def _deserialize_vec(blob: bytes) -> list[float]:
    if not blob or len(blob) % 4:
        raise ValueError("invalid stored float32 embedding")
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _build_header(storage: Storage, agent_id: str | None = None,
                  include_embeddings: bool = True) -> dict:
    conn = storage._get_conn()
    hot = conn.execute("SELECT COUNT(*) FROM hot_memory").fetchone()[0]
    cold = conn.execute("SELECT COUNT(*) FROM cold_memory").fetchone()[0]
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "embedding_backend": _get_embedding_backend_info(),
        # Legacy databases do not record provider/revision/preprocessing for
        # individual vectors. Never relabel them using today's configuration.
        "embedding_space": None,
        "embedding_space_status": "unverified",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "total_hot": hot, "total_cold": cold, "total_count": hot + cold,
        "agent_id": agent_id,  # exporter annotation; NOT a filter/principal
        "include_embeddings": include_embeddings,
    }


def _iter_memories(storage: Storage, target: str | None = None,
                   since: float | None = None, until: float | None = None,
                   tags: list[str] | None = None,
                   include_embeddings: bool = True) -> Iterator[dict]:
    conn = storage._get_conn()
    for layer in ("hot", "cold"):
        clauses, params = [], []
        for field, op, value in (("target", "=", target),
                                 ("created_at", ">=", since),
                                 ("created_at", "<=", until)):
            if value is not None:
                clauses.append(f"{field} {op} ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order = "target, sort_order, created_at, id" if layer == "hot" else "created_at, id"
        for row in conn.execute(f"SELECT * FROM {layer}_memory{where} ORDER BY {order}", params):
            entry = dict(row)
            entry["layer"] = layer
            if layer == "hot":
                yield entry
                continue
            # Keep decoded v1 fields for old readers AND exact stored JSON text
            # for byte-for-byte column recovery (including NULL/whitespace).
            entry["storage_json"] = {k: entry[k] for k in ("tags", "metadata")}
            for key in ("tags", "metadata"):
                if entry[key] is not None:
                    entry[key] = json.loads(entry[key])
            if tags and not any(t in (entry["tags"] or []) for t in tags):
                continue
            if include_embeddings:
                # Explicit absence distinguishes a complete backup from an
                # include_embeddings=False document during conflict checks.
                entry["embedding"] = None
                erow = conn.execute("SELECT * FROM cold_embeddings WHERE memory_id=?",
                                    (entry["id"],)).fetchone()
                if erow is not None:
                    record = dict(erow)
                    entry["embedding"] = _deserialize_vec(record.pop("embedding"))
                    record.pop("memory_id")
                    entry["embedding_model"] = record["model"]
                    entry["embedding_created_at"] = record["created_at"]
                    entry["embedding_record"] = record
                    # Provenance is stored per vector, never inferred from the
                    # provider currently configured on the exporting machine.
                    srow = conn.execute(
                        "SELECT * FROM embedding_spaces WHERE memory_id=?",
                        (entry["id"],)).fetchone()
                    entry["embedding_space_record"] = dict(srow) if srow is not None else None
                    entry["embedding_indexed"] = conn.execute(
                        "SELECT 1 FROM cold_memory_vec WHERE memory_id=?",
                        (entry["id"],)).fetchone() is not None
            yield entry


def export_json(storage: Storage, output: io.IOBase | None = None, *,
                target: str | None = None, since: float | None = None,
                until: float | None = None, tags: list[str] | None = None,
                include_embeddings: bool = True, agent_id: str | None = None,
                jsonlines: bool = False) -> str | None:
    """Export a consistent snapshot. JSONL streams rows, with exact counts.

    agent_id remains an exporter annotation, not an authorization/filter change.
    """
    conn = storage._get_conn()
    conn.execute("SAVEPOINT memory_export")
    try:
        header = _build_header(storage, agent_id, include_embeddings)
        args = (storage, target, since, until, tags)
        counts = {"hot": 0, "cold": 0}
        for mem in _iter_memories(*args, include_embeddings=False):
            counts[mem["layer"]] += 1
        header.update(total_hot=counts["hot"], total_cold=counts["cold"],
                      total_count=sum(counts.values()))
        memories = _iter_memories(*args, include_embeddings=include_embeddings)
        dest = output if output is not None else io.StringIO()
        if jsonlines:
            dest.write(json.dumps({"__header__": header}, allow_nan=False) + "\n")
            for mem in memories:
                dest.write(json.dumps(mem, ensure_ascii=False, allow_nan=False) + "\n")
        else:
            json.dump({"header": header, "memories": list(memories)}, dest,
                      ensure_ascii=False, indent=2, allow_nan=False)
        return None if output is not None else dest.getvalue()
    finally:
        conn.execute("RELEASE SAVEPOINT memory_export")


def export_markdown(storage: Storage, *, target: str | None = None,
                    since: float | None = None, until: float | None = None,
                    tags: list[str] | None = None) -> str:
    """Human-readable view; use JSON/JSONL for a restorable backup."""
    header = _build_header(storage, include_embeddings=False)
    lines = ["# OpenHippo Memory Export", "",
             f"- **Exported at**: {header['exported_at']}",
             f"- **Schema version**: {header['schema_version']}",
             f"- **Total memories**: {header['total_count']}", "", "---", ""]
    for mem in _iter_memories(storage, target, since, until, tags, False):
        title = mem["content"][:60].replace("\n", " ")
        ts = datetime.fromtimestamp(mem["created_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        meta = [f"**Layer**: {mem['layer']}", f"**Target**: {mem['target']}", f"**Created**: {ts}"]
        if mem.get("tags"):
            meta.append(f"**Tags**: {', '.join(mem['tags'])}")
        lines.extend([f"## {title}", "", " | ".join(meta), "", mem["content"], "", "---", ""])
    return "\n".join(lines)


def export_csv(storage: Storage, output: io.IOBase | None = None, *,
               target: str | None = None, since: float | None = None,
               until: float | None = None, tags: list[str] | None = None) -> str | None:
    """Human-readable view; use JSON/JSONL for a restorable backup."""
    fields = ["id", "layer", "target", "content", "source", "tags", "access_count",
              "created_at", "updated_at", "last_accessed", "archived_from", "metadata"]
    dest = output if output is not None else io.StringIO()
    writer = csv.DictWriter(dest, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for mem in _iter_memories(storage, target, since, until, tags, False):
        for key, default in (("tags", []), ("metadata", {})):
            mem[key] = json.dumps(mem.get(key, default))
        writer.writerow(mem)
    return None if output is not None else dest.getvalue()


def _known_space(space: Any) -> bool:
    """The exact core provider_identity v1 SHA-256 contract."""
    return isinstance(space, str) and re.fullmatch(r"v1:[0-9a-f]{64}", space) is not None


def check_embedding_compatibility(header: dict) -> dict:
    """Backend/model strings, including equal 'unknown', are NOT space proof.

    A tag must follow core provider_identity's contract. This
    diagnostic never probes/initializes a provider and never authorizes reembed.
    """
    provider = embedding_module._provider
    current_space = None
    if provider is not None:
        try:
            _, current_space = embedding_module.provider_identity(provider)
        except (ValueError, AttributeError, TypeError):
            pass
    source_space = header.get("embedding_space")
    compatible = (_known_space(source_space) and _known_space(current_space)
                  and source_space == current_space)
    return {"compatible": compatible,
            "export_backend": header.get("embedding_backend", "unknown"),
            "current_backend": _get_embedding_backend_info(),
            "reembed_needed": not compatible,
            "reason": "verified space match" if compatible else "embedding space unverified or different"}
