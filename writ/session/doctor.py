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
import re
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


def _subagent_role_scope_census() -> list[dict]:
    """[{"name": ..., "write_scope": ...}] for every SubagentRole node (async bridged).

    `write_scope` is passed through UNCOALESCED: None means the node declares none and a
    list (empty included) means it declares one, and that distinction is the whole point of
    the check that reads this. One query over a five-node label, via the existing
    get_all_nodes_by_type, so no new db method exists to keep in sync.
    """
    from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
    from writ.graph.db import Neo4jConnection

    async def _run() -> list[dict]:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            return [
                {"name": node.get("name"), "write_scope": node.get("write_scope")}
                for node in await db.get_all_nodes_by_type("SubagentRole")
            ]
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


# The three record labels and the property each one MERGEs on
# (writ/graph/db/record_store.py). Kept here rather than imported so a doctor
# check never pulls the graph package in just to name a label.
_RECORD_KEYS: dict[str, str] = {
    "Decision": "decision_id",
    "FileChange": "change_id",
    "Commit": "commit_hash",
}


def _count_duplicate_records() -> dict[str, int]:
    """{label: number of (id, project) keys held by more than one node}.

    Absent labels are omitted, so a clean graph returns {}. This has to be
    runnable BEFORE the uniqueness constraints are applied, because Neo4j
    refuses to create a constraint over data that already violates it: without
    this check a duplicate turns a silent data problem into an opaque migrate
    failure.
    """
    from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
    from writ.graph.db import Neo4jConnection

    async def _run() -> dict[str, int]:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            found: dict[str, int] = {}
            for label, key in _RECORD_KEYS.items():
                rows = await db._run(
                    f"MATCH (n:{label}) WHERE n.{key} IS NOT NULL "
                    f"WITH n.{key} AS k, n.project AS p, count(*) AS c "
                    "WHERE c > 1 "
                    "RETURN count(*) AS groups"
                )
                groups = rows[0]["groups"] if rows else 0
                if groups:
                    found[label] = groups
            return found
        finally:
            await db.close()

    return asyncio.run(_run())


def _index_degeneracy() -> dict:
    """{"zero_count": n, "sample_size": m} for the live HNSW index on disk.

    Reads the sidecar and .bin directly rather than going through
    `load_index`, which needs a corpus hash the doctor has no reason to know.
    An absent or unreadable index is not degeneracy: it reports a zero-length
    sample, which the check reads as "nothing to judge".
    """
    import hnswlib
    import numpy as np

    from writ.retrieval.embeddings import (
        DEFAULT_HNSW_CACHE_DIR,
        _ZERO_NORM_EPS,
        _degeneracy_sample_ids,
    )

    cache_dir = Path(DEFAULT_HNSW_CACHE_DIR)
    sidecar = cache_dir / "writ_hnsw.json"
    binary = cache_dir / "writ_hnsw.bin"
    if not sidecar.exists() or not binary.exists():
        return {"zero_count": 0, "sample_size": 0}
    data = json.loads(sidecar.read_text())
    rule_count = int(data["rule_count"])
    sample_ids = _degeneracy_sample_ids(rule_count)
    if not sample_ids:
        return {"zero_count": 0, "sample_size": 0}
    idx = hnswlib.Index(space="cosine", dim=int(data["dims"]))
    idx.load_index(str(binary), max_elements=rule_count)
    stored = np.asarray(
        idx.get_items(sample_ids, return_type="numpy"), dtype=np.float64
    )
    norms = np.linalg.norm(stored, axis=1)
    return {
        "zero_count": int((norms <= _ZERO_NORM_EPS).sum()),
        "sample_size": len(sample_ids),
    }


_INSTALL_MODULE = _PACKAGE_ROOT / "bin" / "lib" / "writ_install.py"
_GLOBAL_CONFIG_PATCHER = _PACKAGE_ROOT / "scripts" / "patch-global-config.sh"


def _socket_state() -> dict:
    """The daemon socket's presence, its directory's mode, and whether it answers.

    The DIRECTORY mode is the one that matters. uvicorn chmods the socket itself to
    0o666, so a socket in a traversable directory is reachable by every local
    account and looks private while being nothing of the kind.
    """
    import stat as stat_mod

    from writ.config import get_daemon_socket_path

    path = get_daemon_socket_path()
    state = {"path": path, "exists": False, "dir_mode": None, "answers": False}
    try:
        state["exists"] = stat_mod.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return state
    try:
        state["dir_mode"] = stat_mod.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
    except OSError:
        pass
    if state["exists"]:
        state["answers"] = _socket_answers(path)
    # ASK THE DAEMON, never this process. The flag lives in the service's environment
    # (a systemd drop-in at ~/.config/systemd/user/writ-server.service.d/), which the
    # CLI never inherits, so reading os.environ here printed "TCP still serves every
    # route" at a daemon that was returning 403 to every TCP write. None means the
    # daemon could not be asked, which is a third state and not a default.
    health = _socket_health(path)
    state["tcp_readonly"] = health.get("tcp_readonly") if health else None
    return state


