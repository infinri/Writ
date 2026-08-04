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
     outside the repo (scratch /tmp) are not work-gated; obfuscated writes evade
     (documented coverage limit, not full coverage).
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

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


def _extract(cmd: str, cwd: str = "/proj") -> set[tuple[str, str]]:
    """Run the extractor on a command; return the set of (kind, path) it emits."""
    env = dict(os.environ, WRIT_BASH_CMD=cmd, WRIT_CWD=cwd)
    p = subprocess.run([sys.executable, "-c", _extractor_src()], env=env,
                       capture_output=True, text=True)
    out = set()
    for line in p.stdout.splitlines():
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

    def test_outside_repo_targets_not_workgated(self):
        assert _extract("echo x > /tmp/scratch") == set()
        assert _extract("cat a | tee -a /tmp/log.txt") == set()

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

    def test_outside_repo_write_allowed_in_work_mode(self, tmp_path: Path):
        if not _test_daemon_up():
            pytest.skip("test daemon not running on test port")
        sid = f"bwg-{uuid.uuid4().hex[:8]}"
        subprocess.run([sys.executable, HELPER, "mode", "set", "work", sid],
                       capture_output=True)
        # write target /tmp/... is outside tmp_path (the repo root) -> not work-gated.
        out = _run_hook("echo x > /tmp/writ-scratch-xyz", sid, str(tmp_path))
        subprocess.run([sys.executable, HELPER, "clear", sid], capture_output=True)
        assert out is None


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
