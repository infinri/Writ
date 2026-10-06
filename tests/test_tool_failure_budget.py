"""Program item 7c: the tool-failure budget.

One streak of IDENTICAL failures per agent and tool lives in the session cache
(`tool_failure_streak`). hooks/scripts/writ-tool-failure-record.sh counts on
PostToolUseFailure; hooks/scripts/writ-tool-failure-budget.sh refuses the fourth identical call
on PreToolUse and clears the streak on a successful PostToolUse. The count, the key and the
input hash live in bin/lib/writ_tool_failure.py; the cache file and the hashed input are the
envelope parser's own HOOK_SESSION_ID and HOOK_ENVELOPE.

Every hook test drives the REAL scripts as subprocesses (tests/_hook_runner.py's run_hook and its
fail-closed no-daemon env) against a throwaway WRIT_CACHE_DIR and WRIT_LOG_ROOT under tmp_path.
Nothing is mocked: the cache is a real file, the lock a real flock, and the concurrency test races
real hook processes (ENF-SYS-005: a lock proven against a mock proves nothing).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._hook_runner import hook_env, run_hook, seed_session_cache

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"
BUDGET_HOOK = HOOKS / "writ-tool-failure-budget.sh"
RECORD_HOOK = HOOKS / "writ-tool-failure-record.sh"
BASH_GATE = HOOKS / "writ-bash-write-gate.sh"
HELPER_PATH = REPO / "bin" / "lib" / "writ_tool_failure.py"
HOOKS_JSON = REPO / "hooks" / "hooks.json"

FAILING = {"command": "pytest tests/test_never_passes.py"}
CHANGED = {"command": "pytest tests/test_never_passes.py -x"}
EDIT_INPUT = {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}


def _helper():
    spec = importlib.util.spec_from_file_location("writ_tool_failure_under_test", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def box(tmp_path, monkeypatch):
    cache, logs, proj = tmp_path / "cache", tmp_path / "logs", tmp_path / "proj"
    for d in (cache, logs, proj):
        d.mkdir()
    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache))
    monkeypatch.setenv("WRIT_LOG_ROOT", str(logs))
    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
    monkeypatch.delenv("CLAUDE_TOOL_INPUT", raising=False)
    sid = f"tfb-{uuid.uuid4().hex[:8]}"
    seed_session_cache(str(cache), sid, "work")
    return SimpleNamespace(cache=cache, logs=logs, proj=proj, sid=sid,
                           env=hook_env(cache_dir=str(cache)))


def _envelope(box, event, tool, tool_input, *, sid=None, agent_id="", **extra):
    data = {"session_id": box.sid if sid is None else sid, "hook_event_name": event,
            "tool_name": tool}
    if tool_input is not None:
        data["tool_input"] = tool_input
    if agent_id:
        data["agent_id"] = agent_id
    data.update(extra)
    return json.dumps(data)


def _run(box, script, event, tool, tool_input, *, extra_env=None, **kw):
    proc = run_hook(script, _envelope(box, event, tool, tool_input, **kw),
                    env={**box.env, **(extra_env or {})}, cwd=str(box.proj), timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc


def _fail(box, tool="Bash", tool_input=FAILING, **kw):
    proc = _run(box, RECORD_HOOK, "PostToolUseFailure", tool, tool_input,
                error="Exit code 1", **kw)
    assert proc.stdout == "", f"a PostToolUseFailure hook printed {proc.stdout!r}"


def _succeed(box, tool="Bash", tool_input=FAILING, **kw):
    proc = _run(box, BUDGET_HOOK, "PostToolUse", tool, tool_input, **kw)
    assert proc.stdout == "", f"a PostToolUse reset printed {proc.stdout!r}"


def _pre(box, tool="Bash", tool_input=FAILING, **kw):
    """The PreToolUse verdict: hookSpecificOutput on a refusal, None on a silent allow."""
    proc = _run(box, BUDGET_HOOK, "PreToolUse", tool, tool_input, **kw)
    out = proc.stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def _cache_file(box, sid=None):
    return box.cache / f"writ-session-{sid or box.sid}.json"


def _streaks(box, sid=None):
    return json.loads(_cache_file(box, sid).read_text()).get("tool_failure_streak") or {}


def _rows(box, event):
    rows = []
    for path in box.logs.rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("event") == event:
                rows.append(row)
    return rows


def _seed_child(box, child):
    """A sub-agent cache a real dispatch created."""
    _cache_file(box, child).write_text(json.dumps({
        "mode": "work", "is_subagent": True, "cache_source": "subagent_start",
        "parent_session_id": box.sid, "agent_type": "writ-explorer",
    }))


class TestIdenticalFailuresArmTheRefusal:
    def test_the_fourth_identical_call_is_refused(self, box):
        for _ in range(3):
            _fail(box)
        out = _pre(box)
        assert out is not None and out["permissionDecision"] == "deny", out
        reason = out["permissionDecisionReason"]
        assert reason.startswith("[ENF-TOOL-BUDGET]"), reason
        assert "Bash" in reason and "3 times" in reason
        assert "change the input" in reason.lower()
        assert "prefixing it with ! in the prompt" in reason

    def test_two_identical_failures_do_not_refuse(self, box):
        for _ in range(2):
            _fail(box)
        assert _pre(box) is None

    def test_a_changed_input_is_allowed_after_the_streak(self, box):
        for _ in range(3):
            _fail(box)
        assert _pre(box, tool_input=CHANGED) is None

    def test_a_failure_with_a_new_input_restarts_the_count(self, box):
        helper = _helper()
        for _ in range(2):
            _fail(box)
        _fail(box, tool_input=CHANGED)
        entry = _streaks(box)[helper.streak_key("", "Bash")]
        assert entry["count"] == 1
        assert entry["input_hash"] == helper.input_hash("Bash", CHANGED)
        assert _pre(box, tool_input=CHANGED) is None

    def test_a_non_shell_tool_names_a_non_shell_override(self, box):
        for _ in range(3):
            _fail(box, tool="Edit", tool_input=EDIT_INPUT)
        out = _pre(box, tool="Edit", tool_input=EDIT_INPUT)
        assert out is not None and out["permissionDecision"] == "deny", out
        assert "! in the prompt" not in out["permissionDecisionReason"]
        assert "ask the user" in out["permissionDecisionReason"].lower()

    def test_an_interrupted_call_is_not_counted(self, box):
        for _ in range(3):
            _fail(box, is_interrupt=True)
        assert _streaks(box) == {}
        assert _pre(box) is None

    def test_a_compact_interrupt_spelling_is_not_counted_either(self, box):
        compact = json.dumps({"session_id": box.sid, "hook_event_name": "PostToolUseFailure",
                              "tool_name": "Bash", "tool_input": FAILING,
                              "error": "Interrupted", "is_interrupt": True},
                             separators=(",", ":"))
        for _ in range(3):
            proc = run_hook(RECORD_HOOK, compact, env=box.env, cwd=str(box.proj), timeout=60)
            assert proc.returncode == 0 and proc.stdout == ""
        assert _streaks(box) == {}


class TestBothSidesHashTheParsersInput:
    def test_an_empty_input_is_hashed_alike_on_both_sides(self, box):
        helper = _helper()
        for _ in range(3):
            _fail(box, tool_input=None)
        assert (_streaks(box)[helper.streak_key("", "Bash")]["input_hash"]
                == helper.input_hash("Bash", {}))
        out = _pre(box, tool_input=None)
        assert out is not None and out["permissionDecision"] == "deny", out
        assert _pre(box, tool_input=FAILING) is None

    def test_a_json_string_input_is_normalized_by_the_parser_before_hashing(self, box):
        for _ in range(3):
            _fail(box, tool_input=json.dumps(FAILING))
        out = _pre(box)
        assert out is not None and out["permissionDecision"] == "deny", out

    def test_the_environment_fallback_input_is_what_both_sides_hash(self, box):
        helper = _helper()
        extra = {"CLAUDE_TOOL_INPUT": json.dumps(FAILING)}
        for _ in range(3):
            _fail(box, tool_input=None, extra_env=extra)
        assert (_streaks(box)[helper.streak_key("", "Bash")]["input_hash"]
                == helper.input_hash("Bash", FAILING))
        assert _pre(box, tool_input=None, extra_env=extra) is not None


class TestTheRefusalIsRecorded:
    def test_one_audit_row_and_one_friction_row(self, box):
        for _ in range(3):
            _fail(box)
        assert _pre(box) is not None
        audit = [r for r in _rows(box, "gate_decision") if r.get("gate") == "tool-budget"]
        assert len(audit) == 1 and audit[0]["decision"] == "deny", audit
        assert audit[0]["reason"].startswith("[ENF-TOOL-BUDGET]")
        friction = _rows(box, "tool_budget_denied")
        assert len(friction) == 1, friction
        assert friction[0]["tool"] == "Bash" and int(friction[0]["streak"]) == 3

    def test_the_event_is_mapped_to_the_friction_stream(self):
        from writ.shared.logging import STREAM_MAP
        assert STREAM_MAP["tool_budget_denied"] == "friction"


class TestASuccessEndsTheStreak:
    def test_a_success_of_the_same_tool_clears_its_entry(self, box):
        for _ in range(3):
            _fail(box)
        _succeed(box, tool_input={"command": "ls"})
        assert _streaks(box) == {}
        assert _pre(box) is None

    def test_a_success_of_another_tool_leaves_it_armed(self, box):
        for _ in range(3):
            _fail(box)
        _succeed(box, tool="Read", tool_input={"file_path": "/tmp/x"})
        out = _pre(box)
        assert out is not None and out["permissionDecision"] == "deny", out

    def test_a_successful_file_edit_clears_every_streak_this_agent_holds(self, box):
        for _ in range(3):
            _fail(box)
            _fail(box, tool="WebFetch", tool_input={"url": "https://example.invalid/"})
        _succeed(box, tool="Edit", tool_input=EDIT_INPUT)
        assert _streaks(box) == {}
        assert _pre(box) is None


class TestEachAgentKeepsItsOwnStreak:
    CHILD = "agent-child-1"

    def test_a_sub_agents_failures_never_refuse_the_parent(self, box):
        _seed_child(box, self.CHILD)
        for _ in range(3):
            _fail(box, agent_id=self.CHILD)
        child_out = _pre(box, agent_id=self.CHILD)
        assert child_out is not None and child_out["permissionDecision"] == "deny", child_out
        assert _pre(box) is None
        assert _streaks(box) == {}
        assert list(_streaks(box, self.CHILD)) == [f"{self.CHILD}|Bash"]

    def test_the_parents_failures_never_refuse_a_sub_agent(self, box):
        _seed_child(box, self.CHILD)
        for _ in range(3):
            _fail(box)
        assert _pre(box) is not None
        assert _pre(box, agent_id=self.CHILD) is None

    def test_neither_hook_seeds_a_sub_agent_cache(self, box):
        unseeded = "agent-unseeded-1"
        _pre(box, agent_id=unseeded)
        _succeed(box, agent_id=unseeded)
        _fail(box, agent_id=unseeded)
        assert not _cache_file(box, unseeded).exists(), "a budget hook seeded a sub-agent cache"

    def test_the_helper_writes_the_session_it_is_given(self, box):
        envelope = _envelope(box, "PostToolUseFailure", "Bash", FAILING,
                             sid="someone-else", agent_id="someone-else-child")
        proc = subprocess.run([sys.executable, str(HELPER_PATH), "record", box.sid, ""],
                              input=envelope, capture_output=True, text=True,
                              env=box.env, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert list(_streaks(box)) == ["main|Bash"]


class TestFailOpen:
    def test_no_cache_file_records_nothing_and_allows(self, box):
        ghost = f"tfb-ghost-{uuid.uuid4().hex[:8]}"
        for _ in range(4):
            _fail(box, sid=ghost)
        assert not _cache_file(box, ghost).exists(), "a failure created a session cache"
        assert _pre(box, sid=ghost) is None
        assert not _cache_file(box, ghost).exists()

    def test_an_unreadable_cache_allows_and_is_never_rewritten(self, box):
        helper = _helper()
        corrupt = '{"mode": "work", "%s": {"%s": {"count": 9' % (
            helper.CACHE_KEY, helper.streak_key("", "Bash"))
        _cache_file(box).write_text(corrupt)
        assert _pre(box) is None
        _fail(box)
        _succeed(box)
        assert _cache_file(box).read_text() == corrupt

    def test_an_envelope_with_no_session_id_allows(self, box):
        envelope = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                               "tool_input": FAILING})
        proc = run_hook(BUDGET_HOOK, envelope, env=box.env, cwd=str(box.proj), timeout=60)
        assert proc.returncode == 0 and proc.stdout.strip() == ""


class TestHelperContract:
    def test_the_hash_ignores_key_order(self):
        helper = _helper()
        assert (helper.input_hash("Bash", {"a": 1, "b": 2})
                == helper.input_hash("Bash", {"b": 2, "a": 1}))

    def test_a_non_object_input_hashes_as_the_empty_object(self):
        helper = _helper()
        assert helper.input_hash("Bash", None) == helper.input_hash("Bash", {})

    def test_the_same_input_on_another_tool_hashes_differently(self):
        helper = _helper()
        assert helper.input_hash("Bash", FAILING) != helper.input_hash("Read", FAILING)

    def test_the_hooks_literal_probe_sees_what_the_writer_writes(self, box):
        """The writer/reader contract the hot path rests on: the budget hook decides whether
        to start python with a literal substring test on the cache FILE, so the writer's
        spelling of the key is part of the contract, not a formatting detail."""
        helper = _helper()
        helper.record_failure(box.sid, "", "Bash", FAILING)
        text = _cache_file(box).read_text()
        assert f'"{helper.streak_key("", "Bash")}": {{' in text
        assert f'"{helper.CACHE_KEY}": {{"' in text

    def test_the_default_schema_declares_an_empty_streak_map(self):
        from writ.session.cache import _default_cache
        assert _default_cache()["tool_failure_streak"] == {}


class TestConcurrentFailuresAreAllCounted:
    """ENF-SYS-005: the no-lost-increment claim is proven against real hook processes, a real
    cache file and the real flock in writ.session.cache.mutate_cache, never a mock."""

    N = 8

    def test_parallel_failure_hooks_lose_no_increment(self, box):
        with ThreadPoolExecutor(max_workers=self.N) as pool:
            list(pool.map(lambda _i: _fail(box), range(self.N)))
        helper = _helper()
        assert _streaks(box)[helper.streak_key("", "Bash")]["count"] == self.N


_EXECVE_OK = re.compile(r"\)\s+= 0$")
_EXECVE_PATH = re.compile(r'execve\("([^"]+)"')


def _binaries(trace: str) -> set[str]:
    found = set()
    for line in trace.splitlines():
        match = _EXECVE_PATH.search(line)
        if match and _EXECVE_OK.search(line):
            found.add(os.path.basename(match.group(1)))
    return found


@pytest.mark.skipif(shutil.which("strace") is None,
                    reason="strace unavailable; cannot count execve")
@pytest.mark.skipif(shutil.which("jq") is None,
                    reason="no JSON filter binary: every hook's envelope parse falls back to python")
class TestTheHotPathStartsNoPython:
    @pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
    def test_a_call_with_no_streak_starts_no_python(self, box, event):
        from tests._strace import trace_execve
        env = {**box.env, "HOME": str(box.proj)}
        env.pop("WRIT_BLACKBOX", None)
        trace = trace_execve(["bash", str(BUDGET_HOOK)],
                             input=_envelope(box, event, "Bash", FAILING),
                             env=env, timeout=120)
        python = sum(1 for line in trace.splitlines() if 'python3"' in line)
        assert python == 0, trace[-3000:]
        assert _binaries(trace) <= {"bash", "dirname", "jq"}, sorted(_binaries(trace))


class TestIrreversibleExactRepeat:
    def test_a_second_identical_irreversible_command_is_refused_like_the_first(self, box):
        cmd = {"command": "git reset --hard HEAD~1"}
        for attempt in (1, 2):
            proc = _run(box, BASH_GATE, "PreToolUse", "Bash", cmd)
            out = json.loads(proc.stdout)["hookSpecificOutput"]
            assert out["permissionDecision"] == "deny", (attempt, out)
            assert out["permissionDecisionReason"].startswith("[ENF-IRREVERSIBLE]"), (attempt, out)

    def test_the_budget_adds_no_second_irreversibility_classifier(self):
        for path in (BUDGET_HOOK, RECORD_HOOK, HELPER_PATH):
            text = path.read_text()
            assert "_IRREV_" not in text and "ENF-IRREVERSIBLE" not in text, path.name


class TestRegistration:
    @pytest.mark.parametrize("event, script", [
        ("PostToolUseFailure", "writ-tool-failure-record.sh"),
        ("PreToolUse", "writ-tool-failure-budget.sh"),
        ("PostToolUse", "writ-tool-failure-budget.sh"),
    ])
    def test_registered_once_with_a_catch_all_matcher(self, event, script):
        blocks = json.loads(HOOKS_JSON.read_text())["hooks"][event]
        owners = [b for b in blocks
                  if any(script in h.get("command", "") for h in b.get("hooks", []))]
        assert len(owners) == 1, owners
        assert owners[0]["matcher"] == ".*"
        assert len(owners[0]["hooks"]) == 1

    def test_the_record_script_is_registered_on_no_other_event(self):
        hooks = json.loads(HOOKS_JSON.read_text())["hooks"]
        events = [event for event, blocks in hooks.items() for b in blocks
                  for h in b.get("hooks", [])
                  if "writ-tool-failure-record.sh" in h.get("command", "")]
        assert events == ["PostToolUseFailure"]

    @pytest.mark.parametrize("path", [BUDGET_HOOK, RECORD_HOOK])
    def test_the_scripts_are_executable(self, path):
        assert os.access(path, os.X_OK), path

    @pytest.mark.parametrize("path", [BUDGET_HOOK, RECORD_HOOK])
    def test_neither_script_calls_the_seeding_entry_point(self, path):
        code = "\n".join(line for line in path.read_text().splitlines()
                         if not line.lstrip().startswith("#"))
        assert "load_hook_env" not in code and "writ_seed_subagent_from_fields" not in code
