"""Memory export module — one-click full export with schema versioning."""

from __future__ import annotations

import csv
import io
import json
import struct
import time
from datetime import datetime, timezone
from typing import Iterator, Any

from .embedding import get_provider
from .storage import Storage

# Schema version for export format — bump on breaking changes
EXPORT_SCHEMA_VERSION = "1.0"


def _get_embedding_backend_info() -> str:
    """Get a string identifying the current embedding backend + model."""
    provider = get_provider()
    cls_name = type(provider).__name__
    if cls_name == "OllamaProvider":
        return f"ollama/{provider.model}"
    elif cls_name == "SentenceTransformerProvider":
        return f"sentence-transformers/{provider._model_name}"
    return f"unknown/{cls_name}"


def _deserialize_vec(blob: bytes) -> list[float]:
    """Deserialize sqlite-vec blob to float list."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _build_header(storage: Storage, agent_id: str | None = None,
                  include_embeddings: bool = True) -> dict:
    """Build export file header with metadata."""
    conn = storage._get_conn()
    hot_count = conn.execute("SELECT COUNT(*) FROM hot_memory").fetchone()[0]
    cold_count = conn.execute("SELECT COUNT(*) FROM cold_memory").fetchone()[0]
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "embedding_backend": _get_embedding_backend_info(),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "total_hot": hot_count,
        "total_cold": cold_count,
        "total_count": hot_count + cold_count,
        "agent_id": agent_id,
        "include_embeddings": include_embeddings,
    }


def _iter_memories(storage: Storage, target: str | None = None,
                   since: float | None = None, until: float | None = None,
                   tags: list[str] | None = None,
                   include_embeddings: bool = True) -> Iterator[dict]:
    """Iterate all memories (hot + cold) with optional filters. Streaming — one row at a time."""
    conn = storage._get_conn()

    # Hot memories
    if target:
        hot_rows = conn.execute(
            "SELECT * FROM hot_memory WHERE target=? ORDER BY sort_order, created_at",
            (target,)).fetchall()
    else:
        hot_rows = conn.execute(
            "SELECT * FROM hot_memory ORDER BY target, sort_order, created_at").fetchall()

    for row in hot_rows:
        d = dict(row)
        if since and d["created_at"] < since:
            continue
        if until and d["created_at"] > until:
            continue
        yield {
            "id": d["id"],
            "layer": "hot",
            "target": d["target"],
            "content": d["content"],
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
            "sort_order": d.get("sort_order", 0),
            "metadata": {},
        }

    # Cold memories — iterate without loading all into memory
    where_clauses = []
    params: list[Any] = []
    if target:
        where_clauses.append("cm.target = ?")
        params.append(target)
    if since:
        where_clauses.append("cm.created_at >= ?")
        params.append(since)
    if until:
        where_clauses.append("cm.created_at <= ?")
        params.append(until)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    if include_embeddings:
        sql = f"""SELECT cm.*, ce.embedding AS embedding_blob, ce.model AS embedding_model
                  FROM cold_memory cm
                  LEFT JOIN cold_embeddings ce ON ce.memory_id = cm.id
                  {where_sql}
                  ORDER BY cm.created_at"""
    else:
        sql = f"""SELECT cm.* FROM cold_memory cm {where_sql} ORDER BY cm.created_at"""

    cursor = conn.execute(sql, params)
    while True:
        row = cursor.fetchone()
        if row is None:
            break
        d = dict(row)

        # Tag filter (post-query since tags is JSON)
        if tags:
            entry_tags = json.loads(d.get("tags", "[]"))
            if not any(t in entry_tags for t in tags):
                continue

        entry: dict[str, Any] = {
            "id": d["id"],
            "layer": "cold",
            "target": d["target"],
            "content": d["content"],
            "source": d.get("source", "manual"),
            "tags": json.loads(d.get("tags", "[]")),
            "access_count": d.get("access_count", 0),
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
            "last_accessed": d.get("last_accessed"),
            "archived_from": d.get("archived_from"),
            "metadata": json.loads(d.get("metadata", "{}")),
        }

        if include_embeddings and d.get("embedding_blob"):
            entry["embedding"] = _deserialize_vec(d["embedding_blob"])
            entry["embedding_model"] = d.get("embedding_model", "unknown")

        yield entry


def export_json(storage: Storage, output: io.IOBase | None = None, *,
                target: str | None = None, since: float | None = None,
                until: float | None = None, tags: list[str] | None = None,
                include_embeddings: bool = True,
                agent_id: str | None = None,
                jsonlines: bool = False) -> str | None:
    """Export memories as JSON or JSON Lines.
    
    If output is provided, writes streaming to it and returns None.
    Otherwise returns the full JSON string.
    """
    header = _build_header(storage, agent_id, include_embeddings)
    memories = _iter_memories(storage, target, since, until, tags, include_embeddings)

    if jsonlines:
        # JSON Lines: header on first line, then one memory per line
        if output:
            output.write(json.dumps({"__header__": header}) + "\n")
            count = 0
            for mem in memories:
                output.write(json.dumps(mem, ensure_ascii=False) + "\n")
                count += 1
            return None
        else:
            lines = [json.dumps({"__header__": header})]
            for mem in memories:
                lines.append(json.dumps(mem, ensure_ascii=False))
            return "\n".join(lines) + "\n"
    else:
        # Standard JSON with header
        mem_list = list(memories)
        header["total_count"] = len(mem_list)
        doc = {"header": header, "memories": mem_list}
        if output:
            json.dump(doc, output, ensure_ascii=False, indent=2)
            return None
        return json.dumps(doc, ensure_ascii=False, indent=2)


def export_markdown(storage: Storage, *, target: str | None = None,
                    since: float | None = None, until: float | None = None,
                    tags: list[str] | None = None) -> str:
    """Export memories as human-readable Markdown."""
    header = _build_header(storage, include_embeddings=False)
    lines = [
        f"# OpenHippo Memory Export",
        f"",
        f"- **Exported at**: {header['exported_at']}",
        f"- **Schema version**: {header['schema_version']}",
        f"- **Total memories**: {header['total_count']}",
        f"",
        f"---",
        f"",
    ]

    for mem in _iter_memories(storage, target, since, until, tags, include_embeddings=False):
        title = mem["content"][:60].replace("\n", " ")
        ts = datetime.fromtimestamp(mem["created_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        layer = mem["layer"]
        tgt = mem["target"]
        mem_tags = mem.get("tags", [])

        lines.append(f"## {title}")
        lines.append(f"")
        meta_parts = [f"**Layer**: {layer}", f"**Target**: {tgt}", f"**Created**: {ts}"]
        if mem_tags:
            meta_parts.append(f"**Tags**: {', '.join(mem_tags)}")
        lines.append(" | ".join(meta_parts))
        lines.append(f"")
        lines.append(mem["content"])
        lines.append(f"")
        lines.append(f"---")
        lines.append(f"")

    return "\n".join(lines)


def export_csv(storage: Storage, output: io.IOBase | None = None, *,
               target: str | None = None, since: float | None = None,
               until: float | None = None, tags: list[str] | None = None) -> str | None:
    """Export memories as CSV."""
    fieldnames = ["id", "layer", "target", "content", "source", "tags",
                  "access_count", "created_at", "updated_at", "last_accessed",
                  "archived_from", "metadata"]

    if output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for mem in _iter_memories(storage, target, since, until, tags, include_embeddings=False):
            mem["tags"] = json.dumps(mem.get("tags", []))
            mem["metadata"] = json.dumps(mem.get("metadata", {}))
            writer.writerow(mem)
        return None
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for mem in _iter_memories(storage, target, since, until, tags, include_embeddings=False):
            mem["tags"] = json.dumps(mem.get("tags", []))
            mem["metadata"] = json.dumps(mem.get("metadata", {}))
            writer.writerow(mem)
        return buf.getvalue()


def check_embedding_compatibility(header: dict) -> dict:
    """Check if exported embedding backend matches current config.
    
    Returns:
        {"compatible": bool, "export_backend": str, "current_backend": str, "reembed_needed": bool}
    """
    export_backend = header.get("embedding_backend", "unknown")
    current_backend = _get_embedding_backend_info()

    # Exact match = compatible
    if export_backend == current_backend:
        return {
            "compatible": True,
            "export_backend": export_backend,
            "current_backend": current_backend,
            "reembed_needed": False,
        }

    # Same model family but different runner (ollama vs sentence-transformers)
    # Vectors NOT compatible — cosine similarity only 0.71-0.94
    return {
        "compatible": False,
        "export_backend": export_backend,
        "current_backend": current_backend,
        "reembed_needed": True,
    }
