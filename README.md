<div align="center">

# 🦛 OpenHippo

**Local-first memory engine for AI Agents**

[Documentation](https://github.com/wpsl5168/OpenHippo/wiki) · [PRD](docs/PRD.md) · [Contributing](CONTRIBUTING.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

</div>

---

## What is OpenHippo?

OpenHippo gives AI Agents **persistent memory** — local-first, privacy-first, zero cloud dependency.

Like the hippocampus in the human brain that consolidates short-term memory into long-term memory, OpenHippo lets your agents **remember what matters** across sessions.

### Key Features

- 🔒 **Local-first** — SQLite single-file storage, your data never leaves your machine
- ⚡ **Plug & play** — MCP / REST / CLI, integrate in 5 minutes
- 🧠 **Brain-like** — Hot/cold memory tiers, auto-forgetting, sleep consolidation (Dream)
- 🏗️ **Multi-agent** — GitHub-style memory repos with tenant→agent→session isolation
- 📦 **Zero bloat** — No Neo4j, no vector DB, no external APIs required

### Quick Start

```bash
pip install openhippo

# Start the server
openhippo serve

# Or use as MCP tool
openhippo mcp
```

```python
from openhippo import HippoClient

hippo = HippoClient()
hippo.add("user", "Prefers dark mode and vim keybindings")
results = hippo.search("editor preferences")
```

### Architecture

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│   MCP Tool   │  │  REST API   │  │     CLI     │
└──────┬───────┘  └──────┬──────┘  └──────┬──────┘
       │                 │                 │
       └────────────┬────┘────────────────┘
                    │
            ┌───────▼────────┐
            │   Core Engine   │
            │  ┌───────────┐  │
            │  │ Hot Memory │  │  ← Fast access, injected every turn
            │  ├───────────┤  │
            │  │Cold Memory │  │  ← FTS5 searchable archive
            │  ├───────────┤  │
            │  │  Dream 🌙  │  │  ← Background consolidation
            │  └───────────┘  │
            └───────┬────────┘
                    │
            ┌───────▼────────┐
            │  SQLite + FTS5  │
            └────────────────┘
```

## Project Status

🚧 **Early development** — see [Progress Report](docs/PROGRESS.md) for details.

**Completed (10/26 PRD features):**
- ✅ Core CRUD (write/search/delete/replace) with semantic dedup
- ✅ Hot/cold memory tiers with auto-eviction
- ✅ FTS5 + sqlite-vec hybrid search (RRF fusion)
- ✅ REST API (17 endpoints) + MCP + CLI
- ✅ Embedding abstraction (Ollama / SentenceTransformer — no external API needed)
- ✅ Unified YAML + env config system
- ✅ 55 tests passing

**Next up:** Bearer Token auth → Docker packaging → Multi-VM deployment

See the [PRD](docs/PRD.md) for the full roadmap.

## License

MIT © 2026 OpenHippo Contributors
