"""The interpreter one-liner is a write vector, and it is now gated like a redirect.

THE DEFECT, reproduced live by an audit before this file existed. In a session with
mode=work and NO gates approved:

    echo x > src/sneaky.py                              -> denied, [ENF-GATE-PLAN]
    python3 -c "open('src/sneaky.py','w').write('evil')" -> empty stdout, exit 0,
                                                            no ask, no deny, and no
                                                            audit row at all

Both commands write the same file before plan approval. The first matched the hook's
shell write vectors (`>`, `tee`, `cp`, `mv`, `dd of=`, `sed -i`); the second matched
none of them, so no target was extracted, no gate ran, and nothing was recorded. The
"no code before plan approval" boundary fell to one line and left no trace.

WHAT THE FIX ASSERTS HERE, in the order the brief demanded it:
  1. the exact reported command now denies AND writes an audit row (the row is
     asserted, not just stdout -- silence in the audit stream was half the defect);
  2. the shell redirect still denies, unchanged;
  3. an exempt path (tests/...) named through an interpreter is still ALLOWED, and the
     path was genuinely extracted first -- so the allow comes from the existing
     exemption logic being reused, not from the scan failing to see it;
  4. a benign one-liner naming no project path stays silent, because a fix that gates
     `python3 -c "print(1+1)"` is a fix nobody can keep;
  5. anti-vacuity: with the new scan stubbed to find nothing, the bypass goes
     undetected again -- so the deny in (1) is attributable to the scan and a broken
     scanner cannot read as a pass.

Every hook test here drives the REAL hook with a REAL PreToolUse envelope, in a
throwaway WRIT_CACHE_DIR / WRIT_LOG_ROOT, against a session seeded to mode=work with
no gates approved. WRIT_PORT points at a dead port on purpose: the hook then takes its
documented daemon-unreachable fallback (the same local `can-write` the Write tool
uses), so these tests are deterministic and never skip on "no daemon".
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_bash_write_gate import run_extractor

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
HOOK_SH = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-bash-write-gate.sh")

# The command the audit reproduced. Kept as one constant so every test that speaks
# about "the reported bypass" is speaking about the same string.
BYPASS_CMD = """python3 -c "open('src/sneaky.py','w').write('evil')" """.strip()
REDIRECT_CMD = "echo x > src/sneaky.py"


def _imp(name):
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module(name)


def _extractor_src() -> str:
    """Slice the embedded python extractor block out of the hook script."""
    text = Path(HOOK_SH).read_text()
    marker = text.index("<<'PY'")
    start = text.index("\n", marker) + 1
    end = text.index("\nPY\n", start)
    return text[start:end]


def _extract(cmd: str, cwd: str, src: str | None = None) -> set[tuple[str, ...]]:
    """Run the extractor (optionally a mutated copy) and return the rows it emits.

    REUSED, NOT DUPLICATED: `run_extractor` from tests/test_bash_write_gate.py hands the
    command over on a FILE (`WRIT_BASH_CMD_FILE`), which is the transport the hook itself
    uses, and strips the completion sentinel from the rows."""
    env = dict(os.environ, WRIT_DIR=SKILL_ROOT)
    _p, lines = run_extractor(cmd, cwd, env=env, src=src, timeout=60)
    return {tuple(line.split("\t")) for line in lines if line}


@pytest.fixture()
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A throwaway work-mode session with no gates approved, and its own state+logs."""
    cache, logs, proj = tmp_path / "cache", tmp_path / "logs", tmp_path / "proj"
    for d in (cache, logs, proj / "src", proj / "tests", proj / "scripts"):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache))
    monkeypatch.setenv("WRIT_LOG_ROOT", str(logs))
    # The suite's autouse fixture funnels every stream into one friction file; drop it
    # so this test reads the real typed audit stream the hook writes in production.
    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
    # A dead port: the hook's curl fails and it falls back to the local can-write
    # subprocess. No daemon needed, no skip, same verdict.
    monkeypatch.setenv("WRIT_PORT", "19999")
    monkeypatch.setenv("WRIT_NO_AUTOSTART", "1")

    sid = f"biwg-{uuid.uuid4().hex[:8]}"
    cache_mod = _imp("writ.session.cache")
    data = cache_mod._read_cache(sid)
    data.update({"mode": "work", "gates_approved": [], "current_phase": None})
    cache_mod._write_cache(sid, data)

    return SimpleNamespace(sid=sid, proj=proj, logs=logs, env=dict(os.environ))


