"""Cycle 9: reviewer CRITICAL findings block the commit.

Pins every checkbox in capabilities.md, item for item.

The defect being closed: the writ-reviewer returns its verdict to the orchestrator
and nowhere else, so the author of the code decides whether the critic's findings
matter. This suite pins the three pieces that close it:

  1. a parser that survives what the reviewer ACTUALLY emits (prose, then a fenced
     json block) rather than what its contract claims it emits (json only),
  2. recording at SubagentStop, where the harness hands infrastructure the
     reviewer's own output, so the author is never the courier,
  3. a `git commit` arm in the Bash gate that asks for human confirmation while a
     blocking verdict stands.

Per TEST-TDD-001: skeletons approved before implementation. Imports are LOCAL to
each test so a missing module fails RED rather than skipping the file.

Idioms reused from tests/test_bash_write_gate.py (imported, not duplicated):
`_run_hook`, `_seed`, `SKILL_ROOT`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.test_bash_write_gate import SKILL_ROOT, _run_hook, _seed

# bin/lib is not a package (no __init__.py); the repo convention is to put it on
# sys.path and import the bare module name, as tests/test_approval_patterns.py does.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin", "lib"))

STOP_HOOK = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-subagent-stop.sh")


def _sid() -> str:
    return f"review-block-{uuid.uuid4().hex[:8]}"


# The real shape, captured from this session's writ-reviewer run: several
# paragraphs of prose, THEN a fenced json block. The agent contract says "Output
# JSON only. No prose narrative." It is not obeyed, and a parser that trusts the
# contract records nothing on exactly the path that matters.
REAL_REVIEWER_MESSAGE = """I have everything needed. Let me compile the final findings.

## Summary

I reviewed the diff against `plan.md`. Two real defects found, plus one validation
gap in the new config reader.

```json
{
  "spec_compliance": "pass",
  "status": "changes_requested",
  "critical": [],
  "important": [
    {"file": "writ/config.py", "line": 248, "finding": "does not reject nan/inf", "rule_id": null}
  ],
  "minor": []
}
```
"""

BLOCKING_MESSAGE = """Review complete.

```json
{
  "spec_compliance": "fail",
  "status": "changes_requested",
  "critical": [
    {"file": "writ/gate.py", "line": 12, "finding": "auth check removed", "rule_id": "SEC-AUTHZ-RBAC-001"}
  ],
  "important": [],
  "minor": []
}
```
"""

CLEAN_MESSAGE = """```json
{"spec_compliance": "pass", "status": "approved", "critical": [], "important": [], "minor": []}
```"""


# --------------------------------------------------------------------------- #
# 1. Verdict parsing
# --------------------------------------------------------------------------- #
class TestParseVerdict:
    """parse_verdict tolerates the reviewer's real output, not its stated contract."""

    def test_pure_json_message_parses(self) -> None:
        from review_findings import parse_verdict

        raw = json.dumps({
            "spec_compliance": "pass", "status": "approved",
            "critical": [], "important": [], "minor": [],
        })
        verdict = parse_verdict(raw)
        assert verdict["parsed"] is True
        assert verdict["status"] == "approved"

    def test_prose_then_fenced_json_parses(self) -> None:
        """The shape actually captured from a live writ-reviewer run."""
        from review_findings import parse_verdict

        verdict = parse_verdict(REAL_REVIEWER_MESSAGE)
        assert verdict["parsed"] is True
        assert verdict["status"] == "changes_requested"
        assert verdict["critical"] == []
        assert len(verdict["important"]) == 1

    def test_last_fenced_block_wins(self) -> None:
        """A reviewer that shows an example block before its verdict must not
        have the example recorded as the verdict."""
        from review_findings import parse_verdict

        text = (
            "Here is the schema I will follow:\n\n"
            '```json\n{"spec_compliance": "pass", "status": "approved", '
            '"critical": [], "important": [], "minor": []}\n```\n\n'
            "And here is my actual verdict:\n\n" + BLOCKING_MESSAGE
        )
        verdict = parse_verdict(text)
        assert verdict["parsed"] is True
        assert len(verdict["critical"]) == 1

    def test_message_with_no_json_is_unparseable(self) -> None:
        from review_findings import parse_verdict

        verdict = parse_verdict("The diff looks fine to me, ship it.")
        assert verdict["parsed"] is False

    def test_malformed_json_inside_fence_is_unparseable(self) -> None:
        from review_findings import parse_verdict

        verdict = parse_verdict('```json\n{"critical": [ , ]}\n```')
        assert verdict["parsed"] is False

    def test_empty_message_is_unparseable(self) -> None:
        from review_findings import parse_verdict

        assert parse_verdict("")["parsed"] is False

    def test_malformed_last_block_does_not_fall_back_to_an_earlier_example(self) -> None:
        """Review finding: a botched real verdict must not be replaced by a
        well-formed schema example printed earlier in the same message."""
        from review_findings import parse_verdict

        text = (
            "Schema I follow:\n\n"
            '```json\n{"spec_compliance": "pass", "status": "approved", '
            '"critical": [], "important": [], "minor": []}\n```\n\n'
            "My verdict:\n\n"
            '```json\n{"status": "changes_requested", "critical": [ , ]}\n```\n'
        )
        verdict = parse_verdict(text)
        assert verdict["parsed"] is False, (
            "an earlier example block must never stand in for an unparseable verdict"
        )


