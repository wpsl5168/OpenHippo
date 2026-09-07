"""Release contracts: metadata is not authorization; configuration stays isolated."""
import copy
import csv
import io
import json

import pytest

from openhippo import __version__
from openhippo.core import config
from openhippo.core.engine import HippoEngine


def test_config_env_never_mutates_defaults(monkeypatch, tmp_path):
    baseline = copy.deepcopy(config.DEFAULTS)
    monkeypatch.setenv("HIPPO_OPENAI_API_KEY", "synthetic-not-a-secret")
    config.load_config(tmp_path / "missing.yaml")
    assert config.DEFAULTS == baseline
    monkeypatch.delenv("HIPPO_OPENAI_API_KEY")
    assert config.load_config(tmp_path / "missing.yaml")["embedding"]["openai"]["api_key"] == baseline["embedding"]["openai"]["api_key"]


def test_yaml_config_is_actually_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("HIPPO_PORT", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("server:\n  port: 9182\n", encoding="utf-8")
    assert config.load_config(path)["server"]["port"] == 9182


def test_health_and_openapi_versions_match(client):
    assert client.get("/health").json()["version"] == __version__
    assert client.get("/openapi.json").json()["info"]["version"] == __version__


@pytest.fixture
def export_api(client, tmp_path, monkeypatch):
    import openhippo.api.rest as rest
    engine = HippoEngine(tmp_path / "contract.db")
    ids = []
    for agent in ("agent-A", "agent-B"):
        item = engine.storage.cold_add("memory", "contract-" + agent, agent_id=agent)
        ids.append(item["id"] if isinstance(item, dict) else item)
    monkeypatch.setattr(rest, "_engine", lambda: engine)
    yield client, ids
    engine.storage.close()


@pytest.mark.parametrize("format", ["json", "jsonl", "markdown", "csv"])
@pytest.mark.parametrize("parameter", ["agent_id", "exporter_agent_id"])
def test_export_annotation_never_filters_any_format(export_api, format, parameter):
    client, ids = export_api
    response = client.get("/v1/export", params={"format": format, parameter: "agent-A", "include_embeddings": "false"})
    assert response.status_code == 200
    if format == "json":
        doc = response.json()
        header, records = doc["header"], doc["memories"]
    elif format == "jsonl":
        lines = [json.loads(line) for line in response.text.splitlines()]
        header, records = lines[0]["__header__"], lines[1:]
    elif format == "csv":
        records = list(csv.DictReader(io.StringIO(response.text)))
        assert set(ids) == {row["id"] for row in records}
        return
    else:
        assert "contract-agent-A" in response.text and "contract-agent-B" in response.text
        return
    assert set(ids) == {row["id"] for row in records}
    assert header["total_count"] == len(records) == 2
    assert header["agent_id"] == header["exporter_agent_id"] == "agent-A"
    assert header["agent_id_semantics"] == "exporter_annotation_not_filter"


def test_conflicting_export_aliases_rejected(export_api):
    client, _ = export_api
    response = client.get("/v1/export?agent_id=A&exporter_agent_id=B")
    assert response.status_code == 400


def test_export_openapi_warns_not_filter(client):
    parameters = client.get("/openapi.json").json()["paths"]["/v1/export"]["get"]["parameters"]
    by_name = {p["name"]: p for p in parameters}
    assert by_name["agent_id"]["deprecated"] is True
    assert "never filters" in by_name["exporter_agent_id"]["description"]
