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
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO / "hooks" / "hooks.json"


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
