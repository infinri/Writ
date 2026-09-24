"""Skeletons for defect 1: a gate denial must not disarm the seeder.

Plan: .claude/plans/2412ba38-51e1-4b73-895b-7b240a3c21d3/plan.md.

THE CHAIN THIS PINS. `gates._log_gate_denial` counts a denial inside
`with mutate_cache(session_id)`, whose context manager ends in an unconditional
`_write_cache`. `_read_cache` returns `_default_cache()` on a miss without writing, so
for an unseeded sub-agent a DENIAL is the first thing that ever creates its cache
file. That file carries `denial_counts` populated and every other key at its default:
`mode` null, `cache_source` empty, `is_subagent` false. `seed_subagent_cache`'s
pre-lock check today is a bare `os.path.exists(_cache_path(agent))`, which reads that
file's mere existence as proof the agent was already seeded, so the refusal that
proved the agent needed governance is what locks it out of governance permanently.

RED UNTIL: `writ.session.subagent_seed.already_seeded(cache)` exists and is called at
both the pre-lock check and the in-lock re-check inside `seed_subagent_cache`, and the
seeder carries a prior cache's `denial_counts` forward through `mutate_cache` rather
than only through `_CLEAN_OPERATIONAL_STATE`'s empty default. Every test below fails
today either with an ImportError this module turns into a readable `pytest.fail`
(`already_seeded` does not exist yet) or with an assertion against the CURRENT
behaviour (a denial-created cache reads as already seeded, so nothing ever seeds over
it).

WHAT WOULD MAKE ONE OF THESE PASS VACUOUSLY, and why this module refuses it:

  - Comparing `already_seeded` against a HAND-BUILT dict that merely resembles what
    `_log_gate_denial` writes, instead of the byte-identical artifact that function
    actually persisted. The plan's own test-design section forbids this: a hand-built
    fixture would be a model of the path, not the path. Every "denial-created cache"
    in this module is produced by calling the real `gates._log_gate_denial` against a
    real `WRIT_CACHE_DIR`, then read back off disk through the real `_read_cache`.
  - A "never re-seeded" assertion that passes because the harness never seeds
    ANYTHING, not because the already-seeded cache specifically resists it.
    `TestAnAlreadySeededCacheIsNeverRewritten` carries its own positive control,
    `test_the_harness_can_seed_an_unseeded_cache`, for exactly this reason.
  - Re-implementing the write decision (`_can_write_check`) instead of calling it.
    `TestSeedingOverDenialConfersNoWriteAuthority` calls the real gate function; two
    copies of one mistake would still agree with each other (ENF-SYS-005's sibling
    argument for the python side of this cycle).

ISOLATION. Every test gets its own `WRIT_CACHE_DIR` under `tmp_path` via the `cache_dir`
fixture below (monkeypatch-scoped, restored after the test). Nothing in this module
reads the operator's real session cache, the real friction/metrics streams, or the
repo's own logs.

WHAT THIS MODULE DOES NOT COVER. The bash-reachability half of this cycle (defect 2:
Grep/Glob/ExitPlanMode never reaching the seeder at all) is
`tests/test_subagent_seed_reach.py`'s job. Nothing here spawns a hook subprocess or a
bash function; every seed call in this module is the real python
`seed_mod.seed_subagent_cache`, called in-process.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PARENT = "22222222-3333-4444-5555-666666666666"
AGENT = "b0123456789abcdef"

# A parent mid-implementation with both work gates approved: the state whose
# inheritance would hand a child write authority it must not get, matching the
# fixture `test_subagent_seed.py::PARENT_STATE` uses for the same reason.
PARENT_STATE = {
    "mode": "work",
    "current_phase": "implementation",
    "gates_approved": ["phase-a", "test-skeletons"],
    "project_root": "",
}

DENIAL_GATE = "test-skeletons"
DENIAL_FILE_PATH = "/home/lucio.saldivar/workspaces/ai-stack/src/thing.py"
DENIAL_REASON = "[ENF-GATE-MODE] No mode declared. Set a mode before writing code."

# The true "no cache at all" state, and the control every write-authority assertion in
# `TestSeedingOverDenialConfersNoWriteAuthority` is compared against. A distinct id, never
# AGENT, because AGENT's cache is exactly what those tests seed.
UNGOVERNED_AGENT = "b9876543210fedcba"


def _write_envelope(path: str) -> dict:
    """A Write tool envelope naming `path`, the shape `_can_write_check` parses."""
    return {"tool_name": "Write", "tool_input": {"file_path": path}}


def _seed_module():
    """The `writ.session.subagent_seed` module, or a readable skeleton failure.

    Imported lazily (never `from ... import already_seeded` at module scope) so a
    module that exists today but does not yet define `already_seeded` still lets this
    file COLLECT; the missing attribute is reported per-test through `_require`
    instead of as a collection-time ImportError.
    """
    try:
        from writ.session import subagent_seed
    except ImportError as exc:  # pragma: no cover - RED path
        pytest.fail(f"skeleton: writ/session/subagent_seed.py does not exist yet ({exc})")
    return subagent_seed


def _require(module, *names) -> None:
    """Fail with the missing attribute names, rather than an AttributeError, when a
    skeleton's target function has not been written yet."""
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


