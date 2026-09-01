"""Tests for the ranked/always-on field-dedup render behavior (Cycle A, part 1).

writ/session/budget_tracking.py:cmd_format renders a ranked rule's WHEN/RULE/VIOLATION/
CORRECT fields. Part 1 adds a per-rule `already_injected: true` flag (set by a new pure
helper, writ.retrieval.prompt_bundle.tag_overlap, for rules already delivered by this
turn's always-on channel) that cmd_format must honor in standard/full mode: omit
WHEN/RULE, keep VIOLATION/CORRECT, and add a one-line pointer to the always-active block.
Summary mode ignores the flag entirely (a suppressed entry there would carry zero
content, per plan.md's mode-scoping decision).

RED until cmd_format reads `already_injected`. Most tests here build the flag onto rule
dicts BY HAND, bypassing tag_overlap (covered separately in tests/test_prompt_bundle.py),
so a render defect and a tagging defect fail independently. The final class
(TestNoOverlapIsByteIdenticalToPreChangeRender) is the one exception: it exercises
tag_overlap feeding cmd_format end to end, because that specific capability (no overlap
-> byte-identical to today) is a claim about the two working together.

Covers plan capabilities:
- a ranked hit NOT in the always-on set renders unchanged (WHEN/RULE/VIOLATION/CORRECT)
- a ranked hit IN the always-on set omits WHEN/RULE, keeps VIOLATION/CORRECT, gains a
  pointer to the always-active block
- summary mode is unaffected by the flag
- a tagged rule with empty violation+pass_example still renders header+pointer; the
  "N rules" header count and WRIT_META rule_ids are unchanged by tagging
- the rule-objects cache (--add-rule-objects / extract_rule_objects) still carries
  trigger+statement for a tagged rule -- tagging is additive, never a strip
- the overlap that produces the flag differs by session mode (ENF-PROC-DEBUG-001 is
  always-on in work/debug, absent elsewhere) -- pinned as a render-layer consequence:
  same rule content, flag present vs absent, different render
- when there is no overlap, the ranked block renders byte-identical to today
- writ/server/routes/query.py's call into tag_overlap(...) receives an overlap argument
  that STRUCTURALLY traces back to always_on_rule_ids(...) (the RENDERED ids), not the
  raw /always-on response -- an AST assertion on the call site, not a substring grep
  (see TestOverlapArgumentIsDerivedFromRenderedIds below for why a grep is not enough)

Residual gap (documented, not asserted away): the AST check below proves the SOURCE
calls the right function at the right argument position; it cannot prove a LIVE request
actually produces the correct runtime overlap (that would need a live daemon plus a
graph fixture engineered to have an eligible-but-unrenderable always-on rule, which is
too heavy and too risky to the shared corpus for a unit test -- see
project_graph_wipe_incident_2026_08_05 in memory). The equivalent invariant IS pinned at
the unit level in tests/test_injection_footprint.py::TestComputeOverlap, which the
runtime tagging path is expected to mirror, and the AST check pins that query.py wires
to that same helper rather than reimplementing the filter.
"""
from __future__ import annotations

import ast
import importlib
import io
import json
import os
import sys

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _imp(name):
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module(name)


def _rule(
    rule_id="ENF-TEST-DEDUP-001",
    trigger="When the dedup fixture is used.",
    statement="The fixture provides exactly what the render test needs.",
    violation="agent repeats trigger and statement verbatim",
    pass_example="agent points at the block instead of repeating it",
    severity="high",
    authority="human",
    domain="testing",
    score=0.9,
    already_injected=None,
):
    d = {
        "rule_id": rule_id, "trigger": trigger, "statement": statement,
        "violation": violation, "pass_example": pass_example,
        "severity": severity, "authority": authority, "domain": domain,
        "score": score,
    }
    if already_injected is not None:
        d["already_injected"] = already_injected
    return d


def _render(monkeypatch, capsys, rules, mode="standard"):
    bt = _imp("writ.session.budget_tracking")
    pb = _imp("writ.retrieval.prompt_bundle")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"rules": rules, "mode": mode})))
    bt.cmd_format()
    raw = capsys.readouterr().out
    text, meta = pb.split_format(raw)
    return text, meta


