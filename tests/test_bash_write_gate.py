"""#6: file writes done THROUGH Bash are gated.

Bash can write files (`echo x > src/foo.py`, `tee`, `dd of=`, `cp`/`mv`, `sed -i`)
which bypasses the Write/Edit/NotebookEdit gate stack entirely. Two protections:

  1. CREDENTIAL guard (writ/session/gates.py _is_credential_path + _can_write_check):
     writes to secret paths (.env, *.pem, **/.ssh/**, ...) are denied in EVERY mode,
     for every write vector (Write/Edit/NotebookEdit AND Bash), path-only -- the file
     is never opened (org credential-read ban).
  2. WORK-GATE for Bash (hooks/scripts/writ-bash-write-gate.sh): the redirect/copy
     TARGET path is extracted and fed to the same server gate the Write tool uses,
     so a Bash write to project source is plan-gated exactly like a Write. Targets
     outside the repo are work-gated too as of cycle L (they emit an `outside` row
     and take the same can-write round trip), because the Write door's project
     boundary refuses exactly those paths and skipping them here left that
     boundary enforceable on one of the two write doors; obfuscated writes still
     evade (documented coverage limit, not full coverage).
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
# The two `mode set work` calls below sit behind a daemon-liveness skip, which is why the
# sentinel probe that found the other 26 modules reported this one clean: with no daemon
# listening the tests skipped and never reached the deletion.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
HOOKS_JSON = os.path.join(SKILL_ROOT, "hooks", "hooks.json")
HOOK_SH = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-bash-write-gate.sh")
HELPER = os.path.join(SKILL_ROOT, "bin", "lib", "writ-session.py")


def _imp(name):
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module(name)


def _seed(sid, **fields):
    cache = _imp("writ.session.cache")
    data = cache._read_cache(sid)
    data.update(fields)
    cache._write_cache(sid, data)


def _extractor_src() -> str:
    """Slice the embedded python extractor block out of the hook script."""
    text = Path(HOOK_SH).read_text()
    marker = text.index("<<'PY'")
    start = text.index("\n", marker) + 1
    end = text.index("\nPY\n", start)
    return text[start:end]


_WRAPPERS_TABLE = "WRAPPERS"


def wrapper_names(source: str) -> set[str]:
    """The prefix names the extractor's WRAPPERS table knows, parsed out of the hook's
    own source rather than listed here. Lives in this module (not the new wrapper-prefix
    module or the egress module) because tests/test_bash_egress_gate.py imports FROM this
    module and tests/test_bash_control_operator_split.py imports from the egress module,
    so anything both the new prefix module and the egress ratchet need must sit at the
    base of that chain or the import graph cycles."""
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == _WRAPPERS_TABLE for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("WRAPPERS table not found in the extractor source")


def _named_string_set(source: str, name: str) -> set[str]:
    """The string members of a module-level `NAME = frozenset({...})` in the extractor
    source, parsed rather than restated here. Handles the bare set/list/tuple spellings
    too, so a future reformat of the literal does not silently return nothing: a name
    that cannot be found RAISES, and every caller asserts the result is non-empty."""
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == name for t in node.targets)):
            continue
        val = node.value
        if isinstance(val, ast.Call) and getattr(val.func, "id", "") == "frozenset":
            val = val.args[0] if val.args else None
        if isinstance(val, (ast.Set, ast.List, ast.Tuple)):
            return {e.value for e in val.elts if isinstance(e, ast.Constant)}
    raise AssertionError("%s not found in the extractor source" % name)


def nested_cmd_flags(source: str) -> set[str]:
    """find's command-running flags, from the hook's own NESTED_CMD_FLAGS. Lives in this
    module for the same reason wrapper_names does: this module is the base of the Bash
    test import chain, and the egress ratchet plus the nested-command matrix both need
    it, so anything shared has to sit here or the import graph cycles."""
    return _named_string_set(source, "NESTED_CMD_FLAGS")


def nested_terminator_chars(source: str) -> set[str]:
    """The characters that close a nested-command span, from the hook's own
    NESTED_TERMINATOR_CHARS."""
    return _named_string_set(source, "NESTED_TERMINATOR_CHARS")


# The extractor's COMPLETION SENTINEL, printed as its last line on every path that ran to
# completion (single source in bash: common.sh's WRIT_EXTRACTOR_SENTINEL). It is stripped
# by `run_extractor` below for the same reason the hook strips it before its own consumer
# arms: a row-shaped line that is not a row would be read as one.
EXTRACTOR_SENTINEL = "status\tcomplete"


def run_extractor(cmd: str, cwd: str = "/proj", *, env: dict | None = None,
                   src: str | None = None, timeout: float | None = None):
    """Run the embedded extractor on CMD and return `(proc, lines)`, where `lines` is its
    stdout minus the completion sentinel.

    THE COMMAND GOES ON A FILE, exactly as the hook now hands it over
    (`WRIT_BASH_CMD_FILE`). Every harness in this suite routes through here rather than
    setting its own env var: a harness still feeding `WRIT_BASH_CMD` would pin the
    extractor through a transport production no longer has, which is the "a test that
    MODELS a path is blind to it" failure. The extractor has no default for the variable
    and no try/except around the open, so a harness that forgot it fails loudly instead of
    silently extracting from an empty command.

    `errors="surrogateescape"` on the write mirrors the extractor's own read, so a command
    carrying a byte that is not valid UTF-8 survives the round trip byte for byte.

    `src` runs a MUTATED copy of the extractor (the conditionality proofs); `env` supplies
    a base environment where a test needs one (HOME, OLDPWD, an unset variable).
    """
    base = dict(os.environ if env is None else env)
    fd, cmd_path = tempfile.mkstemp(prefix="writ-test-bashcmd-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(cmd)
        base.update(WRIT_BASH_CMD_FILE=cmd_path, WRIT_CWD=str(cwd))
        proc = subprocess.run(
            [sys.executable, "-c", src if src is not None else _extractor_src()],
            env=base, capture_output=True, text=True, timeout=timeout)
    finally:
        os.unlink(cmd_path)
    lines = [ln for ln in proc.stdout.splitlines() if ln != EXTRACTOR_SENTINEL]
    return proc, lines


def _extract(cmd: str, cwd: str = "/proj") -> set[tuple[str, str]]:
    """Run the extractor on a command; return the set of (kind, path) it emits."""
    _proc, lines = run_extractor(cmd, cwd)
    out = set()
    for line in lines:
        if "\t" in line:
            kind, path = line.split("\t", 1)
            out.add((kind, path))
    return out


def _test_daemon_up() -> bool:
    """Health of the daemon on the SUITE's port (conftest forces WRIT_PORT=8799).
    Checked at run time, not collection: the suite does not auto-start a session
    daemon (a cold one tips perf floors -- see conftest), so the full-hook
    work-gate tests skip unless a daemon is already answering on the test port."""
    try:
        from tests._daemon import _daemon_health
        return _daemon_health() is not None
    except Exception:
        return False


def _run_hook(cmd: str, sid: str, cwd: str) -> dict | None:
    """Invoke the full hook with a synthetic Bash envelope. Returns the parsed
    hookSpecificOutput on a deny, or None when the hook allows (empty stdout)."""
    envelope = json.dumps({"session_id": sid, "tool_name": "Bash",
                           "tool_input": {"command": cmd}})
    p = subprocess.run(["bash", HOOK_SH], input=envelope, cwd=cwd,
                       capture_output=True, text=True)
    out = p.stdout.strip()
    if not out:
        return None
    return json.loads(out).get("hookSpecificOutput", {})


# --------------------------------------------------------------------------- #
# 1. credential path classifier (pure)
# --------------------------------------------------------------------------- #
class TestIsCredentialPath:
    @pytest.mark.parametrize("path", [
        ".env", ".env.local", "config/.env.production", "/srv/app/.env",
        "deploy.pem", "server.key", "cert.p12", "store.jks",
        "id_rsa", "id_ed25519", "credentials", "credentials.json", "credentials.ini",
        "/home/u/.ssh/authorized_keys", "config/secrets/db.yaml",
        ".htpasswd", ".pgpass", ".netrc",
        # case-insensitivity (real on case-preserving filesystems)
        "cert.PEM", "Server.KEY", "ID_RSA", ".ENV", "/X/.SSH/k",
        # dir-segment wins over basename exemptions (allow-list / .pub planted in a secret dir)
        "/home/u/.ssh/backdoor.pub", "secrets/config.pub", "secrets/.env.example",
        "/x/.gnupg/secring.gpg", "home/.kube/config", "etc/secret/token",
        # .pub hiding a private key
        "server.key.pub", "secret.pem.pub",
        # additive patterns
        "production.env", "app.env", "deploy.ppk", "msg.asc", "secret.gpg",
        ".npmrc", ".pypirc", "kubeconfig", ".dockercfg",
    ])
    def test_credential_paths_detected(self, path):
        gates = _imp("writ.session.gates")
        assert gates._is_credential_path(path) is True, path

    @pytest.mark.parametrize("path", [
        "", ".env.example", ".env.sample", ".env.template", ".env.dist",
        "example.env", "sample.env", "template.env",
        "server.pub", "id_rsa.pub", "src/main.py", "README.md",
        "tests/test_env.py", "environment.py", "key_helpers.py",
        "secretsmanager.py",  # 'secrets' as a basename substring, no /secrets/ dir
        # source modules named credentials.* are NOT secrets (the universal-guard false positive)
        "app/credentials.py", "lib/credentials.ts", "credentials.go",
    ])
    def test_non_credential_paths_allowed(self, path):
        gates = _imp("writ.session.gates")
        assert gates._is_credential_path(path) is False, path

    def test_credential_module_not_blocked_on_write_path(self):
        # gates.py runs _is_credential_path first in _can_write_check, so a benign
        # credentials.py edit must NOT be universally denied (was a real false positive).
        gates = _imp("writ.session.gates")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="conversation")
        res = gates._can_write_check(sid, {"tool_input": {"file_path": "/proj/app/credentials.py"}}, SKILL_ROOT)
        assert res["can_write"] is True


# --------------------------------------------------------------------------- #
# 2. credential guard inside _can_write_check (universal: all write vectors)
# --------------------------------------------------------------------------- #
class TestCredentialGuardInWriteCheck:
    def _env(self, path):
        return {"tool_input": {"file_path": path}}

    def test_env_denied_in_conversation_mode(self):
        # conversation has NO write gate -> proves the credential deny is independent.
        gates = _imp("writ.session.gates")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="conversation")
        res = gates._can_write_check(sid, self._env("/proj/.env"), SKILL_ROOT)
        assert res["can_write"] is False
        assert "SEC-CREDENTIAL-WRITE" in (res["reason"] or "")

    def test_credential_deny_beats_skill_dir_exemption(self):
        # A .env under skill_dir would normally be skill_exempt -> allow. The
        # credential guard runs first, so it is still denied.
        gates = _imp("writ.session.gates")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="work")
        res = gates._can_write_check(sid, self._env(os.path.join(SKILL_ROOT, ".env")), SKILL_ROOT)
        assert res["can_write"] is False
        assert "SEC-CREDENTIAL-WRITE" in (res["reason"] or "")

    def test_env_example_not_credential_denied(self):
        gates = _imp("writ.session.gates")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="conversation")
        res = gates._can_write_check(sid, self._env("/proj/.env.example"), SKILL_ROOT)
        assert res["can_write"] is True

    def test_normal_source_allowed_in_conversation(self):
        gates = _imp("writ.session.gates")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="conversation")
        res = gates._can_write_check(sid, self._env("/proj/src/main.py"), SKILL_ROOT)
        assert res["can_write"] is True


# --------------------------------------------------------------------------- #
# 3. redirect/copy target extraction (adversarial Bash parsing)
# --------------------------------------------------------------------------- #
class TestBashExtractor:
    def test_read_only_commands_yield_nothing(self):
        for cmd in ["ls -la", "git status", "python -m pytest tests/",
                    "cat foo.txt", "grep -rn 'pattern' src"]:
            assert _extract(cmd) == set(), cmd

    def test_quoted_redirect_is_not_a_write(self):
        # shlex keeps the '>' inside the quoted token -> no false positive.
        assert _extract("grep -r 'a > b' src") == set()

    def test_dev_null_and_fd_dup_ignored(self):
        assert _extract("echo hi > /dev/null") == set()
        assert _extract("make 2>&1") == set()
        assert _extract("cmd >&2") == set()

    def test_basic_redirect_targets(self):
        assert _extract("echo x > src/foo.py") == {("local", "/proj/src/foo.py")}
        assert _extract("echo x >> src/foo.py") == {("local", "/proj/src/foo.py")}
        assert _extract("echo x >src/foo.py") == {("local", "/proj/src/foo.py")}

    def test_stderr_redirect_to_file_is_a_write(self):
        assert _extract("make build 2> logs/err.log") == {("local", "/proj/logs/err.log")}

    def test_outside_repo_targets_emit_an_outside_row(self):
        # MEASURED after cycle L: an out-of-repo target used to emit NO row, so it
        # reached no gate and left no audit line. It now emits `outside`, which the
        # consumer feeds to the same can-write the Write tool uses. Pre-approval that
        # means an OS-scratch write is refused with [ENF-GATE-PLAN]: consequence C1 in
        # the approved plan, the accepted cost of two-door parity, whose named remedy is
        # a scratch-zone allow arm in gates._check_work_gate, deferred to its own cycle.
        assert _extract("echo x > /tmp/scratch") == {("outside", "/tmp/scratch")}
        assert _extract("cat a | tee -a /tmp/log.txt") == {("outside", "/tmp/log.txt")}

    def test_tee_dd_cp_mv_sed_targets(self):
        assert _extract("cat a | tee src/b.py") == {("local", "/proj/src/b.py")}
        assert _extract("dd if=/dev/zero of=src/big.bin") == {("local", "/proj/src/big.bin")}
        assert _extract("cp /tmp/x.py src/foo.py") == {("local", "/proj/src/foo.py")}
        assert _extract("mv old.py src/new.py") == {("local", "/proj/src/new.py")}
        assert _extract("sed -i s/a/b/ src/foo.py") == {("local", "/proj/src/foo.py")}
        assert _extract("sed -i.bak s/a/b/ README.md") == {("local", "/proj/README.md")}

    def test_credential_targets_flagged_cred(self):
        assert _extract("echo s > .env") == {("cred", ".env")}
        assert _extract("echo s > config/.env.local") == {("cred", "config/.env.local")}
        assert _extract("echo k > deploy.pem") == {("cred", "deploy.pem")}
        assert _extract("echo x > secrets/token.txt") == {("cred", "secrets/token.txt")}

    def test_env_example_is_not_credential(self):
        # .env.example is project-local -> work-gated as a normal file, not cred-denied.
        assert _extract("echo ok > .env.example") == {("local", "/proj/.env.example")}

    def test_segmented_command_extracts_each(self):
        assert _extract("echo done && echo x > src/two.py") == {("local", "/proj/src/two.py")}

    def test_unbalanced_quotes_fail_open(self):
        # shlex raises -> extractor exits cleanly with no targets (no false deny).
        assert _extract('echo "unterminated > src/foo.py') == set()

    # --- false-positive fixes confirmed by the adversarial review ---
    def test_quoted_redirect_char_is_not_a_write(self):
        # `grep '>' file` -- the quoted '>' is an argument, not a redirect operator.
        assert _extract("grep '>' app.pem") == set()
        assert _extract("grep '>' .env.local") == set()
        assert _extract("grep -c '>' secrets/notes.txt") == set()
        assert _extract("grep '>' file.py") == set()

    def test_test_and_bracket_comparison_not_a_write(self):
        assert _extract('[ "$a" > "$b" ]') == set()
        assert _extract('test "$ver" > "1.0"') == set()
        assert _extract('[ "$a" > .env ]') == set()
        assert _extract('[[ "$a" > "$b" ]]') == set()
        assert _extract('if [[ "$x" > "config.txt" ]]; then echo hi; fi') == set()

    def test_arithmetic_comparison_not_a_write(self):
        assert _extract("echo $((3 > 2))") == set()
        assert _extract("(( total >> 2 ))") == set()

    def test_process_substitution_not_a_write(self):
        assert _extract("tee >(logger)") == set()

    # --- in-scope false-negative fixes confirmed by the adversarial review ---
    def test_clobber_override_redirect_caught(self):
        assert _extract("echo SECRET >| .env") == {("cred", ".env")}
        assert _extract("echo x >| src/clobber.py") == {("local", "/proj/src/clobber.py")}

    def test_target_directory_flag_caught(self):
        assert _extract("cp -t .ssh authorized_keys") == {("cred", ".ssh")}
        assert _extract("cp --target-directory=/secrets a.txt") == {("cred", "/secrets")}
        assert _extract("mv -t src foo.py") == {("local", "/proj/src")}

    def test_bsd_sed_empty_suffix_no_phantom_target(self):
        # `sed -i ''` (BSD empty backup suffix) must not target the sed SCRIPT.
        assert _extract("sed -i '' s/a/b/ src/foo.py") == {("local", "/proj/src/foo.py")}


class TestSingleSourceCredential:
    def test_hook_imports_classifier_from_gates(self):
        # The hook must use gates._is_credential_path (single source), not a private
        # copy of the pattern list -- the adversarial review flagged drift risk.
        src = Path(HOOK_SH).read_text()
        assert "from writ.session.gates import _is_credential_path" in src


# --------------------------------------------------------------------------- #
# 4. work-gate verdict the hook forwards (deterministic, no daemon)
#    Proves the SAME _can_write_check decision the hook curls for each Bash
#    target, independent of the daemon round-trip exercised in section 5.
# --------------------------------------------------------------------------- #
class TestWorkGateVerdict:
    def _env(self, path):
        return {"tool_input": {"file_path": path}}

    def test_work_no_plan_denies_project_source(self):
        gates = _imp("writ.session.gates")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="work", gates_approved=[], current_phase=None)
        res = gates._can_write_check(sid, self._env("/proj/src/foo.py"), SKILL_ROOT)
        assert res["can_write"] is False
        assert "ENF-GATE-PLAN" in (res["reason"] or "")

    def test_work_no_plan_allows_excluded_test_file(self):
        # test files stay writable pre-plan so skeletons can be written.
        gates = _imp("writ.session.gates")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="work", gates_approved=[], current_phase=None)
        res = gates._can_write_check(sid, self._env("/proj/tests/test_foo.py"), SKILL_ROOT)
        assert res["can_write"] is True

    def test_work_both_gates_approved_allows_source(self):
        gates = _imp("writ.session.gates")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="work", gates_approved=["phase-a", "test-skeletons"],
              current_phase="implementation")
        res = gates._can_write_check(sid, self._env("/proj/src/foo.py"), SKILL_ROOT)
        assert res["can_write"] is True

    def test_investigate_allows_project_source(self):
        gates = _imp("writ.session.gates")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="investigate")
        res = gates._can_write_check(sid, self._env("/proj/src/foo.py"), SKILL_ROOT)
        assert res["can_write"] is True


# --------------------------------------------------------------------------- #
# 5. full hook end-to-end (synthetic envelope -> permissionDecision)
# --------------------------------------------------------------------------- #
class TestHookEndToEnd:
    def test_credential_write_denied_any_mode(self, tmp_path: Path):
        # credential deny is the local backstop -> no daemon required. Proves the
        # full hook glue (extract -> classify -> emit_deny) for the credential path.
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="conversation")
        out = _run_hook("echo SECRET > .env", sid, str(tmp_path))
        assert out is not None and out.get("permissionDecision") == "deny"
        assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", "")

    def test_read_only_command_allowed(self, tmp_path: Path):
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="work")
        assert _run_hook("ls -la", sid, str(tmp_path)) is None

    def test_work_mode_project_write_denied(self, tmp_path: Path):
        # Full hook -> daemon round-trip. Skips when no daemon answers on the test
        # port (the suite does not auto-start one); the verdict itself is covered
        # deterministically by TestWorkGateVerdict.
        if not _test_daemon_up():
            pytest.skip("test daemon not running on test port")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        subprocess.run([sys.executable, HELPER, "mode", "set", "work", sid],
                       capture_output=True)
        (tmp_path / "src").mkdir()
        out = _run_hook("echo x > src/foo.py", sid, str(tmp_path))
        subprocess.run([sys.executable, HELPER, "clear", sid], capture_output=True)
        assert out is not None and out.get("permissionDecision") == "deny"
        assert "ENF-GATE-PLAN" in out.get("permissionDecisionReason", "")

    def test_outside_repo_write_is_decided_in_work_mode(self, tmp_path: Path):
        # THIS TEST DOES NOT EXECUTE IN THIS ENVIRONMENT (no daemon answers on the test
        # port), so its name is documentation and not evidence; the executable pin for
        # the same property is TestOutOfCwdTargetIsWorkGated below, which reaches the
        # gate through the CLI fallback. The skip guard is left exactly as it was.
        if not _test_daemon_up():
            pytest.skip("test daemon not running on test port")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        subprocess.run([sys.executable, HELPER, "mode", "set", "work", sid],
                       capture_output=True)
        # The target is outside tmp_path (the repo root). It used to produce no row and
        # no verdict; as of cycle L it is DECIDED, by the same _can_write_check the
        # Write tool calls, so this asserts AGREEMENT with that door rather than
        # silence. Pre-approval both refuse, which is consequence C1 in the approved
        # plan, the accepted cost of two-door parity, whose named remedy is a
        # scratch-zone allow arm in gates._check_work_gate, deferred to its own cycle.
        gates = _imp("writ.session.gates")
        target = "/tmp/writ-scratch-xyz"
        out = _run_hook(f"echo x > {target}", sid, str(tmp_path))
        write_door = gates._can_write_check(
            sid, {"tool_input": {"file_path": target}}, SKILL_ROOT)
        subprocess.run([sys.executable, HELPER, "clear", sid], capture_output=True)
        assert write_door["can_write"] is False, write_door
        assert out is not None, "the out-of-repo target was silent; it reached no gate"
        assert out.get("permissionDecision") == "deny", out
        assert "ENF-GATE-PLAN" in out.get("permissionDecisionReason", ""), out


# --------------------------------------------------------------------------- #
# 6. gate-state name guard: executing or forging vectors deny; provably
#    read-only inspection passes. The original blanket form refused even
#    `grep` of audit logs whose rows name the minter, which blocked live
#    diagnosis three times in one session (BUG-manual-test-grant.md section 0).
# --------------------------------------------------------------------------- #
class TestGateStateNameGuard:
    def _deny(self, out):
        assert out is not None and out.get("permissionDecision") == "deny"
        assert "ENF-GATE-STATE" in out.get("permissionDecisionReason", "")

    def _sid(self):
        return f"bwg-{uuid.uuid4().hex[:8]}"

    def test_invoking_the_minter_script_is_denied(self, tmp_path: Path):
        cmd = f"bash {SKILL_ROOT}/hooks/scripts/writ-manual-test-grant.sh"
        self._deny(_run_hook(cmd, self._sid(), str(tmp_path)))

    def test_running_the_grant_lib_is_denied(self, tmp_path: Path):
        cmd = (f"python3 {SKILL_ROOT}/bin/lib/manual_test_grant.py "
               "mint some-sid 'manual testing approved'")
        self._deny(_run_hook(cmd, self._sid(), str(tmp_path)))

    def test_readonly_grep_naming_the_minter_is_allowed(self, tmp_path: Path):
        cmd = "grep -h writ-manual-test-grant var/logs/audit.jsonl"
        assert _run_hook(cmd, self._sid(), str(tmp_path)) is None

    def test_readonly_pipeline_naming_grant_state_is_allowed(self, tmp_path: Path):
        cmd = "grep writ-grant- var/logs/audit.jsonl | tail -5 | wc -l"
        assert _run_hook(cmd, self._sid(), str(tmp_path)) is None

    def test_readonly_verb_with_command_substitution_is_denied(self, tmp_path: Path):
        self._deny(_run_hook("cat $(echo writ-grant-x.json)", self._sid(), str(tmp_path)))

    def test_readonly_verb_with_redirect_is_denied(self, tmp_path: Path):
        cmd = "grep writ-grant- var/logs/audit.jsonl > /tmp/out"
        self._deny(_run_hook(cmd, self._sid(), str(tmp_path)))

    def test_readonly_verb_with_variable_expansion_is_denied(self, tmp_path: Path):
        self._deny(_run_hook("cat $GRANT_FILE writ-grant-x.json", self._sid(), str(tmp_path)))

    def test_readonly_verb_chained_to_interpreter_is_denied(self, tmp_path: Path):
        cmd = "cat writ-grant-x.json && bash forge.sh"
        self._deny(_run_hook(cmd, self._sid(), str(tmp_path)))

    def test_pipeline_segment_with_non_readonly_verb_is_denied(self, tmp_path: Path):
        cmd = "grep writ-grant- var/logs/audit.jsonl | xargs rm"
        self._deny(_run_hook(cmd, self._sid(), str(tmp_path)))


# --------------------------------------------------------------------------- #
# 7. matcher wiring
# --------------------------------------------------------------------------- #
class TestMatcherWired:
    def test_bash_write_gate_on_bash_matcher(self):
        data = json.loads(open(HOOKS_JSON).read())["hooks"]
        scripts = []
        for g in data.get("PreToolUse", []):
            if "Bash" in g.get("matcher", "").split("|"):
                scripts += [h["command"].rsplit("/", 1)[-1] for h in g.get("hooks", [])]
        assert "writ-bash-write-gate.sh" in scripts


class TestTemplatePlaceholderIsNotAPath:
    """A quoted literal holding an unresolved placeholder is a TEMPLATE, not a path.

    PATH_CAND excludes braces, so a run scanned out of `check('<brace>d<brace>/x/y')`
    drops the placeholder and leaves `/x/y`, which reads as an absolute path nobody
    named. That phantom was harmless while out-of-repo targets produced no row at all;
    once this cycle work-gated them it became a REFUSAL, and it fired twice within
    minutes on ordinary agent tooling (a test harness building a subprocess argument
    with an f-string). CODE_PUNCT already lists the braces as characters meaning "this
    token is code, not a filename"; it was simply never applied to runs taken from
    inside a quoted literal.

    The two positives below are the point: the skip is keyed on the placeholder, not on
    being out of repo, so a genuine absolute path outside the project is still gated.
    """

    def test_a_placeholder_literal_yields_no_target(self) -> None:
        brace_open, brace_close = chr(123), chr(125)
        cmd = ('python3 -c "from x import check; check(\'%sd%s/elsewhere/pkg\')"'
               % (brace_open, brace_close))
        assert _extract(cmd, cwd="/proj") == set(), (
            "an unresolved placeholder literal is a template; the phantom path "
            "reached the work gate and refused real agent tooling"
        )

    def test_a_real_absolute_path_outside_the_repo_is_still_a_target(self) -> None:
        got = _extract("python3 -c \"check('/home/other/pkg/thing.py')\"", cwd="/proj")
        assert got == {("outside", "/home/other/pkg/thing.py")}, got

    def test_a_real_path_inside_the_repo_is_still_a_target(self) -> None:
        got = _extract("python3 -c \"open('/proj/src/app.py','w')\"", cwd="/proj")
        assert got == {("local", "/proj/src/app.py")}, got


class TestInterpreterArgDirectoryIsNotAWriteTarget:
    """The inline-interpreter args scan fed DIRECTORY paths to the work gate.

    Observed live: a read-only `python -c "validate('<project root>')"` probe
    was denied as '[Bash write to writ]' -- the target was the repo root
    itself, via the explicit `ap == cwd` branch. No language this vector
    covers can open() a directory for writing, so an EXISTING directory can
    never be an interpreter file-write target; gating it is pure false
    positive. A NONEXISTENT path stays gated: it could be a file about to be
    created. The extractor cannot know which without the filesystem, which is
    why these cases use a real tmp tree instead of the harness's /proj."""

    def test_existing_directory_arg_is_not_a_target(self, tmp_path) -> None:
        (tmp_path / "writ").mkdir()
        got = _extract(
            f"python3 -c \"from x import check; check('{tmp_path}/writ')\"",
            cwd=str(tmp_path),
        )
        assert not any(k == "local" for k, _ in got), (
            f"an existing directory must not be a write target; extractor "
            f"emitted {got}"
        )

    def test_project_root_itself_is_not_a_target(self, tmp_path) -> None:
        got = _extract(
            f"python3 -c \"validate('{tmp_path}')\"", cwd=str(tmp_path)
        )
        assert not any(k == "local" for k, _ in got), (
            f"the project root is always a directory and was the live "
            f"false-positive; extractor emitted {got}"
        )

    def test_existing_directory_outside_cwd_is_not_a_target(self, tmp_path) -> None:
        # The out-of-cwd half of the carve-out, unproven until the `outside` row existed.
        # The two tests above run the directory INSIDE cwd, so a broken carve-out shows up
        # there as a `local` row and their `k == "local"` assertions catch it. Out of cwd a
        # break now emits `outside` instead, which those assertions cannot see. Asserting
        # the row set is EMPTY covers both spellings, so this cannot decay the same way.
        outside_dir = tmp_path / "elsewhere" / "pkg"
        outside_dir.mkdir(parents=True)
        cwd = tmp_path / "proj"
        cwd.mkdir()
        got = _extract(
            f"python3 -c \"from x import check; check('{outside_dir}')\"",
            cwd=str(cwd),
        )
        assert got == set(), (
            f"an existing directory outside cwd must not be a write target; "
            f"extractor emitted {got}"
        )

    def test_existing_file_arg_is_still_a_target(self, tmp_path) -> None:
        (tmp_path / "app.py").write_text("x = 1\n")
        got = _extract(
            f"python3 -c \"open('{tmp_path}/app.py','w')\"", cwd=str(tmp_path)
        )
        assert ("local", str(tmp_path / "app.py")) in got, (
            f"an existing FILE named by interpreter args must stay gated; "
            f"extractor emitted {got}"
        )

    def test_nonexistent_path_arg_is_still_a_target(self, tmp_path) -> None:
        got = _extract(
            f"python3 -c \"open('{tmp_path}/new_module.py','w')\"",
            cwd=str(tmp_path),
        )
        assert ("local", str(tmp_path / "new_module.py")) in got, (
            f"a not-yet-existing path could be a file about to be created and "
            f"must stay gated; extractor emitted {got}"
        )


