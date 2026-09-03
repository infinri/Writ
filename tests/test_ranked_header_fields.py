"""Pins the ranked-rule header slot: severity always renders, authority renders only
when it is not "human", domain is dropped entirely. See plan.md and capabilities.md at
.claude/plans/dfacff61-23d5-474e-846c-2e2f0f0ea482/ for the full decision record.

THE BUG THIS GUARDS AGAINST: writ/retrieval/pipeline.py:_final_rank already sets
severity/authority/domain on each rule_entry. writ/retrieval/ranking.py:_project_rules
then rebuilds a NEW dict from a per-mode whitelist and drops every field not named in
that whitelist, so the header renders "(?, ?, ?)" no matter what the producer wrote. A
test that asserts on the pipeline's rule_entry (pre-projection) would stay green through
that exact regression, so it is not used as an oracle anywhere in this file. Every test
below either drives the PROJECTION (apply_context_budget / _project_rules /
_summary_with_abstractions) or the full chain through cmd_format, so a stripped field is
visible at the assertion, not hidden behind an earlier-stage check.

Chain pinned by TestChainNodeMetadataToRenderedLine (plan D4):

    rule_metadata (what Neo4j hands over)
      -> RetrievalPipeline.query()            [_final_rank + apply_context_budget]
      -> tag_overlap()                        [the one other transform on the live path]
      -> cmd_format()                         [the rendered header line]

The pipeline is built in-process with MagicMock keyword index, vector store, and
encoder, plus injected rule_metadata (the stub pattern already used at
tests/test_phaseM3_project_query_scope.py:47-75). No Neo4j, no ONNX. Copied locally
per this repo's per-module fixture convention rather than imported across test modules.
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys

import numpy as np
import pytest

from writ.retrieval.ranking import (
    SUMMARY_THRESHOLD,
    STANDARD_THRESHOLD,
    apply_context_budget,
)
from writ.retrieval import prompt_bundle as pb
from writ.session import budget_tracking as bt

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
RANKING_PATH = os.path.join(SKILL_ROOT, "writ", "retrieval", "ranking.py")


def _assert_real_number(value, label):
    """bool is a subclass of int in Python (isinstance(True, int) is True), so a plain
    isinstance(x, (int, float)) check would let a stray True/False satisfy a numeric
    assertion. Guard explicitly wherever a count or score is checked for real-ness."""
    assert isinstance(value, (int, float)) and not isinstance(value, bool), (
        f"{label} must be a real number, got {value!r} ({type(value).__name__})"
    )


# --------------------------------------------------------------------------- #
# Local render harness, mirroring tests/test_ranked_alwayson_dedup.py's _render
# (not imported across modules, per this repo's per-module fixture convention).
# --------------------------------------------------------------------------- #
def _render(monkeypatch, capsys, rules, mode="standard"):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"rules": rules, "mode": mode})))
    bt.cmd_format()
    raw = capsys.readouterr().out
    return pb.split_format(raw)


# --------------------------------------------------------------------------- #
# Local stub pipeline, copied from tests/test_phaseM3_project_query_scope.py:47-75
# per plan.md D4 ("The stub is copied locally into the new module rather than
# imported across test modules").
# --------------------------------------------------------------------------- #
def _stub_pipeline(metadata: dict):
    from unittest.mock import MagicMock

    from writ.retrieval.embeddings import ScoredResult
    from writ.retrieval.pipeline import RetrievalPipeline
    from writ.retrieval.traversal import AdjacencyCache

    ids = list(metadata.keys())
    keyword_stub = MagicMock()
    keyword_stub.search.return_value = [{"rule_id": r, "score": 0.9} for r in ids]
    vector_stub = MagicMock()
    vector_stub.search.return_value = [ScoredResult(rule_id=r, score=0.9) for r in ids]
    encoder_stub = MagicMock()
    encoder_stub.encode.return_value = np.zeros(384, dtype=np.float32)
    return RetrievalPipeline(
        keyword_index=keyword_stub,
        vector_store=vector_stub,
        adjacency_cache=AdjacencyCache(),
        embedding_model=encoder_stub,
        rule_metadata=metadata,
    )


def _node_meta(severity="critical", authority="human", domain="canary-domain-77"):
    """A Rule node, node_type + domain chosen so the pipeline's legacy Stage 1 branch
    (no node_routes -> Rule-only, methodology domains excluded) admits it. See
    plan.md D4: "The metadata must be node_type: Rule with a domain outside
    _METHODOLOGY_EXCLUDE_DOMAINS.\""""
    return {
        "node_type": "Rule",
        "domain": domain,
        "severity": severity,
        "authority": authority,
        "confidence": "production-validated",
        "statement": "The chain fixture provides exactly what the header test needs.",
        "trigger": "When the header-fields chain fixture is used.",
        "project": "writ",
    }


# =============================================================================
# Capabilities 1 and 2: the mode-label population is DERIVED from ranking.py by
# an ast walk, not hardcoded, so a fourth budget mode fails BY NAME instead of
# silently passing three-mode-shaped assertions.
# =============================================================================
def _derive_mode_labels_from_ast() -> set:
    """Every string constant apply_context_budget can return as its mode label:
    both `mode = "..."` assignments and a literal in the returned tuple. Parses
    the ranking.py SOURCE, never imports the module, so this reads the shape of
    the code rather than one run of it."""
    with open(RANKING_PATH) as f:
        src = f.read()
    tree = ast.parse(src, filename=RANKING_PATH)

    func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "apply_context_budget":
            func = node
            break
    assert func is not None, (
        "apply_context_budget not found in writ/retrieval/ranking.py by ast walk: "
        "renamed or moved? every derived-population assertion below is grounded on "
        "finding this function"
    )

    labels: set = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "mode"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    labels.add(node.value.value)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    labels.add(elt.value)
    return labels


def _probe_budget_ladder() -> dict:
    """Run apply_context_budget across a ladder built from SUMMARY_THRESHOLD and
    STANDARD_THRESHOLD (never a hardcoded mode-name list), on a single innocuous
    rule, and return {budget_tokens: mode_label} for each probe. This is the
    population capability-1's per-mode test iterates, and capability-2's
    equality check compares against the ast-derived set."""
    rules = [{
        "rule_id": "PROBE-LADDER-001", "node_type": "Rule", "score": 0.5,
        "severity": "medium", "authority": "human", "domain": "probe",
        "statement": "s.", "trigger": "t.",
    }]
    ladder = [SUMMARY_THRESHOLD - 1, SUMMARY_THRESHOLD, STANDARD_THRESHOLD, STANDARD_THRESHOLD + 1]
    out = {}
    for budget_tokens in ladder:
        _trimmed, mode = apply_context_budget(rules, budget_tokens)
        out[budget_tokens] = mode
    return out


class TestModeLabelDerivation:
    def test_derived_label_set_is_not_empty(self):
        """Anti-vacuity floor (plan.md D4): a derivation that silently returned
        nothing would make every dependent assertion in this module vacuously
        pass. RED today only if the ast walk itself is broken; independent of
        the header-fields bug."""
        derived = _derive_mode_labels_from_ast()
        assert derived, (
            "ast walk over apply_context_budget returned zero mode-label string "
            "constants: the derivation is broken, and every population-based "
            "assertion in this module would otherwise pass vacuously"
        )

    def test_derived_label_set_covers_summary_standard_and_full(self):
        derived = _derive_mode_labels_from_ast()
        assert {"summary", "standard", "full"} <= derived, (
            f"derived mode labels {derived!r} do not cover the three known modes"
        )

    def test_probed_budget_ladder_produces_exactly_the_derived_label_set(self):
        """Mutation this catches (plan.md step 5, capability 2, both arms):
        hardcoding the derived set to a subset (e.g. only "standard") fails the
        coverage assertion above; adding a fourth mode branch reachable at a
        budget_tokens value NOT on this ladder makes the ast-derived set a
        strict superset of what the ladder probes, so this equality fails and
        names the mismatch."""
        derived = _derive_mode_labels_from_ast()
        probed = set(_probe_budget_ladder().values())
        assert probed == derived, (
            f"probed labels {probed!r} != ast-derived labels {derived!r}: a mode "
            "reachable in source is not reachable by this budget ladder, or vice versa"
        )


# =============================================================================
# Capability 1: for every mode label apply_context_budget can return (the
# DERIVED population above, not a hardcoded list), its projected rule entries
# carry severity and authority. This is the exact seam that broke: it asserts
# on the OUTPUT of the projection, never on pipeline._final_rank's rule_entry.
# =============================================================================
class TestHeaderFieldsSurviveProjectionForEveryDerivedMode:
    def _source_rule(self):
        return {
            "rule_id": "PROJ-CARRY-001", "node_type": "Rule", "score": 0.834,
            "authority": "ai-provisional", "severity": "critical",
            "domain": "should-not-reach-any-projected-entry",
            "statement": "s.", "trigger": "t.", "violation": "v.",
            "pass_example": "p.", "rationale": "r.", "relationships": [],
        }

    def test_every_derived_mode_projects_severity_and_authority(self):
        """Mutation this catches (plan.md step 5, capabilities 1 and 3): delete
        `severity` from `_HEADER_FIELDS` in ranking.py. The projected entry loses
        the "severity" key (or its value) for every mode this loop visits, and
        this assertion fails by mode name."""
        ladder_results = _probe_budget_ladder()
        derived = _derive_mode_labels_from_ast()
        assert set(ladder_results.values()) == derived  # population sanity, not a duplicate of TestModeLabelDerivation

        for budget_tokens, mode in ladder_results.items():
            trimmed, actual_mode = apply_context_budget([self._source_rule()], budget_tokens)
            assert actual_mode == mode
            assert trimmed, f"mode {mode!r} (budget_tokens={budget_tokens}) projected zero entries"
            entry = trimmed[0]
            assert entry.get("severity") == "critical", (
                f"mode {mode!r}: projected entry lost 'severity', got {entry!r}"
            )
            assert entry.get("authority") == "ai-provisional", (
                f"mode {mode!r}: projected entry lost 'authority', got {entry!r}"
            )
            assert "domain" not in entry, (
                f"mode {mode!r}: projected entry still carries 'domain' after the drop, got {entry!r}"
            )


# =============================================================================
# Capability 3 (THE chain test). This is the anti-recurrence guard named in
# plan.md D4: it starts at injected node METADATA and ends at the RENDERED
# LINE, through query() -> tag_overlap() -> cmd_format() in that order, so the
# projection-strips-a-field failure mode is visible at the final string, not
# hidden behind a rule_entry-level assertion.
# =============================================================================
class TestChainNodeMetadataToRenderedLine:
    def test_severity_reaches_the_rendered_header_for_a_human_authority_rule(self, monkeypatch, capsys):
        """A rule present only as injected node metadata with severity: critical
        and authority: human reaches the rendered block as
        "[<id>] (critical) score=<score>", authority hidden because human is
        the constant case (plan.md D1). Mutation this catches: delete severity
        from _HEADER_FIELDS in ranking.py (the chain goes back to rendering
        "(?, ?, ?)" or "(?, ?)", never "(critical)")."""
        rule_id = "CHAIN-HUMAN-001"
        pipeline = _stub_pipeline({rule_id: _node_meta(severity="critical", authority="human")})

        response = pipeline.query("critical severity rule", budget_tokens=5000)
        tagged = pb.tag_overlap(response["rules"], set())
        text, _meta = _render(monkeypatch, capsys, tagged, mode=response["mode"])

        assert f"[{rule_id}]" in text, text
        assert f"[{rule_id}] (critical) score=" in text, (
            f"expected the human-authority rule to render '(critical)' with no "
            f"second element; got:\n{text}"
        )
        assert "(?, ?, ?)" not in text
        assert "(?, ?)" not in text

    def test_domain_never_reaches_the_rendered_block_through_the_chain(self, monkeypatch, capsys):
        """The sentinel domain string injected on the node metadata must not
        appear anywhere in the rendered block, end to end through the chain."""
        rule_id = "CHAIN-DOMAIN-001"
        sentinel_domain = "canary-domain-should-never-render-77"
        pipeline = _stub_pipeline({rule_id: _node_meta(domain=sentinel_domain)})

        response = pipeline.query("domain sentinel rule", budget_tokens=5000)
        tagged = pb.tag_overlap(response["rules"], set())
        text, _meta = _render(monkeypatch, capsys, tagged, mode=response["mode"])

        assert f"[{rule_id}]" in text, text
        assert sentinel_domain not in text

    def test_ai_provisional_authority_reaches_the_rendered_block_through_the_chain(self, monkeypatch, capsys):
        """The exception branch, driven end to end: a rule whose node metadata
        carries authority: ai-provisional reaches the rendered block with the
        authority visible next to severity."""
        rule_id = "CHAIN-PROVISIONAL-001"
        pipeline = _stub_pipeline({rule_id: _node_meta(severity="high", authority="ai-provisional")})

        response = pipeline.query("provisional authority rule", budget_tokens=5000)
        tagged = pb.tag_overlap(response["rules"], set())
        text, _meta = _render(monkeypatch, capsys, tagged, mode=response["mode"])

        assert f"[{rule_id}] (high, ai-provisional) score=" in text, text


# =============================================================================
# Capability 4: domain drop, isolated to the formatter (cmd_format never reads
# "domain" once it stops looking, independent of what upstream produced).
# =============================================================================
class TestFormatterNeverRendersDomain:
    def test_a_rule_dict_with_domain_set_renders_no_domain_element(self, monkeypatch, capsys):
        """Mutation this catches (plan.md step 5, capability 4): restore the
        three-field f-string in budget_tracking.py's cmd_format. The sentinel
        domain value would then be found in the rendered block and this
        assertion fails. Fed directly to cmd_format (bypassing the projection)
        so this test is red/green purely on the formatter's own behavior."""
        sentinel_domain = "sentinel-domain-value-should-never-render"
        rule = {
            "rule_id": "FMT-DOMAIN-001", "severity": "high", "authority": "human",
            "domain": sentinel_domain, "score": 0.5,
            "statement": "s.", "trigger": "t.",
        }
        text, _meta = _render(monkeypatch, capsys, [rule])
        assert sentinel_domain not in text


