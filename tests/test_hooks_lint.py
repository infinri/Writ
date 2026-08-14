"""Task #7C: static hook-delivery lint.

Synthetic-fixture tests pin the classifier logic (inert / review / clean); a
real-corpus guard asserts the linter never false-flags the known-good hooks
(the C1 lesson: a 13-agent audit once false-flagged a working gate).

Cycle G adds a "rejected" severity: threading the wired event into
_reaches_model alone would NOT have caught the writ-postcompact.sh bug (the
script never reached that check -- it uses a python3 heredoc, not `cat`, and
never calls log_rag_query_event, so it fails both _is_injector branches). The
new detection path runs BEFORE the _is_injector gate and flags any wired
script whose comment-stripped source emits additionalContext on an event
outside writ.shared.delivery.ADDITIONAL_CONTEXT_EVENTS -- "rejected", sorted
above "inert". Comment stripping is the precision guard from the C1 lesson:
writ-precompact.sh mentions additionalContext only in prose about why it does
not use it, and must stay unflagged.
"""
from __future__ import annotations

import json
from pathlib import Path

from writ.hooks_lint import lint_hooks

WRIT_ROOT = Path(__file__).resolve().parent.parent


def _hooks_json(tmp: Path, mapping: dict[str, list[tuple[str, str]]]) -> Path:
    """mapping: event -> [(matcher, script_basename), ...]."""
    hooks: dict = {}
    for event, entries in mapping.items():
        hooks[event] = [
            {"matcher": matcher, "hooks": [
                {"type": "command",
                 "command": f"bash ${{CLAUDE_PLUGIN_ROOT}}/hooks/scripts/{script}"}]}
            for matcher, script in entries
        ]
    p = tmp / "hooks.json"
    p.write_text(json.dumps({"hooks": hooks}))
    return p


def _script(tmp: Path, name: str, body: str) -> None:
    d = tmp / "hooks" / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)


