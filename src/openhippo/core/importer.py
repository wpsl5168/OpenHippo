"""Memory import module — import from export files with reembed support."""

from __future__ import annotations

import json
import logging
from typing import Any

from .embedding import get_embedding
from .export import check_embedding_compatibility
from .storage import Storage

logger = logging.getLogger(__name__)


def import_json(storage: Storage, data: str | dict, *,
                reembed: bool = False, dry_run: bool = False) -> dict:
    """Import memories from JSON or JSONL export.
    
    Args:
        storage: target storage
        data: JSON string or parsed dict (standard JSON format)
        reembed: force re-compute embeddings even if present
        dry_run: preview only, don't write

    Returns:
        {"imported_hot": int, "imported_cold": int, "skipped_dup": int,
         "reembedded": int, "errors": list, "reembed_warning": str|None}
    """
    if isinstance(data, str):
        # Detect JSONL vs JSON
        if data.lstrip().startswith('{"__header__"'):
            return _import_jsonl(storage, data, reembed=reembed, dry_run=dry_run)
        doc = json.loads(data)
    else:
        doc = data

    header = doc.get("header", {})
    memories = doc.get("memories", [])

    # Check embedding compatibility
    compat = check_embedding_compatibility(header)
    reembed_warning = None
    if compat["reembed_needed"] and not reembed:
        reembed_warning = (
            f"Embedding backend mismatch: export used '{compat['export_backend']}', "
            f"current is '{compat['current_backend']}'. "
            f"Imported vectors may not work for semantic search. "
            f"Use reembed=True to re-compute embeddings."
        )
        logger.warning(reembed_warning)

    # Auto-enable reembed if backends differ
    effective_reembed = reembed or compat["reembed_needed"]

    return _do_import(storage, memories, effective_reembed=effective_reembed,
                      dry_run=dry_run, reembed_warning=reembed_warning)


def _import_jsonl(storage: Storage, data: str, *,
                  reembed: bool = False, dry_run: bool = False) -> dict:
    """Import from JSONL format."""
    lines = data.strip().split("\n")
    header = {}
    memories = []

    for line in lines:
        obj = json.loads(line)
        if "__header__" in obj:
            header = obj["__header__"]
        else:
            memories.append(obj)

    compat = check_embedding_compatibility(header)
    reembed_warning = None
    if compat["reembed_needed"] and not reembed:
        reembed_warning = (
            f"Embedding backend mismatch: export used '{compat['export_backend']}', "
            f"current is '{compat['current_backend']}'. Use reembed=True."
        )

    effective_reembed = reembed or compat["reembed_needed"]
    return _do_import(storage, memories, effective_reembed=effective_reembed,
                      dry_run=dry_run, reembed_warning=reembed_warning)


def _do_import(storage: Storage, memories: list[dict], *,
               effective_reembed: bool, dry_run: bool,
               reembed_warning: str | None) -> dict:
    """Core import logic."""
    imported_hot = 0
    imported_cold = 0
    skipped_dup = 0
    reembedded = 0
    errors: list[dict] = []

    for i, mem in enumerate(memories):
        try:
            layer = mem.get("layer", "cold")
            target = mem.get("target", "memory")
            content = mem.get("content", "")

            if not content:
                errors.append({"index": i, "reason": "empty content"})
                continue

            if dry_run:
                if layer == "hot":
                    imported_hot += 1
                else:
                    imported_cold += 1
                continue

            if layer == "hot":
                storage.hot_add(target, content)
                imported_hot += 1
            else:
                result = storage.cold_add(
                    target=target,
                    content=content,
                    source=mem.get("source", "imported"),
                    tags=mem.get("tags", []),
                    metadata=mem.get("metadata", {}),
                )

                # Handle embedding
                if effective_reembed:
                    vec = get_embedding(content)
                    if vec:
                        storage.vec_store(result["id"], vec)
                        reembedded += 1
                elif "embedding" in mem and mem["embedding"]:
                    storage.vec_store(result["id"], mem["embedding"])

                imported_cold += 1

        except Exception as e:
            errors.append({"index": i, "reason": str(e)})

    return {
        "imported_hot": imported_hot,
        "imported_cold": imported_cold,
        "skipped_dup": skipped_dup,
        "reembedded": reembedded,
        "errors": errors,
        "dry_run": dry_run,
        "reembed_warning": reembed_warning,
    }