# =============================================================================
# Capability 5: the authority exception, BOTH branches (hard constraint 5). A
# test covering only the common human branch would pass against an
# implementation that never shows authority at all.
# =============================================================================
class TestAuthorityRenderedOnlyAsException:
    def test_human_authority_renders_with_no_second_element(self, monkeypatch, capsys):
        """Mutation this catches (plan.md step 5, capability 5 arm one): render
        authority unconditionally. The human case would gain ", human" and this
        assertion fails."""
        rule = {
            "rule_id": "FMT-AUTH-HUMAN-001", "severity": "high", "authority": "human",
            "score": 0.5, "statement": "s.", "trigger": "t.",
        }
        text, _meta = _render(monkeypatch, capsys, [rule])
        assert "[FMT-AUTH-HUMAN-001] (high) score=" in text, text
        assert ", human" not in text

    def test_non_human_authority_renders_as_a_visible_second_element(self, monkeypatch, capsys):
        """Mutation this catches (plan.md step 5, capability 5 arm two): drop the
        exception branch entirely. The ai-provisional case would lose its
        marker and this assertion fails."""
        rule = {
            "rule_id": "FMT-AUTH-PROVISIONAL-001", "severity": "medium",
            "authority": "ai-provisional", "score": 0.5, "statement": "s.", "trigger": "t.",
        }
        text, _meta = _render(monkeypatch, capsys, [rule])
        assert "[FMT-AUTH-PROVISIONAL-001] (medium, ai-provisional) score=" in text, text


