"""Dream cycle — sleep-inspired consolidation engine for cold memory.

Stages:
    1. Recall      — fetch candidate cold memories (active, has embedding)
    2. Cluster     — greedy single-link clustering by L2 distance < threshold
    3. Consolidate — merge each cluster into a seed, mark members as 'consolidated' (PR-2)
    4. Forget      — soft-mark low-importance / stale memories as 'dormant' (PR-3, off by default)

Design principles:
- preview() is SIDE-EFFECT FREE — dry-run before any destructive action.
- consolidate() is REVERSIBLE — original rows are not deleted, only marked
  with consolidated_into=<seed_id> and dream_status='consolidated'.
- Every action emits a dream_actions audit row keyed by dream_run_id.
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
    forget_threshold: float = 1.0
    forget_min_age_days: int = 7

    def to_dict(self) -> dict:
        return {
            "l2_threshold": self.l2_threshold,
            "min_cluster_size": self.min_cluster_size,
            "max_candidates": self.max_candidates,
            "knn_fetch": self.knn_fetch,
            "target": self.target,
            "enable_forget": self.enable_forget,
            "forget_threshold": self.forget_threshold,
            "forget_min_age_days": self.forget_min_age_days,
            "version": "f5-pr3",
        }


# ── Forget tuning (PR-3) ──
# decay_score = age_days / 30 - access_count * 0.5 - importance * 2
# Higher score = more forgettable. Threshold above which a row goes dormant.
DEFAULT_FORGET_THRESHOLD = 1.0   # conservative: ~30 days untouched + low importance
DEFAULT_FORGET_MIN_AGE_DAYS = 7  # never forget anything younger than a week


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


@dataclass
class DreamRunResult:
    """Outcome of a real (non-preview) dream cycle."""
    run_id: str
    candidates_count: int
    clusters_count: int
    consolidated_count: int        # number of member rows merged into seeds
    seeds_updated: int             # number of seed rows mutated
    forgotten_count: int           # PR-3 (always 0 in PR-2)
    duration_ms: int
    started_at: float
    config: dict

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "candidates_count": self.candidates_count,
            "clusters_count": self.clusters_count,
            "consolidated_count": self.consolidated_count,
            "seeds_updated": self.seeds_updated,
            "forgotten_count": self.forgotten_count,
            "duration_ms": self.duration_ms,
            "started_at": self.started_at,
            "config": self.config,
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

    # ── Stage 3: Consolidate (mutating) ──
    def consolidate(self, cfg: DreamConfig | None = None) -> DreamRunResult:
        """Run a full dream cycle that actually mutates cold memory.

        Process per cluster:
          1. Pick seed (oldest in cluster — has earliest created_at).
             Rationale: oldest is the original observation; later are echoes.
          2. For each member:
               - mark dream_status='consolidated', consolidated_into=<seed_id>
               - record dream_actions row with full snapshot for restore
          3. Update seed:
               - merged_from = JSON list of member ids (appended if exists)
               - importance += sum(member_importance) * 0.3 (capped at 1.0)
               - access_count += sum(member_access_count)
               - last_dream_at = now
               - dream_actions row recording the merge

        Failure handling: each cluster commits independently. A single bad
        cluster does not poison the whole run — error logged + skipped.

        Reversibility: nothing is deleted. consolidated rows can be restored
        by setting dream_status='active' and consolidated_into=NULL (PR-3 API).
        """
        cfg = cfg or DreamConfig()
        run_id = str(uuid.uuid4())
        started = time.time()
        conn = self.storage._get_conn()

        # Audit: open a 'running' run row
        conn.execute(
            """INSERT INTO dream_runs
               (id, started_at, status, config_snapshot)
               VALUES (?, ?, 'running', ?)""",
            (run_id, _iso(started), json.dumps(cfg.to_dict())),
        )
        conn.commit()

        consolidated_count = 0
        seeds_updated = 0
        forgotten_count = 0
        candidates: list[dict] = []
        clusters: list[Cluster] = []

        try:
            candidates = self._recall_candidates(cfg)
            clusters = self._cluster(candidates, cfg)

            for cluster in clusters:
                try:
                    n_merged = self._apply_consolidation(run_id, cluster)
                    if n_merged > 0:
                        consolidated_count += n_merged
                        seeds_updated += 1
                except Exception as cluster_err:
                    logger.exception(
                        "dream consolidate: cluster %s failed: %s",
                        cluster.seed_id, cluster_err,
                    )
                    # don't break the run

            # Stage 4 — only when explicitly enabled
            if cfg.enable_forget:
                try:
                    forgotten_count = self._apply_forget(run_id, cfg)
                except Exception as forget_err:
                    logger.exception("dream forget stage failed: %s", forget_err)

            duration_ms = int((time.time() - started) * 1000)
            conn.execute(
                """UPDATE dream_runs
                   SET finished_at = ?, status = 'completed',
                       candidates_count = ?, clusters_count = ?,
                       consolidated_count = ?, forgotten_count = ?
                   WHERE id = ?""",
                (
                    _iso(time.time()),
                    len(candidates),
                    len(clusters),
                    consolidated_count,
                    forgotten_count,
                    run_id,
                ),
            )
            conn.commit()

            return DreamRunResult(
                run_id=run_id,
                candidates_count=len(candidates),
                clusters_count=len(clusters),
                consolidated_count=consolidated_count,
                seeds_updated=seeds_updated,
                forgotten_count=forgotten_count,
                duration_ms=duration_ms,
                started_at=started,
                config=cfg.to_dict(),
            )
        except Exception as e:
            conn.execute(
                "UPDATE dream_runs SET status='failed', error=?, finished_at=? WHERE id=?",
                (str(e), _iso(time.time()), run_id),
            )
            conn.commit()
            logger.exception("dream consolidate failed: %s", e)
            raise

    def _apply_consolidation(self, run_id: str, cluster: Cluster) -> int:
        """Merge one cluster atomically. Returns number of members consolidated."""
        conn = self.storage._get_conn()

        # Fetch full rows so we can decide seed by created_at and capture snapshots
        all_ids = cluster.all_ids()
        placeholders = ",".join("?" for _ in all_ids)
        rows = {
            r["id"]: dict(r)
            for r in conn.execute(
                f"""SELECT id, content, target, created_at, importance, access_count,
                           merged_from, dream_status, consolidated_into
                    FROM cold_memory WHERE id IN ({placeholders})""",
                all_ids,
            ).fetchall()
        }

        # Filter out anyone already consolidated by a prior race
        live = {
            mid: r for mid, r in rows.items()
            if (r.get("dream_status") or "active") == "active"
            and not r.get("consolidated_into")
        }
        if len(live) < 2:
            return 0  # nothing to merge

        # Re-elect seed: oldest among live members (PRD: "保留种子：创建最早的为代表")
        seed_id = min(live, key=lambda i: live[i]["created_at"] or 0)
        seed = live[seed_id]
        member_ids = [mid for mid in live if mid != seed_id]
        if not member_ids:
            return 0

        now_iso = _iso(time.time())

        # 1. Mark members as consolidated + audit
        for mid in member_ids:
            member = live[mid]
            conn.execute(
                """UPDATE cold_memory
                   SET dream_status = 'consolidated',
                       consolidated_into = ?,
                       last_dream_at = ?
                   WHERE id = ?""",
                (seed_id, now_iso, mid),
            )
            conn.execute(
                """INSERT INTO dream_actions
                   (dream_run_id, action, memory_id, details, created_at)
                   VALUES (?, 'consolidate_member', ?, ?, ?)""",
                (
                    run_id, mid,
                    json.dumps({
                        "merged_into": seed_id,
                        "snapshot": {
                            "content": member["content"],
                            "target": member["target"],
                            "importance": member.get("importance"),
                            "access_count": member.get("access_count"),
                            "created_at": member.get("created_at"),
                        },
                        "cluster_size": len(live),
                        "avg_distance": cluster.avg_distance,
                    }),
                    now_iso,
                ),
            )

        # 2. Update seed: merged_from, importance, access_count
        existing_merged: list[str] = []
        if seed.get("merged_from"):
            try:
                existing_merged = json.loads(seed["merged_from"])
                if not isinstance(existing_merged, list):
                    existing_merged = []
            except (ValueError, TypeError):
                existing_merged = []
        new_merged = list(dict.fromkeys(existing_merged + member_ids))  # de-dup, preserve order

        member_importance_sum = sum(
            float(live[m].get("importance") or 0.5) for m in member_ids
        )
        member_access_sum = sum(
            int(live[m].get("access_count") or 0) for m in member_ids
        )
        seed_imp = float(seed.get("importance") or 0.5)
        new_importance = min(1.0, seed_imp + member_importance_sum * 0.3)
        new_access = int(seed.get("access_count") or 0) + member_access_sum

        conn.execute(
            """UPDATE cold_memory
               SET merged_from = ?,
                   importance = ?,
                   access_count = ?,
                   last_dream_at = ?
               WHERE id = ?""",
            (json.dumps(new_merged), new_importance, new_access, now_iso, seed_id),
        )
        conn.execute(
            """INSERT INTO dream_actions
               (dream_run_id, action, memory_id, details, created_at)
               VALUES (?, 'consolidate_seed', ?, ?, ?)""",
            (
                run_id, seed_id,
                json.dumps({
                    "absorbed": member_ids,
                    "importance_before": seed_imp,
                    "importance_after": new_importance,
                    "access_count_after": new_access,
                }),
                now_iso,
            ),
        )
        conn.commit()
        return len(member_ids)

    # ── Stage 4: Forget (soft decay) ──
    def _apply_forget(self, run_id: str, cfg: DreamConfig) -> int:
        """Mark stale, low-importance, rarely-accessed rows as 'dormant'.

        decay_score = age_days/30 - access_count*0.5 - importance*2
        Above forget_threshold → dormant. Reversible: restore() flips it back.

        Honored guards:
        - Only `dream_status='active'` rows (don't re-forget consolidated)
        - Skip rows younger than forget_min_age_days (default 7d)
        - cfg.target filter respected
        """
        conn = self.storage._get_conn()
        now = time.time()
        min_age_seconds = cfg.forget_min_age_days * 86400.0

        sql = """
            SELECT id, content, target, created_at, importance, access_count
            FROM cold_memory
            WHERE COALESCE(dream_status, 'active') = 'active'
              AND consolidated_into IS NULL
              AND created_at IS NOT NULL
              AND (? - created_at) >= ?
        """
        params: list = [now, min_age_seconds]
        if cfg.target:
            sql += " AND target = ?"
            params.append(cfg.target)

        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        forgotten = 0
        now_iso = _iso(now)

        for r in rows:
            age_days = max(0.0, (now - float(r["created_at"])) / 86400.0)
            access = int(r.get("access_count") or 0)
            importance = float(r.get("importance") or 0.5)
            decay = age_days / 30.0 - access * 0.5 - importance * 2.0
            if decay <= cfg.forget_threshold:
                continue

            conn.execute(
                """UPDATE cold_memory
                   SET dream_status = 'dormant', last_dream_at = ?
                   WHERE id = ? AND COALESCE(dream_status, 'active') = 'active'""",
                (now_iso, r["id"]),
            )
            conn.execute(
                """INSERT INTO dream_actions
                   (dream_run_id, action, memory_id, details, created_at)
                   VALUES (?, 'forget', ?, ?, ?)""",
                (
                    run_id, r["id"],
                    json.dumps({
                        "decay_score": round(decay, 4),
                        "age_days": round(age_days, 2),
                        "access_count": access,
                        "importance": importance,
                        "threshold": cfg.forget_threshold,
                    }),
                    now_iso,
                ),
            )
            forgotten += 1

        conn.commit()
        return forgotten

    # ── Restore (reversal of forget / consolidate) ──
    def restore(self, memory_id: str) -> dict:
        """Flip a 'dormant' or 'consolidated' row back to 'active'.

        For consolidated rows: also strips consolidated_into, but does NOT
        attempt to undo the seed's accumulated importance/access_count/merged_from
        — that would require time-travel of the audit chain. The seed continues
        to exist independently; restoring a member just makes both visible.
        """
        conn = self.storage._get_conn()
        row = conn.execute(
            "SELECT id, dream_status, consolidated_into FROM cold_memory WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if not row:
            return {"error": f"Memory {memory_id} not found"}

        prev_status = row["dream_status"] or "active"
        prev_seed = row["consolidated_into"]
        if prev_status == "active" and not prev_seed:
            return {"id": memory_id, "status": "noop", "message": "already active"}

        now_iso = _iso(time.time())
        conn.execute(
            """UPDATE cold_memory
               SET dream_status = 'active',
                   consolidated_into = NULL,
                   last_dream_at = ?
               WHERE id = ?""",
            (now_iso, memory_id),
        )
        # Audit row (no run_id since this is a manual op — use a synthetic run)
        # Open a one-shot 'restore' run for FK integrity & history visibility.
        run_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO dream_runs
               (id, started_at, finished_at, status, config_snapshot,
                candidates_count, clusters_count, consolidated_count, forgotten_count)
               VALUES (?, ?, ?, 'restored', '{"manual":true}', 0, 0, 0, 0)""",
            (run_id, now_iso, now_iso),
        )
        conn.execute(
            """INSERT INTO dream_actions
               (dream_run_id, action, memory_id, details, created_at)
               VALUES (?, 'restore', ?, ?, ?)""",
            (
                run_id, memory_id,
                json.dumps({
                    "prev_status": prev_status,
                    "prev_consolidated_into": prev_seed,
                }),
                now_iso,
            ),
        )
        conn.commit()
        return {
            "id": memory_id,
            "status": "restored",
            "from": prev_status,
            "run_id": run_id,
        }

    # ── Run history ──
    def list_runs(self, limit: int = 20) -> list[dict]:
        conn = self.storage._get_conn()
        rows = conn.execute(
            """SELECT * FROM dream_runs ORDER BY started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Metrics ──
    def metrics(self) -> dict:
        """Aggregate health/observability metrics for the F5 Dream subsystem.

        Pulls from dream_runs (durable history) so it survives process restarts.
        Returned shape is intentionally flat for easy Prometheus/JSON scraping.
        """
        conn = self.storage._get_conn()
        # SQLite stores timestamps as ISO-8601 strings; convert to epoch ms
        # via julianday() arithmetic so we can aggregate durations.
        # 86_400_000 ms/day; round to int.
        dur_expr = (
            "CAST((julianday(finished_at) - julianday(started_at)) * 86400000 "
            "AS INTEGER)"
        )

        rows = conn.execute(
            f"""SELECT status, COUNT(*) AS n,
                       COALESCE(SUM(CASE WHEN finished_at IS NOT NULL THEN {dur_expr} END), 0) AS total_ms,
                       COALESCE(AVG(CASE WHEN finished_at IS NOT NULL THEN {dur_expr} END), 0) AS avg_ms,
                       COALESCE(MAX(CASE WHEN finished_at IS NOT NULL THEN {dur_expr} END), 0) AS max_ms
                  FROM dream_runs
                  GROUP BY status"""
        ).fetchall()
        by_status: dict[str, dict] = {}
        total_runs = 0
        for r in rows:
            d = dict(r)
            by_status[d["status"]] = {
                "count": d["n"],
                "total_ms": int(d["total_ms"] or 0),
                "avg_ms": int(d["avg_ms"] or 0),
                "max_ms": int(d["max_ms"] or 0),
            }
            total_runs += d["n"]

        # Last run snapshot (any status)
        last = conn.execute(
            f"""SELECT id, started_at, finished_at, status, error,
                       candidates_count, clusters_count,
                       consolidated_count, forgotten_count,
                       CASE WHEN finished_at IS NOT NULL THEN {dur_expr} END AS duration_ms
                  FROM dream_runs ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
        last_dict = dict(last) if last else None

        # Workload totals
        totals = conn.execute(
            """SELECT COALESCE(SUM(consolidated_count),0) AS consolidated,
                      COALESCE(SUM(forgotten_count),0)    AS forgotten,
                      COALESCE(SUM(candidates_count),0)   AS candidates,
                      COALESCE(SUM(clusters_count),0)     AS clusters
                 FROM dream_runs WHERE status='completed'"""
        ).fetchone()

        # Audit-trail size
        action_total = conn.execute(
            "SELECT COUNT(*) FROM dream_actions"
        ).fetchone()[0]

        return {
            "total_runs": total_runs,
            "by_status": by_status,
            "last_run": last_dict,
            "totals": dict(totals) if totals else {},
            "actions_total": action_total,
        }


def _iso(ts: float) -> str:
    """ISO-8601 UTC string from epoch seconds."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
