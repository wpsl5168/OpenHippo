"""FastAPI REST API for OpenHippo."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..core.engine import HippoEngine

# Auth removed in v0.4: OpenHippo is local-first.
# For remote deployments, use a reverse proxy with auth (Caddy + Cloudflare Access,
# Nginx + OAuth2-Proxy, Tailscale, WireGuard, etc.). See README → Remote Access.
from ..core.config import get_config, get as cfg_get


logger = logging.getLogger(__name__)

# Global engine instance (initialized in lifespan)
engine: HippoEngine | None = None
_dream_task: asyncio.Task | None = None

# In-memory runtime counters for the auto-scheduler. Persistent run history
# lives in dream_runs; these are process-local and complement that view.
_dream_runtime: dict = {
    "auto_enabled": False,
    "interval_seconds": 0.0,
    "started_at": None,
    "iterations_total": 0,
    "iterations_succeeded": 0,
    "iterations_failed": 0,
    "last_iteration_at": None,
    "last_status": None,
    "last_error": None,
    "last_duration_ms": None,
    "next_run_at": None,
}


async def _dream_autoloop(interval_seconds: float) -> None:
    """Background task: run consolidate() every `interval_seconds`.

    Mirrors老王's vision: "记忆 Agent 全自动无感知运行 (像人脑记忆)".
    Runs forget OFF by default — that's a manual decision per env config.
    Crashes are logged and the loop continues (we never want to silently die).
    """
    import time as _time
    from ..core.dream import DreamConfig, DreamEngine
    # Initial delay so we don't slam the engine right at boot
    await asyncio.sleep(min(60.0, interval_seconds))
    while True:
        _dream_runtime["next_run_at"] = None
        iter_started = _time.time()
        _dream_runtime["iterations_total"] += 1
        try:
            if engine is not None:
                eng = DreamEngine(engine.storage)
                cfg = DreamConfig(enable_forget=False)
                # Run sync consolidate() in a worker thread so we don't block the loop
                result = await asyncio.to_thread(eng.consolidate, cfg)
                _dream_runtime["iterations_succeeded"] += 1
                _dream_runtime["last_status"] = "completed"
                _dream_runtime["last_error"] = None
                _dream_runtime["last_duration_ms"] = result.duration_ms
                logger.info(
                    "auto-dream completed: run=%s candidates=%d clusters=%d "
                    "consolidated=%d duration_ms=%d",
                    result.run_id, result.candidates_count, result.clusters_count,
                    result.consolidated_count, result.duration_ms,
                )
        except Exception as e:
            _dream_runtime["iterations_failed"] += 1
            _dream_runtime["last_status"] = "failed"
            _dream_runtime["last_error"] = str(e)
            _dream_runtime["last_duration_ms"] = int((_time.time() - iter_started) * 1000)
            logger.exception("auto-dream loop iteration failed; will retry next cycle")
        finally:
            _dream_runtime["last_iteration_at"] = _time.time()
            _dream_runtime["next_run_at"] = _time.time() + interval_seconds
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, _dream_task
    engine = HippoEngine()

    # Start background dream loop unless explicitly disabled.
    # Knob: env OPENHIPPO_DREAM_AUTO=0 disables; OPENHIPPO_DREAM_INTERVAL_HOURS overrides default.
    auto_enabled = os.environ.get("OPENHIPPO_DREAM_AUTO", "1").lower() not in {"0", "false", "no"}
    interval_hours = float(os.environ.get("OPENHIPPO_DREAM_INTERVAL_HOURS", "24"))
    if auto_enabled and interval_hours > 0:
        import time as _time
        interval_seconds = interval_hours * 3600.0
        _dream_runtime["auto_enabled"] = True
        _dream_runtime["interval_seconds"] = interval_seconds
        _dream_runtime["started_at"] = _time.time()
        _dream_task = asyncio.create_task(_dream_autoloop(interval_seconds))
        logger.info("auto-dream scheduler started (every %sh)", interval_hours)
    try:
        yield
    finally:
        if _dream_task is not None:
            _dream_task.cancel()
            try:
                await _dream_task
            except (asyncio.CancelledError, Exception):
                pass
        if engine:
            engine.close()


app = FastAPI(
    title="OpenHippo",
    description="🦛 Local-first memory engine for AI Agents",
    version="0.3.0",
    lifespan=lifespan,
)

_conf = get_config()
# NOTE: OpenHippo is a local-first service. It binds to 127.0.0.1 by default
# and ships with NO authentication. If you expose it remotely, put a reverse
# proxy with proper auth (e.g. Caddy + Cloudflare Access, Nginx + OAuth2-Proxy,
# Tailscale, WireGuard) in front of it. See README → Remote Access.


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


@app.get("/v1/overview")
def get_overview():
    """End-user facing aggregate dashboard: totals, 30d activity,
    target/agent distribution. Combines hot+cold (no tier distinction)."""
    return {"data": _engine().overview()}


@app.get("/v1/memories/all")
def memories_all(
    target: str | None = None,
    agent_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    date_from: float | None = None,
    date_to: float | None = None,
):
    """Unified timeline (hot+cold combined) for end-user UI.
    Supports pagination via limit+offset and filtering by target/agent_id/date range.
    date_from/date_to are unix timestamps (inclusive lower, exclusive upper).
    Returns {items, total, has_more} envelope for infinite-scroll UIs.
    """
    e = _engine()
    items = e.unified_timeline(target, agent_id, limit, offset, date_from, date_to)
    total = e.unified_count(target, agent_id, date_from, date_to)
    return {"data": {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
    }}


@app.get("/v1/calendar")
def get_calendar(
    target: str | None = None,
    agent_id: str | None = None,
    days: int = 365,
):
    """Daily memory counts over the past N days (default 365).
    Returns [{date: 'YYYY-MM-DD', count: N}] sorted oldest→newest.
    Used to power the date-range scrubber / heatmap calendar in the UI.
    """
    return {"data": _engine().daily_calendar(target, agent_id, days)}

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
    """Liveness + lightweight metrics (F22 monitoring hooks)."""
    from ..core.embedding import cache_stats
    eng = _engine()
    try:
        conn = eng.storage._get_conn()
        vec_size = conn.execute("SELECT COUNT(*) FROM cold_memory_vec").fetchone()[0]
        cold_size = conn.execute("SELECT COUNT(*) FROM cold_memory").fetchone()[0]
        hot_size = conn.execute("SELECT COUNT(*) FROM hot_memory").fetchone()[0]
    except Exception as e:
        return {"status": "degraded", "version": "0.3.0", "error": str(e)}
    return {
        "status": "ok",
        "version": "0.3.0",
        "metrics": {
            "hot_entries": hot_size,
            "cold_entries": cold_size,
            "vec_entries": vec_size,
            "embed_fail_count": getattr(type(eng), "_embed_fail_count", 0),
            "embedding_cache": cache_stats(),
        },
    }


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


class ImportRequest(BaseModel):
    data: dict = Field(..., description="Exported JSON document (with header + memories)")
    reembed: bool = Field(False, description="Force re-compute embeddings")
    dry_run: bool = Field(False, description="Preview only, don't write")


@app.post("/v1/import")
def import_memories(req: ImportRequest):
    """Import memories from an export file. Detects embedding backend changes."""
    from ..core.importer import import_json
    e = _engine()
    result = import_json(e.storage, req.data, reembed=req.reembed, dry_run=req.dry_run)
    return {"data": result}


# ── F5 Dream — sleep-inspired consolidation ──

class DreamPreviewRequest(BaseModel):
    target: str | None = Field(None, description="Restrict to 'memory' or 'user'; None = all")
    l2_threshold: float = Field(0.55, ge=0.1, le=2.0, description="L2 distance ceiling for clustering")
    min_cluster_size: int = Field(2, ge=2, le=20)
    max_candidates: int = Field(500, ge=1, le=5000)
    knn_fetch: int = Field(20, ge=2, le=100)


@app.post("/v1/dream/preview")
def dream_preview(req: DreamPreviewRequest):
    """Dry-run consolidation: cluster cold memories without mutating any data.

    Records a 'preview' row in dream_runs for audit; clusters are returned in
    the response. Use this before /v1/dream/run to verify what would be merged.
    """
    from ..core.dream import DreamConfig, DreamEngine
    cfg = DreamConfig(
        target=req.target,
        l2_threshold=req.l2_threshold,
        min_cluster_size=req.min_cluster_size,
        max_candidates=req.max_candidates,
        knn_fetch=req.knn_fetch,
    )
    eng = DreamEngine(_engine().storage)
    return {"data": eng.preview(cfg).to_dict()}


@app.get("/v1/dream/runs")
def dream_runs_list(limit: int = 20):
    """List recent dream cycles (preview + actual runs)."""
    from ..core.dream import DreamEngine
    eng = DreamEngine(_engine().storage)
    return {"data": eng.list_runs(limit=limit)}


class DreamRunRequest(BaseModel):
    target: str | None = Field(None, description="Restrict to 'memory' or 'user'; None = all")
    l2_threshold: float = Field(0.55, ge=0.1, le=2.0)
    min_cluster_size: int = Field(2, ge=2, le=20)
    max_candidates: int = Field(500, ge=1, le=5000)
    knn_fetch: int = Field(20, ge=2, le=100)
    enable_forget: bool = Field(False, description="Enable Stage 4 soft decay (default off)")
    forget_threshold: float = Field(1.0, ge=0.0, le=10.0)
    forget_min_age_days: int = Field(7, ge=0, le=365)


@app.post("/v1/dream/run")
def dream_run(req: DreamRunRequest):
    """Execute a real dream cycle that consolidates similar cold memories.

    This MUTATES data: cluster members get dream_status='consolidated' and
    consolidated_into=<seed_id>. Originals are preserved (not deleted) so a
    future restore endpoint can roll back.

    Forget stage (Stage 4) is OFF by default per老王's policy. Opt in with
    enable_forget=true to also soft-mark stale low-value rows as 'dormant'.

    PRD: "记忆 Agent 全自动无感知运行"; this is the manual trigger.
    Auto-scheduling runs every 24h in lifespan (PR-3).
    """
    from ..core.dream import DreamConfig, DreamEngine
    cfg = DreamConfig(
        target=req.target,
        l2_threshold=req.l2_threshold,
        min_cluster_size=req.min_cluster_size,
        max_candidates=req.max_candidates,
        knn_fetch=req.knn_fetch,
        enable_forget=req.enable_forget,
        forget_threshold=req.forget_threshold,
        forget_min_age_days=req.forget_min_age_days,
    )
    eng = DreamEngine(_engine().storage)
    return {"data": eng.consolidate(cfg).to_dict()}


@app.post("/v1/dream/restore/{memory_id}")
def dream_restore(memory_id: str):
    """Reverse a forget/consolidate action — flip the row back to 'active'.

    For consolidated members this strips consolidated_into; the seed remains
    independently visible (we don't time-travel its accumulated importance).
    """
    from ..core.dream import DreamEngine
    eng = DreamEngine(_engine().storage)
    result = eng.restore(memory_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return {"data": result}


@app.get("/v1/dream/runs/{run_id}")
def dream_run_detail(run_id: str):
    """Single dream run + ordered list of dream_actions for full audit trail."""
    from ..core.dream import DreamEngine
    eng = DreamEngine(_engine().storage)
    conn = eng.storage._get_conn()
    run_row = conn.execute(
        "SELECT * FROM dream_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if not run_row:
        raise HTTPException(status_code=404, detail=f"dream run {run_id} not found")
    actions = [
        dict(r) for r in conn.execute(
            "SELECT * FROM dream_actions WHERE dream_run_id = ? ORDER BY id ASC",
            (run_id,),
        ).fetchall()
    ]
    return {"data": {"run": dict(run_row), "actions": actions}}


@app.get("/v1/dream/metrics")
def dream_metrics():
    """Observability snapshot for the F5 Dream subsystem.

    Combines two sources:
      * Persistent metrics from dream_runs (totals, last run, by-status aggregates)
      * Process-local scheduler runtime (iterations, last_status, next_run_at)

    Designed for both human inspection (jq) and machine scraping. Adding new
    fields is safe; clients should treat unknown keys as forward-compat extras.
    """
    from ..core.dream import DreamEngine
    eng = DreamEngine(_engine().storage)
    persistent = eng.metrics()
    runtime = dict(_dream_runtime)
    return {"data": {"persistent": persistent, "scheduler": runtime}}


# ── F20 Audit Web UI (single-page, Alpine.js + Tailwind CDN) ──

_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
if _UI_DIR.exists() and (_UI_DIR / "index.html").exists():
    app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def _root_redirect():
        """Redirect root to the audit UI for convenience."""
        return RedirectResponse(url="/ui/")
