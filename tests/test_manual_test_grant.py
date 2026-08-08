"""Manual-testing grant: the user-conceded ENF-PROC-TDD-001 bypass.

Covers the three layers end to end, each against its own WRIT_CACHE_DIR so the
store every process resolves is pinned and observable (the 2026-08-03 failure was
two PROCESSES resolving two different stores -- see BUG-manual-test-grant.md):

  1. bin/lib/manual_test_grant.py -- phrase predicate, mint/active/admit/expiry,
     and the `path` CLI the minter logs so a store split shows up in the audit row.
  2. hooks/scripts/writ-manual-test-grant.sh -- the UserPromptSubmit minter,
     including the read-back verification (a `grant` audit row is only written
     after `active` re-reads the grant from disk in a separate process).
  3. hooks/scripts/validate-test-file.sh -- the gate slice: a live grant admits a
     production file the TDD gate would otherwise deny, and records it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
GRANT_LIB = os.path.join(SKILL_ROOT, "bin", "lib", "manual_test_grant.py")
MINTER_SH = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-manual-test-grant.sh")
GATE_SH = os.path.join(SKILL_ROOT, "hooks", "scripts", "validate-test-file.sh")


def _lib(cache: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, WRIT_CACHE_DIR=str(cache))
    return subprocess.run([sys.executable, GRANT_LIB, *args],
                          env=env, capture_output=True, text=True)


def _grant_file(cache: Path, sid: str) -> Path:
    return cache / f"writ-grant-{sid}.json"


def _run_minter(cache: Path, envelope: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ, WRIT_CACHE_DIR=str(cache))
    return subprocess.run(["bash", MINTER_SH], input=json.dumps(envelope),
                          env=env, capture_output=True, text=True)


def _run_gate(cache: Path, repo: Path, envelope: dict) -> dict | None:
    """Run validate-test-file.sh; parsed hookSpecificOutput on deny, None on allow."""
    env = dict(os.environ, WRIT_CACHE_DIR=str(cache))
    p = subprocess.run(["bash", GATE_SH], input=json.dumps(envelope),
                       cwd=str(repo), env=env, capture_output=True, text=True)
    out = p.stdout.strip()
    if not out:
        return None
    return json.loads(out).get("hookSpecificOutput", {})


def _seed_mode(cache: Path, sid: str, mode: str) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"writ-session-{sid}.json").write_text(json.dumps({"mode": mode}))


# --------------------------------------------------------------------------- #
# 1. phrase predicate (exact clauses, never fuzzy)
# --------------------------------------------------------------------------- #
class TestGrantPhrase:
    @pytest.mark.parametrize("prompt", [
        "manual testing approved",
        "Manual Testing Approved",           # case-insensitive
        "ok, manual testing approved then",  # embedded in a sentence
        "i'll test it manually",
        "I will test this manually",
        "manual\n  verification\napproved",  # line-wrapped whitespace collapses
    ])
    def test_grant_phrases_match(self, tmp_path: Path, prompt: str):
        assert _lib(tmp_path, "is-phrase", prompt).returncode == 0, prompt

    @pytest.mark.parametrize("prompt", [
        "", "ok", "approved", "proceed", "go ahead",
        "ok for testing then it should be manual",   # the real near-miss sentence
        "we should do manual testing at some point",  # names manual, no concession
    ])
    def test_non_grant_phrases_do_not_match(self, tmp_path: Path, prompt: str):
        assert _lib(tmp_path, "is-phrase", prompt).returncode == 1, prompt


# --------------------------------------------------------------------------- #
# 2. mint / active / admit / expiry / path against a pinned store
# --------------------------------------------------------------------------- #
class TestGrantLifecycle:
    def test_path_resolves_inside_pinned_cache_dir(self, tmp_path: Path):
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        p = _lib(tmp_path, "path", sid)
        assert p.returncode == 0
        assert p.stdout.strip() == str(_grant_file(tmp_path, sid))

    def test_mint_creates_grant_and_active_reads_it_back(self, tmp_path: Path):
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        assert _lib(tmp_path, "mint", sid, "manual testing approved").returncode == 0
        gf = _grant_file(tmp_path, sid)
        assert gf.is_file()
        grant = json.loads(gf.read_text())
        assert grant["source"] == "user_prompt"
        assert grant["session_id"] == sid
        act = _lib(tmp_path, "active", sid)
        assert act.returncode == 0
        assert 0 < int(act.stdout.strip()) <= 1800

    def test_mint_refuses_non_grant_prompt(self, tmp_path: Path):
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        assert _lib(tmp_path, "mint", sid, "approved").returncode == 1
        assert not _grant_file(tmp_path, sid).exists()

    def test_admit_records_file_once_and_allows(self, tmp_path: Path):
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        _lib(tmp_path, "mint", sid, "manual testing approved")
        assert _lib(tmp_path, "admit", sid, "/proj/app/x.js").returncode == 0
        assert _lib(tmp_path, "admit", sid, "/proj/app/x.js").returncode == 0
        admitted = json.loads(_grant_file(tmp_path, sid).read_text())["admitted"]
        assert admitted == ["/proj/app/x.js"]

    def test_admit_fails_without_grant(self, tmp_path: Path):
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        assert _lib(tmp_path, "admit", sid, "/proj/app/x.js").returncode == 1

    def test_expired_grant_is_not_active(self, tmp_path: Path):
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        _lib(tmp_path, "mint", sid, "manual testing approved")
        gf = _grant_file(tmp_path, sid)
        grant = json.loads(gf.read_text())
        grant["expires_at"] = grant["granted_at"] - 1
        gf.write_text(json.dumps(grant))
        assert _lib(tmp_path, "active", sid).returncode == 1

    def test_grant_copied_to_another_session_is_rejected(self, tmp_path: Path):
        sid_a = f"mtg-{uuid.uuid4().hex[:8]}"
        sid_b = f"mtg-{uuid.uuid4().hex[:8]}"
        _lib(tmp_path, "mint", sid_a, "manual testing approved")
        _grant_file(tmp_path, sid_b).write_text(_grant_file(tmp_path, sid_a).read_text())
        assert _lib(tmp_path, "active", sid_b).returncode == 1


# --------------------------------------------------------------------------- #
# 3. the UserPromptSubmit minter hook
# --------------------------------------------------------------------------- #
class TestMinterHook:
    def test_grant_phrase_mints_and_emits_directive(self, tmp_path: Path):
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        p = _run_minter(tmp_path, {"session_id": sid, "prompt": "manual testing approved"})
        assert p.returncode == 0
        assert "manual-testing grant is live" in p.stdout
        assert _grant_file(tmp_path, sid).is_file()
        assert _lib(tmp_path, "active", sid).returncode == 0

    def test_non_grant_prompt_mints_nothing(self, tmp_path: Path):
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        p = _run_minter(tmp_path, {"session_id": sid, "prompt": "please fix the bug"})
        assert p.returncode == 0
        assert p.stdout.strip() == ""
        assert not _grant_file(tmp_path, sid).exists()

    def test_agent_id_wins_over_session_id(self, tmp_path: Path):
        # Sub-agent isolation: the grant keys on agent_id when present, matching
        # how every gate resolves its session id (parse-hook-stdin contract).
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        aid = f"agent-{uuid.uuid4().hex[:8]}"
        _run_minter(tmp_path, {"session_id": sid, "agent_id": aid,
                               "prompt": "manual testing approved"})
        assert _grant_file(tmp_path, aid).is_file()
        assert not _grant_file(tmp_path, sid).exists()


# --------------------------------------------------------------------------- #
# 4. the gate slice: a live grant admits what ENF-PROC-TDD-001 would deny
# --------------------------------------------------------------------------- #
class TestGateAdmitsGrant:
    def _envelope(self, sid: str, file_path: Path) -> dict:
        return {"session_id": sid, "tool_name": "Write",
                "tool_input": {"file_path": str(file_path)}}

    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        (repo / "app" / "code" / "V" / "M" / "view" / "web" / "js").mkdir(parents=True)
        return repo

    def test_work_mode_js_without_test_denied_without_grant(self, tmp_path: Path):
        cache = tmp_path / "cache"
        repo = self._repo(tmp_path)
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        _seed_mode(cache, sid, "work")
        target = repo / "app" / "code" / "V" / "M" / "view" / "web" / "js" / "widget.js"
        out = _run_gate(cache, repo, self._envelope(sid, target))
        assert out is not None and out.get("permissionDecision") == "deny"
        assert "ENF-PROC-TDD-001" in out.get("permissionDecisionReason", "")

    def test_live_grant_admits_file_and_records_it(self, tmp_path: Path):
        cache = tmp_path / "cache"
        repo = self._repo(tmp_path)
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        _seed_mode(cache, sid, "work")
        _lib(cache, "mint", sid, "manual testing approved")
        target = repo / "app" / "code" / "V" / "M" / "view" / "web" / "js" / "widget.js"
        out = _run_gate(cache, repo, self._envelope(sid, target))
        assert out is None  # gate allowed: no deny JSON emitted
        admitted = json.loads(_grant_file(cache, sid).read_text())["admitted"]
        assert str(target) in admitted

    def test_grant_for_other_session_does_not_admit(self, tmp_path: Path):
        cache = tmp_path / "cache"
        repo = self._repo(tmp_path)
        sid = f"mtg-{uuid.uuid4().hex[:8]}"
        other = f"mtg-{uuid.uuid4().hex[:8]}"
        _seed_mode(cache, sid, "work")
        _lib(cache, "mint", other, "manual testing approved")
        target = repo / "app" / "code" / "V" / "M" / "view" / "web" / "js" / "widget.js"
        out = _run_gate(cache, repo, self._envelope(sid, target))
        assert out is not None and out.get("permissionDecision") == "deny"


# --------------------------------------------------------------------------- #
# 5. inheritance: a dispatched sub-agent acts on the orchestrating session's
#    behalf, so a live parent grant covers it, same contract as gates_approved
#    in writ-subagent-start.sh. Remaining TTL only, never refreshed.
# --------------------------------------------------------------------------- #
SUBAGENT_SH = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-subagent-start.sh")


class TestGrantInheritance:
    def test_child_inherits_live_grant_with_parent_expiry(self, tmp_path: Path):
        parent = f"mtg-{uuid.uuid4().hex[:8]}"
        child = f"agent-{uuid.uuid4().hex[:8]}"
        _lib(tmp_path, "mint", parent, "manual testing approved")
        _lib(tmp_path, "admit", parent, "/proj/app/parent.js")
        assert _lib(tmp_path, "inherit", parent, child).returncode == 0
        pg = json.loads(_grant_file(tmp_path, parent).read_text())
        cg = json.loads(_grant_file(tmp_path, child).read_text())
        assert cg["expires_at"] == pg["expires_at"]
        assert cg["session_id"] == child
        assert cg["inherited_from"] == parent
        assert cg["admitted"] == []          # child records its own admissions
        assert pg["admitted"] == ["/proj/app/parent.js"]  # parent untouched
        assert _lib(tmp_path, "active", child).returncode == 0

    def test_inherit_without_live_parent_grant_fails(self, tmp_path: Path):
        parent = f"mtg-{uuid.uuid4().hex[:8]}"
        child = f"agent-{uuid.uuid4().hex[:8]}"
        assert _lib(tmp_path, "inherit", parent, child).returncode == 1
        assert not _grant_file(tmp_path, child).exists()

    def test_inherit_to_same_session_is_a_refused_noop(self, tmp_path: Path):
        parent = f"mtg-{uuid.uuid4().hex[:8]}"
        _lib(tmp_path, "mint", parent, "manual testing approved")
        _lib(tmp_path, "admit", parent, "/proj/app/x.js")
        assert _lib(tmp_path, "inherit", parent, parent).returncode == 1
        # the live grant's admitted list must not be reset by a self-inherit
        admitted = json.loads(_grant_file(tmp_path, parent).read_text())["admitted"]
        assert admitted == ["/proj/app/x.js"]

    def test_subagent_start_hook_inherits_and_gate_admits(self, tmp_path: Path):
        # Full slice: parent grant -> SubagentStart -> the worker's own Write is
        # admitted by validate-test-file under the worker's agent_id.
        cache = tmp_path / "cache"
        repo = tmp_path / "repo"
        (repo / "app" / "code" / "V" / "M" / "view" / "web" / "js").mkdir(parents=True)
        parent = f"mtg-{uuid.uuid4().hex[:8]}"
        aid = f"agent-{uuid.uuid4().hex[:8]}"
        cache.mkdir(parents=True)
        (cache / f"writ-session-{parent}.json").write_text(json.dumps({"mode": "work"}))
        _lib(cache, "mint", parent, "manual testing approved")
        env = dict(os.environ, WRIT_CACHE_DIR=str(cache))
        envelope = json.dumps({"session_id": parent, "agent_id": aid,
                               "agent_type": "writ-implementer"})
        p = subprocess.run(["bash", SUBAGENT_SH], input=envelope, env=env,
                           cwd=str(repo), capture_output=True, text=True)
        assert p.returncode == 0
        assert _grant_file(cache, aid).is_file()
        target = repo / "app" / "code" / "V" / "M" / "view" / "web" / "js" / "widget.js"
        gate_envelope = {"session_id": parent, "agent_id": aid, "tool_name": "Write",
                         "tool_input": {"file_path": str(target)}}
        out = _run_gate(cache, repo, gate_envelope)
        assert out is None  # admitted under the worker's agent_id
        admitted = json.loads(_grant_file(cache, aid).read_text())["admitted"]
        assert str(target) in admitted


# --------------------------------------------------------------------------- #
# 6. wiring: the minter is registered on UserPromptSubmit. The 2026-08-03
#    failure's second root cause was the user typing the phrase in a session
#    whose hook set predated this registration -- the minter simply never ran.
# --------------------------------------------------------------------------- #
class TestMinterWired:
    def test_minter_registered_on_user_prompt_submit(self):
        hooks_json = Path(SKILL_ROOT, "hooks", "hooks.json")
        data = json.loads(hooks_json.read_text())["hooks"]
        scripts = [h["command"].rsplit("/", 1)[-1]
                   for g in data.get("UserPromptSubmit", []) for h in g.get("hooks", [])]
        assert "writ-manual-test-grant.sh" in scripts
