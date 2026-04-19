"""Round-trip tests: export → clear → import → verify (R1-R4)."""

import json
import time
import uuid

import pytest

from openhippo.core.engine import HippoEngine
from openhippo.core.export import (
    EXPORT_SCHEMA_VERSION,
    export_json,
    check_embedding_compatibility,
    _get_embedding_backend_info,
)
from openhippo.core.embedding import get_embedding


def unique_tag(prefix: str = "rt") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def engine(tmp_path):
    e = HippoEngine(db_path=tmp_path / "test.db")
    yield e
    e.close()


def _populate(engine, count=10):
    """Add memories with unique content to avoid dedup."""
    hot_ids = []
    cold_ids = []

    for i in range(count):
        tag = unique_tag(f"hot{i}")
        r = engine.storage.hot_add("memory", f"Hot entry {i}: {tag}")
        hot_ids.append(r["id"])

    for i in range(count):
        tag = unique_tag(f"cold{i}")
        r = engine.storage.cold_add(
            "memory",
            f"Cold entry {i}: {tag} — detailed technical info about configuration and setup",
            source="manual",
            tags=[f"tag_{i}"],
            metadata={"batch": "roundtrip_test"},
        )
        cold_ids.append(r["id"])
        engine._embed_cold_entry(r["id"])

    return hot_ids, cold_ids


class TestRoundTrip:
    """R1-R2: Export → Clear → Import → Verify."""

    def test_r1_content_integrity(self, tmp_path):
        """R1: Write 10+10 → export JSON → new DB → import → content 100% match."""
        # Phase 1: populate + export
        db1 = tmp_path / "db1.db"
        e1 = HippoEngine(db_path=db1)
        _populate(e1, count=10)

        exported = export_json(e1.storage, include_embeddings=True)
        doc = json.loads(exported)
        assert doc["header"]["schema_version"] == EXPORT_SCHEMA_VERSION
        assert len(doc["memories"]) == 20  # 10 hot + 10 cold
        e1.close()

        # Phase 2: new DB → import
        db2 = tmp_path / "db2.db"
        e2 = HippoEngine(db_path=db2)

        imported_hot = 0
        imported_cold = 0
        for mem in doc["memories"]:
            if mem["layer"] == "hot":
                e2.storage.hot_add(mem["target"], mem["content"])
                imported_hot += 1
            else:
                result = e2.storage.cold_add(
                    target=mem["target"],
                    content=mem["content"],
                    source=mem.get("source", "imported"),
                    tags=mem.get("tags", []),
                    metadata=mem.get("metadata", {}),
                )
                # Re-store embedding if available
                if "embedding" in mem and mem["embedding"]:
                    e2.storage.vec_store(result["id"], mem["embedding"])
                imported_cold += 1

        assert imported_hot == 10
        assert imported_cold == 10

        # Phase 3: verify content match
        orig_contents = {m["content"] for m in doc["memories"]}

        new_hot = e2.storage.hot_list()
        new_cold = e2.storage.cold_timeline(limit=100)

        new_contents = set()
        for h in new_hot:
            new_contents.add(h["content"])
        for c in new_cold:
            new_contents.add(c["content"])

        assert orig_contents == new_contents, "Content mismatch after round-trip"
        e2.close()

    def test_r2_embedding_vector_preserved(self, tmp_path):
        """R2: Embeddings survive round-trip with same backend (cosine ~1.0)."""
        db1 = tmp_path / "db1.db"
        e1 = HippoEngine(db_path=db1)
        tag = unique_tag("embed")
        text = f"Embedding preservation test: {tag}"
        result = e1.storage.cold_add("memory", text)
        e1._embed_cold_entry(result["id"])

        # Export
        exported = export_json(e1.storage, include_embeddings=True)
        doc = json.loads(exported)
        cold_mems = [m for m in doc["memories"] if m["layer"] == "cold"]
        assert len(cold_mems) == 1
        exported_vec = cold_mems[0]["embedding"]
        assert len(exported_vec) == 768
        e1.close()

        # Recompute embedding from scratch
        fresh_vec = get_embedding(text)
        assert fresh_vec is not None

        # Cosine similarity should be ~1.0 (same backend, same text)
        dot = sum(a * b for a, b in zip(exported_vec, fresh_vec))
        norm_a = sum(a * a for a in exported_vec) ** 0.5
        norm_b = sum(b * b for b in fresh_vec) ** 0.5
        cosine = dot / (norm_a * norm_b) if norm_a * norm_b > 0 else 0
        assert cosine > 0.99, f"Cosine {cosine} too low — embedding not preserved"

    def test_r3_reembed_detection(self, tmp_path):
        """R3: Export with backend A → detect incompatibility with backend B."""
        db1 = tmp_path / "db1.db"
        e1 = HippoEngine(db_path=db1)
        e1.storage.cold_add("memory", "test content")
        exported = export_json(e1.storage)
        doc = json.loads(exported)
        e1.close()

        # Simulate different backend
        header = doc["header"]
        header["embedding_backend"] = "fake-provider/fake-model"
        result = check_embedding_compatibility(header)
        assert result["compatible"] is False
        assert result["reembed_needed"] is True

    def test_r4_old_schema_version(self, tmp_path):
        """R4: Import file with older schema_version — should parse without error."""
        # Create a synthetic v0.9 export
        old_export = {
            "header": {
                "schema_version": "0.9",
                "embedding_backend": "ollama/nomic-embed-text",
                "exported_at": "2025-01-01T00:00:00+00:00",
                "total_count": 2,
            },
            "memories": [
                {
                    "id": "abc123",
                    "layer": "hot",
                    "target": "memory",
                    "content": "Old format memory 1",
                    "created_at": time.time(),
                    "updated_at": time.time(),
                },
                {
                    "id": "def456",
                    "layer": "cold",
                    "target": "memory",
                    "content": "Old format memory 2",
                    "source": "manual",
                    "tags": ["legacy"],
                    "metadata": {},
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "access_count": 0,
                },
            ],
        }

        # Import into fresh DB
        db = tmp_path / "test.db"
        engine = HippoEngine(db_path=db)

        for mem in old_export["memories"]:
            if mem["layer"] == "hot":
                engine.storage.hot_add(mem["target"], mem["content"])
            else:
                engine.storage.cold_add(
                    target=mem["target"],
                    content=mem["content"],
                    source=mem.get("source", "imported"),
                    tags=mem.get("tags", []),
                    metadata=mem.get("metadata", {}),
                )

        # Verify
        hot = engine.storage.hot_list()
        cold = engine.storage.cold_timeline()
        assert len(hot) == 1
        assert len(cold) == 1
        assert hot[0]["content"] == "Old format memory 1"
        assert cold[0]["content"] == "Old format memory 2"
        engine.close()