# --------------------------------------------------------------------------- #
# 2. The blocking decision
# --------------------------------------------------------------------------- #
class TestIsBlocking:
    """Critical blocks (agents/writ-reviewer.md: 'Critical blocks merge')."""

    def test_critical_findings_block(self) -> None:
        from review_findings import is_blocking, parse_verdict

        assert is_blocking(parse_verdict(BLOCKING_MESSAGE)) is True

    def test_important_without_critical_does_not_block(self) -> None:
        """Important is 'should be fixed', not 'blocks merge'."""
        from review_findings import is_blocking, parse_verdict

        assert is_blocking(parse_verdict(REAL_REVIEWER_MESSAGE)) is False

    def test_clean_approval_does_not_block(self) -> None:
        from review_findings import is_blocking, parse_verdict

        assert is_blocking(parse_verdict(CLEAN_MESSAGE)) is False

    def test_unparseable_blocks(self) -> None:
        """Failing to understand the critic must never read as approval."""
        from review_findings import is_blocking, parse_verdict

        assert is_blocking(parse_verdict("no json here")) is True

    def test_no_verdict_at_all_does_not_block(self) -> None:
        """A session where no reviewer ever ran is not blocked: this gate
        enforces a reviewer's verdict, it does not mandate running one."""
        from review_findings import is_blocking

        assert is_blocking(None) is False


