"""Plan dfacff61-23d5-474e-846c-2e2f0f0ea482: the two write doors and the
worktree gate stop discarding a decision above the platform's single-argument
limit, and a decider that faults ASKS instead of allowing silently.

RED until the implementation phase lands the three transport changes named in
plan.md's ## Files: `writ-bash-write-gate.sh` (command on a `mktemp` file, a
completion sentinel, a three-way consumer), `writ-pre-write-dispatch.sh`
(`RESULT`/`CHECK_BODY` on the translator's stdin, an observed-outcome branch
replacing `DECISION="${DECISION:-allow}"`), `writ-worktree-safety.sh` (same
transport and sentinel as the Bash gate), and `bin/lib/common.sh`
(`emit_deny`/`emit_ask` read the reason from stdin, `_gd_emit_now` truncates
`reason`/`target`, a `gate_decider_incomplete` fault helper).

Every capability drives the REAL hook as a subprocess with a real PreToolUse
envelope, in an isolated `WRIT_CACHE_DIR`/`WRIT_FRICTION_LOG`/project tree, the
shape `tests/test_worktree_safety_extractor.py::_run` and
`tests/test_bash_write_gate.py::_run_hook` already use. Sizes are DERIVED as
`32 * os.sysconf("SC_PAGE_SIZE")`, never the literal 131072, and the platform
actually enforcing that cap is proved rather than assumed
(`tests/test_subagent_seed.py::TestOversizeProbeIsReal`,
`require_platform_arg_limit`; the same idiom is reproduced here, not shared as
a live import, so this module's own probe cannot be silently disarmed by an
edit to a different test file's fixture).

REUSED, NOT DUPLICATED: `_run_hook`, `_seed`, `SKILL_ROOT`, `HOOK_SH` and the
`sandbox_cwd` autouse fixture from `tests.test_bash_write_gate`; `_extract`
(the env-transport extractor harness) from the same module, imported
UNMODIFIED and used here as the "through env" side of the byte-fidelity
capability, never as a stand-in for the real hook; `StubDaemon` from
`tests._stub_daemon` for the two write-door capabilities that need a
controlled `/pre-write-check` response (an allowed write above the cap, and
the translator-fault ask) rather than the daemon-down local fallback every
other capability here drives through.

Nothing destructive executes and no test creates or reads a real credential
file: `.env`, `id_rsa`-shaped and similar paths are STRINGS the classifier
matches on, never opened.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests.test_bash_write_gate import (
    HOOK_SH as BASH_HOOK_SH,
    SKILL_ROOT,
    _extract,
    _seed,
)
from tests._stub_daemon import StubDaemon

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"
COMMON_SH = REPO / "bin" / "lib" / "common.sh"
BASH_HOOK = HOOKS / "writ-bash-write-gate.sh"
WORKTREE_HOOK = HOOKS / "writ-worktree-safety.sh"
WRITE_HOOK = HOOKS / "writ-pre-write-dispatch.sh"
SESSION_HELPER = REPO / "bin" / "lib" / "writ-session.py"

assert str(BASH_HOOK) == BASH_HOOK_SH, "SKILL_ROOT-relative path disagrees with the module constant"

# The derived platform limit, the same idiom `tests/test_subagent_seed.py:70` uses:
# never the literal 131072, so this pins the real defect on a kernel whose page size
# differs rather than only on this one.
_MAX_ARG_STRLEN = 32 * os.sysconf("SC_PAGE_SIZE")
_OVERSIZE_MARGIN = 4096
OVERSIZE_PAD_BYTES = _MAX_ARG_STRLEN + _OVERSIZE_MARGIN


def _probe_arg_limit_enforced(pad_bytes: int) -> bool:
    """Execs a real subprocess with one argv of `pad_bytes`, and reports whether the
    platform actually raises OSError(E2BIG). Never assumed, matching
    `tests/test_subagent_seed.py::_probe_arg_limit_enforced`: a threshold that is only
    computed and never checked against a real exec could be wrong on a kernel whose cap
    is not `32 * SC_PAGE_SIZE`."""
    try:
        subprocess.run(["true", "x" * pad_bytes], capture_output=True, timeout=30)
    except OSError as exc:
        return exc.errno == errno.E2BIG
    return False


def _require_oversized_arg_support(pad_bytes: int = OVERSIZE_PAD_BYTES, *,
                                    probe=_probe_arg_limit_enforced) -> int:
    """Return `pad_bytes` after PROVING the platform enforces the limit at that size, or
    skip with a stated reason naming the byte count. `probe` is injectable so capability
    17's negative case (a platform that accepts the oversized argument) is exercised
    directly in `TestOversizeProbeIsReal`, rather than waited for on a machine that has
    it."""
    if not probe(pad_bytes):
        pytest.skip(
            f"platform accepted a {pad_bytes}-byte argv[1] without raising E2BIG; "
            "the derived MAX_ARG_STRLEN probe (32 * SC_PAGE_SIZE) does not hold here, "
            "so the oversized-argument regression cannot be reproduced"
        )
    return pad_bytes


@pytest.fixture(scope="module")
def require_platform_arg_limit() -> int:
    """The derived oversize byte count, after confirming this platform actually
    enforces MAX_ARG_STRLEN at that size. Module-scoped: the probe execs a real
    subprocess, and every oversized test in this module wants the same confirmed
    number, not a fresh probe each time."""
    return _require_oversized_arg_support()


# --------------------------------------------------------------------------- #
# Generic hook-running plumbing, shared by all three doors.
# --------------------------------------------------------------------------- #

def _sid(prefix: str = "exb") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _base_env(cache: Path, friction: Path, extra: dict | None = None) -> dict:
    env = {
        **os.environ,
        "WRIT_CACHE_DIR": str(cache),
        "WRIT_FRICTION_LOG": str(friction),
        "WRIT_PORT": "59999",
        "WRIT_NO_AUTOSTART": "1",
        "WRIT_DIR": str(REPO),
        "SKILL_DIR": str(REPO),
    }
    if extra:
        env.update(extra)
    return env


def _run_script(script: Path, envelope: dict, *, cwd: Path, cache: Path, friction: Path,
                 extra_env: dict | None = None,
                 input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    cache.mkdir(parents=True, exist_ok=True)
    env = _base_env(cache, friction, extra_env)
    data = input_bytes if input_bytes is not None else json.dumps(envelope).encode("utf-8")
    return subprocess.run(["bash", str(script)], input=data, capture_output=True,
                           cwd=str(cwd), env=env, timeout=120)


def _hso(proc: subprocess.CompletedProcess) -> dict | None:
    out = proc.stdout
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    out = out.strip()
    if not out:
        return None
    try:
        return json.loads(out).get("hookSpecificOutput")
    except (ValueError, KeyError):
        return None


def _stderr(proc: subprocess.CompletedProcess) -> str:
    err = proc.stderr
    return err.decode("utf-8", errors="replace") if isinstance(err, bytes) else err


def _friction_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _padded_credential_bash_cmd(total_bytes: int, target: str = ".env") -> str:
    """A Bash command that redirects to a credential path, padded with an inert
    trailing no-op segment (`: '...'`) so the OVERALL command reaches `total_bytes`
    without changing what the extractor resolves as the write target."""
    base = f"echo SECRETVALUE > {target}"
    if total_bytes <= len(base):
        return base
    pad = max(total_bytes - len(base) - len("; : ''"), 0)
    return base + "; : '" + ("a" * pad) + "'"


def _padded_worktree_cmd(total_bytes: int, target: str = "scratch/feature-x",
                          branch: str = "feature-x") -> str:
    base = f"git worktree add {target} {branch}"
    if total_bytes <= len(base):
        return base
    pad = max(total_bytes - len(base) - len("; : ''"), 0)
    return base + "; : '" + ("a" * pad) + "'"


def _path_without_python3(bindir: Path) -> str:
    """A PATH resolving every executable the ambient PATH resolves, MINUS `python3`
    and `python3.*` themselves: precise exclusion of the one binary, not the directory
    it lives in. Measured on this machine: `jq` lives beside `python3` in the same
    `/usr/bin`, so excluding the whole directory silently removed `jq` too, which made
    the interpreter-absent fixture indistinguishable from a jq-absent one."""
    bindir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isdir(d):
            continue
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for name in entries:
            if name == "python3" or name.startswith("python3."):
                continue
            if name in seen:
                continue
            src = os.path.join(d, name)
            if not os.path.isfile(src) or not os.access(src, os.X_OK):
                continue
            try:
                os.symlink(src, str(bindir / name))
                seen.add(name)
            except OSError:
                continue
    return str(bindir)


def _jq_reachable(path_value: str) -> bool:
    return subprocess.run(["bash", "-c", "command -v jq"], env={"PATH": path_value},
                           capture_output=True).returncode == 0


def _sh_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


# The distinctive python-source tokens the PATH shims key on. Each is asserted to
# exist in the real hook source by its own test, so a rename reddens the guard
# instead of silently disarming it (the plan's own stated requirement for
# capability 4).
BASH_EXTRACTOR_MARKER = "from writ.session.gates import _is_credential_path as is_cred"
TRANSLATOR_MARKER = "max_denial_count"


def _crashing_python3_shim(bindir: Path, marker: str) -> str:
    """A fake `python3` on PATH that exits 1 ONLY for the invocation whose program text
    (heredoc stdin, or a `-c` argument) carries `marker`; every other python3 call --
    including `emit_ask`/`emit_deny`'s own heredocs -- is forwarded, byte for byte, to
    the REAL interpreter, so a fault in one decision block cannot silence the ask that
    is supposed to report it. Returns the PATH value with `bindir` prepended."""
    bindir.mkdir(parents=True, exist_ok=True)
    real = shutil.which("python3")
    assert real, "no real python3 on PATH to build the shim against"
    shim = bindir / "python3"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"REAL={_sh_single_quote(real)}\n"
        f"MARKER={_sh_single_quote(marker)}\n"
        'if [ "$#" -eq 0 ]; then\n'
        '    PROG="$(cat)"\n'
        '    case "$PROG" in\n'
        '        *"$MARKER"*) exit 1 ;;\n'
        "    esac\n"
        '    printf %s "$PROG" | exec "$REAL"\n'
        "else\n"
        '    for a in "$@"; do\n'
        '        case "$a" in\n'
        '            *"$MARKER"*) exit 1 ;;\n'
        "        esac\n"
        "    done\n"
        '    exec "$REAL" "$@"\n'
        "fi\n"
    )
    shim.chmod(0o755)
    return f"{bindir}:{os.environ.get('PATH', '')}"


def _recording_python3_shim(bindir: Path, report: Path) -> str:
    """A fake `python3` that never crashes: it appends one line to `report` naming
    whether `WRIT_BASH_CMD_FILE` (or `WRIT_WT_CMD_FILE`) was set and, when set,
    whether that path exists and its octal mode, THEN execs the real interpreter
    unchanged. Used to observe the command-file transport from outside the hook
    without adding a test hook to production code."""
    bindir.mkdir(parents=True, exist_ok=True)
    real = shutil.which("python3")
    assert real, "no real python3 on PATH to build the shim against"
    shim = bindir / "python3"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"REAL={_sh_single_quote(real)}\n"
        f"REPORT={_sh_single_quote(str(report))}\n"
        'for VAR in WRIT_BASH_CMD_FILE WRIT_WT_CMD_FILE; do\n'
        '    eval "VAL=\\${$VAR:-}"\n'
        '    if [ -n "$VAL" ]; then\n'
        '        if [ -e "$VAL" ]; then\n'
        '            MODE=$(stat -c "%a" "$VAL" 2>/dev/null || echo unknown)\n'
        '            printf "%s path=%s exists=1 mode=%s\\n" "$VAR" "$VAL" "$MODE" >> "$REPORT"\n'
        '        else\n'
        '            printf "%s path=%s exists=0\\n" "$VAR" "$VAL" >> "$REPORT"\n'
        '        fi\n'
        "    fi\n"
        "done\n"
        'if [ "$#" -eq 0 ]; then\n'
        '    PROG="$(cat)"\n'
        '    printf %s "$PROG" | exec "$REAL"\n'
        "else\n"
        '    exec "$REAL" "$@"\n'
        "fi\n"
    )
    shim.chmod(0o755)
    return f"{bindir}:{os.environ.get('PATH', '')}"


def _seed_work_mode(cache: Path, sid: str) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(SESSION_HELPER), "mode", "set", "work", sid],
                    env={**os.environ, "WRIT_CACHE_DIR": str(cache)},
                    check=True, capture_output=True)


def _seed_in_cache(cache: Path, sid: str, **fields) -> None:
    """`tests.test_bash_write_gate._seed`, but pointed at an ISOLATED cache dir
    rather than whatever `WRIT_CACHE_DIR` the ambient environment carries.
    `_seed` reads `WRIT_CACHE_DIR` from `os.environ` at call time (it goes
    through `writ.session.cache`, which is not given a directory argument), so
    this pins the env var for the one call and restores it, rather than
    leaving a test's cache dir bleeding into a later test in the same
    process."""
    cache.mkdir(parents=True, exist_ok=True)
    old = os.environ.get("WRIT_CACHE_DIR")
    os.environ["WRIT_CACHE_DIR"] = str(cache)
    try:
        _seed(sid, **fields)
    finally:
        if old is None:
            os.environ.pop("WRIT_CACHE_DIR", None)
        else:
            os.environ["WRIT_CACHE_DIR"] = old


def _gitignored_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True, capture_output=True)
    (proj / ".gitignore").write_text(".worktrees/\n")
    return proj


def _write_envelope(sid: str, file_path: str, content: str) -> dict:
    return {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": "Write",
            "tool_input": {"file_path": file_path, "content": content}}


def _bash_envelope(sid: str, command: str, cwd: str | None = None) -> dict:
    env = {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": "Bash",
           "tool_input": {"command": command}}
    if cwd is not None:
        env["cwd"] = cwd
    return env


# --------------------------------------------------------------------------- #
# Capability 1: the Bash gate's credential arm, oversized command.
# --------------------------------------------------------------------------- #

class TestBashGateCredentialOversized:
    def test_oversized_credential_write_denies(self, tmp_path: Path,
                                                require_platform_arg_limit) -> None:
        """Reddened by restoring `WRIT_BASH_CMD="$CMD"` on the extractor invocation:
        today the command crosses as an env string and dies at execve with E2BIG
        before python starts, so `2>/dev/null || true` swallows it, TARGETS is empty,
        and the credential write allows silently."""
        cmd = _padded_credential_bash_cmd(require_platform_arg_limit)
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl")
        assert proc.returncode == 0, _stderr(proc)
        hso = _hso(proc)
        assert hso is not None, f"the oversized credential write was silent: {proc.stdout!r}"
        assert hso.get("permissionDecision") == "deny", hso
        assert "SEC-CREDENTIAL-WRITE" in hso.get("permissionDecisionReason", ""), hso


# --------------------------------------------------------------------------- #
# Capability 2: below the cap, unchanged in both directions.
# --------------------------------------------------------------------------- #

class TestBashGateSizeBoundaryUnchanged:
    def test_100_bytes_still_denies(self, tmp_path: Path) -> None:
        """Reddened by removing the sentinel print, which turns every command into a
        fault and every allow into an ask -- this small credential write must keep
        denying exactly as it does on HEAD."""
        cmd = _padded_credential_bash_cmd(100)
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl")
        hso = _hso(proc)
        assert hso is not None and hso.get("permissionDecision") == "deny", hso
        assert "SEC-CREDENTIAL-WRITE" in hso.get("permissionDecisionReason", ""), hso

    def test_half_the_derived_cap_still_denies(self, tmp_path: Path,
                                                require_platform_arg_limit) -> None:
        cmd = _padded_credential_bash_cmd(require_platform_arg_limit // 2)
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl")
        hso = _hso(proc)
        assert hso is not None and hso.get("permissionDecision") == "deny", hso
        assert "SEC-CREDENTIAL-WRITE" in hso.get("permissionDecisionReason", ""), hso

    def test_ordinary_write_matches_head_behavior(self, tmp_path: Path) -> None:
        """The regression guard for the whole cycle: an ordinary in-project write in
        work mode with no gates approved must keep producing the SAME verdict and
        stdout it produces on HEAD (an ENF-GATE-PLAN deny through the daemon-down
        local fallback, no size involved at all). Reddened by any change to the
        consumer that alters this unrelated path -- for example, treating the
        sentinel's absence on a HEAD-shaped run as a fault."""
        sid = _sid()
        cache = tmp_path / "cache"
        (tmp_path / "src").mkdir(exist_ok=True)
        _seed_in_cache(cache, sid, mode="work", gates_approved=[], current_phase=None)
        proc = _run_script(BASH_HOOK, _bash_envelope(sid, "echo x > src/foo.py"),
                            cwd=tmp_path, cache=cache, friction=tmp_path / "friction.jsonl")
        out = _hso(proc)
        assert out is not None and out.get("permissionDecision") == "deny", out
        assert "ENF-GATE-PLAN" in out.get("permissionDecisionReason", ""), out


