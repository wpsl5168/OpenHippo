"""Offline, inference-free failed-job reconciliation.

Usage: python -m openhippo.reconcile --db /absolute/path.db [--model M --space-id S]
Default is SQLite mode=ro dry-run; --apply additionally requires --actor/--reason.
Only additive audit records are written; no job is retried, deleted or relabelled
done. Apply accepts ONLY historical failed jobs already at MAX_ATTEMPTS or above,
for every decision, including needs_confirmation. Old workers ignore audits:
refusing retryable jobs avoids a hold that silently disappears on code rollback.
Attempts are never increased to obtain eligibility. Dry-run still classifies all
failed jobs. Existing audits from older policies are not retroactively made safe.

A stored vector plus timestamps/space is NOT proof of its input revision. Only a
completion receipt written atomically by the CAS queue path can prove that link.
Pre-receipt jobs therefore normally remain needs_confirmation, even after legacy
space adoption. Never manufacture receipts for old vectors to clear a counter.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import sys

from .core.embed_queue import MAX_ATTEMPTS, _table_exists, queue_stats
from .core.embedding import validate_vector

POLICY = 'queue-reconcile-v2-exhausted-only'
AUDIT_TABLE = 'embedding_job_reconciliation'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else _json(value).encode()).hexdigest()


def _row(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row is not None else None


def _optional_row(conn, table, sql, args=()):
    return _row(conn, sql, args) if _table_exists(conn, table) else None


def _receipt_object(pairs):
    # Receipts are evidence, not permissive configuration. Duplicate keys must
    # not let a different interpretation of an ambiguous document prove success.
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate receipt key')
        result[key] = value
    return result


def _ensure_audit_schema(conn):
    # No executescript: DDL must share the per-job rollback boundary.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS embedding_job_reconciliation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            policy TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN ('resolved','superseded','needs_confirmation')),
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            original_status TEXT NOT NULL,
            original_error TEXT,
            original_attempts INTEGER NOT NULL,
            original_updated_at TEXT NOT NULL,
            actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
            operator_reason TEXT NOT NULL CHECK(length(trim(operator_reason)) > 0),
            recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_job_reconciliation_latest ON embedding_job_reconciliation(job_id,id)')


def _inspect_job(conn, job_id, model, space_id):
    job = _row(conn, 'SELECT * FROM embedding_jobs WHERE id=?', (job_id,))
    evidence = {'policy': POLICY, 'expected_model': model, 'expected_space_id': space_id,
                'job_sha256': _digest(job)}
    decision, reason = 'needs_confirmation', 'not_failed'
    if job is None or job['status'] != 'failed':
        return {'job_id': job_id, 'decision': decision, 'reason': reason,
                'evidence': evidence, 'fingerprint': _digest(evidence)}
    mid = job['target_id']
    version = _optional_row(conn, 'embedding_job_versions',
                           'SELECT * FROM embedding_job_versions WHERE job_id=?', (job_id,))
    memory = _row(conn, 'SELECT content,created_at,updated_at FROM cold_memory WHERE id=?', (mid,))
    stored = _row(conn, 'SELECT * FROM cold_embeddings WHERE memory_id=?', (mid,))
    space = _optional_row(conn, 'embedding_spaces',
                         'SELECT * FROM embedding_spaces WHERE memory_id=?', (mid,))
    indexed = _optional_row(conn, 'cold_memory_vec',
                           'SELECT embedding FROM cold_memory_vec WHERE memory_id=?', (mid,))
    receipt = _optional_row(conn, 'embedding_job_receipts',
                           'SELECT * FROM embedding_job_receipts WHERE target_id=? ORDER BY job_id DESC LIMIT 1', (mid,))
    proof_job = proof_version = newer = None
    if receipt:
        proof_job = _row(conn, 'SELECT * FROM embedding_jobs WHERE id=?', (receipt['job_id'],))
        proof_version = _optional_row(conn, 'embedding_job_versions',
                                     'SELECT * FROM embedding_job_versions WHERE job_id=?', (receipt['job_id'],))
        newer = _row(conn, 'SELECT id,status FROM embedding_jobs WHERE target_table=? AND target_id=? AND id>? ORDER BY id DESC LIMIT 1',
                     ('cold_memory', mid, receipt['job_id']))
    current = None
    if memory:
        current = {'content_sha256': hashlib.sha256(memory['content'].encode()).hexdigest(),
                   'created_at': memory['created_at'], 'updated_at': memory['updated_at']}
    evidence.update({
        'job_version': version, 'current_revision': current,
        'stored_vector': None if stored is None else {
            'model': stored['model'], 'created_at': stored['created_at'],
            'sha256': _digest(stored['embedding']), 'bytes': len(stored['embedding'])},
        'indexed_sha256': None if indexed is None else _digest(indexed['embedding']),
        'space': space, 'receipt': receipt,
        'proof_job_sha256': _digest(proof_job), 'proof_version': proof_version,
        'newer_job': newer,
    })
    if job['target_table'] != 'cold_memory':
        reason = 'unsupported_target'
    elif memory is None:
        reason = 'target_missing'
    elif stored is None or indexed is None:
        reason = 'vector_missing'
    elif space is None or not space['space_id'] or not space['model']:
        reason = 'unknown_space'
    elif not model or not space_id:
        reason = 'expected_space_required'
    elif space['model'] != stored['model'] or space['model'] != model or space['space_id'] != space_id:
        reason = 'space_conflict'
    elif stored['embedding'] != indexed['embedding']:
        reason = 'blob_vec_conflict'
    else:
        try:
            validate_vector(struct.unpack('<768f', stored['embedding']))
        except (ValueError, TypeError, struct.error):
            reason = 'invalid_vector'
        else:
            reason = 'completion_receipt_missing'
            if receipt:
                try:
                    proof = json.loads(receipt['evidence_json'], object_pairs_hook=_receipt_object)
                except (ValueError, TypeError):
                    proof = None
                assert current is not None
                expected = dict(current, receipt_version=1, vector_sha256=_digest(stored['embedding']),
                                vector_created_at=stored['created_at'], model=model, space_id=space_id)
                if (not isinstance(proof, dict) or type(proof.get('receipt_version')) is not int
                        or proof != expected):
                    reason = 'receipt_revision_or_vector_conflict'
                elif (proof_job is None or proof_version is None or proof_job['status'] != 'done'
                      or proof_job['last_error'] is not None or proof_job['target_table'] != 'cold_memory'
                      or proof_job['target_id'] != mid or proof_job['content'] != memory['content']
                      or any(proof_version[k] != memory[k] for k in ('created_at', 'updated_at'))):
                    reason = 'receipt_job_unverified'
                elif receipt['job_id'] <= job_id or newer:
                    reason = 'newer_job_conflict'
                elif (version is not None and job['content'] == memory['content']
                      and all(version[k] == memory[k] for k in ('created_at', 'updated_at'))):
                    decision, reason = 'resolved', 'same_revision_covered_by_verified_later_completion'
                else:
                    decision, reason = 'superseded', 'current_revision_covered_by_verified_later_completion_not_original_job'
    evidence.update(decision=decision, reason=reason)
    return {'job_id': job_id, 'decision': decision, 'reason': reason,
            'fingerprint': _digest(evidence), 'evidence': evidence}


def plan_job(conn, job_id, *, model=None, space_id=None):
    """Consistent, read-only evidence snapshot. No plaintext in returned plan."""
    if conn.in_transaction:
        raise ValueError('plan_job requires its own read transaction')
    conn.execute('BEGIN')
    try:
        result = _inspect_job(conn, job_id, model, space_id)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


def apply_plan(conn, plan, *, actor, operator_reason):
    """One short write transaction per job; stale plans never change anything.

    No network/inference or full-index scans occur under the lock. A batch is
    intentionally NOT all-or-nothing: each returned applied audit is durable.
    Identical reruns create no additional rows. Conflicts require a new dry-run.
    All retryable failures are refused BEFORE schema creation or idempotency
    checks: an old worker does not honor an audit hold. Only exhausted failures
    are rollback-safe under the established MAX_ATTEMPTS worker contract.
    """
    if not isinstance(actor, str) or not actor.strip() or not isinstance(operator_reason, str) or not operator_reason.strip():
        raise ValueError('apply requires a nonempty actor and operator_reason')
    if conn.in_transaction:
        raise ValueError('apply requires its own BEGIN IMMEDIATE transaction')
    conn.execute('BEGIN IMMEDIATE')
    try:
        fresh = _inspect_job(conn, plan['job_id'], plan['evidence']['expected_model'], plan['evidence']['expected_space_id'])
        if fresh['fingerprint'] != plan['fingerprint'] or fresh['reason'] == 'not_failed':
            conn.rollback()
            return {'job_id': plan['job_id'], 'outcome': 'conflict'}
        job = _row(conn, 'SELECT * FROM embedding_jobs WHERE id=?', (plan['job_id'],))
        assert job is not None
        if type(job['attempts']) is not int or job['attempts'] < MAX_ATTEMPTS:
            conn.rollback()
            return {'job_id': job['id'], 'outcome': 'refused',
                    'reason': 'retryable_job_rollback_unsafe', 'required_min_attempts': MAX_ATTEMPTS}
        _ensure_audit_schema(conn)
        audit = dict(job_id=job['id'], policy=POLICY, fingerprint=fresh['fingerprint'],
                     decision=fresh['decision'], reason=fresh['reason'], evidence_json=_json(fresh['evidence']),
                     original_status=job['status'], original_error=job['last_error'],
                     original_attempts=job['attempts'], original_updated_at=job['updated_at'],
                     actor=actor, operator_reason=operator_reason)
        old = _row(conn, 'SELECT * FROM embedding_job_reconciliation WHERE job_id=? ORDER BY id DESC LIMIT 1',
                   (plan['job_id'],))
        if old and old['policy'] == POLICY and old['fingerprint'] == fresh['fingerprint']:
            # A matching fingerprint alone does not validate the stored decision
            # or original fields. Actor/reason belong to the FIRST application.
            if (any(old[k] != value for k, value in audit.items() if k not in ('actor', 'operator_reason'))
                    or not isinstance(old['actor'], str) or not old['actor'].strip()
                    or not isinstance(old['operator_reason'], str) or not old['operator_reason'].strip()):
                raise RuntimeError('existing audit verification failed')
            conn.commit()
            return {'job_id': plan['job_id'], 'outcome': 'already_applied', 'audit_id': old['id']}
        audit_id = conn.execute('''
            INSERT INTO embedding_job_reconciliation
            (job_id,policy,fingerprint,decision,reason,evidence_json,original_status,original_error,
             original_attempts,original_updated_at,actor,operator_reason)
            VALUES (:job_id,:policy,:fingerprint,:decision,:reason,:evidence_json,:original_status,:original_error,
                    :original_attempts,:original_updated_at,:actor,:operator_reason)
        ''', audit).lastrowid
        # Read the exact target before declaring success; audit insert is the
        # sole mutation (jobs, versions, errors, memories and vectors unchanged).
        saved = _row(conn, 'SELECT * FROM embedding_job_reconciliation WHERE id=?', (audit_id,))
        if saved is None or any(saved[k] != value for k, value in audit.items()):
            raise RuntimeError('audit verification failed')
        conn.commit()
        return {'job_id': job['id'], 'outcome': 'applied', 'audit_id': audit_id}
    except BaseException:
        conn.rollback()
        raise


def connect_database(path, *, apply=False):
    """Do not instantiate Storage: even dry-run must not initialize/migrate."""
    import sqlite_vec
    db = Path(path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(db.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=2)
    try:
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute('PRAGMA foreign_keys=ON')
        if not apply:
            conn.execute('PRAGMA query_only=ON')
        return conn
    except BaseException:
        conn.close()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--apply', action='store_true', help='audit exhausted failed jobs only; refuse retryable jobs; never run embeddings')
    parser.add_argument('--actor')
    parser.add_argument('--reason', help='operator reason for the audit/hold')
    parser.add_argument('--model', help='explicit expected stored model; not inferred from runtime provider')
    parser.add_argument('--space-id', help='explicit expected space identity')
    parser.add_argument('--job-id', type=int, action='append', help='repeat to select exact failed jobs')
    parser.add_argument('--limit', type=int, default=100, help='bounded batch, 1..1000; default 100')
    parser.add_argument('--after-id', type=int, default=0, help='failed-job pagination cursor')
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1000:
        parser.error('--limit must be between 1 and 1000')
    if bool(args.model) != bool(args.space_id):
        parser.error('--model and --space-id must be supplied together')
    if args.apply and (not args.actor or not args.actor.strip() or not args.reason or not args.reason.strip()):
        parser.error('--apply requires nonempty --actor and --reason')
    conn = None
    try:
        conn = connect_database(args.db, apply=args.apply)
        where, params = "status='failed' AND id>?", [args.after_id]
        if args.job_id:
            ids = sorted(set(args.job_id))
            if len(ids) > 1000:
                parser.error('at most 1000 distinct --job-id values')
            where += ' AND id IN (' + ','.join('?' for _ in ids) + ')'
            params.extend(ids)
        conn.execute('BEGIN')
        try:
            total = conn.execute('SELECT count(*) FROM embedding_jobs WHERE ' + where, params).fetchone()[0]
            selected = [r[0] for r in conn.execute('SELECT id FROM embedding_jobs WHERE ' + where + ' ORDER BY id LIMIT ?', params + [args.limit])]
            plans = [_inspect_job(conn, job_id, args.model, args.space_id) for job_id in selected]
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        outcomes = []
        if args.apply:
            for plan in plans:
                try:
                    outcomes.append(apply_plan(conn, plan, actor=args.actor, operator_reason=args.reason))
                except Exception as exc:
                    # Never echo SQLite exceptions containing original content/error.
                    outcomes.append({'job_id': plan['job_id'], 'outcome': 'error', 'error_type': type(exc).__name__})
        result = {
            'mode': 'apply' if args.apply else 'dry_run', 'policy': POLICY,
            'matching_failed': total, 'selected': len(plans), 'has_more': total > len(plans),
            'next_after_id': selected[-1] if selected else args.after_id,
            'decisions': dict(Counter(p['decision'] for p in plans)),
            'jobs': [{k: p[k] for k in ('job_id', 'decision', 'reason', 'fingerprint')} for p in plans],
            'outcomes': outcomes, 'queue_stats': queue_stats(conn),
        }
        print(_json(result))
        return 2 if any(o['outcome'] in ('conflict', 'error', 'refused') for o in outcomes) else 0
    except Exception as exc:
        print(_json({'error': 'reconciliation_failed', 'error_type': type(exc).__name__}), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
