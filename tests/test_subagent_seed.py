"""Cycle M-pre skeletons: govern a sub-agent on the hook that runs, not the event that
might not fire.

RED until `writ/session/subagent_seed.py` exists, `common.sh` seeds from the envelope, and
the write gate refuses to treat a seeded cache as authority.

THE GAP, measured across all 239 log files including 226 gzipped archives my earlier greps
silently skipped: 1,381 distinct agents have a `subagent_start` row and 2,219 hit the
stop-side fallback, and the two sets do NOT intersect. An agent is either governed at spawn
or never governed. 1,425 of the ungoverned nonetheless have `daemon_request` rows filed under
their own agent id, which is the seam this cycle uses: Writ hooks DO run inside them, so the
cache can be seeded on the first one. 794 produced only stop-side rows and are out of reach.

THE INVARIANT THAT MATTERS MORE THAN THE FEATURE. Creating a cache where none existed would
LOOSEN the gate, because the child inherits the parent's mode and approved gates. Measured
before designing:

    TODAY   ungoverned sub-agent (no cache): False  [ENF-GATE-MODE] No mode declared...
    SEEDED  with parent's mode+gates       : True
    SEEDED  without is_subagent            : True

The third line killed the first design: disarming the `is_subagent` bypass changes nothing,
because the work gate allows on its own once a mode and its gates are present. Today's denial
is not a policy, it is the accident of an absent cache. So `TestSeedingGrantsNoAuthority`
below is written as a PROPERTY over a path set, not a list of cases: for every representative
path the decision AND its reason under a `lazy_seed` cache must equal those under no cache at
all. A case list would pass while an unlisted path silently gained authority.

Per ENF-GATE-007: skeletons written and approved before implementation.
Per TEST-ISOLATE-003: every test gets its own `WRIT_CACHE_DIR` and its own fixture tree; no
test reads the real cache dir, the real projects tree, or the repo's own logs.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests._inventory import doctor_check_names

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"
COMMON_SH = REPO / "bin" / "lib" / "common.sh"
START_HOOK = HOOKS / "writ-subagent-start.sh"

GOVERNANCE_CENSUS_CHECK = "subagent-governance-census"

PARENT = "11111111-2222-3333-4444-555555555555"
AGENT = "a0123456789abcdef"

# A parent mid-implementation with both work gates approved: the state whose inheritance
# would hand a child write authority it must not get.
PARENT_STATE = {
    "mode": "work",
    "current_phase": "implementation",
    "gates_approved": ["phase-a", "test-skeletons"],
    "project_root": str(REPO),
}


def _seed_module():
    try:
        from writ.session import subagent_seed
    except ImportError as exc:  # pragma: no cover - RED path
        pytest.fail(f"skeleton: writ/session/subagent_seed.py does not exist yet ({exc})")
    return subagent_seed


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


def _write_parent_cache(cache_dir: Path, session_id: str = PARENT,
                        state: dict | None = None) -> Path:
    path = cache_dir / f"writ-session-{session_id}.json"
    path.write_text(json.dumps(state if state is not None else PARENT_STATE))
    return path


def _child_cache(cache_dir: Path, agent_id: str = AGENT) -> dict | None:
    path = cache_dir / f"writ-session-{agent_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _envelope(path: str) -> dict:
    return {"tool_input": {"file_path": path}}


def _run_hook(script: Path, *, cache: Path, friction: Path, stdin: str,
              extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "WRIT_CACHE_DIR": str(cache),
        "WRIT_FRICTION_LOG": str(friction),
        "WRIT_PORT": "59999",
        "WRIT_NO_AUTOSTART": "1",
        "WRIT_DIR": str(REPO),
        "SKILL_DIR": str(REPO),
        **(extra_env or {}),
    }
    cache.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["bash", str(script)], input=stdin, capture_output=True,
                          text=True, env=env, timeout=120)


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WRIT_CACHE_DIR", str(d))
    return d


@pytest.fixture()
def sinks(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache, tmp_path / "friction.jsonl"


# --------------------------------------------------------------------------- #
# Capabilities 1-4, 7, 11: the seeder itself
# --------------------------------------------------------------------------- #

class TestSeeder:

    def test_it_inherits_the_parents_governance_state(self, cache_dir) -> None:
        mod = _seed_module()
        _require(mod, "seed_subagent_cache", "CACHE_SOURCE_LAZY")
        _write_parent_cache(cache_dir)
        created = mod.seed_subagent_cache(AGENT, PARENT)
        assert created is True
        child = _child_cache(cache_dir)
        assert child is not None, "no child cache was written"
        assert child["mode"] == "work"
        assert child["current_phase"] == "implementation"
        assert child["gates_approved"] == ["phase-a", "test-skeletons"]
        assert child["parent_session_id"] == PARENT
        assert child["is_subagent"] is True

    def test_the_seeded_cache_declares_how_it_was_made(self, cache_dir) -> None:
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        mod.seed_subagent_cache(AGENT, PARENT)
        assert _child_cache(cache_dir)["cache_source"] == mod.CACHE_SOURCE_LAZY

    def test_a_start_hook_seed_is_marked_differently(self, cache_dir) -> None:
        """The distinction the write gate keys on, so it must be set by the caller."""
        mod = _seed_module()
        _require(mod, "CACHE_SOURCE_START")
        _write_parent_cache(cache_dir)
        mod.seed_subagent_cache(AGENT, PARENT, cache_source=mod.CACHE_SOURCE_START)
        assert _child_cache(cache_dir)["cache_source"] == mod.CACHE_SOURCE_START

    def test_a_second_seed_changes_nothing(self, cache_dir) -> None:
        """Several hooks run per tool call; the inherited gate set must not move mid-run."""
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        assert mod.seed_subagent_cache(AGENT, PARENT) is True
        first = _child_cache(cache_dir)
        # Parent advances after the child was seeded.
        _write_parent_cache(cache_dir, state={**PARENT_STATE, "mode": "debug",
                                              "gates_approved": []})
        assert mod.seed_subagent_cache(AGENT, PARENT) is False
        assert _child_cache(cache_dir) == first

    def test_it_does_not_overwrite_a_start_hook_cache(self, cache_dir) -> None:
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        mod.seed_subagent_cache(AGENT, PARENT, cache_source=mod.CACHE_SOURCE_START)
        assert mod.seed_subagent_cache(AGENT, PARENT) is False
        assert _child_cache(cache_dir)["cache_source"] == mod.CACHE_SOURCE_START

    def test_it_refuses_when_the_agent_is_the_session(self, cache_dir) -> None:
        """A main session must never be seeded as a child of itself."""
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        assert mod.seed_subagent_cache(PARENT, PARENT) is False
        assert _child_cache(cache_dir, PARENT)["mode"] == "work"  # untouched

    @pytest.mark.parametrize("agent_id,parent_id", [
        ("", PARENT), (AGENT, ""), ("", ""), ("../escape", PARENT),
    ])
    def test_it_refuses_unusable_ids(self, cache_dir, agent_id, parent_id) -> None:
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        assert mod.seed_subagent_cache(agent_id, parent_id) is False

    def test_an_absent_parent_cache_seeds_nothing(self, cache_dir) -> None:
        """No parent state means nothing to inherit; inventing a mode would be worse than
        leaving the child ungoverned, because a mode is what the gate reads."""
        mod = _seed_module()
        assert mod.seed_subagent_cache(AGENT, PARENT) is False
        assert _child_cache(cache_dir) is None

    def test_an_unreadable_parent_cache_never_raises(self, cache_dir) -> None:
        mod = _seed_module()
        (cache_dir / f"writ-session-{PARENT}.json").write_text("{not json")
        assert mod.seed_subagent_cache(AGENT, PARENT) is False

    def test_a_mode_less_parent_seeds_nothing(self, cache_dir) -> None:
        mod = _seed_module()
        _write_parent_cache(cache_dir, state={"mode": None, "current_phase": None})
        assert mod.seed_subagent_cache(AGENT, PARENT) is False

    def test_it_does_not_stamp_the_project(self, cache_dir) -> None:
        """A regression found by asking what the extra field touches, not by a test failing.

        `rotation._sessions_claiming_project` counts every cache whose project_root equals
        cwd, with no sub-agent exclusion, and the sole-claimant guard then refuses to carry a
        mode forward when more than one session claims the project. A sub-agent stamping its
        parent's project would make every rotation look contested and cost the user their
        mode. Sub-agent caches carried "" before this cycle and must keep doing so.
        """
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        mod.seed_subagent_cache(AGENT, PARENT)
        assert _child_cache(cache_dir)["project_root"] == ""

    def test_a_start_seed_does_not_stamp_the_project_either(self, cache_dir) -> None:
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        mod.seed_subagent_cache(AGENT, PARENT, cache_source=mod.CACHE_SOURCE_START)
        assert _child_cache(cache_dir)["project_root"] == ""

    def test_it_carries_the_role_and_its_source(self, cache_dir) -> None:
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        mod.seed_subagent_cache(AGENT, PARENT, envelope_agent_type="writ-reviewer")
        child = _child_cache(cache_dir)
        assert child["agent_type"] == "writ-reviewer"
        assert child["role_source"] == "envelope"

    def test_an_unresolvable_role_is_recorded_as_unknown(self, cache_dir, tmp_path) -> None:
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        mod.seed_subagent_cache(AGENT, PARENT, projects_dir=tmp_path / "empty-projects")
        child = _child_cache(cache_dir)
        assert child["agent_type"] == "unknown"
        assert child["role_source"] == "unresolved"

    def test_it_records_that_it_acted(self, cache_dir, tmp_path, monkeypatch) -> None:
        """The doctor's census counts lazily governed agents from this row, so a silent
        seed would be invisible in exactly the population being measured."""
        friction = tmp_path / "friction.jsonl"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(friction))
        mod = _seed_module()
        _write_parent_cache(cache_dir)
        mod.seed_subagent_cache(AGENT, PARENT)
        _require(mod, "SEED_EVENT")
        rows = [json.loads(line) for line in friction.read_text().splitlines()
                if line.strip()] if friction.exists() else []
        assert any(r.get("event") == mod.SEED_EVENT for r in rows), (
            f"the seeder wrote no {mod.SEED_EVENT} row: {rows}"
        )


# --------------------------------------------------------------------------- #
# Capabilities 9, 10, 12: seeding grants NO authority. The property, not cases.
# --------------------------------------------------------------------------- #

# Representative of every arm the gate can take: the skill-dir exemption (which precedes
# mode resolution and must stay ALLOW), an ordinary source file in another project (the
# no-mode DENY), a credential path (DENY ahead of everything), the settings exemption, a
# test file, and the two special-cased plan artifacts.
GATE_PATHS = [
    str(REPO / "writ" / "session" / "gates.py"),
    "/home/lucio.saldivar/workspaces/ai-stack/src/thing.py",
    "/home/lucio.saldivar/workspaces/ai-stack/.env",
    str(Path.home() / ".claude" / "settings.json"),
    "/home/lucio.saldivar/workspaces/ai-stack/tests/test_thing.py",
    "/home/lucio.saldivar/workspaces/ai-stack/plan.md",
    "/home/lucio.saldivar/workspaces/ai-stack/capabilities.md",
]


class TestSeedingGrantsNoAuthority:

    def _lazy_cache(self, mod) -> dict:
        return {**PARENT_STATE, "is_subagent": True, "parent_session_id": PARENT,
                "cache_source": mod.CACHE_SOURCE_LAZY}

    @pytest.mark.parametrize("path", GATE_PATHS)
    def test_decision_and_reason_match_the_no_cache_case(self, cache_dir, path) -> None:
        """THE load-bearing test of this cycle. A seeded cache must be worth exactly nothing
        at the gate: same verdict, same reason string, on every path."""
        from writ.session import gates
        from writ.session.cache import _read_cache
        mod = _seed_module()
        ungoverned = _read_cache("no-such-agent")  # the true "no cache" state
        before = gates._can_write_check(AGENT, _envelope(path), str(REPO), ungoverned)
        after = gates._can_write_check(AGENT, _envelope(path), str(REPO),
                                       self._lazy_cache(mod))
        assert (after["can_write"], after["reason"]) == (before["can_write"], before["reason"]), (
            f"seeding changed the decision for {path}: "
            f"{before['can_write']} -> {after['can_write']}"
        )

    def test_an_ordinary_source_write_is_still_refused(self, cache_dir) -> None:
        """Stated as its own case so the property test cannot pass by both sides allowing."""
        from writ.session import gates
        mod = _seed_module()
        result = gates._can_write_check(
            AGENT, _envelope("/home/lucio.saldivar/workspaces/ai-stack/src/thing.py"),
            str(REPO), self._lazy_cache(mod))
        assert result["can_write"] is False
        assert "ENF-GATE-MODE" in (result["reason"] or "")

    def test_the_bypass_needs_a_real_start(self, cache_dir) -> None:
        """Second, independent guard: even if the mode were honoured, is_subagent alone
        must not open the write path for a lazily seeded agent."""
        from writ.session import gates
        mod = _seed_module()
        cache = {"mode": None, "current_phase": None, "is_subagent": True,
                 "cache_source": mod.CACHE_SOURCE_LAZY}
        result = gates._can_write_check(
            AGENT, _envelope("/home/lucio.saldivar/workspaces/ai-stack/src/thing.py"),
            str(REPO), cache)
        assert result["can_write"] is False

    def test_a_start_hook_subagent_keeps_todays_authority(self, cache_dir) -> None:
        """The other direction: this cycle must not tighten the governed path either."""
        from writ.session import gates
        mod = _seed_module()
        cache = {**PARENT_STATE, "is_subagent": True,
                 "cache_source": mod.CACHE_SOURCE_START}
        result = gates._can_write_check(
            AGENT, _envelope("/home/lucio.saldivar/workspaces/ai-stack/src/thing.py"),
            str(REPO), cache)
        assert result["can_write"] is True

    def test_a_legacy_subagent_cache_without_the_marker_keeps_todays_authority(
        self, cache_dir
    ) -> None:
        """Caches written before this cycle carry no cache_source. Treating them as lazy
        would retroactively deny writes to agents that are governed today."""
        from writ.session import gates
        cache = {**PARENT_STATE, "is_subagent": True}
        result = gates._can_write_check(
            AGENT, _envelope("/home/lucio.saldivar/workspaces/ai-stack/src/thing.py"),
            str(REPO), cache)
        assert result["can_write"] is True

    def test_a_main_session_is_unaffected(self, cache_dir) -> None:
        """Asserts the property, not a verdict. An earlier version of this test asserted
        the write was ALLOWED and failed for an unrelated reason: PARENT_STATE carries a
        project_root with no matching plan binding, so cycle H's [ENF-GATE-DRIFT] refuses
        it. That refusal is correct and predates this cycle. What this cycle must not
        change is the mode a main session's decision is made from."""
        from writ.session import gates
        from writ.session.subagent_seed import is_lazily_seeded
        cache = dict(PARENT_STATE)
        assert is_lazily_seeded(cache) is False
        assert gates._authority_mode(cache) == "work"


