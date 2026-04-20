"""Dream cycle — sleep-inspired consolidation engine for cold memory.

Stages (PR-1 implements only Recall+Cluster+Preview):
    1. Recall   — fetch candidate cold memories (active, has embedding)
    2. Cluster  — greedy single-link clustering by L2 distance < threshold
    3. Consolidate (PR-2) — merge each cluster into a seed, mark sources as 'consolidated'
    4. Forget (PR-3) — soft-mark low-importance / stale memories as 'dormant'

Design principle: preview() must be SIDE-EFFECT FREE so老王 can dry-run
before any destructive action. Records a 'preview' run in dream_runs for audit.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openhippo.core.storage import Storage

logger = logging.getLogger(__name__)


# ── Tuning constants (overridable via DreamConfig) ──
DEFAULT_L2_THRESHOLD = 0.55   # cosine ≈ 0.85 — engineering sweet spot
DEFAULT_MIN_CLUSTER_SIZE = 2  # singletons are not interesting to consolidate
DEFAULT_MAX_CANDIDATES = 500  # cap per run to bound runtime
DEFAULT_KNN_FETCH = 20        # neighbors per seed during clustering


@dataclass
class DreamConfig:
    l2_threshold: float = DEFAULT_L2_THRESHOLD
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    knn_fetch: int = DEFAULT_KNN_FETCH
    target: str | None = None   # restrict to one target ('memory' / 'user' / None=all)
    enable_forget: bool = False  # PR-3 — off by default per老王 decision

    def to_dict(self) -> dict:
        return {
            "l2_threshold": self.l2_threshold,
            "min_cluster_size": self.min_cluster_size,
            "max_candidates": self.max_candidates,
            "knn_fetch": self.knn_fetch,
            "target": self.target,
            "enable_forget": self.enable_forget,
            "version": "f5-pr1",
        }


@dataclass
class Cluster:
    seed_id: str
    member_ids: list[str] = field(default_factory=list)   # excludes seed
    avg_distance: float = 0.0

    @property
    def size(self) -> int:
        return 1 + len(self.member_ids)

    def all_ids(self) -> list[str]:
        return [self.seed_id, *self.member_ids]


@dataclass
class DreamPreview:
    run_id: str
    candidates_count: int
    clusters: list[Cluster]
    config: dict
    started_at: float
    duration_ms: int

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "candidates_count": self.candidates_count,
            "clusters_count": len(self.clusters),
            "consolidatable_count": sum(c.size for c in self.clusters),
            "config": self.config,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "clusters": [
                {
                    "seed_id": c.seed_id,
                    "member_ids": c.member_ids,
                    "size": c.size,
                    "avg_distance": round(c.avg_distance, 4),
                }
                for c in self.clusters
            ],
        }


class DreamEngine:
    """Pure logic. No autotask scheduling here (PR-3)."""

    def __init__(self, storage: "Storage"):
        self.storage = storage

    # ── Stage 1: Recall ──
    def _recall_candidates(self, cfg: DreamConfig) -> list[dict]:
        """Active cold memories that have embeddings, ordered by oldest-first.

        Older memories are prioritized — the night-shift consolidates the day's
        accumulated thoughts. Newer ones get a chance next cycle.
        """
        conn = self.storage._get_conn()
        sql = """
            SELECT cm.id, cm.target, cm.content, cm.created_at,
                   cm.dream_status, cm.consolidated_into
            FROM cold_memory cm
            INNER JOIN cold_embeddings ce ON ce.memory_id = cm.id
            WHERE COALESCE(cm.dream_status, 'active') = 'active'
              AND cm.consolidated_into IS NULL
        """
        params: list = []
        if cfg.target:
            sql += " AND cm.target = ?"
            params.append(cfg.target)
        sql += " ORDER BY cm.created_at ASC LIMIT ?"
        params.append(cfg.max_candidates)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _get_embedding(self, memory_id: str) -> list[float] | None:
        """Re-hydrate a stored embedding for clustering queries."""
        conn = self.storage._get_conn()
        row = conn.execute(
            "SELECT embedding FROM cold_embeddings WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if not row:
            return None
        import struct
        blob = row[0]
        n = len(blob) // 4
        return list(struct.unpack(f"<{n}f", blob))

    # ── Stage 2: Cluster (greedy single-link) ──
    def _cluster(self, candidates: list[dict], cfg: DreamConfig) -> list[Cluster]:
        """Greedy single-link clustering by vector distance.

        Algorithm:
            assigned = {}
            for each candidate (oldest first):
                if already assigned: skip
                neighbors = vec_search(emb, k=knn_fetch) filtered by L2 < threshold
                          and same target, and not assigned
                if neighbors >= (min_cluster_size - 1):
                    create cluster with this as seed + neighbors as members
                    mark all assigned

        Trade-offs:
        - O(N · log N) via sqlite-vec KNN; far cheaper than full pairwise
        - Greedy: order matters; oldest-first is deliberate
        - Single-link: tight threshold (0.55) prevents chain-merging unrelated topics
        """
        clusters: list[Cluster] = []
        assigned: set[str] = set()
        cand_by_id = {c["id"]: c for c in candidates}

        for cand in candidates:
            cid = cand["id"]
            if cid in assigned:
                continue
            emb = self._get_embedding(cid)
            if emb is None:
                continue

            neighbors = self.storage.vec_search(
                emb, target=cand.get("target"), limit=cfg.knn_fetch
            )

            members: list[str] = []
            distances: list[float] = []
            for nb in neighbors:
                nid = nb.get("id")
                if not nid or nid == cid or nid in assigned or nid not in cand_by_id:
                    continue
                d = nb.get("vec_distance")
                if d is None or d > cfg.l2_threshold:
                    continue
                members.append(nid)
                distances.append(float(d))

            if len(members) + 1 < cfg.min_cluster_size:
                continue

            avg_d = sum(distances) / len(distances) if distances else 0.0
            cluster = Cluster(seed_id=cid, member_ids=members, avg_distance=avg_d)
            clusters.append(cluster)
            assigned.add(cid)
            assigned.update(members)

        return clusters

    # ── Stage 3: Preview (read-only) ──
    def preview(self, cfg: DreamConfig | None = None) -> DreamPreview:
        """Side-effect-free dry-run.

        Records a 'preview' row in dream_runs (audit only — no memory mutation).
        """
        cfg = cfg or DreamConfig()
        run_id = str(uuid.uuid4())
        started = time.time()

        # Audit log entry — preview is observable but reversible (just a row)
        conn = self.storage._get_conn()
        conn.execute(
            """INSERT INTO dream_runs
               (id, started_at, status, config_snapshot)
               VALUES (?, ?, 'preview', ?)""",
            (run_id, _iso(started), json.dumps(cfg.to_dict())),
        )
        conn.commit()

        try:
            candidates = self._recall_candidates(cfg)
            clusters = self._cluster(candidates, cfg)
            duration_ms = int((time.time() - started) * 1000)

            conn.execute(
                """UPDATE dream_runs
                   SET finished_at = ?, candidates_count = ?, clusters_count = ?,
                       consolidated_count = ?, status = 'preview'
                   WHERE id = ?""",
                (
                    _iso(time.time()),
                    len(candidates),
                    len(clusters),
                    sum(c.size for c in clusters),
                    run_id,
                ),
            )
            conn.commit()

            return DreamPreview(
                run_id=run_id,
                candidates_count=len(candidates),
                clusters=clusters,
                config=cfg.to_dict(),
                started_at=started,
                duration_ms=duration_ms,
            )
        except Exception as e:
            conn.execute(
                "UPDATE dream_runs SET status='failed', error=?, finished_at=? WHERE id=?",
                (str(e), _iso(time.time()), run_id),
            )
            conn.commit()
            logger.exception("dream preview failed: %s", e)
            raise

    # ── Run history ──
    def list_runs(self, limit: int = 20) -> list[dict]:
        conn = self.storage._get_conn()
        rows = conn.execute(
            """SELECT * FROM dream_runs ORDER BY started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _iso(ts: float) -> str:
    """ISO-8601 UTC string from epoch seconds."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