# --------------------------------------------------------------------------- #
# Baseline: a ranked hit NOT in this turn's always-on set renders unchanged.
# These pass against TODAY's cmd_format (an extra unread key is a no-op) --
# they are the regression guard that must stay green after the change lands.
# --------------------------------------------------------------------------- #
class TestUntaggedRuleUnchanged:
    def test_untagged_rule_has_when_rule_violation_and_correct(self, monkeypatch, capsys):
        rule = _rule()
        text, _ = _render(monkeypatch, capsys, [rule])
        assert f"WHEN: {rule['trigger']}" in text
        assert f"RULE: {rule['statement']}" in text
        assert f"VIOLATION: {rule['violation']}" in text
        assert f"CORRECT: {rule['pass_example']}" in text

    def test_absent_flag_is_treated_as_untagged(self, monkeypatch, capsys):
        rule = _rule(already_injected=None)
        assert "already_injected" not in rule
        text, _ = _render(monkeypatch, capsys, [rule])
        assert "WHEN:" in text and "RULE:" in text

    def test_false_flag_is_treated_as_untagged(self, monkeypatch, capsys):
        rule = _rule(already_injected=False)
        text, _ = _render(monkeypatch, capsys, [rule])
        assert "WHEN:" in text and "RULE:" in text


# --------------------------------------------------------------------------- #
# A ranked hit IN this turn's always-on set: suppress WHEN/RULE, keep
# VIOLATION/CORRECT, add a pointer. RED until cmd_format implements this.
# --------------------------------------------------------------------------- #
class TestTaggedRuleSuppression:
    def test_tagged_rule_omits_when_and_rule_labels_and_their_text(self, monkeypatch, capsys):
        rule = _rule(already_injected=True)
        text, _ = _render(monkeypatch, capsys, [rule])
        assert "WHEN:" not in text
        assert "RULE:" not in text
        # The raw trigger/statement text must not leak unlabeled either -- that
        # would just move the repetition, not remove it.
        assert rule["trigger"] not in text
        assert rule["statement"] not in text

    def test_tagged_rule_keeps_violation_and_correct(self, monkeypatch, capsys):
        rule = _rule(already_injected=True)
        text, _ = _render(monkeypatch, capsys, [rule])
        assert f"VIOLATION: {rule['violation']}" in text
        assert f"CORRECT: {rule['pass_example']}" in text

    def test_tagged_rule_keeps_its_header_line(self, monkeypatch, capsys):
        rule = _rule(rule_id="ENF-HEADER-001", already_injected=True)
        text, _ = _render(monkeypatch, capsys, [rule])
        assert "[ENF-HEADER-001]" in text
        assert "score=" in text

    def test_tagged_rule_includes_a_pointer_to_the_always_active_block(self, monkeypatch, capsys):
        rule = _rule(already_injected=True)
        pb = _imp("writ.retrieval.prompt_bundle")
        # Sanity: this really is the always-on block's banner text, not a guess.
        block, _, _ = pb.render_always_on(
            {"total_tokens": 0, "rules": [{"rule_id": rule["rule_id"], "trigger": "t", "statement": "s"}]}
        )
        assert "ALWAYS-ACTIVE" in block

        text, _ = _render(monkeypatch, capsys, [rule])
        assert "ALWAYS-ACTIVE" in text
        assert rule["rule_id"] in text  # the pointer names the rule, not a generic note

    def test_tagged_rendering_is_shorter_than_untagged(self, monkeypatch, capsys):
        rule = _rule(rule_id="ENF-SHORT-001")
        untagged_text, _ = _render(monkeypatch, capsys, [dict(rule)])
        tagged = dict(rule)
        tagged["already_injected"] = True
        tagged_text, _ = _render(monkeypatch, capsys, [tagged])
        assert len(untagged_text.encode("utf-8")) > len(tagged_text.encode("utf-8"))


# --------------------------------------------------------------------------- #
# Summary mode: unaffected by the flag (plan's mode-scoping decision -- a
# suppressed entry in summary mode would carry zero content).
# --------------------------------------------------------------------------- #
class TestSummaryModeUnaffected:
    def test_summary_mode_ignores_the_flag(self, monkeypatch, capsys):
        rule = _rule(already_injected=True)
        text, _ = _render(monkeypatch, capsys, [rule], mode="summary")
        assert f"WHEN: {rule['trigger']}" in text
        assert f"RULE: {rule['statement']}" in text

    def test_summary_mode_tagged_and_untagged_are_byte_identical(self, monkeypatch, capsys):
        rule = _rule(rule_id="ENF-SUMMARY-001")
        untagged_text, _ = _render(monkeypatch, capsys, [dict(rule)], mode="summary")
        tagged = dict(rule)
        tagged["already_injected"] = True
        tagged_text, _ = _render(monkeypatch, capsys, [tagged], mode="summary")
        assert untagged_text == tagged_text


