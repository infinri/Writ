"""Tests for the M0 V1 baseline audit generators."""
from __future__ import annotations

import pytest

from writ_m0.audit import __main__ as audit_main
from writ_m0.audit import cypher, hooks, rules


class TestCypherParser:
    def test_parses_node_with_escapes_and_types(self):
        corpus = cypher.parse_corpus(
            "CREATE (:Rule {_dump_id: 'R-1', statement: 'it\\'s a\\nline', n: 3, x: 1.5, "
            "flag: true, none: null, tags: ['a', 'b']});"
        )
        props = corpus.nodes[0].props
        assert props["statement"] == "it's a\nline"
        assert (props["n"], props["x"], props["flag"], props["none"]) == (3, 1.5, True, None)
        assert props["tags"] == ["a", "b"]

    def test_parses_edge(self):
        corpus = cypher.parse_corpus(
            "MATCH (a {_dump_id: 'A'}), (b {_dump_id: 'B'}) CREATE (a)-[:RELATED_TO]->(b);"
        )
        assert (corpus.edges[0].src, corpus.edges[0].rel, corpus.edges[0].dst) == ("A", "RELATED_TO", "B")

    @pytest.mark.parametrize("line", [
        "MERGE (:Rule {_dump_id: 'X'});",
        "CREATE (:Rule {_dump_id: 'X', a: 'unterminated});",
        "CREATE (:Rule {statement: 'no id'});",
        "CREATE (:Rule {_dump_id: 'X', _dump_id: 'Y'});",
        "CREATE (:Rule {_dump_id: 'X', bad: \\q});",
    ])
    def test_rejects_unknown_or_malformed_statements(self, line):
        with pytest.raises(cypher.CypherFormatError):
            cypher.parse_corpus(line)


class TestHookNormalization:
    def test_write_only_matcher_reports_edit_and_notebook_gaps(self):
        assert hooks.normalized_capabilities("PreToolUse", "Write") == ["file.write"]
        assert hooks.capability_gaps("PreToolUse", "Write") == ["Edit", "NotebookEdit"]

    def test_wildcard_matcher_covers_unknown_tools_without_gap(self):
        assert hooks.normalized_capabilities("PreToolUse", ".*") == ["*"]
        assert hooks.capability_gaps("PreToolUse", ".*") == []

    def test_matcher_uses_full_match(self):
        # "Read" must not select "ReadMany"-style names or substrings.
        assert hooks.matcher_tools("Read") == ["Read"]

    def test_lifecycle_events_map_to_lifecycle_capability(self):
        assert hooks.normalized_capabilities("Stop", "") == ["lifecycle.Stop"]

    def test_logged_deny_with_nonblocking_exit_is_flagged(self):
        source = 'log_gate_decision "x" "deny" "$WHY" "$SID"\nexit 1\n'
        signals = hooks.scan_signals(source)
        reg = hooks.Registration("H99", "Stop", "", 1, "s.sh", None)
        assert hooks.findings(reg, signals, 1) == ["LOGS_DENY_WITHOUT_HOST_BLOCK"]

    def test_comment_lines_are_not_signals(self):
        assert hooks.scan_signals("# emit_deny here\n")["deny"] == []

    def test_post_effect_never_classified_preventive(self):
        signals = hooks.scan_signals("emit_deny x\n")
        assert hooks.coverage_class("PostToolUse", signals) == "post_effect_advisory"
        assert hooks.coverage_class("PreToolUse", signals) == "preventive_candidate"


def _rule(rule_id, statement, **extra):
    props = {"_dump_id": rule_id, "rule_id": rule_id, "statement": statement,
             "domain": "security", "severity": "high", "mandatory": False}
    props.update(extra)
    return cypher.Node("Rule", props)


class TestRuleDisposition:
    def classify(self, *nodes, paths=frozenset({"bin/check.sh"})):
        rows = rules.classify(cypher.Corpus(list(nodes), []), set(paths))
        return {r.rule_id: r.disposition for r in rows}

    def test_precedence(self):
        result = self.classify(
            _rule("A-1", "Validate input."),
            _rule("A-2", "validate   INPUT"),
            _rule("B-1", "A handoff file must be JSON.", mechanical_enforcement_path="bin/check.sh"),
            _rule("C-1", "Hash passwords.", mechanical_enforcement_path="bin/check.sh::fn"),
            _rule("D-1", "Gone.", mechanical_enforcement_path=".claude/hooks/old.sh"),
            _rule("E-1", "Must do X.", mandatory=True),
            _rule("F-1", "Prefer small functions."),
        )
        assert result == {
            "A-1": "advisory", "A-2": "duplicate", "B-1": "obsolete",
            "C-1": "candidate_enforced", "D-1": "unverified", "E-1": "unverified",
            "F-1": "advisory",
        }

    def test_binding_path_extraction(self):
        assert rules._binding_file("bin/run-analysis.sh::analyze") == "bin/run-analysis.sh"
        assert rules._binding_file("bin/run-analysis.sh:78 (PHPStan)") == "bin/run-analysis.sh"
        assert rules._binding_file("a.sh + b.sh") == "a.sh"
        assert rules._binding_file(None) == ""


@pytest.mark.baseline
class TestBaselineArtifacts:
    """Integration: the committed evidence matches the pinned V1 commit."""

    def test_committed_artifacts_are_current(self):
        assert audit_main.main(["check"]) == 0

    def test_documented_baseline_counts_hold(self):
        hook_rows = hooks.build_rows()
        rule_rows, _ = rules.build_rows()
        assert len(hook_rows) == 52
        assert len({r["event"] for r in hook_rows}) == 12
        assert len(rule_rows) == 288
        assert sum(r["mandatory"] == "true" for r in rule_rows) == 32
        assert all(r["review_status"] == "automated_first_pass" for r in rule_rows)

    def test_golden_retrieval_export_preserves_v1_sets(self):
        from writ_m0.audit import golden
        cases = golden.build()["cases"]
        by_expect = {k: sum(1 for c in cases if c["expect"] == k) for k in ("rank", "abstain", "document")}
        assert by_expect == {"rank": 193 + 40, "abstain": 20, "document": 12}
        assert all(c["expected_ids_in_corpus"] for c in cases if c["expect"] == "rank")


@pytest.mark.baseline
class TestRuleReviewBatches:
    """M0-01d preparation: every rule appears exactly once and nothing is pre-reviewed."""

    def test_batches_partition_all_rules_with_blank_review_columns(self):
        import csv
        import io
        from writ_m0.audit import rule_review
        files = rule_review.build()
        rows = [r for name, text in files.items() if name.endswith(".csv")
                for r in csv.DictReader(io.StringIO(text))]
        ids = [r["rule_id"] for r in rows]
        assert len(ids) == 288 and len(set(ids)) == 288
        filled = {"reviewed_disposition", "intent_survives_in_v2", "v2_destination", "enforcement_verified",
                  "conflicts_or_duplicates", "corrections", "reviewer", "reviewed_at", "rationale"}
        assert all(not r[c] for r in rows for c in filled)
        assert all(sum(1 for _ in csv.DictReader(io.StringIO(t))) <= rule_review.MAX_BATCH
                   for n, t in files.items() if n.endswith(".csv"))
