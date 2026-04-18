"""FastAPI REST API for OpenHippo."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..core.engine import HippoEngine

app = FastAPI(
    title="OpenHippo",
    description="🦛 Local-first memory engine for AI Agents",
    version="0.1.0",
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
    return {"data": _engine().search(req.query, req.target, req.source, req.limit)}

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

@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}