class TestSyntheticClassification:
    def test_bare_stdout_injector_on_pretooluse_is_inert(self, tmp_path: Path) -> None:
        _script(tmp_path, "inj.sh", '#!/bin/bash\necho "[Writ: file rules] do X"\n')
        hj = _hooks_json(tmp_path, {"PreToolUse": [("Read", "inj.sh")]})
        out = lint_hooks(hj, tmp_path)
        assert [f["severity"] for f in out] == ["inert"]
        assert out[0]["script"] == "inj.sh"

    def test_log_rag_query_event_with_no_channel_is_inert(self, tmp_path: Path) -> None:
        _script(tmp_path, "rag.sh", '#!/bin/bash\nlog_rag_query_event a b c d e\n')
        hj = _hooks_json(tmp_path, {"PostToolUse": [("Write|Edit", "rag.sh")]})
        out = lint_hooks(hj, tmp_path)
        assert [f["severity"] for f in out] == ["inert"]

    def test_heredoc_marker_to_stdout_is_inert(self, tmp_path: Path) -> None:
        body = "#!/bin/bash\ncat <<'EOF'\n[WRIT QUALITY-JUDGE] review this\nEOF\n"
        _script(tmp_path, "judge.sh", body)
        hj = _hooks_json(tmp_path, {"PostToolUse": [("Write", "judge.sh")]})
        out = lint_hooks(hj, tmp_path)
        assert [f["severity"] for f in out] == ["inert"]

    def test_marker_to_stderr_is_not_flagged(self, tmp_path: Path) -> None:
        # validate-rules.sh pattern: a gate that warns to stderr is NOT an injector.
        _script(tmp_path, "gate.sh", '#!/bin/bash\necho "[Writ: warning] bad" >&2\n')
        hj = _hooks_json(tmp_path, {"PostToolUse": [("Write|Edit", "gate.sh")]})
        assert lint_hooks(hj, tmp_path) == []

    def test_marker_only_in_comment_is_not_flagged(self, tmp_path: Path) -> None:
        # cwd-changed.sh pattern: the marker (and /query) appear only in a comment.
        _script(tmp_path, "cwd.sh", '#!/bin/bash\n# hits the /query endpoint [Writ:]\nexit 0\n')
        hj = _hooks_json(tmp_path, {"CwdChanged": [("", "cwd.sh")]})
        assert lint_hooks(hj, tmp_path) == []

    def test_additional_context_wrapper_is_clean(self, tmp_path: Path) -> None:
        # bible-authoring-push.sh pattern: marker rides additionalContext JSON.
        body = ('#!/bin/bash\nprintf \'%s\' '
                '"{\\"hookSpecificOutput\\":{\\"additionalContext\\":\\"[Writ: x]\\"}}"\n')
        _script(tmp_path, "push.sh", body)
        hj = _hooks_json(tmp_path, {"PostToolUse": [("Write|Edit", "push.sh")]})
        assert lint_hooks(hj, tmp_path) == []

    def test_special_event_stdout_is_clean(self, tmp_path: Path) -> None:
        # rag-inject.sh pattern: bare stdout on UserPromptSubmit DOES reach model.
        _script(tmp_path, "ups.sh", '#!/bin/bash\necho "[Writ: rules] ..."\n')
        hj = _hooks_json(tmp_path, {"UserPromptSubmit": [("", "ups.sh")]})
        assert lint_hooks(hj, tmp_path) == []

    def test_mixed_model_channel_plus_bare_stdout_is_review(self, tmp_path: Path) -> None:
        body = ('#!/bin/bash\n'
                'if deny; then echo "{\\"permissionDecisionReason\\":\\"no\\"}"; fi\n'
                'echo "[Writ: file-context rules] also this"\n')
        _script(tmp_path, "mixed.sh", body)
        hj = _hooks_json(tmp_path, {"PreToolUse": [("Write|Edit", "mixed.sh")]})
        out = lint_hooks(hj, tmp_path)
        assert [f["severity"] for f in out] == ["review"]

    def test_captured_heredoc_is_clean(self, tmp_path: Path) -> None:
        # VAR=$(cat <<EOF ... EOF) feeds a substitution, not stdout -> not bare.
        body = ("#!/bin/bash\nAC=$(cat <<EOF\n[WRIT QUALITY-JUDGE] x\nEOF\n)\n"
                "echo \"{\\\"hookSpecificOutput\\\":{\\\"additionalContext\\\":\\\"$AC\\\"}}\"\n")
        _script(tmp_path, "cap.sh", body)
        hj = _hooks_json(tmp_path, {"PostToolUse": [("Write", "cap.sh")]})
        assert lint_hooks(hj, tmp_path) == []

    def test_substitution_same_line_heredoc_is_clean(self, tmp_path: Path) -> None:
        body = ("#!/bin/bash\nT=$(cat <<'W'\n[Writ: Work mode]\nW\n)\n"
                'printf \'%s\' "{\\"hookSpecificOutput\\":{\\"additionalContext\\":\\"$T\\"}}"\n')
        _script(tmp_path, "sub.sh", body)
        hj = _hooks_json(tmp_path, {"PostToolUse": [("Bash", "sub.sh")]})
        assert lint_hooks(hj, tmp_path) == []

    def test_unreadable_hooks_json_yields_error(self, tmp_path: Path) -> None:
        out = lint_hooks(tmp_path / "missing.json", tmp_path)
        assert out and out[0]["severity"] == "error"

    def test_additional_context_on_postcompact_is_rejected(self, tmp_path: Path) -> None:
        # writ-postcompact.sh pattern: a python3 heredoc (not `cat`, no
        # log_rag_query_event call) emits hookSpecificOutput.additionalContext
        # on PostCompact -- an event outside ADDITIONAL_CONTEXT_EVENTS. This
        # script fails BOTH _is_injector branches, so only the new
        # pre-_is_injector detection path can catch it.
        body = (
            "#!/bin/bash\n"
            "python3 <<'PY'\n"
            "import json\n"
            "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostCompact', "
            "'additionalContext': '[Writ: context compacted]'}}))\n"
            "PY\n"
        )
        _script(tmp_path, "rejected.sh", body)
        hj = _hooks_json(tmp_path, {"PostCompact": [("", "rejected.sh")]})
        out = lint_hooks(hj, tmp_path)
        assert [f["severity"] for f in out] == ["rejected"]
        assert out[0]["script"] == "rejected.sh"
        assert out[0]["event"] == "PostCompact"

    def test_additional_context_mentioned_only_in_comment_is_not_flagged(self, tmp_path: Path) -> None:
        # writ-precompact.sh pattern (the C1 lesson applied to the new path): a
        # comment explaining why the hook does NOT use additionalContext must
        # not itself trip the rejected-severity detector.
        body = (
            "#!/bin/bash\n"
            "# PostCompact has no additionalContext channel; do not emit one here.\n"
            "exit 0\n"
        )
        _script(tmp_path, "commented.sh", body)
        hj = _hooks_json(tmp_path, {"PostCompact": [("", "commented.sh")]})
        assert lint_hooks(hj, tmp_path) == []

    def test_additional_context_on_a_valid_event_is_not_rejected(self, tmp_path: Path) -> None:
        # Same emission shape as the PostCompact fixture above, wired to an
        # ADDITIONAL_CONTEXT_EVENTS member instead -- must not be flagged
        # "rejected" (and is already a known-clean shape for "inert"/"review").
        body = ('#!/bin/bash\nprintf \'%s\' '
                '"{\\"hookSpecificOutput\\":{\\"additionalContext\\":\\"[Writ: x]\\"}}"\n')
        _script(tmp_path, "valid.sh", body)
        hj = _hooks_json(tmp_path, {"Stop": [("", "valid.sh")]})
        assert lint_hooks(hj, tmp_path) == []

    def test_rejected_severity_sorts_above_inert(self, tmp_path: Path) -> None:
        rejected_body = (
            "#!/bin/bash\n"
            "python3 <<'PY'\n"
            "import json\n"
            "print(json.dumps({'hookSpecificOutput': {'additionalContext': '[Writ: x]'}}))\n"
            "PY\n"
        )
        inert_body = '#!/bin/bash\necho "[Writ: file rules] do X"\n'
        _script(tmp_path, "a-rejected.sh", rejected_body)
        _script(tmp_path, "b-inert.sh", inert_body)
        hj = _hooks_json(tmp_path, {
            "PostCompact": [("", "a-rejected.sh")],
            "PreToolUse": [("Read", "b-inert.sh")],
        })
        out = lint_hooks(hj, tmp_path)
        assert [f["severity"] for f in out] == ["rejected", "inert"], (
            f"rejected must sort above inert; got {[(f['severity'], f['script']) for f in out]!r}"
        )


