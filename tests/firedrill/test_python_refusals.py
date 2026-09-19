"""The `writ/session/gates.py` deny arms this drill exists to prove: the plan.md
implementation-freeze deny (D5), the two runtime-lens read denies (D6), the
credential-path deny (D7), and the exemption-ordering pin (D10).

These call `gates._can_write_check` / `gates._can_read_code_check` directly (no
subprocess) -- the same in-process convention `tests/test_role_write_scope.py` and
`tests/test_gate_self_observability.py` already use for this module. Records are
read back through `writ.shared.logging.read_streams` with `WRIT_LOG_ROOT` /
`WRIT_LOG_PROJECT` monkeypatched and `WRIT_FRICTION_LOG` unset (this package's
`conftest.py` already removes it via `no_friction_isolation`), never a collapsed
single file.
"""
from __future__ import annotations

import itertools
import json

import pytest

from writ.analysis.token_audit import attribute_prevented
from writ.session import gates
from writ.shared.logging import read_streams


def _isolate_logs(monkeypatch, tmp_path, project: str) -> None:
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("WRIT_LOG_PROJECT", project)
    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)


class TestPlanFreezeEmitsWriteAttempt:
    """D5: the plan.md implementation-phase deny emits nothing today; its own
    docstring admits it. The record must be emitted at the _can_write_check call
    site, not inside _check_special_files, so that helper stays pure."""

    def test_the_deny_emits_write_attempt_deny_plan_frozen_and_still_denies(
        self, tmp_path, monkeypatch
    ) -> None:
        _isolate_logs(monkeypatch, tmp_path, "firedrill-plan-freeze")
        cache = {"mode": "work", "current_phase": "implementation", "gates_approved": []}
        envelope = {"tool_input": {"file_path": str(tmp_path / "proj" / "plan.md")}}

        result = gates._can_write_check("sid-plan-freeze", envelope, "", cache)

        assert result["can_write"] is False, "plan.md must still be denied during implementation"
        assert result["reason"], "the deny must still carry a reason"

        rows = read_streams("firedrill-plan-freeze", ["audit"])
        assert rows, "audit stream is empty; the plan-freeze deny recorded nothing"
        matches = [
            r for r in rows
            if r.get("event") == "write_attempt"
            and r.get("result") == "deny"
            and r.get("gate_status") == "plan_frozen"
        ]
        assert matches, f"no write_attempt/deny/plan_frozen row in audit: {rows!r}"


_RUNTIME_LENS_CASES = [
    pytest.param("Grep", lambda proj: {"path": str(proj)}, id="grep"),
    pytest.param(
        "Read", lambda proj: {"file_path": str(proj / "src" / "thing.py")}, id="read"
    ),
    pytest.param(
        "Glob",
        lambda proj: {"path": str(proj), "pattern": str(proj / "**" / "*.py")},
        id="glob",
    ),
]
assert len(_RUNTIME_LENS_CASES) == 3


class TestRuntimeLensReadDenialsEmitReadDenied:
    """D6: both Read/Grep/Glob runtime-lens denies emit nothing today. They must
    gain a NEW event `read_denied` (not the existing `read_blocked`, which
    `token_audit.attribute_prevented` sums into a published token-floor count that
    lens denies -- carrying no byte estimate -- would silently inflate)."""

    @pytest.mark.parametrize("tool,build_input", _RUNTIME_LENS_CASES)
    def test_each_denial_emits_read_denied_on_the_audit_stream(
        self, tmp_path, monkeypatch, tool, build_input
    ) -> None:
        project = f"firedrill-read-denied-{tool.lower()}"
        _isolate_logs(monkeypatch, tmp_path, project)
        project_dir = tmp_path / "proj"
        (project_dir / "src").mkdir(parents=True, exist_ok=True)

        session_id = f"sid-read-denied-{tool.lower()}"
        (tmp_path / "cache" / f"writ-session-{session_id}.json").write_text(
            json.dumps({"mode": "debug"})
        )
        envelope = {"tool_name": tool, "tool_input": build_input(project_dir)}

        result = gates._can_read_code_check(session_id, envelope, "")
        assert result["can_read"] is False, (
            f"{tool}: expected a deny in the runtime lens with no debug.md present"
        )

        rows = read_streams(project, ["audit"])
        assert rows, f"{tool}: audit stream is empty; the runtime-lens deny recorded nothing"
        matches = [r for r in rows if r.get("event") == "read_denied"]
        assert matches, f"{tool}: no read_denied row in audit: {rows!r}"


