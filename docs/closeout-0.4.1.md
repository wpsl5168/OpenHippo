# 0.4.1 — remaining audit fixes

## Retrieval

- Migrate the existing broad FTS update trigger atomically. Only an actual content/tags change updates the text index; no full index rebuild or memory rewrite is required.
- Access accounting is best-effort and batched over final result IDs. A busy writer or an existing caller transaction skips accounting rather than failing the read or committing somebody else's transaction. Counts may under-report during contention; they are not a financial/audit ledger.
- Vector search expands a KNN prefix until exhaustion or a strictly farther boundary proves completeness, including tied distances. If the 4096 ceiling prevents proof, or MATCH raises OperationalError, it uses an unlimited exact scalar fallback. The fallback filters lifecycle/metadata before LIMIT, calculates distance once and sorts only ID/distance; full rows are fetched in bounded batches.
- A distance threshold may be applied to the already sorted prefix: once a nearest eligible item exceeds the threshold, every later item also exceeds it. This is not the previous unsafe post-LIMIT lifecycle filtering.
- Optional storage-level scope/agent filters are exact metadata filters, not an authentication or tenant-visibility model. No ACL was added to the API.
- The implementation uses sqlite-vec 0.1.9 exact L2. It is not ANN and does not silently truncate results at a fixed candidate multiplier or the KNN ceiling. Worst-case filtering or tied-distance distributions still require a full scan; latency depends on corpus and load.

## Failed-job reconciliation

```sh
# Read-only by default; no Storage initialization, inference or migration.
python -m openhippo.reconcile --db /path/to/memory.db \
  --model '<verified model>' --space-id '<verified v1:... space>'

# Only after a backup and review of every selected job.
python -m openhippo.reconcile --db /path/to/memory.db \
  --model '<verified model>' --space-id '<verified v1:... space>' \
  --apply --actor '<operator>' --reason '<review rationale>'
```

The v2 policy accepts apply only for already exhausted failed jobs (attempts >= 5). It rejects retryable jobs without modifying their attempts or creating a false old-worker-safe hold. Apply is one short transaction per job; a mixed batch can partially apply and return exit code 2. Read `outcomes`, `has_more`, and the pagination cursor rather than treating command execution as full completion.

Old status/error/attempts and memory/vector data remain unchanged. Audit entries retain the original job fields. `cold_update` supersedes pending/running jobs but preserves historical failed rows. New actual queue completions write atomic receipts linking revision, vector and space; the reconciler never manufactures receipts for old vectors.

A vector's existence and a matching space label are not proof of its input revision. Historical jobs without sufficient receipts remain `needs_confirmation` and remain in `failed_unresolved`; do not convert them to done to make a chart green. Re-running inference or accepting manual evidence requires a separate explicit operator workflow.

## Configuration and export contract

- Package, module, OpenAPI and health versions are 0.4.1; backup schema remains 1.1.
- PyYAML is a runtime dependency; httpx is a test dependency. YAML is actually loaded in a clean install. Environment overrides no longer mutate nested defaults between loads.
- `exporter_agent_id` is header attribution only; `agent_id` is its deprecated alias. Neither filters memories nor authenticates a caller. Conflicting aliases return 400. Markdown/CSV ignore attribution.
- Storage is local, but remote embedding sends inputs to its provider. Preinstall local dependencies/model weights for offline use. Never back up a live WAL database by copying only its main file.

## SQLite portability

CI exposed pre-existing migration SQL using a dynamic RAISE message unsupported by SQLite 3.45. Fresh migrations now use literal messages; migration 014 atomically replaces the three existing triggers without changing their allowed values or any memory/audit row. SQLite 3.45.1 clean-install regression passes. Upgrade an existing database on a runtime that can read its current schema before attempting to move it to an older SQLite.

## Reproducible verification

```sh
python -m pip install -e '.[dev]'
python -m pip check
python scripts/run_offline_tests.py
python -m pytest --noconftest -q scripts/ui_p1_synthetic.py
```

The offline runner creates a temporary HOME, removes credential/config environment variables and installs a deterministic synthetic provider. Python TCP connects are denied. This is accidental-network protection for tests, not a sandbox against malicious subprocesses. GitHub Actions executes the same synthetic suite and retains its result. Real-provider quality and browser validation are separate gates.

The private release evidence includes exact five-table full-corpus JSONL restore, SQLite restore integrity, source/old-worker compatibility, paired weak-label retrieval probes, real Chrome downloads and an independent review. Weak labels and a small topic set are smoke tests, not a comprehensive human-labelled relevance benchmark. Public access evidence is recorded separately from local service health.

## Public health versus authentication

```sh
python scripts/check_endpoint.py https://hippo.example.com/health \
  --access-host team.cloudflareaccess.com
```

The probe never follows a login redirect or emits query strings/cookies. A matching 302 is `AUTH_REQUIRED`, not application failure and not logged-in business success. HTTP 403 remains `UNEXPECTED_HTTP`; clients may receive different edge responses. A blocked WAF API or login session requires the authorized operator, not disabling protection or copying credentials into diagnostics.

## Deployment and rollback

Preserve the previous service definition and source. Build/test outside the live tree, back up the stopped writer's latest SQLite state, then switch code/interpreter using a checked systemd drop-in. Re-read the exact drop-in, process cwd/argv, served UI hash, health/version, original-record preservation and real retrieval.

For the owner's 2026-09-08 rollout the private controller is:

```sh
python3 /home/wpsl5168/work/openhippo-closeout/2026-09-08/ci-release/deploy.py rollback
```

It restores the immediately previous drop-in and code, not the original repository's oldest state. It never copies an older database over new writes. Do not use the older 2026-09-07 controller after this drop-in has changed. Only the verified baseline old worker (MAX_ATTEMPTS=5) is covered by the exhausted-job rollback check.

A local snapshot is not off-host disaster recovery. No old backup or source directory is deleted by this release.
