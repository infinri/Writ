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
import re
import subprocess
from collections import Counter
from pathlib import Path
from unittest import mock

import pytest

from tests._inventory import derive_refusing_scripts, doctor_check_names
from tests._strace import trace_execve

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


# --------------------------------------------------------------------------- #
# Plan 2412ba38-51e1-4b73-895b-7b240a3c21d3, capabilities 1-16: the census that
# cannot see the population it counts.
#
# Two defects, one mechanism. The census keys every bucket AND its own
# denominator on `subagent_complete`, so a lazily seeded agent (which by
# construction never produces that row) is invisible in every field including
# `total`; and the lazy seed path reports nothing on any failure, so the gap the
# first defect hides cannot be ruled out from the logs either.
#
# EVERY TEST BELOW DRIVES THE REAL READER. The plan rejects a
# `classify(evidence) -> bucket` seam on purpose: the reader (archive globbing,
# gzip, the agent/session fallback) is where this function's history of wrong
# answers lives, so each census test writes real JSONL rows and calls
# `_subagent_governance_census`, and the partition is asserted over gzipped
# archive evidence as well as the live file.
#
# NO LIVE FIGURE IS ASSERTED AS A LITERAL. The measurements in the plan (9 lazy
# rows, 10 lazy caches, 520 governed, 6430 total) are what the live corpus must
# satisfy, not constants: the stream grows during a session (6430 -> 6440 in an
# hour), so the operational tests assert thresholds DERIVED from the non-log
# artifact and relationships between buckets, and the bucket names are read from
# the census's own return value (`set(census) - {"total"}`) so a bucket added
# later joins the sum automatically.
# --------------------------------------------------------------------------- #

from writ.shared.state_root import default_log_root, state_root  # noqa: E402

LIVE_METRICS = Path(default_log_root()) / "github.com" / "infinri" / "Writ" / "metrics.jsonl"
LIVE_CACHE_DIR = Path(state_root()) / "session"
CACHE_PREFIX = "writ-session-"

SPAWN_SEED = "subagent_start"
LAZY_SEED = "lazy_seed"
SEED_FAILED_EVENT = "subagent_seed_failed"
LAZY_SEED_HOOK = "seed-subagent-cache"

# Lengths, not counts of anything measured. A failure row is a gap report: an agent, the
# path it failed on and a short reason. A row built from the value that killed the seed
# would die exactly where the seed died, so "bounded" is the property under test.
BOUNDED_ROW_MAX_CHARS = 400
BOUNDED_REASON_MAX_CHARS = 200

# Successful execve only. An execve ATTEMPT overcounts a PATH-resolved binary (one failed
# attempt per PATH entry, all inside the SAME already-forked child), so attempts are never
# quoted as a process count. See tests/test_write_path_process_budget.py.
_EXECVE_OK = re.compile(r"\)\s+= 0$")
# The exec that died: the oversized envelope value the dead-python arm is built from. This
# is the positive control for that arm, not decoration: a run that seeded nothing for some
# OTHER reason would otherwise be read as a reproduction of the condition.
_EXECVE_E2BIG = re.compile(r"=\s+-1\s+E2BIG")


def _start_row(agent: str) -> dict:
    return {"event": "subagent_start", "agent_id": agent}


def _seeded_row(agent: str, cache_source: str | None = None) -> dict:
    row = {"event": "subagent_seeded", "agent_id": agent}
    if cache_source is not None:
        row["cache_source"] = cache_source
    return row


def _seed_failed_row(agent: str) -> dict:
    """The row the lazy path writes, which files the agent under `session` and carries no
    `agent_id` at all: the census's agent/session fallback is part of what is under test."""
    return {"event": SEED_FAILED_EVENT, "session": agent, "hook": LAZY_SEED_HOOK}


def _complete_row(agent: str) -> dict:
    return {"event": "subagent_complete", "agent_id": agent}


def _hook_row(session: str) -> dict:
    """Any non-lifecycle row filed under a session's own id. For a sub-agent it proves a
    Writ hook ran inside it (reachable); for a main session it proves nothing about
    sub-agents at all, which is the distinction capability 5 exists to hold."""
    return {"event": "daemon_request", "session": session}


def _metrics_stream(tmp_path: Path, live_rows, archived_rows=()) -> Path:
    stream = tmp_path / "metrics.jsonl"
    stream.write_text("".join(json.dumps(r) + "\n" for r in live_rows))
    if archived_rows:
        archive = tmp_path / "archive"
        archive.mkdir(exist_ok=True)
        with gzip.open(archive / "metrics-2026-09-01.jsonl.gz", "wt") as fh:
            for row in archived_rows:
                fh.write(json.dumps(row) + "\n")
    return stream


def _census_over(monkeypatch, stream: Path) -> dict:
    from writ.session import doctor
    monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
    census = doctor._subagent_governance_census()
    assert census is not None, f"the census read nothing from {stream}"
    return census


def _check_over(monkeypatch, stream: Path):
    from writ.session import doctor
    monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
    return doctor.check_subagent_governance_census(doctor.DoctorOptions())


def _members_over(monkeypatch, stream: Path):
    """The census's members by name, or None when the accessor does not exist yet.

    Capability 8 compares the lazy bucket's agent ids BY NAME against the on-disk caches,
    which a counts-only return cannot answer. Returning None rather than raising keeps the
    RED state a readable assertion inside the test instead of an AttributeError.
    """
    from writ.session import doctor
    monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
    reader = getattr(doctor, "_subagent_governance_members", None)
    return None if reader is None else reader()


def _bucket_names(census: dict) -> set:
    """Read from the census's own return value, so a bucket added later joins the sum
    instead of silently leaking out of the partition."""
    return set(census) - {"total"}


def _partition_gap(census: dict) -> int:
    """`total` minus the sum of every bucket. Zero, or the census is not a partition."""
    return census["total"] - sum(census[name] for name in _bucket_names(census))


def _population() -> list:
    """One agent per evidence shape the precedence ladder has to rule on, with the bucket
    it is owed. The population IS the fixture: expected counts are derived from this table
    rather than typed, so an agent added here joins the assertion automatically.
    """
    table = [
        ("g-start-and-completion", "governed", [_start_row, _complete_row]),
        ("g-start-no-completion", "governed", [_start_row]),
        # THE LEAK, pinned by construction rather than by a probe row happening to exist:
        # a spawn-marked seed row with no start row falls into no bucket at all today,
        # which is the live off-by-one (the buckets sum to one less than `total`).
        ("g-spawn-seed-no-start", "governed",
         [lambda a: _seeded_row(a, SPAWN_SEED), _complete_row]),
        ("g-start-and-spawn-seed", "governed",
         [_start_row, lambda a: _seeded_row(a, SPAWN_SEED), _complete_row]),
        ("l-lazy-seed-only", "lazy", [lambda a: _seeded_row(a, LAZY_SEED)]),
        ("l-start-and-lazy-seed", "lazy", [_start_row, lambda a: _seeded_row(a, LAZY_SEED)]),
        ("l-bare-seed", "lazy", [_seeded_row, _complete_row]),
        ("l-repaired-failure", "lazy",
         [_seed_failed_row, lambda a: _seeded_row(a, LAZY_SEED)]),
        ("f-unrepaired-only", "seed_failed", [_seed_failed_row]),
        ("f-unrepaired-with-start", "seed_failed", [_start_row, _seed_failed_row]),
        ("r-completion-and-hook-rows", "reachable", [_complete_row, _hook_row]),
        ("u-completion-only", "unreachable", [_complete_row]),
    ]
    return [(agent, bucket, [make(agent) for make in makers])
            for agent, bucket, makers in table]


def _population_rows(population) -> list:
    return [row for _, _, rows in population for row in rows]


def _expected_counts(population) -> Counter:
    return Counter(bucket for _, bucket, _ in population)


# The buckets `check_subagent_governance_census` sums into `covered`, written as a literal
# and never imported from doctor.py: an expectation read out of the code under test cannot
# disagree with it.
COVERED_BUCKETS = ("governed", "lazy")


