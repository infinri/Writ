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

import errno
import gzip
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests._inventory import derive_refusing_scripts, doctor_check_names

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

# The platform's own definition of MAX_ARG_STRLEN (32 pages): debug.md evidence 5 measured
# the live parent cache at 181,529 bytes against a 131,072-byte cap on that machine.
# DERIVED, not the literal 131072, so this pins the real defect on a kernel whose page
# size differs rather than only on this one (plan dfacff61-23d5-474e-846c-2e2f0f0ea482,
# capability 11).
_MAX_ARG_STRLEN = 32 * os.sysconf("SC_PAGE_SIZE")
_OVERSIZE_MARGIN = 4096
OVERSIZE_PAD_BYTES = _MAX_ARG_STRLEN + _OVERSIZE_MARGIN


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


def _oversized_parent_state(pad_bytes: int = OVERSIZE_PAD_BYTES) -> dict:
    """PARENT_STATE plus an operational-state field padded past the platform's
    single-argument limit.

    `loaded_rules` is one of the three fields debug.md names as the real cause of
    the 181,529-byte parent (loaded_rules, token_snapshots, files_written). The
    seeder only ever copies mode/current_phase/gates_approved/project_root off the
    parent BY NAME, so this pad can never reach the child cache regardless of size
   : the assertion that matters is that the hook still runs at all against it.
    """
    return {**PARENT_STATE, "loaded_rules": ["x" * pad_bytes]}


def _injected_context(result: subprocess.CompletedProcess) -> str:
    """The `additionalContext` string from the hook's SubagentStart stdout JSON.

    Empty when the hook printed nothing (both ADDITIONAL_CONTEXT and PHASE_INFO
    empty) or when stdout was not the expected single JSON line, both of which
    are themselves meaningful failures a caller should assert on before trusting
    the string's contents.
    """
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        return (doc.get("hookSpecificOutput") or {}).get("additionalContext", "")
    return ""


