"""Cycle L skeletons: know which role a sub-agent is before anything enforces one.

RED until `writ/session/subagent_role.py` exists, the two subagent hooks use it, and
`common.sh` stops instrumenting the one script under `hooks/scripts/` that is not a hook.

WHAT THE CENSUS FOUND, because the design follows from it (measured 2026-08-27 on this
machine). The harness sends `agent_type: ""` in 10 of 10 captured `SubagentStop` envelopes
while `agent_id` is populated in 10 of 10. The hooks' fallback rewrites that empty string to
the literal `general-purpose`, so those governance records carry a role nobody observed and
are indistinguishable from a real general-purpose dispatch. Keying a write scope on
`agent_type` would therefore have enforced ONE role for every such worker while reading as
though it enforced five.

SCOPE OF THAT CLAIM, CORRECTED AFTER PROBING. This file first said "the live harness", full
stop, which generalized from logs to the build. Three probe dispatches later the same day
all carried `agent_type` POPULATED, on both SubagentStart and SubagentStop, resolving via
SOURCE_ENVELOPE. The empty-envelope population is exactly the one that never receives a
SubagentStart: 1,381 agents have a start row, 2,219 do not, and the sets do not intersect.
So an empty `agent_type` is the signature of an ungoverned spawn, not of this build. That
distinction is what the source field exists to keep visible, and getting it wrong is why
verifying by triggering beats grepping a log.

TWO CORRECTIONS TO THE APPROVED PLAN, recorded here rather than by editing plan.md,
because an edit would change `plan_md_hash` and invalidate the approval that authorized
this file (`gates.py` binds each approval to the plan it was given).

1. THE SIDECAR IS EPHEMERAL. The naming convention holds 10 of 10 against the envelope's
   own `agent_transcript_path`: the role for `agent_id` lives in
   `<parent-session-dir>/subagents/agent-<agent_id>.meta.json`, beside
   `agent-<agent_id>.jsonl`, one directory deeper for workflow fan-out. But Claude Code
   DELETES both after the agent finishes: 0 of 57 logged `agent_id`s have a sidecar on
   disk now, while 68 sidecars from the same session survive for agents whose stop rows
   are not in the log. So a retrospective resolution rate measures deletion, not
   coverage, and the resolved role must be PERSISTED at first sight instead of
   re-derived later. That is why `resolve_role` accepts a cache and why `SOURCE_CACHE`
   exists.

2. THE UNKNOWN BUFFER IS NOT MERELY STRANDED, IT IS HELD OPEN. `writ-flush-events.py`
   already sweeps any buffer whose newest file is older than `ABANDONED_SESSION_SECONDS`,
   and `unknown` is an eligible session name, so the 22 `writ-subagent-stop` rows in
   `writ-events-unknown.buf` would have drained on their own. They never do, because
   `writ-statusline.sh` shares that bucket and writes to it constantly (673 rows in one
   day), keeping the mtime perpetually young. One non-hook keeps the whole bucket from
   ever being collected, which is why the statusline fix is what unstrands the stop hook's
   rows rather than a new drain.

WHY THE EXCLUSION IS ONE HARD-CODED NAME AND NOT A PREDICATE. Three alternatives were
weighed. An env-var opt-out (`WRIT_NOT_A_HOOK=1`) is one line but hands the agent a switch
that silences all hook telemetry, and a control the agent can disable is not a control. A
manifest-membership read costs a subshell per hook invocation, on a path where cycle I
removed 15 execve per write. Moving the script under `bin/` is structurally cleanest but
breaks every installed `statusLine` setting, since `writ_install.py` pins the path.
So: one name in `common.sh`, and `test_exclusion_list_covers_every_unregistered_script`
below makes the enumeration self-maintaining, which is cycle K's pattern (keep the literal
where reviewing it is the job, derive everything else).

Per ENF-GATE-007: skeletons written and approved before implementation.
Per TEST-ISOLATE-003: every test gets its own `WRIT_CACHE_DIR`, its own friction sink and
its own fake projects tree; no test reads the real `~/.claude/projects` or writes into the
repo's `var/`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests._inventory import doctor_check_names, hook_script_names

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"
HOOKS_JSON = REPO / "hooks" / "hooks.json"
COMMON_SH = REPO / "bin" / "lib" / "common.sh"

STOP_HOOK = HOOKS / "writ-subagent-stop.sh"
START_HOOK = HOOKS / "writ-subagent-start.sh"
STATUSLINE = HOOKS / "writ-statusline.sh"

# The two doctor checks this cycle adds.
STRANDED_CHECK = "stranded-telemetry-buffer"
ROLE_COVERAGE_CHECK = "subagent-role-coverage"


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


def _role_module():
    """Import the resolver, failing (never skipping) while it does not exist.

    A skip here would have let this whole file pass green before implementation, which is
    the mistake cycle K's skeletons made with importorskip.
    """
    try:
        from writ.session import subagent_role
    except ImportError as exc:  # pragma: no cover - RED path
        pytest.fail(f"skeleton: writ/session/subagent_role.py does not exist yet ({exc})")
    return subagent_role


def _registered_script_names() -> set[str]:
    """Basenames (no .sh) of every script registered in hooks/hooks.json."""
    doc = json.loads(HOOKS_JSON.read_text())
    names: set[str] = set()
    for entries in doc.get("hooks", {}).values():
        for entry in entries:
            for hook in entry.get("hooks", []) or []:
                for token in str(hook.get("command", "")).split():
                    if token.endswith(".sh"):
                        names.add(token.rsplit("/", 1)[-1][:-3])
    return names


def _sidecar(projects: Path, parent_session: str, agent_id: str, payload: dict,
             *, workflow: str = "") -> Path:
    """Write a sidecar exactly where Claude Code writes one."""
    directory = projects / "-some-project" / parent_session / "subagents"
    if workflow:
        directory = directory / "workflows" / workflow
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"agent-{agent_id}.meta.json"
    path.write_text(json.dumps(payload))
    return path


def _buffer_rows(cache_dir: Path, kind: str = "hook_execution") -> list[list[str]]:
    rows: list[list[str]] = []
    for buf in cache_dir.glob("writ-events-*.buf"):
        for record in buf.read_text(errors="replace").split("\x1e"):
            if not record.strip():
                continue
            fields = record.split("\x1f")
            if fields[0] == kind:
                rows.append(fields + [buf.name])
    return rows


def _run(script: Path, *, cache: Path, friction: Path, stdin: str = "{}",
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
def sinks(tmp_path):
    cache = tmp_path / "cache"
    friction = tmp_path / "friction.jsonl"
    cache.mkdir(parents=True, exist_ok=True)
    return cache, friction


@pytest.fixture()
def projects(tmp_path):
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------- #
# Capabilities 1-4: the resolver, and its refusal to invent an answer
# --------------------------------------------------------------------------- #

class TestResolver:

    def test_envelope_wins_when_it_carries_a_value(self, projects) -> None:
        mod = _role_module()
        _require(mod, "resolve_role", "SOURCE_ENVELOPE")
        role, source = mod.resolve_role(
            "a1", envelope_agent_type="writ-explorer", projects_dir=projects)
        assert (role, source) == ("writ-explorer", mod.SOURCE_ENVELOPE)

    def test_sidecar_answers_when_the_envelope_is_empty(self, projects) -> None:
        mod = _role_module()
        _require(mod, "resolve_role", "SOURCE_SIDECAR")
        _sidecar(projects, "parent-1", "a2", {"agentType": "writ-planner", "spawnDepth": 1})
        role, source = mod.resolve_role("a2", envelope_agent_type="", projects_dir=projects)
        assert (role, source) == ("writ-planner", mod.SOURCE_SIDECAR)

    def test_workflow_nested_sidecar_is_found(self, projects) -> None:
        """61 of this session's 68 sidecars sit one directory deeper, under
        subagents/workflows/wf_<id>/. A flat search misses every workflow worker."""
        mod = _role_module()
        _sidecar(projects, "parent-1", "a3", {"agentType": "workflow-subagent"},
                 workflow="wf_8d89b264-968")
        role, source = mod.resolve_role("a3", projects_dir=projects)
        assert (role, source) == ("workflow-subagent", mod.SOURCE_SIDECAR)

    def test_cache_answers_when_the_sidecar_is_already_deleted(self, projects) -> None:
        """The ephemerality fix: a role seen once must survive the sidecar's deletion."""
        mod = _role_module()
        _require(mod, "SOURCE_CACHE")
        role, source = mod.resolve_role(
            "a4", projects_dir=projects, cache={"agent_type": "writ-reviewer"})
        assert (role, source) == ("writ-reviewer", mod.SOURCE_CACHE)

    def test_envelope_outranks_cache_and_cache_outranks_nothing(self, projects) -> None:
        mod = _role_module()
        role, source = mod.resolve_role(
            "a5", envelope_agent_type="writ-implementer",
            projects_dir=projects, cache={"agent_type": "writ-reviewer"})
        assert (role, source) == ("writ-implementer", mod.SOURCE_ENVELOPE)

    def test_unresolved_is_explicit_and_never_a_plausible_role(self, projects) -> None:
        mod = _role_module()
        _require(mod, "UNKNOWN_ROLE", "SOURCE_UNRESOLVED")
        role, source = mod.resolve_role("nobody", projects_dir=projects)
        assert (role, source) == (mod.UNKNOWN_ROLE, mod.SOURCE_UNRESOLVED)
        assert role != "general-purpose", (
            "an unresolved role must not be reported as a real dispatch type: that is the "
            "defect this cycle exists to remove"
        )

    def test_a_real_general_purpose_dispatch_stays_distinguishable(self, projects) -> None:
        """The whole point of returning a source alongside the role."""
        mod = _role_module()
        real = mod.resolve_role("a6", envelope_agent_type="general-purpose",
                                projects_dir=projects)
        unresolved = mod.resolve_role("a7", projects_dir=projects)
        assert real[1] == mod.SOURCE_ENVELOPE
        assert unresolved[1] == mod.SOURCE_UNRESOLVED
        assert real != unresolved

    @pytest.mark.parametrize("body", ["{not json", "[]", "{}", '{"agentType": ""}',
                                      '{"agentType": null}'])
    def test_malformed_or_empty_sidecar_resolves_unknown(self, projects, body) -> None:
        mod = _role_module()
        directory = projects / "-some-project" / "parent-1" / "subagents"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "agent-a8.meta.json").write_text(body)
        role, source = mod.resolve_role("a8", projects_dir=projects)
        assert (role, source) == (mod.UNKNOWN_ROLE, mod.SOURCE_UNRESOLVED)

    def test_an_unreadable_sidecar_never_raises_into_the_hook(self, projects) -> None:
        """Telemetry failure must not become hook failure (common.sh's own rule)."""
        mod = _role_module()
        directory = projects / "-some-project" / "parent-1" / "subagents"
        directory.mkdir(parents=True, exist_ok=True)
        # A DIRECTORY where the sidecar file should be: open() raises IsADirectoryError.
        (directory / "agent-a9.meta.json").mkdir()
        role, source = mod.resolve_role("a9", projects_dir=projects)
        assert (role, source) == (mod.UNKNOWN_ROLE, mod.SOURCE_UNRESOLVED)

    def test_a_missing_projects_dir_resolves_unknown(self, tmp_path) -> None:
        mod = _role_module()
        role, source = mod.resolve_role("a10", projects_dir=tmp_path / "does-not-exist")
        assert (role, source) == (mod.UNKNOWN_ROLE, mod.SOURCE_UNRESOLVED)

    def test_an_agent_prefixed_id_normalizes(self, projects) -> None:
        """Production ids arrive bare (`a93fab086dab25fe2`); test fixtures and some
        harness paths carry the `agent-` prefix. Both must reach the same sidecar."""
        mod = _role_module()
        _sidecar(projects, "parent-1", "a11", {"agentType": "writ-test-writer"})
        bare = mod.resolve_role("a11", projects_dir=projects)
        prefixed = mod.resolve_role("agent-a11", projects_dir=projects)
        assert bare == prefixed == ("writ-test-writer", mod.SOURCE_SIDECAR)

    def test_an_empty_agent_id_resolves_unknown_without_scanning(self, projects) -> None:
        """An empty id must not glob for `agent-.meta.json` and match something."""
        mod = _role_module()
        _sidecar(projects, "parent-1", "", {"agentType": "writ-explorer"})
        role, source = mod.resolve_role("", projects_dir=projects)
        assert (role, source) == (mod.UNKNOWN_ROLE, mod.SOURCE_UNRESOLVED)

    def test_a_traversing_agent_id_cannot_escape_the_projects_tree(self, projects,
                                                                   tmp_path) -> None:
        """`agent_id` comes off an untrusted envelope and is interpolated into a path."""
        mod = _role_module()
        outside = tmp_path / "outside.meta.json"
        outside.write_text(json.dumps({"agentType": "writ-implementer"}))
        role, source = mod.resolve_role(f"../../{outside.stem}", projects_dir=projects)
        assert (role, source) == (mod.UNKNOWN_ROLE, mod.SOURCE_UNRESOLVED)