# --------------------------------------------------------------------------- #
# Capability 3: a failed mktemp is a FAULT, not an empty command.
# --------------------------------------------------------------------------- #

class TestBashGateUnwritableTmpdir:
    def test_unwritable_tmpdir_asks_not_allows(self, tmp_path: Path) -> None:
        """Reddened by treating a failed `mktemp` as an empty command instead of a
        fault: today TMPDIR is never consulted (the command still crosses as an env
        string), so this credential write denies normally instead of asking, which is
        itself the proof that nothing yet reacts to an unwritable TMPDIR."""
        ro = tmp_path / "readonly-tmp"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            cmd = _padded_credential_bash_cmd(200)
            proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                                cwd=tmp_path, cache=tmp_path / "cache",
                                friction=tmp_path / "friction.jsonl",
                                extra_env={"TMPDIR": str(ro)})
        finally:
            ro.chmod(0o700)
        assert proc.returncode == 0, _stderr(proc)
        hso = _hso(proc)
        assert hso is not None, "an unwritable TMPDIR must not allow silently"
        assert hso.get("permissionDecision") == "ask", hso
        assert hso.get("permissionDecision") != "allow"


# --------------------------------------------------------------------------- #
# Capability 4: a PATH shim that fails only the extractor program.
# --------------------------------------------------------------------------- #

