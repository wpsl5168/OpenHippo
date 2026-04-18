# Contributing to OpenHippo 🦛

## Development Setup

```bash
git clone https://github.com/wpsl5168/OpenHippo.git
cd OpenHippo
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Tests & Lint

```bash
pytest
ruff check .
ruff format .
```

## PR Flow

1. Fork → feature branch → commit → PR
2. All PRs need passing tests

MIT Licensed.
