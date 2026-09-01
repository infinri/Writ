"""Tests for writ/analysis/injection_footprint.py (Cycle A: the measurement instrument).

RED until writ/analysis/injection_footprint.py + the `injection-footprint` CLI command
exist. Mirrors writ/analysis/corpus_footprint.py's shape (pure functions + a thin Typer
wrapper, canary on empty/unreachable input, a `basis` label on every number) but measures
the per-turn RENDERED injection, not the static corpus.

This file pins ONLY the instrument's pure functions, per the plan: per-field byte
accounting, the overlap-set computation, and the daemon-unreachable error path. The
before/after byte deltas themselves are an (operational) capability -- measured by hand
with `writ injection-footprint` against a live daemon and a real corpus, recorded in the
commit message, never asserted in a test (a literal byte pin on live corpus text is the
count-pin trap; see plan.md "How the byte delta is measured, not asserted").

Assumed contract (this test file DEFINES it; the implementer may rename, in which case
this file is the spec to update against):

    InjectionFootprintError(Exception)
        Raised for a daemon-unreachable or malformed-response canary. Mirrors
        CorpusFootprintError's exit-2 CLI convention.

    measure_ranked_channel(qresp: dict) -> dict
        Renders qresp (the exact shape /query returns: {"rules": [...], "mode": ...})
        through the SAME renderer the request path uses (writ.session.budget_tracking
        .cmd_format, which server._run_cmd_format_locked wraps with a stdin/stdout swap
        for the live route -- the two produce byte-identical text, so cmd_format is used
        directly here as the lighter-weight equivalent). Returns:
            {"block_bytes": int, "rule_count": int, "rule_ids": [...],
             "per_rule": {rule_id: {"total_bytes": int,
                                     "fields": {"trigger": int, "statement": int,
                                                "violation": int, "pass_example": int}}},
             "basis": str}
        Each field's byte count is measured by DIFFING two production-rendered outputs
        (the rule as given, and the rule with that one field blanked) -- never a raw
        len() of the field string, which would miss the field's label and newline.

    measure_always_on_channel(ao_json: dict) -> dict
        Renders ao_json through writ.retrieval.prompt_bundle.render_always_on (the SAME
        renderer /prompt-bundle's channel 2 uses). Returns:
            {"block_bytes": int, "rule_count": int, "rendered_rule_ids": [...],
             "per_rule_bytes": {rule_id: int}, "basis": str}
        per_rule_bytes[rule_id] is measured by re-rendering a SINGLETON ao_json
        containing just that rule through the SAME render_always_on call.

    compute_overlap(ao_json: dict, qresp: dict) -> set[str]
        The intersection of the RENDERED always-on ids (always_on_rule_ids(ao_json),
        which already excludes a rule dropped by the renderable filter) with the ranked
        rule ids in qresp. Never a hardcoded id list.

    build_report(base_url: str, mode: str, probe_prompt: str) -> dict
        The IO-performing orchestrator: GET {base_url}/always-on, POST {base_url}/query
        (no session_id), then the two measure_* functions plus compute_overlap. Raises
        InjectionFootprintError, naming what failed, on any connection failure -- never
        returns a zero-byte report as a stand-in for "the daemon is down."
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sys

import pytest

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _imp(name):
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module(name)


# ---------------------------------------------------------------------------
# Minimal rule-dict factory (TEST-FIXTURE-001 / TEST-FIXTURE-002): only the
# fields cmd_format and render_always_on actually read.
# ---------------------------------------------------------------------------
def _rule(
    rule_id="ENF-TEST-001",
    trigger="When the probe fixture is used.",
    statement="The fixture measures exactly the byte delta a test needs.",
    violation="",
    pass_example="",
    severity="high",
    authority="human",
    domain="testing",
    score=0.9,
):
    return {
        "rule_id": rule_id, "trigger": trigger, "statement": statement,
        "violation": violation, "pass_example": pass_example,
        "severity": severity, "authority": authority, "domain": domain,
        "score": score,
    }


def _cmd_format_text(monkeypatch, capsys, rules, mode="standard"):
    """Ground truth: the SAME renderer the live route uses, called directly (no
    stdin/stdout swap needed here since we own the process)."""
    bt = _imp("writ.session.budget_tracking")
    pb = _imp("writ.retrieval.prompt_bundle")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"rules": rules, "mode": mode})))
    bt.cmd_format()
    raw = capsys.readouterr().out
    text, _meta = pb.split_format(raw)
    return text


# --------------------------------------------------------------------------- #
# Per-field byte accounting (ranked channel)
# --------------------------------------------------------------------------- #
class TestPerFieldByteAccounting:
    @pytest.mark.parametrize("field", ["trigger", "statement", "violation", "pass_example"])
    def test_field_bytes_equal_a_diff_of_the_production_renderer(self, monkeypatch, capsys, field):
        ifp = _imp("writ.analysis.injection_footprint")
        full = _rule(
            violation="agent guesses a byte count",
            pass_example="agent measures the byte count",
        )
        without_field = dict(full)
        without_field[field] = ""

        text_full = _cmd_format_text(monkeypatch, capsys, [full], "standard")
        text_without = _cmd_format_text(monkeypatch, capsys, [without_field], "standard")
        expected_diff = len(text_full.encode("utf-8")) - len(text_without.encode("utf-8"))
        assert expected_diff > 0, "fixture field must contribute nonzero bytes or this pin is vacuous"

        measured = ifp.measure_ranked_channel({"rules": [full], "mode": "standard"})
        actual = measured["per_rule"][full["rule_id"]]["fields"][field]
        assert actual == expected_diff

    def test_empty_field_contributes_zero_bytes_but_sibling_field_does_not(self, monkeypatch, capsys):
        ifp = _imp("writ.analysis.injection_footprint")
        rule = _rule(violation="", pass_example="some real content")
        measured = ifp.measure_ranked_channel({"rules": [rule], "mode": "standard"})
        fields = measured["per_rule"][rule["rule_id"]]["fields"]
        assert fields["violation"] == 0
        # Guards an accountant that always returns 0 (which would pass the line above
        # for free): the non-empty sibling field must be measured as non-zero.
        assert fields["pass_example"] > 0

    def test_total_bytes_exceeds_the_sum_of_field_bytes(self):
        """total_bytes includes the header line and field labels (WHEN:, VIOLATION:,
        ...), so it must exceed the raw sum of field contributions whenever any field
        is non-empty -- otherwise the accountant is just summing field lengths, which
        is the reimplementation this instrument must not be."""
        ifp = _imp("writ.analysis.injection_footprint")
        rule = _rule(violation="x", pass_example="y")
        measured = ifp.measure_ranked_channel({"rules": [rule], "mode": "standard"})
        entry = measured["per_rule"][rule["rule_id"]]
        assert entry["total_bytes"] > sum(entry["fields"].values()) > 0

    def test_block_bytes_equal_the_production_render_length(self, monkeypatch, capsys):
        ifp = _imp("writ.analysis.injection_footprint")
        rules = [_rule(rule_id="A-001"), _rule(rule_id="B-001", trigger="When B.", statement="Do B.")]
        expected_text = _cmd_format_text(monkeypatch, capsys, rules, "standard")
        measured = ifp.measure_ranked_channel({"rules": rules, "mode": "standard"})
        assert measured["block_bytes"] > 0
        assert measured["block_bytes"] == len(expected_text.encode("utf-8"))

    def test_rule_ids_present_in_bundle_order(self):
        ifp = _imp("writ.analysis.injection_footprint")
        rules = [_rule(rule_id="A-001"), _rule(rule_id="B-001")]
        measured = ifp.measure_ranked_channel({"rules": rules, "mode": "standard"})
        assert measured["rule_ids"] == ["A-001", "B-001"]
        assert measured["rule_count"] == 2

    def test_no_rules_yields_zero_bytes_not_a_crash(self):
        ifp = _imp("writ.analysis.injection_footprint")
        measured = ifp.measure_ranked_channel({"rules": [], "mode": "standard"})
        assert measured["block_bytes"] == 0
        assert measured["rule_ids"] == []


# --------------------------------------------------------------------------- #
# Per-rule byte accounting (always-on channel) -- must use render_always_on,
# never a reimplementation.
# --------------------------------------------------------------------------- #
class TestAlwaysOnChannelByteAccounting:
    def test_per_rule_bytes_equal_a_singleton_production_render(self):
        ifp = _imp("writ.analysis.injection_footprint")
        pb = _imp("writ.retrieval.prompt_bundle")
        rule = {"rule_id": "ENF-TEST-002", "trigger": "when probing", "statement": "measure it"}
        ao_json = {"total_tokens": 10, "rules": [rule]}

        singleton_block, _, _ = pb.render_always_on({"total_tokens": 0, "rules": [rule]})
        expected = len(singleton_block.encode("utf-8"))
        assert expected > 0

        measured = ifp.measure_always_on_channel(ao_json)
        assert measured["per_rule_bytes"]["ENF-TEST-002"] == expected

    def test_block_bytes_equal_the_production_render_length(self):
        ifp = _imp("writ.analysis.injection_footprint")
        pb = _imp("writ.retrieval.prompt_bundle")
        ao_json = {"total_tokens": 20, "rules": [
            {"rule_id": "A-001", "trigger": "t1", "statement": "s1"},
            {"rule_id": "B-001", "trigger": "t2", "statement": "s2"},
        ]}
        expected_block, _, _ = pb.render_always_on(ao_json)
        measured = ifp.measure_always_on_channel(ao_json)
        assert measured["block_bytes"] > 0
        assert measured["block_bytes"] == len(expected_block.encode("utf-8"))

    def test_no_renderable_rules_is_zero_not_a_crash(self):
        ifp = _imp("writ.analysis.injection_footprint")
        measured = ifp.measure_always_on_channel({"total_tokens": 0, "rules": []})
        assert measured["block_bytes"] == 0
        assert measured["rendered_rule_ids"] == []

    def test_rule_dropped_by_the_renderable_filter_is_excluded_from_the_count(self):
        """Mirrors _renderable_always_on: a rule missing trigger/statement never
        reaches the block, so the instrument's own per-rule map must not invent an
        entry for it."""
        ifp = _imp("writ.analysis.injection_footprint")
        ao_json = {"total_tokens": 0, "rules": [
            {"rule_id": "INCOMPLETE-001", "trigger": "", "statement": "s"},
            {"rule_id": "COMPLETE-001", "trigger": "t", "statement": "s"},
        ]}
        measured = ifp.measure_always_on_channel(ao_json)
        assert measured["rendered_rule_ids"] == ["COMPLETE-001"]
        assert "INCOMPLETE-001" not in measured["per_rule_bytes"]


# --------------------------------------------------------------------------- #
# Overlap-set computation: a live intersection, never a hardcoded id list.
# --------------------------------------------------------------------------- #
class TestComputeOverlap:
    def test_overlap_is_the_intersection_of_rendered_ids(self):
        ifp = _imp("writ.analysis.injection_footprint")
        ao_json = {"rules": [{"rule_id": "SHARED-001", "trigger": "t", "statement": "s"}]}
        qresp = {"rules": [{"rule_id": "SHARED-001"}, {"rule_id": "RANKED-ONLY-001"}]}
        assert ifp.compute_overlap(ao_json, qresp) == {"SHARED-001"}

    def test_no_overlap_is_an_empty_set_not_none(self):
        ifp = _imp("writ.analysis.injection_footprint")
        ao_json = {"rules": [{"rule_id": "AO-ONLY-001", "trigger": "t", "statement": "s"}]}
        qresp = {"rules": [{"rule_id": "RANKED-ONLY-001"}]}
        overlap = ifp.compute_overlap(ao_json, qresp)
        assert overlap == set()
        assert overlap is not None

    def test_overlap_excludes_a_rule_dropped_by_the_renderable_filter(self):
        """Constraint: the overlap must come from the RENDERED always-on ids, not the
        eligible ones. An always-on rule missing its statement never reaches the
        agent, so a ranked hit sharing its id must not be reported as overlapping --
        that would point the reader at a block that never contained it."""
        ifp = _imp("writ.analysis.injection_footprint")
        ao_json = {"rules": [{"rule_id": "DROPPED-001", "trigger": "t", "statement": ""}]}
        qresp = {"rules": [{"rule_id": "DROPPED-001"}]}
        assert ifp.compute_overlap(ao_json, qresp) == set()

    def test_overlap_recomputes_when_inputs_change(self):
        """Guards a hardcoded/cached id list: the same call shape with different
        inputs must produce a different, freshly-recomputed answer."""
        ifp = _imp("writ.analysis.injection_footprint")
        qresp = {"rules": [{"rule_id": "X-001"}, {"rule_id": "Y-001"}]}
        ao_with_x = {"rules": [{"rule_id": "X-001", "trigger": "t", "statement": "s"}]}
        ao_with_y = {"rules": [{"rule_id": "Y-001", "trigger": "t", "statement": "s"}]}
        assert ifp.compute_overlap(ao_with_x, qresp) == {"X-001"}
        assert ifp.compute_overlap(ao_with_y, qresp) == {"Y-001"}

    def test_mode_scoping_changes_the_overlap_for_a_real_process_domain_rule(self):
        """ENF-PROC-DEBUG-001 (domain: process, always_on: true, mandatory: false) is
        stripped from /always-on outside work/debug (writ/server/routes/query.py:670,
        _ALWAYS_ON_PROCESS_MODES = {"work", "debug"}). The overlap computed from a
        work-mode always-on response must include it; the overlap computed from a
        response where /always-on already stripped it (any other mode) must not --
        same ranked hit, different always-on input."""
        ifp = _imp("writ.analysis.injection_footprint")
        qresp = {"rules": [{"rule_id": "ENF-PROC-DEBUG-001"}, {"rule_id": "ENF-COMMS-OUTPUT-001"}]}
        ao_work = {"rules": [
            {"rule_id": "ENF-PROC-DEBUG-001", "trigger": "t", "statement": "s"},
            {"rule_id": "ENF-COMMS-OUTPUT-001", "trigger": "t2", "statement": "s2"},
        ]}
        ao_outside_work_debug = {"rules": [
            {"rule_id": "ENF-COMMS-OUTPUT-001", "trigger": "t2", "statement": "s2"},
        ]}
        assert ifp.compute_overlap(ao_work, qresp) == {"ENF-PROC-DEBUG-001", "ENF-COMMS-OUTPUT-001"}
        assert ifp.compute_overlap(ao_outside_work_debug, qresp) == {"ENF-COMMS-OUTPUT-001"}


# --------------------------------------------------------------------------- #
# basis label, mirroring corpus_footprint.py's shape
# --------------------------------------------------------------------------- #
class TestBasisLabelPresent:
    def test_ranked_channel_reports_a_basis(self):
        ifp = _imp("writ.analysis.injection_footprint")
        measured = ifp.measure_ranked_channel({"rules": [_rule()], "mode": "standard"})
        assert measured.get("basis")

    def test_always_on_channel_reports_a_basis(self):
        ifp = _imp("writ.analysis.injection_footprint")
        measured = ifp.measure_always_on_channel(
            {"total_tokens": 0, "rules": [{"rule_id": "A-001", "trigger": "t", "statement": "s"}]}
        )
        assert measured.get("basis")


# --------------------------------------------------------------------------- #
# Daemon-unreachable error path: loud failure, never a zero-byte report.
#
# Port 1 is a reserved port nothing in this test environment binds to, so the
# connection fails fast and deterministically -- no mock of the transport is
# needed, and the test exercises the real failure mode rather than a simulated
# one.
# --------------------------------------------------------------------------- #
class TestBuildReportDaemonUnreachable:
    UNREACHABLE_BASE_URL = "http://127.0.0.1:1"

    def test_raises_named_error_instead_of_returning_a_report(self):
        ifp = _imp("writ.analysis.injection_footprint")
        with pytest.raises(ifp.InjectionFootprintError) as exc_info:
            ifp.build_report(base_url=self.UNREACHABLE_BASE_URL, mode="work", probe_prompt="probe")
        assert str(exc_info.value), "the error must name what failed, not be a blank exception"

    def test_error_names_the_daemon_as_the_failure(self):
        ifp = _imp("writ.analysis.injection_footprint")
        with pytest.raises(ifp.InjectionFootprintError) as exc_info:
            ifp.build_report(base_url=self.UNREACHABLE_BASE_URL, mode="work", probe_prompt="probe")
        message = str(exc_info.value).lower()
        assert any(word in message for word in ("unreachable", "connect", "daemon", "refused", "serve")), (
            f"error message does not name the daemon as unreachable: {message!r}"
        )


# --------------------------------------------------------------------------- #
# CLI wiring: `writ injection-footprint` (registered next to corpus-footprint /
# token-audit per plan.md). Assumed flag names (--mode, --prompt, --base-url):
# this test defines the contract, mirroring corpus-footprint's exit-2 canary
# convention in writ/cli.py. If the implementer picks different flag spellings,
# this is the test to update -- the CONTRACT (non-zero exit, non-empty error
# text, no silent zero-byte success) is what must hold regardless of spelling.
#
# Two SEPARATE checks, in this order, on purpose: Typer's own "no such command"
# usage error is ALSO a non-zero exit with non-empty output, so a single
# behavior-only assertion would pass today for the wrong reason (the command
# doesn't exist yet) and stay green even if the real implementation never
# handled an unreachable daemon at all. test_command_is_registered proves the
# command EXISTS; test_exit_nonzero_with_a_named_error_on_unreachable_daemon
# proves what it DOES, and additionally guards against the false-positive by
# asserting the failure text is not Click's "No such command" usage error --
# the same shape as `declare -F` before calling a bash function.
# --------------------------------------------------------------------------- #
class TestInjectionFootprintCli:
    def test_command_is_registered(self):
        from typer.testing import CliRunner
        if SKILL_ROOT not in sys.path:
            sys.path.insert(0, SKILL_ROOT)
        from writ.cli import app

        result = CliRunner().invoke(app, ["injection-footprint", "--help"])
        assert result.exit_code == 0, (
            f"'injection-footprint' is not a registered command yet "
            f"(exit {result.exit_code}). Output:\n{result.output}"
        )
        assert "no such command" not in result.output.lower()

    def test_exit_nonzero_with_a_named_error_on_unreachable_daemon(self):
        from typer.testing import CliRunner
        if SKILL_ROOT not in sys.path:
            sys.path.insert(0, SKILL_ROOT)
        from writ.cli import app

        result = CliRunner().invoke(app, [
            "injection-footprint", "--mode", "work", "--prompt", "probe",
            "--base-url", "http://127.0.0.1:1",
        ])
        combined_output = (result.output or "")
        # Guard against the false positive: this must be the COMMAND reporting a
        # daemon-unreachable error, not Typer saying the command doesn't exist.
        assert "no such command" not in combined_output.lower(), (
            "this failure is Typer's usage error for an unregistered command, not "
            "the command reporting a daemon-unreachable error -- register "
            "'injection-footprint' before this assertion means anything"
        )
        assert result.exit_code != 0
        assert combined_output.strip(), "a non-zero exit must still print a named error, not nothing"