class TestBashGateInterpreterFault:
    def test_the_shims_target_token_exists_in_the_extractor(self) -> None:
        """The token the shim in the next test keys on must still be real source, or
        the shim disarms itself silently instead of proving anything."""
        assert BASH_EXTRACTOR_MARKER in BASH_HOOK.read_text(encoding="utf-8")

    def test_shimmed_extractor_crash_asks_instead_of_going_silent(
        self, tmp_path: Path
    ) -> None:
        """Reddened by restoring `|| true` plus `[ -z "$TARGETS" ] && exit 0` on the
        consumer: today a crashed extractor produces empty TARGETS, which is the same
        shape as "ran and found nothing", so this credential write allows silently
        instead of asking."""
        shim_path = _crashing_python3_shim(tmp_path / "shim", BASH_EXTRACTOR_MARKER)
        cmd = _padded_credential_bash_cmd(200)
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl",
                            extra_env={"PATH": shim_path})
        assert proc.returncode == 0, _stderr(proc)
        hso = _hso(proc)
        assert hso is not None, "a crashed extractor must not allow silently"
        assert hso.get("permissionDecision") == "ask", hso


# --------------------------------------------------------------------------- #
# Capability 5: no consumer arm ever sees the sentinel row.
# --------------------------------------------------------------------------- #

