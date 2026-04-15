"""Centralized writ.toml loader using tomllib (Python 3.11+).

Returns typed config dict. All modules read config through this, not hardcoded values.

Per ARCH-CONST-001: all tunables must live in writ.toml with named constant defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

# Per ARCH-CONST-001: named constants for defaults.
DEFAULT_NEO4J_URI = "bolt://localhost:7687"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_NEO4J_PASSWORD = "writdevpass"
DEFAULT_HNSW_CACHE_DIR = str(Path.home() / ".cache" / "writ" / "hnsw")

# Default config file path: writ.toml in the package root (one level above writ/).
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = str(_PACKAGE_ROOT / "writ.toml")


def load_config(path: str | None = None) -> dict[str, Any]:
    """Load and return the parsed writ.toml as a dict.

    Returns an empty dict when the file does not exist or is empty.
    """
    config_path = path if path is not None else _DEFAULT_CONFIG_PATH
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        return data if data else {}
    except Exception:
        return {}


def get_neo4j_uri(path: str | None = None) -> str:
    """Return neo4j.uri from config, falling back to DEFAULT_NEO4J_URI."""
    cfg = load_config(path)
    return cfg.get("neo4j", {}).get("uri", DEFAULT_NEO4J_URI)


def get_neo4j_user(path: str | None = None) -> str:
    """Return neo4j.user from config, falling back to DEFAULT_NEO4J_USER."""
    cfg = load_config(path)
    return cfg.get("neo4j", {}).get("user", DEFAULT_NEO4J_USER)


def get_neo4j_password(path: str | None = None) -> str:
    """Return neo4j.password from config, falling back to DEFAULT_NEO4J_PASSWORD."""
    cfg = load_config(path)
    return cfg.get("neo4j", {}).get("password", DEFAULT_NEO4J_PASSWORD)


def get_hnsw_cache_dir(path: str | None = None) -> str:
    """Return hnsw.cache_dir from config, falling back to DEFAULT_HNSW_CACHE_DIR."""
    cfg = load_config(path)
    return cfg.get("hnsw", {}).get("cache_dir", DEFAULT_HNSW_CACHE_DIR)