class TestReadDeniedNeverInflatesAttributePrevented:
    """The second half of D6's capability: adding read_denied rows to a friction
    event list must not change attribute_prevented's blocked_count, because those
    rows carry no byte estimate."""

    def test_blocked_count_is_unchanged_by_read_denied_rows(self) -> None:
        baseline = [
            {
                "event": "read_blocked",
                "prevented_tokens_floor": 100,
                "gross_bytes_upper_bound": 400,
            },
            {
                "event": "read_blocked",
                "prevented_tokens_floor": 50,
                "gross_bytes_upper_bound": 200,
            },
        ]
        with_denied = baseline + [
            {"event": "read_denied", "tool_name": "Grep"},
            {"event": "read_denied", "tool_name": "Read"},
            {"event": "read_denied", "tool_name": "Glob"},
        ]

        before = attribute_prevented(baseline)
        assert before["blocked_count"] == 2, "precondition: baseline has exactly 2 read_blocked rows"

        after = attribute_prevented(with_denied)
        assert after["blocked_count"] == before["blocked_count"]
        assert after["prevented_tokens_floor"] == before["prevented_tokens_floor"]


_CRED_SUFFIXES = [".env", "server.pem", "id_rsa"]
_CRED_CACHE_KINDS = ["ordinary", "skill_dir", "subagent_cache"]
_CRED_CROSS_PRODUCT = list(itertools.product(_CRED_CACHE_KINDS, _CRED_SUFFIXES))
assert len(_CRED_CROSS_PRODUCT) == 9


class TestCredentialPathDeny:
    """D7: no behavioral test exists for the credential-path deny today (it is
    touched only incidentally by a monotonicity test whose assertion never fires
    for it). Covers .env, *.pem and id_rsa, including under a skill-dir path and a
    sub-agent cache, since this arm runs ahead of every exemption."""

    @pytest.mark.parametrize("cache_kind,suffix", _CRED_CROSS_PRODUCT)
    def test_credential_paths_are_refused_regardless_of_exemption(
        self, tmp_path, cache_kind, suffix
    ) -> None:
        skill_dir = str(tmp_path / "skill")
        if cache_kind == "skill_dir":
            path = f"{skill_dir}/secrets/{suffix}"
            cache = {"mode": "work"}
        elif cache_kind == "subagent_cache":
            path = f"{tmp_path}/proj/{suffix}"
            cache = {"is_subagent": True, "mode": "work"}
        else:
            path = f"{tmp_path}/proj/{suffix}"
            cache = {"mode": "work"}

        envelope = {"tool_input": {"file_path": path}}
        result = gates._can_write_check(f"sid-cred-{cache_kind}", envelope, skill_dir, cache)

        assert result["can_write"] is False, (
            f"{cache_kind}/{suffix}: a credential path must be refused even under an exemption"
        )
        assert result["reason"], f"{cache_kind}/{suffix}: the refusal must carry a reason"


class TestSkillDirExemptionPrecedesRoleScope:
    """D10: the skill-dir exemption returns before the role-scope check, so the
    role deny cannot fire for any path inside the skill directory. The code
    documents this deliberately (you cannot require a gate's permission to edit
    the gate), but nothing pinned the ordering before this drill."""

    def test_a_role_scoped_write_inside_the_skill_dir_is_allowed_as_skill_exempt(
        self, tmp_path, monkeypatch
    ) -> None:
        _isolate_logs(monkeypatch, tmp_path, "firedrill-skill-exempt-ordering")
        skill_dir = str(tmp_path / "skill")
        target = f"{skill_dir}/writ/session/gates.py"
        cache = {
            "is_subagent": True,
            "cache_source": "subagent_start",
            "agent_type": "writ-explorer",
            "role_source": "envelope",
            "role_write_scope": [],  # an empty declared scope denies EVERY out-of-scope path
        }
        envelope = {"tool_input": {"file_path": target}}

        result = gates._can_write_check("sid-skill-exempt-ordering", envelope, skill_dir, cache)

        assert result["can_write"] is True, (
            "a role-scoped write inside the skill dir must be allowed as skill_exempt, "
            "never reach the role deny"
        )
        rows = read_streams("firedrill-skill-exempt-ordering", ["audit", "friction"])
        assert rows, "no telemetry recorded for the skill-dir write at all"
        matches = [r for r in rows if r.get("gate_status") == "skill_exempt"]
        assert matches, f"no skill_exempt row recorded: {rows!r}"
        assert not any(r.get("gate_status") == "role_scope_deny" for r in rows), (
            "the role-scope arm must never fire for a skill-dir path: " f"{rows!r}"
        )
