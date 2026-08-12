"""RED tests for writ/session/transcript_tripwire.py (does not exist yet).

Plan: plan.md "Part 4: a tripwire for something Writ cannot fix" (~line 341) and its
file list (~line 83). Capabilities: capabilities.md, the five unticked tripwire items.

WHAT THE THING IS. A Claude Code harness bug can deliver a queued user keystroke into
a sub-agent's pending turn. Writ cannot fix the harness; it can only refuse to let a
recurrence be invisible. The signature is structural: a `role:"user"` message whose
`content` array holds a bare `{"type":"text"}` element ALONGSIDE a
`{"type":"tool_result"}` element. In a well-formed dispatch, a user message carrying
tool results carries ONLY tool results. Genuine Writ hook injections show up as
`{"type":"attachment"}` content elements carrying a `hook_additional_context` or
`hook_success` marker key, which is a structurally different shape and must never be
flagged.

CONTRACT this file pins (write writ/session/transcript_tripwire.py to satisfy it):

- `is_malformed_user_turn(message: dict) -> bool` -- pure predicate. `message` is the
  decoded `"message"` object of one transcript line, i.e. `{"role": ..., "content":
  ...}`. Returns False when `message.get("role") != "user"`; False when `content` is a
  bare string; False when every content element is `type == "tool_result"`; False when
  every content element is `type == "text"`; False when a `type == "attachment"`
  element (carrying `hook_additional_context` or `hook_success`) sits beside a
  tool_result. Returns True ONLY when a bare `{"type": "text"}` element sits beside a
  `{"type": "tool_result"}` element in the same content list.

- `scan_transcript(path) -> list[Finding]` -- decodes a jsonl file line by line (each
  line is `{"type": ..., "message": {...}, "timestamp": ...}`), calls
  `is_malformed_user_turn` on each line's `message`, and returns one `Finding` per
  flagged line, in file order. Returns `[]` when `path` does not exist. A line that
  fails `json.loads` is skipped, not fatal -- the scan continues and still reports
  findings that occur after the corrupt line.

- `Finding` -- carries `file_path`, `line_number` (1-based), `timestamp`, `digest`
  (`hashlib.sha1(text.encode()).hexdigest()` of the offending text), `text_length`,
  `text_count`, and `tool_result_count`. It may also hold the raw text for a CLI
  `--show-text` path, but `Finding.to_record() -> dict` returns the privacy-safe
  subset (path, line_number, timestamp, digest, text_length, text_count,
  tool_result_count) and must not carry the text itself, directly or as a substring
  of any value.

- `KNOWN_BENIGN_SENTINELS` -- a module-level allowlist constant (an iterable of exact
  sentinel strings), consulted by `is_malformed_user_turn`: when the bare text exactly
  matches an entry, the message is not flagged even though the shape is otherwise the
  flagged shape. Its entries are decided by a corpus measurement that has not run yet,
  so tests pin the MECHANISM (the constant exists, and membership suppresses a flag)
  via monkeypatching, never its contents.

- `subagent_transcript_path(transcript_path, parent_session_id, agent_id) -> Path` --
  SubagentStop payloads carry `transcript_path` naming the PARENT session's transcript.
  The sub-agent's own transcript lives at
  `<dirname(transcript_path)>/<parent_session_id>/subagents/agent-<agent_id>.jsonl`.
  The function computes this path; it never touches the filesystem to do so.

- `resolve_subagent_transcript(payload: dict) -> Path | None` -- resolves the
  sub-agent's own transcript straight from a raw SubagentStop payload, trying in
  order: (1) `payload["agent_transcript_path"]` when non-empty, returned as a `Path`
  even if that file does not exist (Claude Code resolves the sub-agent's own
  transcript itself and this key was present in 42 of 42 captured SubagentStop
  payloads, so it is authoritative); (2) the flat derivation
  `<dirname(transcript_path)>/<session_id>/subagents/agent-<agent_id>.jsonl`, returned
  ONLY if that file exists; (3) the workflow-nested shape, found by globbing
  `<dirname(transcript_path)>/<session_id>/subagents/workflows/wf_*/agent-<agent_id>.jsonl`
  and returning the first match (workflow fan-out dispatches nest one level deeper and
  accounted for 10 of 42 captured payloads); (4) otherwise `None`, meaning the caller
  no-ops. A payload missing `transcript_path` (or any other expected key) must return
  `None`, never raise. Whatever arm produces the candidate, if it resolves
  (`Path.resolve()`) to the SAME FILE as `payload["transcript_path"]`, the answer is
  `None`: that file is the parent session's own transcript, and scanning it would flag
  legitimate user turns as foreign input.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


def _tool_result(tool_use_id: str = "tu-1", content: str = "ok") -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _attachment(marker: str, value: object = "some-context") -> dict:
    return {"type": "attachment", marker: value}


def _line(content, ts: str = "2026-08-01T00:00:00.000Z", role: str = "user") -> dict:
    return {"type": "user", "message": {"role": role, "content": content}, "timestamp": ts}


def _write_jsonl(path, lines: list) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


class TestIsMalformedUserTurnPredicate:
    def test_flags_bare_text_beside_tool_result(self) -> None:
        from writ.session.transcript_tripwire import is_malformed_user_turn

        message = {"role": "user", "content": [_text("queued keystroke"), _tool_result()]}
        assert is_malformed_user_turn(message) is True

    def test_ignores_only_tool_result_content(self) -> None:
        from writ.session.transcript_tripwire import is_malformed_user_turn

        message = {"role": "user", "content": [_tool_result(), _tool_result(tool_use_id="tu-2")]}
        assert is_malformed_user_turn(message) is False

    def test_ignores_only_text_content(self) -> None:
        from writ.session.transcript_tripwire import is_malformed_user_turn

        message = {"role": "user", "content": [_text("an ordinary prompt")]}
        assert is_malformed_user_turn(message) is False

    def test_ignores_non_user_role_message(self) -> None:
        from writ.session.transcript_tripwire import is_malformed_user_turn

        # Same mixed shape, but role is "assistant" -- must not be flagged at all.
        message = {"role": "assistant", "content": [_text("stray text"), _tool_result()]}
        assert is_malformed_user_turn(message) is False

    def test_ignores_string_content(self) -> None:
        from writ.session.transcript_tripwire import is_malformed_user_turn

        message = {"role": "user", "content": "plain string content, not a list"}
        assert is_malformed_user_turn(message) is False

    def test_ignores_attachment_with_hook_additional_context_marker(self) -> None:
        from writ.session.transcript_tripwire import is_malformed_user_turn

        message = {
            "role": "user",
            "content": [_attachment("hook_additional_context"), _tool_result()],
        }
        assert is_malformed_user_turn(message) is False

    def test_ignores_attachment_with_hook_success_marker(self) -> None:
        from writ.session.transcript_tripwire import is_malformed_user_turn

        message = {
            "role": "user",
            "content": [_attachment("hook_success"), _tool_result()],
        }
        assert is_malformed_user_turn(message) is False


class TestIsMalformedUserTurnIgnoresEmptyOrWhitespaceOnlyText:
    """A bare `{"type": "text"}` element that carries no real content is not a
    spliced keystroke -- flagging it would produce a finding whose entire
    payload is the digest of the empty string, which is not the harness bug
    this tripwire exists to catch. `test_real_text_beside_tool_result_is_still_flagged`
    is the required contrast: it proves the two empty/whitespace cases below
    are suppressed because they carry no information, not because a stub
    blanket-suppresses every text+tool_result mix.
    """

    def test_ignores_text_element_with_no_text_key_beside_tool_result(self) -> None:
        from writ.session.transcript_tripwire import is_malformed_user_turn

        message = {"role": "user", "content": [{"type": "text"}, _tool_result()]}
        assert is_malformed_user_turn(message) is False

    def test_ignores_whitespace_only_text_beside_tool_result(self) -> None:
        from writ.session.transcript_tripwire import is_malformed_user_turn

        message = {"role": "user", "content": [_text("   "), _tool_result()]}
        assert is_malformed_user_turn(message) is False

    def test_real_text_beside_tool_result_is_still_flagged(self) -> None:
        from writ.session.transcript_tripwire import is_malformed_user_turn

        message = {"role": "user", "content": [_text("queued keystroke"), _tool_result()]}
        assert is_malformed_user_turn(message) is True

    def test_mixed_empty_and_real_text_element_flags_and_digest_covers_only_real_text(
        self, tmp_path
    ) -> None:
        """Pins the one shape this class does not otherwise cover: ONE
        empty-or-whitespace text element sitting ALONGSIDE ONE real text
        element in the same user turn, both beside a tool_result. The empty
        element must be dropped, not joined in as an empty string -- so the
        turn still flags, `text_count` is 1 (not 2), and the digest is over
        the real text ALONE. A stub that joined the empty text into the
        digest input (e.g. `"" + "\n" + real_text` or `real_text + "\n" +
        ""`) would still satisfy a bare `is_malformed_user_turn` check but
        would produce a DIFFERENT digest here, which is exactly what this
        test is for.
        """
        from writ.session.transcript_tripwire import is_malformed_user_turn, scan_transcript

        real_text = "queued keystroke beside an empty text element"
        content = [{"type": "text"}, _text(real_text), _tool_result()]
        message = {"role": "user", "content": content}
        assert is_malformed_user_turn(message) is True

        p = tmp_path / "transcript.jsonl"
        _write_jsonl(p, [_line(content)])

        findings = scan_transcript(p)

        assert len(findings) == 1
        finding = findings[0]
        assert finding.text_count == 1
        assert finding.digest == hashlib.sha1(real_text.encode()).hexdigest()


class TestAllowlistMechanism:
    def test_allowlist_constant_exists_at_module_level(self) -> None:
        import writ.session.transcript_tripwire as tripwire

        assert hasattr(tripwire, "KNOWN_BENIGN_SENTINELS")
        # Must be iterable (membership-checkable); contents are NOT asserted here.
        iter(tripwire.KNOWN_BENIGN_SENTINELS)

    def test_allowlisted_sentinel_beside_tool_result_is_not_flagged(self, monkeypatch) -> None:
        import writ.session.transcript_tripwire as tripwire

        sentinel = "[Request interrupted by user]"
        message = {"role": "user", "content": [_text(sentinel), _tool_result()]}

        # Baseline: with an allowlist that does NOT contain the sentinel, the mixed
        # shape is flagged (proves the allowlist -- not blanket leniency -- is why the
        # next assertion is False).
        monkeypatch.setattr(tripwire, "KNOWN_BENIGN_SENTINELS", frozenset({"something-else"}))
        assert tripwire.is_malformed_user_turn(message) is True

        # With the sentinel allowlisted, the identical shape is not flagged.
        monkeypatch.setattr(tripwire, "KNOWN_BENIGN_SENTINELS", frozenset({sentinel}))
        assert tripwire.is_malformed_user_turn(message) is False


class TestScanTranscriptFindsMalformedTurns:
    def test_scan_reports_file_line_timestamp_digest_and_counts(self, tmp_path) -> None:
        from writ.session.transcript_tripwire import scan_transcript

        p = tmp_path / "transcript.jsonl"
        text = "queued keystroke leaked into the sub-agent turn"
        ts = "2026-08-01T09:30:00.000Z"
        _write_jsonl(p, [_line([_text(text), _tool_result()], ts=ts)])

        findings = scan_transcript(p)

        assert len(findings) == 1
        finding = findings[0]
        assert str(finding.file_path) == str(p)
        assert finding.line_number == 1
        assert finding.timestamp == ts
        assert finding.digest == hashlib.sha1(text.encode()).hexdigest()
        assert finding.text_length == len(text)
        assert finding.text_count == 1
        assert finding.tool_result_count == 1

    def test_scan_returns_empty_list_for_nonexistent_path(self, tmp_path) -> None:
        from writ.session.transcript_tripwire import scan_transcript

        assert scan_transcript(tmp_path / "does-not-exist.jsonl") == []

    def test_scan_skips_corrupt_json_line_and_still_returns_later_finding(self, tmp_path) -> None:
        from writ.session.transcript_tripwire import scan_transcript

        p = tmp_path / "transcript.jsonl"
        good_line = json.dumps(_line([_text("first bad turn"), _tool_result()]))
        corrupt_line = '{"type": "user", "message": {bad json'
        later_line = json.dumps(_line([_text("second bad turn"), _tool_result()]))
        p.write_text(good_line + "\n" + corrupt_line + "\n" + later_line + "\n")

        findings = scan_transcript(p)

        assert [f.line_number for f in findings] == [1, 3]

    def test_line_numbers_are_one_based_when_finding_is_not_on_first_line(self, tmp_path) -> None:
        from writ.session.transcript_tripwire import scan_transcript

        p = tmp_path / "transcript.jsonl"
        benign_first_line = _line([_text("an ordinary first prompt")])
        malformed_second_line = _line([_text("leaked keystroke"), _tool_result()])
        _write_jsonl(p, [benign_first_line, malformed_second_line])

        findings = scan_transcript(p)

        assert len(findings) == 1
        assert findings[0].line_number == 2


class TestFindingPrivacy:
    def test_to_record_never_leaks_the_foreign_text(self, tmp_path) -> None:
        from writ.session.transcript_tripwire import scan_transcript

        canary = "PRIVACY-CANARY-4471-DO-NOT-LEAK-THIS-STRING"
        p = tmp_path / "transcript.jsonl"
        _write_jsonl(p, [_line([_text(canary), _tool_result()])])

        finding = scan_transcript(p)[0]
        dumped = json.dumps(finding.to_record())

        # Discriminator against a stub that dumps everything: the record retains a
        # fingerprint of the text but never the text itself, nor any substring of it.
        expected_digest = hashlib.sha1(canary.encode()).hexdigest()
        assert canary not in dumped
        for i in range(0, len(canary) - 8, 8):
            assert canary[i:i + 8] not in dumped
        assert expected_digest in dumped


class TestSubagentTranscriptPathDerivation:
    def test_derives_path_without_requiring_the_file_to_exist(self, tmp_path) -> None:
        from writ.session.transcript_tripwire import subagent_transcript_path

        transcript_path = tmp_path / "parent-session-abc.jsonl"
        parent_session_id = "parent-session-abc"
        agent_id = "worker-1"

        result = subagent_transcript_path(transcript_path, parent_session_id, agent_id)

        expected = tmp_path / "parent-session-abc" / "subagents" / "agent-worker-1.jsonl"
        assert result == expected
        assert not result.exists()
        assert not result.parent.exists()


class TestResolveSubagentTranscript:
    """Pins `resolve_subagent_transcript(payload: dict) -> Path | None`, the
    payload-driven resolver that supersedes calling `subagent_transcript_path`
    directly: it tries the payload's own `agent_transcript_path` first, then the
    flat derivation, then the workflow-nested glob, then gives up with `None`.
    """

    def test_payload_agent_transcript_path_wins_over_flat_derivation_when_both_exist(
        self, tmp_path
    ) -> None:
        from writ.session.transcript_tripwire import resolve_subagent_transcript

        session_id = "parent-sess-precedence"
        agent_id = "worker-9"
        transcript_path = tmp_path / f"{session_id}.jsonl"

        # A flat-derived file that DOES exist, so a wrong implementation that
        # ignores payload["agent_transcript_path"] and always derives the flat
        # path would return this one instead.
        flat_path = tmp_path / session_id / "subagents" / f"agent-{agent_id}.jsonl"
        flat_path.parent.mkdir(parents=True)
        flat_path.write_text("FLAT-DERIVATION-FILE\n")

        # The payload's own path, a different file, also on disk.
        payload_path = tmp_path / "payload-supplied-transcript.jsonl"
        payload_path.write_text("PAYLOAD-SUPPLIED-FILE\n")

        payload = {
            "transcript_path": str(transcript_path),
            "session_id": session_id,
            "agent_id": agent_id,
            "agent_transcript_path": str(payload_path),
        }

        result = resolve_subagent_transcript(payload)

        assert result == payload_path
        assert result != flat_path

    def test_payload_agent_transcript_path_returned_even_when_file_does_not_exist(
        self, tmp_path
    ) -> None:
        from writ.session.transcript_tripwire import resolve_subagent_transcript

        missing = tmp_path / "does-not-exist-agent-transcript.jsonl"
        payload = {
            "transcript_path": str(tmp_path / "parent.jsonl"),
            "session_id": "parent-sess",
            "agent_id": "worker-2",
            "agent_transcript_path": str(missing),
        }

        result = resolve_subagent_transcript(payload)

        assert result == missing
        assert not result.exists()

    def test_empty_string_agent_transcript_path_falls_through_to_flat_derivation(
        self, tmp_path
    ) -> None:
        from writ.session.transcript_tripwire import resolve_subagent_transcript

        session_id = "parent-sess-empty"
        agent_id = "worker-3"
        transcript_path = tmp_path / f"{session_id}.jsonl"
        flat_path = tmp_path / session_id / "subagents" / f"agent-{agent_id}.jsonl"
        flat_path.parent.mkdir(parents=True)
        flat_path.write_text("FLAT-DERIVATION-FILE\n")

        payload = {
            "transcript_path": str(transcript_path),
            "session_id": session_id,
            "agent_id": agent_id,
            "agent_transcript_path": "",
        }

        result = resolve_subagent_transcript(payload)

        assert result == flat_path

    def test_flat_derivation_returned_when_no_agent_transcript_path_key_and_file_exists(
        self, tmp_path
    ) -> None:
        from writ.session.transcript_tripwire import resolve_subagent_transcript

        session_id = "parent-sess-flat"
        agent_id = "worker-4"
        transcript_path = tmp_path / f"{session_id}.jsonl"
        flat_path = tmp_path / session_id / "subagents" / f"agent-{agent_id}.jsonl"
        flat_path.parent.mkdir(parents=True)
        flat_path.write_text("FLAT-DERIVATION-FILE\n")

        payload = {
            "transcript_path": str(transcript_path),
            "session_id": session_id,
            "agent_id": agent_id,
        }

        result = resolve_subagent_transcript(payload)

        assert result == flat_path

    def test_workflow_nested_shape_found_via_glob_when_flat_absent(self, tmp_path) -> None:
        from writ.session.transcript_tripwire import resolve_subagent_transcript

        session_id = "parent-sess-nested"
        agent_id = "worker-5"
        transcript_path = tmp_path / f"{session_id}.jsonl"
        # No flat_path created at all -- only the workflow-nested shape exists.
        nested_path = (
            tmp_path / session_id / "subagents" / "workflows" / "wf_abc123"
            / f"agent-{agent_id}.jsonl"
        )
        nested_path.parent.mkdir(parents=True)
        nested_path.write_text("WORKFLOW-NESTED-FILE\n")

        payload = {
            "transcript_path": str(transcript_path),
            "session_id": session_id,
            "agent_id": agent_id,
        }

        result = resolve_subagent_transcript(payload)

        assert result == nested_path

    def test_returns_none_when_nothing_resolvable(self, tmp_path) -> None:
        from writ.session.transcript_tripwire import resolve_subagent_transcript

        payload = {
            "transcript_path": str(tmp_path / "parent-sess-none.jsonl"),
            "session_id": "parent-sess-none",
            "agent_id": "worker-6",
        }

        result = resolve_subagent_transcript(payload)

        assert result is None

    def test_missing_transcript_path_key_returns_none_without_raising(self) -> None:
        from writ.session.transcript_tripwire import resolve_subagent_transcript

        # No "transcript_path" key at all -- a payload shape nobody anticipated.
        payload = {"session_id": "parent-sess-shapeless", "agent_id": "worker-7"}

        result = resolve_subagent_transcript(payload)

        assert result is None


class TestResolveSubagentTranscriptSameFileGuard:
    """`resolve_subagent_transcript` must refuse to hand back a path that names
    the PARENT session's own transcript, however the candidate was spelled or
    however it was reached.

    On a build where a payload's two transcript keys collapse onto one file,
    the naive candidate IS the parent transcript, and scanning it would flag
    every legitimate user turn in the parent session as foreign input. The
    comparison must be by resolved path (`Path.resolve()`), never by string
    equality: the shell comparison this guard replaces
    (`[ "$AGENT_TRANSCRIPT" != "$PARENT_TRANSCRIPT" ]`) could not see through a
    symlink or a `..`-relative spelling of the identical file, which is exactly
    what the second and third tests below construct.
    """

    def test_returns_none_when_agent_transcript_path_is_the_identical_string_as_transcript_path(
        self, tmp_path
    ) -> None:
        from writ.session.transcript_tripwire import resolve_subagent_transcript

        transcript_path = tmp_path / "parent-sess-identical.jsonl"
        payload = {
            "transcript_path": str(transcript_path),
            "session_id": "parent-sess-identical",
            "agent_id": "worker-identical",
            "agent_transcript_path": str(transcript_path),
        }

        assert resolve_subagent_transcript(payload) is None

    def test_returns_none_when_agent_transcript_path_names_the_same_file_via_a_dotdot_spelling(
        self, tmp_path
    ) -> None:
        """The `agent_transcript_path` arm, spelled through a sibling directory
        and back out via `..` -- a string that differs character-for-character
        from `transcript_path` but resolves to the identical file. A plain
        string comparison would see two different paths and let the scan run
        against the parent's own transcript; `Path.resolve()` must not be
        fooled by the detour.
        """
        from writ.session.transcript_tripwire import resolve_subagent_transcript

        transcript_path = tmp_path / "parent-sess-dotdot.jsonl"
        nested_dir = tmp_path / "nested"
        nested_dir.mkdir()
        dotdot_spelling = nested_dir / ".." / "parent-sess-dotdot.jsonl"
        assert str(dotdot_spelling) != str(transcript_path)  # genuinely different spelling

        payload = {
            "transcript_path": str(transcript_path),
            "session_id": "parent-sess-dotdot",
            "agent_id": "worker-dotdot",
            "agent_transcript_path": str(dotdot_spelling),
        }

        assert resolve_subagent_transcript(payload) is None

    def test_returns_none_when_the_flat_derived_arm_is_a_symlink_to_transcript_path(
        self, tmp_path
    ) -> None:
        """The DERIVED arm (no `agent_transcript_path` key at all, so the flat
        formula from `subagent_transcript_path` is tried): the flat file
        exists, but only as a symlink aliasing the parent's own transcript.
        Same discriminator as the `..` case above, applied to the arm that
        touches the filesystem instead of trusting the payload directly.
        """
        from writ.session.transcript_tripwire import resolve_subagent_transcript

        session_id = "parent-sess-symlink-derived"
        agent_id = "worker-symlink-derived"
        transcript_path = tmp_path / f"{session_id}.jsonl"
        transcript_path.write_text("PARENT-TRANSCRIPT\n")

        flat_path = tmp_path / session_id / "subagents" / f"agent-{agent_id}.jsonl"
        flat_path.parent.mkdir(parents=True)
        flat_path.symlink_to(transcript_path)
        assert str(flat_path) != str(transcript_path)  # genuinely different spelling

        payload = {
            "transcript_path": str(transcript_path),
            "session_id": session_id,
            "agent_id": agent_id,
            # No agent_transcript_path key: forces the flat-derivation arm.
        }

        assert resolve_subagent_transcript(payload) is None


class TestTranscriptAuditCliCommand:
    """`writ transcript audit`, the operator's manual re-run of the same
    predicate the SubagentStop hook runs automatically over a file or a whole
    directory tree. Invoked through `CliRunner` against the real `writ.cli.app`,
    following the pattern already established in tests/test_reconcile_command.py
    and tests/test_doctor.py rather than inventing a new invocation style.
    """

    def test_single_file_with_malformed_turn_reports_one_finding_in_text_mode(
        self, tmp_path
    ) -> None:
        from typer.testing import CliRunner

        from writ.cli import app

        transcript = tmp_path / "one-turn.jsonl"
        _write_jsonl(transcript, [_line([_text("queued keystroke"), _tool_result()])])

        result = CliRunner().invoke(app, ["transcript", "audit", str(transcript)])

        assert result.exit_code == 0, result.output
        assert "1 finding" in result.output
        assert "line 1" in result.output

    def test_single_file_json_output_shape_carries_one_record_and_no_raw_text(
        self, tmp_path
    ) -> None:
        from typer.testing import CliRunner

        from writ.cli import app

        canary = "CLI-AUDIT-CANARY-7724-DO-NOT-LEAK-THIS-STRING"
        transcript = tmp_path / "one-turn.jsonl"
        _write_jsonl(transcript, [_line([_text(canary), _tool_result()])])

        result = CliRunner().invoke(app, ["transcript", "audit", str(transcript), "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["files_scanned"] == 1
        assert payload["files_skipped"] == 0
        assert payload["findings"] == 1
        assert payload["skipped"] == []
        assert len(payload["records"]) == 1
        record = payload["records"][0]
        assert record["line_number"] == 1
        assert record["digest"] == hashlib.sha1(canary.encode()).hexdigest()
        assert record["text_count"] == 1
        assert record["tool_result_count"] == 1
        assert canary not in result.output

    def test_single_readable_file_with_no_findings_exits_zero(self, tmp_path) -> None:
        """Contract split: incompleteness (an unreadable file) and detection
        (a finding) are separate signals on the exit code. This is the
        "zero findings, everything readable" corner of that split -- exit 0
        with nothing to report, contrasted with the two tests above (findings
        present, everything readable -> also exit 0) and the unreadable-file
        test below (nothing unreadable is NOT what drives exit 1; findings
        are not what drives it either).
        """
        from typer.testing import CliRunner

        from writ.cli import app

        transcript = tmp_path / "clean.jsonl"
        _write_jsonl(transcript, [_line([_text("an ordinary prompt")])])

        result = CliRunner().invoke(app, ["transcript", "audit", str(transcript), "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["findings"] == 0
        assert payload["files_skipped"] == 0

    def test_directory_scan_skips_unreadable_file_keeps_scanning_and_reports_skipped_count(
        self, tmp_path
    ) -> None:
        """A directory scan must not abort on the first unreadable file (it
        must still reach every file after it) and must not silently
        under-report: the good file's finding still shows up, and the
        skipped file is counted and named rather than disappearing from the
        totals.

        Exit code contract: an unreadable file makes the audit's answer
        PARTIAL, so it exits 1 -- unlike a finding, which never affects the
        exit code (see `test_single_file_with_malformed_turn_reports_one_finding_in_text_mode`
        and `test_single_readable_file_with_no_findings_exits_zero` above, both
        exit 0 regardless of whether they found something). A caller that
        checks only the exit code must see incompleteness, not read a partial
        scan as a clean one.
        """
        from typer.testing import CliRunner

        from writ.cli import app

        scan_dir = tmp_path / "transcripts"
        scan_dir.mkdir()
        # Sorted (the command uses rglob + sorted()) BEFORE the good file, so a
        # scan that aborts on the first OSError instead of continuing would
        # never reach b_good.jsonl -- the exact discriminator this test exists
        # to catch.
        locked = scan_dir / "a_locked.jsonl"
        good = scan_dir / "b_good.jsonl"
        _write_jsonl(locked, [_line([_text("never read"), _tool_result()])])
        _write_jsonl(good, [_line([_text("queued keystroke"), _tool_result()])])
        locked.chmod(0o000)

        try:
            json_result = CliRunner().invoke(app, ["transcript", "audit", str(scan_dir), "--json"])
            text_result = CliRunner().invoke(app, ["transcript", "audit", str(scan_dir)])
        finally:
            locked.chmod(0o644)

        # Incompleteness drives the exit code...
        assert json_result.exit_code == 1, json_result.output
        assert text_result.exit_code == 1, text_result.output
        # ...but detection is reported in full anyway: the readable file's
        # finding is NOT swallowed just because a sibling was unreadable.
        payload = json.loads(json_result.output)
        assert payload["files_scanned"] == 1
        assert payload["findings"] == 1
        assert payload["files_skipped"] == 1
        assert len(payload["skipped"]) == 1
        assert payload["skipped"][0]["path"] == str(locked)
        assert payload["skipped"][0]["error"]
        assert "AUDIT INCOMPLETE" in text_result.output


# --- Hook-level: writ-subagent-stop.sh invoked as a real subprocess -----------------
#
# Pattern follows tests/test_mark_pending_test_hook.py's _env/_run_hook (the
# established precedent in this suite for driving a hook script via subprocess with
# a synthetic Claude Code payload on stdin): an owned WRIT_CACHE_DIR /
# WRIT_FRICTION_LOG / WRIT_LOG_ROOT under tmp_path per test, so a run of this class
# never reads or writes real session cache or friction-log state.
#
# `agent_transcript_path` is set directly in the synthetic payload (resolution
# priority 1, per TestResolveSubagentTranscript above) rather than standing up the
# flat/nested directory shape, so these tests exercise the hook's own wiring of the
# tripwire without also re-deriving the path logic already pinned above.

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBAGENT_STOP_HOOK = REPO_ROOT / "hooks" / "scripts" / "writ-subagent-stop.sh"


def _hook_env(cache_root: Path) -> dict:
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_root)
    env["WRIT_FRICTION_LOG"] = str(cache_root / "friction.log")
    env["WRIT_LOG_ROOT"] = str(cache_root / "logs")
    env["WRIT_NO_AUTOSTART"] = "1"
    return env


def _run_subagent_stop_hook(payload: dict, cwd: Path, cache_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SUBAGENT_STOP_HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, cwd=str(cwd), env=_hook_env(cache_root), timeout=30,
    )


class TestSubagentStopHookTranscriptTripwireIntegration:
    """writ-subagent-stop.sh is the SubagentStop hook that resolves and scans a
    sub-agent's own transcript for a malformed turn. These pin the hook's
    process-boundary contract: it must never crash, never surface a finding as
    `additionalContext` (Claude Code treats a Stop-family hook's
    additionalContext as a turn block), and never leak transcript content to
    stdout or stderr.

    NOTE: none of these four tests can tell a live tripwire from a disabled
    one -- a hook whose tripwire never runs is exactly as silent on stdout as
    one that runs it correctly, by design (a finding must never surface as
    additionalContext). `TestSubagentStopHookTranscriptTripwireArtifacts`
    below is what actually discriminates that case, by reading the friction
    and errors log rows the hook writes instead of its stdout.
    """

    def _cache_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "writ-cache"
        root.mkdir()
        return root

    def test_hook_exits_zero_when_resolved_transcript_has_malformed_turn(self, tmp_path) -> None:
        cache_root = self._cache_root(tmp_path)
        transcript = tmp_path / "malformed-agent-transcript.jsonl"
        _write_jsonl(transcript, [_line([_text("queued keystroke leaked mid-turn"), _tool_result()])])
        payload = {
            "session_id": "parent-sess-hook-1",
            "agent_id": "worker-hook-1",
            "agent_type": "general-purpose",
            "transcript_path": str(tmp_path / "parent-sess-hook-1.jsonl"),
            "agent_transcript_path": str(transcript),
        }

        result = _run_subagent_stop_hook(payload, tmp_path, cache_root)

        assert result.returncode == 0, result.stderr

    def test_hook_emits_no_additional_context_on_stdout_for_malformed_turn(self, tmp_path) -> None:
        cache_root = self._cache_root(tmp_path)
        transcript = tmp_path / "malformed-agent-transcript.jsonl"
        _write_jsonl(transcript, [_line([_text("queued keystroke leaked mid-turn"), _tool_result()])])
        payload = {
            "session_id": "parent-sess-hook-2",
            "agent_id": "worker-hook-2",
            "agent_type": "general-purpose",
            "transcript_path": str(tmp_path / "parent-sess-hook-2.jsonl"),
            "agent_transcript_path": str(transcript),
        }

        result = _run_subagent_stop_hook(payload, tmp_path, cache_root)

        # Hard requirement, not a preference: a Stop-family hook's
        # additionalContext is treated by Claude Code as a turn block, so a
        # malformed-turn finding must never surface this way on stdout.
        assert "additionalContext" not in result.stdout

    def test_hook_exits_zero_and_reports_nothing_when_transcript_absent(self, tmp_path) -> None:
        cache_root = self._cache_root(tmp_path)
        missing = tmp_path / "no-such-agent-transcript.jsonl"
        payload = {
            "session_id": "parent-sess-hook-3",
            "agent_id": "worker-hook-3",
            "agent_type": "general-purpose",
            "transcript_path": str(tmp_path / "parent-sess-hook-3.jsonl"),
            "agent_transcript_path": str(missing),
        }

        result = _run_subagent_stop_hook(payload, tmp_path, cache_root)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_hook_leaks_no_foreign_text_to_stdout_or_stderr(self, tmp_path) -> None:
        cache_root = self._cache_root(tmp_path)
        canary = "SUBAGENT-STOP-CANARY-9931-DO-NOT-LEAK-THIS-STRING"
        transcript = tmp_path / "malformed-agent-transcript.jsonl"
        _write_jsonl(transcript, [_line([_text(canary), _tool_result()])])
        payload = {
            "session_id": "parent-sess-hook-4",
            "agent_id": "worker-hook-4",
            "agent_type": "general-purpose",
            "transcript_path": str(tmp_path / "parent-sess-hook-4.jsonl"),
            "agent_transcript_path": str(transcript),
        }

        result = _run_subagent_stop_hook(payload, tmp_path, cache_root)

        assert canary not in result.stdout
        assert canary not in result.stderr


def _hook_env_separated_streams(cache_root: Path, project: str) -> dict:
    """Like `_hook_env`, but does NOT set `WRIT_FRICTION_LOG`.

    `WRIT_FRICTION_LOG`, when set, collapses every stream (friction, errors,
    metrics) onto one file (see writ.shared.logging.emit), which is fine for
    stdout-only assertions but hides WHICH stream an event actually reached.
    Setting only `WRIT_LOG_ROOT` + `WRIT_LOG_PROJECT` (bypassing git-identity
    project resolution so the log path is deterministic without a repo)
    routes each event to its real classified file:
    `<root>/<project>/friction.jsonl` for `foreign_input_in_subagent_turn`,
    `<root>/<project>/errors.jsonl` for `critical_error` -- needed to prove
    the critical record specifically reaches the errors stream, not merely a
    shared log that would also hold it under a collapsed setup.
    """
    env = os.environ.copy()
    env.pop("WRIT_FRICTION_LOG", None)
    env["WRIT_CACHE_DIR"] = str(cache_root)
    env["WRIT_LOG_ROOT"] = str(cache_root / "logs")
    env["WRIT_LOG_PROJECT"] = project
    env["WRIT_NO_AUTOSTART"] = "1"
    return env


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        rows.append(json.loads(raw_line))
    return rows


class TestSubagentStopHookTranscriptTripwireArtifacts:
    """Pin the tripwire finding to the ARTIFACT the hook writes -- the friction
    and errors log rows -- rather than to stdout, which is deliberately silent
    on every path (see the class above) and therefore cannot discriminate a
    live tripwire from a disabled one.

    MUTATION THIS CLASS CATCHES. A reviewer once disabled the tripwire
    entirely by replacing its guard in hooks/scripts/writ-subagent-stop.sh
    (`if [ -n "$AGENT_TRANSCRIPT" ] || [ -n "$PARENT_TRANSCRIPT" ]; then`)
    with `if false; then`. Every assertion in
    `TestSubagentStopHookTranscriptTripwireIntegration` above (exit code 0, no
    `additionalContext`, no canary on stdout/stderr) still passed against
    that mutant, because a hook whose tripwire never runs is exactly as
    silent on stdout as one that runs it correctly. The tests below read the
    rows `log_friction_event` / `writ_critical` actually append instead.
    Verified twice independently: copying the hook to a scratch path outside
    this repo, applying the exact `if false` mutation above, and re-running
    `test_friction_row_carries_parent_session_line_number_and_digest`'s
    payload against the mutant produces zero rows in `friction.jsonl` and
    zero in `errors.jsonl`, where the live hook produces exactly one row in
    each -- every assertion below that checks `len(matches) == 1` flips to a
    failure on that mutant.
    """

    def _cache_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "writ-cache"
        root.mkdir()
        return root

    def _project_root(self, cache_root: Path, project: str) -> Path:
        return cache_root / "logs" / project

    def _run(self, payload: dict, tmp_path: Path, cache_root: Path, project: str):
        return subprocess.run(
            ["bash", str(SUBAGENT_STOP_HOOK)],
            input=json.dumps(payload),
            capture_output=True, text=True, cwd=str(tmp_path),
            env=_hook_env_separated_streams(cache_root, project), timeout=30,
        )

    def test_friction_row_carries_parent_session_line_number_and_digest(self, tmp_path) -> None:
        """Fails if the tripwire is disabled (no row lands at all -- see the
        class docstring for the mutation this proves against), if the
        finding is attributed to the AGENT's own throwaway session instead
        of the PARENT's (the hook's stated attribution contract: the finding
        is about foreign input delivered INTO the agent's turn, so the agent
        is the courier, not the party of record), or if line_number/digest
        drift from the transcript's actual content.
        """
        cache_root = self._cache_root(tmp_path)
        project = "tripwire-artifact-test-1"
        text = "queued keystroke leaked mid-turn artifact-check"
        transcript = tmp_path / "agent-transcript.jsonl"
        _write_jsonl(transcript, [_line([_text(text), _tool_result()])])
        parent_session = "parent-sess-artifact-1"
        agent_session = "worker-artifact-1"
        payload = {
            "session_id": parent_session,
            "agent_id": agent_session,
            "agent_type": "general-purpose",
            "transcript_path": str(tmp_path / f"{parent_session}.jsonl"),
            "agent_transcript_path": str(transcript),
        }

        result = self._run(payload, tmp_path, cache_root, project)
        assert result.returncode == 0, result.stderr

        friction_rows = _read_jsonl(self._project_root(cache_root, project) / "friction.jsonl")
        matches = [r for r in friction_rows if r.get("event") == "foreign_input_in_subagent_turn"]

        assert len(matches) == 1, f"expected exactly one tripwire finding, got: {friction_rows}"
        row = matches[0]
        assert row["session"] == parent_session
        assert row["session"] != agent_session
        assert row["line_number"] == 1
        assert row["digest"] == hashlib.sha1(text.encode()).hexdigest()
        assert row["text_count"] == 1
        assert row["tool_result_count"] == 1

    def test_friction_row_carries_no_foreign_text(self, tmp_path) -> None:
        """The whole point of `Finding.to_record()` is that a digest survives
        and the text never does; assert it against the actual serialized row
        on disk (what an operator would grep), not just the in-process
        `Finding` object already covered by `TestFindingPrivacy` above.
        """
        cache_root = self._cache_root(tmp_path)
        project = "tripwire-artifact-test-2"
        canary = "HOOK-FRICTION-ROW-CANARY-2618-DO-NOT-LEAK-THIS-STRING"
        transcript = tmp_path / "agent-transcript.jsonl"
        _write_jsonl(transcript, [_line([_text(canary), _tool_result()])])
        parent_session = "parent-sess-artifact-2"
        payload = {
            "session_id": parent_session,
            "agent_id": "worker-artifact-2",
            "agent_type": "general-purpose",
            "transcript_path": str(tmp_path / f"{parent_session}.jsonl"),
            "agent_transcript_path": str(transcript),
        }

        result = self._run(payload, tmp_path, cache_root, project)
        assert result.returncode == 0, result.stderr

        friction_path = self._project_root(cache_root, project) / "friction.jsonl"
        raw = friction_path.read_text(encoding="utf-8") if friction_path.exists() else ""
        assert canary not in raw

        rows = _read_jsonl(friction_path)
        matches = [r for r in rows if r.get("event") == "foreign_input_in_subagent_turn"]
        assert len(matches) == 1
        assert canary not in json.dumps(matches[0])

    def test_matching_critical_record_reaches_the_errors_stream(self, tmp_path) -> None:
        """`writ_critical` files the SAME finding a second time, forced onto
        the errors stream, so an operator grepping errors sees it without
        knowing the friction stream exists. Fails if the hook stops calling
        `writ_critical` for this finding -- a regression that could leave the
        friction row intact while quietly dropping the operator-facing half,
        or if the record lands on the wrong stream file.
        """
        cache_root = self._cache_root(tmp_path)
        project = "tripwire-artifact-test-3"
        text = "queued keystroke for the errors-stream check"
        transcript = tmp_path / "agent-transcript.jsonl"
        _write_jsonl(transcript, [_line([_text(text), _tool_result()])])
        parent_session = "parent-sess-artifact-3"
        payload = {
            "session_id": parent_session,
            "agent_id": "worker-artifact-3",
            "agent_type": "general-purpose",
            "transcript_path": str(tmp_path / f"{parent_session}.jsonl"),
            "agent_transcript_path": str(transcript),
        }

        result = self._run(payload, tmp_path, cache_root, project)
        assert result.returncode == 0, result.stderr

        errors_rows = _read_jsonl(self._project_root(cache_root, project) / "errors.jsonl")
        critical_rows = [r for r in errors_rows if r.get("event") == "critical_error"]

        assert len(critical_rows) == 1, f"expected exactly one critical record, got: {errors_rows}"
        record = critical_rows[0]
        assert record["session"] == parent_session
        digest = hashlib.sha1(text.encode()).hexdigest()
        assert digest in record.get("message", "")
        assert text not in record.get("message", "")

    def test_flat_derivation_transcript_reachable_with_no_agent_transcript_path_key(
        self, tmp_path
    ) -> None:
        """Regression test for the critical this program just fixed: the old
        shell guard in writ-subagent-stop.sh gated the WHOLE tripwire on
        `agent_transcript_path` being present in the payload, which made
        `resolve_subagent_transcript`'s flat-derivation fallback (arm 2)
        dead code in production. Every other hook-level test in this file
        sets `agent_transcript_path` directly, so that gap could hide again
        even with the resolver itself fully covered at the pure-python level
        in `TestResolveSubagentTranscript` above. This test omits the key
        entirely and lays the malformed transcript out ONLY at the
        flat-derived path, so it fails against any hook build that still
        gates on that payload key instead of running the resolver's
        fallback chain.
        """
        cache_root = self._cache_root(tmp_path)
        project = "tripwire-artifact-test-flat"
        text = "queued keystroke via flat-derived transcript path"
        parent_session = "parent-sess-artifact-flat"
        agent_id = "worker-artifact-flat"
        transcript_path = tmp_path / f"{parent_session}.jsonl"
        flat_transcript = tmp_path / parent_session / "subagents" / f"agent-{agent_id}.jsonl"
        flat_transcript.parent.mkdir(parents=True)
        _write_jsonl(flat_transcript, [_line([_text(text), _tool_result()])])

        payload = {
            "session_id": parent_session,
            "agent_id": agent_id,
            "agent_type": "general-purpose",
            "transcript_path": str(transcript_path),
            # Deliberately no "agent_transcript_path" key at all.
        }

        result = self._run(payload, tmp_path, cache_root, project)
        assert result.returncode == 0, result.stderr

        friction_rows = _read_jsonl(self._project_root(cache_root, project) / "friction.jsonl")
        matches = [r for r in friction_rows if r.get("event") == "foreign_input_in_subagent_turn"]

        assert len(matches) == 1, f"expected exactly one tripwire finding, got: {friction_rows}"
        row = matches[0]
        assert row["session"] == parent_session
        assert row["line_number"] == 1
        assert row["digest"] == hashlib.sha1(text.encode()).hexdigest()

    def test_workflow_nested_transcript_reachable_with_no_agent_transcript_path_key(
        self, tmp_path
    ) -> None:
        """Companion to the flat-derivation regression test above, for arm 3
        of `resolve_subagent_transcript` (the workflow-nested glob). This
        layout is real, not hypothetical: it accounted for 10 of the 42
        SubagentStop payloads captured to build this resolver, so a hook
        that only reaches the flat derivation would still miss a quarter of
        real sub-agent dispatches. Same discriminator as above: no
        `agent_transcript_path` key in the payload, transcript laid out ONLY
        at the workflow-nested path.
        """
        cache_root = self._cache_root(tmp_path)
        project = "tripwire-artifact-test-nested"
        text = "queued keystroke via workflow-nested transcript path"
        parent_session = "parent-sess-artifact-nested"
        agent_id = "worker-artifact-nested"
        transcript_path = tmp_path / f"{parent_session}.jsonl"
        nested_transcript = (
            tmp_path / parent_session / "subagents" / "workflows" / "wf_test123"
            / f"agent-{agent_id}.jsonl"
        )
        nested_transcript.parent.mkdir(parents=True)
        _write_jsonl(nested_transcript, [_line([_text(text), _tool_result()])])

        payload = {
            "session_id": parent_session,
            "agent_id": agent_id,
            "agent_type": "general-purpose",
            "transcript_path": str(transcript_path),
            # Deliberately no "agent_transcript_path" key at all.
        }

        result = self._run(payload, tmp_path, cache_root, project)
        assert result.returncode == 0, result.stderr

        friction_rows = _read_jsonl(self._project_root(cache_root, project) / "friction.jsonl")
        matches = [r for r in friction_rows if r.get("event") == "foreign_input_in_subagent_turn"]

        assert len(matches) == 1, f"expected exactly one tripwire finding, got: {friction_rows}"
        row = matches[0]
        assert row["session"] == parent_session
        assert row["line_number"] == 1
        assert row["digest"] == hashlib.sha1(text.encode()).hexdigest()