# --------------------------------------------------------------------------- #
# The empty-body boundary: no entry is ever dropped.
# --------------------------------------------------------------------------- #
class TestEmptyBodyBoundary:
    def test_tagged_rule_with_empty_body_still_has_header_and_pointer(self, monkeypatch, capsys):
        rule = _rule(violation="", pass_example="", already_injected=True)
        text, _ = _render(monkeypatch, capsys, [rule])
        assert f"[{rule['rule_id']}]" in text  # not dropped
        assert "ALWAYS-ACTIVE" in text  # a pointer, not a bare "[ID] score=..." stub
        # VIOLATION:/CORRECT: absence here is inherited from cmd_format's PRE-EXISTING
        # `if violation:` / `if pass_example:` guards (writ/session/budget_tracking.py),
        # not from the new tagging logic -- it would hold identically whether tagging
        # is implemented correctly, implemented with an over-suppression bug that
        # happens not to manifest on empty fields, or not implemented at all. Folded
        # in here as a supporting assertion rather than kept as its own test, so it is
        # not mistaken for a discriminating regression guard on the new capability
        # (the over-suppression bug IS caught, on non-empty fields, by
        # TestTaggedRuleSuppression.test_tagged_rule_keeps_violation_and_correct).
        assert "VIOLATION:" not in text
        assert "CORRECT:" not in text

    def test_header_rule_count_is_unchanged_by_tagging(self, monkeypatch, capsys):
        untagged = _rule(rule_id="A-001")
        tagged = _rule(rule_id="B-001", already_injected=True)
        text, _ = _render(monkeypatch, capsys, [untagged, tagged])
        assert "--- WRIT RULES (2 rules, standard mode) ---" in text

    def test_writ_meta_rule_ids_include_the_tagged_rule(self, monkeypatch, capsys):
        rule = _rule(rule_id="ENF-META-001", already_injected=True)
        _, meta = _render(monkeypatch, capsys, [rule])
        assert "ENF-META-001" in meta["rule_ids"]


# --------------------------------------------------------------------------- #
# Constraint 1: tagging is additive, never a strip. extract_rule_objects (the
# --add-rule-objects cache the compliance-matching path reads, query.py:284)
# must still carry every field for a tagged rule even though the RENDERED text
# omits two of them.
# --------------------------------------------------------------------------- #
class TestAdditiveNotDestructive:
    def test_rule_objects_cache_keeps_trigger_and_statement_for_a_tagged_rule(self, monkeypatch, capsys):
        pb = _imp("writ.retrieval.prompt_bundle")
        rule = _rule(rule_id="ENF-ADDITIVE-001", already_injected=True)
        qresp = {"rules": [rule], "mode": "standard"}

        text, _ = _render(monkeypatch, capsys, [rule])
        assert "WHEN:" not in text  # the render really did omit it

        objects = pb.extract_rule_objects(qresp)
        obj = next(o for o in objects if o["rule_id"] == "ENF-ADDITIVE-001")
        assert obj["trigger"] == rule["trigger"]
        assert obj["statement"] == rule["statement"]
        assert obj["trigger"] and obj["statement"]  # non-empty, not just present as keys

    def test_tagging_does_not_disturb_severity_authority_domain_or_score(self, monkeypatch, capsys):
        rule = _rule(rule_id="ENF-FIELDS-001", severity="critical", authority="human",
                     domain="testing", score=0.77, already_injected=True)
        text, _ = _render(monkeypatch, capsys, [rule])
        assert "[ENF-FIELDS-001] (critical, human, testing) score=0.770" in text


