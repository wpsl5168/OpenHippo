"""Unit tests — pure stdlib, no live server required.

Verifies SDK contract:
  * never raises into caller
  * WAL fallback on transport failure
  * recall returns RecallResult on bad input
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from hippo_sdk import HippoClient, HippoConfig
from hippo_sdk.transport import HippoTransport
from hippo_sdk.wal import WriteAheadLog
from hippo_sdk.types import RecallResult


# ── fixtures ──────────────────────────────────────────────────────────────


class FakeTransport:
    """Records calls; can be configured to fail."""

    def __init__(self, post_returns=None, get_returns=None):
        self.post_returns = post_returns
        self.get_returns = get_returns
        self.posts: list[tuple[str, dict, float | None]] = []
        self.gets: list[tuple[str, float | None]] = []

    def post(self, path, payload, timeout=None):
        self.posts.append((path, payload, timeout))
        if callable(self.post_returns):
            return self.post_returns(path, payload)
        return self.post_returns

    def get(self, path, timeout=None):
        self.gets.append((path, timeout))
        if callable(self.get_returns):
            return self.get_returns(path)
        return self.get_returns

    def healthy(self) -> bool:
        return True


@pytest.fixture
def tmp_config(tmp_path):
    return HippoConfig(
        base_url="http://127.0.0.1:9999",  # unused with FakeTransport
        agent_id="test-agent",
        wal_dir=tmp_path / "wal",
    )


# ── HippoConfig ──────────────────────────────────────────────────────────


def test_config_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("HIPPO_BASE_URL", "http://custom:1234")
    monkeypatch.setenv("HIPPO_AGENT_ID", "env-agent")
    monkeypatch.setenv("HIPPO_SEARCH_TIMEOUT", "0.5")
    cfg = HippoConfig()
    assert cfg.base_url == "http://custom:1234"
    assert cfg.agent_id == "env-agent"
    assert cfg.search_timeout == 0.5


def test_config_headers_with_token():
    cfg = HippoConfig(bearer_token="abc")
    h = cfg.headers()
    assert h["Authorization"] == "Bearer abc"
    assert h["Content-Type"] == "application/json"


def test_config_headers_without_token():
    cfg = HippoConfig(bearer_token="")
    h = cfg.headers()
    assert "Authorization" not in h


# ── HippoClient.remember ──────────────────────────────────────────────────


def test_remember_success(tmp_config):
    fake = FakeTransport(post_returns={"data": {"id": "x", "status": "created"}})
    client = HippoClient(tmp_config, transport=fake)
    assert client.remember("hello world") is True
    assert fake.posts[0][0] == "/v1/memories"
    assert fake.posts[0][1]["content"] == "hello world"
    assert fake.posts[0][1]["agent_id"] == "test-agent"


def test_remember_empty_skipped(tmp_config):
    fake = FakeTransport(post_returns={"ok": True})
    client = HippoClient(tmp_config, transport=fake)
    assert client.remember("") is False
    assert client.remember("   ") is False
    assert fake.posts == []  # never even called


def test_remember_failure_writes_wal(tmp_config):
    fake = FakeTransport(post_returns=None)  # transport says "down"
    client = HippoClient(tmp_config, transport=fake)
    ok = client.remember("important fact")
    assert ok is False  # caller gets honest signal
    # but WAL captured it
    assert client.wal.pending_count() == 1


# ── HippoClient.recall ────────────────────────────────────────────────────


def test_recall_returns_result_object(tmp_config):
    server_resp = {
        "data": {
            "cold": [
                {"content": "fact A", "rrf_score": 0.5, "vec_distance": 0.6},
                {"content": "fact B", "rrf_score": 0.001, "vec_distance": 1.5},
            ]
        }
    }
    fake = FakeTransport(post_returns=server_resp)
    client = HippoClient(tmp_config, transport=fake)
    result = client.recall("query")
    assert isinstance(result, RecallResult)
    assert len(result.cold) == 2
    # filter applies score + distance gates
    filtered = result.filtered_cold(min_score=0.01, max_distance=1.2)
    assert len(filtered) == 1
    assert filtered[0].content == "fact A"


def test_recall_empty_query_skipped(tmp_config):
    fake = FakeTransport(post_returns={"data": {"cold": []}})
    client = HippoClient(tmp_config, transport=fake)
    result = client.recall("")
    assert result.cold == []
    assert fake.posts == []


def test_recall_server_down_returns_empty(tmp_config):
    fake = FakeTransport(post_returns=None)
    client = HippoClient(tmp_config, transport=fake)
    result = client.recall("anything")
    assert result.cold == []
    assert result.hot == []


# ── HippoClient.replace / remove / archive / promote ──────────────────────


def test_replace_round_trip(tmp_config):
    fake = FakeTransport(post_returns={"data": {"status": "replaced"}})
    client = HippoClient(tmp_config, transport=fake)
    ok = client.replace("memory", "old text", "new text")
    assert ok is True
    assert fake.posts[0][0] == "/v1/memories/replace"
    assert fake.posts[0][1]["new_content"] == "new text"


def test_replace_missing_args_skipped(tmp_config):
    fake = FakeTransport(post_returns={"ok": True})
    client = HippoClient(tmp_config, transport=fake)
    assert client.replace("memory", "", "new") is False
    assert client.replace("memory", "old", "") is False
    assert fake.posts == []


def test_remove_archive_promote(tmp_config):
    fake = FakeTransport(post_returns={"data": {}})
    client = HippoClient(tmp_config, transport=fake)
    assert client.remove("memory", "x") is True
    assert client.archive("memory", "x") is True
    assert client.promote("x") is True
    assert [p[0] for p in fake.posts] == [
        "/v1/memories/remove",
        "/v1/memories/archive",
        "/v1/memories/promote",
    ]


# ── cold_add ──────────────────────────────────────────────────────────────


def test_cold_add_full_payload(tmp_config):
    fake = FakeTransport(post_returns={"data": {"id": "c1"}})
    client = HippoClient(tmp_config, transport=fake)
    ok = client.cold_add(
        "session snapshot",
        source="snapshot",
        tags=["s1"],
        metadata={"k": "v"},
        scope="agent",
        session_id="sess-123",
    )
    assert ok is True
    payload = fake.posts[0][1]
    assert payload["content"] == "session snapshot"
    assert payload["tags"] == ["s1"]
    assert payload["session_id"] == "sess-123"
    assert payload["agent_id"] == "test-agent"


# ── WAL ────────────────────────────────────────────────────────────────────


def test_wal_append_and_replay(tmp_config):
    wal = WriteAheadLog(tmp_config)
    wal.append("/v1/memories", {"content": "queued"})
    assert wal.pending_count() == 1

    delivered = []

    def sender(path, payload):
        delivered.append((path, payload))
        return {"ok": True}

    n = wal.replay(sender)
    assert n == 1
    assert wal.pending_count() == 0
    assert delivered[0][1]["content"] == "queued"


def test_wal_retry_increment_and_dead_letter(tmp_config):
    tmp_config.wal_max_retries = 2
    wal = WriteAheadLog(tmp_config)
    wal.append("/v1/memories", {"content": "fail-me"})

    # sender always fails
    n1 = wal.replay(lambda p, pl: None)
    n2 = wal.replay(lambda p, pl: None)
    n3 = wal.replay(lambda p, pl: None)  # this attempt sees retries=2 → dead

    assert (n1, n2, n3) == (0, 0, 0)
    # Entry should now be in dead-letter
    assert wal.pending_count() == 0
    assert wal.dead.exists() and wal.dead.read_text(encoding="utf-8").strip() != ""


def test_drain_wal_via_client(tmp_config):
    fake = FakeTransport(post_returns=None)
    client = HippoClient(tmp_config, transport=fake)
    client.remember("a")
    client.remember("b")
    assert client.wal_pending() == 2

    # Server comes back; drain succeeds
    fake.post_returns = {"ok": True}
    n = client.drain_wal()
    assert n == 2
    assert client.wal_pending() == 0


# ── Transport (real urllib but against a localhost that won't answer) ────


def test_transport_handles_unreachable_host():
    cfg = HippoConfig(base_url="http://127.0.0.1:1")  # nothing listens here
    tx = HippoTransport(cfg)
    assert tx.post("/v1/memories", {"x": 1}, timeout=0.5) is None
    assert tx.get("/health", timeout=0.5) is None
    assert tx.healthy() is False
