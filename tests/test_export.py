"""Tests for memory export functionality (E1-E7)."""

import csv
import io
import json
import time
import uuid

import pytest

from openhippo.core.engine import HippoEngine
from openhippo.core.export import (
    EXPORT_SCHEMA_VERSION,
    export_json,
    export_markdown,
    export_csv,
    check_embedding_compatibility,
    _get_embedding_backend_info,
)


def unique_tag(prefix: str = "export") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def engine(tmp_path):
    e = HippoEngine(db_path=tmp_path / "test.db")
    yield e
    e.close()


@pytest.fixture
def populated_engine(engine):
    """Engine with some hot and cold memories."""
    # Hot memories
    engine.storage.hot_add("memory", "Server is Ubuntu 24.04 with 8GB RAM")
    engine.storage.hot_add("memory", "User prefers dark mode")
    engine.storage.hot_add("user", "Name: Alice, Role: Engineer")

    # Cold memories with tags and metadata
    for i in range(5):
        tag = unique_tag(f"cold{i}")
        result = engine.storage.cold_add(
            "memory",
            f"Cold memory entry {i}: {tag} — important technical detail about system config",
            source="manual",
            tags=[f"tag_{i}", "test"],
            metadata={"source_session": f"session_{i}"},
        )
        # Embed it
        engine._embed_cold_entry(result["id"])

    return engine


class TestExportJSON:
    """E1-E3: JSON export tests."""

    def test_e1_schema_version_and_backend(self, populated_engine):
        """E1: JSON export contains schema_version + embedding_backend."""
        content = export_json(populated_engine.storage)
        doc = json.loads(content)

        header = doc["header"]
        assert header["schema_version"] == EXPORT_SCHEMA_VERSION
        assert "embedding_backend" in header
        assert header["embedding_backend"] != "unknown"
        assert "exported_at" in header
        assert header["total_count"] > 0

    def test_e2_full_export_completeness(self, populated_engine):
        """E2: Full export — verify content/metadata/embedding intact."""
        content = export_json(populated_engine.storage, include_embeddings=True)
        doc = json.loads(content)

        memories = doc["memories"]
        hot_memories = [m for m in memories if m["layer"] == "hot"]
        cold_memories = [m for m in memories if m["layer"] == "cold"]

        assert len(hot_memories) == 3  # 2 memory + 1 user
        assert len(cold_memories) == 5

        # Verify hot memory fields
        for m in hot_memories:
            assert "id" in m
            assert "content" in m
            assert "target" in m
            assert "created_at" in m
            assert m["layer"] == "hot"

        # Verify cold memory fields + embedding
        for m in cold_memories:
            assert "id" in m
            assert "content" in m
            assert "tags" in m
            assert isinstance(m["tags"], list)
            assert "metadata" in m
            assert "embedding" in m  # should have embeddings
            assert isinstance(m["embedding"], list)
            assert len(m["embedding"]) == 768

    def test_e3_filter_by_target(self, populated_engine):
        """E3: Export filtered by target — only matching memories."""
        content = export_json(populated_engine.storage, target="user")
        doc = json.loads(content)
        for m in doc["memories"]:
            assert m["target"] == "user"
        assert len(doc["memories"]) == 1  # only "Name: Alice"

    def test_e4_filter_by_time(self, populated_engine):
        """E4: Export filtered by time range."""
        future = time.time() + 3600
        content = export_json(populated_engine.storage, since=future)
        doc = json.loads(content)
        assert len(doc["memories"]) == 0

    def test_e7_empty_db(self, engine):
        """E7: Empty DB exports gracefully with correct header."""
        content = export_json(engine.storage)
        doc = json.loads(content)
        assert doc["header"]["schema_version"] == EXPORT_SCHEMA_VERSION
        assert doc["header"]["total_count"] == 0
        assert doc["memories"] == []


class TestExportJSONLines:
    """JSONL format tests."""

    def test_jsonl_format(self, populated_engine):
        """JSONL: header on first line, memories on subsequent lines."""
        content = export_json(populated_engine.storage, jsonlines=True)
        lines = content.strip().split("\n")
        assert len(lines) >= 2  # header + at least 1 memory

        header_line = json.loads(lines[0])
        assert "__header__" in header_line
        assert header_line["__header__"]["schema_version"] == EXPORT_SCHEMA_VERSION

        for line in lines[1:]:
            mem = json.loads(line)
            assert "id" in mem
            assert "content" in mem

    def test_jsonl_streaming(self, populated_engine):
        """JSONL output can be written to stream."""
        buf = io.StringIO()
        export_json(populated_engine.storage, output=buf, jsonlines=True)
        buf.seek(0)
        lines = buf.read().strip().split("\n")
        assert len(lines) == 1 + 3 + 5  # header + 3 hot + 5 cold


class TestExportMarkdown:
    """E5: Markdown export tests."""

    def test_e5_markdown_readable(self, populated_engine):
        """E5: Markdown export is human-readable with headings, time, tags."""
        md = export_markdown(populated_engine.storage)
        assert "# OpenHippo Memory Export" in md
        assert "Schema version" in md
        assert "**Layer**:" in md
        assert "**Target**:" in md
        assert "**Created**:" in md
        # Should contain memory content
        assert "Ubuntu" in md


class TestExportCSV:
    """E6: CSV export tests."""

    def test_e6_csv_parseable(self, populated_engine):
        """E6: CSV export is parseable with correct columns."""
        content = export_csv(populated_engine.storage)
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)

        assert len(rows) == 8  # 3 hot + 5 cold
        assert "id" in rows[0]
        assert "layer" in rows[0]
        assert "content" in rows[0]
        assert "target" in rows[0]


class TestEmbeddingCompatibility:
    """Embedding backend compatibility detection."""

    def test_same_backend_compatible(self):
        """Same backend → compatible, no reembed needed."""
        backend = _get_embedding_backend_info()
        header = {"embedding_backend": backend}
        result = check_embedding_compatibility(header)
        assert result["compatible"] is True
        assert result["reembed_needed"] is False

    def test_different_backend_incompatible(self):
        """Different backend → incompatible, reembed needed."""
        header = {"embedding_backend": "ollama/some-other-model"}
        result = check_embedding_compatibility(header)
        # Unless we happen to be using exactly that backend
        current = _get_embedding_backend_info()
        if current != "ollama/some-other-model":
            assert result["compatible"] is False
            assert result["reembed_needed"] is True

    def test_unknown_backend(self):
        """Unknown backend in export → reembed needed."""
        header = {"embedding_backend": "unknown"}
        result = check_embedding_compatibility(header)
        assert result["reembed_needed"] is True
