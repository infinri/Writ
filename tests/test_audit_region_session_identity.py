"""Part 1 of the isolation cycle: bin/audit-region.sh stops guessing its session.

Today (verify on read; the plan was authored against these line numbers and they may have
shifted): audit-region.sh:27-29 falls back to `/tmp/writ-current-session` when `--session`
is not given, with no `$CLAUDE_SESSION_ID` fallback at all. That pointer names whichever
Claude Code session on this machine took a turn most recently, so an operator's
`audit-region.sh some/dir` could freeze its INV-4 coverage denominator into a SESSION OTHER
THAN THE ONE RUNNING IT -- "a wrong id freezes an audit coverage scope into another
session's cache" (plan.md).

After this cycle: `--session SID` or `$CLAUDE_SESSION_ID` is required; neither present
exits 2 with a JSON error; the pointer is never read.

This file is RED until that conversion lands: `TestClaudeSessionIdEnvFallback` exercises
behavior the script does not have today (no env fallback at all), and
`TestNoSessionAndNoEnvExitsLoud` exercises the refusal this script does not perform today
(it still succeeds via the pointer). Per TEST-TDD-001: skeletons approved before
implementation.

Every invocation is a REAL subprocess (bash) against REAL files in tmp_path -- ENF-SYS-005
applies here exactly as it does to the gate-artifact work: a mocked subprocess would only
prove the mock, not that the script itself refuses to guess.

DELIBERATELY NOT HERE: a dynamic "point a fake pointer at a decoy session and prove no
redirection" test. audit-region.sh hardcodes the literal `/tmp/writ-current-session` path
(unlike the python resolver, there is no seam here to monkeypatch), so the only way to
exercise that negative dynamically would be to depend on -- or briefly rewrite -- the REAL,
machine-wide pointer file, which is exactly the shared global state this whole cycle exists
to stop trusting, and which other live Claude Code sessions on this machine also read and
write (see MEMORY: project_global_session_pointer_flaw). That guarantee is proven instead,
deterministically and without touching real state, by the static source scan in
`tests/test_session_identity_no_fallback.py`
(`test_audit_region_no_longer_names_or_reads_the_pointer`), which reads the SCRIPT'S TEXT
rather than the machine's mutable state.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin" / "audit-region.sh"
HELPER = REPO / "bin" / "lib" / "writ-session.py"


def _env(cache_dir: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_SESSION_ID",)}
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env.update(extra)
    return env


def _run(args: list[str], cache_dir: Path, cwd: Path, **extra_env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args], cwd=str(cwd), env=_env(cache_dir, **extra_env),
        capture_output=True, text=True, timeout=60,
    )


def _cache_file(cache_dir: Path, session_id: str) -> Path:
    return cache_dir / f"writ-session-{session_id}.json"


@pytest.fixture()
def target_file(tmp_path) -> Path:
    """A single real, analyzable file so the run gets past region resolution and reaches
    the identity check on its own merits, isolated from a separate "no files" failure."""
    f = tmp_path / "probe.py"
    f.write_text("x = 1\n")
    return f


class TestNoSessionAndNoEnvExitsLoud:
    """Capability 6: bin/audit-region.sh with no --session and no env id exits 2 with a
    JSON error and freezes no coverage scope into any session.

    NOTE ON THIS MACHINE'S STATE: audit-region.sh still reads the real, global
    `/tmp/writ-current-session` today (that read is exactly what this cycle removes), so on
    a host where that file happens to be absent, today's script would ALSO exit 2 by
    accident (empty SID, same as the future behavior) and these four would misleadingly
    pass before the fix lands. Verified on this machine: the file exists and names this
    session, so the run below resolves through it and succeeds (returncode 0), which is
    what makes these four genuinely RED here. The deterministic, host-independent half of
    this capability is `TestClaudeSessionIdEnvFallback` below and the static source scan in
    test_session_identity_no_fallback.py, neither of which depends on any file outside
    tmp_path.
    """

    def test_exits_2(self, tmp_path, target_file):
        proc = _run([str(target_file)], tmp_path, tmp_path)
        assert proc.returncode == 2, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"

    def test_stdout_is_a_json_error(self, tmp_path, target_file):
        proc = _run([str(target_file)], tmp_path, tmp_path)
        payload = json.loads(proc.stdout)
        assert "error" in payload
        assert payload["error"], "the error string must not be empty"

    def test_error_names_how_to_supply_a_session(self, tmp_path, target_file):
        proc = _run([str(target_file)], tmp_path, tmp_path)
        payload = json.loads(proc.stdout)
        text = payload.get("error", "")
        assert "--session" in text or "CLAUDE_SESSION_ID" in text, (
            f"the error must tell the operator how to supply an identity: {text!r}"
        )

    def test_freezes_no_coverage_scope_into_any_session(self, tmp_path, target_file):
        """No cache file of any name may appear: a refused run must leave no trace of a
        scope freeze under a guessed or empty session id."""
        _run([str(target_file)], tmp_path, tmp_path)
        assert list(tmp_path.glob("writ-session-*.json")) == []


class TestClaudeSessionIdEnvFallback:
    """The new acceptance behavior: with --session omitted, $CLAUDE_SESSION_ID is used,
    exactly as every other converted caller in this cycle. Deterministic regardless of
    host state: today the script does not look at $CLAUDE_SESSION_ID at all, so no cache
    file is ever created under "env-sid-1" or "flag-sid" whether or not a real pointer
    happens to exist on the machine running this test.
    """

    def test_env_session_id_is_accepted_with_no_flag(self, tmp_path, target_file):
        proc = _run([str(target_file)], tmp_path, tmp_path, CLAUDE_SESSION_ID="env-sid-1")
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert _cache_file(tmp_path, "env-sid-1").exists()

    def test_explicit_session_flag_still_wins_over_env(self, tmp_path, target_file):
        proc = _run(
            ["--session", "flag-sid", str(target_file)], tmp_path, tmp_path,
            CLAUDE_SESSION_ID="env-sid-should-lose",
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert _cache_file(tmp_path, "flag-sid").exists()
        assert not _cache_file(tmp_path, "env-sid-should-lose").exists()