def _run_hook(gate: SimpleNamespace, cmd: str) -> dict | None:
    """Drive the real hook with a real Bash envelope.

    Returns the parsed hookSpecificOutput, or None when the hook allows silently.
    Asserts the exit contract on every call: the hook exits 0 and carries its decision
    in stdout JSON (Claude Code reads a non-zero PreToolUse exit as a hard failure).
    """
    envelope = json.dumps({
        "session_id": gate.sid,
        "tool_name": "Bash",
        "hook_event_name": "PreToolUse",
        "tool_input": {"command": cmd},
    })
    p = subprocess.run(["bash", HOOK_SH], input=envelope, capture_output=True,
                       text=True, env=gate.env, cwd=str(gate.proj), timeout=60)
    assert p.returncode == 0, f"hook must exit 0; got {p.returncode}\n{p.stderr[-2000:]}"
    out = p.stdout.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]


def _audit_rows(gate: SimpleNamespace) -> list[dict]:
    """Every gate_decision row the hook wrote to the throwaway log root."""
    rows = []
    for f in Path(gate.logs).rglob("*.jsonl"):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("event") == "gate_decision":
                rows.append(row)
    return rows


def _blocked(out: dict | None) -> bool:
    """The gate answered rather than staying silent. deny OR ask both count: the fix's
    contract is 'not silent', and which of the two a target earns is the existing
    decision path's business, not this vector's."""
    return out is not None and out.get("permissionDecision") in ("deny", "ask")


# --------------------------------------------------------------------------- #
# 1. the reported bypass: denied, and recorded
# --------------------------------------------------------------------------- #
class TestReportedBypassIsClosed:
    def test_interpreter_one_liner_is_no_longer_silent(self, gate):
        out = _run_hook(gate, BYPASS_CMD)
        assert _blocked(out), f"the reported bypass produced {out!r}"
        assert "ENF-GATE-PLAN" in out.get("permissionDecisionReason", "")

    def test_interpreter_one_liner_leaves_an_audit_row(self, gate):
        # The row, not the stdout. A gate that refuses without recording is only half a
        # gate: the original defect was as much "no trace" as "no deny".
        _run_hook(gate, BYPASS_CMD)
        rows = [r for r in _audit_rows(gate) if r.get("decision") == "deny"]
        assert rows, f"no deny row in the audit stream: {_audit_rows(gate)!r}"
        row = rows[0]
        assert row["gate"] == "bash-write"
        assert row["session"] == gate.sid
        assert row["target"] == str(gate.proj / "src" / "sneaky.py")
        assert "ENF-GATE-PLAN" in row["reason"]

    @pytest.mark.parametrize("cmd, target", [
        ("""python3 -c "open('src/a.py','w').write('x')" """, "src/a.py"),
        ("""python -c 'open("src/a.py","w")'""", "src/a.py"),
        ("""python3.12 -c "open('src/a.py','w')" """, "src/a.py"),
        ("""node -e "require('fs').writeFileSync('src/a.js','x')" """, "src/a.js"),
        ("""node --eval "fs.writeFileSync('src/a.js','x')" """, "src/a.js"),
        ("""perl -e 'open(F,">","src/a.pl")'""", "src/a.pl"),
        ("""ruby -e 'File.write("src/a.rb","x")'""", "src/a.rb"),
        ("""php -r 'file_put_contents("src/a.php","x");'""", "src/a.php"),
        # stdin forms: the source is not an argument at all.
        ("""echo "open('src/a.py','w')" | python3 -""", "src/a.py"),
        ("""printf "open('src/a.py','w')" | python3""", "src/a.py"),
        ("python3 <<'PY'\nopen('src/a.py','w').write('x')\nPY", "src/a.py"),
        ("python3 < scripts/build.py", "scripts/build.py"),
        # the verb is not always token 0 (verb_at resolves wrappers and assignments).
        ("""sudo python3 -c "open('src/a.py','w')" """, "src/a.py"),
        ("""FOO=1 python3 -c "open('src/a.py','w')" """, "src/a.py"),
    ])
    def test_documented_interpreter_forms_reach_the_gate(self, gate, cmd, target):
        out = _run_hook(gate, cmd)
        assert _blocked(out), f"{cmd!r} produced {out!r}"
        assert os.path.basename(target) in out.get("permissionDecisionReason", "")

    def test_credential_write_through_an_interpreter_is_denied(self, gate):
        # The credential boundary applies in every mode and for every vector; before
        # the fix it applied to `echo k > .env` and not to the interpreter spelling.
        out = _run_hook(gate, """python3 -c "open('.env','w').write('k')" """)
        assert out is not None and out["permissionDecision"] == "deny"
        assert "SEC-CREDENTIAL-WRITE" in out["permissionDecisionReason"]


