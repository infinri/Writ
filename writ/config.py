"""Centralized writ.toml loader using tomllib (Python 3.11+).

Returns typed config dict. All modules read config through this, not hardcoded values.

Per ARCH-CONST-001: all tunables must live in writ.toml with named constant defaults.
"""

from __future__ import annotations

import math
import os
import sys
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
# Hosts the Bash egress guard (hooks/scripts/writ-bash-write-gate.sh) never prompts
# about. Loopback in every spelling a command line can carry, including the bracketed
# IPv6 form that appears inside a URL (`http://[::1]:9/x`) and the bare form a raw
# socket verb takes (`nc ::1 9000`).
DEFAULT_EGRESS_ALLOW_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")
DEFAULT_WRIT_HOST = "localhost"
# Ships OFF. A 2026-08-06 sweep of the 193-query gold set found the preference
# changes nothing on the default retrieval path (the only ai-provisional node in
# the corpus is not semantic-routed), so enabling it by default would be a
# ranking change with no measured gain. See benchmarks/RANKING-LEVERS-2026-08-06.md.
DEFAULT_AUTHORITY_PREFERENCE_THRESHOLD = 0.0

# Default config file path: writ.toml in the package root (one level above writ/).
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = str(_PACKAGE_ROOT / "writ.toml")


def _warn_config_ignored(config_path: str, reason: str) -> None:
    """Emit the single stderr warning shared by load_config's two failure branches.

    A malformed/unparseable writ.toml and an unreadable one both fall back to
    built-in defaults; both must say so visibly (never silently swallow). DRY: one
    helper owns the "writ: warning: config file <path> <reason>; falling back to
    built-in defaults" boilerplate so the two except branches cannot drift.
    """
    print(
        f"writ: warning: config file {config_path} {reason}; "
        f"falling back to built-in defaults",
        file=sys.stderr,
    )


# Paths already reported by _emit_config_resolution, so the event fires ONCE per process
# per path. Every getter (get_neo4j_uri, get_neo4j_password, ...) calls load_config on each
# access, so emitting per call would put dozens of identical rows in every process.
_REPORTED_CONFIG_PATHS: set[str] = set()


def _emit_config_resolution(config_path: str, outcome: str, sections: list[str]) -> None:
    """Record which writ.toml was resolved and what it contributed. Never raises.

    Audit item F: config resolution was undiagnosable after the fact. A missing file was
    entirely silent, so "running on built-in defaults" (including DEFAULT_NEO4J_PASSWORD)
    looked identical to "loaded a config with these values" -- and writ.toml is gitignored,
    so a fresh install genuinely has none.

    Records section and key NAMES only, never values. This file holds neo4j.password and
    bitbucket.token; logging values would move credentials into a 365-day retained stream
    (SEC-DATA-MASK-001). Names are what you need to answer "did my setting get picked up".
    """
    try:
        from writ.shared.logging import emit

        emit(
            "metrics", "config_resolved", "", None,
            path=config_path, outcome=outcome, sections=sections,
        )
    except Exception:  # noqa: BLE001 - config loading must not depend on logging
        pass


def _section_keys(data: dict[str, Any]) -> list[str]:
    """`["neo4j.uri", "neo4j.password", ...]` -- key paths, never values."""
    out: list[str] = []
    for section, body in sorted(data.items()):
        if isinstance(body, dict):
            out.extend(f"{section}.{k}" for k in sorted(body))
        else:
            out.append(section)
    return out


def load_config(path: str | None = None) -> dict[str, Any]:
    """Load and return the parsed writ.toml as a dict.

    Returns an empty dict when the file does not exist or is empty.
    """
    config_path = path if path is not None else _DEFAULT_CONFIG_PATH
    first_time = config_path not in _REPORTED_CONFIG_PATHS
    if first_time:
        _REPORTED_CONFIG_PATHS.add(config_path)
    if not os.path.isfile(config_path):
        if first_time:
            _emit_config_resolution(config_path, "absent-using-defaults", [])
        return {}
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        if first_time:
            _emit_config_resolution(
                config_path, "loaded" if data else "empty-using-defaults",
                _section_keys(data) if data else [],
            )
        return data if data else {}
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        _warn_config_ignored(
            config_path, f"is malformed (unparseable) and was ignored ({e})"
        )
        if first_time:
            # errors stream, not just stderr: a hook's stderr is a swallowed sink and the
            # daemon's goes to journald, so "your config was ignored" reached nobody.
            _emit_config_exception("config.load.malformed", e, config_path)
            _emit_config_resolution(config_path, "malformed-using-defaults", [])
        return {}
    except OSError as e:
        _warn_config_ignored(config_path, f"could not be read ({e})")
        if first_time:
            _emit_config_exception("config.load.unreadable", e, config_path)
            _emit_config_resolution(config_path, "unreadable-using-defaults", [])
        return {}


def _emit_config_exception(component: str, exc: BaseException, config_path: str) -> None:
    """Route a config-load failure to the errors stream. Never raises."""
    try:
        from writ.shared.logging import emit_exception

        emit_exception(component, exc, "", None, config_path=config_path)
    except Exception:  # noqa: BLE001
        pass


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


