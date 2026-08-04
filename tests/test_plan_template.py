"""Plan-format failures fix: canonical templates + citable-injected-ids
(plan.md / capabilities.md, "Plan-format failures" scope added at approval).

Test skeleton for the capability gate. Every test in this file is RED until the
implementer creates templates/plan-template.md + templates/capabilities-template.md,
points every plan-writing surface at the template, and fixes the loaded-rule
tracking so an injected ABS-* abstraction id is citable in ## Rules Applied.

Run interpreter: .venv/bin/python -m pytest

Root-cause note (cmd_format, writ/session/budget_tracking.py:418-424): when the
RAG response contains a summary-mode abstraction entry (a dict with an
'abstraction_id' key instead of 'rule_id' -- see cmd_format's "[ABSTRACT: ...]"
render branch and writ/server "_summary_with_abstractions"), the WRIT_META
rule_ids list built at the end of cmd_format currently walks
`rule.get("rule_id")` and `rule.get("rule_ids", [])` (the covered-rule members)
but never `rule.get("abstraction_id")` itself. The abstraction id the model
actually saw and would naturally cite is therefore NEVER added to
loaded_rule_ids, so _validate_phase_a's hallucination check (approval_workflow.py)
flags it as hallucinated even though Writ's own hook injected it this session.

Capability map (maps to capabilities.md checkboxes):
  [tmpl-1]   templates/plan-template.md filled with dummy data passes _validate_phase_a with no error
  [tmpl-2]   templates/capabilities-template.md uses checkbox format matching the plan gate
  [msg-1]    the missing-plan.md rejection message names the template path
  [msg-2]    the reasonless-##-Files-line rejection message names the template path
  [dir-1]    writ-rag-inject.sh's work-mode directive names the template path
  [dir-2]    agents/writ-planner.md's role prompt names the template path
  [abs-1]    cmd_format's WRIT_META rule_ids includes the abstraction_id of an
             injected [ABSTRACT: ...] entry, not just its covered rule_ids
  [abs-2]    _validate_phase_a accepts a plan citing an ABS-* id that is present
             in the session's loaded_rule_ids (citable-once-injected, generalized
             beyond plain RULE-ID-shaped ids)
  [abs-3]    _validate_phase_a still REJECTS an ABS-* id that was never loaded
             this session (regression guard: the fix must not blanket-accept
             every ABS-* id, only ones actually injected)
  [abs-4]    end-to-end: an abstraction injected via cmd_format flows through
             --add-rules into loaded_rule_ids, and a plan citing that exact
             abstraction id then passes _validate_phase_a
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
PLAN_TEMPLATE = os.path.join(SKILL_ROOT, "templates", "plan-template.md")
CAPABILITIES_TEMPLATE = os.path.join(SKILL_ROOT, "templates", "capabilities-template.md")
RAG_INJECT_SH = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-rag-inject.sh")
PLANNER_MD = os.path.join(SKILL_ROOT, "agents", "writ-planner.md")

_TEMPLATE_REF = "templates/plan-template.md"


def _seed(sid: str, **fields) -> None:
    """Mirrors the `_seed` helper in test_bash_write_gate.py / test_pol6g1_*."""
    from writ.session import cache as session_cache
    data = session_cache._read_cache(sid)
    data.update(fields)
    session_cache._write_cache(sid, data)


# ---------------------------------------------------------------------------
# [tmpl-1] templates/plan-template.md exists and passes the gate as-is
# ---------------------------------------------------------------------------

class TestPlanTemplateFile:
    def test_plan_template_exists(self) -> None:
        assert os.path.exists(PLAN_TEMPLATE), (
            f"{PLAN_TEMPLATE} must exist -- the canonical fill-in plan skeleton"
        )

    def test_capabilities_template_exists(self) -> None:
        # [tmpl-2]
        assert os.path.exists(CAPABILITIES_TEMPLATE), (
            f"{CAPABILITIES_TEMPLATE} must exist -- the matching capabilities checkbox skeleton"
        )

    def test_capabilities_template_uses_checkbox_format(self) -> None:
        # [tmpl-2]: at least one '- [ ] ...' line, matching the plan gate's
        # ## Capabilities checkbox contract.
        content = Path(CAPABILITIES_TEMPLATE).read_text()
        assert "- [ ]" in content, (
            "capabilities-template.md must use '- [ ] description' checkbox format"
        )


class TestPlanTemplatePassesPhaseAGate(object):
    def test_template_filled_with_dummy_data_passes_validate_phase_a(self, tmp_path: Path) -> None:
        # [tmpl-1] (keystone): the template, as authored (its own placeholder/dummy
        # content), must pass _validate_phase_a with NO error -- this is what
        # pins the template to the gate contract so they can never drift apart.
        # Called WITHOUT a session_id, so the rule-id hallucination branch (which
        # only runs `elif has_rule_id and session_id:`) is not exercised here --
        # this test is purely about STRUCTURAL completeness of the four sections.
        from writ.session.approval_workflow import _validate_phase_a

        content = Path(PLAN_TEMPLATE).read_text()
        (tmp_path / "plan.md").write_text(content)

        error = _validate_phase_a(str(tmp_path))
        assert error is None, (
            f"templates/plan-template.md must pass _validate_phase_a as authored; "
            f"got error: {error!r}"
        )

    def test_template_has_all_four_required_sections(self) -> None:
        # Sentinel companion to the gate-pass test above: names the sections
        # explicitly so a future template edit that silently drops one is
        # caught here as well as by the gate check.
        content = Path(PLAN_TEMPLATE).read_text()
        for heading in ("## Files", "## Analysis", "## Rules Applied", "## Capabilities"):
            assert heading in content, f"plan-template.md is missing {heading!r}"

    def test_template_files_section_has_a_fully_annotated_example_line(self) -> None:
        # The ## Files line grammar (path, change_type, reason) is the #1 rejection
        # cause per the plan's Analysis -- the template must model it concretely,
        # not just describe it in prose.
        content = Path(PLAN_TEMPLATE).read_text()
        files_section = content.split("## Files", 1)[1].split("## Analysis", 1)[0]
        assert "`" in files_section and "--" in files_section, (
            "the ## Files section must contain at least one backtick-path "
            "'(change_type) -- reason' example line"
        )

    def test_template_capabilities_section_starts_unchecked(self) -> None:
        # _validate_phase_a rejects a plan whose ## Capabilities has any [x] box.
        content = Path(PLAN_TEMPLATE).read_text()
        caps_section = content.split("## Capabilities", 1)[1]
        assert "[x]" not in caps_section, (
            "the template's ## Capabilities example items must start unchecked ([ ]), "
            "never pre-checked ([x])"
        )


# ---------------------------------------------------------------------------
# [msg-1, msg-2] validator rejection messages name the template path
# ---------------------------------------------------------------------------

class TestValidatorMessagesNameTemplatePath:
    def test_missing_plan_message_names_template_path(self, tmp_path: Path) -> None:
        # [msg-1]: with NO plan.md at all, the returned guidance must point the
        # agent at the canonical template instead of re-describing the four
        # sections from memory (the exact drift the plan.md Analysis names).
        from writ.session.approval_workflow import _validate_phase_a

        error = _validate_phase_a(str(tmp_path))
        assert error is not None
        assert _TEMPLATE_REF in error, (
            f"the plan-missing message must name {_TEMPLATE_REF!r}; got: {error!r}"
        )

    def test_reasonless_files_line_message_names_template_path(self, tmp_path: Path) -> None:
        # [msg-2]: a structurally-present-but-malformed ## Files line is the
        # single most common per-line rejection; its message must also point
        # back at the template so the fix path is unambiguous.
        from writ.session.approval_workflow import _validate_phase_a

        reasonless_plan = (
            "# Plan: broken plan\n\n"
            "## Files\n\n"
            "- `writ/session/plan_harvest.py` (create)\n\n"
            "## Analysis\n\nSome rationale.\n\n"
            "## Rules Applied\n\n- **ERR-HANDLE-001** -- fail-open capture\n\n"
            "## Capabilities\n\n- [ ] some capability\n"
        )
        (tmp_path / "plan.md").write_text(reasonless_plan)
        error = _validate_phase_a(str(tmp_path))
        assert error is not None
        assert _TEMPLATE_REF in error, (
            f"the reasonless-##-Files-line rejection must name {_TEMPLATE_REF!r}; "
            f"got: {error!r}"
        )


# ---------------------------------------------------------------------------
# [dir-1, dir-2] every plan-writing surface points at the template
# ---------------------------------------------------------------------------

class TestSurfacesPointAtTemplate:
    def test_work_mode_directive_names_template_path(self) -> None:
        # [dir-1]: writ-rag-inject.sh's WORKROUTE heredoc (the first thing an
        # agent sees when auto-routed into work mode) must name the template,
        # not just "write plan.md and capabilities.md at the project root".
        content = Path(RAG_INJECT_SH).read_text()
        assert _TEMPLATE_REF in content, (
            f"writ-rag-inject.sh's work-mode directive must reference {_TEMPLATE_REF!r}"
        )

    def test_writ_planner_role_prompt_names_template_path(self) -> None:
        # [dir-2]: agents/writ-planner.md is the sub-agent role explicitly
        # dispatched to author plan.md; its instructions must point at the
        # template rather than re-deriving the section list from memory.
        content = Path(PLANNER_MD).read_text()
        assert _TEMPLATE_REF in content, (
            f"agents/writ-planner.md must reference {_TEMPLATE_REF!r}"
        )


# ---------------------------------------------------------------------------
# [abs-1] cmd_format includes the abstraction_id itself in WRIT_META rule_ids
# ---------------------------------------------------------------------------

class TestCmdFormatIncludesAbstractionId:
    def _format_and_get_meta(self, monkeypatch: pytest.MonkeyPatch, response: dict) -> dict:
        import io
        import json as _json
        from writ.session import budget_tracking

        monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(response)))
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            budget_tracking.cmd_format()
        out = buf.getvalue()
        meta_line = next(
            (line for line in out.splitlines() if line.startswith("WRIT_META:")), None
        )
        assert meta_line is not None, f"cmd_format must emit a WRIT_META: line; got: {out!r}"
        return _json.loads(meta_line[len("WRIT_META:"):])

    def test_abstraction_id_present_in_meta_rule_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # [abs-1] (root-cause pin): a response whose only rule entry is a
        # summary-mode abstraction (abstraction_id + covered rule_ids, no
        # top-level rule_id) must surface the ABSTRACTION's own id in
        # WRIT_META.rule_ids, not just the ids it covers.
        response = {
            "mode": "summary",
            "rules": [{
                "abstraction_id": "ABS-TESTPLAN-001",
                "rule_ids": ["FOO-001", "BAR-002"],
                "summary": "covers 2 rules",
                "domain": "Testing",
            }],
        }
        meta = self._format_and_get_meta(monkeypatch, response)
        assert "ABS-TESTPLAN-001" in meta["rule_ids"], (
            f"WRIT_META.rule_ids must include the abstraction_id itself; "
            f"got: {meta['rule_ids']!r}"
        )

    def test_covered_rule_ids_still_present_alongside_abstraction_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression guard: fixing [abs-1] must not DROP the existing covered
        # rule_ids behavior (they still matter for the exclude-list use).
        response = {
            "mode": "summary",
            "rules": [{
                "abstraction_id": "ABS-TESTPLAN-002",
                "rule_ids": ["FOO-003"],
                "summary": "covers 1 rule",
                "domain": "Testing",
            }],
        }
        meta = self._format_and_get_meta(monkeypatch, response)
        assert "FOO-003" in meta["rule_ids"]
        assert "ABS-TESTPLAN-002" in meta["rule_ids"]

    def test_plain_rule_entries_unaffected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Regression guard: an ordinary (non-abstraction) rule entry must
        # continue to surface its rule_id exactly as before.
        response = {
            "mode": "standard",
            "rules": [{"rule_id": "FOO-004", "statement": "do it", "score": 0.9}],
        }
        meta = self._format_and_get_meta(monkeypatch, response)
        assert meta["rule_ids"] == ["FOO-004"]


# ---------------------------------------------------------------------------
# [abs-2, abs-3] _validate_phase_a citability of ABS-* ids
# ---------------------------------------------------------------------------

class TestValidatePhaseAAcceptsInjectedAbstractionIds:
    def test_accepts_cited_abs_id_present_in_loaded_rule_ids(self, tmp_path: Path) -> None:
        # [abs-2]: an ABS-* id present in the session's loaded_rule_ids (i.e. it
        # was actually injected this session) must be citable in ## Rules
        # Applied without tripping the hallucination check.
        from writ.session.approval_workflow import _validate_phase_a

        sid = "test-plan-tmpl-abs2"
        _seed(sid, loaded_rule_ids=["ABS-TESTPLAN-010"], loaded_rule_ids_by_phase={})

        plan = (
            "# Plan: cites an injected abstraction\n\n"
            "## Files\n\n- `writ/session/x.py` (create) -- adds x\n\n"
            "## Analysis\n\nRationale text here.\n\n"
            "## Rules Applied\n\n- **ABS-TESTPLAN-010** -- covers the relevant pattern\n\n"
            "## Capabilities\n\n- [ ] does x\n"
        )
        (tmp_path / "plan.md").write_text(plan)
        error = _validate_phase_a(str(tmp_path), session_id=sid)
        assert error is None, (
            f"an ABS-* id present in loaded_rule_ids must be citable; got error: {error!r}"
        )

    def test_accepts_cited_abs_id_present_in_always_on_rule_ids(self, tmp_path: Path) -> None:
        # [abs-2] variant: the always-on channel's injected ids are also a
        # legitimate citation source (mirrors the existing rule-id widening at
        # approval_workflow.py:127).
        from writ.session.approval_workflow import _validate_phase_a

        sid = "test-plan-tmpl-abs2b"
        _seed(sid, loaded_rule_ids=[], loaded_rule_ids_by_phase={}, always_on_rule_ids=["ABS-TESTPLAN-011"])

        plan = (
            "# Plan: cites an always-on abstraction\n\n"
            "## Files\n\n- `writ/session/x.py` (create) -- adds x\n\n"
            "## Analysis\n\nRationale text here.\n\n"
            "## Rules Applied\n\n- **ABS-TESTPLAN-011** -- always-on coverage\n\n"
            "## Capabilities\n\n- [ ] does x\n"
        )
        (tmp_path / "plan.md").write_text(plan)
        error = _validate_phase_a(str(tmp_path), session_id=sid)
        assert error is None, f"got error: {error!r}"

    def test_still_rejects_abs_id_never_loaded_this_session(self, tmp_path: Path) -> None:
        # [abs-3] (regression guard): the fix must not blanket-whitelist every
        # ABS-* shaped id -- only ones this session actually saw injected.
        from writ.session.approval_workflow import _validate_phase_a

        sid = "test-plan-tmpl-abs3"
        _seed(sid, loaded_rule_ids=["ABS-TESTPLAN-020"], loaded_rule_ids_by_phase={})

        plan = (
            "# Plan: cites a NEVER-injected abstraction\n\n"
            "## Files\n\n- `writ/session/x.py` (create) -- adds x\n\n"
            "## Analysis\n\nRationale text here.\n\n"
            "## Rules Applied\n\n- **ABS-TESTPLAN-999** -- never actually injected\n\n"
            "## Capabilities\n\n- [ ] does x\n"
        )
        (tmp_path / "plan.md").write_text(plan)
        error = _validate_phase_a(str(tmp_path), session_id=sid)
        assert error is not None, (
            "an ABS-* id that was never loaded this session must still be "
            "flagged as hallucinated -- the fix must be scoped, not a blanket accept"
        )
        assert "ABS-TESTPLAN-999" in error


# ---------------------------------------------------------------------------
# [abs-4] end-to-end: injected abstraction -> loaded_rule_ids -> citable
# ---------------------------------------------------------------------------

class TestEndToEndInjectedAbstractionIsCitable:
    def test_abstraction_injected_via_cmd_format_then_cited_passes_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [abs-4]: reproduces the real session flow end-to-end:
        #   1. a RAG response containing an [ABSTRACT: ...] entry is formatted
        #      by cmd_format, producing a WRIT_META line with rule_ids.
        #   2. those rule_ids are recorded into the session cache exactly as
        #      writ-rag-inject.sh does (via cmd_update's --add-rules flag).
        #   3. a plan citing that exact abstraction id in ## Rules Applied is
        #      then accepted by _validate_phase_a.
        # This is the concrete reproduction of the live defect described in
        # plan.md: "the validator rejected ABS-* abstraction ids as
        # hallucinated although Writ's own hooks injected them this session."
        import contextlib
        import io
        import json as _json
        from writ.session import budget_tracking
        from writ.session.approval_workflow import _validate_phase_a

        response = {
            "mode": "summary",
            "rules": [{
                "abstraction_id": "ABS-TESTPLAN-E2E",
                "rule_ids": ["FOO-E2E-001"],
                "summary": "covers 1 rule",
                "domain": "Testing",
            }],
        }
        monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(response)))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            budget_tracking.cmd_format()
        out = buf.getvalue()
        meta_line = next(line for line in out.splitlines() if line.startswith("WRIT_META:"))
        meta = _json.loads(meta_line[len("WRIT_META:"):])

        sid = "test-plan-tmpl-abs4-e2e"
        budget_tracking.cmd_update(sid, ["--add-rules", _json.dumps(meta["rule_ids"])])

        plan = (
            "# Plan: end-to-end abstraction citation\n\n"
            "## Files\n\n- `writ/session/x.py` (create) -- adds x\n\n"
            "## Analysis\n\nRationale text here.\n\n"
            "## Rules Applied\n\n- **ABS-TESTPLAN-E2E** -- covers the pattern\n\n"
            "## Capabilities\n\n- [ ] does x\n"
        )
        (tmp_path / "plan.md").write_text(plan)
        error = _validate_phase_a(str(tmp_path), session_id=sid)
        assert error is None, (
            f"an abstraction injected this session via cmd_format must be citable "
            f"end-to-end; got error: {error!r}"
        )
