"""POL-5b-3a: collapse redundant cache reads / JSON parses in writ-rag-inject.sh.

writ-rag-inject.sh is the UserPromptSubmit RAG bridge -- it fires on every user
turn, the hottest hook in the system. Four behavior-preserving collapses:

  1. main-path `$CACHE` 4-field extraction (LOADED_RULE_IDS / REMAINING_BUDGET /
     PREFER_RULE_IDS / DETECTED_DOMAIN): 4 `python3` spawns -> 1 (sed-split).
  2. orchestrator path: direct-file-read for is_orchestrator + a second
     `_writ_session read` for the status line -> 1 cache read, reused.
  3. escalation block's second `_writ_session read` -> reuse the main `$CACHE`.
  4. META `rule_ids`+`cost` double-parse at 3 sites -> 1 spawn each.

Source-shape guards prove the redundancy is gone; behavioral guards run the hook
via bash against a daemon this module owns (tests/_hook_runner.py), never a
shared address resolved at import time.

TRIAGE CLUSTER 2: the four behavioral tests below used to be gated by an
import-time `skipif` and, with the mark deleted alone, two of them would have
read green against a DEAD daemon: `writ-rag-inject.sh` prints `[Writ: server
unavailable, proceeding without rules]` with no daemon at all (hook line 569),
and the old assertions accepted any stdout containing `[Writ:`, which that
degrade line satisfies. Every assertion below is therefore either a positive
content check with the degrade lines asserted ABSENT, or a request-count delta
read from the daemon's own access log -- the one thing a source-text scan
structurally cannot see, and the reason the corresponding TestRedundancyRemoved
guards below are the weaker half of this module and not a substitute for these.

RED until writ-rag-inject.sh is refactored (TestRedundancyRemoved) and RED
until tests/_hook_runner.py exists (every behavioral test below).
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from tests._hook_runner import (
    count_requests,
    hook_env,
    isolated_hook_daemon,  # noqa: F401  (imported for pytest fixture discovery)
    run_hook,
    seed_session_cache,
    verify_seeded_mode,
)

SKILL_DIR = Path(__file__).resolve().parent.parent
HOOK = SKILL_DIR / "hooks" / "scripts" / "writ-rag-inject.sh"
SRC = HOOK.read_text()


# --------------------------------------------------------------------------- #
# helpers (test-local; not shared harness state)
# --------------------------------------------------------------------------- #
def _no_py_crash(r) -> None:
    assert "Traceback" not in r.stderr, f"python traceback in hook stderr:\n{r.stderr[:500]}"
    assert "SyntaxError" not in r.stderr, f"python SyntaxError in hook stderr:\n{r.stderr[:500]}"


def _req(method: str, path: str) -> str:
    """A precise uvicorn-access-log substring: the quoted request line up to (not
    including) the HTTP version, so `/session/{sid}` cannot accidentally match a
    longer sibling path like `/session/{sid}/mode`."""
    return f'"{method} {path} HTTP'


def _queries_count(cache_dir: str, sid: str) -> int:
    """The `queries` counter read out of the session file in the daemon's own cache
    dir. Only the SERVER PROCESS writes it (writ/server/routes/query.py), so a
    delta across one turn positively identifies which daemon served that turn.
    Mirrors tests/_prompt_turn.py's private helper of the same name and contract;
    this module's own copy stays private because only one test here needs it."""
    path = Path(cache_dir) / f"writ-session-{sid}.json"
    try:
        return int(json.loads(path.read_text())["queries"])
    except (OSError, ValueError, TypeError, KeyError):
        return 0