def _boundary_population() -> dict:
    """The population that sits ON the threshold, keyed by agent name, each value the
    bucket that agent is owed and the builders that produce its rows.

    Two governed, one lazy, one seed_failed, one reachable, one unreachable, reusing the
    row shapes `_population()` already establishes. Covered is the governed plus the lazy
    and total is the map's length, so `covered * 2 == total` falls out of the map's own
    contents and neither number is typed: an agent added here joins both.
    """
    return {
        "b-governed-start-and-completion": ("governed", [_start_row, _complete_row]),
        "b-governed-start-and-spawn-seed": (
            "governed",
            [_start_row, lambda a: _seeded_row(a, SPAWN_SEED), _complete_row]),
        "b-lazy-seed-only": ("lazy", [lambda a: _seeded_row(a, LAZY_SEED)]),
        "b-unrepaired-failure": ("seed_failed", [_seed_failed_row]),
        "b-completion-and-hook-rows": ("reachable", [_complete_row, _hook_row]),
        "b-completion-only": ("unreachable", [_complete_row]),
    }


def _boundary_rows(population: dict) -> list:
    return [make(agent) for agent, (_bucket, makers) in population.items()
            for make in makers]


def _owed_counts(population: dict) -> Counter:
    return Counter(bucket for bucket, _makers in population.values())


def _covered_in(population: dict) -> int:
    return sum(1 for bucket, _makers in population.values() if bucket in COVERED_BUCKETS)


def _census_of_population(monkeypatch, stream: Path, population: dict) -> dict:
    """The census the REAL reader returns over the population's rows, proven first to BE
    the population the map describes.

    THE TWO SIDES ARE PRODUCED BY DIFFERENT MECHANISMS. The left side is
    `_subagent_governance_census` classifying JSONL rows it read back off disk; the right
    side is the map, which no census code produced. This is what stops an ok verdict coming
    from a population that never reached the boundary: an agent classified into a bucket the
    map did not assign it fails here, before any verdict is read.
    """
    census = _census_over(monkeypatch, stream)
    assert population, "the boundary population is empty, so every count below is zero"
    owed = _owed_counts(population)
    assert census["total"] == len(population), (
        f"the reader counted {census['total']} agent(s) against the {len(population)} the "
        f"map names: {sorted(population)}"
    )
    for bucket in _bucket_names(census):
        assert census[bucket] == owed[bucket], (
            f"{bucket}: the census counted {census[bucket]} against the {owed[bucket]} "
            f"agent(s) the map assigns it "
            f"({sorted(a for a, (b, _m) in population.items() if b == bucket)})"
        )
    return census


def _live_cache_ids(cache_source: str) -> set:
    """Agent ids whose on-disk session cache declares `cache_source`.

    THE NON-LOG ARTIFACT. The log rows and these files are written by different code on
    different paths, so agreement between them is corroboration and disagreement is a
    finding; a count reconciled between two readings of the same log is neither.
    """
    ids = set()
    for path in LIVE_CACHE_DIR.glob(CACHE_PREFIX + "*.json"):
        try:
            cache = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(cache, dict) and cache.get("cache_source") == cache_source:
            ids.add(path.name[len(CACHE_PREFIX):-len(".json")])
    return ids


# The four events the census builds its universe from (`known` in
# writ/session/doctor.py::_subagent_governance_evidence). The live-corpus guards below
# count rows of exactly these kinds.
_LIVE_LIFECYCLE_EVENTS = frozenset({
    "subagent_start", "subagent_seeded", SEED_FAILED_EVENT, "subagent_complete",
})


def _live_lifecycle_rows() -> int:
    """Rows of the kind the census counts, across the live stream AND its archives.

    THE EVIDENCE, NOT A PATH. The guard this replaces was `LIVE_METRICS.exists()`,
    keyed on a file being present, and a file is present the moment ANY row lands in
    it. In CI one unrelated `config_resolved` row was written into this stream during
    collection, so the path existed while the census population was empty and the
    guard never fired: the class ran against nothing and failed. Keying on the event
    kind is what a stray row of some other kind cannot satisfy.

    Reads the RAW artifact, never `_subagent_governance_census`'s return value. A
    guard derived from the value under test would absorb
    `test_the_six_buckets_partition_the_live_universe`'s `total > 0` assertion: a
    machine that HAS lifecycle rows whose census reports zero must still fail, and it
    still does.
    """
    rows = 0
    candidates = [LIVE_METRICS]
    candidates += sorted((LIVE_METRICS.parent / "archive").glob("metrics-*.jsonl*"))
    for path in candidates:
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", errors="replace") as handle:  # type: ignore[operator]
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("event") in _LIVE_LIFECYCLE_EVENTS:
                        rows += 1
        except OSError:
            continue
    return rows


def _require_live_lifecycle_rows() -> None:
    """Abstain when this machine holds no accumulated sub-agent history to census."""
    if _live_lifecycle_rows() == 0:
        pytest.skip(
            "no sub-agent lifecycle rows "
            f"({', '.join(sorted(_LIVE_LIFECYCLE_EVENTS))}) in {LIVE_METRICS} or its "
            "archives, so the operational corroboration has no corpus to run against "
            "on this machine"
        )


def _require_live_caches(cache_source: str) -> set:
    """The on-disk caches declaring `cache_source`, or abstain when there are none.

    These files are the NON-LOG artifact the by-name tests corroborate the census
    against, so with none of them on disk the comparison has no subject at all. That
    is a machine without a corpus, not a finding. Nothing is absorbed: the judges
    those tests keep are the set relationships (`caches & known`, `unseen`,
    `contradicted`) and the derived floor, and a missing corpus cannot satisfy any of
    them, it only stops them from being asked.
    """
    caches = _live_cache_ids(cache_source)
    if not caches:
        pytest.skip(
            f"no cache under {LIVE_CACHE_DIR} carries cache_source={cache_source!r}, "
            "so this machine has no accumulated agent history to corroborate the "
            "census against"
        )
    return caches


def _seed_probe_hook(root: Path) -> Path:
    """A hook that only sources common.sh and calls `load_hook_env`.

    The lazy seed is reached through the helper EVERY hook inherits, so a probe with no
    seeding call of its own is the honest caller: the claims under test (exit 0, nothing on
    stdout, exactly one row per failure) live in bash and at an exec boundary and cannot be
    proven by patching the seeder in-process.
    """
    path = root / "hooks" / "scripts" / "writ-seed-probe.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'#!/usr/bin/env bash\nset -euo pipefail\nsource "{COMMON_SH}"\n'
        'load_hook_env\nexit 0\n')
    path.chmod(0o755)
    return path


def _subagent_stdin(agent: str = AGENT, agent_type: str = "writ-explorer",
                    session: str = PARENT) -> str:
    return json.dumps({"session_id": session, "agent_id": agent, "agent_type": agent_type,
                       "tool_name": "Read", "tool_input": {},
                       "hook_event_name": "PreToolUse"})


def _friction_rows(path: Path, event: str | None = None) -> list:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if event is None or r.get("event") == event]


@pytest.fixture()
def unwritable_cache(tmp_path):
    """A cache dir holding a readable parent cache that no process may write into.

    Drives the `mutate_cache` fault site: `os.open` of the per-session lock file raises
    PermissionError, which is a genuine fault rather than a decline (verified: the parent
    cache still READS, so the seeder gets past the ids, the existing-cache check and the
    inherited mode, and dies only on the write).
    """
    if os.geteuid() == 0:
        pytest.skip("running as root: directory permissions do not refuse a write, so the "
                    "cache-write fault cannot be reproduced here")
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_parent_cache(cache)
    os.chmod(cache, 0o555)
    yield cache
    os.chmod(cache, 0o755)