class TestSidecarCensus:
    """The census artifact: what the resolver can see, reported rather than assumed."""

    def test_census_counts_sidecars_and_groups_by_type(self, projects) -> None:
        mod = _role_module()
        _require(mod, "sidecar_census")
        _sidecar(projects, "p1", "b1", {"agentType": "writ-explorer", "spawnDepth": 1})
        _sidecar(projects, "p1", "b2", {"agentType": "writ-explorer", "spawnDepth": 1})
        _sidecar(projects, "p1", "b3", {"agentType": "workflow-subagent", "spawnDepth": 2},
                 workflow="wf_1")
        out = mod.sidecar_census(projects_dir=projects)
        assert out["sidecars"] == 3
        assert out["by_type"] == {"writ-explorer": 2, "workflow-subagent": 1}

    def test_census_of_an_empty_tree_is_zero_not_an_error(self, projects) -> None:
        mod = _role_module()
        out = mod.sidecar_census(projects_dir=projects)
        assert out["sidecars"] == 0
        assert out["by_type"] == {}


# --------------------------------------------------------------------------- #
# Capabilities 5-7: the stop hook records what it resolved, under the right session
# --------------------------------------------------------------------------- #

class TestStopHookRecordsAResolvedRole:

    def _payload(self, agent_id: str = "c1", agent_type: str = "",
                 parent: str = "parent-1") -> str:
        return json.dumps({
            "agent_id": agent_id,
            "agent_type": agent_type,
            "session_id": parent,
            "hook_event_name": "SubagentStop",
        })

    def _completion_rows(self, friction: Path) -> list[dict]:
        if not friction.exists():
            return []
        out = []
        for line in friction.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event") in ("subagent_complete", "subagent_type_fallback"):
                out.append(row)
        return out

    def test_unresolved_records_unknown_not_general_purpose(self, sinks, projects) -> None:
        cache, friction = sinks
        result = _run(STOP_HOOK, cache=cache, friction=friction,
                      stdin=self._payload(agent_id="c1"),
                      extra_env={"WRIT_PROJECTS_DIR": str(projects)})
        assert result.returncode == 0
        rows = self._completion_rows(friction)
        assert rows, "the stop hook recorded nothing at all"
        assert not any(r.get("agent_type") == "general-purpose" for r in rows), (
            "an unobserved role was written into a governance record as a real one"
        )

    def test_the_sidecar_role_reaches_the_record(self, sinks, projects) -> None:
        cache, friction = sinks
        _sidecar(projects, "parent-1", "c2", {"agentType": "writ-explorer", "spawnDepth": 1})
        _run(STOP_HOOK, cache=cache, friction=friction,
             stdin=self._payload(agent_id="c2"),
             extra_env={"WRIT_PROJECTS_DIR": str(projects)})
        rows = self._completion_rows(friction)
        assert any(r.get("agent_type") == "writ-explorer" for r in rows), (
            f"the resolved role never reached the record: {rows}"
        )

    def test_the_record_names_its_resolution_source(self, sinks, projects) -> None:
        """Cycle M may only key enforcement on a role whose source is known."""
        cache, friction = sinks
        _sidecar(projects, "parent-1", "c3", {"agentType": "writ-planner"})
        _run(STOP_HOOK, cache=cache, friction=friction,
             stdin=self._payload(agent_id="c3"),
             extra_env={"WRIT_PROJECTS_DIR": str(projects)})
        rows = self._completion_rows(friction)
        assert any(r.get("role_source") for r in rows), (
            f"no row carries a role_source, so a default is indistinguishable "
            f"from an observation: {rows}"
        )

    def test_its_own_telemetry_row_is_filed_under_the_agent(self, sinks, projects) -> None:
        """The measured defect: 22 rows sat in writ-events-unknown.buf because this hook
        never set SESSION_ID, so the trap filed them under the literal id `unknown`."""
        cache, friction = sinks
        _run(STOP_HOOK, cache=cache, friction=friction,
             stdin=self._payload(agent_id="c4"),
             extra_env={"WRIT_PROJECTS_DIR": str(projects)})
        assert not (cache / "writ-events-unknown.buf").exists(), (
            "the stop hook still files its own row under a session nothing drains"
        )

    def test_an_empty_agent_id_still_exits_zero(self, sinks, projects) -> None:
        cache, friction = sinks
        result = _run(STOP_HOOK, cache=cache, friction=friction,
                      stdin=self._payload(agent_id=""),
                      extra_env={"WRIT_PROJECTS_DIR": str(projects)})
        assert result.returncode == 0


