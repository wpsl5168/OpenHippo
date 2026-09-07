"""Synthetic SQLite/sqlite-vec reconciliation tests; never infer or use production."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

import pytest

from openhippo import reconcile as r
from openhippo.core import embed_queue as q
from openhippo.core import embedding as emb
from openhippo.core.embedding import EmbeddingVector
from openhippo.core.storage import Storage

MODEL, SPACE = 'reconcile-synthetic', 'reconcile-synthetic-768-v1'
BODY, ERROR = 'PRIVATE_SYNTHETIC_BODY_avoid_output', 'PRIVATE_HISTORICAL_ERROR_keep_exact'


@pytest.fixture(autouse=True)
def no_inference(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('inference/network forbidden in reconciliation')
    monkeypatch.setattr(q, 'get_embedding', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / 'queue.db')
    yield s
    s.close()


def seed(storage, *, vector=True, receipt=True, attempts=5, historical_version=True):
    conn = storage._get_conn()
    entry = storage.cold_add('memory', BODY)
    mid = entry['id']
    jid = q.enqueue(conn, 'cold_memory', mid, BODY)
    conn.execute("UPDATE embedding_jobs SET status='failed', attempts=?, last_error=? WHERE id=?", (attempts, ERROR, jid))
    if not historical_version:
        conn.execute('DELETE FROM embedding_job_versions WHERE job_id=?', (jid,))
    conn.commit()
    vector_data = EmbeddingVector([0.25] + [0.0] * 767, model=MODEL, space_id=SPACE)
    if vector and receipt:
        latest = q.enqueue(conn, 'cold_memory', mid, BODY)
        # Explicit claim of the synthetic replacement avoids retrying failures.
        conn.execute("UPDATE embedding_jobs SET status='running' WHERE id=?", (latest,))
        conn.commit()
        job = dict(conn.execute('SELECT * FROM embedding_jobs WHERE id=?', (latest,)).fetchone())
        assert q.complete_job(storage, job, vector_data)
    elif vector:
        storage.vec_store(mid, vector_data)
    return conn, jid, mid


def plan(conn, jid):
    return r.plan_job(conn, jid, model=MODEL, space_id=SPACE)


def apply(conn, p):
    return r.apply_plan(conn, p, actor='synthetic-test', operator_reason='safe synthetic audit')


def snapshot(conn):
    """Exact logical records, including blobs, errors, all schema-owned fields."""
    return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]
            for table in ('cold_memory', 'cold_embeddings', 'cold_memory_vec', 'embedding_spaces',
                          'embedding_jobs', 'embedding_job_versions')}


def test_resolved_preserves_every_original_field_and_is_idempotent(storage):
    conn, jid, _ = seed(storage)
    before = snapshot(conn)
    schema_before = list(conn.execute('SELECT name FROM sqlite_master ORDER BY name'))
    p = plan(conn, jid)
    assert p['decision'] == 'resolved'
    assert snapshot(conn) == before
    assert list(conn.execute('SELECT name FROM sqlite_master ORDER BY name')) == schema_before
    out = apply(conn, p)
    assert out['outcome'] == 'applied'
    assert snapshot(conn) == before
    saved = dict(conn.execute('SELECT * FROM embedding_job_reconciliation WHERE id=?', (out['audit_id'],)).fetchone())
    assert (saved['original_status'], saved['original_error'], saved['original_attempts']) == ('failed', ERROR, 5)
    assert saved['reason'] == p['reason']
    assert BODY not in saved['evidence_json'] and ERROR not in saved['evidence_json']
    assert apply(conn, plan(conn, jid))['outcome'] == 'already_applied'
    assert conn.execute('SELECT count(*) FROM embedding_job_reconciliation').fetchone()[0] == 1
    assert q.queue_stats(conn) == {'done': 1, 'failed': 1, 'historical_resolved': 1,
                                  'historical_superseded': 0, 'historical_needs_confirmation': 0,
                                  'failed_unresolved': 0}


@pytest.mark.parametrize('historical', ['missing_version', 'different_revision', 'different_content'])
def test_superseded_is_not_claim_that_original_job_completed(storage, historical):
    conn, jid, _ = seed(storage)
    if historical == 'missing_version':
        conn.execute('DELETE FROM embedding_job_versions WHERE job_id=?', (jid,))
    elif historical == 'different_revision':
        conn.execute('UPDATE embedding_job_versions SET updated_at=updated_at-10 WHERE job_id=?', (jid,))
    else:
        conn.execute("UPDATE embedding_jobs SET content='old synthetic revision' WHERE id=?", (jid,))
    conn.commit()
    before = snapshot(conn)
    p = plan(conn, jid)
    assert p['decision'] == 'superseded'
    assert 'not_original_job' in p['reason']
    apply(conn, p)
    assert snapshot(conn) == before
    assert q.queue_stats(conn)['historical_superseded'] == 1


@pytest.mark.parametrize('case,expected', [
    ('no_vector', 'vector_missing'),
    ('blob_only', 'vector_missing'),
    ('unknown_space', 'unknown_space'),
    ('wrong_space', 'space_conflict'),
    ('wrong_model', 'space_conflict'),
    ('blob_vec_mismatch', 'blob_vec_conflict'),
    ('wrong_revision', 'receipt_revision_or_vector_conflict'),
    ('wrong_content', 'receipt_revision_or_vector_conflict'),
    ('rewritten_vector', 'receipt_revision_or_vector_conflict'),
    ('proof_version_missing', 'receipt_job_unverified'),
    ('stale_done_error', 'receipt_job_unverified'),
    ('newer_pending', 'newer_job_conflict'),
    ('malformed_receipt', 'receipt_revision_or_vector_conflict'),
    ('invalid_float', 'invalid_vector'),
])
def test_unverified_cases_remain_failed_and_held(storage, case, expected):
    conn, jid, mid = seed(storage, attempts=q.MAX_ATTEMPTS)
    if case == 'no_vector':
        conn.execute('DELETE FROM cold_embeddings WHERE memory_id=?', (mid,))
    elif case == 'blob_only':
        conn.execute('DELETE FROM cold_memory_vec WHERE memory_id=?', (mid,))
    elif case == 'unknown_space':
        conn.execute('DELETE FROM embedding_spaces WHERE memory_id=?', (mid,))
    elif case == 'wrong_space':
        conn.execute("UPDATE embedding_spaces SET space_id='unknown' WHERE memory_id=?", (mid,))
    elif case == 'wrong_model':
        conn.execute("UPDATE embedding_spaces SET model='wrong' WHERE memory_id=?", (mid,))
    elif case == 'blob_vec_mismatch':
        conn.execute('UPDATE cold_embeddings SET embedding=? WHERE memory_id=?', (bytes(3072), mid))
    elif case == 'wrong_revision':
        conn.execute('UPDATE cold_memory SET updated_at=updated_at+1 WHERE id=?', (mid,))
    elif case == 'wrong_content':
        conn.execute("UPDATE cold_memory SET content='changed without timestamp' WHERE id=?", (mid,))
    elif case == 'rewritten_vector':
        conn.execute('UPDATE cold_embeddings SET created_at=created_at+1 WHERE memory_id=?', (mid,))
    elif case == 'proof_version_missing':
        conn.execute('DELETE FROM embedding_job_versions WHERE job_id IN (SELECT job_id FROM embedding_job_receipts)')
    elif case == 'stale_done_error':
        conn.execute("UPDATE embedding_jobs SET last_error='stale job: discarded' WHERE status='done'")
    elif case == 'newer_pending':
        conn.commit()
        q.enqueue(conn, 'cold_memory', mid, BODY)
    elif case == 'malformed_receipt':
        conn.execute("UPDATE embedding_job_receipts SET evidence_json='not json'")
    elif case == 'invalid_float':
        import struct
        bad = struct.pack('<768f', float('inf'), *([0.0] * 767))
        conn.execute('UPDATE cold_embeddings SET embedding=? WHERE memory_id=?', (bad, mid))
        conn.execute('DELETE FROM cold_memory_vec WHERE memory_id=?', (mid,))
        conn.execute('INSERT INTO cold_memory_vec(memory_id,embedding) VALUES (?,?)', (mid, bad))
    conn.commit()
    before = snapshot(conn)
    p = plan(conn, jid)
    assert (p['decision'], p['reason']) == ('needs_confirmation', expected)
    assert apply(conn, p)['outcome'] == 'applied'
    assert snapshot(conn) == before
    assert q.queue_stats(conn)['failed_unresolved'] == 1
    next_job = q.fetch_one_pending(conn)
    assert next_job is None or next_job['id'] != jid


@pytest.mark.parametrize('historical_version', [True, False])
def test_legacy_vector_and_space_do_not_prove_revision(storage, historical_version):
    conn, jid, _ = seed(storage, receipt=False, historical_version=historical_version)
    p = plan(conn, jid)
    assert p['decision'] == 'needs_confirmation'
    assert p['reason'] == 'completion_receipt_missing'
    assert not q._table_exists(conn, 'embedding_job_receipts')
    apply(conn, p)
    assert not q._table_exists(conn, 'embedding_job_receipts')


def test_requires_explicit_expected_space(storage):
    conn, jid, _ = seed(storage)
    assert r.plan_job(conn, jid)['reason'] == 'expected_space_required'


@pytest.mark.parametrize('change', ['job', 'revision', 'space', 'vec', 'new_job'])
def test_concurrent_change_aborts_apply_without_audit(storage, change):
    conn, jid, mid = seed(storage)
    p = plan(conn, jid)
    other = r.connect_database(storage.db_path, apply=True)
    try:
        if change == 'job':
            other.execute("UPDATE embedding_jobs SET attempts=attempts+1 WHERE id=?", (jid,))
        elif change == 'revision':
            other.execute('UPDATE cold_memory SET updated_at=updated_at+1 WHERE id=?', (mid,))
        elif change == 'space':
            other.execute("UPDATE embedding_spaces SET space_id='changed' WHERE memory_id=?", (mid,))
        elif change == 'vec':
            other.execute('DELETE FROM cold_memory_vec WHERE memory_id=?', (mid,))
        else:
            other.execute('INSERT INTO embedding_jobs(target_table,target_id,content) VALUES (?,?,?)', ('cold_memory', mid, BODY))
        other.commit()
    finally:
        other.close()
    before = snapshot(conn)
    assert apply(conn, p)['outcome'] == 'conflict'
    assert snapshot(conn) == before
    assert not q._table_exists(conn, r.AUDIT_TABLE)


def test_apply_holds_immediate_lock_during_revalidation(storage, monkeypatch):
    conn, jid, mid = seed(storage)
    p = plan(conn, jid)
    other = r.connect_database(storage.db_path, apply=True)
    other.execute('PRAGMA busy_timeout=0')
    original = r._inspect_job
    seen = []
    def inspect(c, *args):
        assert c.in_transaction
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            other.execute('UPDATE cold_memory SET updated_at=updated_at+1 WHERE id=?', (mid,))
        other.rollback()
        seen.append(True)
        return original(c, *args)
    monkeypatch.setattr(r, '_inspect_job', inspect)
    try:
        assert apply(conn, p)['outcome'] == 'applied'
        assert seen == [True]
    finally:
        other.close()


def test_queue_stats_does_not_commit_callers_transaction(storage):
    conn, jid, _ = seed(storage)
    apply(conn, plan(conn, jid))
    conn.execute('BEGIN')
    conn.execute("UPDATE embedding_jobs SET last_error='uncommitted' WHERE id=?", (jid,))
    assert q.queue_stats(conn)['failed'] == 1
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute('SELECT last_error FROM embedding_jobs WHERE id=?', (jid,)).fetchone()[0] == ERROR


def test_audit_insert_failure_rolls_back_and_retry_works(storage):
    conn, jid, _ = seed(storage)
    r._ensure_audit_schema(conn)
    conn.execute("CREATE TRIGGER audit_fault AFTER INSERT ON embedding_job_reconciliation BEGIN SELECT RAISE(ABORT,'injected audit failure'); END")
    conn.commit()
    before = snapshot(conn)
    p = plan(conn, jid)
    with pytest.raises(sqlite3.IntegrityError):
        apply(conn, p)
    assert not conn.in_transaction
    assert snapshot(conn) == before
    assert conn.execute('SELECT count(*) FROM embedding_job_reconciliation').fetchone()[0] == 0
    conn.execute('DROP TRIGGER audit_fault')
    conn.commit()
    assert apply(conn, p)['outcome'] == 'applied'


def test_new_schema_also_rolls_back_if_audit_creation_fails(storage, monkeypatch):
    conn, jid, _ = seed(storage)
    original = r._ensure_audit_schema
    def fail(c):
        original(c)
        raise RuntimeError('after additive DDL')
    monkeypatch.setattr(r, '_ensure_audit_schema', fail)
    with pytest.raises(RuntimeError):
        apply(conn, plan(conn, jid))
    assert not q._table_exists(conn, r.AUDIT_TABLE)
    assert not conn.in_transaction


def test_receipt_failure_rolls_back_vector_job_and_receipt(storage, monkeypatch):
    conn, _, mid = seed(storage, vector=False)
    new_id = q.enqueue(conn, 'cold_memory', mid, BODY)
    job = q.fetch_one_pending(conn)
    assert job is not None
    assert job['id'] == new_id
    before = snapshot(conn)
    original = q._record_completion_receipt
    def fail(*args):
        original(*args)
        raise RuntimeError('receipt fault')
    monkeypatch.setattr(q, '_record_completion_receipt', fail)
    with pytest.raises(RuntimeError):
        q.complete_job(storage, job, EmbeddingVector([0.0] * 768, model=MODEL, space_id=SPACE))
    assert snapshot(conn) == before
    assert not q._table_exists(conn, 'embedding_job_receipts')


def test_audit_transition_and_return_to_prior_evidence_is_append_only(storage):
    conn, jid, _ = seed(storage)
    initial = plan(conn, jid)
    assert apply(conn, initial)['outcome'] == 'applied'
    no_expected_space = r.plan_job(conn, jid)
    assert apply(conn, no_expected_space)['outcome'] == 'applied'
    assert q.queue_stats(conn)['historical_needs_confirmation'] == 1
    assert apply(conn, initial)['outcome'] == 'applied'
    assert q.queue_stats(conn)['historical_resolved'] == 1
    assert apply(conn, initial)['outcome'] == 'already_applied'
    assert conn.execute('SELECT count(*) FROM embedding_job_reconciliation').fetchone()[0] == 3


def test_nested_transactions_and_nonfailed_jobs_rejected(storage):
    conn, jid, _ = seed(storage)
    p = plan(conn, jid)
    conn.execute('BEGIN')
    with pytest.raises(ValueError):
        apply(conn, p)
    with pytest.raises(ValueError):
        plan(conn, jid)
    conn.rollback()
    conn.execute("UPDATE embedding_jobs SET status='running' WHERE id=?", (jid,))
    conn.commit()
    assert apply(conn, p)['outcome'] == 'conflict'
    assert apply(conn, plan(conn, jid))['outcome'] == 'conflict'


def test_cleanup_preserves_receipt_proof_and_audited_rows(storage):
    conn, jid, _ = seed(storage)
    apply(conn, plan(conn, jid))
    conn.execute("UPDATE embedding_jobs SET updated_at=datetime('now','-10 days')")
    conn.commit()
    assert q.cleanup_done(conn) == 0
    assert conn.execute('SELECT count(*) FROM embedding_jobs').fetchone()[0] == 2


def cli(db, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('OPENHIPPO_', 'HIPPO_', 'HERMES_'))
           and not any(x in k.upper() for x in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY'))}
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    env['HOME'] = str(Path(db).parent)
    # Parent monkeypatches do not cross exec. Guard the actual CLI subprocess
    # before importing OpenHippo, not just the parent pytest interpreter.
    bootstrap = '''import runpy, socket
def denied(*args, **kwargs):
    raise AssertionError('TCP forbidden in reconciliation CLI test')
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
runpy.run_module('openhippo.reconcile', run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-c', bootstrap, '--db', str(db), *args],
                            env=env, text=True, capture_output=True, timeout=15)
    assert BODY not in result.stdout + result.stderr
    assert ERROR not in result.stdout + result.stderr
    return result


def test_real_cli_readonly_default_apply_and_idempotency(storage):
    conn, jid, _ = seed(storage)
    before = snapshot(conn)
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    before_hash = hashlib.sha256(Path(storage.db_path).read_bytes()).hexdigest()
    dry = cli(storage.db_path, '--model', MODEL, '--space-id', SPACE)
    assert dry.returncode == 0, dry.stderr
    report = json.loads(dry.stdout)
    assert report['mode'] == 'dry_run' and report['decisions'] == {'resolved': 1}
    assert report['selected'] == report['matching_failed'] == 1 and not report['has_more']
    assert not q._table_exists(conn, r.AUDIT_TABLE)
    assert hashlib.sha256(Path(storage.db_path).read_bytes()).hexdigest() == before_hash
    args = ('--model', MODEL, '--space-id', SPACE, '--apply', '--actor', 'cli-test', '--reason', 'temporary database only')
    applied = cli(storage.db_path, *args)
    assert applied.returncode == 0, applied.stderr
    report = json.loads(applied.stdout)
    assert report['outcomes'][0]['outcome'] == 'applied'
    assert report['queue_stats']['failed'] == 1 and report['queue_stats']['failed_unresolved'] == 0
    assert snapshot(conn) == before
    again = cli(storage.db_path, *args)
    assert again.returncode == 0 and json.loads(again.stdout)['outcomes'][0]['outcome'] == 'already_applied'
    print('REAL_CLI_TEMP_DB_RESULT=' + applied.stdout.strip())


def test_cli_legacy_holds_and_pagination(storage):
    conn, jid, mid = seed(storage, receipt=False, attempts=q.MAX_ATTEMPTS)
    for _ in range(2):
        new = q.enqueue(conn, 'cold_memory', mid, BODY)
        conn.execute("UPDATE embedding_jobs SET status='failed',attempts=?,last_error=? WHERE id=?", (q.MAX_ATTEMPTS, ERROR, new))
        conn.commit()
    dry = cli(storage.db_path, '--limit', '1')
    data = json.loads(dry.stdout)
    assert (data['matching_failed'], data['selected'], data['has_more']) == (3, 1, True)
    assert data['next_after_id'] == jid
    remaining = json.loads(cli(storage.db_path, '--after-id', str(jid)).stdout)
    assert remaining['matching_failed'] == remaining['selected'] == 2 and not remaining['has_more']
    applied = cli(storage.db_path, '--apply', '--actor', 'test', '--reason', 'hold legacy failures')
    assert applied.returncode == 0
    assert json.loads(applied.stdout)['decisions'] == {'needs_confirmation': 3}
    assert q.fetch_one_pending(conn) is None
    assert q.queue_stats(conn)['failed_unresolved'] == 3


def test_cli_guardrails_and_ro_connection(storage, tmp_path):
    conn, jid, _ = seed(storage)
    assert cli(storage.db_path, '--apply').returncode == 2
    assert cli(storage.db_path, '--model', MODEL).returncode == 2
    assert cli(storage.db_path, '--limit', '0').returncode == 2
    missing = tmp_path / 'missing.db'
    assert cli(missing).returncode == 2 and not missing.exists()
    ro = r.connect_database(storage.db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute('CREATE TABLE forbidden(id)')
        assert plan(ro, jid)['decision'] == 'resolved'
    finally:
        ro.close()
    assert not q._table_exists(conn, r.AUDIT_TABLE)


def legacy_eligible_ids(conn):
    """Pre-audit worker predicate on real SQLite, not a full old deployment."""
    return [row[0] for row in conn.execute(
        "SELECT id FROM embedding_jobs WHERE status IN ('pending','failed') AND attempts < ? ORDER BY id",
        (q.MAX_ATTEMPTS,))]


@pytest.mark.parametrize('attempts', [0, 1, q.MAX_ATTEMPTS - 1])
@pytest.mark.parametrize('decision', ['needs_confirmation', 'resolved', 'superseded'])
def test_retryable_apply_refused_without_false_rollback_hold(storage, attempts, decision):
    conn, jid, _ = seed(storage, attempts=attempts, receipt=decision != 'needs_confirmation',
                        historical_version=decision != 'superseded')
    p = plan(conn, jid)
    assert p['decision'] == decision
    before = snapshot(conn)
    schema = list(conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name'))
    out = apply(conn, p)
    assert out == {'job_id': jid, 'outcome': 'refused', 'reason': 'retryable_job_rollback_unsafe',
                   'required_min_attempts': q.MAX_ATTEMPTS}
    assert snapshot(conn) == before
    assert list(conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')) == schema
    assert not conn.in_transaction and not q._table_exists(conn, r.AUDIT_TABLE)
    # Refusal is honest: the original job remains retryable under BOTH workers.
    assert jid in legacy_eligible_ids(conn)
    assert q.fetch_one_pending(conn)['id'] == jid


@pytest.mark.parametrize('attempts', [q.MAX_ATTEMPTS, q.MAX_ATTEMPTS + 1])
def test_exhausted_needs_confirmation_safe_without_new_worker_filter(storage, attempts):
    conn, jid, _ = seed(storage, receipt=False, attempts=attempts)
    before = snapshot(conn)
    assert jid not in legacy_eligible_ids(conn)
    assert apply(conn, plan(conn, jid))['outcome'] == 'applied'
    assert snapshot(conn) == before
    assert jid not in legacy_eligible_ids(conn)
    assert q.fetch_one_pending(conn) is None
    assert q.queue_stats(conn)['failed_unresolved'] == 1
    assert q.queue_stats(conn)['historical_needs_confirmation'] == 1


def test_cli_mixed_batch_reports_refusal_not_success_or_fake_resolution(storage):
    conn, exhausted, _ = seed(storage, receipt=False)
    mid = storage.cold_add('memory', 'separate synthetic target')['id']
    retryable = q.enqueue(conn, 'cold_memory', mid, 'separate synthetic target')
    conn.execute("UPDATE embedding_jobs SET status='failed',attempts=1,last_error=? WHERE id=?", (ERROR, retryable))
    conn.commit()
    before = snapshot(conn)
    dry = cli(storage.db_path, '--model', MODEL, '--space-id', SPACE)
    assert dry.returncode == 0 and json.loads(dry.stdout)['decisions'] == {'needs_confirmation': 2}
    out = cli(storage.db_path, '--apply', '--actor', 'test', '--reason', 'exhausted audit only')
    assert out.returncode == 2
    data = json.loads(out.stdout)
    assert {row['job_id']: row['outcome'] for row in data['outcomes']} == {exhausted: 'applied', retryable: 'refused'}
    assert data['outcomes'][1]['reason'] == 'retryable_job_rollback_unsafe'
    assert data['queue_stats']['failed_unresolved'] == 2
    assert data['queue_stats']['historical_needs_confirmation'] == 1
    assert snapshot(conn) == before
    assert [row[0] for row in conn.execute('SELECT job_id FROM embedding_job_reconciliation')] == [exhausted]


@pytest.mark.parametrize('decision', ['needs_confirmation', 'resolved', 'superseded'])
@pytest.mark.parametrize('new_content', [BODY, 'updated synthetic revision'])
@pytest.mark.parametrize('active_status', ['running', 'pending'])
def test_cold_update_preserves_failed_audit_and_blocks_late_completion(
        storage, monkeypatch, decision, new_content, active_status):
    conn, jid, mid = seed(storage, receipt=decision != 'needs_confirmation',
                        historical_version=decision != 'superseded')
    p = plan(conn, jid)
    assert p['decision'] == decision
    assert apply(conn, p)['outcome'] == 'applied'
    failed_before = dict(conn.execute('SELECT * FROM embedding_jobs WHERE id=?', (jid,)).fetchone())
    audit_before = [tuple(row) for row in conn.execute('SELECT * FROM embedding_job_reconciliation')]
    versions_before = [tuple(row) for row in conn.execute('SELECT * FROM embedding_job_versions WHERE job_id=?', (jid,))]
    receipts_before = ([tuple(row) for row in conn.execute('SELECT * FROM embedding_job_receipts')]
                       if q._table_exists(conn, 'embedding_job_receipts') else None)
    active_id = q.enqueue(conn, 'cold_memory', mid, BODY)
    if active_status == 'running':
        active = q.fetch_one_pending(conn)
        assert active is not None
        assert active['id'] == active_id
    else:
        active = dict(conn.execute('SELECT * FROM embedding_jobs WHERE id=?', (active_id,)).fetchone())
    fresh_vec = EmbeddingVector([0.0, 0.75] + [0.0] * 766, model=MODEL, space_id=SPACE)
    monkeypatch.setattr(emb, 'get_embedding', lambda content: fresh_vec)
    assert storage.cold_update(mid, new_content) == {'id': mid, 'status': 'updated'}
    assert storage.cold_get(mid)['content'] == new_content
    expected_blob = storage._serialize_vec(fresh_vec)
    assert conn.execute('SELECT embedding FROM cold_embeddings WHERE memory_id=?', (mid,)).fetchone()[0] == expected_blob
    assert conn.execute('SELECT embedding FROM cold_memory_vec WHERE memory_id=?', (mid,)).fetchone()[0] == expected_blob
    assert dict(conn.execute('SELECT * FROM embedding_jobs WHERE id=?', (jid,)).fetchone()) == failed_before
    assert [tuple(row) for row in conn.execute('SELECT * FROM embedding_job_reconciliation')] == audit_before
    assert [tuple(row) for row in conn.execute('SELECT * FROM embedding_job_versions WHERE job_id=?', (jid,))] == versions_before
    active_after = dict(conn.execute('SELECT * FROM embedding_jobs WHERE id=?', (active_id,)).fetchone())
    assert active_after['status'] == 'done' and active_after['last_error'] == 'superseded by cold_update'
    after_update = snapshot(conn)
    # Simulate inference started before update, returning only after its commit.
    stale_vec = EmbeddingVector([1.0] + [0.0] * 767, model=MODEL, space_id=SPACE)
    assert q.complete_job(storage, active, stale_vec) is False
    q.mark_failed(conn, active_id, 'late inference failure also cannot revive job')
    assert snapshot(conn) == after_update
    assert [tuple(row) for row in conn.execute('SELECT * FROM embedding_job_reconciliation')] == audit_before
    if receipts_before is None:
        assert not q._table_exists(conn, 'embedding_job_receipts')
    else:
        assert [tuple(row) for row in conn.execute('SELECT * FROM embedding_job_receipts')] == receipts_before
    # Synchronous updates do not manufacture queue receipts for old failures.
    assert plan(conn, jid)['decision'] == 'needs_confirmation'


def test_cold_update_queue_failure_rolls_back_memory_vector_and_preserves_audit(storage, monkeypatch):
    conn, jid, mid = seed(storage, receipt=False)
    assert apply(conn, plan(conn, jid))['outcome'] == 'applied'
    q.enqueue(conn, 'cold_memory', mid, BODY)
    conn.execute("CREATE TRIGGER queue_update_fault AFTER UPDATE ON embedding_jobs "
                 "WHEN NEW.last_error='superseded by cold_update' BEGIN SELECT RAISE(ABORT,'queue fault'); END")
    conn.commit()
    before = snapshot(conn)
    audits = [tuple(row) for row in conn.execute('SELECT * FROM embedding_job_reconciliation')]
    monkeypatch.setattr(emb, 'get_embedding', lambda content: EmbeddingVector([0.0] * 768, model=MODEL, space_id=SPACE))
    with pytest.raises(sqlite3.IntegrityError, match='queue fault'):
        storage.cold_update(mid, 'must roll back')
    assert snapshot(conn) == before
    assert [tuple(row) for row in conn.execute('SELECT * FROM embedding_job_reconciliation')] == audits
    assert not conn.in_transaction


@pytest.mark.parametrize('malformed', ['boolean_version', 'float_version', 'duplicate_key'])
def test_receipt_evidence_rejects_ambiguous_json_schema(storage, malformed):
    conn, jid, _ = seed(storage)
    proof = json.loads(conn.execute('SELECT evidence_json FROM embedding_job_receipts').fetchone()[0])
    if malformed == 'boolean_version':
        proof['receipt_version'] = True  # Python's True == 1 must not count as v1.
    elif malformed == 'float_version':
        proof['receipt_version'] = 1.0
    encoded = json.dumps(proof)
    if malformed == 'duplicate_key':
        encoded = '{"content_sha256":"untrusted-first-value",' + encoded[1:]
    conn.execute('UPDATE embedding_job_receipts SET evidence_json=?', (encoded,))
    conn.commit()
    assert plan(conn, jid)['reason'] == 'receipt_revision_or_vector_conflict'
    assert apply(conn, plan(conn, jid))['outcome'] == 'applied'
    assert q.queue_stats(conn)['failed_unresolved'] == 1


def test_audit_trigger_cannot_silently_change_decision(storage):
    conn, jid, _ = seed(storage, receipt=False)
    r._ensure_audit_schema(conn)
    conn.execute("CREATE TRIGGER audit_corruption AFTER INSERT ON embedding_job_reconciliation BEGIN "
                 "UPDATE embedding_job_reconciliation SET decision='resolved' WHERE id=NEW.id; END")
    conn.commit()
    before = snapshot(conn)
    with pytest.raises(RuntimeError, match='audit verification failed'):
        apply(conn, plan(conn, jid))
    assert snapshot(conn) == before
    assert conn.execute('SELECT count(*) FROM embedding_job_reconciliation').fetchone()[0] == 0
    assert not conn.in_transaction


def test_idempotency_does_not_accept_corrupted_existing_audit(storage):
    conn, jid, _ = seed(storage, receipt=False)
    p = plan(conn, jid)
    assert apply(conn, p)['outcome'] == 'applied'
    conn.execute("UPDATE embedding_job_reconciliation SET decision='resolved'")
    conn.commit()
    before = snapshot(conn)
    with pytest.raises(RuntimeError, match='existing audit verification failed'):
        apply(conn, p)
    assert snapshot(conn) == before and not conn.in_transaction
    # Report corruption; never silently rewrite the historical audit.
    assert conn.execute('SELECT count(*) FROM embedding_job_reconciliation').fetchone()[0] == 1


def test_apply_rechecks_retry_cap_and_refuses_preexisting_unsafe_audit(storage):
    conn, jid, _ = seed(storage, receipt=False)
    p = plan(conn, jid)
    assert apply(conn, p)['outcome'] == 'applied'
    # Simulate an external reset/legacy audit. This is synthetic corruption,
    # never an eligibility-changing step performed by reconciliation.
    conn.execute('UPDATE embedding_jobs SET attempts=1 WHERE id=?', (jid,))
    conn.commit()
    before = snapshot(conn)
    audits = [tuple(row) for row in conn.execute('SELECT * FROM embedding_job_reconciliation')]
    assert apply(conn, p)['outcome'] == 'conflict'
    assert apply(conn, plan(conn, jid))['outcome'] == 'refused'
    assert snapshot(conn) == before
    assert [tuple(row) for row in conn.execute('SELECT * FROM embedding_job_reconciliation')] == audits
    assert jid in legacy_eligible_ids(conn)
    assert q.fetch_one_pending(conn) is None  # Existing hold is NOT rollback safe.


@pytest.mark.parametrize('fault', ['duplicate_receipt', 'job_status_update'])
def test_receipt_and_job_finalization_share_vector_rollback_boundary(storage, monkeypatch, fault):
    conn, _, mid = seed(storage, vector=False)
    q.enqueue(conn, 'cold_memory', mid, BODY)
    active = q.fetch_one_pending(conn)
    assert active is not None
    before = snapshot(conn)
    if fault == 'duplicate_receipt':
        record = q._record_completion_receipt
        def duplicate(*args):
            record(*args)
            record(*args)  # PRIMARY KEY rejects replacement of durable evidence.
        monkeypatch.setattr(q, '_record_completion_receipt', duplicate)
    else:
        conn.execute("CREATE TRIGGER completion_fault AFTER UPDATE ON embedding_jobs WHEN NEW.status='done' "
                     "BEGIN SELECT RAISE(ABORT,'completion fault'); END")
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        q.complete_job(storage, active, EmbeddingVector([0.0] * 768, model=MODEL, space_id=SPACE))
    assert snapshot(conn) == before
    assert not q._table_exists(conn, 'embedding_job_receipts')
    assert not conn.in_transaction


def test_completion_receipt_ddl_and_vector_respect_callers_transaction(storage):
    conn, _, mid = seed(storage, vector=False)
    q.enqueue(conn, 'cold_memory', mid, BODY)
    active = q.fetch_one_pending(conn)
    assert active is not None
    before = snapshot(conn)
    conn.execute('BEGIN')
    assert q.complete_job(storage, active, EmbeddingVector([0.0] * 768, model=MODEL, space_id=SPACE))
    assert conn.in_transaction and q._table_exists(conn, 'embedding_job_receipts')
    assert conn.execute('SELECT count(*) FROM embedding_job_receipts').fetchone()[0] == 1
    conn.rollback()
    assert snapshot(conn) == before
    assert not q._table_exists(conn, 'embedding_job_receipts')


@pytest.mark.parametrize('attempts', [1, q.MAX_ATTEMPTS])
def test_cold_update_preserves_unaudited_failed_rows_too(storage, monkeypatch, attempts):
    conn, jid, mid = seed(storage, receipt=False, attempts=attempts)
    before = dict(conn.execute('SELECT * FROM embedding_jobs WHERE id=?', (jid,)).fetchone())
    fresh_vec = EmbeddingVector([0.0, 0.5] + [0.0] * 766, model=MODEL, space_id=SPACE)
    monkeypatch.setattr(emb, 'get_embedding', lambda content: fresh_vec)
    storage.cold_update(mid, 'new synthetic body')
    assert dict(conn.execute('SELECT * FROM embedding_jobs WHERE id=?', (jid,)).fetchone()) == before
    assert conn.execute('SELECT embedding FROM cold_memory_vec WHERE memory_id=?', (mid,)).fetchone()[0] == storage._serialize_vec(fresh_vec)
    assert not q._table_exists(conn, r.AUDIT_TABLE)