class TestCensusUniverseWidens:
    """Capabilities 1, 2, 6: the universe is every agent named by a lifecycle row, not
    every agent that produced a completion row."""

    def test_a_lazily_seeded_agent_with_no_completion_row_is_counted(self, tmp_path,
                                                                    monkeypatch) -> None:
        """Capability 1. The agent this check exists to find is the one that cannot
        produce the row the check keys on: no SubagentStart means no `subagent_complete`
        either, so today it is absent from every bucket AND from the denominator."""
        census = _census_over(monkeypatch, _metrics_stream(tmp_path, [
            _seeded_row("l1", LAZY_SEED),
        ]))
        assert census["lazy"] == 1, (
            f"a lazy_seed row with no completion row is the whole population this check "
            f"exists to count: {census}"
        )
        assert census["total"] == 1, f"the agent is not in the denominator either: {census}"
        assert _partition_gap(census) == 0, f"the buckets do not sum to total: {census}"

    def test_a_started_agent_with_no_completion_row_is_governed(self, tmp_path,
                                                                monkeypatch) -> None:
        """Capability 2. Widening `total` while leaving the other buckets keyed on
        `completed` would produce parts that do not sum to their total, which is worse
        than the bug."""
        census = _census_over(monkeypatch, _metrics_stream(tmp_path, [_start_row("g1")]))
        assert census["governed"] == 1, census
        assert census["total"] == 1, census
        assert _partition_gap(census) == 0, f"the buckets do not sum to total: {census}"

    def test_an_unrepaired_seed_failure_with_no_completion_row_is_counted(
        self, tmp_path, monkeypatch
    ) -> None:
        """Capability 2. A failure row is the strongest evidence there is that Writ ran
        inside an agent, and today it is dropped unless the agent also completed."""
        census = _census_over(monkeypatch,
                              _metrics_stream(tmp_path, [_seed_failed_row("f1")]))
        assert census["seed_failed"] == 1, census
        assert census["total"] == 1, census
        assert _partition_gap(census) == 0, f"the buckets do not sum to total: {census}"

    def test_completion_only_agents_still_split_into_reachable_and_unreachable(
        self, tmp_path, monkeypatch
    ) -> None:
        """Capability 6, the other direction: the widening must not move the agents that
        are already classified correctly. The lazy agent is in the fixture because a
        version of this that only held completion rows would pass before the change and
        prove nothing about the widened universe."""
        census = _census_over(monkeypatch, _metrics_stream(tmp_path, [
            _complete_row("r1"), _hook_row("r1"),
            _complete_row("u1"),
            _seeded_row("l1", LAZY_SEED),
        ]))
        assert census["reachable"] == 1, f"r1 filed a row under its own id: {census}"
        assert census["unreachable"] == 1, f"u1 ran no Writ hook at all: {census}"
        assert census["total"] == 3, (
            f"r1, u1 and l1 are all sub-agents Writ has a lifecycle record of: {census}"
        )
        assert _partition_gap(census) == 0, f"the buckets do not sum to total: {census}"


class TestCensusExcludesMainSessions:
    """Capability 5. `active` stays a QUALIFIER, never a member-adding population.

    Load bearing, and the highest-value test of this cycle: `active` collects
    `row["session"]` for every row whose event is none of the four lifecycle events, and a
    main session's rows carry a session with no `agent_id`, so every main session Writ has
    ever logged is in it. Measured 2026-09-15: `active` holds 10,032 sessions of which
    3,590 are not sub-agents at all, so a member-adding `active` would take `total` from
    6,440 to 10,064 and the census would report the machine's whole history as ungoverned
    sub-agents. Today the intersection with `completed` filters them out BY ACCIDENT; after
    the widening that filter has to be explicit.
    """

    def test_a_main_sessions_hook_rows_never_enter_the_total(self, tmp_path,
                                                             monkeypatch) -> None:
        agents = ["g1", "l1"]
        mains = ["main-1", "main-2", "main-3"]
        rows = [_start_row("g1"), _complete_row("g1"), _seeded_row("l1", LAZY_SEED)]
        rows += [_hook_row(m) for m in mains]
        census = _census_over(monkeypatch, _metrics_stream(tmp_path, rows))
        assert census["total"] == len(agents), (
            f"only {agents} are sub-agents; {mains} filed rows under their own session id "
            f"and named no sub-agent lifecycle event at all: {census}"
        )
        assert census["reachable"] == 0, (
            f"a main session is not an ungoverned sub-agent that a hook could seed: {census}"
        )
        assert census["unreachable"] == 0, census

    def test_a_main_session_is_named_in_no_bucket(self, tmp_path, monkeypatch) -> None:
        """The same boundary by NAME, so the count assertion above cannot pass by two
        errors cancelling."""
        rows = [_start_row("g1"), _complete_row("g1"), _hook_row("main-1")]
        members = _members_over(monkeypatch, _metrics_stream(tmp_path, rows))
        assert members is not None, (
            "skeleton: writ/session/doctor.py has no _subagent_governance_members yet; "
            "capability 8 compares the lazy bucket's ids BY NAME against the on-disk "
            "caches, which a counts-only return cannot answer"
        )
        named = set().union(*(set(ids) for ids in members.values()))
        assert "main-1" not in named, (
            f"a main session entered the census universe: {sorted(named)}"
        )
        assert named == {"g1"}, f"the universe is exactly the sub-agents: {sorted(named)}"


class TestCensusPartition:
    """Capabilities 3, 4, 6: the five buckets partition the universe, by construction."""

    @pytest.mark.parametrize("placement", ["live", "archive"])
    def test_the_buckets_partition_the_universe(self, tmp_path, monkeypatch,
                                                placement) -> None:
        """THE CENTREPIECE. Every agent in the population carries a different combination
        of evidence and must land in exactly one bucket, so the parts sum to their total
        rather than nearly summing to it.

        The `archive` parametrization is not decoration: 226 gzipped files went unread by
        the grep that produced the wrong diagnosis in the first place, and a census that
        classified only the live file would repeat that mistake one bucket at a time.
        """
        population = _population()
        rows = _population_rows(population)
        stream = (_metrics_stream(tmp_path, rows) if placement == "live"
                  else _metrics_stream(tmp_path, [_hook_row("main-1")], archived_rows=rows))
        census = _census_over(monkeypatch, stream)
        expected = _expected_counts(population)
        assert census["total"] == len(population), (
            f"every agent named by a lifecycle row is a member: {census}"
        )
        assert _partition_gap(census) == 0, (
            f"the buckets leak: {census['total']} members against "
            f"{sum(census[b] for b in _bucket_names(census))} classified; "
            f"expected {dict(expected)}"
        )
        for bucket in _bucket_names(census):
            assert census[bucket] == expected[bucket], (
                f"{bucket}: {census[bucket]} against {expected[bucket]} from "
                f"{[a for a, b, _ in population if b == bucket]}"
            )

    def test_a_spawn_marked_seed_with_no_start_row_is_not_lost(self, tmp_path,
                                                               monkeypatch) -> None:
        """The live off-by-one, isolated. `ungoverned` subtracts `seeded` while `governed`
        only adds `completed & started`, so an agent with a seed row and no start row falls
        into no bucket at all: the reported run sums to one less than its own total, and
        its only live instance is a test probe row, which is not a thing to pin a
        regression on."""
        census = _census_over(monkeypatch, _metrics_stream(tmp_path, [
            _seeded_row("probe-agent-1", SPAWN_SEED), _complete_row("probe-agent-1"),
        ]))
        assert census["total"] == 1, census
        assert _partition_gap(census) == 0, (
            f"the agent is in the denominator and in no bucket: {census}"
        )
        assert census["governed"] == 1, (
            f"a spawn-marked seed row is the strongest positive record that the spawn path "
            f"made the cache, stronger than the start row itself: {census}"
        )

    def test_precedence_gives_each_agent_one_bucket_only(self, tmp_path,
                                                         monkeypatch) -> None:
        """Capability 4. Each agent here would qualify for two buckets under a set-algebra
        reading; the ladder classifies each member exactly once."""
        census = _census_over(monkeypatch, _metrics_stream(tmp_path, [
            _start_row("a-lazy"), _seeded_row("a-lazy", LAZY_SEED),
            _start_row("a-governed"), _seeded_row("a-governed", SPAWN_SEED),
            _start_row("a-failed"), _seed_failed_row("a-failed"),
        ]))
        assert census["total"] == 3, census
        assert census["lazy"] == 1, (
            f"a lazy_seed cache confers nothing at the gate, so it outranks a bare start "
            f"row rather than being counted beside it: {census}"
        )
        assert census["governed"] == 1, census
        assert census["seed_failed"] == 1, (
            f"an unrepaired failure outranks both: {census}"
        )
        assert _partition_gap(census) == 0, f"an agent was counted twice: {census}"

    def test_the_partition_check_can_see_a_leak(self) -> None:
        """The conditionality control for `_partition_gap`. A detector that cannot fire is
        worth nothing, and this repo has shipped two tests that passed because both sides
        returned None."""
        intact = {"governed": 1, "lazy": 1, "seed_failed": 0, "reachable": 0,
                  "unreachable": 0, "total": 2}
        leaking = {**intact, "total": 3}
        doubled = {**intact, "lazy": 2}
        assert _partition_gap(intact) == 0
        assert _partition_gap(leaking) == 1, (
            "the partition assertion would pass over a census that leaks an agent"
        )
        assert _partition_gap(doubled) == -1, (
            "the partition assertion would pass over a census that counts an agent twice"
        )

    def test_a_bucket_added_later_joins_the_sum(self) -> None:
        """`set(census) - {"total"}` is read from the return value, so a sixth bucket is
        summed the day it exists rather than the day someone remembers to add it here."""
        census = {"governed": 1, "lazy": 0, "seed_failed": 0, "reachable": 0,
                  "unreachable": 0, "quarantined": 1, "total": 2}
        assert "quarantined" in _bucket_names(census)
        assert _partition_gap(census) == 0


