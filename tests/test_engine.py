"""Test HippoEngine core operations."""

import tempfile
from pathlib import Path

import pytest
from openhippo import HippoEngine


@pytest.fixture
def engine(tmp_path):
    e = HippoEngine(db_path=tmp_path / "test.db")
    yield e
    e.close()


class TestHotMemory:
    def test_add_and_get(self, engine):
        result = engine.add("memory", "test entry")
        assert result["status"] == "created"
        assert "id" in result
        
        entries = engine.get_hot("memory")
        assert len(entries) == 1
        assert entries[0]["content"] == "test entry"

    def test_add_user(self, engine):
        engine.add("user", "Name: Alice")
        entries = engine.get_hot("user")
        assert len(entries) == 1

    def test_replace(self, engine):
        engine.add("memory", "old content here")
        result = engine.replace("memory", "old content", "new content here")
        assert result["status"] == "replaced"
        entries = engine.get_hot("memory")
        assert entries[0]["content"] == "new content here"

    def test_replace_not_found(self, engine):
        result = engine.replace("memory", "nonexistent", "new")
        assert "error" in result

    def test_remove(self, engine):
        engine.add("memory", "to be removed")
        result = engine.remove("memory", "to be removed")
        assert result["status"] == "removed"
        assert engine.get_hot("memory") == []

    def test_get_hot_text(self, engine):
        engine.add("memory", "entry one")
        engine.add("memory", "entry two")
        text = engine.get_hot_text("memory")
        assert "entry one" in text
        assert "entry two" in text

    def test_invalid_target(self, engine):
        with pytest.raises(ValueError):
            engine.add("invalid", "content")


class TestColdMemory:
    def test_archive_and_search(self, engine):
        engine.add("memory", "important fact about Python")
        result = engine.archive("memory", "important fact")
        assert result["status"] == "archived"
        assert engine.get_hot("memory") == []
        
        # Search cold
        results = engine.cold_search("Python")
        assert len(results) >= 1
        assert "Python" in results[0]["content"]

    def test_promote(self, engine):
        engine.add("memory", "temp entry")
        archived = engine.archive("memory", "temp entry")
        cold_id = archived["cold_id"]
        
        result = engine.promote(cold_id)
        assert result["status"] == "promoted"
        
        entries = engine.get_hot("memory")
        assert len(entries) == 1
        assert "temp entry" in entries[0]["content"]


class TestSearch:
    def test_search_hot(self, engine):
        engine.add("memory", "Python is great")
        engine.add("memory", "Rust is fast")
        result = engine.search("Python", source="hot")
        assert result["total"] == 1
        assert "Python" in result["hot"][0]["content"]

    def test_search_all(self, engine):
        engine.add("memory", "hot Python entry")
        engine.storage.cold_add("memory", "cold Python entry")
        result = engine.search("Python")
        assert result["total"] == 2


class TestStats:
    def test_stats(self, engine):
        engine.add("memory", "test")
        engine.add("user", "name: Bob")
        s = engine.stats()
        assert s["hot_memory_count"] == 1
        assert s["hot_user_count"] == 1
        assert "hot_memory_usage" in s
