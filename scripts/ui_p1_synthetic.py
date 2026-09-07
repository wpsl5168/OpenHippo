"""Offline fixtures/tests: AST-selected real code, synthetic env + in-memory DB.

No OpenHippo import, host credentials/config read, provider, API or real DB.
Run pytest with --noconftest (the repository conftest starts an API engine).
"""
from __future__ import annotations

import ast
import io
import json
import logging
import sqlite3
import struct
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_functions(relative, names, env):
    path = ROOT / relative
    tree = ast.parse(path.read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == names
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), env)
    return env


def export_fixture():
    """Exercise real export_json with synthetic hot/cold rows and embedding."""
    provider = type("OllamaProvider", (), {"model": "synthetic-only"})()
    env = dict(json=json, io=io, struct=struct, datetime=datetime, timezone=timezone,
               EXPORT_SCHEMA_VERSION="1.1", embedding_module=types.SimpleNamespace(_provider=provider))
    load_functions("src/openhippo/core/export.py", {
        "_get_embedding_backend_info", "_deserialize_vec", "_build_header",
        "_iter_memories", "export_json"}, env)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript("""
        CREATE TABLE hot_memory (id TEXT, target TEXT, content TEXT, created_at REAL,
          updated_at REAL, sort_order INTEGER);
        CREATE TABLE cold_memory (id TEXT, target TEXT, content TEXT, source TEXT,
          tags TEXT, access_count INTEGER, created_at REAL, updated_at REAL,
          last_accessed REAL, archived_from TEXT, metadata TEXT);
        CREATE TABLE cold_embeddings (memory_id TEXT, embedding BLOB, model TEXT, created_at REAL);
        CREATE TABLE cold_memory_vec (memory_id TEXT, embedding BLOB);
        CREATE TABLE embedding_spaces (memory_id TEXT, model TEXT, space_id TEXT);
        """)
        conn.execute("INSERT INTO hot_memory VALUES (?,?,?,?,?,?)",
                     ("synthetic-hot", "user", "中文热记忆\n**Markdown**", 1, 2, 0))
        conn.execute("INSERT INTO cold_memory VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("synthetic-cold", "memory", "冷记忆 <literal> & data", "synthetic",
                      '["中文", "test"]', 7, 3, 4, 5, "synthetic-hot", '{"nested":{"ok":true}}'))
        vec = [0.25, -0.5, 1.0] + [0.0] * 765
        blob = struct.pack("<768f", *vec)
        conn.execute("INSERT INTO cold_embeddings VALUES (?,?,?,?)",
                     ("synthetic-cold", blob, "synthetic-only", 4))
        conn.execute("INSERT INTO cold_memory_vec VALUES (?,?)", ("synthetic-cold", blob))
        conn.execute("INSERT INTO embedding_spaces VALUES (?,?,?)",
                     ("synthetic-cold", "synthetic-only", "v1:" + "a" * 64))
        storage = types.SimpleNamespace(_get_conn=lambda: conn)
        result = env["export_json"](storage, include_embeddings=True)
        parsed = json.loads(result)
        assert parsed["header"]["total_count"] == 2
        assert parsed["memories"][1]["embedding"] == vec
        return result


def test_config_debug_omits_values(caplog, tmp_path):
    path = ROOT / "src/openhippo/core/config.py"
    tree = ast.parse(path.read_text())
    env_map = ast.literal_eval(next(n.value for n in tree.body if isinstance(n, ast.Assign)
                                   and any(isinstance(t, ast.Name) and t.id == "ENV_MAP" for t in n.targets)))
    synthetic_key = "SYNTHETIC_KEY_ONLY_do_not_log\nINJECTED_LINE"
    absent = tmp_path / "nonexistent-config.yaml"
    assert not absent.exists()
    logger = logging.getLogger("ui-p1.synthetic-config")
    env = dict(os=types.SimpleNamespace(environ={"HIPPO_OPENAI_API_KEY": synthetic_key,
                                               "HIPPO_OPENAI_MODEL": "synthetic-model",
                                               "HIPPO_PORT": "18200"}),
               Path=Path, logger=logger, ENV_MAP=env_map, DEFAULTS={}, DEFAULT_CONFIG_PATH=absent)
    load_functions("src/openhippo/core/config.py", {"_deep_merge", "_set_nested", "load_config"}, env)
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        config = env["load_config"](absent)
    assert config["embedding"]["openai"]["api_key"] == synthetic_key
    assert config["embedding"]["openai"]["model"] == "synthetic-model"
    assert config["server"]["port"] == 18200
    assert "SYNTHETIC_KEY_ONLY" not in caplog.text
    assert "INJECTED_LINE" not in caplog.text
    assert "synthetic-model" not in caplog.text
    assert "18200" not in caplog.text
    assert "embedding.openai.api_key (from HIPPO_OPENAI_API_KEY)" in caplog.text
    assert all(synthetic_key not in str(record.args) for record in caplog.records)


if __name__ == "__main__":
    if sys.argv[1:] != ["export-fixture"]:
        raise SystemExit("Usage: ui_p1_synthetic.py export-fixture")
    print(export_fixture())