def _run_hook_without_writ_dir(script: Path, *, cache: Path, friction: Path, stdin: str,
                               extra_env: dict | None = None,
                               cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Like `_run_hook`, but WITHOUT `WRIT_DIR` in the child's environment.

    `_run_hook` always sets WRIT_DIR, which is exactly the blind spot the existing
    harness has: `PLAN_DIR_INFO` reads `os.environ.get('WRIT_DIR', '')`, and the
    harness happening to supply it is the stated reason no earlier test caught the
    third defect (WRIT_DIR is a plain, unexported bash variable at line 17, so a
    real dispatch never has it in its environment either).

    Also pins `cwd` away from the repo and pins `HOME` to a scratch directory: a
    caller that left `cwd` at the repo root would let the buggy code's
    `sys.path.insert(0, '')` accidentally resolve `writ` off the current directory,
    which would mask the very defect this is built to reproduce (verified live:
    `import writ` fails from an unrelated cwd with no WRIT_DIR set, and succeeds
    from the repo root regardless of WRIT_DIR).
    """
    fake_home = (cwd or cache.parent) / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "WRIT_CACHE_DIR": str(cache),
        "WRIT_FRICTION_LOG": str(friction),
        "WRIT_PORT": "59999",
        "WRIT_NO_AUTOSTART": "1",
        "SKILL_DIR": str(REPO),
        "HOME": str(fake_home),
        **(extra_env or {}),
    }
    env.pop("WRIT_DIR", None)
    cache.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["bash", str(script)], input=stdin, capture_output=True,
                          text=True, env=env, cwd=str(cwd) if cwd else None,
                          timeout=120)


def _probe_arg_limit_enforced(pad_bytes: int) -> bool:
    """Execs a real subprocess with one argv of `pad_bytes`, and reports whether
    the platform actually raises OSError(E2BIG).

    Never assumed: capability 11 exists because a threshold that is only computed
    and never checked against a real exec could be wrong for a kernel whose cap is
    not 32 * SC_PAGE_SIZE, and a test that silently stopped reproducing the
    condition would be worse than no test at all.
    """
    try:
        subprocess.run(["true", "x" * pad_bytes], capture_output=True, timeout=30)
    except OSError as exc:
        return exc.errno == errno.E2BIG
    return False


def _require_oversized_arg_support(pad_bytes: int = OVERSIZE_PAD_BYTES, *,
                                   probe=_probe_arg_limit_enforced) -> int:
    """Return `pad_bytes` after PROVING the platform enforces the limit at that
    size, or skip with a stated reason naming the byte count.

    `probe` is injectable so capability 11's negative case (a platform that
    accepts the oversized argument) is exercised directly in
    TestOversizeProbeIsReal, rather than waited for on an actual such machine.
    """
    if not probe(pad_bytes):
        pytest.skip(
            f"platform accepted a {pad_bytes}-byte argv[1] without raising E2BIG; "
            "the derived MAX_ARG_STRLEN probe (32 * SC_PAGE_SIZE) does not hold "
            "here, so the oversized-parent regression cannot be reproduced"
        )
    return pad_bytes


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


@pytest.fixture(scope="module")
def require_platform_arg_limit() -> int:
    """The derived oversize byte count, after confirming (capability 11) that this
    platform actually enforces MAX_ARG_STRLEN at that size. Module-scoped: the
    probe execs a real subprocess, and every oversized-parent test in this module
    wants the same confirmed number, not a fresh probe each time."""
    return _require_oversized_arg_support()


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
# Plan dfacff61-23d5-474e-846c-2e2f0f0ea482, capabilities 1-3: the parent's cache
# exceeds the platform's single-argument limit (debug.md evidence 5), and the
# three python3 blocks that used to receive it as argv[1]: the seed exec, the
# phase-line block, and the plan-dir block, must all still work.
#
# Capability 4 (a small-parent dispatch inherits exactly what it inherits today) is
# already covered above by TestStartHookUsesTheSharedSeeder.test_its_inherited_
# fields_are_unchanged; that test uses a normal-sized parent and is not repeated
# here.
# --------------------------------------------------------------------------- #

class TestOversizedParentGovernanceSurvives:

    def _dispatch(self, sinks, tmp_path, pad_bytes: int, *,
                  without_writ_dir: bool = False) -> subprocess.CompletedProcess:
        cache, friction = sinks
        _write_parent_cache(cache, state=_oversized_parent_state(pad_bytes))
        stdin = json.dumps({"agent_id": AGENT, "agent_type": "writ-implementer",
                           "session_id": PARENT, "hook_event_name": "SubagentStart"})
        extra_env = {"WRIT_PROJECTS_DIR": str(tmp_path / "no-projects")}
        if without_writ_dir:
            return _run_hook_without_writ_dir(
                START_HOOK, cache=cache, friction=friction, stdin=stdin,
                extra_env=extra_env, cwd=tmp_path,
            )
        return _run_hook(START_HOOK, cache=cache, friction=friction, stdin=stdin,
                         extra_env=extra_env)

    def test_the_seed_still_creates_the_child_cache(
        self, sinks, tmp_path, require_platform_arg_limit
    ) -> None:
        """THE CENTRAL TEST. Fails today: argv[1] carries this oversized JSON and
        execve dies with E2BIG before python starts (debug.md evidence 4-5), so
        `2>/dev/null || true` swallows it and no cache is ever created."""
        result = self._dispatch(sinks, tmp_path, require_platform_arg_limit)
        assert result.returncode == 0, result.stderr
        child = _child_cache(sinks[0])
        assert child is not None, (
            f"no child cache was created against an oversized parent: {result.stderr}"
        )
        assert child["mode"] == "work"
        assert child["is_subagent"] is True
        assert child["cache_source"] == "subagent_start"

    def test_the_phase_line_names_the_real_state(
        self, sinks, tmp_path, require_platform_arg_limit
    ) -> None:
        """PHASE_INFO (line 311) reads the same oversized argv[1] and dies
        identically today, which is why the debug doc found this cycle's own
        planning sub-agent reading the '[Writ sub-agent: isolated session]'
        fallback instead of its parent's real state."""
        result = self._dispatch(sinks, tmp_path, require_platform_arg_limit)
        assert result.returncode == 0, result.stderr
        context = _injected_context(result)
        assert context, f"no context was injected at all: stdout={result.stdout!r}"
        assert "isolated session" not in context, (
            f"the phase block fell back instead of reading the parent's real "
            f"state: {context!r}"
        )
        assert "mode=work" in context, context
        assert "phase=implementation" in context, context
        assert "gates=phase-a,test-skeletons" in context, context

    def test_the_plan_dir_line_survives_with_writ_dir_absent(
        self, sinks, tmp_path, require_platform_arg_limit
    ) -> None:
        """PLAN_DIR_INFO (line 325) reads the same oversized argv[1] AND resolves
        WRIT_DIR from the environment, so this drives both defects at once: no
        WRIT_DIR in the child's env (the third, independent defect: WRIT_DIR is
        a plain, unexported bash variable, so a real dispatch never has it
        either), and a `cwd` that is not the repo, so the buggy
        `sys.path.insert(0, '')` cannot accidentally resolve `writ` off the
        current directory and mask the failure."""
        result = self._dispatch(sinks, tmp_path, require_platform_arg_limit,
                                without_writ_dir=True)
        assert result.returncode == 0, result.stderr
        context = _injected_context(result)
        assert context, f"no context was injected at all: stdout={result.stdout!r}"
        assert "Writ plan artifacts:" in context, (
            f"no plan-artifacts line was injected: {context!r}"
        )
        assert PARENT in context, (
            f"the plan directory must be scoped to the PARENT session, not the "
            f"worker's own agent id: {context!r}"
        )