class TestStartHookStoresTheResolvedRole:

    def test_the_role_is_persisted_so_a_later_hook_need_not_re_derive_it(
        self, sinks, projects
    ) -> None:
        """The answer to the sidecar's ephemerality: store it at first sight."""
        cache, friction = sinks
        _sidecar(projects, "parent-2", "d1", {"agentType": "writ-implementer"})
        _run(START_HOOK, cache=cache, friction=friction,
             stdin=json.dumps({"agent_id": "d1", "agent_type": "",
                               "session_id": "parent-2",
                               "hook_event_name": "SubagentStart"}),
             extra_env={"WRIT_PROJECTS_DIR": str(projects)})
        written = list(cache.glob("writ-session-d1.json"))
        assert written, f"no cache was created for the sub-agent: {list(cache.iterdir())}"
        stored = json.loads(written[0].read_text())
        assert stored.get("agent_type") == "writ-implementer"
        assert stored.get("role_source"), "the stored role does not say where it came from"


# --------------------------------------------------------------------------- #
# Capability 8: instrumentation covers hooks, and only hooks
# --------------------------------------------------------------------------- #

class TestInstrumentationScope:

    def test_the_statusline_appends_no_row(self, sinks) -> None:
        """673 rows in one day, from a script that is not a hook, on a channel that also
        kept the unknown buffer permanently young."""
        cache, friction = sinks
        _run(STATUSLINE, cache=cache, friction=friction,
             stdin=json.dumps({"session_id": "e1", "workspace": {"current_dir": str(REPO)}}))
        rows = _buffer_rows(cache)
        assert not any(r[1] == "writ-statusline" for r in rows), (
            f"the statusline is still instrumented: {rows}"
        )

    def test_a_registered_hook_still_appends_a_row(self, sinks, tmp_path) -> None:
        """Cycle I's property must survive this narrowing: a real hook still emits."""
        cache, friction = sinks
        script = tmp_path / "hooks" / "scripts" / "writ-memory-capture.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            f'#!/usr/bin/env bash\nsource "{COMMON_SH}"\nSESSION_ID=e2\nexit 0\n')
        _run(script, cache=cache, friction=friction)
        rows = _buffer_rows(cache)
        assert any(r[1] == "writ-memory-capture" for r in rows), (
            f"narrowing the guard silenced a registered hook: {rows}"
        )

    def test_exclusion_list_covers_every_unregistered_script(self) -> None:
        """The enumeration is one name today; this is what keeps it from going stale.

        Cycle K's rule: keep the literal where reviewing it is the job, and let a derived
        test fail when the population changes.
        """
        on_disk = {p.stem for p in HOOKS.glob("*.sh")}
        unregistered = on_disk - _registered_script_names()
        text = COMMON_SH.read_text()
        missing = sorted(n for n in unregistered if n not in text)
        assert not missing, (
            f"{missing} live under hooks/scripts/ without being registered hooks, so they "
            f"are being instrumented as hooks. Add each to the exclusion in common.sh, or "
            f"move it out of hooks/scripts/."
        )

    def test_every_registered_script_is_absent_from_the_exclusion(self) -> None:
        """The inverse mistake: excluding a real hook would silence it."""
        marker_lines = [ln for ln in COMMON_SH.read_text().splitlines()
                        if "writ-statusline" in ln]
        assert marker_lines, (
            "common.sh names no excluded script, so the exclusion does not exist yet"
        )
        for name in sorted(_registered_script_names()):
            for line in marker_lines:
                assert name not in line, (
                    f"{name} is a registered hook and must not appear in the exclusion"
                )

    def test_the_inventory_still_sees_every_script(self) -> None:
        """hook_script_names() is the shared derivation; this cycle must not shrink it."""
        assert len(hook_script_names()) == len(_registered_script_names())


