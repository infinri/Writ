"""Coverage crediting, part 2a: every governed Read is recorded as examined.

Plan: .claude/plans/f7fc2b37-9a53-4011-a69f-e6b97f5e45fe/plan.md

The only `_writ_session update` writ-read-rag.sh runs today (lines 253-257) sits inside
`if [ -n "$META_LINE" ]`, reached only when the daemon answered /query, returned at least
one rule scoring >= 0.4, and format produced a WRIT_META line. Every other Read -- the MISS
path, an unknown-language file, a should-skip-true master, a sub-agent whose query never
scored above threshold -- is never credited, so `pretool_queried_files` (and therefore
`_examined_files`, `cmd_coverage_map`, `cmd_synthesis_gate`) can report 0 examined files
after a real multi-hundred-file fan-out that read every one of them.

This pins capabilities 1 through 9 of the plan: every governed Read (review, debug,
investigate; never work/conversation) is recorded via ONE `_writ_session update` call,
independent of whether the daemon answered, scored, or was reachable at all; the identity
resolution (child cache vs master cache) is unchanged; recording is idempotent; and the
per-Read python3 interpreter-start cost is measured against a literal HEAD baseline rather
than asserted in the abstract.

Per TEST-REGRESSION-001: every "file recorded" assertion below is RED on HEAD (nothing
records a live Read outside the HIT/rule-scored path), and the mode-gating and
missing-file_path cases are regression guards already green on HEAD. GREEN after 2a lands.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.fixtures.net import free_port as _free_port

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

HOOK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "hooks", "scripts", "writ-read-rag.sh")
)
HELPER = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
)
HOOKS_JSON = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "hooks", "hooks.json")
)


# ---------------------------------------------------------------------------
# Stub /query daemons -- reused verbatim shape from
# tests/test_read_rag_investigate_gate.py's _QueryStub, split into a HIT and a
# MISS variant per the plan's "two stub variants" instruction.
# ---------------------------------------------------------------------------

_HIT_RULE_ID = "TEST-RULE-001"


class _HitStub(BaseHTTPRequestHandler):
    """Answers POST /query with one high-score rule."""

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        if self.path == "/query":
            length = int(self.headers.get("Content-Length", 0) or 0)
            self.rfile.read(length)
            body = json.dumps({
                "rules": [{
                    "rule_id": _HIT_RULE_ID,
                    "trigger": "when reading a service class",
                    "statement": "do the governed thing",
                    "violation": "",
                    "pass_example": "",
                    "enforcement": "",
                    "domain": "sec",
                    "severity": "high",
                    "score": 0.95,
                }],
                "meta": {"rule_ids": [_HIT_RULE_ID], "tokens": 120},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_GET(self):  # noqa: N802
        self.send_error(404)


class _MissStub(BaseHTTPRequestHandler):
    """404s /query outright -- the hook's should-skip / format calls fall back to
    the file-direct helper, and no rule is ever scored or banked."""

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        self.send_error(404)

    def do_GET(self):  # noqa: N802
        self.send_error(404)


def _start_stub(handler_cls):
    port = _free_port()
    srv = HTTPServer(("localhost", port), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


@pytest.fixture()
def hit_stub():
    srv, port = _start_stub(_HitStub)
    try:
        yield port
    finally:
        srv.shutdown()


@pytest.fixture()
def miss_stub():
    srv, port = _start_stub(_MissStub)
    try:
        yield port
    finally:
        srv.shutdown()


@pytest.fixture()
def src_file(tmp_path):
    f = tmp_path / "Service.php"
    f.write_text("<?php\nclass Service { public function run(): void {} }\n")
    return str(f)


@pytest.fixture()
def readme_file(tmp_path):
    """A file whose extension detect_language has no case for -- the unknown-language
    ceiling capability 3 pins (no .md branch in bin/lib/common.sh:detect_language)."""
    f = tmp_path / "README.md"
    f.write_text("# Not source\n\nJust docs.\n")
    return str(f)


# ---------------------------------------------------------------------------
# Shared subprocess helpers (writ-session.py + writ-read-rag.sh), no mocks of state.
# ---------------------------------------------------------------------------


def _env(cache_dir, port=None):
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env["WRIT_FRICTION_LOG"] = os.path.join(str(cache_dir), "friction.log")
    if port is not None:
        env["WRIT_HOST"] = "localhost"
        env["WRIT_PORT"] = str(port)
    return env


def _seed_mode(cache_dir, sid, mode):
    subprocess.run(
        [sys.executable, HELPER, "mode", "set", mode, sid],
        env=_env(cache_dir), check=True, capture_output=True, text=True,
    )


def _update(cache_dir, sid, *args):
    subprocess.run(
        [sys.executable, HELPER, "update", sid, *args],
        env=_env(cache_dir), check=True, capture_output=True, text=True,
    )


def _read(cache_dir, sid):
    r = subprocess.run(
        [sys.executable, HELPER, "read", sid],
        env=_env(cache_dir), check=True, capture_output=True, text=True,
    )
    return json.loads(r.stdout)


def _synthesis_gate(cache_dir, sid):
    r = subprocess.run(
        [sys.executable, HELPER, "synthesis-gate", sid],
        env=_env(cache_dir), check=True, capture_output=True, text=True,
    )
    return json.loads(r.stdout)


def _run_hook(cache_dir, port, *, agent_id, session_id=None, file_path=..., extra_env=None):
    """Run writ-read-rag.sh once. `file_path=...` (the sentinel default) sends the file
    path; `file_path=None` omits tool_input.file_path entirely (capability 7's second
    half). `session_id` defaults to `agent_id` (the master-session shape); pass both
    explicitly for the sub-agent shape (capability 6)."""
    env = _env(cache_dir, port)
    if extra_env:
        env.update(extra_env)
    envelope = {
        "agent_id": agent_id,
        "session_id": session_id if session_id is not None else agent_id,
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {} if file_path is None else {"file_path": file_path},
    }
    return subprocess.run(
        ["bash", HOOK], input=json.dumps(envelope),
        capture_output=True, text=True, env=env, timeout=20,
    )


class TestHitPathRecordsAndBanksRule:
    """Capability 1: HIT path leaves file_path in pretool_queried_files AND still
    banks the rule, all from a single update call."""

    def test_hit_path_records_file_and_banks_rule(self, tmp_path, hit_stub, src_file):
        _seed_mode(tmp_path, "hit-1", "investigate")
        r = _run_hook(tmp_path, hit_stub, agent_id="hit-1", file_path=src_file)
        assert r.returncode == 0, r.stderr
        cache = _read(tmp_path, "hit-1")
        assert src_file in cache["pretool_queried_files"]
        assert _HIT_RULE_ID in cache["loaded_rule_ids"]
        assert cache["queries"] == 1


class TestMissPathStillRecords:
    """Capability 2: MISS path (/query answers 404) still records the file_path,
    and loaded_rule_ids / queries are unchanged."""

    def test_miss_path_records_file_without_banking_a_rule(self, tmp_path, miss_stub, src_file):
        _seed_mode(tmp_path, "miss-1", "investigate")
        r = _run_hook(tmp_path, miss_stub, agent_id="miss-1", file_path=src_file)
        assert r.returncode == 0, r.stderr
        cache = _read(tmp_path, "miss-1")
        assert src_file in cache["pretool_queried_files"]
        assert cache["loaded_rule_ids"] == []
        assert cache["queries"] == 0


class TestUnknownLanguageFileIsStillRecorded:
    """Capability 3: a file detect_language calls unknown (README.md, no matching
    extension) is still recorded as examined in investigate mode."""

    def test_readme_is_recorded(self, tmp_path, miss_stub, readme_file):
        _seed_mode(tmp_path, "unk-1", "investigate")
        r = _run_hook(tmp_path, miss_stub, agent_id="unk-1", file_path=readme_file)
        assert r.returncode == 0, r.stderr
        cache = _read(tmp_path, "unk-1")
        assert readme_file in cache["pretool_queried_files"]


class TestModeGating:
    """Capability 4: review and debug record the Read; work and conversation do not."""

    @pytest.mark.parametrize("mode", ["review", "debug"])
    def test_governed_modes_record_the_read(self, tmp_path, miss_stub, src_file, mode):
        sid = f"gated-{mode}"
        _seed_mode(tmp_path, sid, mode)
        r = _run_hook(tmp_path, miss_stub, agent_id=sid, file_path=src_file)
        assert r.returncode == 0, r.stderr
        cache = _read(tmp_path, sid)
        assert src_file in cache["pretool_queried_files"]

    @pytest.mark.parametrize("mode", ["work", "conversation"])
    def test_ungoverned_modes_do_not_record_the_read(self, tmp_path, miss_stub, src_file, mode):
        """Regression guard: green on HEAD already (the mode filter exits first)."""
        sid = f"ungated-{mode}"
        _seed_mode(tmp_path, sid, mode)
        r = _run_hook(tmp_path, miss_stub, agent_id=sid, file_path=src_file)
        assert r.returncode == 0, r.stderr
        cache = _read(tmp_path, sid)
        assert cache["pretool_queried_files"] == []


class TestShouldSkipTrueStillRecords:
    """Capability 5: a master session with remaining_budget 0 (should-skip true)
    still records the Read."""

    def test_budget_exhausted_master_still_records(self, tmp_path, miss_stub, src_file):
        _seed_mode(tmp_path, "skip-1", "investigate")
        _update(tmp_path, "skip-1", "--cost", "999999")
        assert _read(tmp_path, "skip-1")["remaining_budget"] == 0
        r = _run_hook(tmp_path, miss_stub, agent_id="skip-1", file_path=src_file)
        assert r.returncode == 0, r.stderr
        cache = _read(tmp_path, "skip-1")
        assert src_file in cache["pretool_queried_files"]


class TestSubagentReadRecordsChildOnly:
    """Capability 6: a sub-agent Read (agent_id=<child>, session_id=<parent>) records
    the file in the child cache only; the parent's pretool_queried_files is unchanged."""

    def test_child_records_parent_does_not(self, tmp_path, miss_stub, src_file):
        _seed_mode(tmp_path, "parent-6", "work")
        _seed_mode(tmp_path, "child-6", "investigate")
        _update(tmp_path, "child-6", "--parent-session-id", "parent-6", "--is-subagent", "true")
        parent_before = _read(tmp_path, "parent-6")["pretool_queried_files"]

        r = _run_hook(
            tmp_path, miss_stub, agent_id="child-6", session_id="parent-6", file_path=src_file,
        )
        assert r.returncode == 0, r.stderr

        child_after = _read(tmp_path, "child-6")
        assert src_file in child_after["pretool_queried_files"]
        parent_after = _read(tmp_path, "parent-6")["pretool_queried_files"]
        assert parent_after == parent_before == []


