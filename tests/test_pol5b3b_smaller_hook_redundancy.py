"""POL-5b-3b: collapse redundant round-trips / parses in the 3 smaller hooks.

  A. writ-read-rag.sh   -- reuse $MODE (drop 2nd `mode get`); META double-parse -> 1
  B. auto-approve-gate.sh -- defer PROJECT_ROOT + CURRENT_MODE behind the approval
                             gate (gate-first); the common non-approval prompt does
                             neither the python walk nor the mode round-trip
  C. writ-posttool-rag.sh -- META double-parse -> 1

Source-shape guards prove the redundancy is gone; behavioral guards run each
hook via bash against a daemon this module owns (tests/_hook_runner.py), never
a shared address resolved at import time.

TRIAGE CLUSTER 2: the six behavioral tests below used to be gated by an
import-time `skipif`, backed by `rc == 0` plus a no-traceback check (every one
of them passes identically whether or not the hook did anything at all). They
are converted here into three round-trip-counted properties (six cases), and
`test_looks_like_approval_miss_path` is DELETED as a function: its named
mechanism (a substring scan inside "the deferred fetch inside the miss-friction
branch") was removed from the hook (auto-approve-gate.sh:38-42; there is no
miss-friction branch left), so `classify("good question")` takes the identical
`none` path as the plain non-approval prompt and no assertion available to it
could ever fail for the reason its name stated. Its one surviving value -- that
an approval-adjacent prompt still classifies as none -- is preserved as a ROW of
the tier property below, under a name that states the real property.

RED until the three hooks are refactored (the shape classes) and RED until
tests/_hook_runner.py exists (every behavioral test below).
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from tests._hook_runner import (
    count_requests,
    hook_env,
    isolated_hook_daemon,  # noqa: F401  (imported for pytest fixture discovery)
    run_hook,
    seed_session_cache,
    verify_seeded_mode,
)
from tests.fixtures.session_state import write_evidence_transcript

SKILL_DIR = Path(__file__).resolve().parent.parent
HOOKS = SKILL_DIR / "hooks" / "scripts"

READRAG = HOOKS / "writ-read-rag.sh"
AUTOAPPROVE = HOOKS / "auto-approve-gate.sh"
POSTTOOL = HOOKS / "writ-posttool-rag.sh"

READRAG_SRC = READRAG.read_text()
AUTOAPPROVE_SRC = AUTOAPPROVE.read_text()
POSTTOOL_SRC = POSTTOOL.read_text()

# Decision 4 (plan): the old SAMPLE_SOURCE named `writ/server.py`, which stopped
# existing when the server became a package. writ-read-rag.sh never STATS this
# path (it reads it out of the envelope as a plain string and derives the query
# from the string itself), so the old dead path changed nothing about the two
# tests using it -- a HYGIENE fix, not a behavior fix. Pointed at an existing
# python source file inside this repo (not tmp_path: detect_project_root needs
# a path this checkout actually owns, so retrieval scopes to this project's
# records) and asserted to exist so a future rename fails loud instead of
# silently changing what these tests measure.
SAMPLE_SOURCE = str(SKILL_DIR / "writ" / "server" / "routes" / "query.py")
assert Path(SAMPLE_SOURCE).exists(), f"SAMPLE_SOURCE must name a real file: {SAMPLE_SOURCE}"


# --------------------------------------------------------------------------- #
# helpers (test-local; not shared harness state)
# --------------------------------------------------------------------------- #
def _no_py_crash(r) -> None:
    assert "Traceback" not in r.stderr, f"python traceback:\n{r.stderr[:500]}"
    assert "SyntaxError" not in r.stderr, f"python SyntaxError:\n{r.stderr[:500]}"


def _req(method: str, path: str) -> str:
    """A precise uvicorn-access-log substring: the quoted request line up to (not
    including) the HTTP version, so `/session/{sid}` cannot accidentally match a
    longer sibling path like `/session/{sid}/mode`."""
    return f'"{method} {path} HTTP'


def _new_project(tmp_path: Path) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    return str(root)


# --------------------------------------------------------------------------- #
# A. writ-read-rag.sh source-shape
# --------------------------------------------------------------------------- #
class TestReadRagShape:
    def test_single_mode_get_call(self) -> None:
        n = READRAG_SRC.count('$(_writ_session "mode get"')
        assert n == 1, f"read-rag must fetch mode once and reuse it; found {n} `mode get` calls"

    def test_meta_double_parse_collapsed(self) -> None:
        # D-WRITMETA-SH: routed through the centralized parse_writ_meta helper.
        n = READRAG_SRC.count('echo "$META_JSON" | parse_writ_meta')
        assert n == 1, f"read-rag META rule_ids+cost must route through parse_writ_meta once; found {n}"


# --------------------------------------------------------------------------- #
# B. auto-approve-gate.sh source-shape -- deferred past the gate
# --------------------------------------------------------------------------- #
class TestAutoApproveShape:
    # The fetches must be deferred behind the approval-relevance gate. Today they
    # sit at column 0 (unconditional, run on every turn). After the defer they are
    # indented inside an `if approval-related` block -- so no top-level occurrence.
    def test_mode_get_not_unconditional(self) -> None:
        assert re.search(r'^CURRENT_MODE=\$\(_writ_session "mode get"', AUTOAPPROVE_SRC, re.M) is None, (
            "auto-approve must not fetch mode unconditionally (column 0); it belongs "
            "inside the approval-relevance gate"
        )

    def test_project_root_not_unconditional(self) -> None:
        assert re.search(r"^PROJECT_ROOT=\$\(python3", AUTOAPPROVE_SRC, re.M) is None, (
            "auto-approve must not compute PROJECT_ROOT unconditionally (column 0); it "
            "belongs inside the approval-relevance gate"
        )


# --------------------------------------------------------------------------- #
# C. writ-posttool-rag.sh source-shape
# --------------------------------------------------------------------------- #
class TestPosttoolShape:
    def test_meta_double_parse_collapsed(self) -> None:
        # D-WRITMETA-SH: routed through the centralized parse_writ_meta helper.
        n = POSTTOOL_SRC.count('echo "$META_JSON" | parse_writ_meta')
        assert n == 1, f"posttool-rag META rule_ids+cost must route through parse_writ_meta once; found {n}"


# --------------------------------------------------------------------------- #
# behavioral guards (module-owned isolated daemon)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mode, expected_query_count",
    [
        pytest.param("review", 1, id="review"),
        pytest.param("work", 0, id="work"),
    ],
)
def test_read_rag_fires_by_mode(isolated_hook_daemon, tmp_path, mode, expected_query_count) -> None:
    """Collapses tests 5 (`test_review_mode_reads_source`) and 6
    (`test_work_mode_fast_exit`) into one property over mode. Both cases assert
    the mode round trip ran exactly once -- the round-trip form of
    TestReadRagShape::test_single_mode_get_call's source claim, and the in-run
    positive that makes the work-mode row's zero-query assertion non-vacuous.
    The review row does not assert an injection: whether any rule scores >= 0.4
    (hook 200-213) is a ranking fact this test does not control.

    Mutation, work row: widening the mode gate at hook line ~49 to accept `work`
    reddens the `POST /query` count (goes 0 -> 1).
    Mutation, review row: deleting the `MODE=` line and reusing a stale value
    reddens the `GET /session/{sid}/mode` count (the round trip that must fire
    exactly once no longer does).
    """
    daemon = isolated_hook_daemon
    cache_dir = daemon["health"]["cache_dir"]
    sid = f"test-5b3b-readrag-{mode}-{uuid.uuid4().hex[:8]}"
    seed_session_cache(cache_dir, sid, mode)
    verify_seeded_mode(daemon, sid, mode)

    env = hook_env(daemon)
    cwd = _new_project(tmp_path)
    envelope = json.dumps({
        "session_id": sid,
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": SAMPLE_SOURCE},
    })

    before_mode = count_requests(daemon, _req("GET", f"/session/{sid}/mode"))
    before_query = count_requests(daemon, _req("POST", "/query"))
    r = run_hook(READRAG, envelope, env=env, cwd=cwd, timeout=25)
    after_mode = count_requests(daemon, _req("GET", f"/session/{sid}/mode"))
    after_query = count_requests(daemon, _req("POST", "/query"))

    assert r.returncode == 0, f"[{mode}] exit {r.returncode}; stderr={r.stderr[:300]!r}"
    _no_py_crash(r)
    assert after_mode - before_mode == 1, (
        f"[{mode}] the mode gate must read mode exactly once; delta={after_mode - before_mode}"
    )
    assert after_query - before_query == expected_query_count, (
        f"[{mode}] expected {expected_query_count} POST /query; "
        f"delta={after_query - before_query}"
    )


@pytest.mark.parametrize(
    "prompt, tier",
    [
        pytest.param(
            "lets refactor the parser module structure", "none", id="plain-non-approval",
        ),
        pytest.param("good question", "none", id="approval-adjacent-none"),
        pytest.param("approved", "exact", id="exact-with-evidence"),
    ],
)
def test_approval_tier_property(isolated_hook_daemon, tmp_path, prompt, tier) -> None:
    """Collapses tests 7 (`test_plain_non_approval_no_directive`), 8
    (`test_approval_emits_directive`) and 9 (`test_looks_like_approval_miss_path`,
    DELETED as a function) into one property over three rows. The third row
    ("good question") is the surviving input of the deleted test: its only
    remaining claim is that an approval-adjacent prompt still classifies as
    none, which the "none" assertions below (shared with the first row) fully
    state.

    none-tier rows (`plain-non-approval`, `approval-adjacent-none`): stdout is
    EXACTLY empty, stderr carries no `[WRIT CRITICAL]` line (the only observable
    pre-classifier exit), and no gate-token file exists at
    `/tmp/writ-gate-token-<sid>` -- the load-bearing assertion, and the one that
    has never existed at the hook level (approval_match.py's own docstring
    records the hazard: "a casual `ok` left a durable credential in /tmp").
    What no assertion here can distinguish, stated rather than papered over:
    `classify` is fail-closed (`|| echo "none"`), so a BROKEN classifier degrades
    to the none tier and produces the same silence as a correct one; that
    discrimination lives in tests/test_approval_tiers.py and
    tests/test_approval_exact_only.py, which call `classify` directly.
    Mutation for this row-class: adding `good` to `_EMBEDDED_APPROVAL_RE`
    (bin/lib/approval_match.py:160) reddens the `approval-adjacent-none` row
    specifically (both the stdout and the token-file assertions) -- the
    `plain-non-approval` row contains no approval-adjacent token, so it is not
    reddened by that mutation and stands as this class's baseline.

    exact-tier row (`exact-with-evidence`, "approved" with an evidence
    transcript, session seeded mode=work/phase=implementation/both gates
    approved so no gate is pending): the token file exists with five lines,
    line 2 (index 1) empty (bound to no gate); stdout carries
    "[Writ: approval pattern detected, nothing was advanced]"; and the
    `_replan_hint` text is present, which fires only when CURRENT_MODE is work
    and CURRENT_PHASE is implementation and nothing is pending (hook 267-277)
    -- the precise positive for what this test proves, that the deferred
    PROJECT_ROOT and CURRENT_MODE still feed the match path (the generic
    "nothing was advanced" message alone would print just as well with both
    empty). `POST /session/{sid}/advance-phase` delta is 0: no gate state on
    the isolated daemon is touched.
    Mutation: deleting the token mint (hook 416-430) reddens the token-file
    assertion; deleting `_replan_hint`'s call (hook line ~582) reddens the
    hint assertion.
    """
    daemon = isolated_hook_daemon
    cache_dir = daemon["health"]["cache_dir"]
    sid = f"test-5b3b-tier-{uuid.uuid4().hex[:8]}"
    token_path = Path(f"/tmp/writ-gate-token-{sid}")
    try:
        if tier == "exact":
            seed_session_cache(
                cache_dir, sid, "work",
                current_phase="implementation",
                gates_approved=["phase-a", "test-skeletons"],
            )
        else:
            seed_session_cache(cache_dir, sid, "work")
        verify_seeded_mode(daemon, sid, "work")

        envelope_fields = {
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
            # Carries agent_id == session_id, per this cluster's isolation fix:
            # auto-approve-gate.sh writes /tmp/writ-current-session (a file with
            # two live readers outside this suite) whenever agent_id is absent.
            "agent_id": sid,
        }
        if tier == "exact":
            envelope_fields["transcript_path"] = write_evidence_transcript(tmp_path)

        env = hook_env(daemon)
        cwd = _new_project(tmp_path)
        before_advance = count_requests(daemon, _req("POST", f"/session/{sid}/advance-phase"))
        r = run_hook(
            AUTOAPPROVE, json.dumps(envelope_fields), env=env, cwd=cwd, timeout=25,
        )
        after_advance = count_requests(daemon, _req("POST", f"/session/{sid}/advance-phase"))

        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        _no_py_crash(r)

        if tier == "none":
            assert r.stdout == "", (
                f"a none-tier prompt must emit no directive at all; stdout={r.stdout!r}"
            )
            assert "[WRIT CRITICAL]" not in r.stderr, f"stderr={r.stderr[:300]!r}"
            assert not token_path.exists(), (
                f"a none-tier prompt must mint no durable gate-token credential at {token_path}"
            )
        else:
            assert token_path.exists(), (
                f"the exact tier with evidence must mint a gate token at {token_path}"
            )
            lines = token_path.read_text().splitlines()
            assert len(lines) == 5, f"gate token file must have 5 lines; got {lines!r}"
            assert lines[1] == "", (
                f"the token must be bound to no gate (line 2 empty); got {lines[1]!r}"
            )
            assert "[Writ: approval pattern detected, nothing was advanced]" in r.stdout, (
                f"stdout={r.stdout!r}"
            )
            assert 'Reply exactly "replan approved" on your own turn.' in r.stdout, (
                f"the replan hint must fire for a no-gate-pending approval in work "
                f"mode / implementation phase; stdout={r.stdout!r}"
            )
            assert after_advance - before_advance == 0, (
                f"no gate was pending, so no advance-phase POST should have been sent; "
                f"delta={after_advance - before_advance}"
            )
    finally:
        token_path.unlink(missing_ok=True)


def test_write_source_runs_clean(isolated_hook_daemon, tmp_path) -> None:
    """Re-keyed onto a round-trip count: `rc == 0` plus a no-traceback check
    passes whether or not the hook did anything, so this asserts the behavioral
    form of the round-trip collapse TestPosttoolShape's source-shape sibling can
    only claim from the text -- exactly one /query derived from the written
    content, exactly one session read, and ZERO separate mode round trips (L2:
    mode comes from that single cache read).

    The written path lives under tmp_path and is never created on disk: the
    hook reads content from the envelope and never stats the path, so no test
    here names a shared /tmp path.

    Mutation: restoring a separate `_writ_session "mode get"` call in the hook
    reddens the GET /session/{sid}/mode zero-count assertion -- the exact
    regression the source-shape sibling cannot see.
    """
    daemon = isolated_hook_daemon
    cache_dir = daemon["health"]["cache_dir"]
    sid = f"test-5b3b-posttool-{uuid.uuid4().hex[:8]}"
    seed_session_cache(cache_dir, sid, "work")
    verify_seeded_mode(daemon, sid, "work")

    env = hook_env(daemon)
    cwd = _new_project(tmp_path)
    written_path = str(tmp_path / "pol5b3b_sample.py")
    envelope = json.dumps({
        "session_id": sid,
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {
            "file_path": written_path,
            "content": "class Sample:\n    def handler(self):\n        return 1\n",
        },
    })

    before_query = count_requests(daemon, _req("POST", "/query"))
    before_read = count_requests(daemon, _req("GET", f"/session/{sid}"))
    before_mode = count_requests(daemon, _req("GET", f"/session/{sid}/mode"))
    r = run_hook(POSTTOOL, envelope, env=env, cwd=cwd, timeout=25)
    after_query = count_requests(daemon, _req("POST", "/query"))
    after_read = count_requests(daemon, _req("GET", f"/session/{sid}"))
    after_mode = count_requests(daemon, _req("GET", f"/session/{sid}/mode"))

    assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
    _no_py_crash(r)
    assert not Path(written_path).exists(), "the hook must not need the file on disk"
    assert after_query - before_query == 1, (
        f"posttool-rag must derive exactly one /query from the written content; "
        f"delta={after_query - before_query}"
    )
    assert after_read - before_read == 1, (
        f"posttool-rag must read the session cache exactly once; "
        f"delta={after_read - before_read}"
    )
    assert after_mode - before_mode == 0, (
        f"mode must come from the single cache read, with no separate mode round "
        f"trip; delta={after_mode - before_mode}"
    )