class TestBashGateSentinelNeverLeaksIntoAnArm:
    """Each sub-test drives a DIFFERENT consumer arm with rows present, so a sentinel
    line left in TARGETS (instead of stripped before the arms run) would corrupt a
    different arm each time -- most visibly the unknown-target arm, which would ask
    on a command with no unresolved target at all. Every arm here already behaves
    correctly on HEAD (there is no sentinel yet to leak), so these are regression
    pins: reddened only by a FUTURE implementation that leaves the sentinel line in
    place."""

    def test_credential_arm_is_unaffected(self, tmp_path: Path) -> None:
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), "echo SECRET > .env"),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl")
        hso = _hso(proc)
        assert hso is not None and hso.get("permissionDecision") == "deny"
        assert "SEC-CREDENTIAL-WRITE" in hso.get("permissionDecisionReason", "")

    def test_gate_state_arm_is_unaffected(self, tmp_path: Path) -> None:
        cmd = f"bash {SKILL_ROOT}/hooks/scripts/writ-manual-test-grant.sh"
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl")
        hso = _hso(proc)
        assert hso is not None and hso.get("permissionDecision") == "deny"
        assert "ENF-GATE-STATE" in hso.get("permissionDecisionReason", "")

    def test_unknown_target_arm_is_unaffected(self, tmp_path: Path) -> None:
        """THE arm a leaked sentinel row corrupts first: a stray `status\tcomplete`
        row would be read as an unresolved target, and this command has none."""
        proc = _run_script(BASH_HOOK,
                            _bash_envelope(_sid(), "cp README.md $NOPE_UNSET_XYZ/probe.txt"),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl")
        hso = _hso(proc)
        assert hso is not None and hso.get("permissionDecision") == "ask", hso
        assert "NOPE_UNSET_XYZ" in hso.get("permissionDecisionReason", ""), hso

    def test_egress_arm_is_unaffected(self, tmp_path: Path) -> None:
        proc = _run_script(
            BASH_HOOK,
            _bash_envelope(_sid(), "scp ./notes.md user@remote.example.com:/tmp/"),
            cwd=tmp_path, cache=tmp_path / "cache", friction=tmp_path / "friction.jsonl")
        hso = _hso(proc)
        assert hso is not None and hso.get("permissionDecision") == "ask", hso
        assert "remote.example.com" in hso.get("permissionDecisionReason", ""), hso

    def test_can_write_arm_is_unaffected(self, tmp_path: Path) -> None:
        sid = _sid()
        cache = tmp_path / "cache"
        (tmp_path / "src").mkdir(exist_ok=True)
        _seed_in_cache(cache, sid, mode="work", gates_approved=[], current_phase=None)
        proc = _run_script(BASH_HOOK, _bash_envelope(sid, "echo x > src/foo.py"),
                            cwd=tmp_path, cache=cache, friction=tmp_path / "friction.jsonl")
        out = _hso(proc)
        assert out is not None and out.get("permissionDecision") == "deny"
        assert "ENF-GATE-PLAN" in out.get("permissionDecisionReason", "")


# --------------------------------------------------------------------------- #
# Capability 6: the deliberate unbalanced-quotes fail-open stays silent.
# --------------------------------------------------------------------------- #

class TestBashGateUnbalancedQuotesStaySilent:
    def test_unbalanced_quotes_still_allows_silently(self, tmp_path: Path) -> None:
        """Reddened by removing the sentinel print from the `:2753` early-exit branch:
        that would turn every quote-unbalanced command into a fault (an ask), which
        the plan calls a real usability regression. Passes on HEAD already; the
        implementation must not change this specific outcome."""
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), "echo 'unbalanced"),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl")
        assert proc.returncode == 0, _stderr(proc)
        assert _hso(proc) is None, _hso(proc)