# --------------------------------------------------------------------------- #
# 8. project-boundary parity on the Bash door. THE GAP IS CLOSED as of cycle L.
#
#    THE OLD ARGUMENT FOR LEAVING IT OPEN, kept because it is the reason the
#    remedy below is NAMED instead of forgotten: the classifier's cwd-only filter
#    was DELIBERATELY left open by plan.md capability 26 ("a project-boundary
#    predicate on writes"), on the grounds that closing it "would convert routine
#    pre-approval scratch writes into [ENF-GATE-PLAN] refusals with no way out,
#    the 'refusal names no action' defect".
#
#    THAT COST IS REAL, AND NOW MEASURED rather than predicted: pre-approval,
#    `echo x > /tmp/writ-scratch-xyz` denies with [ENF-GATE-PLAN] where it used
#    to be silent (see tests/test_bash_interpreter_write_gate.py's two out-of-repo
#    pins for the measurement). It is consequence C1 in the approved plan, and it
#    was accepted at approval as the price of the parity requirement.
#
#    WHAT OVERRULED IT: with no row emitted, an out-of-cwd Bash write reached
#    _can_write_check not at all, while the Write door refused exactly those paths
#    through writ/session/project_boundary.py. So a boundary the user had approved
#    was enforceable on one of the two write doors -- measured live as a Bash
#    heredoc write that succeeded on a path the Write tool had refused minutes
#    earlier. A confinement enforced on one door is not a confinement, and the
#    silence was indistinguishable from an allow.
#
#    THE REMEDY FOR C1 HAS SHIPPED: a scratch-zone allow arm in
#    gates._check_work_gate, placed after the drift check and before
#    [ENF-GATE-PLAN]. It belongs there and not in this producer because both doors
#    go through _can_write_check, so one arm restores pre-approval scratch
#    usability on both and parity survives. Special-casing the OS temp dir in the
#    classifier instead would have reproduced the old silence and restored the
#    divergence, while leaving the parity property green.
# --------------------------------------------------------------------------- #
class TestOutOfCwdTargetIsWorkGated:
    """An out-of-cwd target is classified `outside` AND decided like a `local` one.

    Deliberately NOT "no `local` row appears". That assertion is now true for two
    different reasons -- the row says `outside` (the fix) or no row was emitted at all
    (the defect) -- so it cannot tell the fix from its absence, and it read as a PASS
    after the behaviour it was written to pin had been deliberately reversed. Each pin
    below names the row exactly, or checks the decision that row earns.
    """

    OUT_OF_CWD = "/home/other-project/src/thing.py"

    def test_a_target_outside_the_hooks_cwd_emits_an_outside_row(self):
        # EXACT set equality, not membership and not an absence: emitting nothing and
        # emitting `local` are both failures, and catching the first is the point.
        got = _extract(f"echo x > {self.OUT_OF_CWD}", cwd="/proj")
        assert got == {("outside", self.OUT_OF_CWD)}, (
            f"an out-of-cwd target must emit exactly one `outside` row so it reaches "
            f"the same gate the Write door uses; extractor emitted {got}"
        )

    def test_an_out_of_cwd_target_reaches_the_same_decision_as_a_local_one(
        self, tmp_path: Path
    ) -> None:
        # The row is only half the fix: the consumer's kind test has to FEED `outside`
        # to the same can-write a `local` row gets. Two targets, one session, one
        # verdict. The out-of-cwd target sits in the OS scratch zone on purpose, so the
        # project boundary abstains on it and the decision comes from the same work gate
        # that decides the in-cwd file; otherwise the two denials could agree for
        # different reasons.
        # MEASURED, AND STILL DENY AFTER THE SCRATCH ARM SHIPPED, for a reason worth
        # naming so nobody reads this as the arm being broken: _seed below records no
        # project_root, cache.py defaults it to "", and the arm routes through
        # boundary_root, which abstains on an empty root. So this asserts door AGREEMENT
        # in a rootless session, not the behavior of a real pre-approval scratch write.
        # The arm's own two-door coverage is the directional pin in
        # tests/test_write_door_parity.py, which stamps a root and moves the zone.
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="work", gates_approved=[], current_phase=None)
        (tmp_path / "src").mkdir()
        scratch = os.path.join(tempfile.gettempdir(), "writ-outside-cwd-probe.py")
        assert not scratch.startswith(str(tmp_path)), (
            "the probe path must be OUTSIDE the hook's cwd or this pin proves nothing"
        )
        local_out = _run_hook("echo x > src/foo.py", sid, str(tmp_path))
        outside_out = _run_hook(f"echo x > {scratch}", sid, str(tmp_path))
        for name, out in (("local", local_out), ("outside", outside_out)):
            assert out is not None, (
                f"the {name} target was silent, so it reached no gate at all"
            )
            assert out.get("permissionDecision") == "deny", (name, out)
            assert "ENF-GATE-PLAN" in out.get("permissionDecisionReason", ""), (name, out)
        assert (outside_out["permissionDecision"]
                == local_out["permissionDecision"]), (local_out, outside_out)