# --------------------------------------------------------------------------- #
# 2. the shell vector, in the repo unchanged and out of it now decided
#    The IN-REPO redirect is untouched by this file's fix and by cycle L. The
#    OUT-OF-REPO redirect changed in cycle L: it used to be silent and is now
#    decided by the same gate the Write door uses. See the second pin.
# --------------------------------------------------------------------------- #
class TestShellRedirectVector:
    def test_redirect_still_denies_with_its_audit_row(self, gate):
        out = _run_hook(gate, REDIRECT_CMD)
        assert out is not None and out["permissionDecision"] == "deny"
        assert "ENF-GATE-PLAN" in out["permissionDecisionReason"]
        rows = [r for r in _audit_rows(gate) if r.get("decision") == "deny"]
        assert rows and rows[0]["target"] == str(gate.proj / "src" / "sneaky.py")

    def test_redirect_outside_the_repo_reaches_the_gate(self, gate):
        # MEASURED after cycle L, in this PRE-APPROVAL session: `echo x > <target>`
        # denies with [ENF-GATE-PLAN] ("Say approved to proceed") where it used to be
        # silent, because the out-of-repo target now emits an `outside` row and takes
        # the same can-write round trip an in-repo target takes. That pre-approval
        # refusal is consequence C1 in the approved plan, the accepted cost of two-door
        # parity. Its named remedy, a scratch-zone allow arm in
        # gates._check_work_gate, has since SHIPPED, and this still denies because the
        # fixture records no project_root: the arm routes through boundary_root, which
        # abstains on an empty root. Real pre-approval scratch behavior is pinned in
        # tests/test_write_door_parity.py and tests/test_scratch_zone_write_arm.py.
        target = "/tmp/writ-scratch-xyz"
        out = _run_hook(gate, f"echo x > {target}")
        assert out is not None, "the out-of-repo redirect was silent; it reached no gate"
        assert out["permissionDecision"] == "deny", out
        assert "ENF-GATE-PLAN" in out["permissionDecisionReason"], out
        # The refusal is not the property; the AGREEMENT is. The same target in the same
        # session gets the same verdict from the Write door, which is the function both
        # transports call.
        gates = _imp("writ.session.gates")
        write_door = gates._can_write_check(
            gate.sid, {"tool_input": {"file_path": target}}, SKILL_ROOT)
        assert write_door["can_write"] is False, write_door