# --------------------------------------------------------------------------- #
# Capability 7: the worktree gate, oversized and under the cap.
# --------------------------------------------------------------------------- #

class TestWorktreeGateOversized:
    def test_oversized_command_denies_project_local_target(
        self, tmp_path: Path, require_platform_arg_limit
    ) -> None:
        """Reddened by restoring `WRIT_WT_CMD="$CMD"`: today this command dies at
        execve with E2BIG before python starts, VERDICT is empty, and the worktree
        allows silently."""
        sid = _sid("wt")
        cache = tmp_path / "cache"
        _seed_work_mode(cache, sid)
        proj = _gitignored_project(tmp_path)
        cmd = _padded_worktree_cmd(require_platform_arg_limit)
        proc = _run_script(WORKTREE_HOOK, _bash_envelope(sid, cmd, cwd=str(proj)),
                            cwd=proj, cache=cache, friction=tmp_path / "friction.jsonl")
        hso = _hso(proc)
        assert hso is not None, f"the oversized worktree command was silent: {proc.stdout!r}"
        assert hso.get("permissionDecision") == "deny", hso
        assert "ENF-PROC-WORKTREE-001" in hso.get("permissionDecisionReason", ""), hso

    def test_under_the_cap_deny_direction_is_unchanged(self, tmp_path: Path) -> None:
        sid = _sid("wt")
        cache = tmp_path / "cache"
        _seed_work_mode(cache, sid)
        proj = _gitignored_project(tmp_path)
        cmd = "git worktree add scratch/feature-x feature-x"
        proc = _run_script(WORKTREE_HOOK, _bash_envelope(sid, cmd, cwd=str(proj)),
                            cwd=proj, cache=cache, friction=tmp_path / "friction.jsonl")
        hso = _hso(proc)
        assert hso is not None and hso.get("permissionDecision") == "deny", hso
        assert "ENF-PROC-WORKTREE-001" in hso.get("permissionDecisionReason", ""), hso

    def test_under_the_cap_gitignored_allow_is_unchanged(self, tmp_path: Path) -> None:
        sid = _sid("wt")
        cache = tmp_path / "cache"
        _seed_work_mode(cache, sid)
        proj = _gitignored_project(tmp_path)
        cmd = "git worktree add .worktrees/feature-z feature-z"
        proc = _run_script(WORKTREE_HOOK, _bash_envelope(sid, cmd, cwd=str(proj)),
                            cwd=proj, cache=cache, friction=tmp_path / "friction.jsonl")
        assert _hso(proc) is None, _hso(proc)


# --------------------------------------------------------------------------- #
# Capability 8: the Write/Edit door, oversized and under the cap.
#
# The third door's own oversized/control pair used to live here as
# `TestMemoryPolicyGuardOversizedStillBypasses`, `xfail(strict=True)`. Plan
# 2412ba38-51e1-4b73-895b-7b240a3c21d3 fixes that door (the nested-program
# restructure the class's own docstring said was its own cycle), so the class
# is DELETED rather than flipped to a plain pass: its name asserted a bypass
# that stops being true, and a surviving `strict=True` xfail would XPASS and
# FAIL the moment the fix lands, which is pytest enforcing the deletion by
# itself. Its two tests are RE-HOMED, not weakened, in
# `tests/test_memory_policy_scan_transport.py::TestOversizedRuleWeakeningMemoryWriteRefused`
# and `::TestControlSizeStillDenies`, where the oversized case is re-expressed
# against BOTH the derived byte count and the plan's own literal 200,000, and
# is deliberately NOT gated behind `require_platform_arg_limit` (plan.md
# Decision 4: the fixed guard's verdict must be size-independent regardless of
# whether this kernel enforces E2BIG).
# --------------------------------------------------------------------------- #