class TestCensusMembersNameTheAgents:

    def test_the_members_agree_with_the_counts_bucket_for_bucket(self, tmp_path,
                                                                 monkeypatch) -> None:
        """The two readings cannot drift: a by-name corroboration is worthless if the names
        come from a second classifier that the published counts do not use."""
        population = _population()
        stream = _metrics_stream(tmp_path, _population_rows(population))
        members = _members_over(monkeypatch, stream)
        assert members is not None, (
            "skeleton: writ/session/doctor.py has no _subagent_governance_members yet"
        )
        census = _census_over(monkeypatch, stream)
        assert set(members) == _bucket_names(census), (
            f"members and counts disagree about the buckets: {sorted(members)} against "
            f"{sorted(_bucket_names(census))}"
        )
        expected = _expected_counts(population)
        for bucket, ids in members.items():
            assert len(set(ids)) == expected[bucket] == census[bucket], (
                f"{bucket}: {sorted(ids)} and a published count of {census[bucket]} "
                f"against the {expected[bucket]} agent(s) the population owes it"
            )
        for agent, bucket, _rows in population:
            assert agent in set(members[bucket]), (
                f"{agent} is owed {bucket} and the members put it in "
                f"{[b for b, ids in members.items() if agent in set(ids)]}"
            )


class TestCensusCheckStillJudges:
    """Capability 7: the check's verdict is computed over the widened total."""

    def test_it_warns_when_the_covered_share_is_a_minority(self, tmp_path,
                                                           monkeypatch) -> None:
        """Five agents whose only evidence is a seed failure are invisible today, so the
        check reports ok over a corpus where most sub-agents inherited nothing."""
        rows = [_start_row("g1"), _complete_row("g1"),
                _start_row("g2"), _complete_row("g2")]
        rows += [_seed_failed_row(f"f{i}") for i in range(5)]
        result = _check_over(monkeypatch, _metrics_stream(tmp_path, rows))
        assert result.status == "warn", (
            f"2 covered of 7 is a minority, and the check reported {result.status}: "
            f"{result.detail}"
        )

    def test_it_reports_ok_when_the_covered_share_is_the_majority(self, tmp_path,
                                                                 monkeypatch) -> None:
        """The other direction, and the one that reddens today for the opposite reason: the
        four lazily seeded agents are invisible, so the check sees three unreachable
        dispatches and cries wolf."""
        rows = [_complete_row(f"u{i}") for i in range(3)]
        rows += [_seeded_row(f"l{i}", LAZY_SEED) for i in range(4)]
        result = _check_over(monkeypatch, _metrics_stream(tmp_path, rows))
        assert result.status == "ok", (
            f"4 covered of 7 is a majority, and the check reported {result.status}: "
            f"{result.detail}"
        )

    def test_it_reports_ok_at_the_boundary_where_the_covered_are_exactly_half(
            self, tmp_path, monkeypatch) -> None:
        """Three covered of six, the point the two shipped verdict tests sit either side of.

        The threshold's stated contract is "warn only when the MAJORITY is ungoverned", and
        at `covered * 2 == total` the ungoverned are half and not a majority, so ok is what
        equality is owed. Nothing observed this point before, so a strict-to-non-strict flip
        moved behaviour at exactly one input and no test moved with it.
        """
        population = _boundary_population()
        stream = _metrics_stream(tmp_path, _boundary_rows(population))
        census = _census_of_population(monkeypatch, stream, population)
        covered = _covered_in(population)
        assert covered * 2 == census["total"], (
            f"the fixture never reached the boundary: {covered} covered of "
            f"{census['total']}"
        )

        result = _check_over(monkeypatch, stream)
        assert result.status == "ok", (
            f"{covered} covered of {census['total']} is exactly half, so the ungoverned are "
            f"not a majority, and the check reported {result.status}: {result.detail}"
        )

    def test_it_warns_one_agent_below_the_boundary(self, tmp_path, monkeypatch) -> None:
        """The same map extended by one uncovered agent: three covered of seven.

        One test proves half a comparison. This is the other half: the pair brackets the
        threshold from both sides, so a mutation that moves the verdict reddens one of them
        whichever direction it moves.
        """
        base = _boundary_population()
        population = dict(base)
        population["b-completion-only-one-below"] = ("unreachable", [_complete_row])
        stream = _metrics_stream(tmp_path, _boundary_rows(population))
        census = _census_of_population(monkeypatch, stream, population)
        covered = _covered_in(population)
        below_by = len(population) - len(base)
        assert covered * 2 == census["total"] - below_by, (
            f"the fixture does not sit {below_by} agent(s) below the boundary: {covered} "
            f"covered of {census['total']}"
        )

        result = _check_over(monkeypatch, stream)
        assert result.status == "warn", (
            f"{covered} covered of {census['total']} leaves the ungoverned a majority, and "
            f"the check reported {result.status}: {result.detail}"
        )


@pytest.fixture(scope="module")
def live_census():
    """The real census over the repo's own archives, read once.

    Module scoped because the read walks 68 archive files; the value is read-only, so no
    test can hand another a mutated one.
    """
    from writ.session import doctor
    _require_live_lifecycle_rows()
    with mock.patch.object(doctor, "stream_path", lambda *a, **k: str(LIVE_METRICS)):
        census = doctor._subagent_governance_census()
    assert census is not None, f"the census read nothing from {LIVE_METRICS}"
    return census


@pytest.fixture(scope="module")
def live_members():
    from writ.session import doctor
    _require_live_lifecycle_rows()
    reader = getattr(doctor, "_subagent_governance_members", None)
    if reader is None:
        return None
    with mock.patch.object(doctor, "stream_path", lambda *a, **k: str(LIVE_METRICS)):
        return reader()


