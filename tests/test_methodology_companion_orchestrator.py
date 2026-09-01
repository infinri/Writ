"""PSR-008 Finding 1: methodology companion must fire in orchestrator mode.

Context: PSR-008 surfaced 0 events with `query_source: "methodology"` despite
the agent running in Work mode. Root cause: writ-rag-inject.sh's orchestrator
short-circuit (`exit 0` at the IS_ORCHESTRATOR=true branch) bypasses the
methodology companion block entirely.

Fix contract (this test pins): when a session has both `mode=work` AND
`is_orchestrator=true`, the inject hook MUST still fire the methodology
companion query before short-circuiting. The status-line output and the
`exit 0` are preserved -- only the methodology block runs additionally.

This is checked structurally (the orchestrator branch of the hook source
references the methodology query) AND end-to-end (running the hook against
a seeded orchestrator cache produces a `query_source: "methodology"` line
in the friction log).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from writ.shared.logging import read_streams, resolve_project  # noqa: E402

# Exercises the router's cwd-based project-scope resolution to a tmp subdir;
# opt out of the autouse WRIT_FRICTION_LOG redirect so the rag_query telemetry
# routes to the split per-project streams under WRIT_LOG_ROOT (P1 router).
pytestmark = pytest.mark.no_friction_isolation


SKILL_DIR = str(Path(__file__).resolve().parent.parent)
HOOK = f"{SKILL_DIR}/hooks/scripts/writ-rag-inject.sh"


class TestOrchestratorMethodologyCompanionStructural:
    """Structural: the orchestrator branch must not short-circuit before the
    shared /prompt-bundle call that now carries both the always-on floor and
    the methodology companion (plan dfacff61, Decision 2).

    RE-KEYED (was: "the word 'methodology' appears anywhere in the branch").
    That guard passed for a branch that mentions methodology only in a
    comment, including the CURRENT, pre-fix branch, which says "methodology
    context" in a comment two lines above the `exit 0` that stops it from
    ever being delivered for the always-on floor. Per HARD CONSTRAINT
    (dedup-before-name-swap), the replacement is keyed on the MECHANISM that
    now guarantees delivery: the branch falls through past its own closing
    `fi` into the shared bundle call instead of returning early, not on a
    second name to grep for.

    MUTATION: restoring `exit 0` inside the `IS_ORCHESTRATOR` branch (the
    pre-fix shape, which short-circuited immediately after the hand-rolled
    companion call) turns this red. The OLD "methodology" substring guard
    would NOT have caught that exact mutation, because the word still
    appears in a comment two lines above the exit it is meant to catch.
    """

    def test_orchestrator_branch_has_no_early_exit(self) -> None:
        with open(HOOK) as f:
            body = f.read()

        # Locate the orchestrator branch: everything between the
        # `if [ "$IS_ORCHESTRATOR" = "true" ]; then` and its closing `fi`
        # before the next major block.
        m = re.search(
            r'if \[ "\$IS_ORCHESTRATOR" = "true" \]; then(.+?)\nfi\n',
            body,
            re.DOTALL,
        )
        assert m is not None, "could not locate orchestrator branch in hook source"
        branch_body = m.group(1)

        assert "exit 0" not in branch_body, (
            "the orchestrator branch still short-circuits before reaching the "
            "shared /prompt-bundle call, so a master would keep receiving zero "
            "always-on rules per turn regardless of what the branch's comments "
            "say. Branch body:\n" + branch_body[:1500]
        )


class TestOrchestratorMethodologyCompanionEndToEnd:
    """End-to-end: run the hook with a seeded orchestrator cache and a
    user prompt, verify the friction log gets a methodology rag_query."""

    def _ensure_aligned_daemon(self, cache_dir: str) -> bool:
        """Start (and realign) the daemon on WRIT_PORT against cache_dir.

        Mirrors test_fix2_cache_alignment: ensure-server.sh with WRIT_REALIGN_CACHE=1
        guarantees a daemon whose cache_dir matches where this test seeded, so the
        hook's HTTP session read sees the orchestrator cache. Returns True once
        /health answers, False if it never comes up (no Neo4j, etc.)."""
        import time
        import urllib.request

        from tests._daemon import _port

        ensure = Path(SKILL_DIR) / "scripts" / "ensure-server.sh"
        if not ensure.exists():
            return False
        env = {
            **os.environ,
            "WRIT_PORT": _port(),
            "WRIT_HOST": "localhost",
            "WRIT_CACHE_DIR": cache_dir,
            "WRIT_REALIGN_CACHE": "1",  # realign a leftover misaligned daemon
            "WRIT_NO_AUTOSTART": "",     # this explicit start IS the daemon
        }
        subprocess.run(["bash", str(ensure)], capture_output=True, text=True,
                       env=env, timeout=40, check=False)
        health = f"http://localhost:{_port()}/health"
        for _ in range(40):
            try:
                with urllib.request.urlopen(health, timeout=2):
                    return True
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        return False

    def _stop_daemon(self) -> None:
        """Stop the daemon this test started, by exact port (never the 8765
        interactive singleton)."""
        from tests._daemon import _port

        subprocess.run(["pkill", "-f", f"writ serve --port {_port()}"],
                       capture_output=True)

    def _seed_orchestrator_cache(
        self, cache_dir: str, session_id: str
    ) -> None:
        path = os.path.join(cache_dir, f"writ-session-{session_id}.json")
        with open(path, "w") as f:
            json.dump(
                {
                    "mode": "work",
                    "is_orchestrator": True,
                    "is_subagent": False,
                    "current_phase": "implementation",
                    "loaded_rule_ids": [],
                    "loaded_rule_ids_by_phase": {},
                    "remaining_budget": 8000,
                    "context_percent": 0,
                    "queries": 0,
                    "files_written": [],
                    "loaded_rules": [],
                },
                f,
            )

    def test_orchestrator_fires_methodology_companion(
        self, tmp_path
    ) -> None:
        """End-to-end: invoke the hook against the LIVE server with a
        seeded orchestrator cache. The hook delegates session reads to
        the running Writ server via HTTP, so the cache must live in
        the server's CACHE_DIR (default tempfile.gettempdir()). Use a
        unique session ID and clean up after.

        Pass criterion: the project's P1 metrics stream gets at least one
        rag_query with query_source=methodology."""
        import uuid
        sid = f"orch-method-e2e-{uuid.uuid4().hex[:8]}"
        # Seed at the server's cache dir (writ-session.py:58 resolution), not
        # the test's tmp_path. Hardcoding "/tmp" missed the dir under TMPDIR.
        server_cache_dir = os.environ.get("WRIT_CACHE_DIR", tempfile.gettempdir())
        cache_path = os.path.join(server_cache_dir, f"writ-session-{sid}.json")
        self._seed_orchestrator_cache(server_cache_dir, sid)

        # This end-to-end path needs a LIVE daemon aligned to server_cache_dir on
        # WRIT_PORT: the hook reads the seeded orchestrator cache over HTTP. It used
        # to get one for free because writ-rag-inject.sh auto-started a daemon when
        # none answered -- but that auto-start is a leak (the daemon outlived the
        # run), so the suite now forces WRIT_NO_AUTOSTART=1. Start an aligned daemon
        # explicitly and stop it in finally, rather than depend on the leak.
        started_daemon = self._ensure_aligned_daemon(server_cache_dir)
        if not started_daemon:
            pytest.skip("could not start an aligned daemon on the test port")

        try:
            # Project root the hook runs in; a .git marker makes the router's
            # project-scope resolution derive a stable identity from this cwd.
            project_root = tmp_path / "proj"
            project_root.mkdir()
            (project_root / ".git").mkdir()  # marker

            envelope = json.dumps({
                "session_id": sid,
                "prompt": (
                    "I want to implement a small Python function that takes "
                    "a list of integers and returns their sum. Please plan "
                    "this carefully and write tests first."
                ),
            })

            result = subprocess.run(
                ["bash", HOOK],
                input=envelope,
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=15,
            )
        finally:
            # Always clean up the seeded cache and the daemon this test started.
            try:
                os.unlink(cache_path)
            except FileNotFoundError:
                pass
            self._stop_daemon()
        # Hook must not error out.
        assert result.returncode == 0, (
            f"hook returned {result.returncode}; "
            f"stderr={result.stderr[:1000]}"
        )

        # Drain the session event buffer first, which is what a real turn's Stop hook
        # does. Since the orchestrator branch stopped hand-rolling its own companion
        # call and started using the shared /prompt-bundle path, these rows are
        # BUFFERED (writ_friction_buffer_append) rather than appended synchronously, so
        # reading the stream without a drain measures the buffer, not the router.
        subprocess.run(
            [sys.executable, os.path.join(SKILL_DIR, "bin", "lib", "writ-flush-events.py"), sid],
            capture_output=True, text=True, cwd=str(project_root), timeout=60, check=False,
        )

        # Inspect the router's metrics stream (rag_query -> metrics) for the
        # project scope derived from the hook's cwd, for a methodology rag_query.
        events = read_streams(resolve_project(str(project_root)), ["metrics"])

        methodology = [
            e for e in events
            if e.get("event") == "rag_query"
            and e.get("query_source") == "methodology"
        ]

        assert methodology, (
            "no rag_query with query_source=methodology in orchestrator "
            f"hook run. All events:\n{json.dumps(events, indent=2)}"
        )
