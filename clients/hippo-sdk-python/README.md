# hippo-sdk

Universal client SDK for the **Hippo (海马体)** memory engine. Connect any AI
agent — Hermes, Claude Code, Cursor, your custom bot — to a shared, multi-agent
memory pool with five-minute integration.

## Why

Most agents reinvent memory storage badly. Hippo separates concerns:

- **Storage** — single Hippo server, shared across agents (per-agent slicing
  via `agent_id` + scope tags).
- **SDK (this package)** — drop-in client. Stdlib only. Zero new deps.
- **Your agent** — keeps its hooks/tools; just calls `hippo.remember(...)` and
  `hippo.recall(...)`.

## Install

```bash
pip install -e ./clients/hippo-sdk-python   # local dev
# (PyPI: coming soon)
```

## 5-minute integration

```python
from hippo_sdk import HippoClient

# 1. Construct once per agent process
hippo = HippoClient()  # reads HIPPO_BASE_URL, HIPPO_AGENT_ID from env

# 2. Wherever you'd persist a memory in your agent:
hippo.remember("user prefers dark mode")

# 3. Wherever you build LLM context:
result = hippo.recall("user preferences", limit=5)
for entry in result.filtered_cold(min_score=0.01, max_distance=1.2):
    print(entry.content)
```

## Resilience guarantees

- **Network failure** → write goes to a local WAL (`~/.hermes/plugins/openhippo/wal.jsonl` by default), retried on next call.
- **Server down** → recall returns empty result, never raises.
- **Bad payload** → swallowed, logged at DEBUG, agent continues.

The SDK **never raises into your agent code.** Period.

## Configuration

Environment variables (all optional):

| Var | Default | Purpose |
|---|---|---|
| `HIPPO_BASE_URL` | `http://127.0.0.1:8200` | Server endpoint |
| `HIPPO_TOKEN` | _(empty)_ | Bearer token for remote servers |
| `HIPPO_AGENT_ID` | `default-agent` | Your agent's identity (multi-tenant slicing) |
| `HIPPO_SEARCH_TIMEOUT` | `2.0` | Recall timeout (seconds) |
| `HIPPO_WRITE_TIMEOUT` | `5.0` | Write timeout (seconds) |
| `HIPPO_WAL_DIR` | `~/.hermes/plugins/openhippo` | WAL directory |

## Roadmap

- **v0.1 (P1, current)** — extract from Hermes plugin; preserve semantics
- **v0.2 (P2)** — async outbox + LRU recall cache (zero-latency main loop)
- **v0.3 (P3)** — `X-Agent-ID` header + scope (`private/shared/global`)
- **v0.4 (P4)** — local SoT (Source of Truth) JSONL mirror + rebuild CLI
- **v0.5 (P5)** — export / backup / restore / merge tooling

See `~/obsidian-vault/20-项目/海马体/架构方案-v0.4-多agent共享池.md` for full spec.

## License

MIT.