def _new_project(tmp_path: Path) -> str:
    """A throwaway project dir with a `.git` marker, so detect_project_root stops
    inside it rather than climbing out to the real checkout."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    return str(root)


# --------------------------------------------------------------------------- #
# 1. source-shape guards -- the redundancy is gone
# --------------------------------------------------------------------------- #
class TestRedundancyRemoved:
    def test_cache_read_count_is_one(self) -> None:
        # A1: was 3 daemon round-trips (standalone `mode get`, orchestrator read,
        # main read), then 2 after POL-5b-3a; now ONE `_writ_session read` whose
        # $CACHE is reused everywhere (orchestrator branch via the CACHE_DATA
        # alias, main path, escalation, backward-context). mode + is_orchestrator
        # derive from it via the jq-first parsed_field/parsed_bool helpers.
        # Count the command substitution, not bare comment mentions.
        n = SRC.count("$(_writ_session read")
        assert n == 1, (
            f"expected exactly 1 `_writ_session read` call (single shared read); found {n}"
        )

    def test_orchestrator_direct_file_read_gone(self) -> None:
        # the shell-interpolated direct cache-file open used only by the old
        # is_orchestrator check; the other two `writ-session-{session_id}` uses
        # are python-arg style and stay.
        assert "writ-session-${SESSION_ID}.json" not in SRC, (
            "is_orchestrator must derive from a shared `_writ_session read`, "
            "not a direct cache-file open"
        )

    def test_main_channels_via_single_prompt_bundle(self) -> None:
        # #8 superseded POL-5b-3a's "collapse to one parse": the broad/always-on/
        # methodology channels moved into ONE warm /prompt-bundle call. The endpoint
        # reads the cache + renders, so the main-path `$CACHE` 4-field parse is GONE
        # from the hook entirely (not just collapsed).
        assert SRC.count("/prompt-bundle") >= 1, "main channels must route through /prompt-bundle"
        assert 'echo "$CACHE" | python3' not in SRC, (
            "main-path $CACHE field-parse must be gone -- the endpoint reads the cache"
        )

    def test_main_meta_parse_moved_server_side(self) -> None:
        # The broad /query META (rule_ids+cost) is parsed + applied server-side; the
        # hook gets the rendered text + meta from the bundle instead.
        assert 'echo "$META_JSON" | parse_writ_meta' not in SRC, (
            "broad META is handled server-side; no hook-side parse_writ_meta"
        )

    def test_methodology_meta_parse_moved_server_side(self) -> None:
        assert 'echo "$METHOD_META_JSON" | parse_writ_meta' not in SRC, (
            "methodology-companion META is handled server-side; no hook-side parse_writ_meta"
        )

    def test_orchestrator_meta_parse_moved_server_side(self) -> None:
        """WAS test_orchestrator_meta_double_parse_collapsed, which asserted the
        orchestrator companion's META was parsed EXACTLY ONCE (it had been parsed
        twice; POL-5b-3a collapsed it to one).

        Plan dfacff61 deleted the hand-rolled orchestrator companion outright: a
        master now goes through the shared /prompt-bundle call like every other
        session, and the endpoint parses and applies the companion META
        server-side. So the count is zero, not one, and the redundancy the old
        assertion guarded cannot recur by any edit to this hook, because there is
        no hook-side parse left to duplicate.

        Re-keyed to the state that actually holds rather than deleted outright:
        an equality-to-one pin would now have to be an equality-to-zero pin, and
        this says WHY zero is right. It sits beside its two siblings above, which
        make the same "moved server-side" claim for the broad and methodology
        channels.
        """
        assert SRC.count('echo "$ORCH_METHOD_META_JSON" | parse_writ_meta') == 0, (
            "the hand-rolled orchestrator companion META parse is back; the "
            "master must use the shared /prompt-bundle path, which parses and "
            "applies that META server-side"
        )
        assert "ORCH_METHOD_META_JSON" not in SRC, (
            "the orchestrator companion's hook-side META variable is back"
        )


# --------------------------------------------------------------------------- #
# 2. behavioral guards -- nothing broke (module-owned isolated daemon)
#
# Four functions, four different paths: nothing honest to collapse. Every
# absence assertion below (the degrade lines, the zero-request delta) carries a
# companion positive from the same run, per the plan this module implements.
# --------------------------------------------------------------------------- #
def test_work_mode_runs_past_request_gate(isolated_hook_daemon, tmp_path) -> None:
    """Mutation: an empty `always_on_block` returned from
    writ/retrieval/prompt_bundle.py reddens the ALWAYS-ACTIVE RULES assertion
    while `rc` stays 0 -- the reason the request-count and queries-counter
    assertions travel alongside it rather than standing in for it."""
    daemon = isolated_hook_daemon
    cache_dir = daemon["health"]["cache_dir"]
    sid = f"test-5b3a-{uuid.uuid4().hex[:8]}"
    seed_session_cache(cache_dir, sid, "work")
    verify_seeded_mode(daemon, sid, "work")

    env = hook_env(daemon)
    cwd = _new_project(tmp_path)
    envelope = json.dumps({
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
        "prompt": "How do I add a new controller endpoint with input validation",
    })

    before_bundle = count_requests(daemon, _req("POST", "/prompt-bundle"))
    before_queries = _queries_count(cache_dir, sid)
    r = run_hook(HOOK, envelope, env=env, cwd=cwd, timeout=25)
    after_bundle = count_requests(daemon, _req("POST", "/prompt-bundle"))
    after_queries = _queries_count(cache_dir, sid)

    assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
    _no_py_crash(r)
    assert "ALWAYS-ACTIVE RULES" in r.stdout, (
        f"work-mode hook must inject the always-on block; stdout={r.stdout[:400]!r}"
    )
    assert "server unavailable" not in r.stdout, (
        f"a degraded run (no-daemon fallback) cannot read as this test's green; stdout={r.stdout[:400]!r}"
    )
    assert "query failed" not in r.stdout, (
        f"a degraded run (bundle error fallback) cannot read as this test's green; stdout={r.stdout[:400]!r}"
    )
    assert after_bundle - before_bundle == 1, (
        f"expected exactly one POST /prompt-bundle for this turn; delta={after_bundle - before_bundle}"
    )
    assert after_queries > before_queries, (
        "the daemon's own `queries` counter for this session did not increase, so the "
        "daemon that served this turn cannot be confirmed as the one whose cache was seeded"
    )


def test_orchestrator_status_line(isolated_hook_daemon, tmp_path) -> None:
    """Mutation: adding `exit 0` at the end of the orchestrator branch (hook line
    ~510) reddens the POST /prompt-bundle delta -- the exact regression hook
    lines 502-509 record as a fix (a master used to return before the always-on
    floor). Dropping `mode=` from the status-line f-string reddens the content
    assertion."""
    daemon = isolated_hook_daemon
    cache_dir = daemon["health"]["cache_dir"]
    sid = f"test-5b3a-{uuid.uuid4().hex[:8]}"
    seed_session_cache(cache_dir, sid, "work", is_orchestrator=True)
    verify_seeded_mode(daemon, sid, "work")

    env = hook_env(daemon)
    cwd = _new_project(tmp_path)
    envelope = json.dumps({
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Plan the refactor of the session module into submodules",
    })

    before_bundle = count_requests(daemon, _req("POST", "/prompt-bundle"))
    r = run_hook(HOOK, envelope, env=env, cwd=cwd, timeout=25)
    after_bundle = count_requests(daemon, _req("POST", "/prompt-bundle"))

    assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
    _no_py_crash(r)
    assert "[Writ: mode=work, phase=" in r.stdout, (
        f"status line must carry the seeded mode back out of the rendered line; "
        f"stdout={r.stdout[:400]!r}"
    )
    assert "gates=" in r.stdout and "violations=" in r.stdout, (
        f"status line must carry the gates and violations fields; stdout={r.stdout[:400]!r}"
    )
    assert after_bundle - before_bundle == 1, (
        "an orchestrator master must still reach the shared /prompt-bundle call "
        f"for the always-on floor + companion; delta={after_bundle - before_bundle}"
    )


def test_short_prompt_fast_path(isolated_hook_daemon, tmp_path) -> None:
    """Mutation: `MIN_QUERY_LENGTH=1` reddens both halves at once -- the request
    count (the gate no longer exits before the bundle call) and the hook's own
    breadcrumb (the gate message it would have printed no longer fires)."""
    daemon = isolated_hook_daemon
    cache_dir = daemon["health"]["cache_dir"]
    sid = f"test-5b3a-{uuid.uuid4().hex[:8]}"
    seed_session_cache(cache_dir, sid, "work")
    verify_seeded_mode(daemon, sid, "work")

    env = hook_env(daemon)
    cwd = _new_project(tmp_path)
    envelope = json.dumps({
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
        "prompt": "hi",
    })
    debug_log = tmp_path / "rag-inject-debug.log"

    before_bundle = count_requests(daemon, _req("POST", "/prompt-bundle"))
    r = run_hook(HOOK, envelope, env=env, cwd=cwd, timeout=25, debug_log=str(debug_log))
    after_bundle = count_requests(daemon, _req("POST", "/prompt-bundle"))

    assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
    _no_py_crash(r)
    assert after_bundle - before_bundle == 0, (
        f"a below-threshold prompt must exit at the length gate before any bundle "
        f"call; delta={after_bundle - before_bundle}"
    )
    breadcrumb = debug_log.read_text() if debug_log.exists() else ""
    assert "skipped: prompt too short (2 < 10)" in breadcrumb, (
        f"the hook's own gate breadcrumb (naming both numbers) must be present; "
        f"debug log={breadcrumb!r}"
    )


def test_new_session_no_cache_file(isolated_hook_daemon, tmp_path) -> None:
    """Mutation: deleting the `if [ -z "$CURRENT_MODE" ]` guard around the
    mode-classification directive (hook line ~749) reddens the directive
    assertion."""
    daemon = isolated_hook_daemon
    sid = f"test-5b3a-fresh-{uuid.uuid4().hex[:8]}"
    env = hook_env(daemon)
    cwd = _new_project(tmp_path)
    prompt = "Explain how the retrieval pipeline ranks candidate rules"

    # In-run check of the premise: a prompt the auto-router classifies takes the
    # "mode set automatically" arm instead, which would silently move this test
    # onto a different branch than the one its name states.
    bin_lib = str(SKILL_DIR / "bin" / "lib")
    if bin_lib not in sys.path:
        sys.path.insert(0, bin_lib)
    from writ_mode_hint import classify_mode_hint  # noqa: E402  (path-loaded; single source)

    assert classify_mode_hint(prompt) is None, (
        "this prompt must not auto-classify, or the hook takes the auto-route "
        "branch instead of the no-mode-set directive this test is named for"
    )

    envelope = json.dumps({
        "session_id": sid,
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    })
    r = run_hook(HOOK, envelope, env=env, cwd=cwd, timeout=25)

    assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
    _no_py_crash(r)
    assert "[Writ: set mode before proceeding]" in r.stdout, (
        f"a brand-new session with no cache file must get the mode-classification "
        f"directive; stdout={r.stdout[:400]!r}"
    )
    assert "Declare: python3" in r.stdout and "mode set" in r.stdout, (
        f"the directive's Declare line must be present; stdout={r.stdout[:400]!r}"
    )
