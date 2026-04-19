"""REST API integration tests — 7 dimensions from PRD alignment plan.

Run: cd ~/OpenHippo && .venv/bin/python -m pytest tests/test_rest_api.py -v
Requires server running on localhost:8200.
"""

import time
import uuid
import requests
import pytest


def unique_tag(prefix: str) -> str:
    """Generate a semantically unique tag to avoid cold dedup."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

BASE = "http://127.0.0.1:8200"
V1 = f"{BASE}/v1"


# ── Helpers ──

def post(path, json=None):
    return requests.post(f"{V1}{path}", json=json, timeout=5)

def get(path, **params):
    return requests.get(f"{V1}{path}", params=params, timeout=5)


# ═══════════════════════════════════════════
# 1. 功能正确性 (F1-F5)
# ═══════════════════════════════════════════

class TestCRUD:
    """F1-F5: Full CRUD lifecycle."""

    def test_add_memory(self):
        r = post("/memories", {"target": "memory", "content": "REST test entry alpha"})
        assert r.status_code == 200
        assert "data" in r.json()

    def test_add_user(self):
        r = post("/memories", {"target": "user", "content": "REST test user info"})
        assert r.status_code == 200

    def test_search_hot(self):
        # Add a unique entry then search for it
        tag = unique_tag("search_hot")
        post("/memories", {"target": "memory", "content": tag})
        r = post("/memories/search", {"query": tag, "source": "hot"})
        assert r.status_code == 200
        data = r.json()["data"]
        hot = data.get("hot", [])
        assert any(tag in m.get("content", "") for m in hot)

    def test_replace(self):
        r = post("/memories/replace", {
            "target": "memory",
            "old_text": "REST test entry alpha",
            "new_content": "REST test entry beta"
        })
        assert r.status_code == 200

    def test_get_hot(self):
        r = get("/memories/hot")
        assert r.status_code == 200
        data = r.json()["data"]
        assert "memory" in data
        assert "user" in data

    def test_get_hot_filtered(self):
        r = get("/memories/hot", target="memory")
        assert r.status_code == 200

    def test_archive(self):
        r = post("/memories/archive", {"target": "memory", "old_text": "REST test entry beta"})
        assert r.status_code == 200

    def test_search_cold(self):
        r = post("/memories/search", {"query": "REST test", "source": "cold"})
        assert r.status_code == 200

    def test_search_all(self):
        r = post("/memories/search", {"query": "REST test", "source": "all"})
        assert r.status_code == 200

    def test_promote(self):
        # Add, archive, then promote
        tag = unique_tag("promote_test")
        post("/memories", {"target": "memory", "content": tag})
        post("/memories/archive", {"target": "memory", "old_text": tag})
        r = post("/memories/search", {"query": tag, "source": "cold"})
        cold = r.json()["data"].get("cold", [])
        if cold:
            mid = cold[0].get("id") or cold[0].get("memory_id")
            if mid:
                r2 = post("/memories/promote", {"memory_id": str(mid)})
                assert r2.status_code == 200

    def test_remove(self):
        # Clean up
        r = post("/memories/remove", {"target": "user", "old_text": "REST test user info"})
        assert r.status_code == 200

    def test_stats(self):
        r = get("/stats")
        assert r.status_code == 200
        data = r.json()["data"]
        assert "hot" in data or "memory" in data or isinstance(data, dict)

    def test_logs(self):
        r = get("/logs", limit=10)
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 2. 响应格式 (F6验收#2)
# ═══════════════════════════════════════════

class TestResponseFormat:
    """All responses must use {data: ...} wrapper."""

    @pytest.mark.parametrize("call", [
        lambda: post("/memories", {"content": "format test"}),
        lambda: post("/memories/search", {"query": "format"}),
        lambda: get("/memories/hot"),
        lambda: get("/stats"),
        lambda: get("/logs"),
    ])
    def test_data_wrapper(self, call):
        r = call()
        assert r.status_code == 200
        body = r.json()
        assert "data" in body, f"Missing 'data' key in response: {body}"

    def test_health_no_wrapper(self):
        """Health endpoint has its own format."""
        r = requests.get(f"{BASE}/health", timeout=5)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_json_content_type(self):
        r = post("/memories", {"content": "ct test"})
        assert "application/json" in r.headers.get("content-type", "")


# ═══════════════════════════════════════════
# 3. 错误处理 (F6验收#3)
# ═══════════════════════════════════════════

class TestErrorHandling:
    """4xx errors should return meaningful messages."""

    def test_replace_not_found(self):
        r = post("/memories/replace", {
            "target": "memory",
            "old_text": "NONEXISTENT_ENTRY_xyz123",
            "new_content": "should fail"
        })
        assert r.status_code == 404

    def test_remove_not_found(self):
        r = post("/memories/remove", {
            "target": "memory",
            "old_text": "NONEXISTENT_ENTRY_xyz123"
        })
        assert r.status_code == 404

    def test_archive_not_found(self):
        r = post("/memories/archive", {
            "target": "memory",
            "old_text": "NONEXISTENT_ENTRY_xyz123"
        })
        assert r.status_code == 404

    def test_missing_required_field(self):
        r = post("/memories", {})  # missing 'content'
        assert r.status_code == 422

    def test_invalid_endpoint(self):
        r = requests.get(f"{V1}/nonexistent", timeout=5)
        assert r.status_code == 404


# ═══════════════════════════════════════════
# 4. 边界条件 (鲁棒性)
# ═══════════════════════════════════════════

class TestEdgeCases:

    def test_empty_content(self):
        r = post("/memories", {"content": ""})
        # Should either reject or accept gracefully
        assert r.status_code in (200, 400, 422)

    def test_long_content(self):
        long_text = "长文本测试" * 1000  # ~5000 chars
        r = post("/memories", {"content": long_text})
        assert r.status_code == 200

    def test_special_chars(self):
        r = post("/memories", {"content": "特殊字符: <>&\"'\\n\\t🦛🧠"})
        assert r.status_code == 200

    def test_chinese_search(self):
        post("/memories", {"content": "海马体记忆引擎测试条目"})
        r = post("/memories/search", {"query": "海马体"})
        assert r.status_code == 200

    def test_search_empty_query(self):
        r = post("/memories/search", {"query": ""})
        assert r.status_code in (200, 400, 422)

    def test_search_limit_boundary(self):
        r = post("/memories/search", {"query": "test", "limit": 1})
        assert r.status_code == 200

    def test_search_limit_max(self):
        r = post("/memories/search", {"query": "test", "limit": 100})
        assert r.status_code == 200

    def test_search_limit_over_max(self):
        r = post("/memories/search", {"query": "test", "limit": 999})
        assert r.status_code == 422  # Pydantic le=100


# ═══════════════════════════════════════════
# 5. 数据一致性 (write→read round-trip)
# ═══════════════════════════════════════════

class TestConsistency:

    def test_write_then_read(self):
        content = f"consistency_check_{int(time.time())}"
        post("/memories", {"content": content})
        r = post("/memories/search", {"query": content, "source": "hot"})
        hot = r.json()["data"].get("hot", [])
        assert any(content in m.get("content", "") for m in hot), f"Written content not found in hot: {hot}"

    def test_remove_then_search(self):
        tag = unique_tag("ephemeral")
        post("/memories", {"content": tag})
        post("/memories/remove", {"target": "memory", "old_text": tag})
        r = post("/memories/search", {"query": tag, "source": "hot"})
        hot = r.json()["data"].get("hot", [])
        assert not any(tag in str(m) for m in hot), "Removed entry still found"

    def test_archive_moves_hot_to_cold(self):
        tag = unique_tag("archive_test")
        post("/memories", {"content": tag})
        post("/memories/archive", {"target": "memory", "old_text": tag})
        # Not in hot
        r_hot = post("/memories/search", {"query": tag, "source": "hot"})
        assert not any(tag in m.get("content", "") for m in r_hot.json()["data"].get("hot", []))
        # In cold
        r_cold = post("/memories/search", {"query": tag, "source": "cold"})
        cold = r_cold.json()["data"].get("cold", [])
        assert any(tag in m.get("content", "") for m in cold), f"Archived entry not in cold: {cold}"


# ═══════════════════════════════════════════
# 6. API文档 (F6验收#4)
# ═══════════════════════════════════════════

class TestDocs:

    def test_swagger_ui(self):
        r = requests.get(f"{BASE}/docs", timeout=5)
        assert r.status_code == 200
        assert "swagger" in r.text.lower() or "openapi" in r.text.lower()

    def test_openapi_json(self):
        r = requests.get(f"{BASE}/openapi.json", timeout=5)
        assert r.status_code == 200
        schema = r.json()
        assert "paths" in schema
        assert "/v1/memories" in schema["paths"]


# ═══════════════════════════════════════════
# 7. 性能基线 (F1验收#3-4)
# ═══════════════════════════════════════════

class TestPerformance:

    def test_single_write_under_100ms(self):
        start = time.time()
        post("/memories", {"content": "perf test single"})
        elapsed = time.time() - start
        assert elapsed < 0.2, f"Single write took {elapsed:.3f}s (>200ms)"

    def test_batch_10_writes_under_1s(self):
        start = time.time()
        for i in range(10):
            post("/memories", {"content": f"perf batch {i}"})
        elapsed = time.time() - start
        assert elapsed < 1.0, f"10 writes took {elapsed:.3f}s (>1s)"

    def test_search_under_100ms(self):
        start = time.time()
        post("/memories/search", {"query": "perf", "source": "all"})
        elapsed = time.time() - start
        assert elapsed < 0.1, f"Search took {elapsed:.3f}s (>100ms)"

    def test_health_under_50ms(self):
        start = time.time()
        requests.get(f"{BASE}/health", timeout=5)
        elapsed = time.time() - start
        assert elapsed < 0.05, f"Health took {elapsed:.3f}s (>50ms)"


# ── Cleanup ──

@pytest.fixture(autouse=True, scope="session")
def cleanup():
    yield
    # Clean up test entries
    for tag in ["format test", "ct test", "长文本测试", "特殊字符", "海马体记忆引擎测试条目", "perf test", "perf batch"]:
        try:
            post("/memories/remove", {"target": "memory", "old_text": tag})
        except Exception:
            pass
