"""RED guard for Wave-5 Cycle 5.4 -- false docstring/comment claims + 2 stale-pin tests.

A 148-file docstring hunt (scan + adversarial verify) found 9 present-tense claims in test
docstrings/comments that are FALSE against the current code, plus 2 pre-existing FAILING tests
whose hardcoded pins drifted stale as later features shipped. Per plan.md Cycle 5.4:

Group A -- pure docstring/comment rewords (6 files, no test-behavior change):
- test_inv3_research_doctrine.py: "DISPATCHES the source-eval Technique" is false (Techniques
  are INVOKES targets, not DISPATCHES targets; the file's own
  test_playbook_invokes_source_eval asserts INVOKES) -> reword to INVOKES. The stale "Wired:
  writ-rag-inject.sh gains an `investigate)` arm ... header \"[Writ: investigation doctrine]\""
  bullet describes a hook-side arm that does not exist -> reword to the actual server-side
  delivery (writ/server/routes/query.py maps investigate to query_source
  investigation-doctrine).
- test_phase51_doc_counts.py: the module-docstring summary line says the hooks count is 35;
  the file's own test_hooks_json_entry_count already pins and passes at 41 -- only the prose
  is stale.
- test_pol6_a3_server_valid_modes_dedup.py: an inline comment cites `server.py:474`; server.py
  was split into the writ/server/ package (Wave 2 POL-6) -- the mode-membership check now
  lives at writ/server/routes/session_state.py:109.
- test_pol6b2_cache_dir_env.py: a class docstring cites `server.py:299` for the same reason.
- test_pol6_pre_gate_compat.py: the module docstring claims "the hook makes no server calls";
  is_work_mode now goes daemon-first (`_writ_session mode get`) and only falls back to the
  cache file on failure.
- test_retrieval.py: the module docstring claims "MRR@5 evaluation happens via human review
  sessions, not automated tests"; tests/test_graph_proximity.py's test_mrr5_no_regression is an
  automated MRR@5 regression test against MRR5_FLOOR.

Group B -- two pre-existing FAILING tests renamed off their stale pin (their fix ALSO updates
the assertion value, but this guard only checks the rename, since the value fix is exercised by
running the target files' own test suites, not by this hermetic source-scan):
- test_phase_edge_retire.py: test_edge_count_is_17 is false today (ALLOWED_EDGE_TYPES has 24
  members, not 17) -> renamed to test_edge_count_matches_allowed_set.
- test_phase6_provenance.py: test_valid_provenance_set_is_the_four_states is false today
  (VALID_PROVENANCE has 5 members including `record`, not 4) -> renamed to
  test_valid_provenance_set.

This guard is FULLY HERMETIC: it is a pure source-text scan. It does NOT import, execute, or
collect fixtures from any of the 9 target files, and it does NOT touch Neo4j or a daemon. It
reads each file's text with `Path.read_text()` and does substring / literal checks only.

RED today (2026-07-17, pre-implementation): every test below fails, because each false phrase
is still present and/or each corrected fact / rename is still absent. GREEN only once the
corresponding reword or rename in plan.md Cycle 5.4 lands in the target file.
"""

from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def _read(filename: str) -> str:
    """Read `filename`'s source text relative to this tests/ dir.

    Never imports or executes the file -- pure text read, so this guard cannot itself import
    the target module, collect its fixtures, or touch a daemon/Neo4j.
    """
    path = TESTS_DIR / filename
    assert path.exists(), f"expected {path} to exist"
    return path.read_text()


# ---------------------------------------------------------------------------
# Group A -- false phrase gone, corrected fact present (6 files)
# ---------------------------------------------------------------------------


def test_inv3_dispatches_reworded() -> None:
    """test_inv3_research_doctrine.py must say INVOKES, not DISPATCHES, the source-eval
    Technique -- Techniques are INVOKES targets (DISPATCHES is reserved for
    Playbook->SubagentRole), and the file's own test_playbook_invokes_source_eval already
    asserts INVOKES.
    """
    src = _read("test_inv3_research_doctrine.py")
    assert "DISPATCHES the source-eval" not in src, (
        "test_inv3_research_doctrine.py must not claim the Playbook DISPATCHES the "
        "source-eval Technique -- Techniques are INVOKES targets, and this file's own "
        "test_playbook_invokes_source_eval asserts INVOKES, not DISPATCHES"
    )
    assert "INVOKES the source-eval" in src, (
        "test_inv3_research_doctrine.py must state the corrected fact: the Playbook INVOKES "
        "the source-eval Technique"
    )


def test_inv3_wired_arm_reworded() -> None:
    """test_inv3_research_doctrine.py's 'Wired' bullet must drop the false claim that
    writ-rag-inject.sh gains an `investigate)` arm with header "[Writ: investigation
    doctrine]" -- no such hook-side arm exists. It must instead describe the server-side
    delivery that actually ships (writ/server/routes/query.py mapping investigate to
    query_source investigation-doctrine).
    """
    src = _read("test_inv3_research_doctrine.py")
    assert "[Writ: investigation doctrine]" not in src, (
        "test_inv3_research_doctrine.py must not claim the false header string "
        '"[Writ: investigation doctrine]" -- writ-rag-inject.sh gains no such arm'
    )
    assert "investigate) arm" not in src, (
        "test_inv3_research_doctrine.py must not claim writ-rag-inject.sh gains an "
        "`investigate)` arm -- the actual delivery is server-side "
        "(writ/server/routes/query.py), not a new hook arm"
    )


