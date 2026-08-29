"""Unit tests for `writ.analysis.blackbox`, the pure reader that turns captured
blackbox JSONL records into a per (event, hook, direction) census of observed
keys and counts (plan.md `dfacff61-23d5-474e-846c-2e2f0f0ea482`, Decision 6).

Runs in the normal suite, not only under the `firedrill` marker: this module has
no gate to trigger and no subprocess to spawn, so it does not need the drill's
isolation machinery, and it is real coverage the full suite should carry on its
own.

SEAM CONTRACT for the implementer (writ/analysis/blackbox.py does not exist yet):

  read_capture_records(path) -> list[dict]
      Reads one capture JSONL file. Each row is `{"ts", "hook", "direction",
      "session", "pid", "payload"}` (bin/lib/common.sh::blackbox_log's own shape),
      where `payload` is the RAW JSON-encoded string of the envelope/output CC
      exchanged with the hook. A line that fails to parse as JSON at all
      contributes zero records and is otherwise skipped (surrounding valid lines
      are still counted). An absent or empty file returns `[]`.

  build_census(records, *, hooks_json_path=None) -> dict
      Pure: records in, census out, no file IO beyond an optional read of
      `hooks_json_path` (default `hooks/hooks.json`) to compute
      `events_never_observed`. Returns:
        {
          "records": {
              "<event>|<hook>|<direction>": {
                  "event": str, "hook": str, "direction": "in"|"out",
                  "count": int, "first_ts": str, "last_ts": str,
                  "keys": {<top-level envelope key>: <count>, ...},
                  "tool_input_keys": {<tool_name>: {<key>: <count>}},   # IN only
                  "hook_specific_output_keys": {<key>: <count>},        # OUT only
                  "mechanisms": {<mechanism>: <count>},                 # OUT only,
                                     # classified through writ.shared.delivery
              },
              ...
          },
          "events_never_observed": [str, ...],   # derived from hooks_json_path
        }
      `event` for a given row is read from the parsed payload's
      `hook_event_name` (IN) or `hookSpecificOutput.hookEventName` (OUT).

NEW IN THIS CYCLE (plan.md `dfacff61-23d5-474e-846c-2e2f0f0ea482`, the audit plus
Tier 1). `build_census`'s return value gains, ALONGSIDE the shape above (nothing
above changes):

  "synthetic_records": dict, the SAME per-key entry shape as "records", holding the
      record classes for rows classified synthetic (an IN row with no
      `hook_event_name` at all, or an OUT row that inherited a synthetic origin --
      see below). ROUTING IS BY ORIGIN, NOT BY EVENT NAME: a row goes here when it is
      classified synthetic, and undetermined rows STAY in "records" so no OUT class
      loses the evidence a provenance claim reads.

      An earlier draft of this contract added "and the event name `unknown` appears
      ONLY under this key". THAT IS NOT TRUE and the real capture log disproves it:
      an OUT row can name no event AND inherit a non-synthetic origin, which is
      `unknown|writ-pre-write-dispatch|out`, 68 rows, origins {harness 0,
      undetermined 68}. Making it true would mean routing by event name, which would
      move undetermined rows out of "records" and is exactly what this design
      forbids. The property that DOES hold without exception, and the one to assert
      against, is narrower: a row that CARRIED a `hook_event_name` never appears
      under the event name "unknown".
  "origin_counts": {"synthetic": int, "harness": int, "undetermined": int}, the
      GLOBAL row counts across "records" plus "synthetic_records" combined, each
      key present with an explicit 0 when that origin has no rows.
  Each entry under "records" (never under "synthetic_records", since every row
  there IS synthetic by definition) gains:
      "origins": {"harness": int, "undetermined": int}, each key present with an
      explicit 0.
  "edit_replace_all": {"true": int, "false": int, "absent": int} over IN rows whose
      `tool_name` is "Edit", keyed on `tool_input.get("replace_all")`: `True` ->
      "true", `False` -> "false", the key missing entirely -> "absent". Each key
      present with an explicit 0.
  "write_target_extensions": {<extension without its leading dot, or "" for a
      `file_path` with none>: int} over IN rows whose `tool_name` is "Write" or
      "Edit" and whose `tool_input.file_path` is non-empty.
  "write_rows_without_file_path": int, the count of IN rows whose `tool_name` is
      "Write" or "Edit" and whose `tool_input.file_path` is absent or empty.
  "hooks_never_captured": [str, ...], manifest-declared hook script basenames
      (`.sh` stripped, matching the "hook" field's own convention) with zero rows
      across BOTH "records" and "synthetic_records". Every manifest script when the
      log is empty.
  "directions_never_observed": {<event>: [<direction>, ...]}, for every event
      that appears at least once (in "records" or "synthetic_records"), which of
      "in"/"out" never appeared for THAT event, across every hook. {} when nothing
      has been observed at all.

THE THREE-WAY CLASSIFICATION, on a POSITIVE fingerprint (never inferred from what
is missing):
  IN row:
    synthetic     the payload carries no "hook_event_name" key at all
    harness       the payload carries "hook_event_name" AND ("transcript_path" in
                  the payload OR "prompt_id" in the payload)
    undetermined  the payload carries "hook_event_name" but neither harness-only
                  field ("cwd", "session_id" and "tool_use_id" are deliberately
                  EXCLUDED from the fingerprint: a hand-built probe plausibly
                  carries all three)
  OUT row: inherits the origin of the most recent IN row sharing (hook, pid,
    session); with no such IN row it is undetermined, and it is NEVER harness by
    default (only ever harness through inheritance).
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from writ.cli import app

REPO = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO / "hooks" / "hooks.json"
BLACKBOX_MODULE_PATH = REPO / "writ" / "analysis" / "blackbox.py"


def _blackbox_module():
    try:
        import writ.analysis.blackbox as blackbox
    except ImportError as exc:
        pytest.fail(f"skeleton: writ/analysis/blackbox.py does not exist yet: {exc}")
    return blackbox


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


class TestCensusFromRecords:
    def test_a_census_carries_observed_keys_counts_and_a_single_timestamp(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [
            {
                "ts": "2026-08-20T00:00:00+00:00",
                "hook": "writ-state-write-gate.sh",
                "direction": "in",
                "session": "s1",
                "payload": json.dumps({
                    "session_id": "s1",
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Write",
                    "tool_input": {"file_path": "x"},
                }),
            },
            {
                "ts": "2026-08-20T00:05:00+00:00",
                "hook": "writ-state-write-gate.sh",
                "direction": "out",
                "session": "s1",
                "payload": json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "no",
                    },
                }),
            },
        ]
        census = blackbox.build_census(records)
        record_map = census.get("records") or {}
        assert record_map, f"no per-(event, hook, direction) entries in census: {census!r}"

        in_key = "PreToolUse|writ-state-write-gate.sh|in"
        assert in_key in record_map, f"expected key {in_key!r} in {sorted(record_map)!r}"
        in_entry = record_map[in_key]
        assert in_entry["count"] == 1
        assert in_entry["first_ts"] == "2026-08-20T00:00:00+00:00"
        assert in_entry["last_ts"] == "2026-08-20T00:00:00+00:00"
        assert in_entry["keys"].get("tool_name") == 1
        assert in_entry.get("tool_input_keys", {}).get("Write", {}).get("file_path") == 1

        out_key = "PreToolUse|writ-state-write-gate.sh|out"
        assert out_key in record_map, f"expected key {out_key!r} in {sorted(record_map)!r}"
        out_entry = record_map[out_key]
        assert out_entry["count"] == 1
        assert out_entry.get("hook_specific_output_keys", {}).get("permissionDecision") == 1
        assert out_entry.get("mechanisms", {}).get("permissionDecisionReason") == 1

    def test_a_second_record_for_the_same_key_increments_count_and_extends_the_window(
        self,
    ) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        base = {
            "hook": "writ-state-write-gate.sh",
            "direction": "in",
            "session": "s1",
            "payload": json.dumps({
                "session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Write",
            }),
        }
        records = [
            {**base, "ts": "2026-08-20T00:00:00+00:00"},
            {**base, "ts": "2026-08-20T01:00:00+00:00"},
        ]
        census = blackbox.build_census(records)
        entry = census["records"]["PreToolUse|writ-state-write-gate.sh|in"]
        assert entry["count"] == 2, f"expected 2 records for the repeated key: {entry!r}"
        assert entry["first_ts"] == "2026-08-20T00:00:00+00:00"
        assert entry["last_ts"] == "2026-08-20T01:00:00+00:00"


class TestAbsentOrEmptyCaptureLog:
    def test_an_absent_log_yields_a_well_formed_artifact_with_zero_records(
        self, tmp_path
    ) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "read_capture_records", "build_census")
        missing = tmp_path / "does-not-exist.jsonl"

        records = blackbox.read_capture_records(missing)
        assert records == [], "an absent capture log must read as zero records, not an error"

        census = blackbox.build_census(records, hooks_json_path=HOOKS_JSON)
        assert census.get("records") == {}, (
            f"zero input records must produce a zero-record census: {census!r}"
        )
        never_observed = census.get("events_never_observed")
        assert never_observed, (
            "events_never_observed must be derived from hooks.json, not left empty, "
            "when nothing has been captured"
        )
        assert "PreToolUse" in never_observed, never_observed

    def test_an_empty_log_file_also_yields_zero_records(self, tmp_path) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "read_capture_records")
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        assert blackbox.read_capture_records(empty) == []


class TestMalformedLineIsSkipped:
    def test_a_malformed_line_contributes_zero_records_and_valid_ones_are_still_counted(
        self, tmp_path
    ) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "read_capture_records", "build_census")
        log = tmp_path / "capture.jsonl"
        good_row = lambda ts: json.dumps({
            "ts": ts,
            "hook": "h",
            "direction": "in",
            "session": "s",
            "payload": json.dumps({"hook_event_name": "PreToolUse"}),
        })
        log.write_text(
            good_row("2026-08-20T00:00:00+00:00") + "\n"
            + "{this is not valid json\n"
            + good_row("2026-08-20T00:01:00+00:00") + "\n"
        )

        records = blackbox.read_capture_records(log)
        assert len(records) == 2, (
            f"the malformed line must contribute zero records, valid ones still counted: "
            f"{records!r}"
        )

        census = blackbox.build_census(records)
        entry = census["records"]["PreToolUse|h|in"]
        assert entry["count"] == 2, entry


class TestCommittedArtifactLeaksNoHomePath:
    """The checked-in census artifact must carry no absolute home path.

    It is COMMITTED and the repo has a public mirror, so a generating machine's
    home path (and therefore its username) baked into `source_log` would be
    published permanently and would also be false on every other machine. The
    generator writes the path home-relative instead. This asserts the property on
    the artifact itself rather than on the helper, so it catches the leak however
    it got in, including a hand-edited file.
    """

    def test_the_artifact_contains_no_expanded_home_path(self) -> None:
        artifact = Path(__file__).resolve().parents[1] / "docs" / "reference" / "blackbox-census.json"
        if not artifact.exists():
            pytest.skip("no census artifact checked in yet")
        raw = artifact.read_text(encoding="utf-8")
        assert raw.strip(), "the artifact exists but is empty, so this check would be vacuous"
        home = os.path.expanduser("~")
        assert home not in raw, (
            f"the committed census artifact contains this machine's home path ({home}), "
            "which would be published by the public mirror; the generator must write it "
            "home-relative"
        )


def _manifest_hook_scripts() -> set[str]:
    """Every hook script basename `hooks/hooks.json` registers, `.sh` stripped --
    matching the "hook" field's own convention (`bin/lib/common.sh::blackbox_log`
    takes `basename "$0" .sh`). Derived independently here, from the manifest
    itself, rather than imported from the module under test, so this is a real
    check and not a restatement of whatever the implementation computed."""
    manifest = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    scripts: set[str] = set()
    for matcher_groups in manifest.get("hooks", {}).values():
        for group in matcher_groups:
            for hook in group.get("hooks", []):
                match = re.search(r"hooks/scripts/([^/\s]+)\.sh", hook.get("command", ""))
                if match:
                    scripts.add(match.group(1))
    return scripts


class TestOriginClassificationOnIn:
    """The three-way classification, on IN rows, from a POSITIVE fingerprint."""

    def test_no_hook_event_name_at_all_is_synthetic_and_absent_from_records(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [{
            "ts": "t", "hook": "probe", "direction": "in", "session": "s", "pid": 1,
            "payload": json.dumps({"tool_name": "Write", "tool_input": {"file_path": "x"}}),
        }]
        census = blackbox.build_census(records)
        synthetic = census.get("synthetic_records") or {}
        assert synthetic, f"no synthetic_records entries at all: {census!r}"
        key = "unknown|probe|in"
        assert key in synthetic, f"expected {key!r} in synthetic_records: {sorted(synthetic)!r}"
        assert synthetic[key]["count"] == 1, synthetic[key]
        assert key not in (census.get("records") or {}), (
            "a proven-synthetic row must not also appear under records"
        )

    def test_hook_event_name_plus_transcript_path_is_harness(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [{
            "ts": "t", "hook": "h", "direction": "in", "session": "s", "pid": 1,
            "payload": json.dumps({
                "hook_event_name": "Stop", "transcript_path": "/x/transcript.jsonl",
            }),
        }]
        census = blackbox.build_census(records)
        entry = census["records"]["Stop|h|in"]
        assert entry["origins"].get("harness") == 1, entry
        assert entry["origins"].get("undetermined", 0) == 0, entry

    def test_hook_event_name_plus_prompt_id_is_also_harness(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [{
            "ts": "t", "hook": "h", "direction": "in", "session": "s", "pid": 1,
            "payload": json.dumps({"hook_event_name": "UserPromptSubmit", "prompt_id": "p1"}),
        }]
        census = blackbox.build_census(records)
        entry = census["records"]["UserPromptSubmit|h|in"]
        assert entry["origins"].get("harness") == 1, entry

    def test_hook_event_name_alone_is_undetermined_and_stays_in_records(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [{
            "ts": "t", "hook": "h", "direction": "in", "session": "s", "pid": 1,
            "payload": json.dumps({
                "hook_event_name": "PreToolUse", "cwd": "/x",
                "session_id": "s", "tool_use_id": "u1",
            }),
        }]
        census = blackbox.build_census(records)
        entry = census["records"]["PreToolUse|h|in"]
        assert entry["origins"].get("undetermined") == 1, entry
        assert entry["origins"].get("harness", 0) == 0, (
            "cwd/session_id/tool_use_id are deliberately excluded from the harness "
            f"fingerprint (a probe plausibly carries all three): {entry!r}"
        )

    def test_unknown_event_name_never_appears_under_records(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [
            {"ts": "t", "hook": "probe", "direction": "in", "session": "s", "pid": 1,
             "payload": json.dumps({"tool_name": "Write"})},
            {"ts": "t", "hook": "h", "direction": "in", "session": "s", "pid": 2,
             "payload": json.dumps({"hook_event_name": "PreToolUse"})},
        ]
        census = blackbox.build_census(records)
        assert any(k.startswith("unknown|") for k in census.get("synthetic_records") or {}), (
            census.get("synthetic_records")
        )
        assert not any(k.startswith("unknown|") for k in census.get("records") or {}), (
            f"'unknown' must never appear under records for a row that carried a "
            f"hook_event_name: {census.get('records')!r}"
        )


class TestOutRowOriginInheritance:
    """An OUT payload carries no harness fingerprint of its own (it is the hook's own
    reply); it inherits the origin of the most recent IN row sharing (hook, pid,
    session)."""

    def test_an_out_row_inherits_the_harness_origin_of_the_matching_in_row(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [
            {"ts": "t1", "hook": "h", "direction": "in", "session": "s", "pid": 100,
             "payload": json.dumps({"hook_event_name": "PreToolUse", "transcript_path": "/x"})},
            {"ts": "t2", "hook": "h", "direction": "out", "session": "s", "pid": 100,
             "payload": json.dumps({
                 "hookSpecificOutput": {
                     "hookEventName": "PreToolUse", "permissionDecisionReason": "no",
                 },
             })},
        ]
        census = blackbox.build_census(records)
        out_entry = census["records"]["PreToolUse|h|out"]
        assert out_entry["origins"].get("harness") == 1, out_entry

    def test_an_out_row_with_no_matching_in_row_is_undetermined_never_harness(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [
            {"ts": "t", "hook": "h", "direction": "out", "session": "s", "pid": 999,
             "payload": json.dumps({
                 "hookSpecificOutput": {
                     "hookEventName": "PreToolUse", "permissionDecisionReason": "no",
                 },
             })},
        ]
        census = blackbox.build_census(records)
        out_entry = census["records"]["PreToolUse|h|out"]
        assert out_entry["origins"].get("undetermined") == 1, out_entry
        assert out_entry["origins"].get("harness", 0) == 0, out_entry

    def test_interleaved_synthetic_and_harness_rows_attribute_each_out_to_its_own_process(
        self,
    ) -> None:
        """Two DIFFERENT processes of the SAME hook script: pid, not hook alone, is
        the join key, so a probe's OUT row must not inherit the harness process's
        origin (or vice versa) merely because they share a hook name."""
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [
            {"ts": "t1", "hook": "h", "direction": "in", "session": "s", "pid": 1,
             "payload": json.dumps({"tool_name": "Write"})},  # synthetic, pid 1
            {"ts": "t2", "hook": "h", "direction": "in", "session": "s", "pid": 2,
             "payload": json.dumps({
                 "hook_event_name": "PreToolUse", "transcript_path": "/x",
             })},  # harness, pid 2
            {"ts": "t3", "hook": "h", "direction": "out", "session": "s", "pid": 1,
             "payload": json.dumps({})},
            {"ts": "t4", "hook": "h", "direction": "out", "session": "s", "pid": 2,
             "payload": json.dumps({
                 "hookSpecificOutput": {
                     "hookEventName": "PreToolUse", "permissionDecisionReason": "no",
                 },
             })},
        ]
        census = blackbox.build_census(records)
        synthetic = census.get("synthetic_records") or {}
        assert "unknown|h|out" in synthetic, (
            f"pid 1's OUT row must inherit the probe's synthetic origin: {synthetic!r}"
        )

        harness_out = census["records"]["PreToolUse|h|out"]
        assert harness_out["origins"].get("harness") == 1, (
            f"pid 2's OUT row must inherit the harness origin, not the probe's: "
            f"{harness_out!r}"
        )


class TestOriginCounts:
    def test_origin_counts_reports_all_three_with_an_explicit_zero(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [{
            "ts": "t", "hook": "h", "direction": "in", "session": "s", "pid": 1,
            "payload": json.dumps({"hook_event_name": "PreToolUse", "transcript_path": "/x"}),
        }]
        census = blackbox.build_census(records)
        counts = census.get("origin_counts")
        assert counts is not None, f"no origin_counts key: {census!r}"
        assert counts == {"synthetic": 0, "harness": 1, "undetermined": 0}, counts

    def test_an_absent_log_reports_all_three_origins_at_zero(self, tmp_path) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "read_capture_records", "build_census")
        records = blackbox.read_capture_records(tmp_path / "missing.jsonl")
        census = blackbox.build_census(records, hooks_json_path=HOOKS_JSON)
        assert census.get("origin_counts") == {"synthetic": 0, "harness": 0, "undetermined": 0}


class TestMalformedLineStillClassifiesSurvivors:
    """Extends TestMalformedLineIsSkipped's fixture: the surviving rows are not just
    counted, they are classified too."""

    def test_the_malformed_line_contributes_nothing_and_survivors_get_an_origin(
        self, tmp_path
    ) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "read_capture_records", "build_census")
        log = tmp_path / "capture.jsonl"
        good_row = lambda ts: json.dumps({
            "ts": ts, "hook": "h", "direction": "in", "session": "s", "pid": 1,
            "payload": json.dumps({"hook_event_name": "PreToolUse"}),
        })
        log.write_text(
            good_row("2026-08-20T00:00:00+00:00") + "\n"
            + "{this is not valid json\n"
            + good_row("2026-08-20T00:01:00+00:00") + "\n"
        )

        records = blackbox.read_capture_records(log)
        assert len(records) == 2, records

        census = blackbox.build_census(records)
        entry = census["records"]["PreToolUse|h|in"]
        assert entry["count"] == 2, entry
        assert entry["origins"].get("undetermined") == 2, (
            f"both surviving rows must be classified undetermined: {entry!r}"
        )


class TestEditReplaceAll:
    def test_true_false_and_absent_are_each_counted(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")

        def _edit_row(pid: int, *, include_key: bool, replace_all: bool = False) -> dict:
            tool_input = {"old_string": "a", "new_string": "b", "file_path": "/x.py"}
            if include_key:
                tool_input["replace_all"] = replace_all
            return {
                "ts": "t", "hook": "h", "direction": "in", "session": "s", "pid": pid,
                "payload": json.dumps({
                    "hook_event_name": "PreToolUse", "tool_name": "Edit",
                    "tool_input": tool_input,
                }),
            }

        records = [
            _edit_row(1, include_key=True, replace_all=True),
            _edit_row(2, include_key=True, replace_all=False),
            _edit_row(3, include_key=False),
        ]
        census = blackbox.build_census(records)
        counts = census.get("edit_replace_all")
        assert counts is not None, f"no edit_replace_all key: {census!r}"
        assert counts == {"true": 1, "false": 1, "absent": 1}, counts

    def test_a_proven_synthetic_row_contributes_to_neither_evidence_axis(self) -> None:
        """A probe row is excluded from the evidence axes, not just from `records`.

        These two axes are what a later cycle is told to read as evidence for sizing
        the deferred analyzer work, so a probe counted here is a contaminated number
        presented as a measurement, which is the exact defect this cycle exists to
        remove. The synthetic row below is identical to the harness one except that it
        carries no `hook_event_name`, so any difference in the counts is attributable
        to origin alone.
        """
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")

        def _edit_row(pid: int, *, harness: bool) -> dict:
            payload = {
                "tool_name": "Edit",
                "tool_input": {"file_path": "/x/y.py", "replace_all": True},
            }
            if harness:
                payload["hook_event_name"] = "PreToolUse"
                payload["prompt_id"] = "p1"
            return {
                "ts": "t", "hook": "h", "direction": "in", "session": "s", "pid": pid,
                "payload": json.dumps(payload),
            }

        synthetic_only = blackbox.build_census([_edit_row(1, harness=False)])
        assert synthetic_only["origin_counts"]["synthetic"] == 1, (
            "precondition: the row must actually classify as synthetic, or this test "
            f"proves nothing: {synthetic_only['origin_counts']!r}"
        )
        assert synthetic_only["edit_replace_all"] == {"true": 0, "false": 0, "absent": 0}, (
            "a proven-synthetic row contaminated edit_replace_all: "
            f"{synthetic_only['edit_replace_all']!r}"
        )
        assert synthetic_only["write_target_extensions"] == {}, (
            "a proven-synthetic row contaminated write_target_extensions: "
            f"{synthetic_only['write_target_extensions']!r}"
        )

        # The mirror case, so a pass here can never come from the fold being broken
        # outright: an otherwise identical HARNESS row must still be counted.
        harness_only = blackbox.build_census([_edit_row(2, harness=True)])
        assert harness_only["edit_replace_all"]["true"] == 1, harness_only["edit_replace_all"]
        assert harness_only["write_target_extensions"] == {"py": 1}, (
            harness_only["write_target_extensions"]
        )

    def test_a_non_edit_tool_row_does_not_affect_the_counts(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [{
            "ts": "t", "hook": "h", "direction": "in", "session": "s", "pid": 1,
            "payload": json.dumps({
                "hook_event_name": "PreToolUse", "tool_name": "Write",
                "tool_input": {"file_path": "/x.py"},
            }),
        }]
        census = blackbox.build_census(records)
        assert census.get("edit_replace_all") == {"true": 0, "false": 0, "absent": 0}, (
            census.get("edit_replace_all")
        )


class TestWriteTargetExtensions:
    def test_extension_distribution_over_write_and_edit_file_paths(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")

        def _row(pid: int, tool_name: str, file_path: str | None) -> dict:
            tool_input = {}
            if file_path is not None:
                tool_input["file_path"] = file_path
            return {
                "ts": "t", "hook": "h", "direction": "in", "session": "s", "pid": pid,
                "payload": json.dumps({
                    "hook_event_name": "PreToolUse", "tool_name": tool_name,
                    "tool_input": tool_input,
                }),
            }

        records = [
            _row(1, "Write", "/a/b.py"),
            _row(2, "Edit", "/a/b.sh"),
            _row(3, "Write", "/a/Makefile"),
            _row(4, "Write", None),
            _row(5, "Read", "/a/c.py"),
        ]
        census = blackbox.build_census(records)
        ext_dist = census.get("write_target_extensions")
        assert ext_dist is not None, f"no write_target_extensions key: {census!r}"
        assert ext_dist == {"py": 1, "sh": 1, "": 1}, ext_dist
        assert census.get("write_rows_without_file_path") == 1, (
            census.get("write_rows_without_file_path")
        )

    def test_an_absent_log_yields_an_empty_distribution_and_a_zero_count(
        self, tmp_path
    ) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "read_capture_records", "build_census")
        records = blackbox.read_capture_records(tmp_path / "missing.jsonl")
        census = blackbox.build_census(records, hooks_json_path=HOOKS_JSON)
        assert census.get("write_target_extensions") == {}
        assert census.get("write_rows_without_file_path") == 0


class TestHooksNeverCaptured:
    def test_every_manifest_script_is_listed_when_the_log_is_empty(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        expected = _manifest_hook_scripts()
        assert expected, "no scripts derived from hooks.json; this assertion would be vacuous"

        census = blackbox.build_census([], hooks_json_path=HOOKS_JSON)
        never_captured = census.get("hooks_never_captured")
        assert never_captured is not None, f"no hooks_never_captured key: {census!r}"
        assert set(never_captured) == expected, (
            f"an empty log must list every manifest-declared script as never "
            f"captured: missing={expected - set(never_captured)!r} "
            f"extra={set(never_captured) - expected!r}"
        )

    def test_an_unreadable_manifest_is_reported_not_read_as_full_coverage(
        self, tmp_path
    ) -> None:
        """An unreadable manifest must not read as the strongest coverage claim.

        `hooks_never_captured` and `events_never_observed` are both derived from the
        manifest, so an unreadable one yields an empty registered set and both come
        back EMPTY, which a reader takes as "every hook was captured and every event
        observed". That is a maximal coverage claim produced by a failure to read a
        file. `manifest_read` makes "nothing to report" distinguishable from "could
        not tell", which is this module's premise applied to its own input.
        """
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")

        good = blackbox.build_census([], hooks_json_path=HOOKS_JSON)
        assert good.get("manifest_read") is True, good.get("manifest_read")
        assert good["hooks_never_captured"], (
            "precondition: a readable manifest must yield a non-empty list here, or "
            "the comparison below proves nothing"
        )

        broken = tmp_path / "not-json.json"
        broken.write_text("{this is not json")
        bad = blackbox.build_census([], hooks_json_path=broken)
        assert bad.get("manifest_read") is False, (
            f"an unreadable manifest must report manifest_read False: {bad.get('manifest_read')!r}"
        )
        assert bad["hooks_never_captured"] == [], bad["hooks_never_captured"]
        assert bad["events_never_observed"] == [], bad["events_never_observed"]

    def test_a_captured_hook_drops_out_of_the_never_captured_list(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        expected = _manifest_hook_scripts()
        assert "pre-validate-file" in expected, expected
        records = [{
            "ts": "2026-08-20T00:00:00+00:00", "hook": "pre-validate-file",
            "direction": "in", "session": "s1", "pid": 1,
            "payload": json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Edit"}),
        }]
        census = blackbox.build_census(records, hooks_json_path=HOOKS_JSON)
        never_captured = set(census.get("hooks_never_captured") or [])
        assert "pre-validate-file" not in never_captured, never_captured
        assert never_captured == expected - {"pre-validate-file"}, never_captured


class TestDirectionsNeverObserved:
    def test_an_event_observed_only_in_reports_out_as_never_observed(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [{
            "ts": "t", "hook": "h", "direction": "in", "session": "s", "pid": 1,
            "payload": json.dumps({"hook_event_name": "PreToolUse"}),
        }]
        census = blackbox.build_census(records)
        never = census.get("directions_never_observed")
        assert never is not None, f"no directions_never_observed key: {census!r}"
        assert never.get("PreToolUse") == ["out"], never

    def test_an_event_observed_on_both_directions_has_no_gap(self) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "build_census")
        records = [
            {"ts": "t", "hook": "h", "direction": "in", "session": "s", "pid": 1,
             "payload": json.dumps({"hook_event_name": "PreToolUse"})},
            {"ts": "t", "hook": "h2", "direction": "out", "session": "s", "pid": 2,
             "payload": json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse"}})},
        ]
        census = blackbox.build_census(records)
        never = census.get("directions_never_observed") or {}
        assert "PreToolUse" not in never, never

    def test_an_absent_log_reports_no_gaps_because_nothing_was_observed(
        self, tmp_path
    ) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "read_capture_records", "build_census")
        records = blackbox.read_capture_records(tmp_path / "missing.jsonl")
        census = blackbox.build_census(records, hooks_json_path=HOOKS_JSON)
        assert census.get("directions_never_observed") == {}, (
            census.get("directions_never_observed")
        )


class TestAbsentLogCarriesEveryNewKeyWithAZeroOrEmptyValue:
    """The absence-is-stated-not-inferred property, checked across EVERY new key at
    once: each key must be PRESENT (never simply missing) and hold its zero/empty
    value."""

    def test_every_new_key_is_present_for_an_absent_log(self, tmp_path) -> None:
        blackbox = _blackbox_module()
        _require(blackbox, "read_capture_records", "build_census")
        records = blackbox.read_capture_records(tmp_path / "missing.jsonl")
        census = blackbox.build_census(records, hooks_json_path=HOOKS_JSON)

        expected_scripts = _manifest_hook_scripts()
        assert expected_scripts, "no manifest scripts derived; this assertion would be vacuous"

        new_keys = (
            "synthetic_records", "origin_counts", "edit_replace_all",
            "write_target_extensions", "write_rows_without_file_path",
            "hooks_never_captured", "directions_never_observed",
        )
        for key in new_keys:
            assert key in census, f"{key!r} is a MISSING key rather than a stated zero/empty value"

        assert census["synthetic_records"] == {}
        assert census["origin_counts"] == {"synthetic": 0, "harness": 0, "undetermined": 0}
        assert census["edit_replace_all"] == {"true": 0, "false": 0, "absent": 0}
        assert census["write_target_extensions"] == {}
        assert census["write_rows_without_file_path"] == 0
        assert set(census["hooks_never_captured"]) == expected_scripts
        assert census["directions_never_observed"] == {}


class TestBlackboxModuleImportBudget:
    """PERF-QBUDGET-001: the census reader runs with no daemon and no graph, which
    only holds if it never imports one. Statically parses the module's own AST
    (never imports it) so a NEW import added while building the origin
    classification, `edit_replace_all` or `write_target_extensions` features is
    caught even if that import is never exercised by another test."""

    def test_imports_nothing_beyond_stdlib_and_the_two_named_writ_modules(self) -> None:
        allowed_writ_modules = {"writ.analysis.jsonl", "writ.shared.delivery"}
        tree = ast.parse(
            BLACKBOX_MODULE_PATH.read_text(encoding="utf-8"),
            filename=str(BLACKBOX_MODULE_PATH),
        )
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root == "writ":
                        if alias.name not in allowed_writ_modules:
                            offenders.append(alias.name)
                    elif root not in sys.stdlib_module_names:
                        offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                root = module.split(".")[0]
                if root == "writ":
                    if module not in allowed_writ_modules:
                        offenders.append(module)
                elif root and root not in sys.stdlib_module_names:
                    offenders.append(module)
        assert not offenders, (
            f"writ/analysis/blackbox.py imports beyond its stated budget "
            f"(stdlib + writ.analysis.jsonl + writ.shared.delivery): {offenders!r}"
        )


class TestCLISummaryLineReportsOriginSplit:
    """`writ blackbox-census` (writ/cli.py) prints the harness/synthetic/undetermined
    split on its summary line, so a reader cannot take `record_count` for a count of
    real Claude Code traffic."""

    def test_the_summary_line_names_every_origin_and_carries_its_count(
        self, tmp_path
    ) -> None:
        log = tmp_path / "capture.jsonl"
        rows = [
            {"ts": "t", "hook": "probe-hook", "direction": "in", "session": "s",
             "payload": json.dumps({"tool_name": "Write"})},
            {"ts": "t", "hook": "real-hook", "direction": "in", "session": "s",
             "payload": json.dumps({"hook_event_name": "PreToolUse", "transcript_path": "/x"})},
            {"ts": "t", "hook": "other-hook", "direction": "in", "session": "s",
             "payload": json.dumps({"hook_event_name": "PreToolUse"})},
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        out = tmp_path / "out.json"

        runner = CliRunner()
        result = runner.invoke(app, ["blackbox-census", "--log", str(log), "--out", str(out)])
        assert result.exit_code == 0, result.output

        written = json.loads(out.read_text())
        origin_counts = written.get("origin_counts")
        assert origin_counts, f"no origin_counts key in the written artifact: {written!r}"
        assert origin_counts == {"synthetic": 1, "harness": 1, "undetermined": 1}, origin_counts

        for origin, count in origin_counts.items():
            assert origin in result.output, (
                f"the summary line must name every origin ({origin!r} missing): "
                f"{result.output!r}"
            )
            assert str(count) in result.output, (
                f"the summary line must carry {origin}'s count ({count}): {result.output!r}"
            )
