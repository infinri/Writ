"""`writ doctor [--fix]` operability self-diagnostic.

Runs a battery of 10 read-only checks against the live Writ install (daemon,
Neo4j, embedding stack, corpus parity, Bitbucket creds presence, git
post-commit hook, PATH symlink, CC hook registration, mode/gate sanity) and
returns a list of CheckResult records. The CLI (writ/cli.py) renders the table,
applies --fix, and sets the exit code.

Design constraints:
- This module is importable WITHOUT a live daemon/Neo4j: nothing connects at
  import time. All I/O happens inside the seam functions (the `_`-prefixed
  callables) at call time, so the unit tests can monkeypatch each seam.
- Every subprocess probe is invoked with an argument list, never shell=True
  (SEC-INJ-CMD-001).
- The bitbucket check uses config-getter truthiness ONLY; it never reads
  writ.toml itself and never returns/logs/prints credential values.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------------------- #
# Public contract
# --------------------------------------------------------------------------- #

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

_HEALTH_URL = "http://localhost:8765/health"
_DAEMON_UNIT = "writ-server.service"
_DAEMON_PORT = 8765
_SYSTEMD_SERVICE = "writ-server"

# The package root is two levels above this file (.../writ/session/doctor.py).
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
_VENV_PYTHON = _PACKAGE_ROOT / ".venv" / "bin" / "python"
_ONNX_DIR = Path.home() / ".cache" / "writ" / "models" / "onnx"
_BIBLE_DIR = _PACKAGE_ROOT / "bible"


@dataclass(frozen=True)
class CheckResult:
    """One operability check outcome.

    `fix` is a zero-arg side-effecting callable (or None). It is invoked only
    under --fix, only when status != ok and fixable is True.
    """

    name: str
    status: str
    detail: str
    fixable: bool
    fix: Callable[[], None] | None


def _ok(name: str, detail: str, *, fixable: bool = False,
        fix: Callable[[], None] | None = None) -> CheckResult:
    return CheckResult(name=name, status=STATUS_OK, detail=detail, fixable=fixable, fix=fix)


def _warn(name: str, detail: str, *, fixable: bool = False,
          fix: Callable[[], None] | None = None) -> CheckResult:
    return CheckResult(name=name, status=STATUS_WARN, detail=detail, fixable=fixable, fix=fix)


def _fail(name: str, detail: str, *, fixable: bool = False,
          fix: Callable[[], None] | None = None) -> CheckResult:
    return CheckResult(name=name, status=STATUS_FAIL, detail=detail, fixable=fixable, fix=fix)


@dataclass
class DoctorOptions:
    """Run options for the doctor checks."""

    net: bool = False
    session_id: str | None = None
    repo: str = "."


# --------------------------------------------------------------------------- #
# Seams (module-level callables; tests monkeypatch these by name)
# --------------------------------------------------------------------------- #


def _http_get_health() -> dict | None:
    """GET the daemon /health endpoint; parsed JSON dict or None on any error."""
    try:
        with urllib.request.urlopen(_HEALTH_URL, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _systemctl_is_active(unit: str) -> str | None:
    """Run `systemctl --user is-active <unit>`; stdout.strip(), or None if absent."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    return proc.stdout.strip()