# --------------------------------------------------------------------------- #
# Plan dfacff61-23d5-474e-846c-2e2f0f0ea482, capability 5: no parent session at
# all is not this cycle's bug, but the refactor described in the plan's section 5
# replaces the python shape that renders it ('{}' rather than a cache read), so
# this pins that the refactor does not regress the existing critical or the
# phase-line default.
# --------------------------------------------------------------------------- #

class TestNoParentSessionInPayload:

    def _envelope(self) -> str:
        return json.dumps({"agent_id": AGENT, "agent_type": "writ-explorer",
                           "hook_event_name": "SubagentStart"})

    def test_it_still_records_the_existing_critical_and_creates_no_cache(
        self, sinks, tmp_path
    ) -> None:
        cache, friction = sinks
        result = _run_hook(START_HOOK, cache=cache, friction=friction,
                           stdin=self._envelope(),
                           extra_env={"WRIT_PROJECTS_DIR": str(tmp_path / "no-projects")})
        assert result.returncode == 0, result.stderr
        assert "no parent session in payload" in result.stderr, result.stderr
        assert _child_cache(cache) is None

    def test_the_phase_line_falls_back_to_the_documented_defaults(
        self, sinks, tmp_path
    ) -> None:
        cache, friction = sinks
        result = _run_hook(START_HOOK, cache=cache, friction=friction,
                           stdin=self._envelope(),
                           extra_env={"WRIT_PROJECTS_DIR": str(tmp_path / "no-projects")})
        assert result.returncode == 0, result.stderr
        context = _injected_context(result)
        assert "mode=work, phase=planning, gates=none" in context, context


# --------------------------------------------------------------------------- #
# Plan dfacff61-23d5-474e-846c-2e2f0f0ea482, capabilities 6-7: the seed exec
# cannot be reached directly (the seeder already passes in isolation; debug.md
# evidence 1), so it is triggered through an oversized `agent_type`: the plan's
# named trigger. ROLE_RESOLUTION's own env-based exec fails on the same limit,
# leaving AGENT_TYPE at its raw oversized value, and THAT is what the seed exec
# then dies on.
# --------------------------------------------------------------------------- #