class TestIdempotentAndMissingFilePath:
    """Capability 7: two Reads of the same path leave exactly one entry; an envelope
    with no file_path records nothing and exits 0."""

    def test_two_reads_of_the_same_path_leave_one_entry(self, tmp_path, miss_stub, src_file):
        _seed_mode(tmp_path, "dup-1", "investigate")
        for _ in range(2):
            r = _run_hook(tmp_path, miss_stub, agent_id="dup-1", file_path=src_file)
            assert r.returncode == 0, r.stderr
        cache = _read(tmp_path, "dup-1")
        assert cache["pretool_queried_files"].count(src_file) == 1

    def test_no_file_path_records_nothing_and_exits_zero(self, tmp_path, miss_stub):
        """Regression guard: green on HEAD already (the empty-FILE_PATH exit)."""
        _seed_mode(tmp_path, "nofp-1", "investigate")
        r = _run_hook(tmp_path, miss_stub, agent_id="nofp-1", file_path=None)
        assert r.returncode == 0, r.stderr
        cache = _read(tmp_path, "nofp-1")
        assert cache["pretool_queried_files"] == []


class TestEmbeddedNewlineFilePath:
    """Regression: the merged id/file_path parse must not truncate a file_path that
    contains a newline (a line-per-value split kept only the part before it)."""

    def test_newline_in_file_path_is_recorded_intact(self, tmp_path, miss_stub):
        weird = tmp_path / "weird\nfile.php"
        weird.write_text("<?php\nclass Weird {}\n")
        _seed_mode(tmp_path, "nl-1", "investigate")
        r = _run_hook(tmp_path, miss_stub, agent_id="nl-1", file_path=str(weird))
        assert r.returncode == 0, r.stderr
        cache = _read(tmp_path, "nl-1")
        assert str(weird) in cache["pretool_queried_files"]
        assert str(tmp_path / "weird") not in cache["pretool_queried_files"]