class TestRealCorpus:
    """Run against the live hooks.json -- regression guard for precision.

    Cycle G: writ-postcompact.sh now emits NOTHING (its former
    hookSpecificOutput.additionalContext reply is what CC's validator rejected
    on the real PostCompact event), so it carries neither an additionalContext
    emission nor a bare-stdout marker and the new "rejected" detection path
    finds nothing to flag either. validate-exit-plan.sh's allow path already
    uses additionalContext on a member event. The live lint must report
    NOTHING. This is the mechanism that confirms the fix: a regression back to
    emitting additionalContext on PostCompact (or to bare stdout) re-flags the
    script here."""

    def _findings(self) -> list[dict]:
        return lint_hooks(WRIT_ROOT / "hooks" / "hooks.json", WRIT_ROOT)

    def test_fixed_injectors_no_longer_flagged(self) -> None:
        # #2 converted these to additionalContext -- must be clean now.
        flagged = {f["script"] for f in self._findings()}
        assert flagged.isdisjoint({
            "writ-read-rag.sh", "writ-posttool-rag.sh",
            "inject-tier-workflow.sh", "writ-quality-judge.sh",
            "writ-pre-write-dispatch.sh",
        })

    def test_no_inert_or_review_findings_remain(self) -> None:
        # Item 6 fixed the last two known offenders (writ-postcompact.sh: inert,
        # validate-exit-plan.sh: review). The lint is the authority on scope here:
        # after the fix it must report zero inert and zero review findings.
        findings = self._findings()
        assert findings == [], (
            "live hook-delivery lint expected zero findings after item 6 "
            f"(writ-postcompact.sh, validate-exit-plan.sh); got: {findings!r}"
        )

    def test_working_hooks_never_flagged(self) -> None:
        flagged = {f["script"] for f in self._findings()}
        # bible-push/dispatch-discipline use additionalContext; validate-rules &
        # cwd-changed are gates/state hooks (stderr / comment-only marker).
        assert flagged.isdisjoint({
            "writ-bible-authoring-push.sh", "writ-dispatch-discipline.sh",
            "writ-rag-inject.sh", "session-start-bootstrap.sh",
            "auto-approve-gate.sh", "validate-rules.sh", "writ-cwd-changed.sh",
        })