# --------------------------------------------------------------------------- #
# 3. the exemption logic is REUSED, not reimplemented
# --------------------------------------------------------------------------- #
class TestExemptionsAreReused:
    def test_test_file_named_by_an_interpreter_is_allowed(self, gate):
        # tests/* stays writable before plan approval so skeletons can be written.
        assert _run_hook(gate, """python3 -c "open('tests/test_x.py','w')" """) is None

    def test_that_allow_is_an_exemption_and_not_a_miss(self, gate):
        # The distinction the test above cannot make on its own: an allow because the
        # gate exempted the path, versus an allow because the scan never saw it. The
        # extractor must have produced the target for the exemption to be what decided.
        rows = _extract("""python3 -c "open('tests/test_x.py','w')" """, str(gate.proj))
        assert ("local", str(gate.proj / "tests" / "test_x.py")) in rows

    def test_outside_the_repo_reaches_the_gate_through_an_interpreter(self, gate):
        # The sibling vector, and the reason cycle L did not carve interpreter hits out:
        # suppressing them would have re-opened the interpreter write door outside the
        # repo while the redirect pin above stayed green.
        # MEASURED after cycle L, in this PRE-APPROVAL session: this denies with
        # [ENF-GATE-PLAN] ("Say approved to proceed") where it used to be silent. That
        # refusal is consequence C1 in the approved plan, the accepted cost of two-door
        # parity. Its named remedy, a scratch-zone allow arm in
        # gates._check_work_gate, has since SHIPPED, and this still denies because the
        # fixture records no project_root: the arm routes through boundary_root, which
        # abstains on an empty root. Real pre-approval scratch behavior is pinned in
        # tests/test_write_door_parity.py and tests/test_scratch_zone_write_arm.py.
        target = "/tmp/scratch.py"
        out = _run_hook(gate, f"""python3 -c "open('{target}','w')" """)
        assert out is not None, (
            "the out-of-repo interpreter write was silent; it reached no gate"
        )
        assert out["permissionDecision"] == "deny", out
        assert "ENF-GATE-PLAN" in out["permissionDecisionReason"], out
        gates = _imp("writ.session.gates")
        write_door = gates._can_write_check(
            gate.sid, {"tool_input": {"file_path": target}}, SKILL_ROOT)
        assert write_door["can_write"] is False, write_door


# --------------------------------------------------------------------------- #
# 4. the regression that would make the fix unusable
# --------------------------------------------------------------------------- #
class TestBenignOneLinersStaySilent:
    @pytest.mark.parametrize("cmd", [
        """python3 -c "print(1+1)" """,
        """python3 -c "import os, sys; print(os.path.join('a','b'))" """,
        """python3 -c "import json; print(json.dumps({'a': 1}))" """,
        """python3 -c "print(3/2)" """,
        """python3 -c "print('utf-8'.upper())" """,
        # Attribute chains are not paths. A first cut of this scan read interpreter
        # source as flat text, so `console.log` became a .log file and `process.env` a
        # credential, and every node one-liner was refused. Both stay here as the
        # regression guard for that: only STRING LITERALS are scanned now.
        """node -e "console.log(process.version)" """,
        """node -e "console.log(process.env)" """,
        """node -e "console.log(process.env.NODE_ENV)" """,
        """python3 -c "import logging; logging.getLogger().info('hi')" """,
        # module execution is not inline code, and its own flags are not ours.
        "python3 -m pytest -q",
        ".venv/bin/python -m pytest -q -c setup.cfg",
        # a script invocation names its script, and running one was never this gate's
        # business -- the write vectors above still apply to whatever it does.
        "python3 script.py --verbose",
        "node server.js | tail -3",
    ])
    def test_no_project_path_means_no_prompt(self, gate, cmd):
        assert _run_hook(gate, cmd) is None, cmd

    def test_a_silent_allow_records_no_denial(self, gate):
        _run_hook(gate, """python3 -c "print(1+1)" """)
        assert [r for r in _audit_rows(gate) if r.get("decision") != "allow"] == []