# =============================================================================
# Capability 6: the visible-absence case. Goes through apply_context_budget
# (not a raw dict handed straight to cmd_format) so the mutation described in
# plan.md step 5 (_carry_header_fields defaulting to "" instead of a
# conditional copy) is actually exercised: a raw rule that never declared
# severity or authority in the first place.
# =============================================================================
class TestAbsentFieldsRenderAsVisibleQuestionMarks:
    def test_a_rule_with_neither_field_projects_and_renders_paired_question_marks(self, monkeypatch, capsys):
        """Mutation this catches (plan.md step 5, capability 6): change the copy
        in _carry_header_fields to `entry[f] = rule.get(f, "")`. Because this
        source rule never declared severity/authority, the mutant would set both
        keys present but EMPTY, so the render becomes "(, )" instead of "(?, ?)",
        and this assertion fails. Models the caller plan.md D2 names explicitly:
        "a caller that hands apply_context_budget bare dicts."
        """
        bare_rule = {
            "rule_id": "BARE-NO-HEADER-001", "node_type": "Rule", "score": 0.42,
            "statement": "s.", "trigger": "t.",
            # deliberately no "severity", no "authority", no "domain"
        }
        trimmed, mode = apply_context_budget([bare_rule], STANDARD_THRESHOLD)
        assert mode == "standard"
        text, _meta = _render(monkeypatch, capsys, trimmed, mode=mode)

        assert "[BARE-NO-HEADER-001] (?, ?) score=" in text, text
        assert "()" not in text
        assert "(, )" not in text