# --------------------------------------------------------------------------- #
# Capabilities 5, 6: the hook-side trigger, structural rather than remembered
# --------------------------------------------------------------------------- #

class TestHookSideSeeding:

    def _subagent_envelope(self) -> str:
        return json.dumps({"session_id": PARENT, "agent_id": AGENT,
                           "agent_type": "writ-explorer", "tool_name": "Read",
                           "tool_input": {"file_path": str(REPO / "README.md")},
                           "hook_event_name": "PreToolUse"})

    def _probe(self, tmp_path) -> Path:
        """A script under hooks/scripts/ that only sources common.sh and calls
        load_hook_env: no seeding call of its own, which is the point."""
        path = tmp_path / "hooks" / "scripts" / "writ-seed-probe.sh"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'#!/usr/bin/env bash\nset -euo pipefail\nsource "{COMMON_SH}"\n'
            'load_hook_env\nexit 0\n')
        path.chmod(0o755)
        return path

    def test_a_hook_inside_a_subagent_seeds_the_cache(self, sinks, tmp_path) -> None:
        cache, friction = sinks
        _write_parent_cache(cache)
        _run_hook(self._probe(tmp_path), cache=cache, friction=friction,
                  stdin=self._subagent_envelope())
        child = _child_cache(cache)
        assert child is not None, (
            f"no cache was seeded for the sub-agent: {sorted(p.name for p in cache.iterdir())}"
        )
        assert child["mode"] == "work"
        assert child["cache_source"] == "lazy_seed"

    def test_running_it_twice_rewrites_nothing(self, sinks, tmp_path) -> None:
        cache, friction = sinks
        _write_parent_cache(cache)
        probe = self._probe(tmp_path)
        _run_hook(probe, cache=cache, friction=friction, stdin=self._subagent_envelope())
        first = _child_cache(cache)
        assert first is not None, "nothing was seeded, so idempotence is untested"
        _write_parent_cache(cache, state={**PARENT_STATE, "gates_approved": []})
        _run_hook(probe, cache=cache, friction=friction, stdin=self._subagent_envelope())
        assert _child_cache(cache) == first

    def _cache_files(self, cache: Path) -> list[str]:
        """Session caches only. The telemetry buffer the instrumentation trap appends is
        expected here and is not a cache; an earlier version of these two tests compared
        the whole directory and failed on that buffer."""
        return sorted(p.name for p in cache.glob("writ-session-*.json"))

    def test_a_main_session_hook_creates_no_child_cache(self, sinks, tmp_path) -> None:
        cache, friction = sinks
        _write_parent_cache(cache)
        before = self._cache_files(cache)
        _run_hook(self._probe(tmp_path), cache=cache, friction=friction,
                  stdin=json.dumps({"session_id": PARENT, "tool_name": "Read",
                                    "tool_input": {}, "hook_event_name": "PreToolUse"}))
        assert self._cache_files(cache) == before

    def test_an_envelope_whose_agent_equals_its_session_seeds_nothing(self, sinks,
                                                                      tmp_path) -> None:
        cache, friction = sinks
        _write_parent_cache(cache)
        before = self._cache_files(cache)
        _run_hook(self._probe(tmp_path), cache=cache, friction=friction,
                  stdin=json.dumps({"session_id": PARENT, "agent_id": PARENT,
                                    "tool_name": "Read", "tool_input": {},
                                    "hook_event_name": "PreToolUse"}))
        assert self._cache_files(cache) == before

    def test_the_hook_still_exits_zero_when_seeding_is_impossible(self, sinks,
                                                                  tmp_path) -> None:
        """No parent cache: the hook must not fail because governance could not be
        inherited. Telemetry and seeding never become enforcement failures."""
        cache, friction = sinks
        result = _run_hook(self._probe(tmp_path), cache=cache, friction=friction,
                           stdin=self._subagent_envelope())
        assert result.returncode == 0
        assert _child_cache(cache) is None