def _cache_file(cache_dir: Path, session_id: str) -> Path:
    return cache_dir / f"writ-session-{session_id}.json"


def _write_parent_cache(cache_dir: Path, session_id: str = PARENT,
                        state: dict | None = None) -> Path:
    """Write a parent session's cache file directly (never through the seeder): the
    governance state a child should, or should not, inherit."""
    path = _cache_file(cache_dir, session_id)
    path.write_text(json.dumps(PARENT_STATE if state is None else state))
    return path


def _write_child_cache(cache_dir: Path, agent_id: str, cache: dict) -> Path:
    """Write `cache` straight to `writ-session-<agent_id>.json`.

    Built directly rather than through `seed_mod.seed_subagent_cache`, so a fixture
    reproducing 'a cache already carrying this shape' does not depend on the very
    function `TestAnAlreadySeededCacheIsNeverRewritten` is testing.
    """
    path = _cache_file(cache_dir, agent_id)
    path.write_text(json.dumps(cache))
    return path


def _read_child_cache(cache_dir: Path, agent_id: str = AGENT) -> dict | None:
    """The child cache as it exists on disk right now, or None when no file exists."""
    path = _cache_file(cache_dir, agent_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _produce_denial_cache(cache_dir: Path, session_id: str = AGENT, *,
                          gate: str = DENIAL_GATE,
                          file_path: str = DENIAL_FILE_PATH,
                          reason: str = DENIAL_REASON) -> dict:
    """Produce the ONLY artifact this module is allowed to call "a denial-created
    cache": the real `gates._log_gate_denial(session_id, cache, gate, file_path,
    reason)`, called against an unseeded session's own `_read_cache` result (never a
    dict this test wrote by hand), then read back off disk.

    `_log_gate_denial` writes through `mutate_cache`, a real file-locked
    read-modify-write (ENF-SYS-005): nothing here may substitute a mock for it.
    Returns the resulting on-disk cache dict, via `writ.session.cache._read_cache`,
    so the assertions in this module are made against what was actually persisted.
    """
    from writ.session import gates
    from writ.session.cache import _read_cache

    gates._log_gate_denial(session_id, _read_cache(session_id), gate, file_path, reason)
    assert _cache_file(cache_dir, session_id).exists(), (
        "the denial did not create a cache file, so the defect this module pins "
        "(a denial artifact disarming the seeder) is not being reproduced"
    )
    return _read_cache(session_id)


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """A fresh `WRIT_CACHE_DIR` under `tmp_path` for exactly one test.

    Every cache this module reads or writes lives under this directory; nothing in
    this module ever points at the operator's real session cache.
    """
    d = tmp_path / "cache"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WRIT_CACHE_DIR", str(d))
    return d


# --------------------------------------------------------------------------- #
# Capability 1: the denial artifact is not seeded state.
# --------------------------------------------------------------------------- #

class TestDenialArtifactIsNotAlreadySeeded:
    """RED until `writ.session.subagent_seed.already_seeded` exists.

    A VACUOUS PASS THIS CLASS MUST NOT ALLOW: `already_seeded` answering False for
    every input, seeded or not. `test_a_lazily_seeded_cache_is_reported_as_already_
    seeded` is the positive control that rules that out.
    """

    def test_a_cache_created_only_by_a_gate_denial_is_not_already_seeded(
        self, cache_dir
    ) -> None:
        """The denial-created cache carries `denial_counts` and nothing that reads as
        governance: no `cache_source`, `is_subagent` still False. `already_seeded`
        must answer False here, or the refusal that proved the agent needed
        governance is what locks it out of governance forever."""
        mod = _seed_module()
        _require(mod, "already_seeded")
        denial = _produce_denial_cache(cache_dir)
        assert denial["denial_counts"] == {DENIAL_GATE: 1}, (
            f"the denial did not record itself: {denial.get('denial_counts')!r}"
        )
        assert denial["cache_source"] == ""
        assert denial["is_subagent"] is False
        assert denial["mode"] is None
        assert mod.already_seeded(denial) is False, (
            "a cache created by a gate denial reads as already seeded, so the seeder "
            "will never govern this agent again"
        )

    def test_a_lazily_seeded_cache_is_reported_as_already_seeded(self, cache_dir) -> None:
        """POSITIVE CONTROL. A cache the real seeder already wrote must read as
        seeded, so the False verdict above cannot be explained by `already_seeded`
        answering False unconditionally."""
        mod = _seed_module()
        _require(mod, "already_seeded")
        _write_parent_cache(cache_dir)
        assert mod.seed_subagent_cache(AGENT, PARENT) is True
        seeded = _read_child_cache(cache_dir)
        assert seeded["cache_source"] == mod.CACHE_SOURCE_LAZY
        assert mod.already_seeded(seeded) is True


# --------------------------------------------------------------------------- #
# Capability 2: a hook seeds over the denial artifact.
# --------------------------------------------------------------------------- #

class TestAHookSeedsOverTheDenialArtifact:
    """Capability 2. The denial-created cache must not be a dead end: the next thing
    that calls `seed_mod.seed_subagent_cache` for this agent must still be able to
    seed it. This class calls the real python seeder directly, in-process; the
    bash-reachability half of this claim (a hook that has already consumed stdin
    still reaching this same function) is `test_subagent_seed_reach.py`'s job, not
    this module's.
    """

    def test_it_inherits_the_parents_mode_and_gates(self, cache_dir) -> None:
        """RED until the pre-lock `os.path.exists(_cache_path(agent))` early return
        (which today reads ANY existing file, including a denial artifact, as
        'already seeded') is replaced by a call to `already_seeded`."""
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        denial = _produce_denial_cache(cache_dir)
        assert denial["mode"] is None, "the denial artifact already carried a mode"
        assert mod.seed_subagent_cache(AGENT, PARENT) is True, (
            "the seeder declined an agent whose only cache is a denial artifact"
        )
        child = _read_child_cache(cache_dir)
        assert child["mode"] == PARENT_STATE["mode"]
        assert child["gates_approved"] == PARENT_STATE["gates_approved"]
        assert child["current_phase"] == PARENT_STATE["current_phase"]
        assert child["parent_session_id"] == PARENT
        assert child["is_subagent"] is True

    def test_it_is_marked_as_a_lazy_seed(self, cache_dir) -> None:
        """The seeded-over cache must carry `cache_source == CACHE_SOURCE_LAZY`, the
        marker `gates._authority_mode` reads to resolve its mode as absent."""
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        _produce_denial_cache(cache_dir)
        assert mod.seed_subagent_cache(AGENT, PARENT) is True
        child = _read_child_cache(cache_dir)
        assert child["cache_source"] == mod.CACHE_SOURCE_LAZY
        assert mod.is_lazily_seeded(child) is True


# --------------------------------------------------------------------------- #
# Capability 3: denial_counts survive seeding.
# --------------------------------------------------------------------------- #

class TestDenialCountsSurviveSeeding:
    """Capability 3. `denial_counts` is the one key in `_CLEAN_OPERATIONAL_STATE` with
    an enforcement consequence: the write gate escalates deny to ask on a repeat
    count. Resetting it on every seed would silently erase an agent's escalation
    record the moment a hook happened to seed over its denial artifact.
    """

    def test_seeding_over_a_denial_cache_carries_denial_counts_forward(
        self, cache_dir
    ) -> None:
        """A denial-created cache already carries a non-empty `denial_counts`
        (produced by a real, prior `_log_gate_denial` call). Seeding over it must
        leave that dict unchanged, byte for byte, rather than resetting it to `{}`."""
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        _produce_denial_cache(cache_dir)
        denial = _produce_denial_cache(cache_dir, gate="phase-a")
        before = dict(denial["denial_counts"])
        assert before == {DENIAL_GATE: 1, "phase-a": 1}, (
            f"the escalation record under test was never built: {before!r}"
        )
        assert mod.seed_subagent_cache(AGENT, PARENT) is True
        assert _read_child_cache(cache_dir)["denial_counts"] == before, (
            "seeding erased the escalation record the write gate reads to escalate "
            "deny to ask on a repeat count"
        )

    def test_seeding_with_no_prior_cache_leaves_denial_counts_empty(
        self, cache_dir
    ) -> None:
        """The no-branch case: a fresh sub-agent with no prior cache at all must
        still get an empty `denial_counts`, exactly what `_CLEAN_OPERATIONAL_STATE`
        already carries today. Stated as its own case so the carry-forward test
        above cannot pass merely because the seeder always copies whatever it finds,
        empty or not."""
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        assert _read_child_cache(cache_dir) is None
        assert mod.seed_subagent_cache(AGENT, PARENT) is True
        assert _read_child_cache(cache_dir)["denial_counts"] == {}


# --------------------------------------------------------------------------- #
# Capability 4: an already-seeded cache is never rewritten.
# --------------------------------------------------------------------------- #

class TestAnAlreadySeededCacheIsNeverRewritten:
    """Capability 4. Both shapes of "already governed" must stop the seeder cold,
    even after the parent's own mode changes: the modern `cache_source` marker, and
    the legacy shape (an `is_subagent` cache with NO `cache_source` key at all, the
    only shape a pre-cycle cache can carry).

    THE POSITIVE CONTROL for the two negatives in this class is
    `test_the_harness_can_seed_an_unseeded_cache`: it proves this class's own fixture
    plumbing is capable of observing a seed happen at all, so the two "nothing
    changed" assertions above it cannot be passing only because nothing here is ever
    observable.
    """

    def test_a_cache_source_seeded_cache_is_not_reseeded_after_the_parent_changes(
        self, cache_dir
    ) -> None:
        """Seed the child once, advance the parent's mode and clear its gates, seed
        again: the child's mode and gates_approved must be untouched by the second
        call."""
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        assert mod.seed_subagent_cache(AGENT, PARENT) is True
        first = _read_child_cache(cache_dir)
        _write_parent_cache(cache_dir, state={**PARENT_STATE, "mode": "debug",
                                              "gates_approved": []})
        assert mod.seed_subagent_cache(AGENT, PARENT) is False
        after = _read_child_cache(cache_dir)
        assert after["mode"] == first["mode"]
        assert after["gates_approved"] == first["gates_approved"]
        assert after == first

    def test_a_legacy_is_subagent_only_cache_is_not_reseeded_after_the_parent_changes(
        self, cache_dir
    ) -> None:
        """The same property for a cache written before `cache_source` existed:
        `is_subagent: true` with no `cache_source` key at all. Caches written before
        this cycle must keep the write authority, or lack of it, they have today."""
        mod = _seed_module()
        legacy = {"mode": "work", "current_phase": "implementation",
                  "gates_approved": ["phase-a"], "is_subagent": True,
                  "parent_session_id": PARENT}
        _write_child_cache(cache_dir, AGENT, legacy)
        _write_parent_cache(cache_dir, state={**PARENT_STATE, "mode": "debug",
                                              "gates_approved": []})
        assert mod.seed_subagent_cache(AGENT, PARENT) is False
        after = _read_child_cache(cache_dir)
        assert "cache_source" not in after, (
            "the legacy cache was rewritten, which is how a pre-cycle sub-agent would "
            "lose the authority it has today"
        )
        assert after == legacy

    def test_the_harness_can_seed_an_unseeded_cache(self, cache_dir) -> None:
        """POSITIVE CONTROL. The exact same seed call the two tests above make,
        pointed at a cache with NEITHER marker, must actually change the child's
        state. Without this, a seeder that silently no-ops on every call would leave
        both negative assertions above green."""
        mod = _seed_module()
        unseeded = {"mode": "review", "current_phase": None, "gates_approved": [],
                    "queries": 3}
        _write_child_cache(cache_dir, AGENT, unseeded)
        _write_parent_cache(cache_dir)
        assert mod.seed_subagent_cache(AGENT, PARENT) is True
        after = _read_child_cache(cache_dir)
        assert after != unseeded
        assert after["mode"] == PARENT_STATE["mode"]
        assert after["gates_approved"] == PARENT_STATE["gates_approved"]
        assert after["cache_source"] == mod.CACHE_SOURCE_LAZY


# --------------------------------------------------------------------------- #
# Capability 5: seeding over a denial confers no write authority.
# --------------------------------------------------------------------------- #

class TestSeedingOverDenialConfersNoWriteAuthority:
    """Capability 5. The one cache shape `test_subagent_seed.py::
    TestSeedingGrantsNoAuthority` does not cover: a cache lazily seeded OVER a prior
    denial artifact, rather than over an empty path. Calls the real
    `gates._can_write_check`, never a hand-rolled re-implementation of the decision:
    two copies of one mistake would still agree with each other.
    """

    def test_a_cache_seeded_over_denial_is_refused_like_an_unseeded_subagent(
        self, cache_dir
    ) -> None:
        """Must refuse with the same `[ENF-GATE-MODE]` reason an unseeded sub-agent
        gets: `_authority_mode` resolves a lazily seeded cache's mode as absent
        regardless of how that cache came to exist."""
        from writ.session import gates
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        _produce_denial_cache(cache_dir)
        assert mod.seed_subagent_cache(AGENT, PARENT) is True
        seeded = gates._can_write_check(AGENT, _write_envelope(DENIAL_FILE_PATH),
                                        str(REPO))
        ungoverned = gates._can_write_check(UNGOVERNED_AGENT,
                                            _write_envelope(DENIAL_FILE_PATH), str(REPO))
        assert seeded["can_write"] is False
        assert "[ENF-GATE-MODE]" in (seeded["reason"] or "")
        # The reason names each session's own id in its `writ mode set` command, so the
        # ids are swapped for a placeholder before the two refusals are compared.
        assert (seeded["can_write"], seeded["reason"].replace(AGENT, "<sid>")) == (
            ungoverned["can_write"], ungoverned["reason"].replace(UNGOVERNED_AGENT, "<sid>")
        ), "seeding over a denial changed the write decision, which it must never do"

    def test_a_subagent_start_cache_is_still_allowed(self, cache_dir) -> None:
        """POSITIVE CONTROL. A `subagent_start` cache, untouched by this cycle, must
        still be allowed, so the refusal above cannot be explained by
        `_can_write_check` now refusing every sub-agent regardless of provenance."""
        from writ.session import gates
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        _produce_denial_cache(cache_dir)
        assert mod.seed_subagent_cache(AGENT, PARENT,
                                       cache_source=mod.CACHE_SOURCE_START) is True
        assert _read_child_cache(cache_dir)["cache_source"] == mod.CACHE_SOURCE_START
        result = gates._can_write_check(AGENT, _write_envelope(DENIAL_FILE_PATH),
                                        str(REPO))
        assert result["can_write"] is True