# --------------------------------------------------------------------------- #
# Constraint 3: mode scoping. ENF-PROC-DEBUG-001 (domain: process, always_on:
# true, mandatory: false, per bible/methodology/ENF-PROC-DEBUG-001.md) is in
# the always-on block in work/debug and stripped from it elsewhere
# (writ/server/routes/query.py:670, _ALWAYS_ON_PROCESS_MODES = {"work",
# "debug"}). The flag this cycle adds is set upstream from that same per-mode
# overlap, so the SAME ranked-rule content must render differently only
# because the flag differs -- pinned here at the render layer, independent of
# how the flag got set upstream.
# --------------------------------------------------------------------------- #
class TestModeScopingConsequenceAtRender:
    _RULE_ID = "ENF-PROC-DEBUG-001"

    def _debug_rule(self, **overrides):
        base = _rule(
            rule_id=self._RULE_ID,
            domain="process",
            violation="agent guesses a fix without runtime evidence",
            pass_example="agent cites a traceback line before proposing a fix",
        )
        base.update(overrides)
        return base

    def test_tagged_in_work_or_debug_scope_is_suppressed(self, monkeypatch, capsys):
        rule = self._debug_rule(already_injected=True)
        text, _ = _render(monkeypatch, capsys, [rule])
        assert "WHEN:" not in text and "RULE:" not in text
        assert "VIOLATION:" in text and "CORRECT:" in text

    def test_untagged_outside_work_or_debug_scope_is_unchanged(self, monkeypatch, capsys):
        # Outside work/debug the process-domain strip removes ENF-PROC-DEBUG-001
        # from /always-on entirely, so the overlap set never contains it and this
        # ranked hit is never flagged.
        rule = self._debug_rule(already_injected=None)
        text, _ = _render(monkeypatch, capsys, [rule])
        assert f"WHEN: {rule['trigger']}" in text
        assert f"RULE: {rule['statement']}" in text


# --------------------------------------------------------------------------- #
# Capability: when there is no overlap, the ranked block renders byte-identical
# to the pre-change render. This is the one class that exercises tag_overlap
# and cmd_format together, because the claim is about the two working in
# concert on the "nothing to dedup" path.
# --------------------------------------------------------------------------- #
class TestNoOverlapIsByteIdenticalToPreChangeRender:
    def test_tag_overlap_with_empty_overlap_renders_identically_to_untouched_rules(self, monkeypatch, capsys):
        pb = _imp("writ.retrieval.prompt_bundle")
        rules = [_rule(rule_id="A-001"), _rule(rule_id="B-001", trigger="When B.", statement="Do B.")]

        baseline_text, baseline_meta = _render(monkeypatch, capsys, [dict(r) for r in rules])

        tagged_rules = pb.tag_overlap(rules, set())
        tagged_text, tagged_meta = _render(monkeypatch, capsys, tagged_rules)

        assert baseline_text == tagged_text
        assert baseline_meta == tagged_meta


# --------------------------------------------------------------------------- #
# AST helpers for the wiring assertion below. A substring grep for
# "always_on_rule_ids(" is not enough: that name already appears in query.py for
# an unrelated, pre-existing purpose (recording ao_ids for citation validation),
# so a grep would pass even if the NEW overlap argument used something else
# entirely. These walk the actual call site's argument expression instead.
#
# This is a best-effort STATIC approximation, not a real data-flow analysis: it
# resolves a bare-name argument to the last textually-preceding assignment to
# that name within the smallest enclosing function, and does not reason about
# branches, loops, or reassignment order across control flow. That is enough to
# answer "what expression feeds this call" for the straight-line style this
# codebase's route handlers use; it is not a substitute for a real type/data-flow
# checker.
# --------------------------------------------------------------------------- #
def _find_calls(tree, func_name):
    """Every ast.Call whose callee is the bare name `func_name` (tag_overlap(...))
    or an attribute access ending in that name (pb.tag_overlap(...))."""
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == func_name:
                calls.append(node)
            elif isinstance(fn, ast.Attribute) and fn.attr == func_name:
                calls.append(node)
    return calls


def _expr_is_derived_from(node, target_func_name):
    """True if `node` is a call to target_func_name, or a call that wraps a call to
    target_func_name in one of its (positional) arguments -- e.g. set(x) or
    sorted(list(x)) where x is target_func_name(...)."""
    if isinstance(node, ast.Call):
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
        if name == target_func_name:
            return True
        for arg in node.args:
            if _expr_is_derived_from(arg, target_func_name):
                return True
    return False


def _enclosing_function(tree, lineno):
    """The smallest (most-nested) function/async-function containing `lineno`."""
    enclosing = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            if node.lineno <= lineno <= end:
                if enclosing is None or node.lineno > enclosing.lineno:
                    enclosing = node
    return enclosing


def _resolve_name_within_function(func_node, name, before_lineno):
    """The RHS of the LAST assignment to `name` textually before `before_lineno`
    within func_node's body, or None if no such assignment is found."""
    last_value = None
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign) and node.lineno < before_lineno:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    last_value = node.value
    return last_value


def _call_traces_to(tree, call, target_func_name):
    """Does at least one of `call`'s arguments (positional beyond the first, or any
    keyword) trace back to target_func_name, either directly or via one level of
    bare-name assignment resolution in the enclosing function?"""
    candidates = list(call.args[1:]) + [kw.value for kw in call.keywords]
    for arg in candidates:
        if _expr_is_derived_from(arg, target_func_name):
            return True
        if isinstance(arg, ast.Name):
            func = _enclosing_function(tree, call.lineno)
            if func is not None:
                resolved = _resolve_name_within_function(func, arg.id, call.lineno)
                if resolved is not None and _expr_is_derived_from(resolved, target_func_name):
                    return True
    return False