class TestStartHookUsesTheSharedSeeder:

    def test_it_marks_its_cache_as_a_start_seed(self, sinks, tmp_path) -> None:
        cache, friction = sinks
        _write_parent_cache(cache)
        _run_hook(START_HOOK, cache=cache, friction=friction,
                  stdin=json.dumps({"agent_id": AGENT, "agent_type": "writ-implementer",
                                    "session_id": PARENT,
                                    "hook_event_name": "SubagentStart"}),
                  extra_env={"WRIT_PROJECTS_DIR": str(tmp_path / "no-projects")})
        child = _child_cache(cache)
        assert child is not None, "the start hook created no cache"
        assert child["cache_source"] == "subagent_start"

    def test_its_inherited_fields_are_unchanged(self, sinks, tmp_path) -> None:
        """Refactoring the start hook onto the shared seeder must not alter what a
        sub-agent inherits, or every governed dispatch changes behaviour silently."""
        cache, friction = sinks
        _write_parent_cache(cache)
        _run_hook(START_HOOK, cache=cache, friction=friction,
                  stdin=json.dumps({"agent_id": AGENT, "agent_type": "writ-implementer",
                                    "session_id": PARENT,
                                    "hook_event_name": "SubagentStart"}),
                  extra_env={"WRIT_PROJECTS_DIR": str(tmp_path / "no-projects")})
        child = _child_cache(cache)
        assert child["mode"] == "work"
        assert child["current_phase"] == "implementation"
        assert child["gates_approved"] == ["phase-a", "test-skeletons"]
        assert child["is_subagent"] is True
        assert child["parent_session_id"] == PARENT
        assert child["agent_type"] == "writ-implementer"
        # Operational state starts clean, as it did before the refactor.
        assert child["loaded_rule_ids"] == []
        assert child["files_written"] == []