# --------------------------------------------------------------------------- #
# 3. Recording: the endpoint
# --------------------------------------------------------------------------- #
class TestReviewFindingsEndpoint:
    def test_post_stores_and_get_returns(self, tmp_path: Path, monkeypatch) -> None:
        from fastapi.testclient import TestClient

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.server import app

        client = TestClient(app)
        sid = _sid()
        resp = client.post(
            f"/session/{sid}/review-findings",
            json={"message": BLOCKING_MESSAGE, "agent_id": "agent-1"},
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        assert resp.json().get("blocking") is True

        got = client.get(f"/session/{sid}/review-findings").json()
        assert got["blocking"] is True
        assert len(got["verdict"]["critical"]) == 1

    def test_later_verdict_replaces_earlier(self, tmp_path: Path, monkeypatch) -> None:
        """Fixing findings and re-running the reviewer lifts the block."""
        from fastapi.testclient import TestClient

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.server import app

        client = TestClient(app)
        sid = _sid()
        client.post(f"/session/{sid}/review-findings",
                    json={"message": BLOCKING_MESSAGE, "agent_id": "a1"})
        client.post(f"/session/{sid}/review-findings",
                    json={"message": CLEAN_MESSAGE, "agent_id": "a2"})
        got = client.get(f"/session/{sid}/review-findings").json()
        assert got["blocking"] is False

    def test_lifting_a_block_is_recorded_in_the_audit_trail(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The command-text guard is a confirmation boundary, not containment, so
        the transition from blocking to clear must leave a trace a human can audit."""
        import review_findings

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        events: list[tuple] = []
        import writ.session.friction as friction

        monkeypatch.setattr(
            friction, "_log_friction_event",
            lambda sid, mode, event, **kw: events.append((event, kw)),
        )
        sid = _sid()
        review_findings.record(sid, BLOCKING_MESSAGE, "agent-1")
        assert not events, "recording a blocking verdict is not a lift"
        review_findings.record(sid, CLEAN_MESSAGE, "agent-2")
        assert [e for e, _ in events] == ["review_block_lifted"]
        assert events[0][1]["clearing_agent_id"] == "agent-2"

    def test_get_with_nothing_recorded_is_not_blocking(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from fastapi.testclient import TestClient

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.server import app

        got = TestClient(app).get(f"/session/{_sid()}/review-findings").json()
        assert got["blocking"] is False
        assert got["verdict"] is None


# --------------------------------------------------------------------------- #
# 4. Recording: the SubagentStop hook (infrastructure, not the author)
# --------------------------------------------------------------------------- #
class TestSubagentStopRecording:
    def _run_stop_hook(self, payload: dict, cache_dir: Path) -> None:
        env = dict(os.environ, WRIT_CACHE_DIR=str(cache_dir))
        subprocess.run(["bash", STOP_HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, cwd=SKILL_ROOT)

    def _recorded(self, sid: str, cache_dir: Path):
        import importlib
        import sys

        if SKILL_ROOT not in sys.path:
            sys.path.insert(0, SKILL_ROOT)
        os.environ["WRIT_CACHE_DIR"] = str(cache_dir)
        cache = importlib.import_module("writ.session.cache")
        return cache._read_cache(sid).get("review_findings_state")

    def test_records_verdict_for_writ_reviewer(self, tmp_path: Path) -> None:
        sid = _sid()
        self._run_stop_hook({
            "hook_event_name": "SubagentStop",
            "session_id": sid,
            "agent_id": "agent-rev-1",
            "agent_type": "writ-reviewer",
            "last_assistant_message": BLOCKING_MESSAGE,
        }, tmp_path)
        state = self._recorded(sid, tmp_path)
        assert state is not None
        assert len(state["verdict"]["critical"]) == 1

    def test_records_nothing_for_other_agent_types(self, tmp_path: Path) -> None:
        """An implementer's stop must not be mistaken for a review verdict."""
        sid = _sid()
        self._run_stop_hook({
            "hook_event_name": "SubagentStop",
            "session_id": sid,
            "agent_id": "agent-impl-1",
            "agent_type": "writ-implementer",
            "last_assistant_message": BLOCKING_MESSAGE,
        }, tmp_path)
        assert self._recorded(sid, tmp_path) is None

    def test_reviewer_with_no_final_message_is_recorded_as_blocking(
        self, tmp_path: Path
    ) -> None:
        """Review finding: skipping the record on an empty message left 'no
        verdict', which does NOT block, so a reviewer that stopped without a final
        word read as approval."""
        from review_findings import is_blocking

        sid = _sid()
        self._run_stop_hook({
            "hook_event_name": "SubagentStop",
            "session_id": sid,
            "agent_id": "agent-rev-3",
            "agent_type": "writ-reviewer",
            "last_assistant_message": "",
        }, tmp_path)
        state = self._recorded(sid, tmp_path)
        assert state is not None, "an empty reviewer message must still be recorded"
        assert is_blocking(state["verdict"]) is True

    def test_unparseable_reviewer_output_is_recorded_as_unparseable(
        self, tmp_path: Path
    ) -> None:
        sid = _sid()
        self._run_stop_hook({
            "hook_event_name": "SubagentStop",
            "session_id": sid,
            "agent_id": "agent-rev-2",
            "agent_type": "writ-reviewer",
            "last_assistant_message": "Looks good to me.",
        }, tmp_path)
        state = self._recorded(sid, tmp_path)
        assert state is not None
        assert state["verdict"]["parsed"] is False


# --------------------------------------------------------------------------- #
# 5. The block: `git commit` in the Bash gate
# --------------------------------------------------------------------------- #
class TestCommitGate:
    def _seed_verdict(self, sid: str, message: str | None) -> None:
        import sys

        if SKILL_ROOT not in sys.path:
            sys.path.insert(0, SKILL_ROOT)
        from review_findings import parse_verdict

        fields = {"mode": "work"}
        if message is not None:
            verdict = parse_verdict(message)
            fields["review_findings_state"] = {
                "verdict": verdict, "agent_id": "a1", "recorded_at": "2026-08-06T00:00:00",
            }
        _seed(sid, **fields)

    def test_commit_asks_while_critical_findings_stand(self, tmp_path: Path) -> None:
        sid = _sid()
        self._seed_verdict(sid, BLOCKING_MESSAGE)
        out = _run_hook('git commit -m "ship it"', sid, str(tmp_path))
        assert out is not None and out.get("permissionDecision") == "ask"
        assert "critical" in out.get("permissionDecisionReason", "").lower()

    def test_commit_allowed_when_verdict_is_clean(self, tmp_path: Path) -> None:
        sid = _sid()
        self._seed_verdict(sid, CLEAN_MESSAGE)
        assert _run_hook('git commit -m "ship it"', sid, str(tmp_path)) is None

    def test_commit_allowed_when_only_important_findings(self, tmp_path: Path) -> None:
        sid = _sid()
        self._seed_verdict(sid, REAL_REVIEWER_MESSAGE)
        assert _run_hook('git commit -m "ship it"', sid, str(tmp_path)) is None

    def test_commit_allowed_when_no_reviewer_ever_ran(self, tmp_path: Path) -> None:
        sid = _sid()
        self._seed_verdict(sid, None)
        assert _run_hook('git commit -m "ship it"', sid, str(tmp_path)) is None

    def test_git_status_is_unaffected(self, tmp_path: Path) -> None:
        sid = _sid()
        self._seed_verdict(sid, BLOCKING_MESSAGE)
        assert _run_hook("git status --porcelain", sid, str(tmp_path)) is None

    @pytest.mark.parametrize("cmd", [
        'grep "git commit" hooks/scripts/writ-bash-write-gate.sh',
        'echo "run git commit when ready"',
        "git log --grep='git commit'",
    ])
    def test_quoted_mention_of_git_commit_does_not_ask(
        self, cmd: str, tmp_path: Path
    ) -> None:
        """Review finding: the case arm is a raw substring match, so a command that
        merely MENTIONS `git commit` reached the ask. A false ask is user-visible
        friction, not a harmless extra spawn."""
        sid = _sid()
        self._seed_verdict(sid, BLOCKING_MESSAGE)
        assert _run_hook(cmd, sid, str(tmp_path)) is None

    @pytest.mark.parametrize("cmd", [
        'git commit -m "x"',
        "git -C /some/repo commit -am wip",
        'cd /tmp && git commit -m "x"',
        'echo hi; git commit -m "x"',
    ])
    def test_real_commit_spellings_still_ask(self, cmd: str, tmp_path: Path) -> None:
        """The boundary check must not lose the spellings it exists to catch."""
        sid = _sid()
        self._seed_verdict(sid, BLOCKING_MESSAGE)
        out = _run_hook(cmd, sid, str(tmp_path))
        assert out is not None and out.get("permissionDecision") == "ask", cmd

    def test_direct_record_invocation_is_refused(self, tmp_path: Path) -> None:
        """The CRITICAL review finding: nothing stopped the agent from writing its
        own clean verdict to clear a real block, which is the defect this cycle
        exists to close, reachable through the enforcement mechanism itself."""
        sid = _sid()
        self._seed_verdict(sid, BLOCKING_MESSAGE)
        out = _run_hook(
            f'python3 bin/lib/review_findings.py record {sid}', sid, str(tmp_path)
        )
        assert out is not None and out.get("permissionDecision") == "deny"

    def test_direct_endpoint_post_is_refused(self, tmp_path: Path) -> None:
        """Same hole through the unauthenticated localhost endpoint."""
        sid = _sid()
        self._seed_verdict(sid, BLOCKING_MESSAGE)
        out = _run_hook(
            f'curl -sX POST http://localhost:8765/session/{sid}/review-findings '
            f"-d '{{}}'", sid, str(tmp_path)
        )
        assert out is not None and out.get("permissionDecision") == "deny"

    @pytest.mark.parametrize("cmd", [
        "git add bin/lib/review_findings.py",
        "git diff bin/lib/review_findings.py",
        "grep -n record bin/lib/review_findings.py",
        "curl -s http://localhost:8765/session/abc/review-findings",
    ])
    def test_non_mutating_use_of_the_recorder_is_allowed(
        self, cmd: str, tmp_path: Path
    ) -> None:
        """The provenance guard must refuse WRITING a verdict, not touching the file.
        A guard broad enough to block `git add` on its own source cannot ship."""
        sid = _sid()
        self._seed_verdict(sid, BLOCKING_MESSAGE)
        assert _run_hook(cmd, sid, str(tmp_path)) is None, cmd

    def test_commit_ask_names_the_finding(self, tmp_path: Path) -> None:
        """The prompt must carry enough to decide without re-reading the review."""
        sid = _sid()
        self._seed_verdict(sid, BLOCKING_MESSAGE)
        out = _run_hook("git commit -am wip", sid, str(tmp_path))
        assert out is not None
        assert "writ/gate.py" in out.get("permissionDecisionReason", "")
