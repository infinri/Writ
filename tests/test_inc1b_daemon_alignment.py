"""INC-1b: daemon cache-desync test hardening (NRV-0 follow-up).

`bin/lib/common.sh::_writ_session` mutates session state in the DAEMON first (curl), falling
back to the file helper. Live-hook tests set/read state via writ-session.py directly (file).
When the daemon's cache_dir diverges from the tests' computed dir, a hook's write (daemon
dir) and a test's read (file dir) hit different files -> the reset "succeeds" but the test
reads a stale value -> en-masse live-hook/session failure.

The fix (tests/_daemon.py + conftest pytest_sessionstart) aligns the daemon to the tests'
expected dir at suite start. This file pins the contract:
  - classify_daemon_alignment(daemon_dir, expected) -> 'down' | 'aligned' | 'diverged' (pure)
  - ensure_daemon_aligned() realigns a diverged daemon (idempotent)
  - end-to-end: a daemon-routed reset is visible to a file-helper read once aligned.

The classifier test is pure (always runs). Live tests skip only when no daemon is reachable.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from tests._daemon import (
    _isolated_health,
    _pids_serving_port,
    classify_daemon_alignment,
    daemon_cache_dir,
    ensure_daemon_aligned,
    expected_cache_dir,
)
from tests._hook_runner import count_requests, hook_env, isolated_daemon, seed_session_cache

SKILL_DIR = Path(__file__).resolve().parent.parent
POSTCOMPACT_HOOK = SKILL_DIR / "hooks" / "scripts" / "writ-postcompact.sh"


# --- 1. Alignment classifier (pure, always runs) -----------------------------


class TestClassifier:
    def test_no_daemon_is_down(self) -> None:
        assert classify_daemon_alignment(None, "/tmp/claude-1001") == "down"

    def test_matching_dirs_aligned(self) -> None:
        assert classify_daemon_alignment("/tmp/claude-1001", "/tmp/claude-1001") == "aligned"

    def test_differing_dirs_diverged(self) -> None:
        assert classify_daemon_alignment("/tmp", "/tmp/claude-1001") == "diverged"

    def test_diverged_is_never_aligned(self) -> None:
        # Regression guard: a desynced daemon must never read as 'aligned'.
        assert classify_daemon_alignment("/tmp", "/tmp/claude-1001") != "aligned"


# --- 2. ensure_daemon_aligned brings the daemon into alignment (live) --------
#
# OWNED DAEMON, one HTTP call each (Decision 4/5, plan.md
# 2412ba38-51e1-4b73-895b-7b240a3c21d3, module 9): WRIT_PORT/WRIT_CACHE_DIR are
# monkeypatched at an owned, ALREADY-aligned daemon so ensure_daemon_aligned()
# takes its idempotent arm, and both tests prove NO restart happened rather
# than merely that the return value says "aligned" -- a helper satisfied by
# restarting the daemon and getting lucky would still return "aligned".
#
# A LOAD-BEARING CONSTRAINT: ensure_daemon_aligned() is NEVER called in the
# diverged state here. It runs stop-server.sh then ensure-server.sh against
# whatever WRIT_PORT names, which would kill this fixture's own daemon and
# start an UNOWNED one on that port the module's stop verdict knows nothing
# about. The diverged case below calls the pure classifier over a live probe,
# never the realigner.


class TestEnsureAligned:
    def _pids(self, daemon: dict) -> set[int]:
        measured, pids = _pids_serving_port(daemon["port"])
        assert measured, f"process table could not be read for port {daemon['port']}"
        return set(pids)

    def test_daemon_aligned_after_ensure(self, isolated_daemon, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_PORT", str(isolated_daemon["port"]))
        monkeypatch.setenv("WRIT_CACHE_DIR", isolated_daemon["cache_dir"])
        # This module's subject is CACHE-DIR alignment; the suite's own autouse
        # per-test WRIT_FRICTION_LOG would otherwise disagree with whatever
        # friction log the isolated daemon resolved at its OWN start, tripping
        # ensure_daemon_aligned()'s separate friction-alignment arm and taking
        # the destructive restart path this class's docstring forbids.
        monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
        before_startup = isolated_daemon["health"].get("startup_time")
        before_pids = self._pids(isolated_daemon)

        state = ensure_daemon_aligned()

        assert state == "aligned", f"an aligned owned daemon must report 'aligned'; got {state!r}"
        assert daemon_cache_dir() == expected_cache_dir()
        # THE IN-RUN POSITIVE that no restart happened.
        after_health = _isolated_health(isolated_daemon["port"])
        assert after_health is not None, "the owned daemon stopped answering /health mid-test"
        assert after_health.get("startup_time") == before_startup, (
            "ensure_daemon_aligned() must not restart an already-aligned daemon "
            "(startup_time changed)"
        )
        assert self._pids(isolated_daemon) == before_pids, (
            "ensure_daemon_aligned() must not restart an already-aligned daemon "
            "(process identity changed)"
        )

    def test_ensure_is_idempotent(self, isolated_daemon, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_PORT", str(isolated_daemon["port"]))
        monkeypatch.setenv("WRIT_CACHE_DIR", isolated_daemon["cache_dir"])
        monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
        before_startup = isolated_daemon["health"].get("startup_time")
        before_pids = self._pids(isolated_daemon)

        ensure_daemon_aligned()
        state = ensure_daemon_aligned()

        assert state == "aligned"
        assert daemon_cache_dir() == expected_cache_dir()
        after_health = _isolated_health(isolated_daemon["port"])
        assert after_health is not None
        assert after_health.get("startup_time") == before_startup
        assert self._pids(isolated_daemon) == before_pids
    # MUTATION: making classify_daemon_alignment return "aligned" for unequal
    # dirs, or having ensure_daemon_aligned() restart on the aligned arm,
    # reddens the startup_time/pid positives above even though the bare
    # "aligned" return would still look right -- the anti-masking contract
    # classify_daemon_alignment's own docstring claims.

    def test_diverged_cache_dir_classifies_diverged_against_a_real_daemon(
        self, isolated_daemon, tmp_path, monkeypatch
    ) -> None:
        """ONE NEW ASSERTION, the property TestClassifier can only make about
        literals: with WRIT_CACHE_DIR monkeypatched to a DIFFERENT dir,
        classify_daemon_alignment(daemon_cache_dir(), expected_cache_dir()) is
        "diverged" against a REAL daemon. Calls the pure classifier over a
        live probe, never ensure_daemon_aligned() -- see the class docstring's
        load-bearing constraint.
        """
        monkeypatch.setenv("WRIT_PORT", str(isolated_daemon["port"]))
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "not-the-daemons-dir"))
        state = classify_daemon_alignment(daemon_cache_dir(), expected_cache_dir())
        assert state == "diverged", (
            f"a daemon whose cache_dir differs from expected_cache_dir() must "
            f"classify as 'diverged', never 'aligned'; got {state!r}"
        )


# --- 3. End-to-end: daemon-routed reset is visible to a file-helper read -----


class TestCrossPathReset:
    """Reproduces the exact failure: a hook resets via the DAEMON, a test
    reads via the file helper. Once the daemon this test owns is the one both
    sides talk to, the read must see the reset.

    OWNED DAEMON, one hook subprocess (Decision 4/5, module 9). The in-run
    positive is the delta on the session-mutation route
    bin/lib/common.sh::_writ_session curls (POST /session/{id}/reset-after-
    compaction), asserted at least one: with a zero delta the local-subprocess
    fallback produced the 8000 and this test would have measured nothing.
    """

    def test_postcompact_reset_visible_after_alignment(self, isolated_daemon) -> None:
        sid = f"inc1b-{uuid.uuid4().hex[:8]}"
        cache_dir = isolated_daemon["health"]["cache_dir"]
        cache_path = seed_session_cache(cache_dir, sid, "work")
        # Deplete the budget (a field PostCompact resets to
        # DEFAULT_SESSION_BUDGET=8000). A fresh seed already starts at 8000.
        seeded = json.loads(Path(cache_path).read_text())
        seeded["remaining_budget"] = 3000
        Path(cache_path).write_text(json.dumps(seeded))

        pattern = f'"POST /session/{sid}/reset-after-compaction HTTP'
        before = count_requests(isolated_daemon, pattern)
        proc = subprocess.run(
            ["bash", str(POSTCOMPACT_HOOK)],
            input=json.dumps({"session_id": sid, "event": "compact"}),
            capture_output=True, text=True, cwd=str(SKILL_DIR), timeout=15,
            env=hook_env(isolated_daemon),
        )
        after = count_requests(isolated_daemon, pattern)
        assert proc.returncode == 0, f"postcompact hook failed: {proc.stderr[:300]!r}"
        assert after == before + 1, (
            f"the postcompact reset must reach the daemon's session-mutation "
            f"route exactly once; before={before}, after={after}. A zero delta "
            f"means the local-subprocess fallback produced the result instead, "
            f"which is the original INC-1b failure this test exists to catch."
        )

        # Read back BOTH from the DAEMON (GET /session/{id}, the same route
        # session_read wraps) and from the FILE in the dir the daemon reports:
        # the whole property is that the reset went through the daemon and was
        # visible to a file read, so either read alone would miss half of it.
        import urllib.request

        with urllib.request.urlopen(
            f"{isolated_daemon['base_url']}/session/{sid}", timeout=10
        ) as resp:
            from_daemon = json.loads(resp.read())
        assert from_daemon.get("remaining_budget") == 8000, (
            f"the daemon's OWN session read must report remaining_budget=8000 "
            f"after the reset; got {from_daemon.get('remaining_budget')!r}"
        )
        reread = json.loads(Path(cache_path).read_text())
        assert reread.get("remaining_budget") == 8000, (
            "daemon-routed postcompact reset was not visible to a file read in "
            f"the dir the daemon reports (got {reread.get('remaining_budget')!r})"
        )
    # MUTATION: pointing the harness at a dead port takes the session-route
    # delta to 0 and reddens this test.