class TestSeedFailureIsVisible:

    def _envelope_with_oversized_role(self, pad_bytes: int) -> str:
        return json.dumps({"session_id": PARENT, "agent_id": AGENT,
                           "agent_type": "x" * pad_bytes,
                           "hook_event_name": "SubagentStart"})

    def test_the_hook_still_exits_zero_and_creates_no_cache(
        self, sinks, tmp_path, require_platform_arg_limit
    ) -> None:
        """A dispatch must never fail because governance could not be inherited."""
        cache, friction = sinks
        _write_parent_cache(cache)
        result = _run_hook(
            START_HOOK, cache=cache, friction=friction,
            stdin=self._envelope_with_oversized_role(require_platform_arg_limit),
            extra_env={"WRIT_PROJECTS_DIR": str(tmp_path / "no-projects")},
        )
        assert result.returncode == 0, result.stderr
        assert _child_cache(cache) is None

    def test_it_prints_a_critical_line_naming_the_hook(
        self, sinks, tmp_path, require_platform_arg_limit
    ) -> None:
        cache, friction = sinks
        _write_parent_cache(cache)
        result = _run_hook(
            START_HOOK, cache=cache, friction=friction,
            stdin=self._envelope_with_oversized_role(require_platform_arg_limit),
            extra_env={"WRIT_PROJECTS_DIR": str(tmp_path / "no-projects")},
        )
        assert "[WRIT CRITICAL] writ-subagent-start" in result.stderr, result.stderr

    def test_it_records_exactly_one_bounded_seed_failed_row(
        self, sinks, tmp_path, require_platform_arg_limit
    ) -> None:
        """The row must carry the hook name and the parent session and NOTHING
        derived from the oversized value that killed the seed block, a row
        built from that value would die exactly where the block did."""
        cache, friction = sinks
        _write_parent_cache(cache)
        _run_hook(
            START_HOOK, cache=cache, friction=friction,
            stdin=self._envelope_with_oversized_role(require_platform_arg_limit),
            extra_env={"WRIT_PROJECTS_DIR": str(tmp_path / "no-projects")},
        )
        rows = [json.loads(line) for line in friction.read_text().splitlines()
                if line.strip()] if friction.exists() else []
        failed = [r for r in rows if r.get("event") == "subagent_seed_failed"]
        assert len(failed) == 1, f"expected exactly one subagent_seed_failed row: {rows}"
        row = failed[0]
        assert row.get("session") == AGENT
        assert row.get("hook") == "writ-subagent-start"
        assert row.get("parent_session") == PARENT
        assert set(row) <= {"ts", "session", "mode", "event", "hook", "parent_session"}, (
            f"the row carries a field beyond the bounded set: {row}"
        )


# --------------------------------------------------------------------------- #
# Plan dfacff61-23d5-474e-846c-2e2f0f0ea482, capability 8.
# --------------------------------------------------------------------------- #

class TestSeedFailedEventClassification:

    def test_it_classifies_to_the_metrics_stream(self) -> None:
        """The governance census reads the metrics stream (writ/session/doctor.py
        ::_subagent_governance_census), beside subagent_start and
        subagent_seeded."""
        from writ.shared.logging import stream_for
        assert stream_for("subagent_seed_failed") == "metrics"


# --------------------------------------------------------------------------- #
# Plan dfacff61-23d5-474e-846c-2e2f0f0ea482, capability 11.
# --------------------------------------------------------------------------- #

class TestOversizeProbeIsReal:

    def test_the_derived_threshold_is_actually_enforced_here(self) -> None:
        """The positive control every oversized-parent test in this module rests
        on: the probe must confirm the premise on THIS machine, not merely assume
        it."""
        assert _probe_arg_limit_enforced(OVERSIZE_PAD_BYTES) is True, (
            "the platform accepted an oversized argv[1] without E2BIG; every "
            "oversized-parent test in this module rests on this being true"
        )

    def test_a_platform_that_accepts_the_argument_skips_with_a_stated_reason(
        self,
    ) -> None:
        """Simulates the negative case directly: a probe reporting that the
        platform accepted the oversized argument must produce a SKIP naming the
        byte count, never a silent pass."""
        with pytest.raises(pytest.skip.Exception) as exc_info:
            _require_oversized_arg_support(OVERSIZE_PAD_BYTES, probe=lambda pad: False)
        assert str(OVERSIZE_PAD_BYTES) in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Plan dfacff61-23d5-474e-846c-2e2f0f0ea482, capability 12.