# =============================================================================
# Capability 7: the summary-with-abstractions ungrouped-rule fallback carries
# both header fields too: it is a SECOND stripping site (plan.md D2, point 2)
# that must agree with _project_rules independently.
# =============================================================================
class TestSummaryAbstractionsUngroupedFallbackCarriesHeaderFields:
    def test_an_ungrouped_rule_still_carries_severity_and_authority(self, monkeypatch, capsys):
        """Mutation this catches (plan.md step 5, capability 7): remove the
        _carry_header_fields call from the ungrouped fallback branch in
        _summary_with_abstractions. The ungrouped rule would render "(?, ?)"
        instead of its real severity/authority and this assertion fails."""
        ungrouped_rule = {
            "rule_id": "UNGROUPED-001", "node_type": "Rule", "score": 0.61,
            "severity": "low", "authority": "ai-provisional",
            "domain": "should-not-reach-the-render",
            "statement": "s.", "trigger": "t.",
        }
        # An abstraction that covers a DIFFERENT rule id, so UNGROUPED-001 falls
        # into the "elif not abst" branch of _summary_with_abstractions.
        abstractions = [{
            "abstraction_id": "ABS-OTHER-001", "summary": "unrelated summary",
            "rule_ids": ["SOME-OTHER-RULE-999"], "domain": "other", "compression_ratio": 2.0,
        }]
        trimmed, mode = apply_context_budget(
            [ungrouped_rule], SUMMARY_THRESHOLD - 1, abstractions=abstractions,
        )
        assert mode == "summary"
        entries = [e for e in trimmed if e.get("rule_id") == "UNGROUPED-001"]
        assert entries, f"UNGROUPED-001 not found ungrouped in {trimmed!r}"
        entry = entries[0]
        assert entry.get("severity") == "low", entry
        assert entry.get("authority") == "ai-provisional", entry
        assert "domain" not in entry, entry

        text, _meta = _render(monkeypatch, capsys, trimmed, mode=mode)
        assert "[UNGROUPED-001] (low, ai-provisional) score=" in text, text


