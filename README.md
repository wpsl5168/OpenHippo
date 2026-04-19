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

OpenHippo is an open-source, local-first memory engine designed for AI agents. It provides persistent, searchable memory with hot/cold tiering, hybrid retrieval (full-text + semantic vector search), and a clean REST API — all backed by SQLite. No cloud dependency. No vendor lock-in. Your data stays on your machine.

## Why OpenHippo?

Most AI agent memory solutions are either cloud-hosted (privacy concerns) or tightly coupled to a specific framework. OpenHippo takes a different approach:

- **Local-first** — SQLite + [sqlite-vec](https://github.com/asg017/sqlite-vec) for storage and vector search. No external database needed.
- **Privacy by design** — All data stays on disk. Embedding runs locally via [sentence-transformers](https://sbert.net/) or Ollama.
- **Hot/cold tiering** — Frequently accessed memories stay "hot" (fast, capacity-limited); older entries archive to "cold" storage with full vector indexing.
- **Hybrid retrieval** — Combines FTS5 full-text search with vector similarity via Reciprocal Rank Fusion (RRF).
- **Semantic deduplication** — Prevents storing near-duplicate entries using both exact hash and vector distance checks.
- **Agent integration** — Hook/plugin system for seamless, zero-config memory sync with AI agents. Also exposes a REST API for direct access.
- **Auditable** — Full CRUD operations on stored memories. Timeline browsing. Operation logs. Users can inspect, edit, and delete any memory.

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
│   post_llm   │            │  + Bearer    │
│   post_tool) │            │    Auth      │
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
| `GET` | `/health` | Health check |

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
  provider: local              # "local" (sentence-transformers) or "ollama"
  model: nomic-embed-text-v1.5
  dimensions: 768

  ollama:
    base_url: http://localhost:11434

server:
  host: 0.0.0.0
  port: 8200
```

Every config value can be overridden via environment variables:

```bash
HIPPO_EMBEDDING_PROVIDER=ollama  # Switch to Ollama backend
HIPPO_DB_PATH=/data/memory.db    # Custom database path
HIPPO_SERVER_PORT=9000           # Custom port
```

## Embedding Backends

| Backend | Install | GPU Required | Model Size | Notes |
|---------|---------|-------------|------------|-------|
| **sentence-transformers** (default) | `pip install -e ".[local]"` | No (CPU OK) | ~80 MB | Zero external dependencies |
| **Ollama** | [ollama.com](https://ollama.com) | No | ~270 MB | Shared with other Ollama models |

Both backends use `nomic-embed-text-v1.5` (768 dimensions) by default for consistent vector quality.

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

# Configure endpoint (local or remote)
export HIPPO_BASE_URL=http://localhost:8200   # or remote server
export HIPPO_TOKEN=your-secret-token          # if auth enabled

# Restart your agent — done. Memory sync is fully automatic.
```

**Offline resilience:** When OpenHippo is unreachable, writes are cached to a local WAL (Write-Ahead Log) and replayed automatically on reconnection.

## Development

```bash
# Install dev dependencies
pip install -e ".[local,dev]"

# Run tests
pytest -v

# Lint
ruff check src/

# Type check
mypy src/
```

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
- [x] Bearer token authentication
- [x] Docker image and compose deployment
- [x] Remote agent connection (multi-VM support)
- [ ] Multi-tenant support
- [ ] Web UI for memory inspection
- [ ] Scheduled memory consolidation (auto-summarize)
- [ ] Webhook / event-driven memory triggers

## License

[MIT](LICENSE)

---

<p align="center">
  <sub>Built with 🧠 by <a href="https://github.com/wpsl5168">Pei Wang</a></sub>
</p>