class TestLiveArchiveCensus:
    """Capability 8 (operational): the census against the corpus it was wrong about.

    THRESHOLDS AND RELATIONSHIPS, NEVER FIXED COUNTS. The stream is live and grows while
    the suite runs (measured: 6,430 -> 6,440 between two runs an hour apart), so the
    floors here are DERIVED from the on-disk caches at assert time and the rest are
    relationships between buckets.
    """

    def test_the_six_buckets_partition_the_live_universe(self, live_census) -> None:
        """Today this sums to one less than its own total, and that gap is a real agent,
        not a rounding artifact.

        Renamed for plan 2412ba38-51e1-4b73-895b-7b240a3c21d3, which adds a sixth bucket
        (`uncached_at_stop`); the assertion below is derived from the census's own return
        value rather than typed, so it stays correct across the rename untouched."""
        assert live_census["total"] > 0, f"the live corpus is empty: {live_census}"
        assert _partition_gap(live_census) == 0, (
            f"the live buckets leak {_partition_gap(live_census)} agent(s): {live_census}"
        )

    def test_the_lazy_bucket_names_the_lazy_seed_caches(self, live_census,
                                                        live_members) -> None:
        """The log rows corroborated against the NON-LOG artifact they are supposed to
        describe, by name. A difference is reported as the ids that differ, never
        reconciled into a count: the two artifacts are written by different code on
        different paths, so which ids disagree is the whole finding.

        The comparison is scoped to caches whose agent the census has a lifecycle row for.
        A cache with no row at all (a local probe, a manual seed) is outside the universe
        by definition, so the scope is structural rather than an allowlist of names.
        """
        assert live_members is not None, (
            "skeleton: writ/session/doctor.py has no _subagent_governance_members yet"
        )
        lazy = set(live_members["lazy"])
        known = set().union(*(set(ids) for ids in live_members.values()))
        caches = _require_live_caches(LAZY_SEED)
        assert caches & known, (
            f"no {LAZY_SEED} cache names an agent the census holds a lifecycle row for, "
            f"so every assertion below is vacuous: {sorted(caches)}"
        )
        unseen = sorted((caches & known) - lazy)
        assert not unseen, (
            f"these agents have a {LAZY_SEED} cache on disk AND a lifecycle row in the "
            f"archives, and the census does not call them lazy: {unseen}"
        )
        contradicted = sorted(lazy & _live_cache_ids(SPAWN_SEED))
        assert not contradicted, (
            f"the census calls these agents lazy while their on-disk cache says they were "
            f"seeded by the spawn path: {contradicted}"
        )
        assert live_census["lazy"] >= len(caches & known), (
            f"lazy={live_census['lazy']} is below the floor the on-disk caches set "
            f"({len(caches & known)}): {sorted(caches & known)}"
        )

    def test_the_governed_bucket_names_the_spawn_seeded_caches(self, live_census,
                                                               live_members) -> None:
        """The same corroboration on the other bucket, which is what stops the widening
        from being a way to move agents INTO lazy: governed has its own non-log artifact
        and its own derived floor."""
        assert live_members is not None, (
            "skeleton: writ/session/doctor.py has no _subagent_governance_members yet"
        )
        governed = set(live_members["governed"])
        known = set().union(*(set(ids) for ids in live_members.values()))
        caches = _require_live_caches(SPAWN_SEED)
        assert caches & known, (
            f"no {SPAWN_SEED} cache names an agent the census holds a lifecycle row for, "
            f"so every assertion below is vacuous: {len(caches)} cache(s) on disk"
        )
        unseen = sorted((caches & known) - governed)
        assert not unseen, (
            f"these agents have a {SPAWN_SEED} cache on disk AND a lifecycle row in the "
            f"archives, and the census does not call them governed: {unseen}"
        )
        assert live_census["governed"] >= len(caches & known), (
            f"governed={live_census['governed']} is below the floor the on-disk caches "
            f"set ({len(caches & known)})"
        )