# =============================================================================
# Capability 8: the [ABSTRACT: ...] line keeps its domain, unaffected by the
# rule-header change. This should already be true today (a regression guard,
# not part of the RED set at plan.md step 1); it fails only if the domain
# drop is mistakenly applied to the abstraction branch too.
# =============================================================================
class TestAbstractionLineDomainUnaffected:
    def test_abstract_line_keeps_its_domain(self, monkeypatch, capsys):
        """Mutation this catches (plan.md step 5, capability 8): apply the
        domain drop to the [ABSTRACT: ...] branch as well. The abstraction line
        would lose its domain and this assertion fails."""
        covered_rule = {
            "rule_id": "COVERED-001", "node_type": "Rule", "score": 0.71,
            "severity": "high", "authority": "human", "domain": "irrelevant-to-this-branch",
            "statement": "s.", "trigger": "t.",
        }
        sentinel_domain = "abstract-line-domain-sentinel-42"
        abstractions = [{
            "abstraction_id": "ABS-COVER-001", "summary": "covers exactly one rule",
            "rule_ids": ["COVERED-001"], "domain": sentinel_domain, "compression_ratio": 3.0,
        }]
        trimmed, mode = apply_context_budget(
            [covered_rule], SUMMARY_THRESHOLD - 1, abstractions=abstractions,
        )
        assert mode == "summary"
        abst_entries = [e for e in trimmed if e.get("abstraction_id") == "ABS-COVER-001"]
        assert abst_entries, f"abstraction entry not found in {trimmed!r}"
        abst_entry = abst_entries[0]

        covered = len(abst_entry.get("rule_ids", []))
        _assert_real_number(covered, "covered-rule count")
        assert covered == 1

        text, _meta = _render(monkeypatch, capsys, trimmed, mode=mode)
        assert f"[ABSTRACT: ABS-COVER-001] (covers 1 rules, {sentinel_domain})" in text, text
