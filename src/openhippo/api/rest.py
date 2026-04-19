"""FastAPI REST API for OpenHippo."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..core.engine import HippoEngine

app = FastAPI(
    title="OpenHippo",
    description="🦛 Local-first memory engine for AI Agents",
    version="0.2.0",
)

# Auth middleware (loaded from config)
from ..core.config import get_config, get as cfg_get
from .auth import BearerAuthMiddleware

_conf = get_config()
app.add_middleware(
    BearerAuthMiddleware,
    enabled=cfg_get(_conf, "auth.enabled", False),
    tokens=cfg_get(_conf, "auth.tokens", []),
)

# Global engine instance (initialized on startup)
engine: HippoEngine | None = None


@app.on_event("startup")
def startup():
    global engine
    engine = HippoEngine()


@app.on_event("shutdown")
def shutdown():
    if engine:
        engine.close()


def _engine() -> HippoEngine:
    if engine is None:
        raise HTTPException(500, "Engine not initialized")
    return engine


# ── Request/Response Models ──

class AddRequest(BaseModel):
    target: str = Field("memory", description="'memory' or 'user'")
    content: str = Field(..., description="Memory content")

class ReplaceRequest(BaseModel):
    target: str = Field("memory")
    old_text: str = Field(..., description="Unique substring to identify entry")
    new_content: str = Field(..., description="Replacement content")

class RemoveRequest(BaseModel):
    target: str = Field("memory")
    old_text: str = Field(..., description="Unique substring to identify entry")

class SearchRequest(BaseModel):
    query: str
    target: str | None = None
    source: str = Field("all", description="'all', 'hot', 'cold'")
    limit: int = Field(20, ge=1, le=100)
    mode: str = Field("hybrid", description="'hybrid' (FTS+vec RRF), 'fts', 'vector'")

class ArchiveRequest(BaseModel):
    target: str = Field("memory")
    old_text: str = Field(..., description="Unique substring to identify hot entry")

class PromoteRequest(BaseModel):
    memory_id: str = Field(..., description="Cold memory ID to promote")


# ── Endpoints ──

@app.post("/v1/memories")
def add_memory(req: AddRequest):
    return {"data": _engine().add(req.target, req.content)}

@app.post("/v1/memories/search")
def search_memories(req: SearchRequest):
    return {"data": _engine().search(req.query, req.target, req.source, req.limit, req.mode)}

@app.post("/v1/memories/replace")
def replace_memory(req: ReplaceRequest):
    result = _engine().replace(req.target, req.old_text, req.new_content)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return {"data": result}

@app.post("/v1/memories/remove")
def remove_memory(req: RemoveRequest):
    result = _engine().remove(req.target, req.old_text)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return {"data": result}

@app.post("/v1/memories/archive")
def archive_memory(req: ArchiveRequest):
    result = _engine().archive(req.target, req.old_text)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return {"data": result}

@app.post("/v1/memories/promote")
def promote_memory(req: PromoteRequest):
    result = _engine().promote(req.memory_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return {"data": result}

@app.get("/v1/memories/hot")
def get_hot(target: str | None = None):
    if target:
        return {"data": _engine().get_hot(target)}
    return {"data": {
        "memory": _engine().get_hot("memory"),
        "user": _engine().get_hot("user"),
    }}

@app.get("/v1/stats")
def get_stats():
    return {"data": _engine().stats()}

@app.get("/v1/logs")
def get_logs(limit: int = 50):
    return {"data": _engine().get_logs(limit)}

class UpdateMemoryRequest(BaseModel):
    content: str = Field(..., description="New content for the memory")


# ── Audit APIs (CRUD by ID + timeline) ──

@app.get("/v1/memories/timeline")
def memory_timeline(target: str | None = None, limit: int = 50, offset: int = 0):
    """Browse cold memories ordered by creation time (newest first)."""
    return {"data": _engine().cold_timeline(target, limit, offset)}


@app.get("/v1/memories/{memory_id}")
def get_memory(memory_id: str):
    """Get a single memory by ID (checks hot first, then cold)."""
    e = _engine()
    # Check hot
    for target in ("memory", "user"):
        for entry in e.get_hot(target):
            if entry["id"] == memory_id:
                return {"data": {**entry, "source": "hot"}}
    # Check cold
    cold = e.cold_get(memory_id)
    if cold:
        return {"data": {**cold, "source": "cold"}}
    raise HTTPException(404, f"Memory {memory_id} not found")


@app.put("/v1/memories/{memory_id}")
def update_memory(memory_id: str, req: UpdateMemoryRequest):
    """Update a cold memory by ID."""
    result = _engine().cold_update(memory_id, req.content)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return {"data": result}


@app.delete("/v1/memories/{memory_id}")
def delete_memory(memory_id: str):
    """Delete a cold memory by ID."""
    result = _engine().cold_delete(memory_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return {"data": result}


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.2.0"}


@app.get("/v1/export")
def export_memories(
    format: str = "json",
    target: str | None = None,
    agent_id: str | None = None,
    since: float | None = None,
    until: float | None = None,
    tags: str | None = None,
    include_embeddings: bool = True,
):
    """Export all memories as JSON, JSONL, Markdown, or CSV.
    
    One-click full export with schema versioning for zero-lock-in portability.
    """
    import io
    from ..core.export import export_json, export_markdown, export_csv

    e = _engine()
    tag_list = [t.strip() for t in tags.split(",")] if tags else None

    if format == "json":
        content = export_json(
            e.storage, target=target, since=since, until=until,
            tags=tag_list, include_embeddings=include_embeddings,
            agent_id=agent_id,
        )
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=memories.json"},
        )
    elif format == "jsonl":
        buf = io.StringIO()
        export_json(
            e.storage, output=buf, target=target, since=since, until=until,
            tags=tag_list, include_embeddings=include_embeddings,
            agent_id=agent_id, jsonlines=True,
        )
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=memories.jsonl"},
        )
    elif format == "markdown" or format == "md":
        content = export_markdown(
            e.storage, target=target, since=since, until=until, tags=tag_list,
        )
        return StreamingResponse(
            iter([content]),
            media_type="text/markdown",
            headers={"Content-Disposition": "attachment; filename=memories.md"},
        )
    elif format == "csv":
        buf = io.StringIO()
        export_csv(
            e.storage, output=buf, target=target, since=since, until=until, tags=tag_list,
        )
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=memories.csv"},
        )
    else:
        raise HTTPException(400, f"Unsupported format: {format}. Use json, jsonl, markdown, or csv.")

@app.post("/v1/embeddings/backfill")
def backfill_embeddings():
    """Generate embeddings for all cold memories missing them."""
    return {"data": _engine().embed_all_cold()}