# --------------------------------------------------------------------------- #
# 4b. item A (plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe): `open(<literal>)` and
# `open(<literal>, 'r'|'rt'|'rb')` in inline interpreter source is a READ, and is
# no longer gated; any other mode, any keyword form, any non-literal argument, and
# any method call spelled `X.open(...)` stays gated exactly as before. RED at HEAD:
# READ_OPEN_MODES / READ_OPEN_CALL do not exist yet, so every one-liner below is
# still gated like a write.
# --------------------------------------------------------------------------- #
class TestReadOnlyOpenIsNotAWrite:
    """`open(<literal>)` and `open(<literal>, 'r'|'rt'|'rb')` are reads, and the
    write gate must not treat them as writes: not blocked, AND the extractor must
    yield no target for them (the allow is a genuine miss on the scan, not the
    exemption logic reused from elsewhere)."""

    @pytest.mark.parametrize("cmd", [
        """python3 -c "print(open('src/x.py').read())" """,
        """python3 -c "import json; print(json.load(open('config.json')))" """,
        """python3 -c "open('src/x.py','rb').read()" """,
        """python3 -c 'print(open("src/x.py", "rt").read())'""",
    ], ids=["bare-read", "load-open-config", "rb-mode", "rt-mode-mixed-quotes"])
    def test_not_blocked(self, gate, cmd):
        assert _run_hook(gate, cmd) is None, cmd

    @pytest.mark.parametrize("cmd, path", [
        ("""python3 -c "print(open('src/x.py').read())" """, "src/x.py"),
        ("""python3 -c "import json; print(json.load(open('config.json')))" """, "config.json"),
        ("""python3 -c "open('src/x.py','rb').read()" """, "src/x.py"),
        ("""python3 -c 'print(open("src/x.py", "rt").read())'""", "src/x.py"),
    ], ids=["bare-read", "load-open-config", "rb-mode", "rt-mode-mixed-quotes"])
    def test_no_target_is_extracted_the_allow_is_a_genuine_miss(self, gate, cmd, path):
        rows = _extract(cmd, str(gate.proj))
        assert ("local", str(gate.proj / path)) not in rows, (
            f"{cmd!r} still produced a target for {path!r}: {rows!r}"
        )

    def test_a_heredoc_body_reading_a_file_is_not_blocked(self, gate):
        cmd = "python3 <<'PY'\nprint(open('src/x.py').read())\nPY"
        assert _run_hook(gate, cmd) is None, cmd


class TestStillGatedReads:
    """Everything the read-only allow must NOT widen to: a bare pipe into stdin
    (no `open()` call at all -- `src/x.py` is a bare literal fed to python as the
    program, and the approved rule keeps bare literals gated), a printed literal, a
    dict key, a keyword-form mode, a method call spelled `X.open(...)`, and the
    WRITE half of a mixed read/write one-liner."""

    @pytest.mark.parametrize("cmd", [
        """cat src/x.py | python3 -""",
        """python3 -c "print('src/x.py')" """,
        """python3 -c "d={'src/x.py': 1}" """,
        """python3 -c "open('src/x.py', mode='r')" """,
        """python3 -c "shelve.open('src/x.db')" """,
    ], ids=["cat-pipe-bare-literal", "printed-literal", "dict-key",
            "keyword-mode-not-matched", "method-call-not-bare-open"])
    def test_still_blocked(self, gate, cmd):
        assert _blocked(_run_hook(gate, cmd)), cmd

    def test_mixed_read_and_write_one_liner_gates_only_the_write_half(self, gate):
        cmd = """python3 -c "d=open('a.json').read(); open('src/x.py','w').write(d)" """
        out = _run_hook(gate, cmd)
        assert _blocked(out), f"the write half must still be gated: {out!r}"
        assert os.path.basename("src/x.py") in out.get("permissionDecisionReason", "")
        rows = _extract(cmd, str(gate.proj))
        assert ("local", str(gate.proj / "a.json")) not in rows, (
            f"the read half must not be a target: {rows!r}"
        )
        assert ("local", str(gate.proj / "src" / "x.py")) in rows, (
            f"the write half must still be a target: {rows!r}"
        )

    @pytest.mark.parametrize("mode", ["w", "a", "x", "r+", "wb", "w+"])
    def test_every_other_open_mode_stays_blocked(self, gate, mode):
        cmd = f"""python3 -c "open('src/x.py', '{mode}')" """
        assert _blocked(_run_hook(gate, cmd)), cmd