# --------------------------------------------------------------------------- #
# Capability 13: the doctor counts all four populations, from the whole corpus
# --------------------------------------------------------------------------- #

class TestDoctorGovernanceCensus:

    def _seed_stream(self, tmp_path, rows, *, gzipped_rows=()) -> Path:
        stream = tmp_path / "metrics.jsonl"
        stream.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        archive = tmp_path / "archive"
        archive.mkdir(exist_ok=True)
        if gzipped_rows:
            with gzip.open(archive / "metrics-2026-08-01.jsonl.gz", "wt") as fh:
                for row in gzipped_rows:
                    fh.write(json.dumps(row) + "\n")
        return stream

    def test_the_check_is_registered(self) -> None:
        assert GOVERNANCE_CENSUS_CHECK in doctor_check_names()

    def test_it_separates_the_four_populations(self, tmp_path, monkeypatch) -> None:
        from writ.session import doctor
        _require(doctor, "check_subagent_governance_census")
        stream = self._seed_stream(tmp_path, [
            {"event": "subagent_start", "agent_id": "a1"},
            {"event": "subagent_complete", "agent_id": "a1"},
            {"event": "subagent_seeded", "agent_id": "a2"},
            {"event": "subagent_complete", "agent_id": "a2"},
            {"event": "daemon_request", "session": "a3"},
            {"event": "subagent_complete", "agent_id": "a3",
             "role_source": "unresolved"},
            {"event": "subagent_complete", "agent_id": "a4",
             "role_source": "unresolved"},
        ])
        monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
        result = doctor.check_subagent_governance_census(doctor.DoctorOptions())
        detail = result.detail
        for expected in ("1 governed", "1 lazily", "1 "):
            assert expected in detail, f"{expected!r} missing from: {detail}"
        assert "unreachable" in detail

    def test_it_reads_gzipped_archives(self, tmp_path, monkeypatch) -> None:
        """The mistake that produced the wrong diagnosis: 226 .gz files went unread, so
        1,421 start rows looked like zero. A census that skips them lies by omission."""
        from writ.session import doctor
        stream = self._seed_stream(
            tmp_path,
            [{"event": "subagent_complete", "agent_id": "live1"}],
            gzipped_rows=[{"event": "subagent_start", "agent_id": "arch1"},
                          {"event": "subagent_complete", "agent_id": "arch1"}],
        )
        monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
        result = doctor.check_subagent_governance_census(doctor.DoctorOptions())
        assert "1 governed" in result.detail, (
            f"the archived start row was not counted: {result.detail}"
        )

    def test_no_dispatches_is_ok_not_an_accusation(self, tmp_path, monkeypatch) -> None:
        from writ.session import doctor
        stream = tmp_path / "metrics.jsonl"
        stream.write_text("")
        monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
        result = doctor.check_subagent_governance_census(doctor.DoctorOptions())
        assert result.status == "ok"

    def test_an_unreadable_stream_is_ok(self, tmp_path, monkeypatch) -> None:
        from writ.session import doctor
        monkeypatch.setattr(doctor, "stream_path",
                            lambda *a, **k: str(tmp_path / "absent.jsonl"))
        result = doctor.check_subagent_governance_census(doctor.DoctorOptions())
        assert result.status == "ok"

    def test_it_warns_when_most_dispatches_are_ungoverned(self, tmp_path,
                                                          monkeypatch) -> None:
        from writ.session import doctor
        rows = [{"event": "subagent_complete", "agent_id": f"u{i}",
                 "role_source": "unresolved"} for i in range(9)]
        rows.append({"event": "subagent_start", "agent_id": "g1"})
        rows.append({"event": "subagent_complete", "agent_id": "g1"})
        stream = self._seed_stream(tmp_path, rows)
        monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
        result = doctor.check_subagent_governance_census(doctor.DoctorOptions())
        assert result.status == "warn", result.detail
