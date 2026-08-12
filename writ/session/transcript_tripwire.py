"""Tripwire for a Claude Code delivery bug Writ cannot fix, only detect.

WHAT THIS DETECTS. The harness can splice a queued user keystroke into a sub-agent's
pending turn. Writ has no way to fix the harness; the most it can do is refuse to let a
recurrence be invisible. The signature is structural, not textual: a `role:"user"`
message whose `content` array holds a bare `{"type":"text"}` element ALONGSIDE a
`{"type":"tool_result"}` element. In a well-formed dispatch, a user message that carries
tool results carries ONLY tool results, so the two shapes never legitimately mix.

MEASURED, not assumed. The predicate below was run over 130 local transcript files,
covering roughly 10,446 `role:"user"` messages with list content. It matched exactly 2
of them (about 0.02%), and neither match was a benign harness sentinel. That measurement
is the whole justification for the design: the shape is rare enough that a match is
worth surfacing, and specific enough that surfacing it is not noise.

Two consequences of that measurement are wired in below:

1. `KNOWN_BENIGN_SENTINELS` ships EMPTY. The corpus produced no benign shape to exempt,
   so inventing entries would only widen a hole. The constant exists so that a future
   MEASURED sentinel has one obvious home.
2. Writ's own hook injections cannot match. In real transcripts they appear as
   TOP-LEVEL jsonl records shaped
   `{"type":"attachment","attachment":{"type":"hook_success"|"hook_additional_context",...}}`,
   never as elements inside a user message's `content` list. The `attachment` arm of the
   predicate is therefore defensive: it guards a nested shape Claude Code could
   plausibly adopt, not one observed today.

Privacy. A finding records a sha1 digest, a length and counts. The foreign text is held
on the `Finding` for one caller only (`writ transcript audit --show-text`, an operator
inspecting a file already on their own disk); `to_record()` never carries it.

Stdlib only. No graph, no network, no daemon, no `writ.server` import, no import-time
side effects: this module has to be safe to load from a hook.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

# Exact bare-text strings that carry the flagged shape benignly. Measured over 130
# transcripts / ~10,446 candidate messages: NO benign shape was found, so this ships
# empty on purpose. It exists so a future measured sentinel has one obvious home, and
# is read through the module attribute (never inlined) so tests can pin the mechanism.
KNOWN_BENIGN_SENTINELS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Finding:
    """One flagged user turn: where it was, what shape it had, and a text fingerprint.

    `text` is the raw offending text and is deliberately excluded from `repr` and from
    `to_record()`. Only an explicit `--show-text` operator request may surface it.
    """

    file_path: Path
    line_number: int
    timestamp: str
    digest: str
    text_length: int
    text_count: int
    tool_result_count: int
    text: str = field(default="", repr=False, compare=False)

    def to_record(self) -> dict:
        """The privacy-safe subset: a fingerprint of the text, never the text."""
        return {
            "path": str(self.file_path),
            "line_number": self.line_number,
            "timestamp": self.timestamp,
            "digest": self.digest,
            "text_length": self.text_length,
            "text_count": self.text_count,
            "tool_result_count": self.tool_result_count,
        }


def _bare_texts_and_tool_results(content: list) -> tuple[list[str], int]:
    """Split a content list into its bare texts and its tool_result count."""
    texts: list[str] = []
    tool_results = 0
    for element in content:
        if not isinstance(element, dict):
            continue
        element_type = element.get("type")
        if element_type == "tool_result":
            tool_results += 1
        elif element_type == "text":
            value = element.get("text")
            if not isinstance(value, str) or not value.strip():
                # NOT A BARE TEXT. A text element with no `text` key, a non-string
                # value, or nothing but whitespace carries zero information: flagging it
                # produced a finding whose whole content was the digest of the empty
                # string with text_length=0, which still burned one of the hook's 5
                # capped slots and fired a critical alert with nothing to show. An empty
                # text is not a spliced keystroke, so it cannot contribute to a flag.
                # CONSIDERED, NOT OBSERVED: the 130-transcript corpus never produced
                # this shape, so this guards a shape Claude Code could emit rather than
                # one measured. Turns that do carry text keep their real text_count.
                continue
            texts.append(value)
        elif element_type == "attachment":
            # DEFENSIVE ARM. A nested attachment element (the hook_additional_context /
            # hook_success markers Writ's own injections carry) is neither a bare text
            # nor a tool_result, so it can never contribute to a flag. The corpus shows
            # Writ injections actually arrive as TOP-LEVEL attachment records, so this
            # guards a shape Claude Code could adopt rather than one observed today.
            continue
    return texts, tool_results


def is_malformed_user_turn(message: dict) -> bool:
    """True when a user turn mixes a bare text element with a tool_result element.

    `message` is the decoded `"message"` object of one transcript line, i.e.
    `{"role": ..., "content": ...}`. Pure: no filesystem, no clock, no state.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        # A bare string content is an ordinary prompt: no tool results to mix with.
        return False

    texts, tool_results = _bare_texts_and_tool_results(content)
    if not texts or not tool_results:
        return False
    # Read the allowlist off the module so a monkeypatch (and a future real entry) is
    # honoured. A turn whose every bare text is an exact allowlisted sentinel is not a
    # spliced keystroke, even though its shape matches.
    if all(text in KNOWN_BENIGN_SENTINELS for text in texts):
        return False
    return True


