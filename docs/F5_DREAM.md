# F5 Dream — Sleep-Inspired Memory Consolidation

> Status: shipped (PR-1 preview, PR-2 consolidate, PR-3 forget+restore+auto+metrics)
> Default: enabled, runs every 24 h, forget OFF.

OpenHippo's F5 Dream is a background lifecycle for cold memories modeled on
the way human sleep consolidates the day's experiences:

```
   recall ─→ cluster ─→ consolidate ─→ (optional) forget
                                ↑
                          restore (any time)
```

Nothing is ever deleted — rows transition through `dream_status`:

| status        | meaning                                                                  |
|---------------|--------------------------------------------------------------------------|
| `active`      | normal cold row, returned by all searches                                |
| `consolidated`| merged into a cluster seed; hidden from default search but recoverable   |
| `dormant`     | soft-decayed by the forget stage; hidden by default, fully restorable    |

Every state change is written to `dream_actions` with a full snapshot, so
`POST /v1/dream/restore/{id}` can roll any row back at any time.

---

## 1. The four stages

### Stage 1 — Recall
Fetch the most recent `max_candidates` cold rows that have embeddings and
are still `active`. Keeps the working set bounded so a cycle never blows up.

### Stage 2 — Cluster
For each candidate, run a `sqlite-vec` k-NN query. Anything within
`l2_threshold` (default **0.55**) becomes a neighbor. We then run a tiny
union-find to merge transitively-connected neighbors into clusters of size
≥ `min_cluster_size` (default 2).

### Stage 3 — Consolidate (mutating)
For each cluster, the row with the **highest importance** (ties broken by
oldest creation time) becomes the seed. All other members get
`dream_status='consolidated'` and `consolidated_into=<seed_id>`. The seed
inherits the union of tags and the sum of access counts.

### Stage 4 — Forget (off by default)
For rows older than `forget_min_age_days` (default 7), compute:

```
decay = age_days / 30 − access_count * 0.5 − importance * 2
```

If `decay > forget_threshold` (default 1.0), mark `dream_status='dormant'`.
Designed conservatively: recent or valuable memories are protected.

Enable per-call with `enable_forget: true` on `/v1/dream/run`. The
auto-scheduler always runs with forget OFF — manual opt-in only.

---

## 2. REST endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/v1/dream/preview` | Dry run — return clusters, mutate nothing |
| `POST` | `/v1/dream/run` | Real cycle — consolidate (+ optional forget) |
| `POST` | `/v1/dream/restore/{memory_id}` | Flip a row back to `active` |
| `GET`  | `/v1/dream/runs?limit=20` | Recent run history (preview + actual) |
| `GET`  | `/v1/dream/runs/{run_id}` | Single run + ordered audit trail |
| `GET`  | `/v1/dream/metrics` | Persistent + scheduler observability |

All requests/responses use the standard `{ "data": ... }` wrapper.

### Example: preview before mutating

```bash
curl -X POST http://localhost:8200/v1/dream/preview \
  -H 'content-type: application/json' \
  -d '{"l2_threshold": 0.55, "min_cluster_size": 2, "max_candidates": 500}'
```

### Example: trigger a real cycle

```bash
curl -X POST http://localhost:8200/v1/dream/run \
  -H 'content-type: application/json' \
  -d '{"l2_threshold": 0.55, "enable_forget": false}'
```

### Example: restore a consolidated row

```bash
curl -X POST http://localhost:8200/v1/dream/restore/abcd-1234
```

---

## 3. Auto-scheduler

A background asyncio task runs on FastAPI lifespan startup:

* Default cadence: **every 24 hours**, with a 60 s warm-up delay.
* Always runs with `enable_forget=False` — auto-forget is a deliberate
  manual decision per老王's policy.
* Exceptions are logged and the loop continues; one bad cycle never kills
  the scheduler.

| Env var | Default | Effect |
|---------|---------|--------|
| `OPENHIPPO_DREAM_AUTO` | `1` | Set to `0` / `false` to disable the loop entirely |
| `OPENHIPPO_DREAM_INTERVAL_HOURS` | `24` | Override cadence (float; tests use small values) |

---

## 4. Observability — `/v1/dream/metrics`

Two complementary views in a single response:

```jsonc
{
  "data": {
    "persistent": {
      "total_runs": 42,
      "by_status": {
        "completed": {"count": 40, "total_ms": 18342, "avg_ms": 458, "max_ms": 1903},
        "preview":   {"count": 2,  "total_ms": 87,   "avg_ms": 43,  "max_ms": 50}
      },
      "last_run": { "id": "...", "status": "completed", "duration_ms": 412, ... },
      "totals": { "consolidated": 138, "forgotten": 0, "candidates": 4000, "clusters": 67 },
      "actions_total": 138
    },
    "scheduler": {
      "auto_enabled": true,
      "interval_seconds": 86400.0,
      "started_at": 1745140800.0,
      "iterations_total": 3,
      "iterations_succeeded": 3,
      "iterations_failed": 0,
      "last_iteration_at": 1745227200.0,
      "last_status": "completed",
      "last_error": null,
      "last_duration_ms": 412,
      "next_run_at": 1745313600.0
    }
  }
}
```

* `persistent` is computed from `dream_runs` and `dream_actions`, so it
  survives process restarts.
* `scheduler` is process-local — it tells you what the background loop
  has been doing since this server started.

The shape is deliberately flat for easy `jq` / Prometheus scraping.
Adding new keys is forward-compatible; clients should ignore unknowns.

---

## 5. Configuration cheat-sheet

| Knob | Default | Range | Notes |
|------|---------|-------|-------|
| `l2_threshold` | 0.55 | 0.1–2.0 | Lower = stricter clustering |
| `min_cluster_size` | 2 | 2–20 | Singletons never get touched |
| `max_candidates` | 500 | 1–5000 | Per-cycle work cap |
| `knn_fetch` | 20 | 2–100 | Neighbors fetched per candidate |
| `enable_forget` | `false` | bool | Stage 4 opt-in |
| `forget_threshold` | 1.0 | 0–10 | Decay score ceiling |
| `forget_min_age_days` | 7 | 0–365 | Protect recent rows |

---

## 6. Safety properties

1. **Reversible.** Every `consolidate`/`forget` writes a snapshot; restore is one call away.
2. **Bounded.** `max_candidates` + `knn_fetch` cap per-cycle CPU and memory.
3. **Non-blocking.** Sync work runs in `asyncio.to_thread`, the event loop stays responsive.
4. **Auditable.** `dream_runs` + `dream_actions` give a full forensic trail.
5. **Conservative by default.** Forget OFF, 7-day minimum age, importance/access weighted strongly.

---

## 7. Testing

```bash
cd ~/OpenHippo
.venv/bin/python -m pytest tests/test_dream*.py -v
```

Three suites cover Dream end-to-end:

* `tests/test_dream.py` — preview / clustering invariants (10)
* `tests/test_dream_consolidate.py` — mutating consolidate (8)
* `tests/test_dream_pr3.py` — forget / restore / auto-loop (12)
* `tests/test_dream_metrics.py` — metrics endpoint + engine (4)

Total: **34 Dream-specific tests**, part of the full **112-test** suite.