def test_phase51_hooks_count_reworded() -> None:
    """test_phase51_doc_counts.py's module-docstring summary must state the hooks count
    its own test_hooks_json_entry_count asserts (the prose said 35, which was stale).

    The pinned literal moves with that assertion: 41 -> 44 when the manual-testing
    grant's two registrations and the auto-memory mirror's one landed. The guard's job
    is prose/assertion AGREEMENT, so it tracks the current count rather than freezing
    one.
    """
    src = _read("test_phase51_doc_counts.py")
    assert 'command" leaves == 35' not in src, (
        "test_phase51_doc_counts.py's docstring must not claim the hooks.json "
        '\'command\" leaves\' count is 35 -- that count is long stale'
    )
    assert 'command" leaves == 44' in src, (
        "test_phase51_doc_counts.py's docstring must state the current hooks.json "
        '\'command\" leaves\' count of 44, matching test_hooks_json_entry_count'
    )


def test_pol6a3_server_ref_reworded() -> None:
    """test_pol6_a3_server_valid_modes_dedup.py's inline comment must repoint from the
    pre-split `server.py:474` to the current `writ/server/routes/session_state.py:109`
    location of the mode-membership check (server.py was split into the writ/server/
    package during Wave 2 POL-6).
    """
    src = _read("test_pol6_a3_server_valid_modes_dedup.py")
    assert "server.py:474" not in src, (
        "test_pol6_a3_server_valid_modes_dedup.py must not cite the pre-split server.py:474 "
        "location -- server.py was split into the writ/server/ package"
    )
    assert "session_state.py:109" in src, (
        "test_pol6_a3_server_valid_modes_dedup.py must cite the current "
        "writ/server/routes/session_state.py:109 location of the mode-membership check"
    )


def test_pol6b2_server_ref_reworded() -> None:
    """test_pol6b2_cache_dir_env.py's class docstring must drop the pre-split
    `server.py:299` reference (server.py was split into the writ/server/ package).
    """
    src = _read("test_pol6b2_cache_dir_env.py")
    assert "server.py:299" not in src, (
        "test_pol6b2_cache_dir_env.py must not cite the pre-split server.py:299 location -- "
        "server.py was split into the writ/server/ package during Wave 2 POL-6"
    )


def test_pol6_pre_gate_no_server_calls_reworded() -> None:
    """test_pol6_pre_gate_compat.py's module docstring must drop the false claim that the
    hook makes no server calls -- is_work_mode now goes daemon-first via
    `_writ_session mode get` and only falls back to the cache file.
    """
    src = _read("test_pol6_pre_gate_compat.py")
    assert "makes no server calls" not in src, (
        "test_pol6_pre_gate_compat.py must not claim the hook makes no server calls -- "
        "is_work_mode now attempts a daemon call first (`_writ_session mode get`) and only "
        "falls back to the cache file"
    )


def test_retrieval_mrr_claim_reworded() -> None:
    """test_retrieval.py's module docstring must drop the false claim that MRR@5 evaluation
    happens only via human review sessions -- tests/test_graph_proximity.py's
    test_mrr5_no_regression is an automated MRR@5 regression test against MRR5_FLOOR.
    """
    src = _read("test_retrieval.py")
    assert "not automated tests" not in src, (
        "test_retrieval.py must not claim MRR@5 evaluation happens via human review, "
        '"not automated tests" -- test_graph_proximity.py test_mrr5_no_regression is an '
        "automated MRR@5 regression test"
    )
    assert "human review sessions" not in src, (
        "test_retrieval.py must not claim MRR@5 evaluation happens via human review "
        "sessions -- an automated MRR@5 regression test (test_mrr5_no_regression against "
        "MRR5_FLOOR) already exists"
    )


# ---------------------------------------------------------------------------
# Group B -- stale-pin tests renamed off the false number/word (2 files)
# ---------------------------------------------------------------------------


def test_edge_count_test_renamed() -> None:
    """test_phase_edge_retire.py's failing test_edge_count_is_17 must be renamed to
    test_edge_count_matches_allowed_set -- ALLOWED_EDGE_TYPES has 24 members today, not 17,
    so the old name is itself a false claim.
    """
    src = _read("test_phase_edge_retire.py")
    assert "def test_edge_count_is_17" not in src, (
        "test_phase_edge_retire.py must not keep the stale test_edge_count_is_17 name -- "
        "ALLOWED_EDGE_TYPES has 24 members today, not 17, so the name is a false claim"
    )
    assert "def test_edge_count_matches_allowed_set" in src, (
        "test_phase_edge_retire.py must rename the edge-count test to "
        "test_edge_count_matches_allowed_set, pinning the current 24-entry "
        "ALLOWED_EDGE_TYPES without baking a stale number into the test name"
    )


def test_provenance_set_test_renamed() -> None:
    """test_phase6_provenance.py's failing test_valid_provenance_set_is_the_four_states must
    be renamed to test_valid_provenance_set -- VALID_PROVENANCE has 5 members including
    `record` today, not 4, so the old name is itself a false claim.
    """
    src = _read("test_phase6_provenance.py")
    assert "def test_valid_provenance_set_is_the_four_states" not in src, (
        "test_phase6_provenance.py must not keep the stale "
        "test_valid_provenance_set_is_the_four_states name -- VALID_PROVENANCE has 5 members "
        "(including `record`) today, not 4, so the name is a false claim"
    )
    # Trailing "(" excludes the longer old name so this does not pass on a partial match.
    assert "def test_valid_provenance_set(" in src, (
        "test_phase6_provenance.py must rename the provenance-set test to "
        "test_valid_provenance_set, pinning the current 5-member VALID_PROVENANCE "
        "(including `record`) without baking a stale word into the test name"
    )