class TestSynthesisGateTransitionsOnGovernedRead:
    """Capability 8: with a frozen scope containing the file, one governed Read turns
    synthesis-gate from ready=false/examined_in_scope=0 to ready=true/examined_in_scope=1."""

    def test_one_read_flips_the_gate(self, tmp_path, miss_stub, src_file):
        _seed_mode(tmp_path, "gate-8", "investigate")
        _update(tmp_path, "gate-8", "--freeze-scope", json.dumps({"files": [src_file]}))

        before = _synthesis_gate(tmp_path, "gate-8")
        assert before["ready"] is False
        assert before["examined_in_scope"] == 0

        r = _run_hook(tmp_path, miss_stub, agent_id="gate-8", file_path=src_file)
        assert r.returncode == 0, r.stderr

        after = _synthesis_gate(tmp_path, "gate-8")
        assert after["ready"] is True
        assert after["examined_in_scope"] == 1


class TestPython3ExecCountPerRead:
    """Capability 9: python3 exec count per Read, against the HEAD baseline pinned
    here as a literal constant, measured with a PATH shim that appends one line per
    python3 exec to a counter file and then execs the real interpreter.

    Measured on HEAD (this batch's changes not yet applied), 2026-09-25, against this
    same harness: HIT=19, MISS=10, should-skip-true=3. These are the literal, already-
    reported baseline; do not re-derive them from the design analysis's prose (the
    counts include a handful of exec calls the shell helpers make that the prose does
    not enumerate one-by-one).

    Expected AFTER 2a lands (from the plan's Analysis section): the HIT path pays one
    fewer exec (FILE_PATH parse merged into the id parse; the update was already
    paid). The MISS path stays level (minus the parse, plus the one new update). The
    should-skip-true path is the one path that grows, by exactly one (it used to exit
    before the FILE_PATH parse; now it pays one update).
    """

    _BASELINE_HIT_EXECS = 19
    _BASELINE_MISS_EXECS = 10
    _BASELINE_SHOULD_SKIP_EXECS = 3

    @staticmethod
    def _shim_dir(tmp_path):
        real_python3 = shutil.which("python3")
        assert real_python3, "no python3 on PATH to build the exec-count shim from"
        shim_dir = tmp_path / "path-shim"
        shim_dir.mkdir()
        wrapper = shim_dir / "python3"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            'echo exec >> "${WRIT_EXEC_COUNTER:?}"\n'
            f'exec "{real_python3}" "$@"\n'
        )
        wrapper.chmod(0o755)
        return shim_dir

    def _measure(self, tmp_path, cache_dir, port, sid, file_path):
        counter = tmp_path / "exec-count.txt"
        counter.write_text("")
        shim_dir = self._shim_dir(tmp_path)
        env = _env(cache_dir, port)
        env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
        env["WRIT_EXEC_COUNTER"] = str(counter)
        envelope = {
            "agent_id": sid, "session_id": sid, "hook_event_name": "PreToolUse",
            "tool_name": "Read", "tool_input": {"file_path": file_path},
        }
        r = subprocess.run(
            ["bash", HOOK], input=json.dumps(envelope),
            capture_output=True, text=True, env=env, timeout=20,
        )
        assert r.returncode == 0, r.stderr
        return len(counter.read_text().splitlines())

    def test_hit_path_uses_one_fewer_exec_than_baseline(self, tmp_path, hit_stub, src_file):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _seed_mode(cache_dir, "exec-hit", "investigate")
        execs = self._measure(tmp_path, cache_dir, hit_stub, "exec-hit", src_file)
        assert execs == self._BASELINE_HIT_EXECS - 1

    def test_miss_path_matches_baseline_exactly(self, tmp_path, miss_stub, src_file):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _seed_mode(cache_dir, "exec-miss", "investigate")
        execs = self._measure(tmp_path, cache_dir, miss_stub, "exec-miss", src_file)
        assert execs == self._BASELINE_MISS_EXECS

    def test_should_skip_true_path_uses_one_more_exec_than_baseline(
        self, tmp_path, miss_stub, src_file,
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _seed_mode(cache_dir, "exec-skip", "investigate")
        _update(cache_dir, "exec-skip", "--cost", "999999")
        execs = self._measure(tmp_path, cache_dir, miss_stub, "exec-skip", src_file)
        assert execs == self._BASELINE_SHOULD_SKIP_EXECS + 1


class TestHooksJsonMatcherUnchanged:
    """Capability 9 (second half): the hooks.json matcher for writ-read-rag.sh is
    still exactly "Read" -- Grep and Glob are not counted. Regression guard, green
    on both sides of this batch."""

    def test_matcher_is_exactly_read(self):
        with open(HOOKS_JSON) as f:
            hooks = json.load(f)
        matchers = [
            entry["matcher"]
            for entry in hooks["hooks"]["PreToolUse"]
            if any(
                "writ-read-rag.sh" in h.get("command", "")
                for h in entry.get("hooks", [])
            )
        ]
        assert matchers == ["Read"], matchers