class TestWriteDoorContentSizeBoundary:
    @pytest.mark.parametrize("nbytes", [100, 120_000])
    def test_under_the_cap_still_denies(self, tmp_path: Path, nbytes: int) -> None:
        proc = _run_script(WRITE_HOOK,
                            _write_envelope(_sid(), ".env", "A" * nbytes),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl")
        hso = _hso(proc)
        assert hso is not None and hso.get("permissionDecision") == "deny", (nbytes, hso)
        assert "SEC-CREDENTIAL-WRITE" in hso.get("permissionDecisionReason", ""), hso

    @pytest.mark.parametrize("nbytes", [131_000, 140_000, 300_000])
    def test_oversized_content_denies(self, tmp_path: Path, nbytes: int) -> None:
        """Reddened by restoring `"$RESULT" "$CHECK_BODY"` as argv on the translator,
        or by swapping the two NUL records so the decision is read out of the body:
        today the argv carrying `CHECK_BODY` (which embeds the write's content) dies
        at execve with E2BIG, DISPATCH_BLOB is empty, `DECISION="${DECISION:-allow}"`
        substitutes "allow", and this credential-path write allows silently."""
        proc = _run_script(WRITE_HOOK,
                            _write_envelope(_sid(), ".env", "A" * nbytes),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl")
        assert proc.returncode == 0, _stderr(proc)
        hso = _hso(proc)
        assert hso is not None, (
            f"a {nbytes}-byte credential write allowed silently: {proc.stdout!r}"
        )
        assert hso.get("permissionDecision") == "deny", (nbytes, hso)
        assert "SEC-CREDENTIAL-WRITE" in hso.get("permissionDecisionReason", ""), hso


# --------------------------------------------------------------------------- #
# Capability 9: an ALLOWED write above the cap keeps its file-context rules.
# --------------------------------------------------------------------------- #

class TestWriteDoorAllowAboveCapKeepsContext:
    def test_allow_above_cap_still_injects_additional_context(self, tmp_path: Path) -> None:
        """Reddened by asking unconditionally on a non-empty RESULT: the fix must not
        convert the allow path into an ask. Drives a real daemon (a loopback stub) so
        `/pre-write-check` answers `allow` WITH `rag_rules`, because without that a
        no-daemon allow emits nothing on stdout at all and this capability could not
        be distinguished from a silent failure."""
        sid = _sid()
        with StubDaemon(pre_write_check={
            "decision": "allow", "reason": "", "mode": "work",
            "rag_rules": "[TEST-RULE-001] a rule that must reach the model",
            "rag_meta": {"rule_ids": ["TEST-RULE-001"], "tokens": 12},
        }, always_on={"rules": []}) as stub:
            extra = {"WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(stub.port)}
            proc = _run_script(WRITE_HOOK,
                                _write_envelope(sid, "src/ordinary.py", "A" * 200_000),
                                cwd=tmp_path, cache=tmp_path / "cache",
                                friction=tmp_path / "friction.jsonl",
                                extra_env=extra)
            assert proc.returncode == 0, _stderr(proc)
            assert stub.saw("POST", "/pre-write-check"), stub.describe()
        out = proc.stdout.strip()
        assert out, "an allowed write above the cap produced no stdout at all"
        doc = json.loads(out)
        hso = doc.get("hookSpecificOutput", {})
        assert "permissionDecision" not in hso, (
            f"an allow must not carry a permissionDecision: {hso}"
        )
        context = hso.get("additionalContext", "")
        assert "file-context rules" in context, context
        assert "TEST-RULE-001" in context, context


# --------------------------------------------------------------------------- #
# Capability 10: the translator forced to fail asks, never allows or is silent.
# --------------------------------------------------------------------------- #

class TestWriteDoorTranslatorFault:
    def test_the_shims_target_token_exists_in_the_translator(self) -> None:
        assert TRANSLATOR_MARKER in WRITE_HOOK.read_text(encoding="utf-8")

    def test_shimmed_translator_crash_asks(self, tmp_path: Path) -> None:
        """Reddened by restoring `DECISION="${DECISION:-allow}"`: today a crashed
        DISPATCH_BLOB leaves DECISION empty, the default substitutes "allow", and a
        server-side deny that DID complete is discarded silently."""
        sid = _sid()
        shim_path = _crashing_python3_shim(tmp_path / "shim", TRANSLATOR_MARKER)
        with StubDaemon(pre_write_check={
            "decision": "deny", "reason": "[SEC-CREDENTIAL-WRITE] server-side deny",
            "mode": "work",
        }) as stub:
            extra = {"WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(stub.port),
                      "PATH": shim_path}
            proc = _run_script(WRITE_HOOK, _write_envelope(sid, ".env", "secret"),
                                cwd=tmp_path, cache=tmp_path / "cache",
                                friction=tmp_path / "friction.jsonl",
                                extra_env=extra)
            assert stub.saw("POST", "/pre-write-check"), stub.describe()
        assert proc.returncode == 0, _stderr(proc)
        hso = _hso(proc)
        assert hso is not None, "a crashed translator must not allow silently"
        assert hso.get("permissionDecision") == "ask", hso


# --------------------------------------------------------------------------- #
# Capability 11: no python3 on PATH -- allow and announce, on all three hooks.
# --------------------------------------------------------------------------- #

class TestInterpreterAbsentAllowsAndAnnounces:
    """`load_hook_env` needs jq OR python3; this whole class only exercises the
    "python3 absent" arm, so every test skips with a stated reason when jq is also
    absent here, per the plan's own scope."""

    def _skip_unless_jq(self, path_value: str) -> None:
        if not _jq_reachable(path_value):
            pytest.skip("jq is also absent on this machine's PATH; the "
                        "interpreter-absent path needs one of jq/python3 to parse "
                        "the envelope at all, so this fixture cannot be reproduced here")

    def test_bash_gate_allows_and_announces(self, tmp_path: Path) -> None:
        """Reddened by removing the `command -v python3` probe: today, with no
        interpreter, the extractor's own exec fails silently (`2>/dev/null`) and the
        hook produces neither a decision NOR a `[WRIT CRITICAL]` line."""
        path_value = _path_without_python3(tmp_path / "bin")
        self._skip_unless_jq(path_value)
        cmd = _padded_credential_bash_cmd(200)
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl",
                            extra_env={"PATH": path_value})
        assert proc.returncode == 0, _stderr(proc)
        assert _hso(proc) is None, "an interpreter-less machine must not emit a decision"
        err = _stderr(proc)
        assert err.count("[WRIT CRITICAL]") == 1, err

    def test_worktree_gate_allows_and_announces(self, tmp_path: Path) -> None:
        path_value = _path_without_python3(tmp_path / "bin")
        self._skip_unless_jq(path_value)
        sid = _sid("wt")
        cache = tmp_path / "cache"
        _seed_work_mode(cache, sid)
        proj = _gitignored_project(tmp_path)
        cmd = "git worktree add scratch/feature-x feature-x"
        proc = _run_script(WORKTREE_HOOK, _bash_envelope(sid, cmd, cwd=str(proj)),
                            cwd=proj, cache=cache, friction=tmp_path / "friction.jsonl",
                            extra_env={"PATH": path_value})
        assert proc.returncode == 0, _stderr(proc)
        assert _hso(proc) is None
        err = _stderr(proc)
        assert err.count("[WRIT CRITICAL]") == 1, err

    def test_write_door_allows_and_announces(self, tmp_path: Path) -> None:
        """The write door's reachability differs from the other two (named in the
        plan's Analysis): `_writ_session pre-write-check` is curl-first, so a
        non-empty RESULT with no interpreter present is reachable at all, which is
        why a loopback stub daemon is driven here rather than relying on the
        daemon-down fallback the other two capabilities in this class use."""
        path_value = _path_without_python3(tmp_path / "bin")
        self._skip_unless_jq(path_value)
        sid = _sid()
        with StubDaemon(pre_write_check={
            "decision": "deny", "reason": "[SEC-CREDENTIAL-WRITE] server-side deny",
            "mode": "work",
        }) as stub:
            extra = {"WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(stub.port),
                      "PATH": path_value}
            proc = _run_script(WRITE_HOOK, _write_envelope(sid, ".env", "secret"),
                                cwd=tmp_path, cache=tmp_path / "cache",
                                friction=tmp_path / "friction.jsonl",
                                extra_env=extra)
        assert proc.returncode == 0, _stderr(proc)
        assert _hso(proc) is None
        err = _stderr(proc)
        assert err.count("[WRIT CRITICAL]") == 1, err


# --------------------------------------------------------------------------- #
# Capability 12: the fault record, bounded and correctly classified.
# --------------------------------------------------------------------------- #

class TestGateDeciderIncompleteRecord:
    def test_stream_classification_is_audit(self) -> None:
        """Reddened by leaving the event out of STREAM_MAP: an unregistered event
        defaults to the friction stream, per `writ/shared/logging.py`'s own stated
        default."""
        from writ.shared.logging import stream_for
        assert stream_for("gate_decider_incomplete") == "audit"

    def test_exactly_one_bounded_row_on_a_faulted_bash_gate_run(
        self, tmp_path: Path
    ) -> None:
        """Reddened by adding the reason or the command to the row (a row built from
        the value that killed the block would die exactly where the block did), or by
        leaving the event out of STREAM_MAP. Drives the same crashing shim as
        capability 4's fault, which today writes NO `gate_decider_incomplete` row at
        all, because the event does not exist yet."""
        shim_path = _crashing_python3_shim(tmp_path / "shim", BASH_EXTRACTOR_MARKER)
        friction = tmp_path / "friction.jsonl"
        cmd = _padded_credential_bash_cmd(200)
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                            cwd=tmp_path, cache=tmp_path / "cache", friction=friction,
                            extra_env={"PATH": shim_path})
        assert proc.returncode == 0, _stderr(proc)
        rows = [r for r in _friction_rows(friction) if r.get("event") == "gate_decider_incomplete"]
        assert len(rows) == 1, f"expected exactly one gate_decider_incomplete row: {rows}"
        row = rows[0]
        assert row.get("hook"), row
        assert row.get("stage"), row
        assert set(row) <= {"ts", "session", "mode", "event", "hook", "stage"}, (
            f"the row carries a field beyond the bounded set: {row}"
        )


# --------------------------------------------------------------------------- #
# Capability 13: a 200,000-character deny reason still reaches the model, and
# the matching audit row is truncated.
# --------------------------------------------------------------------------- #

class TestOversizedDenyReasonReachesTheModelTruncatedInTheAudit:
    def test_oversized_reason_still_denies_with_a_truncated_audit_row(
        self, tmp_path: Path
    ) -> None:
        """Reddened by restoring `WRIT_DENY_REASON="$1"` on `emit_deny` (a 200,000-byte
        env string dies at execve, and `_reply` falls back to "", so `emit_hook_reply`
        returns 0 on the empty payload and the deny disappears), or by dropping the
        truncation in `_gd_emit_now` (the same env-string cap would silently drop the
        AUDIT ROW for the denial even if the reply itself got through). The oversized
        value here is the credential FILENAME, which drives site 1's own fix (the
        command-file transport) at the same time: this is a deliberate joint pin, not
        an accident, matching the plan's own framing that the two `common.sh` edits
        are dependencies of site 1 rather than separable cleanup."""
        long_name = "a" * 200_000 + ".env"
        cmd = f"echo x > {long_name}"
        friction = tmp_path / "friction.jsonl"
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                            cwd=tmp_path, cache=tmp_path / "cache", friction=friction)
        assert proc.returncode == 0, _stderr(proc)
        hso = _hso(proc)
        assert hso is not None, f"a 200,000-character credential path allowed silently: {proc.stdout!r}"
        assert hso.get("permissionDecision") == "deny", hso
        reason = hso.get("permissionDecisionReason", "")
        assert "SEC-CREDENTIAL-WRITE" in reason, reason
        rows = [r for r in _friction_rows(friction) if r.get("event") == "gate_decision"
                and r.get("decision") == "deny"]
        assert rows, "no gate_decision deny row was recorded for the oversized reason"
        row = rows[-1]
        assert len(row.get("reason", "")) <= 4096, (
            f"the audit row's reason was not truncated at the named bound: "
            f"{len(row.get('reason', ''))} chars"
        )
        assert len(row.get("target", "")) <= 4096, (
            f"the audit row's target was not truncated at the named bound: "
            f"{len(row.get('target', ''))} chars"
        )