class TestHookLintSummary:
    """Cycle H's folded fix pins `writ.cli._hook_lint_summary(findings: list[dict])
    -> str`, the planned module-level private helper the `integrity` command's
    hook-delivery lint header will call instead of its two inline list
    comprehensions (`inert = [...]`, `review = [...]`). Today's header --

        f"\\nHook delivery lint (#7C, WARNING) -- {len(inert)} inert, {len(review)} review:"

    -- counts only two of the four severities `lint_hooks` can emit. A
    `rejected` finding (cycle G) prints in the body loop but is invisible in
    the count line an operator actually reads. The fixed helper names all
    three countable severities, `rejected` first (mirroring the sort order
    `TestSyntheticClassification.test_rejected_severity_sorts_above_inert`
    already pins in `lint_hooks` itself), and leaves `error` (the
    unreadable-hooks.json case) out of every count, matching today's
    behavior.

    Each test imports `_hook_lint_summary` locally rather than at module
    scope: the helper does not exist yet, so a module-level import would
    turn every class above into a single collection error. A local import
    keeps that RED confined to this class -- `writ.cli` has no
    `_hook_lint_summary` attribute yet, so each test below fails with an
    ImportError, not a syntax or collection error, and the classes above
    are unaffected.
    """

    def _summary(self, findings: list[dict]) -> str:
        from writ.cli import _hook_lint_summary
        return _hook_lint_summary(findings)

    def test_mixed_findings_name_all_three_counts_rejected_first(self) -> None:
        findings = [
            {"severity": "rejected"},
            {"severity": "rejected"},
            {"severity": "inert"},
            {"severity": "review"},
            {"severity": "review"},
            {"severity": "review"},
        ]
        out = self._summary(findings)
        assert out == (
            "\nHook delivery lint (#7C, WARNING) -- "
            "2 rejected, 1 inert, 3 review:"
        )

    def test_rejected_only_list_reports_inert_and_review_as_zero(self) -> None:
        findings = [
            {"severity": "rejected"},
            {"severity": "rejected"},
            {"severity": "rejected"},
        ]
        out = self._summary(findings)
        assert out == (
            "\nHook delivery lint (#7C, WARNING) -- "
            "3 rejected, 0 inert, 0 review:"
        )

    def test_no_rejected_findings_matches_the_pre_fix_inert_and_review_tail(self) -> None:
        # Additive, no regression: before cycle H the header carried only
        # f"{len(inert)} inert, {len(review)} review:" with no rejected
        # count at all. With zero rejected findings the fixed helper must
        # still produce that exact inert/review substring unchanged, merely
        # prepending "0 rejected, " ahead of it.
        findings = [
            {"severity": "inert"},
            {"severity": "inert"},
            {"severity": "review"},
        ]
        out = self._summary(findings)
        assert out == (
            "\nHook delivery lint (#7C, WARNING) -- "
            "0 rejected, 2 inert, 1 review:"
        )
        assert out.endswith("2 inert, 1 review:")  # the pre-fix tail, unchanged

    def test_error_severity_is_excluded_from_every_count(self) -> None:
        # The unreadable-hooks.json case: lint_hooks returns a single
        # {"severity": "error", ...} finding. error stays uncounted in the
        # header today and must stay uncounted after the fix.
        findings = [{"severity": "error"}]
        out = self._summary(findings)
        assert out == (
            "\nHook delivery lint (#7C, WARNING) -- "
            "0 rejected, 0 inert, 0 review:"
        )

    def test_error_finding_mixed_with_others_is_not_miscounted_as_rejected(self) -> None:
        # One of each countable severity plus one error: the error must not
        # inflate the rejected bucket (or any other bucket) -- each count
        # stays at exactly 1, not 2.
        findings = [
            {"severity": "error"},
            {"severity": "rejected"},
            {"severity": "inert"},
            {"severity": "review"},
        ]
        out = self._summary(findings)
        assert out == (
            "\nHook delivery lint (#7C, WARNING) -- "
            "1 rejected, 1 inert, 1 review:"
        )

    def test_empty_findings_list_renders_a_falsy_header(self) -> None:
        # The `integrity` command keeps its existing `if hl:` guard around
        # the new `typer.echo(_hook_lint_summary(hl))` call (the plan's
        # "Shape of the fix"), so the silent-when-clean invariant is really
        # the caller's job. This pins the helper's half of that contract:
        # called on an empty list it must not fabricate a header worth
        # echoing, so `if hl:` and `if _hook_lint_summary(hl):` never
        # disagree about when `integrity` should stay silent.
        assert self._summary([]) == ""
