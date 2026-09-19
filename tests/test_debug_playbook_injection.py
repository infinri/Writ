"""Increment 2: surface the systematic-debugging playbook at `mode set debug`.

When a session is in debug mode, writ-rag-inject.sh must fire a second /query
restricted to Playbook+Technique nodes (domain=process) keyed on the user's
prompt, so PBK-PROC-DEBUG-001 (and the diagnose-* classifiers from Increment 3)
surface against the actual symptom. This mirrors the work-mode methodology
companion at writ-rag-inject.sh:749, parameterized by mode.

Contract pinned here:
1. The /query retrieval contract returns PBK-PROC-DEBUG-001 for a debug symptom
   (integration, real server -- unprovable by mocks; TEST-INT-001).
2. End-to-end: a debug-mode hook run logs a rag_query with
   query_source='debug-playbook'.
3. No regression: a work-mode hook run still logs query_source='methodology'.
4. The debug query does NOT fire in conversation mode (TEST-EDGE-001).
5. Structural: the hook source's methodology block references the debug path.

Live-hook tests run against a per-module isolated daemon (tests/_prompt_turn.py),
never a shared address resolved at import time. A turn is TWO subprocesses, run
back-to-back from the same project cwd: the real UserPromptSubmit hook, then the
real Stop hook, because production only ever drains a turn's buffered rag_query
rows into the `metrics` stream from the Stop hook. Reading the stream after only
the first hook is one hook-invocation too early and prints `events: []` for
every mode; that is what the previous skip-on-unreachable-server form of this
file was hiding, not a genuine skip.

W2 (server package split, branch refactor/w2-server-split): the structural
assertion in TestDebugPlaybookStructural reads via writ_server_source()
(tests/conftest.py), which is layout-agnostic -- it scans every *.py under
writ/server/ if that directory exists (post-split: this content is expected in
routes/query.py), else the single writ/server.py file (pre-split).
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests.conftest import writ_server_source
from tests._prompt_turn import run_prompt_turn, seed_session_cache
from tests._prompt_turn import isolated_prompt_daemon  # noqa: F401

# Exercises the router's cwd-based project-scope resolution to a tmp subdir;
# opt out of the autouse WRIT_FRICTION_LOG redirect so rag_query telemetry
# routes to the split per-project streams under WRIT_LOG_ROOT (P1 router).
pytestmark = pytest.mark.no_friction_isolation

DEBUG_SYMPTOM = (
    "A unit test is failing unexpectedly after my change and I need to debug "
    "the root cause -- help me investigate this systematically."
)


def _triage_message(
    expected_source: str, events: list[dict], stdout: str, queries_delta: int,
) -> str:
    """One failure message that carries every triage rung, so a reader tells a
    DRAIN failure from a RETRIEVAL failure without re-running anything.

    The discriminator comes from the mechanism, not from a guess: a drain
    problem cannot be selective, because it takes the whole list (every
    query_source, plus the `hook_execution` rows), and its symptom is
    `events == []`. A retrieval problem is ONE source missing from a NON-EMPTY
    list. The `queries` delta then splits the empty case in two: zero means the
    turn never reached this daemon at all (transport), positive means the daemon
    served it and nothing landed (drain)."""
    rag_events = [e for e in events if e.get("event") == "rag_query"]
    sources = sorted({e.get("query_source") for e in rag_events})
    return (
        f"no rag_query with query_source={expected_source!r} in the drained "
        f"turn.\n"
        f"rung 1 (transport vs drain): events empty={not events}, "
        f"daemon queries delta={queries_delta}\n"
        f"rung 2 ('broad' present -- retrieval + drain both worked): "
        f"{'broad' in sources}\n"
        f"rung 3 (companion header rendered on stdout): "
        f"{'[Writ: methodology companion]' in stdout}\n"
        f"query_source values seen: {sources}\n"
        f"events:\n{json.dumps(events, indent=2)}\n"
        f"stdout:\n{stdout}"
    )


class TestDebugPlaybookRetrievalContract:
    """The /query mechanism the hook relies on surfaces the debug playbook."""

    def test_query_surfaces_debug_playbook(self, isolated_prompt_daemon) -> None:
        """Reddened by the debug symptom no longer ranking PBK-PROC-DEBUG-001
        into the top-k Playbook/Technique hits for domain=process -- a content
        or ranking regression in the graph, not a harness defect."""
        req = urllib.request.Request(
            f"{isolated_prompt_daemon['base_url']}/query",
            data=json.dumps({
                "query": DEBUG_SYMPTOM,
                "node_types": ["Playbook", "Technique"],
                "domain": "process",
                "budget_tokens": 2000,
                "top_k": 6,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        ids = [r.get("rule_id") for r in body.get("rules", [])]
        assert "PBK-PROC-DEBUG-001" in ids, (
            f"debug playbook must surface for a debug symptom; got {ids}"
        )


class TestDebugPlaybookInjectionEndToEnd:
    """Run the real two-hook turn with a seeded mode; assert the drained stream."""

    def test_debug_mode_fires_debug_playbook_query(
        self, isolated_prompt_daemon, tmp_path,
    ) -> None:
        """Reddened by the Stop hook draining a buffer under a different
        WRIT_CACHE_DIR or a different cwd than the UserPromptSubmit hook used
        (the drain then finds nothing), or by removing the Stop-hook run from
        the turn entirely (the drain never happens at all, events == [])."""
        daemon = isolated_prompt_daemon
        sid = f"debug-pbk-e2e-{uuid.uuid4().hex[:8]}"
        cache_path = seed_session_cache(daemon["health"]["cache_dir"], sid, "debug")
        try:
            events, stdout, queries_delta = run_prompt_turn(
                daemon, tmp_path, sid, DEBUG_SYMPTOM,
            )
        finally:
            try:
                os.unlink(cache_path)
            except FileNotFoundError:
                pass
        debug_q = [
            e for e in events
            if e.get("event") == "rag_query"
            and e.get("query_source") == "debug-playbook"
        ]
        assert debug_q, _triage_message("debug-playbook", events, stdout, queries_delta)

    def test_work_mode_still_fires_methodology(
        self, isolated_prompt_daemon, tmp_path,
    ) -> None:
        """Regression oracle: parameterizing the block by mode must not break
        work mode. Reddened the same way as the debug case above: a Stop hook
        that drains the wrong buffer, or one that never runs, prints
        events == [] here too."""
        daemon = isolated_prompt_daemon
        sid = f"work-method-e2e-{uuid.uuid4().hex[:8]}"
        cache_path = seed_session_cache(
            daemon["health"]["cache_dir"], sid, "work", current_phase="implementation",
        )
        try:
            events, stdout, queries_delta = run_prompt_turn(
                daemon, tmp_path, sid,
                "Implement a function that sums a list of integers; plan and "
                "write tests first please.",
            )
        finally:
            try:
                os.unlink(cache_path)
            except FileNotFoundError:
                pass
        method_q = [
            e for e in events
            if e.get("event") == "rag_query"
            and e.get("query_source") == "methodology"
        ]
        assert method_q, _triage_message("methodology", events, stdout, queries_delta)

    def test_conversation_mode_does_not_fire_debug_playbook(
        self, isolated_prompt_daemon, tmp_path,
    ) -> None:
        """The point of this cycle: an absent debug-playbook row is asserted
        alongside its companion methodology-conversation row from the SAME
        channel, one dict key apart (writ/server/routes/query.py:349-353), so
        the absence cannot be a dead channel wearing a correct decline's face.
        Reddened by either: (a) a regression that makes the debug arm fire
        outside debug mode, or (b) a channel-3 failure (mode unresolved, the
        remaining_budget > 600 gate, or a companion error) that would ALSO
        remove the companion row this test requires to be present."""
        daemon = isolated_prompt_daemon
        sid = f"conv-e2e-{uuid.uuid4().hex[:8]}"
        cache_path = seed_session_cache(
            daemon["health"]["cache_dir"], sid, "conversation",
        )
        try:
            events, stdout, queries_delta = run_prompt_turn(
                daemon, tmp_path, sid, DEBUG_SYMPTOM,
            )
        finally:
            try:
                os.unlink(cache_path)
            except FileNotFoundError:
                pass
        debug_q = [
            e for e in events
            if e.get("event") == "rag_query"
            and e.get("query_source") == "debug-playbook"
        ]
        companion_q = [
            e for e in events
            if e.get("event") == "rag_query"
            and e.get("query_source") == "methodology-conversation"
        ]
        assert not debug_q, (
            f"debug-playbook query must NOT fire in conversation mode; "
            f"events:\n{json.dumps(events, indent=2)}"
        )
        assert companion_q, (
            "conversation mode must still fire its own methodology-conversation "
            "companion row; without it, this absence is indistinguishable from "
            "channel 3 having died entirely.\n"
            + _triage_message("methodology-conversation", events, stdout, queries_delta)
        )


class TestTheTurnIsServedByThisModulesOwnDaemon:
    """Positive proof of daemon identity, read out of the destination the
    dependency itself reports -- never inferred from the port the fixture set."""

    def test_the_daemons_reported_cache_dir_is_under_the_fixtures_own_tmp_root(
        self, isolated_prompt_daemon, tmp_path_factory,
    ) -> None:
        """Reddened by dropping WRIT_CACHE_DIR from the harness env: the
        daemon would then resolve its own default cache dir (not this
        fixture's throwaway one), so neither the equality nor the containment
        check below would hold."""
        daemon = isolated_prompt_daemon
        reported = Path(daemon["health"]["cache_dir"])
        assert reported == Path(daemon["cache_dir"])
        assert reported.is_relative_to(tmp_path_factory.getbasetemp()), (
            f"{reported} is not under this fixture's own tmp root "
            f"{tmp_path_factory.getbasetemp()}"
        )

    def test_a_turn_increments_queries_in_the_seeded_session_file_in_that_dir(
        self, isolated_prompt_daemon, tmp_path,
    ) -> None:
        """The counter only the SERVER PROCESS writes
        (writ/server/routes/query.py:318-324, 366-369, cmd_update
        --inc-queries), into its own cache dir. Reddened by pointing the
        harness at any other daemon (a dropped WRIT_SOCKET, a wrong
        WRIT_PORT, or a dropped WRIT_CACHE_DIR): no other process on the
        machine writes into this fixture's own throwaway cache dir, so the
        session file would never move and the delta would read zero."""
        daemon = isolated_prompt_daemon
        sid = f"isolation-proof-{uuid.uuid4().hex[:8]}"
        cache_path = seed_session_cache(daemon["health"]["cache_dir"], sid, "debug")
        try:
            before = json.loads(Path(cache_path).read_text())["queries"]
            events, stdout, queries_delta = run_prompt_turn(
                daemon, tmp_path, sid, DEBUG_SYMPTOM,
            )
            after = json.loads(Path(cache_path).read_text())["queries"]
        finally:
            try:
                os.unlink(cache_path)
            except FileNotFoundError:
                pass
        assert after - before >= 1, (
            f"session file {cache_path} did not gain a queries increment "
            f"across one turn (before={before}, after={after}).\n"
            + _triage_message("debug-playbook", events, stdout, queries_delta)
        )
        assert queries_delta == after - before, (
            "run_prompt_turn's own reported queries delta must agree with "
            f"the session file's before/after read: harness delta="
            f"{queries_delta}, file delta={after - before}"
        )


class TestDebugPlaybookStructural:
    """Lint-level guard: the methodology block must handle the debug path so a
    future regression that drops it is caught without a live server."""

    def test_methodology_block_references_debug_playbook(self) -> None:
        # #8: the debug -> debug-playbook query_source map moved into the
        # /prompt-bundle endpoint; the hook delivers the rendered bundle.
        server = writ_server_source()
        assert "debug-playbook" in server, (
            "the /prompt-bundle endpoint must map debug -> 'debug-playbook'"
        )
        # 1.7 CUTOVER: methodology is delivered by /methodology-companion now (the
        # companion serves the debug FLOOR from node-declared floor_modes), not by
        # a mode->node_type /query. Lint guards: the debug case exists and the hook
        # posts to the companion endpoint (behavioral firing proven by the e2e test).
        assert re.search(r'"debug":\s*"debug-playbook"', server), (
            "the endpoint must keep a debug-mode query_source case"
        )
        assert "methodology_companion" in server, (
            "the endpoint must deliver methodology via the methodology_companion handler"
        )