# --------------------------------------------------------------------------- #
# Capability 14: byte fidelity through the (future) file transport.
# --------------------------------------------------------------------------- #

class TestByteFidelityThroughTheFileTransport:
    """Two halves. The first drives the REAL hook, because tabs/newlines/quotes are
    ordinary Unicode text a JSON envelope carries without difficulty, and pins that
    the file-transport hook produces the SAME verdict `_extract` (the unmodified,
    env-based direct harness) produces for the same command. The second is narrower
    and stated as such: a JSON envelope cannot carry a byte that is not valid UTF-8
    at all (every JSON string escape decodes to a valid Unicode scalar), so the
    property under test -- that the file reader added by this cycle decodes with
    `errors="surrogateescape"`, matching how `os.environ` already decodes an
    invalid-UTF-8 env value -- is pinned directly against that round trip rather
    than through the full hook, which cannot receive such a byte through its real
    input channel at all."""

    def test_tabs_newlines_and_quotes_match_the_direct_harness(self, tmp_path: Path) -> None:
        cmd = "echo x\t> .env\n; : 'quote\" and \\'inner\\''"
        direct = _extract(cmd, cwd=str(tmp_path))
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl")
        hso = _hso(proc)
        cred_row = next((row for row in direct if row[0] == "cred"), None)
        if cred_row is not None:
            assert hso is not None and hso.get("permissionDecision") == "deny", (
                f"direct harness saw a credential row {cred_row} but the real hook "
                f"disagreed: {hso}"
            )
        else:
            assert hso is None or hso.get("permissionDecision") != "deny"

    def test_an_invalid_utf8_byte_survives_the_surrogateescape_round_trip(
        self, tmp_path: Path
    ) -> None:
        """The property the new file reader depends on: writing a raw invalid-UTF-8
        byte to a file and reading it back with `errors="surrogateescape"` must
        reproduce the EXACT string `os.environ` already hands the extractor today for
        the same byte. Reddened by a reader that drops `errors="surrogateescape"` (it
        would raise UnicodeDecodeError instead of reproducing the value) -- this is
        deliberately a property test of the transport's byte fidelity in isolation,
        not of the hook, per this class's own docstring."""
        raw = b"echo SECRETVALUE > .env; : '\xff\xfe'"
        via_env = os.fsdecode(raw)
        cmd_file = tmp_path / "cmd-bytes"
        cmd_file.write_bytes(raw)
        via_file = cmd_file.read_text(encoding="utf-8", errors="surrogateescape")
        assert via_file == via_env, (via_file, via_env)
        # And the existing extractor, fed the env-decoded value, still resolves the
        # credential row -- proving the round trip did not corrupt the classification
        # a file-based reader would perform identically.
        rows = _extract(via_env, cwd=str(tmp_path))
        assert any(row[0] == "cred" for row in rows), rows


