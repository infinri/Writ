"""A hook may not read a payload key its event does not deliver.

MEASURED 2026-09-21, build 2.1.278: `SubagentStart` stopped carrying `cwd` and
`transcript_path`, 0 of 40 rows each, against 22 of 47 rows across three August 2026
capture days. Recorded in `docs/reference/claude-code-blackbox.md`.

Writ survived that only by accident of style: `hooks/scripts/writ-subagent-start.sh`
resolves its directory with `pwd -P` rather than reading the payload. Nothing enforced it.
A payload read added tomorrow yields an empty string, and an empty string in a shell hook
is not an error, it is a silently wrong project root.

WHY THE POPULATION IS DERIVED. Exactly one hook is registered under `SubagentStart` today.
Naming it here would make this test blind the moment a second one is added, which is the
failure mode this project has already hit with hardcoded populations. The registered set is
read from `hooks/hooks.json` instead.

WHY THE DETECTOR READS MECHANISMS, NOT WORDS. `writ-subagent-start.sh` contains `cwd`
inside a comment explaining why the payload key is unused. A grep for the word fails on
correct code, and the obvious way to make it pass again is to delete the explanation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = SKILL_ROOT / "hooks" / "hooks.json"
EVENT = "SubagentStart"

# THE SINGLE PLACE TO EDIT IF A BUILD RESTORES A KEY. The live evidence is a capture log
# that does not exist in a clean checkout, so the measurement is carried here as a constant
# with its counts and date rather than re-derived at test time.
#
# `session_id` is deliberately NOT listed. It is absent on only 12 of 300 rows, and
# writ-subagent-start.sh already handles the empty case explicitly, refusing to fall back
# to a machine-wide pointer because inheriting an unrelated parent's gate state is worse
# than inheriting nothing. That is correct behaviour, and flagging it would be a false
# positive that invites someone to "fix" working code.
MEASURED_ABSENT: dict[str, dict[str, str]] = {
    "SubagentStart": {
        "cwd": "0 of 40 rows on 2026-09-21 (build 2.1.278); 22 of 47 across three August 2026 days",
        "transcript_path": "0 of 40 rows on 2026-09-21 (build 2.1.278); 22 of 47 across August 2026",
    },
}


def strip_comments(source: str) -> str:
    """Return shell source with comment text removed.

    A `#` starts a comment only outside quotes, so the scan tracks quote state. This is what
    separates a key that is USED from a key that is EXPLAINED, and the explanation is the
    thing most worth keeping: the real hook's comment about `cwd` is why nobody has
    reintroduced the payload read.
    """
    out = []
    for line in source.splitlines():
        in_single = in_double = False
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def _extraction_patterns(key: str) -> list[re.Pattern[str]]:
    """The mechanisms this project actually uses to pull a field out of a JSON payload.

    Word boundaries matter more than they look: `\\btranscript_path\\b` must NOT match
    inside `agent_transcript_path`, which is a different key that `SubagentStop` really
    does deliver. `_` is a word character, so the boundary does that work.
    """
    k = re.escape(key)
    return [
        # parsed_field "$STDIN_JSON" "cwd"   (the project's own helper)
        re.compile(rf"parsed_field\s+\S+\s+[\"']?\b{k}\b[\"']?"),
        # parsed_fields "$X" VAR=cwd OTHER=y   (possibly continued across lines)
        re.compile(rf"parsed_fields\b(?:[^\n]|\\\n)*?=\s*[\"']?\b{k}\b"),
        # jq -r '.cwd'  /  jq -r ".cwd"
        re.compile(rf"jq\b(?:[^\n]|\\\n)*?\.\s*\b{k}\b"),
        # python: json.load(...).get("cwd")  /  payload["cwd"]
        re.compile(rf"\.get\(\s*[\"']\b{k}\b[\"']"),
        re.compile(rf"\[\s*[\"']\b{k}\b[\"']\s*\]"),
    ]


def extracts_key(source: str, key: str) -> bool:
    """True when `source` pulls `key` out of a JSON payload by any known mechanism."""
    code = strip_comments(source)
    return any(p.search(code) for p in _extraction_patterns(key))


def _registrations(hooks_json: Path) -> dict:
    raw = json.loads(hooks_json.read_text())
    return raw.get("hooks", raw)


def hooks_registered_for(event: str, hooks_json: Path = HOOKS_JSON) -> list[Path]:
    """Return the script paths registered under `event`.

    Raises KeyError when the event has no section at all. A gate that quietly covers an
    empty population is the decayed case this project has been bitten by before, so the
    absence is an error rather than an empty list.
    """
    reg = _registrations(hooks_json)
    if event not in reg:
        raise KeyError(
            f"{hooks_json} declares no '{event}' section, so this check would cover nothing. "
            f"If the event was genuinely removed, delete this test and say why."
        )
    root = hooks_json.resolve().parent.parent
    found: list[Path] = []
    for entry in reg[event]:
        for hook in entry.get("hooks", []):
            command = hook.get("command", "")
            for token in command.replace('"', "").split():
                if token.endswith(".sh"):
                    found.append(Path(token.replace("${CLAUDE_PLUGIN_ROOT}", str(root))))
    return found


def violations(event: str, hooks_json: Path = HOOKS_JSON,
               absent: dict[str, dict[str, str]] | None = None) -> list[str]:
    """Hooks registered under `event` that read a key that event does not deliver.

    The real assertion and every mutation proof call THIS function, so the proofs exercise
    the shipped path rather than a hand-rolled copy of it.
    """
    absent = MEASURED_ABSENT if absent is None else absent
    keys = absent.get(event, {})
    out: list[str] = []
    for script in hooks_registered_for(event, hooks_json):
        if not script.exists():
            continue
        source = script.read_text(errors="replace")
        for key, evidence in keys.items():
            if extracts_key(source, key):
                out.append(f"{script.name} reads '{key}' from the {event} payload ({evidence})")
    return out


def _write_tree(tmp_path: Path, scripts: dict[str, str], event: str = EVENT) -> Path:
    """Build a synthetic hook tree whose shape matches the real one, and return its
    registrations file. Layout is <root>/hooks/hooks.json plus <root>/hooks/scripts/*."""
    hooks_dir = tmp_path / "hooks"
    (hooks_dir / "scripts").mkdir(parents=True, exist_ok=True)
    entries = []
    for name, body in scripts.items():
        (hooks_dir / "scripts" / name).write_text(body)
        entries.append({"matcher": "", "hooks": [
            {"type": "command", "command": f"bash ${{CLAUDE_PLUGIN_ROOT}}/hooks/scripts/{name}"}]})
    path = hooks_dir / "hooks.json"
    path.write_text(json.dumps({"hooks": {event: entries}}))
    return path


READS_CWD = 'CWD=$(parsed_field "$STDIN_JSON" "cwd")\necho "$CWD"\n'
NO_READ = 'CWD=$(pwd -P)\necho "$CWD"\n'


class TestTheRegisteredPopulationIsDerived:
    def test_the_event_has_at_least_one_registered_hook(self):
        assert hooks_registered_for(EVENT), (
            f"no hook is registered under {EVENT}, so this test would pass vacuously"
        )

    def test_a_registrations_file_without_the_event_fails_rather_than_passing_empty(self, tmp_path):
        path = tmp_path / "hooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"Stop": []}}))
        with pytest.raises(KeyError):
            hooks_registered_for(EVENT, path)

    def test_a_newly_registered_hook_is_picked_up_without_editing_this_test(self, tmp_path):
        path = _write_tree(tmp_path, {"one.sh": NO_READ, "two.sh": NO_READ})
        names = {p.name for p in hooks_registered_for(EVENT, path)}
        assert names == {"one.sh", "two.sh"}


class TestNoRegisteredHookReadsAnAbsentKey:
    @pytest.mark.parametrize("key", sorted(MEASURED_ABSENT[EVENT]))
    def test_no_subagent_start_hook_extracts_the_key(self, key):
        found = violations(EVENT, absent={EVENT: {key: MEASURED_ABSENT[EVENT][key]}})
        assert found == [], "; ".join(found)

    def test_the_real_hook_resolves_its_directory_itself(self):
        scripts = [p for p in hooks_registered_for(EVENT) if p.exists()]
        assert scripts, "no readable hook source for the event"
        assert any("pwd -P" in p.read_text(errors="replace") for p in scripts), (
            "no hook under this event resolves its own directory; if the payload key came "
            "back, update MEASURED_ABSENT rather than deleting this assertion"
        )


class TestTheDetectorRecognizesMechanismsNotWords:
    @pytest.mark.parametrize("form,snippet", [
        ("parsed_field", 'X=$(parsed_field "$STDIN_JSON" "cwd")'),
        ("parsed_fields", 'parsed_fields "$STDIN_JSON" \\\n  A=agent_id \\\n  B=cwd'),
        ("jq", "X=$(printf '%s' \"$STDIN_JSON\" | jq -r '.cwd')"),
        ("python3", 'X=$(python3 -c "import sys,json; print(json.load(sys.stdin).get(\'cwd\',\'\'))")'),
    ])
    def test_each_extraction_form_is_detected(self, form, snippet):
        assert extracts_key(snippet, "cwd"), f"{form} extraction was not detected"

    def test_a_key_named_only_in_a_comment_is_not_an_extraction(self):
        source = '# the payload no longer carries cwd, so we call pwd -P instead\nX=$(pwd -P)\n'
        assert not extracts_key(source, "cwd")

    def test_the_real_hook_comment_does_not_trip_the_detector(self):
        hook = SKILL_ROOT / "hooks" / "scripts" / "writ-subagent-start.sh"
        source = hook.read_text(errors="replace")
        assert "cwd" in source, (
            "the real hook no longer mentions cwd at all, so this test no longer proves that a "
            "mention survives the detector; point it at whichever hook carries the explanation"
        )
        assert not extracts_key(source, "cwd")

    def test_the_detector_sees_the_keys_the_real_hook_does_read(self):
        """POSITIVE CONTROL. Every assertion against the real file above expects False, so a
        detector that had stopped working, or a strip_comments that returned nothing, would
        satisfy all of them. This pins that the same detector, on the same file, still finds
        the keys the hook genuinely extracts."""
        hook = SKILL_ROOT / "hooks" / "scripts" / "writ-subagent-start.sh"
        source = hook.read_text(errors="replace")
        assert strip_comments(source).strip(), "stripping left nothing; the negatives above are vacuous"
        for key in ("agent_id", "agent_type", "session_id"):
            assert extracts_key(source, key), (
                f"the detector no longer sees '{key}', which this hook does read, so its "
                f"negative findings prove nothing"
            )

    def test_a_different_key_is_not_matched_by_a_substring(self):
        source = 'AGENT=$(parsed_field "$STDIN_JSON" "agent_transcript_path")'
        assert extracts_key(source, "agent_transcript_path")
        assert not extracts_key(source, "transcript_path")


class TestTheDetectorIsConditional:
    def test_a_synthetic_hook_that_extracts_the_key_fails_by_name(self, tmp_path):
        path = _write_tree(tmp_path, {"leaky.sh": READS_CWD, "clean.sh": NO_READ})
        found = violations(EVENT, path)
        assert len(found) == 1, found
        assert "leaky.sh" in found[0] and "'cwd'" in found[0]

    def test_the_same_tree_with_the_extraction_removed_passes(self, tmp_path):
        path = _write_tree(tmp_path, {"leaky.sh": NO_READ, "clean.sh": NO_READ})
        assert violations(EVENT, path) == []


class TestTheInverseDirection:
    def test_a_transcript_consumer_registered_under_the_event_fails_by_name(self, tmp_path):
        body = 'T=$(parsed_field "$STDIN_JSON" "transcript_path")\ncat "$T"\n'
        path = _write_tree(tmp_path, {"reader.sh": body})
        found = violations(EVENT, path)
        assert len(found) == 1, found
        assert "reader.sh" in found[0] and "'transcript_path'" in found[0]

    def test_the_current_transcript_consumers_are_wired_to_events_that_deliver_it(self):
        reg = _registrations(HOOKS_JSON)
        assert EVENT in reg, f"{EVENT} section missing; this check would cover nothing"
        registered_here = {p.name for p in hooks_registered_for(EVENT)}
        scripts_dir = SKILL_ROOT / "hooks" / "scripts"
        consumers = {
            p.name for p in sorted(scripts_dir.glob("*.sh"))
            if extracts_key(p.read_text(errors="replace"), "transcript_path")
        }
        assert consumers, "no hook reads transcript_path at all; the detector may have decayed"
        overlap = consumers & registered_here
        assert not overlap, (
            f"{sorted(overlap)} read transcript_path but are registered under {EVENT}, "
            f"which does not deliver it"
        )
