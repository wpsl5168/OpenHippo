"""RAISE's message must be literal on SQLite < 3.47; keep constraints intact."""
from pathlib import Path
import importlib.util
import sqlite3
import pytest
from openhippo.core.storage import Storage, atomic
import openhippo.core.storage as storage_module


def migration():
    path = Path(storage_module.__file__).parent / 'migrations/014_static_trigger_messages.py'
    spec = importlib.util.spec_from_file_location('static_trigger_messages', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def triggers(conn):
    return list(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"))


def test_static_messages_enforce_original_domains(tmp_path):
    store = Storage(tmp_path / 'fresh.db')
    conn = store._get_conn()
    item = store.cold_add('memory', 'preserve this record')
    before = tuple(conn.execute('SELECT * FROM cold_memory WHERE id=?', (item['id'],)).fetchone())
    with pytest.raises(sqlite3.IntegrityError, match='invalid dream_status'):
        conn.execute("UPDATE cold_memory SET dream_status='illegal' WHERE id=?", (item['id'],))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='invalid dream_status'):
        conn.execute("INSERT INTO cold_memory(id,target,content,dream_status) VALUES ('invalid','memory','bad','illegal')")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='invalid dream_action'):
        conn.execute("INSERT INTO dream_actions(dream_run_id,action,memory_id) VALUES ('nonexistent','illegal',?)", (item['id'],))
    conn.rollback()
    assert tuple(conn.execute('SELECT * FROM cold_memory WHERE id=?', (item['id'],)).fetchone()) == before
    assert all('||' not in row['sql'] for row in triggers(conn) if row['name'] in ['cold_memory_bi_dreamstatus','cold_memory_bu_dreamstatus','dream_actions_bi_action'])
    store.close()


def test_existing_rows_unchanged_by_trigger_upgrade(tmp_path):
    store = Storage(tmp_path / 'upgrade.db')
    row = store.cold_add('memory', 'existing content')
    conn = store._get_conn()
    before = [tuple(r) for r in conn.execute('SELECT * FROM cold_memory')]
    with atomic(conn):
        migration().upgrade(conn)
    assert [tuple(r) for r in conn.execute('SELECT * FROM cold_memory')] == before
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    store.close()


def test_trigger_upgrade_failure_rolls_back_original_ddl(tmp_path):
    store = Storage(tmp_path / 'rollback.db')
    conn = store._get_conn()
    before = [tuple(r) for r in triggers(conn)]
    def deny(action, arg1, arg2, db, source):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TRIGGER and arg1 == 'dream_actions_bi_action' else sqlite3.SQLITE_OK
    conn.set_authorizer(deny)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            with atomic(conn):
                migration().upgrade(conn)
    finally:
        conn.set_authorizer(None)
    assert [tuple(r) for r in triggers(conn)] == before
    store.close()