def _port_owner_pids(port: int) -> list[int]:
    """PIDs holding the given TCP port (lsof -ti, fallback ss); [] if none/absent."""
    try:
        proc = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
        )
        pids = [int(line) for line in proc.stdout.split() if line.strip().isdigit()]
        if pids:
            return pids
    except FileNotFoundError:
        pass
    # Fallback: ss -ltnp, parse the pid=NNN field on rows mentioning the port.
    try:
        proc = subprocess.run(
            ["ss", "-ltnp"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    pids = []
    for line in proc.stdout.splitlines():
        if f":{port} " not in line and not line.rstrip().endswith(f":{port}"):
            if f":{port}" not in line:
                continue
        marker = "pid="
        idx = line.find(marker)
        while idx != -1:
            start = idx + len(marker)
            end = start
            while end < len(line) and line[end].isdigit():
                end += 1
            if end > start:
                pids.append(int(line[start:end]))
            idx = line.find(marker, end)
    return pids


def _ps_writ_serve_orphans() -> list[dict]:
    """Scan ps output for `writ serve` rows with ppid==1; [] if ps absent."""
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,ppid,cmd"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    orphans: list[dict] = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, ppid_s, cmd = parts
        if not pid_s.isdigit() or not ppid_s.isdigit():
            continue
        if "writ serve" in cmd and int(ppid_s) == 1:
            orphans.append({"pid": int(pid_s), "ppid": int(ppid_s)})
    return orphans


def _tcp_can_connect(host: str, port: int) -> bool:
    """True if a TCP connection to (host, port) succeeds within the timeout."""
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _count_neo4j_rules() -> int:
    """Return the graph Rule count via Neo4jConnection.count_rules() (async bridged)."""
    from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
    from writ.graph.db import Neo4jConnection

    async def _run() -> int:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            return await db.count_rules()
        finally:
            await db.close()

    return asyncio.run(_run())


def _list_neo4j_constraint_names() -> list[str]:
    """Return every constraint name currently applied (async bridged)."""
    from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
    from writ.graph.db import Neo4jConnection

    async def _run() -> list[str]:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            return [c["name"] for c in await db.list_constraints()]
        finally:
            await db.close()

    return asyncio.run(_run())


def _apply_neo4j_constraints() -> None:
    """Fix callable: create every missing uniqueness constraint/index (idempotent)."""
    from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
    from writ.graph.db import Neo4jConnection

    async def _run() -> None:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            await db.apply_constraints()
        finally:
            await db.close()

    asyncio.run(_run())


def _venv_import_ok() -> bool:
    """True iff the .venv interpreter imports onnxruntime + tokenizers cleanly."""
    try:
        proc = subprocess.run(
            [
                str(_VENV_PYTHON),
                "-c",
                "import onnxruntime; from tokenizers import Tokenizer",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return proc.returncode == 0


def _onnx_model_files_present() -> tuple[bool, bool]:
    """(model.onnx exists, tokenizer.json exists) under the ONNX model dir."""
    return (
        (_ONNX_DIR / "model.onnx").exists(),
        (_ONNX_DIR / "tokenizer.json").exists(),
    )


def _detect_parity_violations() -> list[dict]:
    """Parity violations of bible/ against the graph; [] on any error (fail-open)."""
    from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
    from writ.graph.db import Neo4jConnection
    from writ.graph.integrity import IntegrityChecker

    async def _run() -> list[dict]:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            checker = IntegrityChecker(db._driver)
            return await checker.detect_parity_violations(_BIBLE_DIR)
        finally:
            await db.close()

    try:
        return asyncio.run(_run())
    except Exception:
        return []


def _run_reconcile() -> None:
    """Reconcile the graph against bible/ (side-effecting; only called under --fix)."""
    from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
    from writ.graph.db import Neo4jConnection
    from writ.graph.methodology_ingest import reconcile as _reconcile

    async def _run() -> None:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            await _reconcile(_BIBLE_DIR, db, project="writ")
        finally:
            await db.close()

    asyncio.run(_run())


def _bitbucket_creds_present() -> tuple[bool, bool]:
    """(email_present, token_present) from config-getter truthiness ONLY.

    Never returns, logs, or prints the values themselves.
    """
    from writ import config

    return (bool(config.get_bitbucket_email()), bool(config.get_bitbucket_token()))


def _bitbucket_live_auth(repo: str) -> int | None:
    """Authenticated GET to the repo's Bitbucket endpoint; returns the HTTP status.

    Validates the minimum scope writ actually uses (read:repository) by pinging
    GET /2.0/repositories/{workspace}/{repo_slug}, NOT /2.0/user (which requires
    the broader account scope writ never requests). The workspace/slug are derived
    the same way `writ pr sync` derives them, reusing derive_project_identity and
    parse_bitbucket_remote (no re-implementation of remote parsing).

    Returns the HTTP status code on success or HTTPError, or None (the no-remote
    sentinel) when the repo has no Bitbucket remote or cannot be resolved. The
    token is never printed, logged, or returned. Called ONLY when opts.net is True.
    """
    import base64

    from writ import config

    try:
        from writ.session.git_identity import derive_project_identity
        from writ.session.remote_parse import parse_bitbucket_remote

        _repo_root, remote_url, _ = derive_project_identity(repo)
        parsed = parse_bitbucket_remote(remote_url)
    except Exception:
        return None

    if parsed is None:
        return None
    workspace, repo_slug = parsed

    email = config.get_bitbucket_email() or ""
    token = config.get_bitbucket_token() or ""
    cred = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {cred}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.getcode()
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


def _git_hook_installed(repo: str) -> bool:
    """True iff the Writ post-commit marker is present in repo."""
    from writ.session import git_hooks

    return git_hooks.git_hooks_installed(repo)


def _install_git_hook(repo: str) -> None:
    """Install the Writ post-commit hook into repo (idempotent, side-effecting)."""
    from writ.session import git_hooks

    git_hooks.install_git_hooks(repo)


def _path_symlink_ok() -> tuple[bool, bool]:
    """(which_resolves, readlink_ends_at_skill_bin) resolved in-process (no subprocess)."""
    resolved = shutil.which("writ")
    if not resolved:
        return (False, False)
    target = os.path.realpath(resolved)
    expected = os.path.realpath(str(_PACKAGE_ROOT / "bin" / "writ"))
    return (True, target == expected)


def _recreate_symlink() -> None:
    """Recreate ~/.local/bin/writ -> <skill>/bin/writ (side-effecting).

    Warns to stderr when ~/.local/bin is not on PATH: the recreated link is
    unreachable until the directory is added, so a silent recreate would
    leave `writ` still unresolved.
    """
    import sys

    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    link = local_bin / "writ"
    target = _PACKAGE_ROOT / "bin" / "writ"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    if str(local_bin) not in os.environ.get("PATH", "").split(os.pathsep):
        print(
            f"warning: {local_bin} is not on PATH; add it so `writ` resolves.",
            file=sys.stderr,
        )


def _cc_registration_ok() -> tuple[bool, list[str]]:
    """(all_ok, [missing_or_non_exec_paths]) for every .sh referenced by hooks.json."""
    plugin_json = _PACKAGE_ROOT / ".claude-plugin" / "plugin.json"
    missing: list[str] = []
    try:
        with open(plugin_json) as f:
            plugin = json.load(f)
    except (OSError, ValueError):
        return (False, [str(plugin_json)])

    hooks_ref = plugin.get("hooks", "")
    hooks_path = (_PACKAGE_ROOT / str(hooks_ref).lstrip("./")).resolve()
    try:
        with open(hooks_path) as f:
            hooks_doc = json.load(f)
    except (OSError, ValueError):
        return (False, [str(hooks_path)])

    referenced = _collect_hook_scripts(hooks_doc)
    for ref in referenced:
        path = _resolve_hook_script(ref)
        if path is None:
            continue
        if not path.exists():
            missing.append(f"{ref} (missing)")
        elif not os.access(path, os.X_OK):
            missing.append(f"{ref} (not executable)")
    return (not missing, missing)


def _collect_hook_scripts(hooks_doc: object) -> list[str]:
    """Walk the hooks.json structure and collect every `command` string referencing a .sh."""
    found: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "command" and isinstance(value, str) and ".sh" in value:
                    found.append(value)
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(hooks_doc)
    return found


def _resolve_hook_script(command: str) -> Path | None:
    """Extract the .sh path from a hook command string and resolve it under the package root."""
    token = None
    for part in command.split():
        if ".sh" in part:
            token = part
            break
    if token is None:
        return None
    token = token.replace("${CLAUDE_PLUGIN_ROOT}", str(_PACKAGE_ROOT))
    return Path(token)


def _latest_session_cache(session_id: str | None) -> dict | None:
    """Read the named session cache, or the most-recent by mtime; None if none exist."""
    import glob

    from writ.session import cache as cache_mod

    cache_dir = cache_mod._cache_dir()
    if session_id:
        path = os.path.join(cache_dir, f"writ-session-{session_id}.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    # Falsy session_id: delegate to the canonical resolver (pointer/env first) so the
    # doctor reads the SAME session the live hooks do, not a bare mtime max that a
    # rotated cache could win. The mtime-glob stays only as the resolver's own last
    # resort; if it too returns None, fall back to it here for an offline doctor.
    resolved = cache_mod.resolve_current_session_id()
    if resolved:
        path = os.path.join(cache_dir, f"writ-session-{resolved}.json")
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (OSError, ValueError):
                return None
        # resolved id has no cache in this dir -> fall through to the mtime last resort.

    candidates = glob.glob(os.path.join(cache_dir, "writ-session-*.json"))
    if not candidates:
        return None
    latest = max(candidates, key=os.path.getmtime)
    try:
        with open(latest) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _restart_daemon() -> None:
    """Restart the systemd user service (side-effecting)."""
    subprocess.run(["systemctl", "--user", "restart", _SYSTEMD_SERVICE])


def _kill_port_owner(port: int) -> None:
    """Kill every pid holding the port, then restart the daemon (side-effecting)."""
    for pid in _port_owner_pids(port):
        try:
            os.kill(pid, 15)
        except (ProcessLookupError, PermissionError):
            continue
    _restart_daemon()


# --------------------------------------------------------------------------- #
# Checks (each a pure function check_<name>(opts) -> CheckResult)
# --------------------------------------------------------------------------- #


def check_daemon_liveness(opts: DoctorOptions) -> CheckResult:
    name = "daemon-liveness"
    health = _http_get_health()
    active = _systemctl_is_active(_DAEMON_UNIT)
    sysd_note = "" if active is not None else " (systemctl unavailable; HTTP-only)"

    if health is None:
        return _fail(
            name=name,
            detail=(
                "Daemon /health unreachable; restart with "
                "`systemctl --user restart writ-server`." + sysd_note
            ),
            fixable=True,
            fix=_restart_daemon,
        )

    status = health.get("status")
    index_state = health.get("index_state")
    rule_count = health.get("rule_count", 0)

    if status == "healthy" and index_state == "warm" and rule_count > 0:
        return _ok(
            name=name,
            detail=f"Daemon healthy; index warm; {rule_count} rule(s)." + sysd_note,
        )

    if status == "healthy":
        return _fail(
            name=name,
            detail=(
                f"Daemon up but index_state={index_state!r} rule_count={rule_count}; "
                "restart with `systemctl --user restart writ-server`." + sysd_note
            ),
            fixable=True,
            fix=_restart_daemon,
        )

    if status == "degraded":
        return _warn(
            name=name,
            detail=(
                "Daemon degraded (DB/index split); "
                "consider `systemctl --user restart writ-server`." + sysd_note
            ),
            fixable=True,
            fix=_restart_daemon,
        )

    return _fail(
        name=name,
        detail=(
            f"Daemon status={status!r}; restart with "
            "`systemctl --user restart writ-server`." + sysd_note
        ),
        fixable=True,
        fix=_restart_daemon,
    )


def check_stale_orphan_port_conflict(opts: DoctorOptions) -> CheckResult:
    name = "stale-orphan-port-conflict"
    orphans = _ps_writ_serve_orphans()
    active = _systemctl_is_active(_DAEMON_UNIT)
    # Only a PPID-1 orphan that actually holds the daemon port signals the
    # crash loop; a stray `writ serve` on another port is not this conflict.
    port_pids = set(_port_owner_pids(_DAEMON_PORT))
    port_holding_orphans = [o for o in orphans if o.get("pid") in port_pids]

    if port_holding_orphans and active in {"activating", "failed"}:
        return _fail(
            name=name,
            detail=(
                f"systemd is {active} while a PPID-1 `writ serve` orphan holds "
                f":{_DAEMON_PORT} (crash loop). Kill the orphan and restart."
            ),
            fixable=True,
            fix=lambda: _kill_port_owner(_DAEMON_PORT),
        )

    if active == "active" and not port_holding_orphans:
        return _ok(
            name=name,
            detail=f"systemd service active; no PPID-1 orphan on :{_DAEMON_PORT}.",
        )

    return _warn(
        name=name,
        detail=(
            "Could not conclusively assess the port (systemctl/lsof/ss/ps absent "
            "or inconclusive)."
        ),
    )


def check_neo4j_connectivity(opts: DoctorOptions) -> CheckResult:
    name = "neo4j-connectivity"
    from writ.config import get_neo4j_uri

    uri = get_neo4j_uri()
    host, port = _parse_bolt_uri(uri)

    if not _tcp_can_connect(host, port):
        return _fail(
            name=name,
            detail=(
                f"No TCP connection to Neo4j at {host}:{port}; start it with "
                "`docker compose up -d neo4j` and wait for bolt."
            ),
        )

    try:
        count = _count_neo4j_rules()
    except Exception as exc:
        return _fail(
            name=name,
            detail=(
                f"Bolt port open but count_rules() failed ({exc}); check Neo4j is "
                "up via `docker compose ps`."
            ),
        )

    return _ok(
        name=name,
        detail=f"Neo4j reachable at {host}:{port}; {count} rule(s).",
    )


_MIN_EXPECTED_CONSTRAINTS = 17


def check_uniqueness_constraints(opts: DoctorOptions) -> CheckResult:
    """Detects the missing-constraint state that lets MERGE silently create
    duplicate nodes for anything keyed on (id, project) -- found live: 287
    legacy Rule nodes with no `project` property meant every re-import
    created fresh duplicates instead of updating in place."""
    name = "uniqueness-constraints"

    try:
        names = _list_neo4j_constraint_names()
    except Exception as exc:
        return _fail(
            name=name,
            detail=f"Could not list Neo4j constraints ({exc}).",
            fixable=True,
            fix=_apply_neo4j_constraints,
        )

    if len(names) < _MIN_EXPECTED_CONSTRAINTS:
        return _fail(
            name=name,
            detail=(
                f"Only {len(names)} constraint(s) applied (expected >= "
                f"{_MIN_EXPECTED_CONSTRAINTS}); MERGE on unconstrained keys can "
                "silently create duplicate nodes. Fix with `apply_constraints()`."
            ),
            fixable=True,
            fix=_apply_neo4j_constraints,
        )

    return _ok(
        name=name,
        detail=f"{len(names)} constraint(s) applied.",
    )


def check_embedding_stack(opts: DoctorOptions) -> CheckResult:
    name = "embedding-stack"
    import_ok = _venv_import_ok()
    model_ok, tokenizer_ok = _onnx_model_files_present()

    if import_ok and model_ok and tokenizer_ok:
        return _ok(
            name=name,
            detail="onnxruntime + tokenizers import; model.onnx + tokenizer.json present.",
        )

    parts = []
    if not import_ok:
        parts.append("onnxruntime/tokenizers import failed in .venv")
    if not model_ok:
        parts.append("model.onnx missing")
    if not tokenizer_ok:
        parts.append("tokenizer.json missing")
    return _fail(
        name=name,
        detail=(
            f"{'; '.join(parts)}. Run `.venv/bin/pip install -e .[dev]` and "
            "`python scripts/export_onnx.py` to rebuild the onnx model files."
        ),
    )


def check_corpus_drift(opts: DoctorOptions) -> CheckResult:
    name = "corpus-drift"
    violations = _detect_parity_violations()
    if not violations:
        return _ok(
            name=name,
            detail="Graph matches bible/ (no parity violations).",
        )
    return _warn(
        name=name,
        detail=(
            f"{len(violations)} graph node(s) absent from bible/ (drift). "
            "`writ validate` reports without mutating; --fix runs reconcile."
        ),
        fixable=True,
        fix=_run_reconcile,
    )


def check_bitbucket_creds(opts: DoctorOptions) -> CheckResult:
    name = "bitbucket-creds"
    email_present, token_present = _bitbucket_creds_present()

    if not (email_present and token_present):
        return _fail(
            name=name,
            detail=(
                "Bitbucket email and/or token absent from writ.toml [bitbucket]; "
                "add them (a token cannot be auto-fixed)."
            ),
        )

    if opts.net:
        code = _bitbucket_live_auth(opts.repo)
        if code is None:
            return _ok(
                name=name,
                detail=(
                    "Bitbucket credentials present; no Bitbucket remote in this "
                    "repo to verify auth (presence-only)."
                ),
            )
        if code == 200:
            return _ok(
                name=name,
                detail="Bitbucket credentials present and authenticated (200).",
            )
        return _fail(
            name=name,
            detail=(
                f"Bitbucket auth returned {code}; regenerate the Atlassian token "
                "with scopes read:repository, read:pullrequest, write:pullrequest."
            ),
        )

    return _ok(
        name=name,
        detail="Bitbucket credentials present (presence-only; use --net to verify auth).",
    )


def check_git_post_commit_hook(opts: DoctorOptions) -> CheckResult:
    name = "git-post-commit-hook"
    if _git_hook_installed(opts.repo):
        return _ok(
            name=name,
            detail="Writ post-commit hook installed.",
        )
    repo = opts.repo
    return _fail(
        name=name,
        detail="Writ post-commit hook absent; --fix installs it idempotently.",
        fixable=True,
        fix=lambda: _install_git_hook(repo),
    )


def check_writ_path_symlink(opts: DoctorOptions) -> CheckResult:
    name = "writ-path-symlink"
    which_ok, readlink_ok = _path_symlink_ok()
    if which_ok and readlink_ok:
        return _ok(
            name=name,
            detail="`writ` resolves on PATH to the skill bin/writ shim.",
        )
    if not which_ok:
        detail = (
            "`writ` does not resolve on PATH; --fix recreates "
            "~/.local/bin/writ (ensure ~/.local/bin is on PATH)."
        )
    else:
        detail = (
            "`writ` resolves but points elsewhere than the skill bin/writ; "
            "--fix recreates the symlink."
        )
    return _fail(
        name=name,
        detail=detail,
        fixable=True,
        fix=_recreate_symlink,
    )


def check_cc_hook_registration(opts: DoctorOptions) -> CheckResult:
    name = "cc-hook-registration"
    all_ok, missing = _cc_registration_ok()
    if all_ok:
        return _ok(
            name=name,
            detail=(
                "plugin.json + hooks.json present; all referenced hooks executable "
                "(a new hooks.json mapping needs a fresh CC session to load)."
            ),
        )
    return _fail(
        name=name,
        detail=(
            "Hook scripts missing or non-executable: "
            f"{', '.join(missing)}. Restore them and start a fresh CC session."
        ),
    )


def _global_settings_path() -> Path:
    """The user-level settings file whose `hooks` block would double-register Writ."""
    return Path(os.environ.get("WRIT_SETTINGS_TARGET", "")) or (
        Path.home() / ".claude" / "settings.json"
    )


def _loaded_plugin_paths() -> list[str]:
    """Install paths of currently loaded Claude Code plugins."""
    if shutil.which("claude") is None:
        return []
    try:
        out = subprocess.run(
            ["claude", "plugin", "list", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return []
        return [
            e.get("installPath", "")
            for e in json.loads(out.stdout)
            if e.get("enabled") is not False and e.get("installPath")
        ]
    except Exception:
        return []


def check_duplicate_hook_registration(opts: DoctorOptions) -> CheckResult:
    """Writ's hooks registered twice: once by the plugin loader, once in settings.json.

    `patch-global-config.sh --hooks` refuses to create this state, but it can still arise
    by another route -- most plausibly seeding an arbitrary-path install and later moving it
    under ~/.claude/skills, where it also becomes plugin-discoverable. Both surfaces would
    then fire all 12 events: doubled rule injection, doubled gate evaluation, duplicated
    telemetry, and a single-use gate token one path could consume before the other reads it.
    A settings.json registration on its own is CORRECT (that is the install the seeding step
    exists for), so only the overlap is reported.
    """
    name = "duplicate-hook-registration"
    settings = _global_settings_path()
    try:
        doc = json.loads(settings.read_text()) if settings.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return _ok(name=name, detail=f"{settings} unreadable; nothing to compare.")

    settings_hooks = doc.get("hooks") or {}
    if not settings_hooks:
        return _ok(name=name, detail="No hooks block in user settings; the plugin owns hooks.")

    skill_root = str(Path(__file__).resolve().parent.parent.parent)
    if skill_root not in _loaded_plugin_paths():
        return _ok(
            name=name,
            detail=(
                f"{len(settings_hooks)} hook events in user settings and this install is not "
                "plugin-loaded: settings.json is the only registration surface, as intended "
                "for an install nothing auto-discovers."
            ),
        )

    return _warn(
        name=name,
        detail=(
            f"Writ hooks are registered TWICE: {len(settings_hooks)} events in {settings} AND "
            f"this install ({skill_root}) is loaded as a plugin, which registers them again. "
            "Every hook likely fires twice (doubled rule injection, doubled gate evaluation, "
            f"duplicated telemetry). Remove the `hooks` block from {settings} and keep the "
            "plugin registration."
        ),
    )


def _user_agents_dir() -> Path:
    """The user-level agents dir bootstrap.sh symlinks the writ-* role files into."""
    return Path.home() / ".claude" / "agents"


def check_role_symlinks(opts: DoctorOptions) -> CheckResult:
    """Dangling ~/.claude/agents/writ-*.md symlinks after the role files moved.

    bootstrap.sh links the five role files into ~/.claude/agents so they are available as
    USER agents in every project. When the files moved from .claude/agents/ to agents/, any
    existing symlink began pointing at a deleted path. Re-running bootstrap.sh repairs them
    (link_all relinks any target that is already a symlink), but an upgrade without a re-run
    leaves five broken links, and a broken agent definition is exactly the kind of silence
    worth surfacing. Having NO links is fine: a plugin-only install gets the roles from the
    plugin itself.
    """
    name = "role-symlinks"
    agents = _user_agents_dir()
    if not agents.is_dir():
        return _ok(name=name, detail=f"No {agents}; the plugin provides the roles.")

    broken = sorted(
        p.name for p in agents.glob("writ-*.md")
        if p.is_symlink() and not p.exists()
    )
    if not broken:
        return _ok(
            name=name,
            detail=f"No dangling writ-* role symlinks in {agents}.",
        )
    return _warn(
        name=name,
        detail=(
            f"Dangling role symlinks in {agents}: {', '.join(broken)}. The role files moved "
            "to agents/ in the install; re-run scripts/bootstrap.sh to repoint them, or "
            "delete them to fall back on the plugin-provided roles."
        ),
    )


def check_mode_gate_sanity(opts: DoctorOptions) -> CheckResult:
    name = "mode-gate-sanity"
    from writ.session import mode_engine

    valid_modes = getattr(mode_engine, "VALID_MODES", None)
    if valid_modes is None:
        valid_modes = set(getattr(mode_engine, "MODE_CONFIG", {}))

    cache = _latest_session_cache(opts.session_id)
    if cache is None:
        return _warn(
            name=name,
            detail="No session cache found; run `writ mode set <mode>` to start a session.",
        )

    mode = cache.get("mode")
    if not mode:
        return _warn(
            name=name,
            detail="Session cache has no mode set; run `writ mode set <mode>`.",
        )

    if mode not in valid_modes:
        return _warn(
            name=name,
            detail=(
                f"Session mode {mode!r} is not a valid mode; "
                "run `writ mode set <mode>` to reset it."
            ),
        )

    if cache.get("phase") == "planning":
        gates = cache.get("gates") or {}
        advanced = any(
            (g.get("advanced") if isinstance(g, dict) else g)
            for g in gates.values()
        )
        if advanced:
            return _warn(
                name=name,
                detail=(
                    "Stale state: phase is 'planning' but a gate has advanced; "
                    "run `writ mode set <mode>` to reset the cycle."
                ),
            )

    return _ok(
        name=name,
        detail=f"Session mode {mode!r} is valid and consistent.",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _parse_bolt_uri(uri: str) -> tuple[str, int]:
    """Extract (host, port) from a bolt:// URI; defaults to localhost:7687."""
    rest = uri.split("://", 1)[-1]
    rest = rest.split("/", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[-1]
    if ":" in rest:
        host, port_s = rest.rsplit(":", 1)
        try:
            return (host or "localhost", int(port_s))
        except ValueError:
            return (host or "localhost", 7687)
    return (rest or "localhost", 7687)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #

_CHECKS: list[tuple[str, Callable[[DoctorOptions], CheckResult]]] = [
    ("daemon-liveness", check_daemon_liveness),
    ("stale-orphan-port-conflict", check_stale_orphan_port_conflict),
    ("neo4j-connectivity", check_neo4j_connectivity),
    ("uniqueness-constraints", check_uniqueness_constraints),
    ("embedding-stack", check_embedding_stack),
    ("corpus-drift", check_corpus_drift),
    ("bitbucket-creds", check_bitbucket_creds),
    ("git-post-commit-hook", check_git_post_commit_hook),
    ("writ-path-symlink", check_writ_path_symlink),
    ("cc-hook-registration", check_cc_hook_registration),
    ("duplicate-hook-registration", check_duplicate_hook_registration),
    ("role-symlinks", check_role_symlinks),
    ("mode-gate-sanity", check_mode_gate_sanity),
]


def run_all_checks(opts: DoctorOptions) -> list[CheckResult]:
    """Run every check in order; isolate each so one exception never crashes the rest."""
    results: list[CheckResult] = []
    for check_name, fn in _CHECKS:
        try:
            results.append(fn(opts))
        except Exception as exc:  # ERR-GRACEFUL-001: one bad check never stops the rest.
            results.append(
                _fail(
                    name=check_name,
                    detail=str(exc),
                )
            )
    return results