def _socket_health(path: str = "") -> dict | None:
    """The daemon's /health payload, over the socket if it answers, else over TCP.

    `GET /health` is a read, and enforcement bounds only what TCP may CHANGE, so this
    fallback works while enforcement is on, which is exactly when the answer matters
    most. Returns None when
    neither transport answers, so the caller can report "could not ask" rather than
    guessing in either direction (CLEAN-ERR-001).
    """
    import http.client
    import json as _json

    if path:
        class _Conn(http.client.HTTPConnection):
            def connect(self) -> None:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect(path)
                self.sock = sock

        conn = _Conn("localhost", timeout=2.0)
        try:
            conn.request("GET", "/health", headers={"Host": "localhost"})
            response = conn.getresponse()
            if response.status == 200:
                return _json.loads(response.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - a diagnostic must not raise
            pass
        finally:
            conn.close()

    return _http_get_health()


def _socket_answers(path: str) -> bool:
    """True when the daemon serves /health over this socket."""
    import http.client

    class _Conn(http.client.HTTPConnection):
        def connect(self) -> None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(path)
            self.sock = sock

    conn = _Conn("localhost", timeout=2.0)
    try:
        conn.request("GET", "/health", headers={"Host": "localhost"})
        return conn.getresponse().status == 200
    except Exception:  # noqa: BLE001 - a diagnostic must not raise
        return False
    finally:
        conn.close()


def _missing_allow_entries(
    target: Path = Path.home() / ".claude" / "settings.json",
) -> list[str]:
    """Shipped Writ permission entries and managed settings keys absent from `target`.

    Asks writ_install.py, the module that OWNS both lists, instead of keeping a copy
    here. A copy drifts, and a drifted copy makes this check silently always-fail.
    Raises when the file cannot be read or parsed, so the check can report that
    distinctly from "entries are missing".

    `target` defaults to the real per-user settings file, because `writ doctor` with no
    argument has to diagnose the actual machine. This is read-only: nothing here writes.
    The parameter is the seam a test uses to point the whole diagnosis path at a temp
    file instead of mocking subprocess.run and asserting on a mock's own return value.

    A managed key the user set to a different value is deliberately NOT returned:
    check-settings prints it on a `[check-settings]`-prefixed line, which the parse below
    drops, because Writ's policy is to keep that value rather than to fix it.
    """
    proc = subprocess.run(
        ["python3", str(_INSTALL_MODULE), "check-settings", "--target", str(target)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode == 0:
        return []
    entries = [
        line.strip() for line in proc.stdout.splitlines()
        if line.strip() and not line.startswith("[check-settings]")
    ]
    if entries:
        return entries
    raise RuntimeError((proc.stderr or proc.stdout).strip() or "check-settings failed")


def _patch_global_config() -> None:
    """Fix callable: re-run the patcher that merges the entries (idempotent)."""
    subprocess.run(["bash", str(_GLOBAL_CONFIG_PATCHER)], check=False, timeout=300)


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


# Where a plugin's hook registrations live when its manifest does not say.
#
# Claude Code discovers them by CONVENTION, so a plugin.json with no `hooks` key is
# normal: this repo's has keys $schema, author, commands, description, keywords,
# license, name and version, and its hooks fire. Before this default, an absent key
# made `hooks_ref` empty, `_PACKAGE_ROOT / ""` resolved to the package root, and
# open() on a directory raised IsADirectoryError, which the except turned into
# "hook scripts missing or non-executable: <the package root>". A correct install
# reported a directory as a missing script.
#
# A CONSTANT, not an inline literal, because this encodes a convention rather than a
# contract: if the loader ever stops auto-detecting this path, the day it changes
# there should be one place to look.
DEFAULT_HOOKS_MANIFEST = "hooks/hooks.json"


def _cc_registration_ok() -> tuple[bool, list[str]]:
    """(all_ok, [missing_or_non_exec_paths]) for every .sh referenced by hooks.json.

    An explicit `hooks` key in plugin.json wins; its ABSENCE falls back to
    DEFAULT_HOOKS_MANIFEST. A declared path that cannot be read is still a failure,
    and so are a missing script and a non-executable one: only the absence of a
    declaration is benign.
    """
    plugin_json = _PACKAGE_ROOT / ".claude-plugin" / "plugin.json"
    missing: list[str] = []
    try:
        with open(plugin_json) as f:
            plugin = json.load(f)
    except (OSError, ValueError):
        return (False, [str(plugin_json)])

    hooks_ref = plugin.get("hooks") or DEFAULT_HOOKS_MANIFEST
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
    """Read the named session cache, or the CURRENT session's; None when neither exists."""
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

    # Falsy session_id: the canonical resolver is the ONLY answer. It reports the session
    # this process actually belongs to ($CLAUDE_SESSION_ID, then basename($CLAUDE_JOB_DIR))
    # or None, and None is reported as none.
    #
    # NO LOCAL MTIME GLOB. This used to re-implement the resolver's own deleted last resort
    # (newest writ-session-*.json in a directory every project's sessions share), so
    # deleting that tier from the resolver alone would have left `writ doctor` still
    # reporting a stranger's cache as this session's state. The doctor is the tool an
    # operator reaches for once they ALREADY suspect the state is wrong, so a confident
    # wrong answer here is worse than "no session": it retires the suspicion that was
    # correct. Same reason a resolved id with no cache file returns None instead of
    # falling through; the honest answer is that this session has no cache yet.
    resolved = cache_mod.resolve_current_session_id()
    if not resolved:
        return None
    path = os.path.join(cache_dir, f"writ-session-{resolved}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
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


# A FLOOR on the count, deliberately not raised when the record constraints were
# added, because a count cannot express which constraints exist. Measured on the
# live graph: it already carried uniqueness on (decision_id, project) and
# (commit_hash, project) under legacy names, and `CREATE CONSTRAINT ... IF NOT
# EXISTS` is satisfied by an equivalent constraint whatever it is called, so the
# statement is a no-op and the count does not move. Raising this to 20 made the
# check FAIL on a graph that was correctly constrained. Constraint identity is
# asserted by (label, properties) in tests/test_write_side_integrity.py instead.
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


def check_duplicate_records(opts: DoctorOptions) -> CheckResult:
    """Surfaces forked record nodes, which block the uniqueness constraints.

    NOT fixable from here on purpose. Which of two forked Decisions to keep is a
    judgement about the user's own history, not a migration, so this reports and
    a human resolves.
    """
    name = "duplicate-records"

    try:
        duplicates = _count_duplicate_records()
    except Exception as exc:
        return _fail(name=name, detail=f"Could not count duplicate records ({exc}).")

    if not duplicates:
        return _ok(name=name, detail="No duplicate record keys.")

    parts = ", ".join(f"{label}: {count}" for label, count in sorted(duplicates.items()))
    return _fail(
        name=name,
        detail=(
            f"Duplicate record keys ({parts}). Each is one (id, project) held by "
            "more than one node, so the matching uniqueness constraint cannot be "
            "created until they are merged by hand."
        ),
    )


def check_index_degeneracy(opts: DoctorOptions) -> CheckResult:
    """Surfaces a partially zero-norm HNSW index.

    A fully degenerate index is rejected at load and rebuilt. A PARTIAL one
    loads and serves noise for those rows, and used to be reported only to a
    logger nobody reads: 313 zero-norm vectors did that for six days.
    """
    name = "index-degeneracy"

    try:
        state = _index_degeneracy()
    except Exception as exc:
        return _warn(name=name, detail=f"Could not sample the HNSW index ({exc}).")

    sample_size = state.get("sample_size", 0)
    zero_count = state.get("zero_count", 0)
    if not sample_size:
        return _ok(name=name, detail="No HNSW index on disk to sample.")
    if not zero_count:
        return _ok(name=name, detail=f"{sample_size} sampled vector(s), none zero-norm.")
    return _warn(
        name=name,
        detail=(
            f"{zero_count} of {sample_size} sampled vector(s) have zero norm. "
            "Retrieval serves noise for those rules. Rebuild the index to clear "
            "it; a rebuild that does not clear it means the corpus text is empty."
        ),
    )


def check_daemon_socket(opts: DoctorOptions) -> CheckResult:
    """Reports the daemon's private transport, and whether it is actually private.

    A socket in a world-traversable directory is the failure this check exists for:
    uvicorn creates the socket 0o666, so the 0700 directory is the entire control,
    and getting that wrong produces a private-looking door anyone can open.
    """
    name = "daemon-socket"

    try:
        state = _socket_state()
    except Exception as exc:
        return _warn(name=name, detail=f"Could not inspect the daemon socket ({exc}).")

    path = state.get("path") or "(unresolved)"
    if not state.get("exists"):
        return _warn(
            name=name,
            detail=(
                f"No daemon socket at {path}; clients fall back to TCP on "
                f"{_DAEMON_PORT}, which every local account can reach."
            ),
        )

    mode = state.get("dir_mode")
    if mode is not None and mode != 0o700:
        return _fail(
            name=name,
            detail=(
                f"The socket directory for {path} is {oct(mode)}, not 0o700. uvicorn "
                "creates the socket itself world-writable (0o666), so the directory "
                "is the only thing keeping other local accounts out."
            ),
        )

    if not state.get("answers"):
        return _warn(
            name=name,
            detail=f"Socket present at {path} but the daemon did not answer /health over it.",
        )

    # Whether TCP is restricted is the question this check exists to answer alongside
    # the socket's health: a working socket with TCP wide open is the state the sweep
    # started from, and it is indistinguishable from the finished state without this.
    readonly = state.get("tcp_readonly")
    if readonly is True:
        tcp = "TCP restricted to read-only requests plus POST /query"
    elif readonly is False:
        tcp = ("TCP still serves every route (set WRIT_TCP_READONLY=1 for the daemon "
               "once the transport census reads zero state-touching TCP writes)")
    else:
        # Neither transport answered /health. Saying either "restricted" or "open"
        # here would be the same false confidence this check was fixed to remove.
        tcp = "could not ask the daemon whether TCP is restricted"
    return _ok(
        name=name,
        detail=f"Daemon answers over {path}; directory is 0o700; {tcp}.",
    )


def check_permissions_allowlist(opts: DoctorOptions) -> CheckResult:
    """Detects a HALF-APPLIED install: the plugin loaded, the permission patch did not.

    The plugin manifest schema has no permissions field, so the allowlist can only
    arrive from scripts/patch-global-config.sh. bootstrap-plugin.sh only WARNS when
    that step fails, and the SessionStart detector keys on the venv, so
    venv-present-but-allowlist-absent looks healthy indefinitely while every
    read-only Writ command prompts the user.
    """
    name = "permissions-allowlist"

    try:
        missing = _missing_allow_entries()
    except Exception as exc:
        return _fail(
            name=name,
            detail=f"Could not read the Writ permission entries ({exc}).",
            fixable=True,
            fix=_patch_global_config,
        )

    if not missing:
        return _ok(name=name, detail="Writ permission entries and settings keys present.")

    return _fail(
        name=name,
        detail=(
            f"{len(missing)} Writ item(s) missing from ~/.claude/settings.json "
            "(permission entries and/or managed settings keys): a missing permission "
            "entry makes read-only Writ commands prompt on every call, and a missing "
            "settings key means Writ's shipped default was never applied. Fix with "
            "`bash scripts/patch-global-config.sh`."
        ),
        fixable=True,
        fix=_patch_global_config,
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


def _registered_hook_scripts() -> dict[str, Path]:
    """Hook name to script path, from the plugin's hooks manifest. Empty when unreadable.

    Names come from the `.sh` token in each registered command, which is also the basename
    the telemetry row carries, so the two sets are directly comparable.
    """
    manifest = _PACKAGE_ROOT / DEFAULT_HOOKS_MANIFEST
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found: dict[str, Path] = {}
    for entries in (doc.get("hooks") or {}).values():
        for entry in entries or []:
            for hook in entry.get("hooks") or []:
                for token in str(hook.get("command", "")).split():
                    if not token.endswith(".sh"):
                        continue
                    # Commands are `bash ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/<name>.sh`;
                    # everything after the closing brace is the package-relative path.
                    relative = token.split("}", 1)[-1].lstrip("/")
                    found[token.rsplit("/", 1)[-1][:-3]] = _PACKAGE_ROOT / relative
    return found


def _observed_hook_names() -> set[str]:
    """`hook_name` values present in the metrics stream, live file plus archives.

    Separate from the structural check so the observational arm can be substituted in
    tests, and so a missing or unreadable log degrades to "nothing observed" rather than
    to a failure: absence of rows is never itself a defect here.
    """
    import gzip

    # stream_path, NOT the friction-log resolver: that one answers with whatever
    # WRIT_FRICTION_LOG names (on this machine a bare `workflow-friction.log` at the repo
    # root), so its parent is not the per-project stream directory and the metrics file was
    # never found. Caught by the doctor reporting "no metrics stream" against a live file
    # with rows in it.
    try:
        from writ.shared.logging import resolve_project, stream_path

        metrics = stream_path(resolve_project(), "metrics")
    except Exception:  # noqa: BLE001 - a log-path fault must not fail the check
        return set()
    project_dir = metrics.parent
    names: set[str] = set()
    candidates = [project_dir / "metrics.jsonl"]
    candidates += sorted((project_dir / "archive").glob("metrics-*.jsonl*"))
    for path in candidates:
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", errors="replace") as handle:  # type: ignore[operator]
                for line in handle:
                    if "hook_execution" not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("event") == "hook_execution" and row.get("hook_name"):
                        names.add(str(row["hook_name"]))
        except OSError:
            continue
    return names


def stream_path(project: str, kind: str) -> Path:
    """The per-project log stream, as one patchable seam.

    A thin indirection over writ.shared.logging.stream_path so a check that reads a stream
    can be pointed at a fixture without also faking project resolution. Module level on
    purpose: that is what makes it substitutable.
    """
    from writ.shared.logging import stream_path as _stream_path

    return Path(_stream_path(project, kind))


def _stream_rows(stream: str, event: str) -> list[dict] | None:
    """Rows of one event kind from one per-project stream, or None when none is readable.

    The WHOLE corpus: the live file, every archive generation, and the gzipped ones. A
    refusal that survives only in an archive must not read as a refusal that never
    happened, which is what a live-file-only reader would report.

    None and [] are DIFFERENT and every caller depends on it: None means no file could be
    read at all (fresh install, redirected log, pruned directory), while [] means the
    stream was read and held no such row. Cycle I's first version conflated them and
    accused all 40 hooks of never running on a machine whose log simply was not there.
    """
    import gzip

    try:
        from writ.shared.logging import resolve_project

        live = stream_path(resolve_project(), stream)
    except Exception:  # noqa: BLE001 - a log-path fault must not fail a check
        return None

    project_dir = Path(live).parent
    candidates = [Path(live)]
    if Path(live).name != f"{stream}.jsonl":
        candidates.append(project_dir / f"{stream}.jsonl")
    candidates += sorted((project_dir / "archive").glob(f"{stream}-*.jsonl*"))

    rows: list[dict] = []
    read_any = False
    for path in candidates:
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", errors="replace") as handle:  # type: ignore[operator]
                read_any = True
                for line in handle:
                    if event not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("event") == event:
                        rows.append(row)
        except OSError:
            continue
    if not read_any:
        return None
    return rows


def _metrics_rows(event: str) -> list[dict] | None:
    """Rows of one event kind from the metrics stream, or None when nothing is readable.

    ONE ARGUMENT, and it stays that way: `tests/test_doctor.py::_patch_all_ok`
    monkeypatches this name as `lambda event: []`, so the shared body lives in
    `_stream_rows(stream, event)` and this is the metrics-stream binding of it.
    """
    return _stream_rows("metrics", event)


def _audit_rows(event: str) -> list[dict] | None:
    """Rows of one event kind from the audit stream, or None when nothing is readable.

    The audit-stream sibling of `_metrics_rows`. `gate_decision` lives here, which is why
    nothing reading `metrics` could ever tell whether a gate had refused.
    """
    return _stream_rows("audit", event)


# --------------------------------------------------------------------------- #
# Gate refusal liveness: two independent artifacts, kept apart on purpose
# --------------------------------------------------------------------------- #
#
# CAPABILITY comes from the hook SOURCES and BEHAVIOUR comes from the AUDIT STREAM, and
# neither is computed from the other. A check whose expected side is derived from the
# side under test cannot fail, and this repo has shipped that shape twice.
#
# A REFUSAL IS `deny` OR `ask`, never "anything that is not allow". Three live gates
# prove both halves: `irreversible` records denies and no allows at all because its arm
# only logs on refusal, `bash-egress` and `review-blocking` record only asks because
# asking IS how they refuse, and `manual-test-grant` spells `grant`, `error` and
# `inherit`, none of which is a refusal. A naive rule misreads all three.

REFUSING_DECISIONS = frozenset({"deny", "ask"})

# Everything a call site spells that does NOT stop the action. Declared rather than
# inferred, so a new verb added to a hook reddens in
# `tests/test_gate_refusal_liveness.py` instead of being silently counted as a
# non-refusal.
NON_REFUSING_DECISIONS = frozenset({"allow", "grant", "error", "inherit", "unknown"})

# A call site whose decision is a runtime VARIABLE. It counts as capable of refusing,
# which is the fail-loud direction and the one that matters: the source cannot prove such
# an arm unreachable, and reading uncertainty as "cannot refuse" would excuse the gate
# most likely to have a live deny path.
RUNTIME_DECIDED = "<runtime>"

_CLASS_REFUSING = "refusing"
_CLASS_NEVER_REFUSED = "never-refused"
_CLASS_UNEXERCISED = "unexercised"
_CLASS_CANNOT_REFUSE = "cannot-refuse"
_CLASS_ORPHANED = "orphaned"

_HOOK_SCRIPTS_DIR = _PACKAGE_ROOT / "hooks" / "scripts"

# A real call, not a mention: anchored at the start of a line, optionally behind an `&&`,
# and never inside a comment. `writ-debug-code-gate.sh` describes its own deny arm in
# prose, so a scan that read comments would credit a gate with a call site it does not
# have.
_GATE_CALL_RE = re.compile(
    r"^[ \t]*(?:[^#\n]*&&[ \t]*)?log_gate_decision[ \t]+(?P<args>\S.*)$", re.MULTILINE
)
_GATE_ARG_RE = re.compile(r'"([^"\n]*)"')
# `${NAME:-default}`, `${NAME-default}` and the `:=` spellings. The variable is what makes
# the gate capable; the default is a token the vocabulary still has to bucket.
_DEFAULTED_ARG_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*:?[-=]([^}$]*)\}$")


def _gate_deny_capability(
    *, scripts_dir: Path = _HOOK_SCRIPTS_DIR
) -> dict[str, dict[str, list[str]]] | None:
    """Which gate can refuse, derived from the hook sources, keyed by gate name.

    `{"<gate>": {"decisions": [...], "scripts": [...]}}`. None when no source tree was
    readable, `{}` when the tree was read and holds no call site. Those two are different
    and the check reports them differently: conflating them would tell a machine with no
    hook tree that every one of its gates had lost its deny arm.

    A gate logged by two scripts names both, because the class is about the GATE: one
    script losing its deny arm while a sibling keeps one is not a defect.
    """
    root = Path(scripts_dir)
    if not root.is_dir():
        return None
    capability: dict[str, dict[str, list[str]]] = {}
    try:
        paths = sorted(root.glob("*.sh"))
    except OSError:
        return None
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _GATE_CALL_RE.finditer(text):
            args = _GATE_ARG_RE.findall(match.group("args"))
            if len(args) < 2:
                continue
            gate, decision = args[0], args[1]
            if not gate or "$" in gate:
                continue
            entry = capability.setdefault(gate, {"decisions": [], "scripts": []})
            for token in _decision_tokens(decision):
                if token not in entry["decisions"]:
                    entry["decisions"].append(token)
            if path.name not in entry["scripts"]:
                entry["scripts"].append(path.name)
    for entry in capability.values():
        entry["decisions"].sort()
        entry["scripts"].sort()
    return capability


def _decision_tokens(argument: str) -> list[str]:
    """The decision tokens one call site's second argument can emit."""
    if "$" not in argument:
        return [argument] if argument else []
    tokens = [RUNTIME_DECIDED]
    defaulted = _DEFAULTED_ARG_RE.match(argument)
    if defaulted and defaulted.group(1):
        tokens.append(defaulted.group(1))
    return tokens


def _gate_refusal_classes(
    capability: dict[str, dict[str, list[str]]] | None, rows: list[dict] | None
) -> dict[str, str]:
    """One class per gate, from the two artifacts. Pure: no log and no source is read.

    Five outcomes, because three do not cover two states the corpus holds. `unexercised`
    exists because gates with call sites and zero rows are gates nothing has ever asked,
    not gates that stopped answering. `orphaned` exists because a renamed gate leaves its
    old rows in a 365-day stream forever and an alarm on history is noise.

    BEHAVIOUR CANNOT VOUCH FOR CAPABILITY. Refusal rows for a gate whose sources spell no
    refusing token leave it `cannot-refuse`, and a gate with no call site at all is
    `orphaned` however many rows it carries.
    """
    decided: dict[str, int] = {}
    refused: dict[str, int] = {}
    for row in rows or []:
        gate = row.get("gate")
        if not gate:
            continue
        gate = str(gate)
        decided[gate] = decided.get(gate, 0) + 1
        if str(row.get("decision") or "") in REFUSING_DECISIONS:
            refused[gate] = refused.get(gate, 0) + 1

    classes: dict[str, str] = {}
    for gate, entry in (capability or {}).items():
        tokens = set(entry.get("decisions") or ())
        if not (tokens & REFUSING_DECISIONS) and RUNTIME_DECIDED not in tokens:
            classes[gate] = _CLASS_CANNOT_REFUSE
        elif not decided.get(gate):
            classes[gate] = _CLASS_UNEXERCISED
        elif refused.get(gate):
            classes[gate] = _CLASS_REFUSING
        else:
            classes[gate] = _CLASS_NEVER_REFUSED
    for gate in decided:
        classes.setdefault(gate, _CLASS_ORPHANED)
    return classes


def _decision_date(row: dict) -> str | None:
    """The day a decision was made, as an ISO date, or None when the row carries neither.

    `decided_at` WINS WHERE IT EXISTS, and it is EPOCH SECONDS IN A STRING rather than
    ISO: `ts` on a buffered row is the FLUSH time, so where both are present the decision
    time is the honest one. `ts` is the fallback and not the other way round, because
    `_gd_emit_now` is the synchronous branch every deny and every ask takes (a denial must
    not wait for a drain) and it writes no decision stamp at all. Measured over the live
    corpus: none of the 952 refusals carries `decided_at`, so a reader that required it
    would report every refusal on this machine as dateless.
    """
    from datetime import datetime, timezone

    decided = row.get("decided_at")
    if decided:
        try:
            return datetime.fromtimestamp(
                int(str(decided).strip()), tz=timezone.utc).date().isoformat()
        except (ValueError, OSError, OverflowError):
            pass
    stamp = row.get("ts")
    if isinstance(stamp, str) and len(stamp) >= 10:
        return stamp[:10]
    return None


def _subagent_governance_evidence() -> tuple[dict[str, set[str]], set[str]] | None:
    """The census's buckets AND, separately, the universe they are supposed to partition.

    THE UNIVERSE IS RETURNED, NOT RECOVERED FROM THE BUCKETS, and that is the whole reason
    this function is not just `_subagent_governance_members`. `total` used to be the SUM of
    the bucket lengths, which made "the buckets partition the universe" true for every
    census this code could produce: the live partition assertion was identically zero, so
    neither a member that fell into NO bucket (the pre-cycle leak, one real agent) nor a
    member counted TWICE could redden it. `total` is `len(known)` now, computed from the
    universe set, so the sum is checked against something that does not depend on it.
    """
    import gzip

    try:
        from writ.shared.logging import resolve_project

        metrics = stream_path(resolve_project(), "metrics")
    except Exception:  # noqa: BLE001 - a log-path fault must not fail a check
        return None

    project_dir = Path(metrics).parent
    candidates = [Path(metrics)]
    if Path(metrics).name != "metrics.jsonl":
        candidates.append(project_dir / "metrics.jsonl")
    candidates += sorted((project_dir / "archive").glob("metrics-*.jsonl*"))

    started: set[str] = set()
    seeded: set[str] = set()
    spawn_seeded: set[str] = set()
    lazily_seeded: set[str] = set()
    seed_failed: set[str] = set()
    completed: set[str] = set()
    active: set[str] = set()
    read_any = False

    for path in candidates:
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", errors="replace") as handle:  # type: ignore[operator]
                read_any = True
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    event = row.get("event")
                    agent = str(row.get("agent_id") or "")
                    # The lazy path's failure row files the agent under `session` and
                    # carries no `agent_id` at all, because the process that writes it has
                    # only the one id to give.
                    who = agent or str(row.get("session") or "")
                    if event == "subagent_start":
                        started.add(who)
                    elif event == "subagent_seeded":
                        seeded.add(who)
                        source = str(row.get("cache_source") or "")
                        if source == "subagent_start":
                            spawn_seeded.add(who)
                        elif source == "lazy_seed":
                            lazily_seeded.add(who)
                    elif event == "subagent_seed_failed":
                        seed_failed.add(who)
                    elif event == "subagent_complete":
                        completed.add(agent)
                    else:
                        # Any other row filed under an agent's own id proves a Writ hook
                        # ran inside that agent, which is what makes it seedable.
                        session = str(row.get("session") or "")
                        if session and session != agent:
                            active.add(session)
        except OSError:
            continue

    if not read_any:
        return None

    # A later seed of any kind REPAIRS a failure, so only agents that were never seeded
    # afterwards are held against the governed count.
    unrepaired = seed_failed - seeded
    known = started | seeded | seed_failed | completed
    known.discard("")

    members: dict[str, set[str]] = {
        "governed": set(),
        "lazy": set(),
        "seed_failed": set(),
        "reachable": set(),
        "unreachable": set(),
    }
    for who in known:
        if who in unrepaired:
            bucket = "seed_failed"
        elif who in spawn_seeded:
            bucket = "governed"
        elif who in lazily_seeded:
            bucket = "lazy"
        elif who in started:
            bucket = "governed"
        elif who in seeded:
            bucket = "lazy"
        elif who in active:
            bucket = "reachable"
        else:
            bucket = "unreachable"
        members[bucket].add(who)
    return members, known


def _subagent_governance_members() -> dict[str, set[str]] | None:
    """The census's buckets, each naming the agents in it, or None when nothing is readable.

    THE PUBLISHED COUNTS ARE MADE OF THIS, not of a second reading of the same stream:
    `_subagent_governance_census` counts exactly this return value, so a by-name
    corroboration (the lazy bucket held against the `lazy_seed` caches on disk) can never
    drift from the number the doctor prints. The universe, the precedence ladder and why
    `active` adds no member are all recorded on that function.
    """
    evidence = _subagent_governance_evidence()
    return None if evidence is None else evidence[0]


def _subagent_governance_census() -> dict | None:
    """Bucket every sub-agent Writ has seen by how it came to be governed.

    Returns None when no stream is readable, which is NOT the same as "nothing is governed".

    Five buckets, because averaging them hides the thing worth knowing:
      governed:    the spawn path made the cache (a `subagent_seeded` row naming
                   `subagent_start`, or failing that a `subagent_start` row), and no
                   unrepaired seed failure contradicts it
      lazy:        a `subagent_seeded` row that does NOT name the spawn path, so a hook
                   inside the agent made the cache
      seed_failed: a `subagent_seed_failed` row with no later seed of any kind, so the
                   hook ran and the seeding inside it did not
      reachable:   none of those, but Writ hooks ran inside it (a daemon or hook row under
                   its own agent id), so a future hook could seed it
      unreachable: only stop-side rows, so Writ never ran anything inside it

    THE UNIVERSE IS EVERY AGENT NAMED BY A LIFECYCLE ROW, NOT EVERY AGENT THAT FINISHED.
    Every bucket used to be intersected with `completed`, and `total` was `len(completed)`.
    A lazily seeded agent exists BECAUSE its harness never delivered `SubagentStart`, and
    the same gap means no `subagent_complete` row arrives either, so the one population
    this check exists to find was absent from all five buckets AND from the denominator:
    the census reported `lazy: 0` against 9 `lazy_seed` rows in the archives and 10
    `lazy_seed` caches on disk. The universe is now
    `started | seeded | seed_failed | completed`.

    WHAT `total` MEANS TO A READER NOW. Not "dispatches that finished" but "sub-agents Writ
    has any lifecycle record of". That is the honest denominator for the question this
    check asks, because an agent that inherited nothing and then vanished is precisely the
    case being counted, and requiring it to complete makes the measurement conditional on
    the harness gap that causes the condition.

    THE REJECTED ALTERNATIVE was widening `total` alone and leaving the other four buckets
    keyed on `completed`. That yields parts which do not sum to their own total, a worse
    artifact than the defect it fixes, and it would have left `governed`, `reachable` and
    `unreachable` measuring a population chosen by the harness rather than by Writ.

    `active` IS A QUALIFIER AND NEVER ADDS A MEMBER. It collects `row["session"]` for every
    row whose event is none of the four lifecycle events, and a main session's rows carry a
    session with no `agent_id`, so every main session Writ has ever logged is in it.
    Measured 2026-09-16: `active` holds 10,110 sessions of which 3,593 have no sub-agent
    lifecycle row at all, so a member-adding `active` would take `total` from about 6,550 to
    about 10,140 and report the machine's whole session history as ungoverned sub-agents.
    The old intersection with `completed` filtered them out by accident; the exclusion is
    explicit now because the accident is gone. Those figures are approximate and dated on
    purpose: the stream grows while the machine is used, so a number written to the digit
    here would be wrong by the next hour and stay wrong.

    ONE PRECEDENCE LADDER, NOT SET ALGEBRA, so `sum(buckets) == total` is a CHECKABLE
    claim rather than an identity. `total` is `len(known)`, taken from the universe set and
    not from the bucket lengths, so the sum can come out below it (a member classified into
    no bucket) or above it (a member classified twice); summing the buckets into `total`
    would make both unobservable. The old expressions leaked: `ungoverned`
    subtracted `seeded` while `governed` only added `completed & started`, so an agent with
    a seed row and no start row fell into no bucket at all and the live run summed to one
    less than its own total. Each member is classified exactly once now, and a
    `lazy_seed`-marked seed row outranks a bare `subagent_start` row ON PURPOSE: the gate
    reads the cache's source and a `lazy_seed` cache confers no authority, so calling such
    an agent governed would overstate what happened.

    A SEED FAILURE IS A POSITIVE RECORD, NEVER AN INFERENCE FROM A MISSING ROW. A
    `subagent_start` row is written at the END of the hook whether or not the seed block ran,
    so that row alone stopped proving governance; what subtracts an agent from `governed` is
    its own `subagent_seed_failed` row and nothing else. The archives hold no such row, so
    every dispatch recorded before this event existed keeps exactly the classification it
    had, and nothing is retroactively relabelled.

    A SPAWN-MARKED SEED IS NOT A LAZY ONE. `seed_subagent_cache` logs `subagent_seeded` on
    both paths, so counting every such row as lazy would count each governed dispatch twice
    once spawn seeding works again. A row with no `cache_source` at all keeps its earlier
    lazy meaning: it is not evidence that the spawn hook seeded.

    READS THE ARCHIVES. The wrong diagnosis that produced this cycle came from a grep that
    silently skipped 226 gzipped files holding 1,421 `subagent_start` rows, so a census that
    reads only the live file would repeat the mistake it exists to correct.
    """
    evidence = _subagent_governance_evidence()
    if evidence is None:
        return None
    members, known = evidence
    census = {bucket: len(ids) for bucket, ids in members.items()}
    census["total"] = len(known)
    return census


def check_subagent_role_scope_coverage(opts: DoctorOptions) -> CheckResult:
    """How many sub-agent roles declare a write scope, and which ones do not.

    THE GAP IS THE POINT OF THE CHECK. A role that declares no `write_scope` keeps the
    unconditional write allow every dispatched sub-agent has always had, which is the
    correct degradation (a missing record must not become a refusal) but is invisible from
    the gate: nothing is logged for enforcement that never fired. This is where the share
    is reported, so "the boundary is not declared here" is a number rather than a surprise.

    AN UNREACHABLE GRAPH IS UNMEASURED, NOT A FAILURE. The doctor already fails loudly on
    Neo4j connectivity in its own check; failing again here would report one outage twice
    and, worse, would read as an accusation against the corpus.

    A CORPUS WHERE NOTHING DECLARES A SCOPE IS ONE WARN, NOT ONE PER ROLE. Roles are
    authored by hand and an undeclared scope is a legitimate choice (writ-implementer's
    scope is per-dispatch data, so no static glob can state it truthfully); the warn says
    the mechanism is inert, not that the roles are wrong.
    """
    name = "subagent-role-scope-coverage"

    try:
        roles = _subagent_role_scope_census()
    except Exception as exc:
        return _ok(
            name=name,
            detail=(
                f"Could not read SubagentRole nodes ({exc}), so declared write-scope "
                "coverage is unmeasured; neo4j-connectivity is the check that judges "
                "reachability."
            ),
        )

    if not roles:
        return _ok(
            name=name,
            detail="No SubagentRole nodes in the graph, so there is no scope to declare.",
        )

    # A LIST IS A DECLARATION, INCLUDING AN EMPTY ONE: `write_scope: []` is a role saying
    # it writes nothing, which is the strictest declaration there is, not a missing one.
    declared = [r for r in roles if isinstance(r.get("write_scope"), list)]
    undeclared = sorted(
        str(r.get("name") or "(unnamed)")
        for r in roles if not isinstance(r.get("write_scope"), list)
    )
    detail = f"{len(declared)} of {len(roles)} sub-agent role(s) declare a write scope"
    detail += f"; undeclared: {', '.join(undeclared)}." if undeclared else "."

    if not declared:
        return _warn(
            name=name,
            detail=(
                detail + " No role declares one, so the write gate's role-scope arm never "
                "fires and every dispatched sub-agent keeps the blanket write allow. "
                "Declare write_scope in the role's bible/methodology/ROL-*.md frontmatter "
                "and re-import if that is not intended."
            ),
        )
    return _ok(name=name, detail=detail)


def check_subagent_governance_census(opts: DoctorOptions) -> CheckResult:
    """How many sub-agents actually inherited a mode, and how many ran outside Writ?

    Measured 2026-08-27: 1,381 agents had a `subagent_start` row and 2,219 did not, with no
    overlap, so 64% inherited no mode, no approved gates and no injected rule. Those agents
    were not more dangerous for it (with no cache the write gate refused them outright), but
    they were invisible, and an ungoverned worker nobody counts is the thing this check
    exists to stop being normal.
    """
    name = "subagent-governance-census"
    census = _subagent_governance_census()
    if census is None:
        return _ok(name=name, detail="No readable metrics stream, so governance is unmeasured.")
    if not census["total"]:
        return _ok(name=name, detail="No sub-agent dispatches recorded yet.")

    # THE DENOMINATOR IS NOT "DISPATCHES" ANY MORE, and saying so is the point of the
    # rewording: it counts every sub-agent Writ holds a lifecycle row for, including the
    # lazily seeded ones that never produce a completion row at all. Calling those
    # "dispatches" is what let a whole population sit outside a number that looked total.
    detail = (
        f"{census['governed']} governed at spawn, {census['lazy']} lazily seeded, "
        f"{census['seed_failed']} whose seeding failed, "
        f"{census['reachable']} ungoverned but reachable, "
        f"{census['unreachable']} unreachable, of {census['total']} sub-agent(s) Writ has "
        "a lifecycle record of. "
        "The seeding-failure count comes only from subagent_seed_failed rows. Both the "
        "spawn path and the lazy path write one now, but only for faults the seeder "
        "could see and report: an unreadable parent cache is already turned into an "
        "empty one a layer down, so the seeder finds no mode to inherit and declines "
        "instead of failing, and the row bash writes when the seeder never ran needs its "
        "own friction-append to survive. A zero here means no REPORTED failure, not a "
        "proven clean seed, and it still means unrecorded for the archived dispatches "
        "that predate the row."
    )
    covered = census["governed"] + census["lazy"]
    # Warn only when the MAJORITY is ungoverned. A handful of unreachable agents is the
    # harness's business, not a defect, and crying wolf about it trains the reader to skip
    # this line.
    if covered * 2 < census["total"]:
        return _warn(
            name=name,
            detail=(
                detail + " Most sub-agents inherit no mode and no rules. The reachable ones "
                "are seeded by any hook that runs inside them (see "
                "writ/session/subagent_seed.py); the unreachable ones run no Writ hook at "
                "all and cannot be reached from here."
            ),
        )
    return _ok(name=name, detail=detail)


def _unknown_buffer() -> Path:
    """The buffer bash writes when a hook files a row with no session to file it under."""
    from writ.session.cache import _cache_dir

    return Path(_cache_dir()) / "writ-events-unknown.buf"


def check_stranded_telemetry_buffer(opts: DoctorOptions) -> CheckResult:
    """Are any hook rows filed under a session that will never drain them?

    Rows are buffered per session and released by that session's own drain. A hook that
    resolves no session id files its rows under the literal id `unknown`, and while
    writ-flush-events.py does sweep an idle buffer after ABANDONED_SESSION_SECONDS, a
    frequent writer keeps the mtime young and the sweep never fires. Measured 2026-08-27:
    695 rows sat here, 673 from writ-statusline (not a hook, since excluded from
    instrumentation) and 22 from writ-subagent-stop (which now sets its session).

    WARN, NOT FAIL, and the rows are named rather than counted. Nothing is broken for the
    user: the rows are telemetry, not enforcement, and the sweep still collects them once
    the frequent writer stops. Naming the hooks is what makes the report actionable, since
    the fix is always "that hook is not setting a session id".
    """
    name = "stranded-telemetry-buffer"
    buffer_path = _unknown_buffer()
    try:
        raw = buffer_path.read_text(errors="replace")
    except OSError:
        # Absent or unreadable is the healthy state: nothing has filed an orphan row.
        return _ok(name=name, detail="No unattributable hook rows are buffered.")

    hooks: dict[str, int] = {}
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        fields = record.split("\x1f")
        if len(fields) < 2 or fields[0] != "hook_execution":
            continue
        hooks[fields[1]] = hooks.get(fields[1], 0) + 1

    if not hooks:
        # A zero-byte or separator-only file is not a stranded row. Reporting it would be
        # crying wolf, which cycle D spent a whole pass removing.
        return _ok(name=name, detail="No unattributable hook rows are buffered.")

    listed = ", ".join(f"{hook} ({count})" for hook, count in sorted(hooks.items()))
    total = sum(hooks.values())
    return _warn(
        name=name,
        detail=(
            f"{total} hook rows are buffered under the session id 'unknown' at "
            f"{buffer_path}, so they reach no log until the abandoned-session sweep runs: "
            f"{listed}. Each of these hooks resolves no session id; set SESSION_ID (or "
            f"HOOK_SESSION_ID) in the hook, or stop instrumenting it if it is not a hook."
        ),
    )


def check_subagent_role_coverage(opts: DoctorOptions) -> CheckResult:
    """How often is a dispatched sub-agent's ROLE actually known?

    This is the number cycle M's enforcement is not allowed to skip. `agent_type` arrives
    empty for the sub-agents that never receive a SubagentStart (10 of 10 captured
    SubagentStop payloads from that population, 2026-08-27; 2,219 records corpus-wide), and
    the hooks used to rewrite it to the literal `general-purpose`, so those records named a
    role nobody observed. An ordinary Agent dispatch carries it populated, probe-verified
    the same day. Roles are now resolved from the sidecar and carry a `role_source`, and
    this check reports the rate.

    NO OBSERVATIONS IS OK, NOT AN ACCUSATION. A machine that has dispatched nothing, or
    whose log is absent, is not a machine with a broken resolver.

    A ROW WITH NO `role_source` IS UNMEASURED, NOT UNRESOLVED, and the difference is the
    whole check. Every dispatch recorded before this field existed lacks it; counting those
    as failed resolutions reported "0 of 1066 resolved" on a machine where the resolver had
    simply never run yet, which is cycle I's mistake one level down, at row granularity.
    Only rows that carry the field are in the denominator.
    """
    name = "subagent-role-coverage"
    rows = _metrics_rows("subagent_complete")
    if rows is None:
        return _ok(
            name=name,
            detail="No readable metrics stream, so sub-agent role coverage is unmeasured.",
        )
    if not rows:
        return _ok(name=name, detail="No sub-agent dispatches recorded yet.")

    measured = [row for row in rows if row.get("role_source")]
    legacy = len(rows) - len(measured)
    if not measured:
        return _ok(
            name=name,
            detail=(
                f"{legacy} sub-agent dispatches recorded, none since role resolution was "
                "added, so coverage is not measurable yet."
            ),
        )

    unresolved = sum(1 for row in measured
                     if str(row.get("role_source")) == "unresolved")
    resolved = len(measured) - unresolved
    detail = (
        f"{resolved} of {len(measured)} measured sub-agent dispatches carry an observed "
        f"role ({unresolved} unresolved"
        + (f", {legacy} predate the field" if legacy else "")
        + ")."
    )
    if resolved == 0:
        return _warn(
            name=name,
            detail=(
                detail + " No role has been resolved since resolution was added, so "
                "nothing may key an authority decision on one yet: check that the sidecar "
                "layout under ~/.claude/projects still matches "
                "writ/session/subagent_role.py."
            ),
        )
    return _ok(name=name, detail=detail)


def check_hook_telemetry_coverage(opts: DoctorOptions) -> CheckResult:
    """Can Writ say whether each registered hook ran?

    Until 2026-08-27 it could not: 8 of 40 registered hooks emitted no `hook_execution`
    row by any means, behind a comment in common.sh claiming coverage was universal and
    enforced. Coverage is now structural, installed by common.sh for any script under
    `hooks/scripts/` that sources it, so this check verifies the two preconditions that
    makes true rather than scanning for a helper call. Three successive lexical scans for
    such a call returned 21, 12 and 10 before the right answer, 8, which is why this one
    does not count spellings.

    THE STRUCTURAL ARM FAILS, THE OBSERVATIONAL ARM WARNS. A registered hook that is not
    under `hooks/scripts/` or does not source `common.sh` cannot be instrumented, which is
    definite and readable from disk. A hook with no row in the retention window is only a
    hint: PreCompact, SessionEnd and the git hooks fire rarely by design, and
    `writ-cwd-changed` went a full window with none simply because the working directory
    did not change.
    """
    name = "hook-telemetry-coverage"
    registered = _registered_hook_scripts()
    if not registered:
        return _warn(
            name=name,
            detail=(
                f"Could not read {_PACKAGE_ROOT / DEFAULT_HOOKS_MANIFEST}, so hook "
                "telemetry coverage is unknown."
            ),
        )

    broken: list[str] = []
    for hook, path in sorted(registered.items()):
        if "/hooks/scripts/" not in path.as_posix():
            broken.append(f"{hook} (registered outside hooks/scripts/)")
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            broken.append(f"{hook} (script unreadable at {path})")
            continue
        if "common.sh" not in body:
            broken.append(f"{hook} (does not source common.sh)")
    if broken:
        return _fail(
            name=name,
            detail=(
                "These registered hooks cannot record that they ran, so nothing can tell "
                "a dead hook from a quiet one: " + "; ".join(broken) + ". A hook must live "
                "under hooks/scripts/ and source bin/lib/common.sh, which installs the "
                "telemetry trap."
            ),
        )

    observed = _observed_hook_names()
    if not observed:
        # NOT "every hook is dead". An empty set means the metrics stream could not be
        # read at all: a fresh install, a redirected WRIT_FRICTION_LOG, a pruned log dir.
        # Listing all 40 hooks here would be the loudest possible false alarm, and a check
        # that cannot tell no-log from no-runs must not accuse either way.
        return _ok(
            name=name,
            detail=(
                f"All {len(registered)} registered hooks are instrumented. No metrics "
                "stream was readable, so per-hook execution could not be observed."
            ),
        )
    silent = sorted(hook for hook in registered if hook not in observed)
    if silent:
        return _warn(
            name=name,
            detail=(
                f"All {len(registered)} registered hooks are instrumented, but these have "
                "no hook_execution row in the metrics retention window: "
                + ", ".join(silent)
                + ". Rare triggers (compaction, session end, cwd change, git hooks) "
                "explain silence legitimately; a hook you expect on every turn does not."
            ),
        )
    return _ok(
        name=name,
        detail=(
            f"All {len(registered)} registered hooks are instrumented and have recorded "
            "at least one execution."
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


# The positive control the log cannot be, named in every detail this check emits. A quiet
# gate and a dead gate produce identical rows, so the absence of refusals has a ready
# innocent explanation, which is exactly when a metric needs a positive signal instead.
# The fire drill triggers each refusal for real.
_FIRE_DRILL = "tests/firedrill/"


def check_gate_refusal_liveness(opts: DoctorOptions) -> CheckResult:
    """Which gates can refuse, and which of those ever have.

    Nothing in this file read a `gate_decision` row before it, so a gate that stopped
    refusing, or never started, alarmed nowhere.

    THE WHOLE CORPUS IS READ AND THE ALARM IS "HAS NEVER REFUSED", not "has not refused
    lately". No window constant is introduced, and the corpus is why: several observed
    gates have fewer than ten decisions in total, so any window short enough to notice
    inertness reports them as inert every time nothing dangerous was attempted. For a
    refusal gate, "no refusals this month" is the expected healthy state. Each refusing
    gate's last refusal date is printed instead, so a long-inert gate is VISIBLE and the
    reader rules.
    """
    name = "gate-refusal-liveness"
    capability = _gate_deny_capability()
    if capability is None:
        return _warn(
            name=name,
            detail=(
                f"Hook sources could not be read at {_HOOK_SCRIPTS_DIR}, so which gates "
                "can refuse is unknown: refusal capability is underived and no gate can "
                "be classified. Nothing here says a gate is healthy or broken."
            ),
        )
    if not capability:
        return _warn(
            name=name,
            detail=(
                f"No gate call site was derived from {_HOOK_SCRIPTS_DIR}, so refusal "
                "capability is unknown. 'No gate is in the alarm class' is true of an "
                "empty derivation and says nothing about this tree."
            ),
        )

    rows = _audit_rows("gate_decision")
    classes = _gate_refusal_classes(capability, rows or [])
    last_refusal: dict[str, str] = {}
    for row in rows or []:
        gate = str(row.get("gate") or "")
        if not gate or str(row.get("decision") or "") not in REFUSING_DECISIONS:
            continue
        date = _decision_date(row)
        if date and date > last_refusal.get(gate, ""):
            last_refusal[gate] = date

    def _named(gate: str) -> str:
        scripts = (capability.get(gate) or {}).get("scripts") or []
        suffix = f" [{', '.join(scripts)}]" if scripts else ""
        if gate in last_refusal:
            return f"{gate} (last refusal {last_refusal[gate]}){suffix}"
        return f"{gate}{suffix}"

    def _members(wanted: str) -> list[str]:
        return [_named(gate) for gate in sorted(classes) if classes[gate] == wanted]

    alarming = _members(_CLASS_NEVER_REFUSED)
    sections = [
        (_CLASS_NEVER_REFUSED, "can refuse and never has", alarming),
        (_CLASS_REFUSING, "can refuse and has", _members(_CLASS_REFUSING)),
        (_CLASS_UNEXERCISED, "can refuse, no decision recorded",
         _members(_CLASS_UNEXERCISED)),
        (_CLASS_CANNOT_REFUSE, "no refusing token at any call site",
         _members(_CLASS_CANNOT_REFUSE)),
        (_CLASS_ORPHANED, "rows recorded, no call site anywhere",
         _members(_CLASS_ORPHANED)),
    ]
    body = "; ".join(
        f"{label} ({gloss}): {', '.join(members)}"
        for label, gloss, members in sections if members
    )
    if rows is None:
        preface = (
            "No audit stream was readable, so gate behaviour is UNMEASURED and every "
            "gate below is classified from its sources alone. "
        )
    elif not rows:
        preface = (
            "The audit stream was read and holds no gate_decision row yet, so none is "
            "recorded for any gate. "
        )
    else:
        preface = ""
    closing = (
        f" The log cannot tell a quiet gate from a dead one; {_FIRE_DRILL} is the "
        "positive control, triggering each refusal for real."
    )
    detail = preface + body + "." + closing
    if alarming:
        return _warn(name=name, detail=detail)
    return _ok(name=name, detail=detail)


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
    ("duplicate-records", check_duplicate_records),
    ("index-degeneracy", check_index_degeneracy),
    ("permissions-allowlist", check_permissions_allowlist),
    ("daemon-socket", check_daemon_socket),
    ("embedding-stack", check_embedding_stack),
    ("corpus-drift", check_corpus_drift),
    ("bitbucket-creds", check_bitbucket_creds),
    ("git-post-commit-hook", check_git_post_commit_hook),
    ("writ-path-symlink", check_writ_path_symlink),
    ("cc-hook-registration", check_cc_hook_registration),
    ("duplicate-hook-registration", check_duplicate_hook_registration),
    ("hook-telemetry-coverage", check_hook_telemetry_coverage),
    ("stranded-telemetry-buffer", check_stranded_telemetry_buffer),
    ("subagent-role-coverage", check_subagent_role_coverage),
    ("subagent-role-scope-coverage", check_subagent_role_scope_coverage),
    ("subagent-governance-census", check_subagent_governance_census),
    ("role-symlinks", check_role_symlinks),
    ("mode-gate-sanity", check_mode_gate_sanity),
    ("gate-refusal-liveness", check_gate_refusal_liveness),
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