# --------------------------------------------------------------------------- #
# Capability 15: the command file, private and cleaned up on every outcome.
# --------------------------------------------------------------------------- #

class TestCommandFileLifecycle:
    """Reddened by replacing `mktemp` with a fixed `/tmp/writ-cmd-$$` name, or by
    removing the `writ_on_exit` cleanup registration. Today none of this exists: the
    recording shim never sees `WRIT_BASH_CMD_FILE` set at all, which is itself the
    RED signal -- the mechanism this capability pins has not been built yet."""

    def _run_with_recorder(self, tmp_path: Path, cmd: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        report = tmp_path / "shim-report.txt"
        shim_path = _recording_python3_shim(tmp_path / "shim", report)
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl",
                            extra_env={"PATH": shim_path})
        lines = report.read_text().splitlines() if report.exists() else []
        return proc, lines

    def test_the_command_file_exists_with_a_private_mode_while_python_runs(
        self, tmp_path: Path
    ) -> None:
        proc, lines = self._run_with_recorder(tmp_path, _padded_credential_bash_cmd(200))
        assert proc.returncode == 0, _stderr(proc)
        matches = [ln for ln in lines if ln.startswith("WRIT_BASH_CMD_FILE ")]
        assert matches, (
            "WRIT_BASH_CMD_FILE was never set for the extractor invocation: "
            f"shim report was {lines!r}"
        )
        assert "exists=1" in matches[0], matches[0]
        assert "mode=600" in matches[0], matches[0]

    @pytest.mark.parametrize("cmd,outcome", [
        (_padded_credential_bash_cmd(200), "deny"),
        ("cp README.md $NOPE_UNSET_XYZ/probe.txt", "ask"),
        ("echo x > src/foo.py", "silent-allow"),
    ])
    def test_the_command_file_is_gone_after_the_hook_exits(
        self, tmp_path: Path, cmd: str, outcome: str
    ) -> None:
        report = tmp_path / f"shim-report-{outcome}.txt"
        shim_path = _recording_python3_shim(tmp_path / f"shim-{outcome}", report)
        proc = _run_script(BASH_HOOK, _bash_envelope(_sid(), cmd),
                            cwd=tmp_path, cache=tmp_path / "cache",
                            friction=tmp_path / "friction.jsonl",
                            extra_env={"PATH": shim_path})
        assert proc.returncode == 0, _stderr(proc)
        lines = report.read_text().splitlines() if report.exists() else []
        matches = [ln for ln in lines if ln.startswith("WRIT_BASH_CMD_FILE ")]
        assert matches, f"[{outcome}] WRIT_BASH_CMD_FILE was never set: {lines!r}"
        recorded_path = matches[0].split("path=", 1)[1].split(" ", 1)[0]
        assert not Path(recorded_path).exists(), (
            f"[{outcome}] the command file at {recorded_path} survived the hook exiting"
        )


# --------------------------------------------------------------------------- #
# Capability 17: the oversize probe is real, not merely computed.
# --------------------------------------------------------------------------- #

class TestOversizeProbeIsReal:
    def test_the_derived_threshold_is_actually_enforced_here(self) -> None:
        assert _probe_arg_limit_enforced(OVERSIZE_PAD_BYTES) is True, (
            "the platform accepted an oversized argv[1] without E2BIG; every "
            "oversized test in this module rests on this being true"
        )

    def test_a_platform_that_accepts_the_argument_skips_with_a_stated_reason(self) -> None:
        """Reddened by making `_require_oversized_arg_support` return its pad size
        unconditionally."""
        with pytest.raises(pytest.skip.Exception) as exc_info:
            _require_oversized_arg_support(OVERSIZE_PAD_BYTES, probe=lambda pad: False)
        assert str(OVERSIZE_PAD_BYTES) in str(exc_info.value)