def _finding_for_line(path: Path, line_number: int, record: dict, message: dict) -> Finding:
    texts, tool_results = _bare_texts_and_tool_results(message.get("content") or [])
    # One bare text is what the corpus showed; join on newline so a multi-text turn
    # still yields one stable digest over exactly the text that was spliced in.
    text = "\n".join(texts)
    timestamp = record.get("timestamp")
    return Finding(
        file_path=path,
        line_number=line_number,
        timestamp=timestamp if isinstance(timestamp, str) else "",
        digest=hashlib.sha1(text.encode()).hexdigest(),
        text_length=len(text),
        text_count=len(texts),
        tool_result_count=tool_results,
        text=text,
    )


def scan_transcript(path) -> list[Finding]:
    """Scan one jsonl transcript and return a Finding per flagged line, in file order.

    Returns `[]` when the file is absent -- sub-agent transcripts are not durable, so a
    missing file is the normal case, not an error. A line that fails to decode is
    skipped and the scan continues, so one corrupt line cannot hide the findings after
    it.
    """
    path = Path(path)
    if not path.is_file():
        return []

    findings: list[Finding] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            message = record.get("message")
            if not isinstance(message, dict) or not is_malformed_user_turn(message):
                continue
            findings.append(_finding_for_line(path, line_number, record, message))
    return findings


def subagent_transcript_path(transcript_path, parent_session_id: str, agent_id: str) -> Path:
    """Derive a sub-agent's transcript path from the PARENT transcript path.

    SubagentStop payloads carry `transcript_path` naming the parent session's
    transcript; the sub-agent's own transcript sits at
    `<dirname(transcript_path)>/<parent_session_id>/subagents/agent-<agent_id>.jsonl`.
    Pure by contract: it computes a path and never touches the filesystem, so callers
    can derive a name for a file that does not exist (or never will).
    """
    return Path(transcript_path).parent / parent_session_id / "subagents" / f"agent-{agent_id}.jsonl"


def _resolves_to_same_file(candidate: Path, parent_transcript) -> bool:
    """True when `candidate` names the same file as the payload's `transcript_path`.

    THE COLLAPSE GUARD, and it is load-bearing. On a build where a payload's two
    transcript keys collapse onto one file, `candidate` IS the parent session's
    transcript, and scanning that would flag every legitimate user turn in the parent
    session -- this project's own parent transcript contains one genuine match of the
    flagged shape, so the guard's absence is not theoretical.

    Compared by resolved path, never by string: the shell comparison this replaces could
    not see through a symlink or through a relative-versus-absolute spelling of the same
    file. `Path.resolve()` is non-strict, so a path that does not exist still compares
    (an absent transcript is the normal case here). Touches the filesystem only to
    resolve the two paths, so it is testable with nothing but those two paths.
    """
    if not parent_transcript:
        return False
    try:
        return candidate.resolve() == Path(parent_transcript).resolve()
    except OSError:
        # A path so broken it cannot be resolved (a symlink cycle, for instance). Fall
        # back to the literal comparison rather than let the guard raise: a resolver
        # that throws would take the whole hook down for a path it merely cannot name.
        return str(candidate) == str(parent_transcript)


def _candidate_subagent_transcript(payload: dict) -> Path | None:
    """The best candidate for the sub-agent's own transcript, before the collapse guard.

    Resolution order, each step earned from 42 captured SubagentStop payloads:

    1. `agent_transcript_path`. Claude Code resolves the sub-agent's own transcript
       itself, and this key was present in 42 of 42 captured payloads -- it is
       authoritative and needs no derivation. Returned even when the file is absent:
       the scan then yields `[]` and the caller no-ops, which is the right outcome for
       a transcript the harness has already deleted.
    2. The flat formula via `subagent_transcript_path`, returned only if it exists.
    3. The workflow fan-out shape, one directory level deeper:
       `<dirname>/<session_id>/subagents/workflows/wf_*/agent-<agent_id>.jsonl`. This
       is real, not hypothetical: it accounted for 10 of the 42 payloads (24%), so a
       resolver that knows only the flat formula misses a quarter of the sub-agents in
       this repo's own usage.
    4. None -- nothing to scan, the caller no-ops.
    """
    direct = payload.get("agent_transcript_path")
    if direct:
        return Path(direct)

    transcript_path = payload.get("transcript_path")
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id")
    if not (transcript_path and session_id and agent_id):
        return None

    flat = subagent_transcript_path(transcript_path, session_id, agent_id)
    if flat.is_file():
        return flat

    workflows = Path(transcript_path).parent / session_id / "subagents" / "workflows"
    for candidate in sorted(workflows.glob(f"wf_*/agent-{agent_id}.jsonl")):
        return candidate
    return None


def resolve_subagent_transcript(payload: dict) -> Path | None:
    """Locate the sub-agent transcript a SubagentStop payload refers to, or None.

    Precedence lives in `_candidate_subagent_transcript`; this function owns the one
    refusal that applies to EVERY arm of it. Whatever arm produced the candidate, if it
    resolves to the same file as `payload["transcript_path"]` the answer is None: that
    file is the PARENT session's transcript, and scanning it would flag legitimate user
    turns as foreign input.

    The guard used to live in the calling hook as a shell string comparison, which had
    the side effect of gating the whole tripwire on `agent_transcript_path` being
    present and so made arms 2 and 3 unreachable in production. It belongs here, where
    precedence is already decided and where it can be tested.
    """
    if not isinstance(payload, dict):
        return None

    candidate = _candidate_subagent_transcript(payload)
    if candidate is None:
        return None
    if _resolves_to_same_file(candidate, payload.get("transcript_path")):
        return None
    return candidate