class TestLazySeedFailureIsRecorded:
    """Capabilities 9-13, 15: a lazy-path failure leaves a trace, and a decline does not.

    RUN THROUGH THE REAL HOOK, NOT A PATCHED SEEDER. The three swallows this cycle removes
    are `except Exception: pass` inside the inline python, `2>/dev/null` and `|| true`, and
    two of the three live in bash on the far side of an exec. Patching the seeder
    in-process would prove none of them gone.
    """

    def test_a_dead_seeder_exec_records_one_bounded_row(
        self, sinks, tmp_path, require_platform_arg_limit
    ) -> None:
        """Capability 9 and 13. An oversized `agent_type` rides the seed exec's env, and
        MAX_ARG_STRLEN applies to env strings too, so execve dies with E2BIG and the python
        never runs: the one arm where bash itself has to speak.

        The row must carry NOTHING derived from the envelope that killed the exec, because
        a row built from that value dies exactly where the seed died. That lesson already
        cost this repo one cycle.
        """
        cache, friction = sinks
        _write_parent_cache(cache)
        probe = _seed_probe_hook(tmp_path)
        # The unmutated envelope FIRST: a detector that is never shown staying quiet on the
        # ordinary input is not a detector. This also pins the other half of capability 12,
        # that a seed which worked is never reported as a failure.
        control_cache = tmp_path / "control-cache"
        control_cache.mkdir()
        control_log = tmp_path / "control.jsonl"
        _write_parent_cache(control_cache)
        _run_hook(probe, cache=control_cache, friction=control_log,
                  stdin=_subagent_stdin())
        assert _child_cache(control_cache) is not None, (
            "the ordinary envelope seeded nothing either, so this harness proves nothing "
            "about the oversized one"
        )
        assert _friction_rows(control_log, SEED_FAILED_EVENT) == [], (
            f"a seed that worked was reported as a failure: "
            f"{_friction_rows(control_log)}"
        )
        result = _run_hook(
            probe, cache=cache, friction=friction,
            stdin=_subagent_stdin(agent_type="x" * require_platform_arg_limit),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "", (
            f"a hook's stdout is a model channel on some events: {result.stdout!r}"
        )
        assert _child_cache(cache) is None, (
            "the seed SUCCEEDED here, so this run never reproduced the dead-exec "
            "condition and the row asserted below would be measuring something else"
        )
        rows = _friction_rows(friction, SEED_FAILED_EVENT)
        assert len(rows) == 1, (
            f"expected exactly one {SEED_FAILED_EVENT} row: {_friction_rows(friction)}"
        )
        row = rows[0]
        assert row.get("session") == AGENT, (
            f"the agent id rides as the friction session argv, where friction-append.py "
            f"handles quoting: {row}"
        )
        assert row.get("hook") == LAZY_SEED_HOOK, (
            f"the row names the PATH that failed, not the calling script: {row}"
        )
        assert row.get("cache_source") == LAZY_SEED, row
        assert set(row) <= {"ts", "session", "mode", "event", "hook", "cache_source"}, (
            f"the row carries a field beyond the bounded set: {row}"
        )
        blob = json.dumps(row)
        assert "x" * 64 not in blob, (
            f"the row carries the value that killed the exec: {blob[:200]}"
        )
        assert PARENT not in blob, (
            f"no parent session and no agent type: a row built from the envelope dies "
            f"where the seed died: {blob[:200]}"
        )
        assert len(blob) <= BOUNDED_ROW_MAX_CHARS, f"unbounded row: {len(blob)} chars"

    def test_a_cache_write_fault_records_one_row_naming_the_lazy_path(
        self, unwritable_cache, tmp_path
    ) -> None:
        """Capability 10 and 13. The python IS alive here, so it records the fault at the
        site where the fault and a decline are still distinguishable, and bash stays
        silent: exactly one row, and no second process."""
        friction = tmp_path / "friction.jsonl"
        probe = _seed_probe_hook(tmp_path)
        control_cache = tmp_path / "control-cache"
        control_cache.mkdir()
        control_log = tmp_path / "control.jsonl"
        _write_parent_cache(control_cache)
        _run_hook(probe, cache=control_cache, friction=control_log,
                  stdin=_subagent_stdin())
        assert _child_cache(control_cache) is not None, (
            "the same probe against a writable cache dir seeded nothing, so the fault arm "
            "below is not isolating the write"
        )
        assert _friction_rows(control_log, SEED_FAILED_EVENT) == [], (
            f"a seed that worked was reported as a failure: {_friction_rows(control_log)}"
        )
        result = _run_hook(probe, cache=unwritable_cache, friction=friction,
                           stdin=_subagent_stdin())
        assert result.returncode == 0, result.stderr
        assert result.stdout == "", (
            f"nothing reaches the model channel on a failure arm: {result.stdout!r}"
        )
        assert _child_cache(unwritable_cache) is None, (
            "the cache was written, so no fault happened and this run measures nothing"
        )
        rows = _friction_rows(friction, SEED_FAILED_EVENT)
        assert len(rows) == 1, (
            f"expected exactly one {SEED_FAILED_EVENT} row: {_friction_rows(friction)}"
        )
        row = rows[0]
        assert AGENT in {row.get("session"), row.get("agent_id")}, row
        assert row.get("cache_source") == LAZY_SEED, (
            f"the row names the path that failed, which is what separates this from the "
            f"spawn-path fault: {row}"
        )
        reason = str(row.get("reason") or "")
        assert reason, f"capability 10 requires a bounded reason on the row: {row}"
        assert "\n" not in reason and "Traceback" not in reason, (
            f"an unbounded traceback must not land in the operator's log: {reason!r}"
        )
        assert len(reason) <= BOUNDED_REASON_MAX_CHARS, f"unbounded reason: {reason!r}"
        assert len(json.dumps(row)) <= BOUNDED_ROW_MAX_CHARS, f"unbounded row: {row}"

    def test_the_same_fault_on_the_spawn_path_records_one_row_only(
        self, unwritable_cache, tmp_path
    ) -> None:
        """Capability 11. The same blindness exists on the SPAWN path today: a mutate_cache
        fault returns False, the hook prints `skipped`, its SEED_STATUS is non-empty, and
        nothing is recorded (verified against the current source). The fault site closes
        both at once, and the start hook must not then add a second row for one failure.
        """
        friction = tmp_path / "friction.jsonl"
        result = _run_hook(
            START_HOOK, cache=unwritable_cache, friction=friction,
            stdin=json.dumps({"agent_id": AGENT, "agent_type": "writ-implementer",
                              "session_id": PARENT, "hook_event_name": "SubagentStart"}),
            extra_env={"WRIT_PROJECTS_DIR": str(tmp_path / "no-projects")},
        )
        assert result.returncode == 0, result.stderr
        assert _child_cache(unwritable_cache) is None, (
            "the cache was written, so no fault happened and this run measures nothing"
        )
        rows = _friction_rows(friction, SEED_FAILED_EVENT)
        assert len(rows) == 1, (
            f"the seeder spoke for itself, so bash must stay silent: one failure, one "
            f"row: {rows}"
        )
        assert rows[0].get("cache_source") == SPAWN_SEED, (
            f"the row names the path that failed: {rows[0]}"
        )

    @pytest.mark.parametrize("arm", ["no-parent-mode", "unusable-ids",
                                     "cache-already-present"])
    def test_a_decline_records_no_row_while_a_real_fault_does(self, arm, tmp_path,
                                                              unwritable_cache) -> None:
        """Capability 12, with its own positive control.

        A DECLINE IS NOT A FAILURE: no parent mode means there is nothing to inherit, and
        688 of 714 sub-agent sessions with hook activity never resolved one. Reporting
        those as failures would bury the real gap under a population thirty times its size.
        The fault arm runs in the same test because "no row was written" is exactly what a
        seeder that never runs at all also produces.
        """
        probe = _seed_probe_hook(tmp_path)
        declined = tmp_path / "declined"
        declined.mkdir()
        stdin = _subagent_stdin()
        if arm == "no-parent-mode":
            _write_parent_cache(declined, state={"mode": None, "current_phase": None})
        elif arm == "unusable-ids":
            _write_parent_cache(declined)
            stdin = _subagent_stdin(agent="../escape")
        else:
            _write_parent_cache(declined)
            (declined / f"writ-session-{AGENT}.json").write_text(json.dumps(
                {"mode": "work", "is_subagent": True, "cache_source": LAZY_SEED}))
        decline_log = tmp_path / "declined.jsonl"
        result = _run_hook(probe, cache=declined, friction=decline_log, stdin=stdin)
        assert result.returncode == 0, result.stderr
        assert _friction_rows(decline_log, SEED_FAILED_EVENT) == [], (
            f"a declined seed ({arm}) was recorded as a failure, which turns a gap report "
            f"into a false alarm: {_friction_rows(decline_log)}"
        )
        fault_log = tmp_path / "fault.jsonl"
        _run_hook(probe, cache=unwritable_cache, friction=fault_log,
                  stdin=_subagent_stdin())
        assert len(_friction_rows(fault_log, SEED_FAILED_EVENT)) == 1, (
            "the control arm recorded nothing, so the assertion above holds whether or not "
            "a decline is distinguished from a failure"
        )

    def test_the_recorded_failure_reaches_the_real_census(self, unwritable_cache,
                                                          tmp_path, monkeypatch) -> None:
        """Capability 15. The CHAIN, not the producer: a row that is written and then
        dropped one module downstream is the shape that kept a property broken for three
        cycles here. The rows a real failing hook writes are read back by the real census.

        Two runs, because a persistently failing agent writes one row per hook that
        retries and the census counts AGENTS through sets.
        """
        stream_dir = tmp_path / "stream"
        stream_dir.mkdir()
        stream = stream_dir / "metrics.jsonl"
        probe = _seed_probe_hook(tmp_path)
        for _ in range(2):
            result = _run_hook(probe, cache=unwritable_cache, friction=stream,
                               stdin=_subagent_stdin())
            assert result.returncode == 0, result.stderr
        rows = _friction_rows(stream, SEED_FAILED_EVENT)
        assert len(rows) == 2, f"each retry inside the agent records its own row: {rows}"
        census = _census_over(monkeypatch, stream)
        assert census["seed_failed"] == 1, (
            f"two rows, one agent: the census counts agents, not rows: {census}"
        )
        assert census["total"] == 1, census
        assert _partition_gap(census) == 0, census


class TestLazySeedProcessBudget:
    """Capability 14: the cost of the reporting, measured rather than reasoned about.

    SUCCESSFUL execve ONLY, and PYTHON starts specifically. An execve ATTEMPT overcounts a
    PATH-resolved binary, and the whole-process count moves with the read-only arm's mkdir
    retries; the python starts are what the budget is stated in and they were stable across
    repeated runs of every arm.

    DELTAS BETWEEN ARMS OF THE SAME PROBE IN THE SAME ENVIRONMENT, never absolute numbers:
    with no jq on the box the envelope parse itself costs a python, and that would move
    every arm equally while the budget is unchanged.
    """

    def _python_starts(self, probe: Path, *, cache: Path, friction: Path, stdin: str,
                       home: Path) -> tuple:
        env = {
            **os.environ,
            "WRIT_CACHE_DIR": str(cache),
            "WRIT_FRICTION_LOG": str(friction),
            "WRIT_PORT": "59999",
            "WRIT_NO_AUTOSTART": "1",
            "WRIT_DIR": str(REPO),
            "SKILL_DIR": str(REPO),
            # The blackbox capture switch enables itself from a sentinel under $HOME and
            # then spawns one python per hook, which would put a developer's ambient debug
            # setting inside the budget.
            "HOME": str(home),
        }
        text = trace_execve(["bash", str(probe)], input=stdin, env=env, timeout=180)
        starts = sum(1 for line in text.splitlines()
                     if 'python3"' in line and _EXECVE_OK.search(line))
        return starts, text

    def _arm(self, tmp_path: Path, name: str, *, stdin: str, child_cache: bool = False,
             unwritable: bool = False) -> tuple:
        root = tmp_path / name
        cache = root / "cache"
        cache.mkdir(parents=True)
        home = root / "home"
        home.mkdir()
        _write_parent_cache(cache)
        if child_cache:
            (cache / f"writ-session-{AGENT}.json").write_text(json.dumps(
                {"mode": "work", "is_subagent": True, "cache_source": LAZY_SEED}))
        if unwritable:
            os.chmod(cache, 0o555)
        try:
            return self._python_starts(_seed_probe_hook(tmp_path), cache=cache,
                                       friction=root / "friction.jsonl", stdin=stdin,
                                       home=home)
        finally:
            if unwritable:
                os.chmod(cache, 0o755)

    def test_the_python_budget_holds_on_every_arm(self, tmp_path,
                                                  require_platform_arg_limit) -> None:
        if os.geteuid() == 0:
            pytest.skip("running as root: the cache-write fault arm cannot be reproduced")
        main, _ = self._arm(tmp_path, "main", stdin=json.dumps(
            {"session_id": PARENT, "tool_name": "Read", "tool_input": {},
             "hook_event_name": "PreToolUse"}))
        existing, _ = self._arm(tmp_path, "existing", stdin=_subagent_stdin(),
                                child_cache=True)
        success, _ = self._arm(tmp_path, "success", stdin=_subagent_stdin())
        recorded, _ = self._arm(tmp_path, "recorded", stdin=_subagent_stdin(),
                                unwritable=True)
        dead, dead_trace = self._arm(tmp_path, "dead", stdin=_subagent_stdin(
            agent_type="x" * require_platform_arg_limit))
        assert main == existing, (
            f"a main session and a sub-agent whose cache already exists both cost nothing "
            f"for seeding: {main} against {existing}"
        )
        assert success == existing + 1, (
            f"the seed is one python on the FIRST hook inside a sub-agent and nothing on "
            f"any later one: {success} against {existing}"
        )
        assert recorded == success, (
            f"a fault the python can see is recorded by the process already running, so a "
            f"recorded failure adds nothing: {recorded} against {success}"
        )
        assert dead == success, (
            f"when the exec dies, bash writes the row through friction-append.py, which is "
            f"one python on the failure path only: {dead} against {success}"
        )
        assert _EXECVE_E2BIG.search(dead_trace), (
            "no execve failed with E2BIG in the dead-python arm, so that run never "
            "reproduced the condition and its count means nothing"
        )


# --------------------------------------------------------------------------- #
# Plan 2412ba38-51e1-4b73-895b-7b240a3c21d3, capabilities 4-11: defect A's
# reader side. A `subagent_complete` row now carries `cache_state`, and the
# census gains a sixth bucket, `uncached_at_stop`, splitting the existing
# `active` arm rather than sitting above or below it (see plan.md's placement
# argument: above `active` would drain `unreachable`, above `seeded`/`started`
# would relabel governed agents whose cache was later swept).
#
# A NEW population, not an extension of `_population()` above. That table
# backs `TestCensusPartition.test_the_buckets_partition_the_universe`, which
# this plan does not touch and which must keep passing against today's five
# buckets exactly as it does now; merging the two tables would make that
# already-passing test fail the moment this cycle's evidence shapes joined
# it, for a bucket its own reader does not classify into yet. So this
# cycle's evidence shapes get their own table, deliberately never merged into
# `_population()`.
# --------------------------------------------------------------------------- #

UNCACHED_BUCKET = "uncached_at_stop"


def _complete_row_with_cache_state(agent: str, cache_state: str | None = None) -> dict:
    """A `subagent_complete` row for `agent`, carrying `cache_state` when given and
    carrying no such field at all when `cache_state` is None -- the shape the
    "a row written before the field existed" control (capability 5) needs.
    """
    row = {"event": "subagent_complete", "agent_id": agent}
    if cache_state is not None:
        row["cache_state"] = cache_state
    return row


def _absent_row(agent: str) -> dict:
    return _complete_row_with_cache_state(agent, "absent")


def _present_row(agent: str) -> dict:
    return _complete_row_with_cache_state(agent, "present")


def _cache_state_population() -> list:
    """One agent per cache_state evidence shape capabilities 4-8 rule on, in the same
    (agent, owed bucket, row makers) shape `_population()` uses above, so the same
    style of partition assertion applies to it. Deliberately not merged into
    `_population()` -- see the module comment above this block.

    EVERY AGENT HERE CARRIES A `_hook_row` EXCEPT THE LAST ONE, on purpose. `_hook_row`
    is what puts an agent in `active`, which is the arm the new rung splits; without it
    the ladder never reaches that arm and an assertion about placement would pass
    without testing placement.
    """
    table = [
        ("c-absent-and-hook-rows", UNCACHED_BUCKET, [_absent_row, _hook_row]),
        ("c-unrecorded-and-hook-rows", "reachable",
         [_complete_row_with_cache_state, _hook_row]),
        ("c-present-and-hook-rows", "reachable", [_present_row, _hook_row]),
        ("c-started-and-absent", "governed", [_start_row, _absent_row, _hook_row]),
        ("c-lazy-seeded-and-absent", "lazy",
         [lambda a: _seeded_row(a, LAZY_SEED), _absent_row, _hook_row]),
        ("c-absent-no-own-rows", "unreachable", [_absent_row]),
    ]
    return [(agent, bucket, [make(agent) for make in makers])
            for agent, bucket, makers in table]


class TestUncachedAtStopBucket:
    """Capabilities 4-8: the sixth bucket, and the two conditional pairs the plan
    names -- a completion row carrying `cache_state: "absent"` against the identical
    row with no field at all, and that same absent row with and without an
    accompanying own-id hook row."""

    def _one_agent(self, tmp_path, monkeypatch, agent: str, rows: list) -> dict:
        census = _census_over(monkeypatch, _metrics_stream(tmp_path, rows))
        assert census["total"] == 1, (
            f"{agent} is the only agent these rows name, so anything else means the "
            f"universe was built from something other than the evidence: {census}"
        )
        assert _partition_gap(census) == 0, (
            f"{agent} is in the denominator and in no bucket, or in two: {census}"
        )
        return census

    def test_an_agent_with_its_own_rows_and_an_absent_cache_state_is_uncached_at_stop(
        self, tmp_path, monkeypatch
    ) -> None:
        """Capability 4. An agent with a non-lifecycle row under its own id (proving a
        Writ hook ran inside it, the same evidence `_hook_row` supplies above) and a
        completion row whose `cache_state` is the literal string "absent" must land in
        the new `uncached_at_stop` bucket, not in `reachable`."""
        agent = "c-absent"
        census = self._one_agent(tmp_path, monkeypatch, agent,
                                 [_hook_row(agent), _absent_row(agent)])
        assert census[UNCACHED_BUCKET] == 1, (
            f"the agent's own completion row says no cache existed when it stopped, and "
            f"the census does not count it as such: {census}"
        )
        assert census["reachable"] == 0, (
            f"'ran ungoverned while doing work' and 'Writ never saw a cache' are still "
            f"summed into one number: {census}"
        )

    def test_the_identical_row_with_no_cache_state_field_stays_reachable(
        self, tmp_path, monkeypatch
    ) -> None:
        """Capability 5, and the positive control for the test above: the same agent
        shape, the only difference being that the completion row carries no
        `cache_state` field at all (the historical shape, exactly
        `("r-completion-and-hook-rows", "reachable", ...)` in `_population()` above),
        must stay in `reachable` -- unrecorded, never read as `absent`."""
        agent = "c-unrecorded"
        historical = _complete_row_with_cache_state(agent)
        assert "cache_state" not in historical, (
            "the control row carries the field, so it is not the historical shape and "
            "this test proves nothing about the 6,736 archived rows"
        )
        census = self._one_agent(tmp_path, monkeypatch, agent,
                                 [_hook_row(agent), historical])
        assert census["reachable"] == 1, (
            f"a row written before the field existed was relabelled: {census}"
        )
        assert census[UNCACHED_BUCKET] == 0, (
            f"a MISSING field was read as an observed absence, which is exactly what a "
            f"boolean would have done: {census}"
        )

    def test_an_agent_with_cache_state_present_stays_reachable(
        self, tmp_path, monkeypatch
    ) -> None:
        """Capability 6."""
        agent = "c-present"
        census = self._one_agent(tmp_path, monkeypatch, agent,
                                 [_hook_row(agent), _present_row(agent)])
        assert census["reachable"] == 1, census
        assert census[UNCACHED_BUCKET] == 0, (
            f"a cache existed when this agent stopped and it is counted as uncached: "
            f"{census}"
        )

    def test_a_governed_agent_keeps_its_bucket_despite_an_absent_completion_cache_state(
        self, tmp_path, monkeypatch
    ) -> None:
        """Capability 7, the governed arm: a `subagent_start` row must keep an agent in
        `governed` even when its completion row says `cache_state: "absent"`."""
        agent = "c-governed-absent"
        census = self._one_agent(
            tmp_path, monkeypatch, agent,
            [_start_row(agent), _hook_row(agent), _absent_row(agent)])
        assert census["governed"] == 1, (
            f"a positive seed record must outrank a later absence: the cache was made "
            f"and then swept, which is not the same as never having one: {census}"
        )
        assert census[UNCACHED_BUCKET] == 0, (
            f"the new rung sits above `started`, so it relabels governed agents: {census}"
        )

    def test_a_lazily_seeded_agent_keeps_its_bucket_despite_an_absent_completion_cache_state(
        self, tmp_path, monkeypatch
    ) -> None:
        """Capability 7, the lazy arm: the same, for a `subagent_seeded`
        (cache_source=lazy_seed) row."""
        agent = "c-lazy-absent"
        census = self._one_agent(
            tmp_path, monkeypatch, agent,
            [_seeded_row(agent, LAZY_SEED), _hook_row(agent), _absent_row(agent)])
        assert census["lazy"] == 1, census
        assert census[UNCACHED_BUCKET] == 0, (
            f"the new rung sits above `seeded`, so it relabels lazily seeded agents: "
            f"{census}"
        )

    def test_an_absent_cache_state_with_no_row_under_its_own_id_stays_unreachable(
        self, tmp_path, monkeypatch
    ) -> None:
        """Capability 8: a completion row saying `cache_state: "absent"` is not, on its
        own, evidence that a Writ hook ran inside the agent; with no other row under its
        own id the agent stays `unreachable`, exactly as `("u-completion-only",
        "unreachable", [_complete_row])` does in `_population()` above."""
        agent = "c-absent-alone"
        census = self._one_agent(tmp_path, monkeypatch, agent, [_absent_row(agent)])
        assert census["unreachable"] == 1, (
            f"nothing ran inside this agent, and that is a different fact from hooks "
            f"having run and never made a cache: {census}"
        )
        assert census[UNCACHED_BUCKET] == 0, (
            f"the new rung sits above `active`, so it drains `unreachable`: {census}"
        )

    @pytest.mark.parametrize("placement", ["live", "archive"])
    def test_the_new_population_partitions_including_a_gzipped_archive(
        self, tmp_path, monkeypatch, placement
    ) -> None:
        """Capability 9, scoped to this cycle's own population (see the module comment
        above `_cache_state_population`). Mirrors `TestCensusPartition.test_the_
        buckets_partition_the_universe`'s own `live`/`archive` parametrization, so the
        sixth bucket is proven readable from a gzipped archive as well as the live
        file, not only from whichever one a lazier test happened to seed."""
        population = _cache_state_population()
        rows = _population_rows(population)
        stream = (_metrics_stream(tmp_path, rows) if placement == "live"
                  else _metrics_stream(tmp_path, [_hook_row("main-1")], archived_rows=rows))
        census = _census_over(monkeypatch, stream)
        expected = _expected_counts(population)
        assert expected[UNCACHED_BUCKET] > 0, (
            "the population owes the new bucket nothing, so this run would partition "
            "without ever exercising it"
        )
        assert census["total"] == len(population), (
            f"every agent named by a lifecycle row is a member: {census}"
        )
        assert _partition_gap(census) == 0, (
            f"the sixth bucket did not join the sum: {census['total']} members against "
            f"{sum(census[b] for b in _bucket_names(census))} classified; "
            f"expected {dict(expected)}"
        )
        for bucket in _bucket_names(census):
            assert census[bucket] == expected[bucket], (
                f"{bucket}: {census[bucket]} against {expected[bucket]} from "
                f"{[a for a, b, _ in population if b == bucket]}"
            )


class TestDoctorReportsTheSixthBucket:
    """Capability 10: the doctor's census line names the new count between
    `reachable` and `unreachable`, and states what a missing field means."""

    def _stream(self, tmp_path) -> Path:
        """Counts chosen to DIFFER between the two buckets, so a detail that printed one
        number twice could not pass both assertions below."""
        rows = [_hook_row("d-uncached"), _absent_row("d-uncached")]
        for i in range(3):
            rows += [_hook_row(f"d-reachable-{i}"), _complete_row(f"d-reachable-{i}")]
        return _metrics_stream(tmp_path, rows)

    def test_the_detail_names_uncached_at_stop_separately_from_reachable(
        self, tmp_path, monkeypatch
    ) -> None:
        """Reads `check_subagent_governance_census(...).detail`, the same artifact
        `TestDoctorGovernanceCensus` above already asserts against; not a
        documentation-prose assertion, since `detail` is the check's own returned
        value, not a doc file."""
        stream = self._stream(tmp_path)
        census = _census_over(monkeypatch, stream)
        assert census[UNCACHED_BUCKET] != census["reachable"], (
            f"the two counts are equal, so one number could satisfy both assertions "
            f"below: {census}"
        )
        detail = _check_over(monkeypatch, stream).detail
        assert re.search(rf"\b{census[UNCACHED_BUCKET]}\b[^,]*cache", detail), (
            f"the detail does not report {census[UNCACHED_BUCKET]} agent(s) as having "
            f"stopped with no cache: {detail}"
        )
        assert re.search(rf"\b{census['reachable']}\b[^,]*reachable", detail), (
            f"the detail does not report {census['reachable']} agent(s) as ungoverned "
            f"but reachable: {detail}"
        )

    def test_the_detail_states_that_rows_without_the_field_are_unrecorded(
        self, tmp_path, monkeypatch
    ) -> None:
        """The same `detail` string must say that an agent without `cache_state` at all
        is unrecorded rather than counted as either `present` or `absent` -- the
        6,736 historical rows this cycle does not move, per plan.md."""
        detail = _check_over(monkeypatch, self._stream(tmp_path)).detail
        assert "cache_state" in detail, (
            f"the detail never names the field the split is made on, so a reader cannot "
            f"tell which rows carry the observation: {detail}"
        )
        assert "unrecorded" in detail, (
            f"the detail does not say that a row written before the field existed is "
            f"unrecorded rather than counted as either: {detail}"
        )


class TestWarnThresholdUnchangedBySixthBucket:
    """Capability 11: the coverage boundary (`governed + lazy` against `total`) is
    unmoved by the new bucket, bracketed from both sides exactly as
    `TestCensusCheckStillJudges` already brackets it for the five-bucket census."""

    def _at_boundary(self) -> dict:
        """`_boundary_population()` extended by one agent in the new bucket and one
        covered agent to balance it, so the map still sits exactly on
        `covered * 2 == total` while containing the bucket this cycle adds."""
        population = dict(_boundary_population())
        population["b-uncached-at-stop"] = (
            UNCACHED_BUCKET, [_absent_row, _hook_row])
        population["b-governed-balancing"] = ("governed", [_start_row, _complete_row])
        return population

    def test_a_population_one_agent_below_the_boundary_still_warns(
        self, tmp_path, monkeypatch
    ) -> None:
        base = self._at_boundary()
        population = dict(base)
        population["b-completion-only-one-below"] = ("unreachable", [_complete_row])
        stream = _metrics_stream(tmp_path, _boundary_rows(population))
        census = _census_of_population(monkeypatch, stream, population)
        covered = _covered_in(population)
        below_by = len(population) - len(base)
        assert census[UNCACHED_BUCKET] > 0, (
            f"the fixture holds no agent in the new bucket, so it says nothing about "
            f"the threshold under the split: {census}"
        )
        assert covered * 2 == census["total"] - below_by, (
            f"the fixture does not sit {below_by} agent(s) below the boundary: {covered} "
            f"covered of {census['total']}"
        )
        result = _check_over(monkeypatch, stream)
        assert result.status == "warn", (
            f"{covered} covered of {census['total']} leaves the ungoverned a majority, "
            f"and splitting `reachable` must not suppress that alarm; the check reported "
            f"{result.status}: {result.detail}"
        )

    def test_a_population_exactly_at_the_boundary_does_not_warn(
        self, tmp_path, monkeypatch
    ) -> None:
        population = self._at_boundary()
        stream = _metrics_stream(tmp_path, _boundary_rows(population))
        census = _census_of_population(monkeypatch, stream, population)
        covered = _covered_in(population)
        assert census[UNCACHED_BUCKET] > 0, (
            f"the fixture holds no agent in the new bucket: {census}"
        )
        assert covered * 2 == census["total"], (
            f"the fixture never reached the boundary: {covered} covered of "
            f"{census['total']}"
        )
        result = _check_over(monkeypatch, stream)
        assert result.status == "ok", (
            f"{covered} covered of {census['total']} is exactly half, so the ungoverned "
            f"are not a majority, and the check reported {result.status}: {result.detail}"
        )