# --------------------------------------------------------------------------- #
# Capabilities 9-10: the doctor reports what it found, and accuses nobody on no data
# --------------------------------------------------------------------------- #

class TestDoctorReportsStrandedRows:

    def test_the_check_is_registered(self) -> None:
        assert STRANDED_CHECK in doctor_check_names()

    def test_a_seeded_unknown_buffer_is_reported_and_names_its_hooks(self, tmp_path,
                                                                     monkeypatch) -> None:
        from writ.session import doctor
        _require(doctor, "check_stranded_telemetry_buffer")
        cache = tmp_path / "cache"
        cache.mkdir()
        row = "hook_execution\x1fwrit-subagent-stop\x1f5\x1f0\x1fwork\x1f0\x1e"
        (cache / "writ-events-unknown.buf").write_text(row * 3)
        monkeypatch.setenv("WRIT_CACHE_DIR", str(cache))
        result = doctor.check_stranded_telemetry_buffer(doctor.DoctorOptions())
        assert result.status != "ok"
        assert "writ-subagent-stop" in result.detail

    def test_no_unknown_buffer_is_ok(self, tmp_path, monkeypatch) -> None:
        from writ.session import doctor
        cache = tmp_path / "cache"
        cache.mkdir()
        monkeypatch.setenv("WRIT_CACHE_DIR", str(cache))
        result = doctor.check_stranded_telemetry_buffer(doctor.DoctorOptions())
        assert result.status == "ok"

    def test_an_empty_unknown_buffer_is_ok(self, tmp_path, monkeypatch) -> None:
        """A zero-byte file is not a stranded row; reporting it would be crying wolf."""
        from writ.session import doctor
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "writ-events-unknown.buf").write_text("")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(cache))
        result = doctor.check_stranded_telemetry_buffer(doctor.DoctorOptions())
        assert result.status == "ok"


