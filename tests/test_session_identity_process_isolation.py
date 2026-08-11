"""Part 1 of the isolation cycle: the resolver's isolation claim, proven with REAL OS
processes against REAL files -- not mocks.

Per ENF-SYS-005, a mocked concurrency test is not evidence for a concurrency claim, and
this cycle's whole purpose is a concurrency claim: the resolver's guessing tiers let one
session's identity resolution be redirected by whatever OTHER session most recently wrote
the pointer, or whichever session's cache file happened to be newest, in a directory every
project's sessions share. This file spawns two independent `bin/lib/writ-session.py`
processes -- genuinely separate interpreters, not two calls in one test process -- against
ONE shared cache directory, and proves neither one's identity resolution can be hijacked by
the other's cache file, including one deliberately given a newer mtime (the exact tier-4
shape) and a stray cache left over from a THIRD, unrelated session (the exact hazard named
in plan.md: a prior test run left the pointer naming `tier-embed-directive-597898df`, a
session with no cache at all).

RED today: the resolver still has tiers 3 and 4, so a stray "newest" cache file in the
shared directory can win over env-resolved identity depending on timing, which is precisely
the bug. Per TEST-TDD-001: skeletons approved before implementation.

Never touches the real /tmp/writ-current-session (see test_session_id_resolver.py's
docstring for why): every process here is given its identity via $CLAUDE_SESSION_ID, which
this cycle keeps as tier 1, so no test in this file depends on the pointer's real, live,
machine-wide content.

SAFETY: `mode set` stamps cache["project_root"] from the SPAWNED PROCESS's cwd and clears
`<project_root>/.claude/gates/*.approved` there. Every subprocess below omits `cwd=`, so it
inherits pytest's OWN cwd -- which is why the autouse `sandbox_cwd` fixture is imported
(never referenced directly; importing it is what activates it for this module). Without it,
these real subprocess calls would run with this repo's own root as cwd and delete this
repo's own live gate artifacts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# ruff: noqa: F811 -- shared fixture is consumed by name (autouse) rather than as an arg.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "bin" / "lib" / "writ-session.py"


def _mode_set(cache_dir: Path, mode: str, *, claude_session_id: str) -> subprocess.CompletedProcess:
    """One real OS process: `writ-session.py mode set <mode>` with no explicit sid,
    resolving via $CLAUDE_SESSION_ID exactly as a human's bare `mode set work` would.
    No cwd= is passed, so this inherits the sandboxed cwd the autouse fixture set."""
    env = {**os.environ, "WRIT_CACHE_DIR": str(cache_dir), "CLAUDE_SESSION_ID": claude_session_id}
    env.pop("CLAUDE_JOB_DIR", None)
    return subprocess.run(
        [sys.executable, str(HELPER), "mode", "set", mode],
        env=env, capture_output=True, text=True, timeout=30,
    )


def _read_cache(cache_dir: Path, session_id: str) -> dict:
    with open(cache_dir / f"writ-session-{session_id}.json") as f:
        return json.load(f)


class TestTwoRealProcessesShareOneCacheDirWithoutCrossContamination:
    """Capability 1 + 2 + the isolation claim, at the process boundary."""

    def test_a_stray_newer_cache_from_an_unrelated_session_does_not_redirect_either_process(
        self, tmp_path
    ):
        """Seed the exact hazard: a cache file for a session neither process is, given a
        deliberately NEWER mtime than anything the two real writes below will get (the old
        tier 4 would have picked this one). Then run two real processes, each with its own
        $CLAUDE_SESSION_ID, and prove each one's write lands under its OWN session id."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        decoy = cache_dir / "writ-session-tier-embed-directive-597898df.json"
        decoy.write_text(json.dumps({"mode": "conversation"}))
        os.utime(decoy, (9_999_999_999, 9_999_999_999))  # far future: always "newest"

        proc_a = _mode_set(cache_dir, "review", claude_session_id="real-process-A")
        assert proc_a.returncode == 0, f"stdout={proc_a.stdout!r} stderr={proc_a.stderr!r}"

        proc_b = _mode_set(cache_dir, "debug", claude_session_id="real-process-B")
        assert proc_b.returncode == 0, f"stdout={proc_b.stdout!r} stderr={proc_b.stderr!r}"

        assert _read_cache(cache_dir, "real-process-A")["mode"] == "review"
        assert _read_cache(cache_dir, "real-process-B")["mode"] == "debug"
        # The decoy must be completely untouched by either real process.
        assert json.loads(decoy.read_text())["mode"] == "conversation"

    def test_process_b_running_after_process_a_does_not_alter_process_as_resolution(
        self, tmp_path
    ):
        """Process A writes first, becoming (under the old tier 4) the "newest" cache
        that a later, unrelated resolution might have preferred. Process B must still
        resolve to its OWN env id, and re-reading A's cache afterward must show A's mode
        unchanged -- two real OS processes, sequenced, sharing one real directory, neither
        able to see or move the other's identity."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        proc_a = _mode_set(cache_dir, "work", claude_session_id="sequenced-A")
        assert proc_a.returncode == 0, proc_a.stderr

        proc_b = _mode_set(cache_dir, "investigate", claude_session_id="sequenced-B")
        assert proc_b.returncode == 0, proc_b.stderr

        assert _read_cache(cache_dir, "sequenced-A")["mode"] == "work", (
            "B's real process must not have touched A's cache file"
        )
        assert _read_cache(cache_dir, "sequenced-B")["mode"] == "investigate"

    def test_a_real_process_with_no_resolvable_identity_refuses_even_beside_a_populated_directory(
        self, tmp_path
    ):
        """The refusal side, at the process boundary: a THIRD real process given no env
        signal at all must exit 2 rather than adopting one of the two sibling sessions'
        ids already sitting in the same directory from the tests above's shape."""
        cache_dir = tmp_path / "shared"
        cache_dir.mkdir()
        sibling = cache_dir / "writ-session-some-other-real-session.json"
        sibling.write_text(json.dumps({"mode": "work"}))
        os.utime(sibling, (9_999_999_998, 9_999_999_998))

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_SESSION_ID", "CLAUDE_JOB_DIR")}
        env["WRIT_CACHE_DIR"] = str(cache_dir)
        proc = subprocess.run(
            [sys.executable, str(HELPER), "mode", "set", "work"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 2, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert list(cache_dir.glob("writ-session-*.json")) == [sibling], (
            "a refused real process must create no new cache file of its own"
        )
