"""Unified configuration — YAML file + environment variable overrides."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_DATA_DIR = Path.home() / ".hippocampus"
DEFAULT_CONFIG_PATH = DEFAULT_DATA_DIR / "config.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# Default configuration
DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8200,
    },
    "storage": {
        "db_path": str(DEFAULT_DATA_DIR / "memory.db"),
    },
    "embedding": {
        "provider": "auto",  # auto | ollama | local | openai
        "ollama": {
            "base_url": "http://127.0.0.1:11434",
            "model": "nomic-embed-text",
        },
        "local": {
            "model": "nomic-ai/nomic-embed-text-v1.5",
            "device": "cpu",
        },
        "openai": {
            "model": "text-embedding-3-small",
            "api_key": "",
        },
    },
    "logging": {
        "level": "INFO",
    },
}

# Environment variable mapping: ENV_VAR → config dotpath
ENV_MAP = {
    "HIPPO_HOST": "server.host",
    "HIPPO_PORT": "server.port",
    "HIPPO_DB_PATH": "storage.db_path",
    "HIPPO_EMBEDDING_PROVIDER": "embedding.provider",
    "HIPPO_OLLAMA_URL": "embedding.ollama.base_url",
    "HIPPO_OLLAMA_MODEL": "embedding.ollama.model",
    "HIPPO_LOCAL_MODEL": "embedding.local.model",
    "HIPPO_LOCAL_DEVICE": "embedding.local.device",
    "HIPPO_OPENAI_API_KEY": "embedding.openai.api_key",
    "HIPPO_OPENAI_MODEL": "embedding.openai.model",
    "HIPPO_LOG_LEVEL": "logging.level",
}


def _set_nested(d: dict, dotpath: str, value: Any) -> None:
    """Set a value in a nested dict using dot notation."""
    keys = dotpath.split(".")
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    # Type coercion
    final_key = keys[-1]
    if final_key == "port":
        value = int(value)
    d[final_key] = value


def _get_nested(d: dict, dotpath: str, default: Any = None) -> Any:
    """Get a value from nested dict using dot notation."""
    keys = dotpath.split(".")
    for key in keys:
        if isinstance(d, dict) and key in d:
            d = d[key]
        else:
            return default
    return d


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load configuration: defaults → YAML file → environment variables.
    
    Priority (highest wins): env vars > config file > defaults.
    """
    config = DEFAULTS.copy()

    # Load YAML if exists
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if path.exists():
        try:
            import yaml
            with open(path) as f:
                file_config = yaml.safe_load(f) or {}
            config = _deep_merge(config, file_config)
            logger.info("Loaded config from %s", path)
        except ImportError:
            # PyYAML not installed — try simple key-value parsing
            logger.warning("PyYAML not installed, skipping config file")
        except Exception as e:
            logger.warning("Failed to load config from %s: %s", path, e)
    else:
        logger.debug("No config file at %s, using defaults", path)

    # Apply environment variable overrides
    for env_var, dotpath in ENV_MAP.items():
        value = os.environ.get(env_var)
        if value is not None:
            _set_nested(config, dotpath, value)
            logger.debug("Config override: %s=%s (from %s)", dotpath, value, env_var)

    return config


def get(config: dict, dotpath: str, default: Any = None) -> Any:
    """Convenience: get a nested config value."""
    return _get_nested(config, dotpath, default)


# Singleton
_config: dict[str, Any] | None = None


def get_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Get or load the singleton config."""
    global _config
    if _config is None:
        _config = load_config(config_path)
    return _config


def reset_config() -> None:
    """Reset singleton (for testing)."""
    global _config
    _config = None
