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
    """Run the extractor (optionally a mutated copy) and return the rows it emits."""
    env = dict(os.environ, WRIT_BASH_CMD=cmd, WRIT_CWD=cwd, WRIT_DIR=SKILL_ROOT)
    p = subprocess.run([sys.executable, "-c", src if src is not None else _extractor_src()],
                       env=env, capture_output=True, text=True, timeout=60)
    return {tuple(line.split("\t")) for line in p.stdout.splitlines() if line}


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
# 2. the shell vector is untouched
# --------------------------------------------------------------------------- #
class TestShellRedirectUnchanged:
    def test_redirect_still_denies_with_its_audit_row(self, gate):
        out = _run_hook(gate, REDIRECT_CMD)
        assert out is not None and out["permissionDecision"] == "deny"
        assert "ENF-GATE-PLAN" in out["permissionDecisionReason"]
        rows = [r for r in _audit_rows(gate) if r.get("decision") == "deny"]
        assert rows and rows[0]["target"] == str(gate.proj / "src" / "sneaky.py")

    def test_redirect_outside_the_repo_is_still_not_work_gated(self, gate):
        assert _run_hook(gate, "echo x > /tmp/writ-scratch-xyz") is None


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

    def test_outside_the_repo_is_not_work_gated_through_an_interpreter(self, gate):
        assert _run_hook(gate, """python3 -c "open('/tmp/scratch.py','w')" """) is None


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
# 4b. the cost of the fix, stated instead of discovered
# --------------------------------------------------------------------------- #
class TestAcceptedFalsePositives:
    """These commands only READ, and they are no longer silent. That is the deliberate
    trade: telling a write from a read inside interpreter source is a parser arms race,
    and the alternative to over-refusing is the silent write this fix exists to close.
    Recorded as tests so the cost is visible to whoever changes this next, rather than
    turning up as a surprise in someone's session."""

    @pytest.mark.parametrize("cmd", [
        """python3 -c "print(open('src/x.py').read())" """,
        """python3 -c "import json; print(json.load(open('config.json')))" """,
        """cat src/x.py | python3 -""",
    ])
    def test_read_only_one_liner_naming_a_project_file_is_gated(self, gate, cmd):
        assert _blocked(_run_hook(gate, cmd)), cmd


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
