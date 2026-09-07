"""A vec0 MATCH regression must not remove the exact scalar fallback."""
import sqlite3
import pytest
from openhippo.core import embedding
from openhippo.core.storage import Storage


@pytest.mark.parametrize("empty", [False, True])
def test_match_operational_error_falls_back_without_writes(tmp_path, monkeypatch, empty):
    store = Storage(tmp_path / "fallback.db")
    vector = embedding.get_embedding("fallback target")
    assert vector is not None
    expected = []
    if not empty:
        row = store.cold_add("memory", "fallback target")
        store.vec_store(row["id"], vector)
        expected = [row["id"]]
    conn = store._get_conn()
    writes = conn.total_changes
    statements = []
    class MatchFailure:
        def __getattr__(self, name):
            return getattr(conn, name)
        def execute(self, sql, params=()):
            statements.append(sql)
            if "embedding MATCH" in sql:
                raise sqlite3.OperationalError("synthetic vec0 MATCH unavailable")
            return conn.execute(sql, params)
    monkeypatch.setattr(store, "_get_conn", lambda: MatchFailure())
    hits = store.vec_search(vector, limit=10)
    assert [row["id"] for row in hits] == expected
    assert conn.total_changes == writes
    assert any("vec_distance_l2" in sql for sql in statements)
    store.close()