# --------------------------------------------------------------------------- #

class TestNoRefusalSurfaceAdded:

    def test_the_hook_is_still_absent_from_the_refusing_script_set(self) -> None:
        """This cycle adds a writ_critical call and a friction row, neither of
        which is a refusal mechanism (no emit_deny/emit_ask, no nonzero exit, no
        permissionDecision literal), so the fire drill's completeness check needs
        no new entry for this hook."""
        assert "writ-subagent-start.sh" not in derive_refusing_scripts()


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

    def test_a_seed_failure_is_not_counted_as_governed(self, tmp_path,
                                                       monkeypatch) -> None:
        """Plan dfacff61-23d5-474e-846c-2e2f0f0ea482, capability 9: a
        `subagent_start` row alone no longer proves an agent was governed --
        today's defect is that row firing unconditionally near the end of the
        hook even when the seed exec upstream of it died. `governed` must
        subtract any agent with an unrepaired `subagent_seed_failed` row, and the
        failure must be counted on its own rather than folded into
        `unreachable` (which means 'no Writ hook ran inside it at all', a
        stronger and different claim than 'a hook ran and seeding failed')."""
        from writ.session import doctor
        stream = self._seed_stream(tmp_path, [
            {"event": "subagent_start", "agent_id": "a1"},
            {"event": "subagent_seed_failed", "agent_id": "a1"},
            {"event": "subagent_complete", "agent_id": "a1"},
            {"event": "subagent_start", "agent_id": "a2"},
            {"event": "subagent_complete", "agent_id": "a2"},
        ])
        monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
        census = doctor._subagent_governance_census()
        assert census is not None
        assert "seed_failed" in census, f"no seed_failed bucket in the census: {census}"
        assert census["seed_failed"] == 1
        assert census["governed"] == 1, (
            f"a1 has an unrepaired subagent_seed_failed row and must not count "
            f"as governed: {census}"
        )
        assert census["unreachable"] == 0, (
            f"a1's Writ hook DID run (subagent_start fired); it must not fall "
            f"into the never-reached population: {census}"
        )

    def test_a_spawn_marked_seeded_row_joins_governed_not_lazy(self, tmp_path,
                                                               monkeypatch) -> None:
        """Plan dfacff61-23d5-474e-846c-2e2f0f0ea482, capability 10: once spawn
        seeding works again, a governed agent ALSO gets a `subagent_seeded` row
        (the seeder logs it on every path, START included), and counting every
        such row as lazy would double the two figures. `lazy` narrows to rows
        whose `cache_source` is not `subagent_start`; a row with none at all (the
        pre-cycle shape) keeps its current lazy meaning."""
        from writ.session import doctor
        stream = self._seed_stream(tmp_path, [
            {"event": "subagent_start", "agent_id": "a1"},
            {"event": "subagent_seeded", "agent_id": "a1",
             "cache_source": "subagent_start"},
            {"event": "subagent_complete", "agent_id": "a1"},
            {"event": "subagent_seeded", "agent_id": "a2", "cache_source": "lazy_seed"},
            {"event": "subagent_complete", "agent_id": "a2"},
            {"event": "subagent_seeded", "agent_id": "a3"},
            {"event": "subagent_complete", "agent_id": "a3"},
        ])
        monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
        census = doctor._subagent_governance_census()
        assert census is not None
        assert census["governed"] == 1, (
            f"a1 fired both subagent_start and a spawn-marked subagent_seeded "
            f"row; it is one governed dispatch, not two: {census}"
        )
        assert census["lazy"] == 2, (
            f"a2 (cache_source=lazy_seed) and a3 (no cache_source at all, the "
            f"pre-cycle legacy meaning) both stay lazy; a1's spawn-marked row "
            f"must not join them: {census}"
        )
