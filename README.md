<p align="center">
  <h1 align="center">🦛 OpenHippo</h1>
  <p align="center"><strong>Local-first memory engine for AI agents</strong></p>
  <p align="center">
    <a href="https://github.com/wpsl5168/OpenHippo/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
    <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python"></a>
    <img src="https://img.shields.io/badge/status-alpha-orange.svg" alt="Status">
  </p>
</p>

---

OpenHippo is an open-source, local-first memory engine designed for AI agents. It provides persistent, searchable memory with hot/cold tiering, hybrid retrieval (full-text + semantic vector search), and a clean REST API — all backed by SQLite. No external database is required. Local embedding keeps memory and query text on your machine; an explicitly configured remote embedding provider receives the text sent for inference.

## The Problem

Today's AI agents have amnesia. Every conversation starts from scratch. The "memory" solutions that exist are either cloud-hosted (your data goes to someone else's server), locked into a specific framework, or require the agent itself to decide what to remember — which is like asking you to consciously manage your own hippocampus.

**We believe memory should be a separate, autonomous system** — just like the human brain. Your hippocampus doesn't ask for permission to form memories. It runs in the background, silently encoding experiences, consolidating knowledge during sleep, and surfacing relevant context when you need it.

OpenHippo is that hippocampus for AI agents:
- **Decoupled** — Memory is not a feature inside the agent; it's an independent service. Any agent, any framework, any VM can connect.
- **Automatic** — Hook into the agent's lifecycle. Memories are captured and recalled without explicit commands.
- **Transparent** — Unlike a real hippocampus, this one is fully auditable. Users can inspect, edit, and delete any memory at any time. Zero opacity.

## Why OpenHippo over alternatives?

Most agent memory solutions (Mem0, Zep, etc.) are either cloud-hosted or tightly coupled to a specific framework. OpenHippo takes a different approach:

- **Local-first** — SQLite + [sqlite-vec](https://github.com/asg017/sqlite-vec) for storage and vector search. No external database needed.
- **Provider-aware privacy** — SQLite remains local. Local sentence-transformers or a locally hosted Ollama can keep inference local; remote Ollama and Copilot endpoints receive embedding inputs.
- **Hot/cold tiering** — Frequently accessed memories stay "hot" (fast, capacity-limited); older entries archive to "cold" storage with full vector indexing.
- **Hybrid retrieval** — Combines FTS5 full-text search with vector similarity via Reciprocal Rank Fusion (RRF).
- **Semantic deduplication** — Prevents storing near-duplicate entries using both exact hash and vector distance checks.
- **Agent integration** — Hook/plugin system for seamless, zero-config memory sync with AI agents. Also exposes a REST API for direct access.
- **Auditable** — Full CRUD operations on stored memories. Timeline browsing. Operation logs. Users can inspect, edit, and delete any memory.
- **Markdown-rendered UI** — Memory content is rendered as Markdown in the web UI (lists, code blocks, tables, links), so structured notes from agents stay readable instead of collapsing into a wall of text.
- **Source attribution** — Cold memories track `originator` (user/assistant) and `channel` (weixin/feishu/telegram/cli/discord/...), surfaced in the UI as a colored left border + channel emoji so you can tell at a glance who said what and where.

## Architecture

```
┌─────────────────────────────────────────────────┐
│              AI Agent (Hermes, etc.)             │
└────────┬──────────────────────────┬──────────────┘
         │ hooks (auto-sync)        │ REST API
         ▼                          ▼
┌──────────────┐            ┌──────────────┐
│  Plugin/Hook │            │   REST API   │
│  (pre_llm    │            │  (FastAPI)   │
│   post_llm   │            │  127.0.0.1   │
│   post_tool) │            │  no auth     │
└──────┬───────┘            └──────┬───────┘
       │                          │
       └────────────┬─────────────┘
                    │
             ┌──────▼───────┐
             │  HippoEngine │  ← dedup, tiering, search
             └──────┬───────┘
                    │
        ┌───────────┼───────────┐
        │           │           │
  ┌─────▼───┐ ┌────▼───┐ ┌────▼─────┐
  │ Storage  │ │Embedding│ │  Config  │
  │ (SQLite  │ │Provider │ │  (YAML)  │
  │  +vec)   │ │ (local/ │ │          │
  │          │ │ ollama) │ │          │
  └──────────┘ └────────┘ └──────────┘
```

## Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/wpsl5168/OpenHippo.git
cd OpenHippo

# Install with local embedding support (recommended)
pip install -e ".[local]"

# Or minimal install (requires Ollama for embeddings)
pip install -e .
```

### Run the Server

```bash
# Start the REST API server (default: http://localhost:8200)
openhippo serve --port 8200

# Or run directly with uvicorn
uvicorn openhippo.api.rest:app --host 0.0.0.0 --port 8200
```

### Basic Usage

```bash
# Store a memory
curl -X POST http://localhost:8200/v1/memories \
  -H "Content-Type: application/json" \
  -d '{"target": "memory", "content": "User prefers dark mode in all applications"}'

# Search memories (hybrid: full-text + vector)
curl -X POST http://localhost:8200/v1/memories/search \
  -H "Content-Type: application/json" \
  -d '{"query": "UI preferences", "mode": "hybrid"}'

# View hot memories
curl http://localhost:8200/v1/memories/hot

# Browse cold memory timeline
curl http://localhost:8200/v1/memories/timeline?limit=20

# Get system stats
curl http://localhost:8200/v1/stats
```

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/memories` | Store a new memory |
| `POST` | `/v1/memories/search` | Hybrid search (FTS + vector + RRF) |
| `POST` | `/v1/memories/replace` | Replace a hot memory by substring match |
| `POST` | `/v1/memories/remove` | Remove a hot memory by substring match |
| `POST` | `/v1/memories/archive` | Move a hot memory to cold storage |
| `POST` | `/v1/memories/promote` | Promote a cold memory back to hot |
| `GET` | `/v1/memories/hot` | List all hot memories |
| `GET` | `/v1/memories/timeline` | Browse cold memories chronologically |
| `GET` | `/v1/memories/{id}` | Get a single memory by ID |
| `PUT` | `/v1/memories/{id}` | Update a cold memory |
| `DELETE` | `/v1/memories/{id}` | Delete a cold memory |
| `GET` | `/v1/stats` | Storage statistics |
| `GET` | `/v1/logs` | Operation audit log |
| `POST` | `/v1/embeddings/backfill` | Generate missing embeddings |
| `POST` | `/v1/dream/preview` | Preview consolidation clusters (non-mutating) |
| `POST` | `/v1/dream/run` | Execute a dream cycle (consolidate + optional forget) |
| `POST` | `/v1/dream/restore/{id}` | Reverse a consolidate/forget action |
| `GET` | `/v1/dream/runs` | List recent dream cycles |
| `GET` | `/v1/dream/runs/{id}` | Single dream run + audit trail |
| `GET` | `/v1/dream/metrics` | Persistent + scheduler observability snapshot |
| `GET` | `/health` | Health check |

> See [`docs/F5_DREAM.md`](docs/F5_DREAM.md) for the full F5 Dream guide — staging model, configuration, auto-scheduler, and observability.

## Configuration

OpenHippo uses a YAML config file with environment variable overrides.

```bash
# Copy the example config
cp config.example.yaml ~/.hippocampus/config.yaml
```

```yaml
# ~/.hippocampus/config.yaml
storage:
  db_path: ~/.hippocampus/memory.db

embedding:
  provider: local              # auto | local | ollama | copilot
  local:
    model: nomic-ai/nomic-embed-text-v1.5
    device: cpu

  ollama:
    base_url: http://localhost:11434

server:
  host: 127.0.0.1
  port: 8200
```

Supported overrides are listed in `src/openhippo/core/config.py:ENV_MAP`. For example:

```bash
HIPPO_EMBEDDING_PROVIDER=ollama  # Switch to Ollama backend
HIPPO_DB_PATH=/data/memory.db    # Custom database path
HIPPO_PORT=9000           # Custom port
```

## Embedding Backends

| Backend | Install | GPU Required | Model Size | Notes |
|---------|---------|-------------|------------|-------|
| **sentence-transformers** (default) | `pip install -e ".[local]"` | No (CPU OK) | ~80 MB | Zero external dependencies |
| **Ollama** | [ollama.com](https://ollama.com) | No | ~270 MB | Shared with other Ollama models |

`auto` probes the configured Ollama endpoint before trying sentence-transformers. Select a provider explicitly for reproducible deployments. The local defaults use nomic models and 768 dimensions, but equal dimensions do not prove equal embedding spaces.

An optional `copilot` provider uses the configured account and `text-embedding-3-small` at 768 dimensions. It requires an authorized account and sends input text to the remote service. It is not part of offline tests. Legacy `openai` configuration keys are not an implemented provider selection.

> **Note on long content (Ollama backend):** `nomic-embed-text` has a 2048-token context window. Inputs over ~1800 chars (especially CJK text, where one character ≈ 2-3 tokens) trigger HTTP 500 from Ollama. `OllamaProvider` automatically truncates prompts to `MAX_PROMPT_CHARS=1800` and falls back through `[1800 → 1200 → 800 → 400]` chars on 500 errors, so long memories embed cleanly without manual chunking. If you need the full content searchable as multiple vectors, split it into chunks before calling `add_memory`.

## Agent Integration (Hook/Plugin)

OpenHippo integrates with AI agents via a **hook/plugin system** — no manual API calls needed. The agent's memory operations are automatically mirrored to OpenHippo in the background.

**Three hooks, fully automatic:**

| Hook | Trigger | What it does |
|------|---------|-------------|
| `pre_llm_call` | Before each LLM request | Semantic search → inject relevant memories as context |
| `post_llm_call` | After LLM response | Extract memorable facts from conversation (rule-based) |
| `post_tool_call` | After `memory` tool use | Mirror add/replace/remove operations to OpenHippo |

**Setup (Hermes Agent example):**

```bash
# Copy plugin to agent's plugin directory
cp -r plugin/hermes ~/.hermes/plugins/openhippo

# Configure endpoint (local)
export HIPPO_BASE_URL=http://localhost:8200

# Restart your agent — done. Memory sync is fully automatic.
```

**Offline resilience:** When OpenHippo is unreachable, writes are cached to a local WAL (Write-Ahead Log) and replayed automatically on reconnection.

## Remote Access (Advanced)

OpenHippo is **local-first**. The CLI binds to `127.0.0.1` by default and the API ships with **no authentication**. This restricts inbound access only; remote embedding still sends input text to the configured provider. A custom service may bind a different address.

If you want to access OpenHippo from another machine (e.g. your phone, a remote agent), **do not just `--host 0.0.0.0`**. Put a reverse proxy with proper authentication in front. Recommended patterns:

| Setup | Best For | Difficulty |
|---|---|---|
| **Tailscale / WireGuard** | Personal remote access, no public DNS | ⭐ easy |
| **Caddy + Cloudflare Access** (Email OTP / SSO) | Public domain, single user / small team | ⭐⭐ medium |
| **Nginx + OAuth2-Proxy** (GitHub / Google) | Self-hosted, full control | ⭐⭐⭐ harder |

**Minimal Caddy + Cloudflare Access example** (replace `hippo.example.com`):

```caddyfile
hippo.example.com {
    tls {
        dns cloudflare {env.CLOUDFLARE_API_TOKEN}
    }
    reverse_proxy 127.0.0.1:8200
}
```

Then in Cloudflare Zero Trust → Access → Applications, add a Self-hosted app for `hippo.example.com` with an Email OTP policy restricted to your address.

> ⚠️ **Without auth in front, anyone on the network can read, edit, and delete every memory in your database.** OpenHippo intentionally does not implement auth itself — battle-tested reverse proxies do it better.

## Export attribution and backup scope

Release **0.4.1** uses JSON/JSONL backup schema **1.1** (separate version numbers). `GET /v1/export` filters by `target`, `since`, `until` and `tags`; **`exporter_agent_id` only annotates the JSON/JSONL header**. The old `agent_id` parameter is a deprecated alias, not a record filter or authenticated principal. Conflicting aliases return HTTP 400. Markdown/CSV ignore attribution and are human-readable views, not full-fidelity backups. Never expose this endpoint as a tenant-isolated export without a separately designed authorization boundary.

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run offline synthetic tests (temporary HOME, TCP denied, no model download)
python scripts/run_offline_tests.py

# Lint
ruff check src/

# Type check
mypy src/
```

## FAQ: Why local-first?

**Q: Why not use a hosted memory service like Mem0 or Zep?**
Local-first means you own the SQLite store and can use local inference without an external database. It does not guarantee zero egress: remote embedding sends memory/query text to its provider, whose retention and billing policies apply. Choose a local provider explicitly for offline deployments.

**Q: Is SQLite really fast enough for memory storage?**
Measure your corpus and configured provider. OpenHippo uses SQLite WAL, FTS5 and sqlite-vec exact vector search; latency includes embedding, filtering and optional access accounting. There is no universal sub-50ms or corpus-size guarantee. Keep the existing database until measured load justifies a migration.

**Q: How do I integrate OpenHippo with my existing agent?**
Two paths, pick whichever fits. The hook/plugin route auto-captures conversations from supported agent frameworks with zero glue code (see [Agent Integration (Hook/Plugin)](#agent-integration-hookplugin)). The REST API route gives you full CRUD over `/v1/memories` and search via `/v1/memories/search` — works with any language or framework that speaks HTTP.

**Q: How do I back up my memories?**
The default database is `~/.hippocampus/memory.db`. While the service runs, use SQLite backup API or a coordinated filesystem snapshot: copying only the live `.db` file can omit committed WAL data. Restore to a separate path and verify integrity before any cutover. JSON/JSONL 1.1 preserve memory fields, vectors and space metadata; they do not include every queue/Dream audit table. See [data-safe release and rollback](docs/data-safety-release.md). Never overwrite newer production data with an older backup.

**Q: Can I run completely offline (no network)?**
Yes, after dependencies and model files have been provisioned locally. Select `local` or an Ollama endpoint on loopback; downloading model weights initially needs network access. Local inference and SQLite work offline. The browser UI currently loads third-party CDN assets, so an air-gapped UI also needs locally vendored assets.

**Q: How is this project developed?**
Dev/QA/Ship three-stage workflow via the `hermes-team` wrapper — a developer agent writes the code, a QA agent reviews it, and a ship agent merges to main and cleans up branches. See `~/.hermes/skills/devops/team-sop` for the full SOP.

## Roadmap

- [x] Hot/cold memory tiering with capacity management
- [x] FTS5 full-text search
- [x] Vector semantic search (sqlite-vec)
- [x] Hybrid retrieval with RRF fusion
- [x] Semantic deduplication
- [x] REST API with full CRUD
- [x] Hook/plugin agent integration (auto-sync)
- [x] Audit log and memory timeline
- [x] Pluggable embedding backends (local / Ollama)
- [x] Unified YAML + env config system
- [x] Remote-access guidance (authentication belongs to the trusted proxy; no built-in API auth)
- [x] Docker image and compose deployment
- [x] Remote agent connection (multi-VM support)
- [x] F5 Dream — sleep-inspired memory consolidation (cluster + consolidate + soft forget + restore)
- [x] Auto-scheduler with metrics observability (`/v1/dream/metrics`)
- [ ] Multi-tenant support
- [x] Web UI for memory inspection
- [ ] Webhook / event-driven memory triggers

## License

[MIT](LICENSE)

---

<p align="center">
  <sub>Built with 🧠 by <a href="https://github.com/wpsl5168">Pei Wang</a></sub>
</p>