class TestDoctorReportsRoleCoverage:

    def test_the_check_is_registered(self) -> None:
        assert ROLE_COVERAGE_CHECK in doctor_check_names()

    def test_it_counts_resolved_and_unresolved_dispatches(self, tmp_path,
                                                          monkeypatch) -> None:
        from writ.session import doctor
        _require(doctor, "check_subagent_role_coverage")
        stream = tmp_path / "metrics.jsonl"
        stream.write_text("\n".join(json.dumps(r) for r in [
            {"event": "subagent_complete", "agent_type": "writ-explorer",
             "role_source": "sidecar"},
            {"event": "subagent_complete", "agent_type": "writ-explorer",
             "role_source": "envelope"},
            {"event": "subagent_complete", "agent_type": "unknown",
             "role_source": "unresolved"},
        ]) + "\n")
        monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
        result = doctor.check_subagent_role_coverage(doctor.DoctorOptions())
        assert "3" in result.detail, f"the dispatch total is not reported: {result.detail}"
        assert "2" in result.detail, f"the resolved count is not reported: {result.detail}"

    def test_rows_predating_the_field_are_unmeasured_not_unresolved(self, tmp_path,
                                                                    monkeypatch) -> None:
        """Found by running the doctor live: it read 1066 historical rows, none of which
        could carry a role_source, and reported "0 of 1066 resolved" as a warn. A row
        without the field is unmeasured; only rows that carry it are in the denominator."""
        from writ.session import doctor
        stream = tmp_path / "metrics.jsonl"
        stream.write_text("\n".join(json.dumps(r) for r in [
            {"event": "subagent_complete", "agent_type": "general-purpose"},
            {"event": "subagent_complete", "agent_type": "general-purpose"},
        ]) + "\n")
        monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
        result = doctor.check_subagent_role_coverage(doctor.DoctorOptions())
        assert result.status == "ok", result.detail
        assert "not measurable yet" in result.detail

    def test_no_dispatches_observed_reports_ok_not_an_accusation(self, tmp_path,
                                                                 monkeypatch) -> None:
        """Cycle I's lesson: an empty observation window is not evidence of a defect.
        The first version of the telemetry check accused all 40 hooks on no data."""
        from writ.session import doctor
        stream = tmp_path / "metrics.jsonl"
        stream.write_text("")
        monkeypatch.setattr(doctor, "stream_path", lambda *a, **k: str(stream))
        result = doctor.check_subagent_role_coverage(doctor.DoctorOptions())
        assert result.status == "ok"

    def test_an_unreadable_stream_reports_ok(self, tmp_path, monkeypatch) -> None:
        from writ.session import doctor
        monkeypatch.setattr(
            doctor, "stream_path", lambda *a, **k: str(tmp_path / "absent.jsonl"))
        result = doctor.check_subagent_role_coverage(doctor.DoctorOptions())
        assert result.status == "ok"


# --------------------------------------------------------------------------- #
# Capability 12: must-not-regress. No gate decision changes in this cycle.
# --------------------------------------------------------------------------- #

class TestNoGateDecisionChanges:

    def test_the_subagent_allow_is_untouched(self) -> None:
        """This cycle measures the role; it does not act on one. gates.py:314 stays a
        blanket allow until cycle M has a coverage number."""
        from writ.session import gates
        text = Path(gates.__file__).read_text()
        assert 'cache.get("is_subagent")' in text
        assert "subagent_bypass" in text

    def test_the_skill_exemption_still_short_circuits(self) -> None:
        from writ.session import gates
        result = gates._can_write_check(
            "f1",
            {"tool_input": {"file_path": str(REPO / "writ" / "session" / "gates.py")}},
            str(REPO),
            {"mode": "work", "current_phase": "planning"},
        )
        assert result["can_write"] is True

    def test_a_write_with_no_mode_is_still_refused(self) -> None:
        from writ.session import gates
        result = gates._can_write_check(
            "f2",
            {"tool_input": {"file_path": "/tmp/somewhere/else.py"}},
            "",
            {"mode": None, "current_phase": None},
        )
        assert result["can_write"] is False
