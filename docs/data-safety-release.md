# Data-safety release (2026-09-07)

## Boundaries

The data-safety fixes preserve existing memory/vector rows. They do not clear the database, regenerate the corpus, change the configured embedding backend, or treat same dimensionality as space compatibility.

- Vector writes validate finite float32 values and dimension, carry provider/model/endpoint/preprocessing provenance, and atomically update the blob, vec0 index and space record.
- Queue completion checks the stored content revision under the same write transaction. Late results cannot overwrite a newer cold_update; failures do not commit a partial vector replacement.
- Dream reconstructs stored vector provenance rather than attaching the currently configured provider.
- JSON/JSONL 1.1 retain memory identity, provenance, lifecycle, raw JSON columns, float32 vectors and per-vector space records. Import is atomic per record, not per document. Conflicts are reported; existing records are never silently overwritten. Repeated matching imports are idempotent.
- Unknown-space vector records are rejected before inserting the record. The source backup remains intact. Text-only 0.9/1.0 records remain importable. Re-embedding requires explicit opt-in and is refused if the provider configuration changes during inference.
- Highlighting creates Text/mark DOM nodes; it never reinterprets already-sanitized text as HTML. Downloads validate HTTP status/document/count and retain original JSON bytes. DEBUG config logs omit values.

## Additive schema and legacy data

Initialization adds embedding_spaces, embedding_job_versions, embedding_space_adoptions and ordinary metadata indexes. It does not rewrite old model labels or vectors. Old data without a space record remains fail-closed until an operator explicitly supplies evidence.

`Storage.adopt_legacy_space(model, space_id, evidence)` is NOT a startup migration. It requires its own BEGIN IMMEDIATE, validates the entire corpus for model/dimension/finite float32/byte-identical blob-index correspondence and existing-space conflicts, then inserts only missing space records and an adoption audit. Structural consistency is not semantic proof; the operator must retain the basis of the legacy attestation. Sampling is not a proof about every vector.

Physical blob/vec0 verification is not performed on each query. The hot-path metadata gate uses ordinary indexed tables; arbitrary manual SQL corruption remains an operational integrity concern.

## Verified deployment pattern

1. Preserve the effective runtime source, including pre-existing uncommitted changes.
2. Use SQLite backup (not a live copy of only memory.db) so committed WAL contents are included. Retain a private configuration/service copy. Restore separately and validate integrity, IDs, exact row hashes and blob/index correspondence.
3. Develop/test in an isolated candidate directory. Use a temporary HOME/DB, denied TCP and a deterministic fake provider for automated tests. Test real API export/import and real browser downloads separately.
4. On a restored copy, run the new initialization and any explicit legacy attestation; verify all original rows remain identical. Start new then old code against the same copy to verify code rollback compatibility.
5. Freeze a release and source hashes. Stop only OpenHippo, take another consistent backup of the latest accepted writes, perform the already-verified additive attestation, and switch a systemd drop-in's WorkingDirectory/PYTHONPATH to the release. Keep the existing Python environment and database path.
6. Re-read service state, process cwd/PYTHONPATH, health, served UI hash and memory/vector identity preservation. Exercise a known existing record through the real semantic API without logging its content.

### Deployment-specific rollback

The private, evidence-bearing release controller is:

`/home/wpsl5168/work/openhippo-fix/2026-09-07/deploy.py`

Code-only rollback command:

```sh
python3 /home/wpsl5168/work/openhippo-fix/2026-09-07/deploy.py rollback
```

The controller checks ownership/content of its systemd drop-in, removes only that drop-in, and starts the original service definition. It does **not** copy an old database over current data. Original source remains at `/home/wpsl5168/OpenHippo`; the shared Python environment is intentionally retained there.

A later reactivation after old-code writes requires fresh validation: old writers may create vector rows without new space records. Never assume a previous adoption covers subsequent old-code writes. Whole-database recovery is a separate operator procedure with a maintenance window and preservation/reconciliation of writes newer than the backup.

## Tests and limits

- Complete isolated Python suite: 272 tests passed; an existing Starlette/AnyIO deprecation warning remains. Synthetic providers do not establish semantic account quality.
- Browser regression: `scripts/ui_p1_browser.cjs`; requires Chromium, puppeteer-core and matching local marked/DOMPurify vendors. Paths are overridable through CHROME_BIN, PUPPETEER_CORE, UI_TEST_VENDORS and UI_TEST_PYTHON. `UI_TEST_FIXTURE` can supply a real REST-export fixture.
- Config logging regression: `python -m pytest --noconftest scripts/ui_p1_synthetic.py`.
- Record-level JSON export is not a full backup of dream audit/queue tables; SQLite backups cover those tables.
- Large vector exports/imports can require substantial client memory/time. This release does not claim a full-corpus JSON-vector restore benchmark or a comprehensive retrieval-quality benchmark.
- Existing FTS access-counter write amplification, dormant-candidate window exhaustion and historical exhausted job reconciliation are not addressed in this batch.
- Local snapshots protect this deployment change, not whole-host/disk failure; off-host disaster recovery is a separate requirement.