class TestReadOpenMutationProofs:
    """Same technique as TestAntiVacuity: replace one production constant in the
    extractor's source text and prove the boundary moves for exactly the expected
    reason. RED at HEAD: READ_OPEN_MODES / READ_OPEN_CALL do not exist yet, so the
    signature checks below fail outright rather than merely being vacuous."""

    MODES_SIGNATURE = 'READ_OPEN_MODES = frozenset({"r", "rt", "rb"})'
    CALL_SIGNATURE = "READ_OPEN_CALL = re.compile("

    def test_widening_read_open_modes_to_include_w_lets_a_write_escape(self, gate):
        src = _extractor_src()
        assert self.MODES_SIGNATURE in src, (
            "READ_OPEN_MODES moved or is not spelled exactly this; this test is "
            "now vacuous and must be re-pinned to the new spelling"
        )
        mutated = src.replace(
            self.MODES_SIGNATURE,
            'READ_OPEN_MODES = frozenset({"r", "rt", "rb", "w"})',
        )
        cmd = """python3 -c "open('src/x.py','w')" """
        target = ("local", str(gate.proj / "src" / "x.py"))
        assert target not in _extract(cmd, str(gate.proj), mutated), (
            "widening READ_OPEN_MODES to include 'w' let a write mode escape the scan"
        )
        assert target in _extract(cmd, str(gate.proj)), (
            "the real, unmutated source must still gate this write"
        )

    def test_a_read_open_call_that_never_matches_gates_the_read_only_one_liner_again(
        self, gate
    ):
        src = _extractor_src()
        assert self.CALL_SIGNATURE in src, (
            "READ_OPEN_CALL moved or is not spelled exactly this; this test is "
            "now vacuous and must be re-pinned to the new spelling"
        )
        start = src.index(self.CALL_SIGNATURE)
        # The assignment's closing paren is the first line, after `start`, that is
        # exactly `)` (module-level constants in this file have no continuation
        # after the statement closes), which is robust to how the multi-line
        # pattern literal itself is wrapped.
        close = src.index("\n)", start)
        end = close + len("\n)")
        never_matches_source = 'READ_OPEN_CALL = re.compile(r"(?!)")'
        mutated = src[:start] + never_matches_source + src[end:]
        cmd = """python3 -c "print(open('src/x.py').read())" """
        target = ("local", str(gate.proj / "src" / "x.py"))
        assert target not in _extract(cmd, str(gate.proj)), (
            "the real, unmutated source must not gate a read-only one-liner"
        )
        assert target in _extract(cmd, str(gate.proj), mutated), (
            "a READ_OPEN_CALL that never matches must make the read-only one-liner "
            "gated again -- proving the allow comes from the new strip and nothing else"
        )


# --------------------------------------------------------------------------- #
# 5. anti-vacuity: the deny above is caused by the new scan
# --------------------------------------------------------------------------- #
class TestAntiVacuity:
    """A passing suite must not survive a scanner that finds nothing.

    The mutation is surgical: only `scan_tokens` (the seam the interpreter pass feeds
    from) is stubbed to return []. If the bypass were being caught by something else --
    the gate-state guard, an incidental redirect match, a credential glob -- the stub
    would change nothing and these tests would fail, which is the point.
    """

    REAL_SIGNATURE = "def scan_tokens(toks):"

    def _stubbed_src(self) -> str:
        src = _extractor_src()
        assert self.REAL_SIGNATURE in src, "scan_tokens moved; this test is now vacuous"
        return src.replace(self.REAL_SIGNATURE,
                           "def scan_tokens(toks):\n    return []\n\n\ndef _unused(toks):")

    def test_real_scan_finds_the_bypass_target(self, gate):
        assert ("local", str(gate.proj / "src" / "sneaky.py")) in _extract(
            BYPASS_CMD, str(gate.proj))

    def test_stubbed_scan_finds_nothing_for_the_bypass(self, gate):
        # Same command, same extractor, scan disabled -> the pre-fix silence returns.
        assert _extract(BYPASS_CMD, str(gate.proj), self._stubbed_src()) == set()

    def test_stub_leaves_the_shell_vector_intact(self, gate):
        # Proves the mutation disabled the INTERPRETER scan specifically, rather than
        # breaking the extractor outright (which would make the test above pass for a
        # reason that has nothing to do with the fix).
        assert ("local", str(gate.proj / "src" / "sneaky.py")) in _extract(
            REDIRECT_CMD, str(gate.proj), self._stubbed_src())