# --------------------------------------------------------------------------- #
# Mutation-style self-check of the detector above: it must accept a call site
# that DOES derive from always_on_rule_ids and reject one that does not, using
# synthetic source (never the real query.py), or it is a check against nothing.
# --------------------------------------------------------------------------- #
class TestOverlapArgumentDetectorItself:
    def _parse_and_find(self, src):
        tree = ast.parse(src)
        return tree, _find_calls(tree, "tag_overlap")

    def test_accepts_a_call_derived_via_an_intermediate_variable(self):
        src = (
            "def handler(ao_json, rules):\n"
            "    ids = always_on_rule_ids(ao_json)\n"
            "    return tag_overlap(rules, ids)\n"
        )
        tree, calls = self._parse_and_find(src)
        assert len(calls) == 1
        assert _call_traces_to(tree, calls[0], "always_on_rule_ids")

    def test_rejects_a_call_derived_from_the_raw_eligible_list(self):
        src = (
            "def handler(ao_json, rules):\n"
            "    ids = [r['rule_id'] for r in ao_json.get('rules', [])]\n"
            "    return tag_overlap(rules, ids)\n"
        )
        tree, calls = self._parse_and_find(src)
        assert len(calls) == 1
        assert not _call_traces_to(tree, calls[0], "always_on_rule_ids")

    def test_accepts_a_direct_inline_call_with_no_intermediate_variable(self):
        src = (
            "def handler(ao_json, rules):\n"
            "    return tag_overlap(rules, always_on_rule_ids(ao_json))\n"
        )
        tree, calls = self._parse_and_find(src)
        assert _call_traces_to(tree, calls[0], "always_on_rule_ids")

    def test_accepts_a_set_wrapped_call(self):
        src = (
            "def handler(ao_json, rules):\n"
            "    return tag_overlap(rules, set(always_on_rule_ids(ao_json)))\n"
        )
        tree, calls = self._parse_and_find(src)
        assert _call_traces_to(tree, calls[0], "always_on_rule_ids")

    def test_rejects_a_call_with_no_overlap_argument_at_all(self):
        src = (
            "def handler(rules):\n"
            "    return tag_overlap(rules)\n"
        )
        tree, calls = self._parse_and_find(src)
        assert not _call_traces_to(tree, calls[0], "always_on_rule_ids")

    def test_no_calls_found_is_reported_as_an_empty_list_not_a_crash(self):
        tree, calls = self._parse_and_find("def handler(): pass\n")
        assert calls == []


# --------------------------------------------------------------------------- #
# Constraint 2 (wiring): the overlap argument /prompt-bundle hands to
# tag_overlap(...) must trace back to always_on_rule_ids(...) (the RENDERED
# ids), never to the raw /always-on response's `rules` list. This is the
# constraint whose failure mode is invisible everywhere else: if the overlap
# came from eligible-but-unrendered ids, a rule's text would be suppressed AND
# the pointer would name a block that never contained it -- no exception, no
# missing field, no visible symptom. Nothing else in this suite catches it.
#
# RED until writ/server/routes/query.py actually calls tag_overlap(...).
# --------------------------------------------------------------------------- #
class TestOverlapArgumentIsDerivedFromRenderedIds:
    def test_tag_overlap_call_site_traces_to_always_on_rule_ids(self):
        query_py = os.path.join(SKILL_ROOT, "writ", "server", "routes", "query.py")
        with open(query_py) as f:
            src = f.read()
        tree = ast.parse(src, filename=query_py)

        calls = _find_calls(tree, "tag_overlap")
        assert calls, (
            "no call to tag_overlap(...) found in writ/server/routes/query.py -- "
            "part 1's tagging call has not been wired into /prompt-bundle yet"
        )

        for call in calls:
            assert _call_traces_to(tree, call, "always_on_rule_ids"), (
                f"tag_overlap call at query.py:{call.lineno} does not appear to "
                "receive an argument derived from always_on_rule_ids(...) (the "
                "RENDERED always-on ids). If the overlap set is built from the raw "
                "/always-on response's `rules` list instead, a rule dropped by the "
                "renderable filter would be suppressed in the ranked channel and "
                "pointed at a block that never contained it."
            )