def get_bitbucket_email(path: str | None = None) -> str | None:
    """Return the Bitbucket account email from writ.toml [bitbucket].email.

    writ.toml is gitignored (see writ.toml.example) so the credential never lands
    in a tracked file. Mirrors get_neo4j_password (also toml-sourced, no env).
    Returns None when the section or value is absent.
    """
    cfg = load_config(path)
    return cfg.get("bitbucket", {}).get("email") or None


def get_bitbucket_token(path: str | None = None) -> str | None:
    """Return the Bitbucket API token from writ.toml [bitbucket].token.

    writ.toml is gitignored (see writ.toml.example) so the token never lands in a
    tracked file. The token is never logged. Returns None when the section or
    value is absent.
    """
    cfg = load_config(path)
    return cfg.get("bitbucket", {}).get("token") or None


def get_hnsw_cache_dir(path: str | None = None) -> str:
    """Return hnsw.cache_dir from config, falling back to DEFAULT_HNSW_CACHE_DIR.

    TOML strings like "~/.cache/writ/hnsw" are expanded to an absolute path.
    Without this, Path() treats "~" as a literal dir name and creates a
    stray "~" folder wherever the process runs.
    """
    cfg = load_config(path)
    raw = cfg.get("hnsw", {}).get("cache_dir", DEFAULT_HNSW_CACHE_DIR)
    return os.path.expanduser(raw)


def get_egress_allow_hosts(path: str | None = None) -> list[str]:
    """Hosts the Bash egress guard may send local data to without prompting.

    The union of three sources, lowercased and de-duplicated:
      * DEFAULT_EGRESS_ALLOW_HOSTS (loopback, always allowed),
      * writ.toml `[egress] allow_hosts` (a list of strings),
      * the comma-separated `WRIT_EGRESS_ALLOW_HOSTS` env var and the daemon host
        from `WRIT_HOST` (the daemon answers on the loopback name by default, but a
        remote daemon must not make its own gate prompt).

    `WRIT_CONFIG_PATH` overrides the config file when no explicit `path` is passed:
    the guard runs inside a hook subprocess that resolves this list in-process, and
    the real writ.toml is gitignored install state a test must never write to. The
    env seam mirrors WRIT_STRICT / WRIT_PORT, and is scoped to this getter so the
    other readers keep their single fixed location.

    Never raises: an absent, empty or malformed writ.toml falls back to the
    built-in defaults (load_config already warns), so a bad config NARROWS the
    allowlist rather than opening the gate.
    """
    if path is None:
        path = os.environ.get("WRIT_CONFIG_PATH") or None
    cfg = load_config(path)
    hosts = list(DEFAULT_EGRESS_ALLOW_HOSTS)
    configured = cfg.get("egress", {}).get("allow_hosts") or []
    if isinstance(configured, str):
        configured = configured.split(",")
    hosts.extend(str(h) for h in configured)
    hosts.extend((os.environ.get("WRIT_EGRESS_ALLOW_HOSTS") or "").split(","))
    hosts.append(os.environ.get("WRIT_HOST") or DEFAULT_WRIT_HOST)
    return sorted({h.strip().lower() for h in hosts if h and h.strip()})


def get_authority_preference_threshold(path: str | None = None) -> float:
    """Return [retrieval] authority_preference_threshold, defaulting to OFF (0.0).

    The threshold drives `apply_authority_preference`: within this score gap, a
    human / ai-promoted rule outranks an ai-provisional one. 0.0 disables the pass.

    Never raises. A non-numeric, negative, or non-finite value falls back to the
    default: this is an optional tuning key read during daemon startup, and a typo
    in it must not stop the server from coming up. A bad value therefore leaves
    ranking at its shipped behavior rather than at an arbitrary one.

    The non-finite check is not defensive padding. TOML has literal `nan` / `inf`,
    and an overflowing exponent (`1e400`) parses to `inf`. Both are floats and
    neither is negative, so without this guard they reach
    `apply_authority_preference`, where `threshold <= 0.0` is False and every
    `gap > threshold` comparison is False: the pass would then swap EVERY adjacent
    authority-mismatched pair regardless of score distance, which is the opposite
    of a safe fallback.
    """
    cfg = load_config(path)
    raw = cfg.get("retrieval", {}).get(
        "authority_preference_threshold", DEFAULT_AUTHORITY_PREFERENCE_THRESHOLD
    )
    # bool is an int subclass; `True` must not silently become 1.0.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_AUTHORITY_PREFERENCE_THRESHOLD
    value = float(raw)
    if not math.isfinite(value) or value < 0.0:
        return DEFAULT_AUTHORITY_PREFERENCE_THRESHOLD
    return value


def get_logs_backup_dest(path: str | None = None) -> str | None:
    """Return the logs backup destination from writ.toml [logs].backup_dest.

    A leading ~ is expanded (os.path.expanduser), mirroring get_hnsw_cache_dir,
    so a configured "~/writ-backups" resolves to an absolute path rather than a
    stray "~" directory. Returns None when the section or value is absent
    (mirrors get_bitbucket_email); there is no meaningful default destination,
    so `writ logs backup` requires an explicit --dest or a configured value.
    """
    cfg = load_config(path)
    raw = cfg.get("logs", {}).get("backup_dest")
    if not raw:
        return None
    return os.path.expanduser(raw)
