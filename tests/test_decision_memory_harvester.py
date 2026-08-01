"""Tests for the mechanical decision-memory harvester (writ/session/harvester.py).

Pure-function tests (mocked git, tmp transcripts) + orchestrator tests with a fake
async db double. No real Neo4j: the orchestrator's db calls are asserted on the double,
and the version-fragile transcript parsing is exercised against tmp fixtures.

Capability map:
  [hv-ts]      _parse_ts handles 'Z' and '+00:00' and orders correctly
  [hv-dir]     _project_transcript_dir maps cwd -> ~/.claude/projects/<encoded>
  [hv-plans]   _collect_plan_writes extracts plan.md writes; [] on missing dir (fail soft)
  [hv-gov]     _governing_plan picks the latest plan at or before the commit ts
  [hv-git]     _git_commits parses commits oldest-first with files (mocked _run_git)
  [hv-A]       capability A: no plan -> reason is the commit subject
  [hv-B]       capability B: matching plan -> reason is the plan reason + Decision wiring
  [hv-soft]    fail-soft: path absent from plan -> commit-subject fallback
  [hv-idem]    idempotent ids across re-runs
  [hv-norepo]  ensure_project_registered None -> zeroed stats, no writes
  [hv-fc-*]    3b: mechanical extraction of queried rules from transcripts
  [hv-queried-*] 3b: harvest() populates queried_rule_ids per FileChange
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# --- fake async db double ---------------------------------------------------

class _FakeDB:
    """Records every harvester db call for assertion. All methods are async.

    create_* validate via the REAL Pydantic models (mirroring Neo4jConnection) so a
    missing required field fails here too, not only against live Neo4j. The real db
    auto-stamps FileChange/Commit ts but NOT Decision ts, so the double mirrors that.
    """

    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.commits: list[dict] = []
        self.filechanges: list[dict] = []
        self.edges: list[tuple] = []
        self.resolved: list[tuple] = []

    async def create_decision(self, **kw):
        from writ.graph.schema import Decision
        Decision(**kw)  # Decision.ts is required from the caller (not auto-stamped)
        self.decisions.append(kw)
        return kw["decision_id"]

    async def create_commit(self, **kw):
        from writ.graph.schema import Commit
        kw.setdefault("ts", "2026-01-01T00:00:00Z")  # real db auto-stamps ts
        Commit(**kw)
        self.commits.append(kw)
        return kw["commit_hash"]

    async def create_filechange(self, **kw):
        from writ.graph.schema import FileChange
        kw.setdefault("ts", "2026-01-01T00:00:00Z")  # real db auto-stamps ts
        FileChange(**kw)
        self.filechanges.append(kw)
        return kw["change_id"]

    async def wire_has_decision(self, *a):
        self.edges.append(("HAS_DECISION", a))

    async def wire_governed_by(self, *a):
        self.edges.append(("GOVERNED_BY", a))

    async def wire_has_commit(self, *a):
        self.edges.append(("HAS_COMMIT", a))

    async def wire_has_change(self, *a):
        self.edges.append(("HAS_CHANGE", a))

    async def wire_includes(self, *a):
        self.edges.append(("INCLUDES", a))

    async def wire_motivated_by(self, *a):
        self.edges.append(("MOTIVATED_BY", a))

    async def wire_realizes(self, *a):
        self.edges.append(("REALIZES", a))

    async def resolve_file_claims(self, *a):
        self.resolved.append(a)
        return 1


def _edge_types(db: _FakeDB) -> set[str]:
    return {e[0] for e in db.edges}


def _patch_project(monkeypatch, name: str | None) -> None:
    async def _fake_register(db, cwd, **kw):
        return name
    monkeypatch.setattr("writ.session.harvester.ensure_project_registered", _fake_register)


# --- transcript fixture builders (3b) ---------------------------------------

def _attachment_record(
    *, tool_use_id: str, hook_name: str, fmt: str, rule_text: str,
    basename: str = "harvester.py", ts: str = "2026-06-26T00:00:00Z",
) -> str:
    """One transcript attachment line in Format A/B/C carrying a rule block."""
    block = f"[Writ: file-context rules for {basename}]\n{rule_text}"
    att: dict = {"hookName": hook_name, "toolUseID": tool_use_id}
    if fmt == "A":
        att["type"] = "hook_success"
        att["content"] = block
    elif fmt == "B":
        att["type"] = "hook_success"
        att["content"] = ""
        att["stdout"] = json.dumps(
            {"hookSpecificOutput": {"additionalContext": block}}
        )
    elif fmt == "C":
        att["type"] = "hook_additional_context"
        att["content"] = [block]
    return json.dumps({"type": "attachment", "timestamp": ts, "attachment": att})


def _tool_use_record(
    *, tool_use_id: str, name: str, file_path: str,
    ts: str = "2026-06-26T00:00:00Z",
) -> str:
    """One assistant tool_use line linking a toolUseID to an absolute file path."""
    return json.dumps({
        "type": "assistant", "timestamp": ts,
        "message": {"content": [
            {"type": "tool_use", "id": tool_use_id, "name": name,
             "input": {"file_path": file_path}},
        ]},
    })


# --- pure-function tests ----------------------------------------------------

class TestPureFunctions:

    def test_parse_ts_handles_z_and_offset_and_orders(self) -> None:
        # [hv-ts]
        from writ.session.harvester import _parse_ts
        assert _parse_ts("2026-06-26T00:00:00Z") < _parse_ts("2026-06-26T00:00:01+00:00")
        assert _parse_ts("") < _parse_ts("2026-06-26T00:00:00Z")

    def test_project_transcript_dir_encoding(self) -> None:
        # [hv-dir]: each '/' and '.' becomes '-'.
        from writ.session.harvester import _project_transcript_dir
        got = _project_transcript_dir("/home/u/.claude/skills/writ", Path("/c"))
        assert got == Path("/c/projects/-home-u--claude-skills-writ")

    def test_collect_plan_writes_extracts_and_fails_soft(self, tmp_path: Path) -> None:
        # [hv-plans]: a plan.md Write is extracted; a non-plan Write is ignored.
        from writ.session.harvester import _collect_plan_writes
        line = json.dumps({
            "type": "assistant",
            "timestamp": "2026-06-26T00:00:00Z",
            "message": {"content": [
                {"type": "tool_use", "name": "Write",
                 "input": {"file_path": "/r/plan.md", "content": "PLAN BODY"}},
                {"type": "tool_use", "name": "Write",
                 "input": {"file_path": "/r/other.py", "content": "nope"}},
            ]},
        })
        (tmp_path / "s1.jsonl").write_text(line + "\n")
        got = _collect_plan_writes(tmp_path)
        assert len(got) == 1
        assert got[0]["plan_text"] == "PLAN BODY"

    def test_collect_plan_writes_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        # [hv-plans]: a missing transcript dir yields [] (fail soft, no raise).
        from writ.session.harvester import _collect_plan_writes
        assert _collect_plan_writes(tmp_path / "nope") == []

    def test_governing_plan_picks_latest_at_or_before(self) -> None:
        # [hv-gov]
        from writ.session.harvester import _governing_plan
        plans = [
            {"ts": "2026-06-26T00:00:00Z", "plan_text": "p0"},
            {"ts": "2026-06-26T02:00:00Z", "plan_text": "p2"},
        ]
        assert _governing_plan(plans, "2026-06-26T01:00:00Z")["plan_text"] == "p0"
        assert _governing_plan(plans, "2026-06-26T03:00:00Z")["plan_text"] == "p2"
        assert _governing_plan(plans, "2025-01-01T00:00:00Z") is None

    def test_git_commits_parses_mocked_output(self, monkeypatch) -> None:
        # [hv-git]: _git_commits parses log + show into commit dicts with files.
        from writ.session import harvester

        def fake_run_git(repo, args):
            if args[0] == "rev-parse":
                return "mybranch\n"
            if args[0] == "log":
                return "h1\n"
            if args[0] == "show" and "-s" in args:
                return "h1\x00the subject\x00body\x00alice\x002026-06-26T00:00:00+00:00"
            if args[0] == "show":
                return "M\tfile.py\n"
            return ""

        monkeypatch.setattr(harvester, "_run_git", fake_run_git)
        commits = harvester._git_commits("/repo", None)
        assert len(commits) == 1
        c = commits[0]
        assert c["hash"] == "h1"
        assert c["subject"] == "the subject"
        assert c["branch"] == "mybranch"
        assert c["files"][0]["path"] == "file.py"


# --- orchestrator tests -----------------------------------------------------

class TestHarvestOrchestrator:

    @pytest.mark.asyncio
    async def test_capability_a_fallback_to_commit_subject(self, monkeypatch) -> None:
        # [hv-A]: no governing plan -> FileChange.reason is the commit subject.
        from writ.session import harvester
        _patch_project(monkeypatch, "proj")
        monkeypatch.setattr(harvester, "_collect_plan_writes", lambda d: [])
        monkeypatch.setattr(harvester, "_git_commits", lambda r, s: [
            {"hash": "h1", "subject": "do the thing", "body": "", "author": "a",
             "branch": "b", "ts": "2026-06-26T00:00:00Z",
             "files": [{"path": "f.py", "change_type": "modify"}]},
        ])
        db = _FakeDB()
        stats = await harvester.harvest(db, "/repo")
        assert db.filechanges[0]["reason"] == "do the thing"
        assert stats["commits"] == 1
        assert stats["decisions"] == 0
        assert stats["with_plan_reason"] == 0
        assert stats["fallback_reason"] == 1
        assert "MOTIVATED_BY" not in _edge_types(db)

    @pytest.mark.asyncio
    async def test_capability_b_plan_reason_and_wiring(self, monkeypatch) -> None:
        # [hv-B]: a matching plan -> plan reason + Decision + MOTIVATED_BY/REALIZES/GOVERNED_BY.
        from writ.session import harvester
        _patch_project(monkeypatch, "proj")
        monkeypatch.setattr(harvester, "_collect_plan_writes", lambda d: [
            {"ts": "2026-06-26T00:00:00Z", "plan_text": "PLAN"},
        ])
        monkeypatch.setattr(harvester, "harvest_plan", lambda text: {
            "rationale": "the why", "cited_rules": ["R1"],
            "files": [{"path": "f.py", "change_type": "modify", "reason": "because Y"}],
        })
        monkeypatch.setattr(harvester, "_git_commits", lambda r, s: [
            {"hash": "h1", "subject": "subj", "body": "", "author": "a", "branch": "b",
             "ts": "2026-06-26T01:00:00Z",
             "files": [{"path": "f.py", "change_type": "modify"}]},
        ])
        db = _FakeDB()
        stats = await harvester.harvest(db, "/repo")
        assert db.filechanges[0]["reason"] == "because Y"
        assert stats["decisions"] == 1
        assert stats["with_plan_reason"] == 1
        assert {"MOTIVATED_BY", "REALIZES", "GOVERNED_BY", "HAS_DECISION"} <= _edge_types(db)

    @pytest.mark.asyncio
    async def test_fail_soft_path_not_in_plan(self, monkeypatch) -> None:
        # [hv-soft]: plan present but the committed path is not in it -> subject fallback.
        from writ.session import harvester
        _patch_project(monkeypatch, "proj")
        monkeypatch.setattr(harvester, "_collect_plan_writes", lambda d: [
            {"ts": "2026-06-26T00:00:00Z", "plan_text": "PLAN"},
        ])
        monkeypatch.setattr(harvester, "harvest_plan", lambda text: {
            "rationale": "x", "cited_rules": [],
            "files": [{"path": "other.py", "change_type": "modify", "reason": "r"}],
        })
        monkeypatch.setattr(harvester, "_git_commits", lambda r, s: [
            {"hash": "h1", "subject": "the subject", "body": "", "author": "a",
             "branch": "b", "ts": "2026-06-26T01:00:00Z",
             "files": [{"path": "f.py", "change_type": "modify"}]},
        ])
        db = _FakeDB()
        stats = await harvester.harvest(db, "/repo")
        assert db.filechanges[0]["reason"] == "the subject"
        assert stats["fallback_reason"] == 1
        assert ("MOTIVATED_BY") not in _edge_types(db)

    @pytest.mark.asyncio
    async def test_idempotent_ids_across_runs(self, monkeypatch) -> None:
        # [hv-idem]: re-running harvest derives identical change_id / decision_id.
        from writ.session import harvester
        _patch_project(monkeypatch, "proj")
        monkeypatch.setattr(harvester, "_collect_plan_writes", lambda d: [
            {"ts": "2026-06-26T00:00:00Z", "plan_text": "PLAN"},
        ])
        monkeypatch.setattr(harvester, "harvest_plan", lambda text: {
            "rationale": "x", "cited_rules": [],
            "files": [{"path": "f.py", "change_type": "modify", "reason": "r"}],
        })
        monkeypatch.setattr(harvester, "_git_commits", lambda r, s: [
            {"hash": "h1", "subject": "s", "body": "", "author": "a", "branch": "b",
             "ts": "2026-06-26T01:00:00Z",
             "files": [{"path": "f.py", "change_type": "modify"}]},
        ])
        db1, db2 = _FakeDB(), _FakeDB()
        await harvester.harvest(db1, "/repo")
        await harvester.harvest(db2, "/repo")
        assert db1.filechanges[0]["change_id"] == db2.filechanges[0]["change_id"]
        assert db1.decisions[0]["decision_id"] == db2.decisions[0]["decision_id"]

    @pytest.mark.asyncio
    async def test_transcript_dir_uses_absolute_path(self, monkeypatch) -> None:
        # [hv-abspath]: harvest must resolve repo_cwd to an absolute path before
        # deriving the transcript dir (Claude Code names projects/ by absolute cwd).
        # Regression: passing '.' encoded to 'projects/-' and silently skipped B.
        import os
        from writ.session import harvester
        _patch_project(monkeypatch, "proj")
        seen: dict = {}

        def fake_dir(repo_root, home):
            seen["repo_root"] = repo_root
            return Path("/nonexistent-transcript-dir")

        monkeypatch.setattr(harvester, "_project_transcript_dir", fake_dir)
        monkeypatch.setattr(harvester, "_git_commits", lambda r, s: [])
        db = _FakeDB()
        await harvester.harvest(db, ".")
        assert os.path.isabs(seen["repo_root"]), (
            f"transcript dir must derive from an absolute path; got {seen['repo_root']!r}"
        )

    @pytest.mark.asyncio
    async def test_norepo_returns_zeroed_stats(self, monkeypatch) -> None:
        # [hv-norepo]: ensure_project_registered None -> no writes, zeroed stats.
        from writ.session import harvester
        _patch_project(monkeypatch, None)
        db = _FakeDB()
        stats = await harvester.harvest(db, "/not/a/repo")
        assert stats["project"] is None
        assert stats["commits"] == 0
        assert db.commits == [] and db.filechanges == []


# ---------------------------------------------------------------------------
# Phase 3b: backfill queried_rule_ids per FileChange from the transcript
# ---------------------------------------------------------------------------

class TestFileContextExtraction:
    """Cap [hv-fc-*]: mechanical extraction of queried rules from transcripts."""

    def test_pure_extract_write_plus_hook(self, tmp_path) -> None:
        # [hv-fc-extract]: one Write tool_use + a linked PreToolUse block ->
        # one {file_path, rule_ids} entry keyed by normalize_path(abs).
        from writ.session.harvester import _collect_file_context_rules
        lines = [
            _tool_use_record(tool_use_id="t1", name="Write",
                             file_path="/r/writ/session/harvester.py"),
            _attachment_record(tool_use_id="t1", hook_name="PreToolUse:Write",
                               fmt="C", rule_text="[DOC-ARCH-001] WHEN: ..."),
        ]
        (tmp_path / "s.jsonl").write_text("\n".join(lines) + "\n")
        got = _collect_file_context_rules(tmp_path)
        assert len(got) == 1
        assert got[0]["file_path"] == "r/writ/session/harvester.py"
        assert got[0]["rule_ids"] == ["DOC-ARCH-001"]

    def test_all_three_formats_and_bc_dedup(self, tmp_path) -> None:
        # [hv-fc-variants]: A, B, C all parse; a B and C for the SAME toolUseID
        # dedupe to a single entry preferring C (no double-count).
        from writ.session.harvester import _collect_file_context_rules
        lines = [
            _tool_use_record(tool_use_id="ta", name="Read", file_path="/r/a.py"),
            _attachment_record(tool_use_id="ta", hook_name="PreToolUse:Read",
                               fmt="A", rule_text="[AAA-001] x"),
            _tool_use_record(tool_use_id="tb", name="Edit", file_path="/r/b.py"),
            _attachment_record(tool_use_id="tb", hook_name="PreToolUse:Edit",
                               fmt="B", rule_text="[BBB-001] x"),
            _tool_use_record(tool_use_id="tc", name="Write", file_path="/r/c.py"),
            _attachment_record(tool_use_id="tc", hook_name="PreToolUse:Write",
                               fmt="B", rule_text="[CCC-001] x"),
            _attachment_record(tool_use_id="tc", hook_name="PreToolUse:Write",
                               fmt="C", rule_text="[CCC-001] x"),
        ]
        (tmp_path / "s.jsonl").write_text("\n".join(lines) + "\n")
        got = {e["file_path"]: e["rule_ids"] for e in
               _collect_file_context_rules(tmp_path)}
        assert got == {"r/a.py": ["AAA-001"], "r/b.py": ["BBB-001"],
                       "r/c.py": ["CCC-001"]}

    def test_bc_dedup_c_then_b_b_does_not_override_c(self, tmp_path) -> None:
        # [hv-fc-variants]: the OTHER arrival order -- Format C arrives BEFORE
        # Format B for the same toolUseID. The B must NOT override the C (the
        # guard is `is_c and not prev.fmt_c`), so the kept rule ids are C's.
        from writ.session.harvester import _collect_file_context_rules
        lines = [
            _tool_use_record(tool_use_id="tc", name="Write", file_path="/r/c.py"),
            _attachment_record(tool_use_id="tc", hook_name="PreToolUse:Write",
                               fmt="C", rule_text="[CCC-001] x"),
            _attachment_record(tool_use_id="tc", hook_name="PreToolUse:Write",
                               fmt="B", rule_text="[BBB-999] x"),
        ]
        (tmp_path / "s.jsonl").write_text("\n".join(lines) + "\n")
        got = {e["file_path"]: e["rule_ids"] for e in
               _collect_file_context_rules(tmp_path)}
        assert got == {"r/c.py": ["CCC-001"]}

    def test_basename_resolves_to_full_path(self, tmp_path) -> None:
        # [hv-fc-fullpath]: the block header carries only a basename; the full
        # path comes from the linked tool_use input, NOT the header.
        from writ.session.harvester import _collect_file_context_rules
        lines = [
            _tool_use_record(tool_use_id="t1", name="Write",
                             file_path="/deep/nested/dir/harvester.py"),
            _attachment_record(tool_use_id="t1", hook_name="PreToolUse:Write",
                               fmt="C", rule_text="[RID-001] x",
                               basename="harvester.py"),
        ]
        (tmp_path / "s.jsonl").write_text("\n".join(lines) + "\n")
        got = _collect_file_context_rules(tmp_path)
        assert got[0]["file_path"] == "deep/nested/dir/harvester.py"

    def test_abstract_capture_unknown_exclusion_u2028(self, tmp_path) -> None:
        # [hv-fc-grammar]: ABSTRACT captured as bare id; [UNKNOWN] excluded;
        # U+2028 line separators normalized before matching.
        # GUARD: the normalization must replace the actual U+2028 LINE SEPARATOR
        # (u' '), NOT a literal ASCII space -- replacing a literal space
        # would break the [ABSTRACT: ...] regex (which needs the space after the
        # colon) and this test would then fail on [ABSTRACT: ABS-SECURITY-001].
        from writ.session.harvester import _collect_file_context_rules
        # Embed the actual U+2028 character between tokens so normalization
        # is required to split them into separate lines before matching.
        u2028 = " "
        text = f"[STD-001] a{u2028}[ABSTRACT: ABS-SECURITY-001] b{u2028}[UNKNOWN] c"
        lines = [
            _tool_use_record(tool_use_id="t1", name="Write", file_path="/r/x.py"),
            _attachment_record(tool_use_id="t1", hook_name="PreToolUse:Write",
                               fmt="C", rule_text=text),
        ]
        (tmp_path / "s.jsonl").write_text("\n".join(lines) + "\n")
        got = _collect_file_context_rules(tmp_path)
        assert got[0]["rule_ids"] == ["ABS-SECURITY-001", "STD-001"]

    def test_posttool_blocks_excluded(self, tmp_path) -> None:
        # [hv-fc-pretool]: a PostToolUse block must NOT contribute (it would
        # double-count the same rules).
        from writ.session.harvester import _collect_file_context_rules
        lines = [
            _tool_use_record(tool_use_id="t1", name="Write", file_path="/r/x.py"),
            _attachment_record(tool_use_id="t1", hook_name="PostToolUse:Write",
                               fmt="C", rule_text="[STD-001] x"),
        ]
        (tmp_path / "s.jsonl").write_text("\n".join(lines) + "\n")
        assert _collect_file_context_rules(tmp_path) == []

    def test_fail_soft_missing_dir_and_bad_json(self, tmp_path) -> None:
        # [hv-fc-soft]: missing dir -> []; a garbled line is skipped, valid lines
        # in the same file still parse.
        from writ.session.harvester import _collect_file_context_rules
        assert _collect_file_context_rules(tmp_path / "nope") == []
        lines = [
            '{ this is not json "toolUseID"',
            _tool_use_record(tool_use_id="t1", name="Write", file_path="/r/x.py"),
            _attachment_record(tool_use_id="t1", hook_name="PreToolUse:Write",
                               fmt="C", rule_text="[STD-001] x"),
        ]
        (tmp_path / "s.jsonl").write_text("\n".join(lines) + "\n")
        got = _collect_file_context_rules(tmp_path)
        assert got and got[0]["rule_ids"] == ["STD-001"]

    def test_subagent_transcript_scanned_via_rglob(self, tmp_path) -> None:
        # [hv-fc-rglob]: a sub-agent transcript in a nested dir is scanned.
        from writ.session.harvester import _collect_file_context_rules
        sub = tmp_path / "subagents"
        sub.mkdir()
        lines = [
            _tool_use_record(tool_use_id="t1", name="Write", file_path="/r/x.py"),
            _attachment_record(tool_use_id="t1", hook_name="PreToolUse:Write",
                               fmt="C", rule_text="[SUB-001] x"),
        ]
        (sub / "child.jsonl").write_text("\n".join(lines) + "\n")
        got = _collect_file_context_rules(tmp_path)
        assert got and got[0]["rule_ids"] == ["SUB-001"]

    def test_format_drift_guard(self, tmp_path) -> None:
        # [hv-fc-drift]: a known-good SYNTHETIC fixture -> exact expected rule_ids.
        # Guards the PARSING LOGIC against internal regressions (it goes RED if the
        # extractor changes shape). It does NOT detect a real Anthropic transcript
        # format change -- that would need a captured real-transcript fixture.
        from writ.session.harvester import _collect_file_context_rules
        lines = [
            _tool_use_record(tool_use_id="t1", name="Write",
                             file_path="/r/writ/cli.py"),
            _attachment_record(tool_use_id="t1", hook_name="PreToolUse:Write",
                               fmt="C",
                               rule_text="[PY-PROTO-001] WHEN: x\n[SOLID-LSP-001] y"),
        ]
        (tmp_path / "s.jsonl").write_text("\n".join(lines) + "\n")
        got = _collect_file_context_rules(tmp_path)
        assert got == [{"ts": "2026-06-26T00:00:00Z",
                        "file_path": "r/writ/cli.py",
                        "rule_ids": ["PY-PROTO-001", "SOLID-LSP-001"]}]


class TestGoverningFileContextRules:
    """Cap [hv-fc-window]: window filter + per-file union."""

    def test_window_includes_in_excludes_out(self) -> None:
        # [hv-fc-window]: (since, until] -- strict-left, inclusive-right.
        from writ.session.harvester import (
            _governing_file_context_rules, _parse_ts,
        )
        entries = [
            {"ts": "2026-06-26T00:00:00Z", "file_path": "x.py", "rule_ids": ["A"]},
            {"ts": "2026-06-26T01:00:00Z", "file_path": "x.py", "rule_ids": ["B"]},
            {"ts": "2026-06-26T03:00:00Z", "file_path": "x.py", "rule_ids": ["C"]},
        ]
        since = _parse_ts("2026-06-26T00:00:00Z")
        until = _parse_ts("2026-06-26T02:00:00Z")
        got = _governing_file_context_rules(entries, since, until)
        # 00:00 excluded (strict-left), 01:00 included, 03:00 excluded.
        assert got == {"x.py": ["B"]}

    def test_first_commit_floor_admits_all_at_or_before(self) -> None:
        # [hv-fc-window]: datetime.min floor admits everything at or before until.
        from datetime import datetime, timezone
        from writ.session.harvester import (
            _governing_file_context_rules, _parse_ts,
        )
        entries = [
            {"ts": "2026-06-26T00:00:00Z", "file_path": "x.py", "rule_ids": ["A"]},
            {"ts": "2026-06-26T01:00:00Z", "file_path": "x.py", "rule_ids": ["B"]},
        ]
        floor = datetime.min.replace(tzinfo=timezone.utc)
        until = _parse_ts("2026-06-26T01:00:00Z")
        got = _governing_file_context_rules(entries, floor, until)
        assert got == {"x.py": ["A", "B"]}


# ---------------------------------------------------------------------------
# Item 10 (moved from test_decision_memory_commit.py): harvester queried_rule_ids
# ---------------------------------------------------------------------------

class TestHarvesterQueriedRuleIdsBackfill:
    """Cap [hv-queried-*]: harvest() populates queried_rule_ids from transcripts.

    Renamed from TestHarvesterQueriedRuleIdsEmpty: 3b makes the no-transcript
    case the documented empty path and adds the windowed happy path."""

    @pytest.mark.asyncio
    async def test_no_transcript_leaves_queried_empty(self, monkeypatch) -> None:
        # [hv-queried-empty-1]: no file-context source -> queried_rule_ids = [].
        from writ.session import harvester
        _patch_project(monkeypatch, "proj")
        monkeypatch.setattr(harvester, "_collect_plan_writes", lambda d: [])
        monkeypatch.setattr(harvester, "_collect_file_context_rules", lambda d: [])
        monkeypatch.setattr(harvester, "_git_commits", lambda r, s: [
            {"hash": "h1", "subject": "s", "body": "", "author": "a",
             "branch": "main", "ts": "2026-06-26T10:00:00Z",
             "files": [{"path": "writ/session/harvester.py",
                        "change_type": "modify"}]},
        ])
        db = _FakeDB()
        await harvester.harvest(db, "/repo")
        assert db.filechanges[0]["queried_rule_ids"] == []

    @pytest.mark.asyncio
    async def test_backfill_populates_queried_from_window(self, monkeypatch) -> None:
        # [hv-queried-backfill]: an in-window transcript block -> the committed
        # file's queried_rule_ids carries those rule ids, keyed by abs path.
        import os
        from writ.session import harvester
        from writ.session.remote_parse import normalize_path
        _patch_project(monkeypatch, "proj")
        monkeypatch.setattr(harvester, "_collect_plan_writes", lambda d: [])
        key_cwd = os.path.abspath(os.path.realpath("/repo"))
        abs_path = normalize_path(os.path.join(key_cwd, "f.py"))
        monkeypatch.setattr(harvester, "_collect_file_context_rules", lambda d: [
            {"ts": "2026-06-26T00:30:00Z", "file_path": abs_path,
             "rule_ids": ["DOC-ARCH-001"]},
        ])
        monkeypatch.setattr(harvester, "_git_commits", lambda r, s: [
            {"hash": "h1", "subject": "s", "body": "", "author": "a",
             "branch": "main", "ts": "2026-06-26T01:00:00Z",
             "files": [{"path": "f.py", "change_type": "modify"}]},
        ])
        db = _FakeDB()
        await harvester.harvest(db, "/repo")
        assert db.filechanges[0]["queried_rule_ids"] == ["DOC-ARCH-001"]

    @pytest.mark.asyncio
    async def test_per_commit_window_isolation(self, monkeypatch) -> None:
        # [hv-queried-isolation]: a block before the first commit attaches to it,
        # not to the second; each commit's window is disjoint.
        import os
        from writ.session import harvester
        from writ.session.remote_parse import normalize_path
        _patch_project(monkeypatch, "proj")
        monkeypatch.setattr(harvester, "_collect_plan_writes", lambda d: [])
        key_cwd = os.path.abspath(os.path.realpath("/repo"))
        f1 = normalize_path(os.path.join(key_cwd, "a.py"))
        f2 = normalize_path(os.path.join(key_cwd, "b.py"))
        monkeypatch.setattr(harvester, "_collect_file_context_rules", lambda d: [
            {"ts": "2026-06-26T00:30:00Z", "file_path": f1, "rule_ids": ["R1"]},
            {"ts": "2026-06-26T01:30:00Z", "file_path": f2, "rule_ids": ["R2"]},
        ])
        monkeypatch.setattr(harvester, "_git_commits", lambda r, s: [
            {"hash": "h1", "subject": "s1", "body": "", "author": "a",
             "branch": "main", "ts": "2026-06-26T01:00:00Z",
             "files": [{"path": "a.py", "change_type": "modify"}]},
            {"hash": "h2", "subject": "s2", "body": "", "author": "a",
             "branch": "main", "ts": "2026-06-26T02:00:00Z",
             "files": [{"path": "b.py", "change_type": "modify"}]},
        ])
        db = _FakeDB()
        await harvester.harvest(db, "/repo")
        by_hash = {fc["commit_hash"]: fc["queried_rule_ids"]
                   for fc in db.filechanges}
        assert by_hash["h1"] == ["R1"]
        assert by_hash["h2"] == ["R2"]


# ---------------------------------------------------------------------------
# Phase 3c: _resolve_rev validation + --since robustness
# ---------------------------------------------------------------------------

class TestHarvestSinceValidation:
    """harvest --since must be a git revision; a non-rev fails clearly (Phase 3c)."""

    def test_resolve_rev_raises_value_error_on_non_rev(self, monkeypatch) -> None:
        # [hv-since-bad]: a date-string since -> clear ValueError, not CalledProcessError.
        from writ.session import harvester

        class _Res:
            returncode = 128
            stdout = ""
            stderr = "fatal: bad revision\n"

        def fake_run(args, **kw):
            assert "--verify" in args and "--quiet" in args
            return _Res()

        monkeypatch.setattr(harvester.subprocess, "run", fake_run)
        with pytest.raises(ValueError) as exc:
            harvester._resolve_rev("/repo", "2026-06-15")
        msg = str(exc.value)
        assert "--since" in msg and "date" in msg

    def test_resolve_rev_accepts_valid_rev(self, monkeypatch) -> None:
        # [hv-since-good]: a real rev resolves quietly (no raise).
        from writ.session import harvester

        class _Res:
            returncode = 0
            stdout = "abc123\n"
            stderr = ""

        monkeypatch.setattr(harvester.subprocess, "run", lambda args, **kw: _Res())
        harvester._resolve_rev("/repo", "HEAD~3")  # must not raise

    def test_git_commits_validates_since_before_log(self, monkeypatch) -> None:
        # [hv-since-range]: a valid since builds <since>..HEAD; resolve runs first.
        from writ.session import harvester

        calls: list[list[str]] = []

        def fake_run_git(repo, args):
            calls.append(args)
            if args[0] == "rev-parse" and "--abbrev-ref" in args:
                return "main\n"
            if args[0] == "log":
                assert "v1.0..HEAD" in args
                return ""  # no commits; the range is what we assert
            return ""

        # valid rev: _resolve_rev passes, _git_commits builds the range
        monkeypatch.setattr(harvester, "_resolve_rev", lambda repo, since: None)
        monkeypatch.setattr(harvester, "_run_git", fake_run_git)
        commits = harvester._git_commits("/repo", "v1.0")
        assert commits == []
        assert any(a[0] == "log" for a in calls)

    def test_git_commits_raises_on_bad_since(self, monkeypatch) -> None:
        # [hv-since-guard]: _git_commits surfaces the _resolve_rev ValueError.
        from writ.session import harvester

        def bad_resolve(repo, since):
            raise ValueError("--since must be a git revision ... not a date")

        monkeypatch.setattr(harvester, "_resolve_rev", bad_resolve)
        monkeypatch.setattr(harvester, "_run_git", lambda r, a: "")
        with pytest.raises(ValueError):
            harvester._git_commits("/repo", "2026-06-15")

    def test_harvest_cmd_maps_bad_since_to_bad_parameter_before_db(self, monkeypatch) -> None:
        # [hv-since-cli]: harvest_cmd validates --since up front and maps the
        # _resolve_rev ValueError to a clean typer parameter error, BEFORE opening
        # the db -- so an internal ValueError can never be mislabeled as --since and
        # no Neo4j connection is touched on the bad-input path.
        from typer.testing import CliRunner
        from writ.cli import app
        from writ.session import harvester

        def bad_resolve(repo, since):
            raise ValueError("--since must be a commit, branch, or tag, not a date")

        monkeypatch.setattr(harvester, "_resolve_rev", bad_resolve)
        result = CliRunner().invoke(app, ["harvest", "--since", "2026-06-15", "--repo", "."])
        assert result.exit_code != 0
        from tests._ansi import plain

        assert "--since" in plain(result.output)


# ---------------------------------------------------------------------------
# Unplanned-file fix: flag + honest-prefix (plan.md unplanned-file fix)
# ---------------------------------------------------------------------------
# Capability map additions:
#   [hv-unplanned-b]  state (b) partial plan -> unplanned_files flag + honest prefix
#   [hv-unplanned-a]  state (a) no plan -> no flag, bare subject (GREEN)
#   [hv-unplanned-c]  state (c) stale plan -> no flag, bare subject (GREEN)
#   [hv-unplanned-prior] prior-claim rescue -> not flagged, prior reason verbatim (GREEN)

class TestUnplannedFileFlag:
    """Tests for the unplanned-file fix in harvest_one_commit.

    Tests [hv-unplanned-b] is RED before the implementation: stats["unplanned_files"]
    does not exist yet and the honest prefix is not yet stored.
    Tests [hv-unplanned-a], [hv-unplanned-c], [hv-unplanned-prior] are GREEN before
    and after the fix (they use stats.get("unplanned_files", []) to avoid KeyError).
    """

    @pytest.mark.asyncio
    async def test_partial_plan_flags_unplanned_file_and_honest_prefix(
        self, monkeypatch
    ) -> None:
        # [hv-unplanned-b] (RED): a plan governs the commit (names planned.py with a
        # reason), and a second committed file (unplanned.py) is NOT in the plan's
        # ## Files section. After the fix:
        #   - unplanned.py's FileChange.reason starts with the honest prefix
        #   - unplanned.py's FileChange.reason contains the commit subject
        #   - stats["unplanned_files"] == ["unplanned.py"]
        #   - planned.py's FileChange.reason is the plan reason (NO prefix)
        #   - planned.py is NOT in stats["unplanned_files"]
        from writ.session import harvester

        subject = "feat: add widget and untracked helper"
        plan_text = (
            "## Files\n\n"
            "- `planned.py` (modify) -- the real per-file plan reason\n"
        )
        files = [
            {"path": "planned.py", "change_type": "modify"},
            {"path": "unplanned.py", "change_type": "add"},
        ]

        # Monkeypatch harvest_plan to return an explicit reason for planned.py only.
        monkeypatch.setattr(
            harvester, "harvest_plan",
            lambda text: {
                "rationale": "add widget",
                "cited_rules": [],
                "files": [{"path": "planned.py", "change_type": "modify",
                            "reason": "the real per-file plan reason"}],
            },
        )

        db = _FakeDB()
        stats = await harvester.harvest_one_commit(
            db, "proj",
            commit_hash="unpl-b-001",
            subject=subject,
            author="tester",
            branch="main",
            commit_ts="2026-06-30T10:00:00Z",
            files=files,
            plan_text=plan_text,
            plan_ts="2026-06-30T09:00:00Z",
        )

        fc_by_path = {fc["path"]: fc for fc in db.filechanges}

        # planned.py must carry the plan reason (no prefix).
        planned_reason = fc_by_path["planned.py"]["reason"]
        assert planned_reason == "the real per-file plan reason", (
            f"planned.py FileChange.reason must be the plan reason; got {planned_reason!r}"
        )

        # unplanned.py must carry the honest prefix + subject.
        honest_prefix = "(not itemized in the plan; commit-level context) "
        unplanned_reason = fc_by_path["unplanned.py"]["reason"]
        assert unplanned_reason.startswith(honest_prefix), (
            f"unplanned.py reason must start with honest prefix; got {unplanned_reason!r}"
        )
        assert subject in unplanned_reason, (
            f"unplanned.py reason must contain the subject {subject!r}; "
            f"got {unplanned_reason!r}"
        )

        # stats["unplanned_files"] must list exactly the unplanned path.
        unplanned = stats["unplanned_files"]
        assert unplanned == ["unplanned.py"], (
            f"stats['unplanned_files'] must be ['unplanned.py']; got {unplanned!r}"
        )

        # planned.py must NOT appear in unplanned_files.
        assert "planned.py" not in unplanned, (
            "planned.py (which has a plan reason) must not appear in unplanned_files"
        )

    @pytest.mark.asyncio
    async def test_no_plan_no_unplanned_flag(self, monkeypatch) -> None:
        # [hv-unplanned-a] (GREEN before+after): plan_text=None -> decision_id stays
        # None -> the else branch keeps the bare subject -> no unplanned_files flag.
        from writ.session import harvester

        subject = "fix: bare subject fallback"
        files = [{"path": "f.py", "change_type": "modify"}]

        db = _FakeDB()
        stats = await harvester.harvest_one_commit(
            db, "proj",
            commit_hash="unpl-a-001",
            subject=subject,
            author="tester",
            branch="main",
            commit_ts="2026-06-30T10:00:00Z",
            files=files,
            plan_text=None,
            plan_ts=None,
        )

        assert db.filechanges[0]["reason"] == subject, (
            f"no-plan: reason must be bare subject; got {db.filechanges[0]['reason']!r}"
        )
        # Use .get so this passes before the key is added (pre-fix).
        assert stats.get("unplanned_files", []) == [], (
            f"no-plan: unplanned_files must be empty; got {stats.get('unplanned_files')!r}"
        )

    @pytest.mark.asyncio
    async def test_stale_plan_no_unplanned_flag(self, monkeypatch) -> None:
        # [hv-unplanned-c] (GREEN before+after): a plan exists but names only
        # other.py (not in the commit); parsed_files empty -> decision_id None ->
        # the else branch keeps the bare subject -> no unplanned_files flag.
        # This is the state-(c) mirror of test_fail_soft_path_not_in_plan (unchanged).
        from writ.session import harvester

        subject = "fix: stale plan subject"
        files = [{"path": "f.py", "change_type": "modify"}]

        # harvest_plan returns a file NOT in the commit's files.
        monkeypatch.setattr(
            harvester, "harvest_plan",
            lambda text: {
                "rationale": "x", "cited_rules": [],
                "files": [{"path": "other.py", "change_type": "modify",
                            "reason": "stale reason"}],
            },
        )

        db = _FakeDB()
        stats = await harvester.harvest_one_commit(
            db, "proj",
            commit_hash="unpl-c-001",
            subject=subject,
            author="tester",
            branch="main",
            commit_ts="2026-06-30T10:00:00Z",
            files=files,
            plan_text="## Files\n\n- `other.py` (modify) -- stale reason\n",
            plan_ts="2026-06-30T09:00:00Z",
        )

        assert db.filechanges[0]["reason"] == subject, (
            f"stale-plan: reason must be bare subject; got {db.filechanges[0]['reason']!r}"
        )
        assert stats.get("unplanned_files", []) == [], (
            f"stale-plan: unplanned_files must be empty; got "
            f"{stats.get('unplanned_files')!r}"
        )

    @pytest.mark.asyncio
    async def test_prior_claim_rescue_not_flagged(self, monkeypatch) -> None:
        # [hv-unplanned-prior] (GREEN before+after): a file has a prior-claim reason
        # and is NOT in the governing plan, but the plan DOES name another committed
        # file (so decision_id is set). The rescued file takes the prior reason branch
        # (never reaching the bare-subject else), so it is not prefixed and not in
        # stats["unplanned_files"].
        from writ.session import harvester

        subject = "feat: two files"
        prior_reason = "pre-existing open Decision reason"
        files = [
            {"path": "governed.py", "change_type": "modify"},
            {"path": "rescued.py", "change_type": "modify"},
        ]
        # Plan names governed.py only -> decision_id is set.
        # rescued.py has a prior_claims entry with a real reason.
        plan_text = "## Files\n\n- `governed.py` (modify) -- the governed reason\n"
        prior_claims = [
            {"path": "rescued.py", "reason": prior_reason,
             "decision_id": "DEC-prior-aabbcc112233",
             "governing_rule_ids": []},
        ]

        monkeypatch.setattr(
            harvester, "harvest_plan",
            lambda text: {
                "rationale": "governs governed.py",
                "cited_rules": [],
                "files": [{"path": "governed.py", "change_type": "modify",
                            "reason": "the governed reason"}],
            },
        )

        db = _FakeDB()
        stats = await harvester.harvest_one_commit(
            db, "proj",
            commit_hash="unpl-prior-001",
            subject=subject,
            author="tester",
            branch="main",
            commit_ts="2026-06-30T10:00:00Z",
            files=files,
            plan_text=plan_text,
            plan_ts="2026-06-30T09:00:00Z",
            prior_claims=prior_claims,
        )

        fc_by_path = {fc["path"]: fc for fc in db.filechanges}

        # rescued.py must carry the prior reason verbatim (no prefix).
        rescued_reason = fc_by_path["rescued.py"]["reason"]
        assert rescued_reason == prior_reason, (
            f"prior-claim-rescued file must carry prior reason verbatim; "
            f"got {rescued_reason!r}"
        )
        assert not rescued_reason.startswith("(not itemized"), (
            "prior-claim-rescued file must NOT be prefixed with the honest prefix"
        )

        # rescued.py must NOT appear in unplanned_files (it was rescued, not bare).
        assert "rescued.py" not in stats.get("unplanned_files", []), (
            "prior-claim-rescued file must not appear in stats['unplanned_files']"
        )


# ---------------------------------------------------------------------------
# Plan.md §Testing item 5: harvest() backfill emits committed_file_not_in_plan
# ---------------------------------------------------------------------------
# Capability map addition:
#   [hv-backfill-friction]  harvest() emits committed_file_not_in_plan with
#                           session_id="" for each unplanned file in a commit
#                           that is governed by a plan (partial coverage).

class TestHarvestBackfillUnplannedFileFriction:
    """Cap [hv-backfill-friction]: harvest() per-commit loop emits friction events.

    The harvest() loop calls harvest_one_commit and then, for each path in
    stats["unplanned_files"], emits _log_friction_event("", None,
    "committed_file_not_in_plan", file_path=path, commit_hash=..., project=name).

    The autouse _isolate_friction_log conftest fixture redirects WRIT_FRICTION_LOG
    to tmp_path/workflow-friction.log so no test writes to the repo log and the
    event is readable by inspecting that file.
    """

    @pytest.mark.asyncio
    async def test_harvest_backfill_emits_unplanned_file_friction_with_empty_session(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # [hv-backfill-friction]: drive harvest() over a single commit that touches
        # planned.py (named in the governing plan's ## Files) and unplanned.py (not
        # named). The harvest() loop must:
        #   - emit exactly one committed_file_not_in_plan friction event
        #   - that event's file_path is "unplanned.py"
        #   - that event's session_id is "" (the backfill marker, not a live session)
        #   - that event carries the commit hash
        #   - no such event is emitted for planned.py (which has a plan reason)
        import json
        import os
        from writ.session import harvester

        _commit_hash = "hv-friction-bkfill-001"
        _plan_text = (
            "## Files\n\n"
            "- `planned.py` (modify) -- the per-file plan reason\n\n"
            "## Rules Applied\n\nNo matching rules\n"
        )

        _patch_project(monkeypatch, "proj")
        # Return a plan that names only planned.py.
        monkeypatch.setattr(harvester, "_collect_plan_writes", lambda d: [
            {"ts": "2026-06-26T00:00:00Z", "plan_text": _plan_text},
        ])
        # harvest_plan parses that plan and returns a files list with planned.py only.
        monkeypatch.setattr(harvester, "harvest_plan", lambda text: {
            "rationale": "plan rationale",
            "cited_rules": [],
            "files": [{"path": "planned.py", "change_type": "modify",
                        "reason": "the per-file plan reason"}],
        })
        # The commit touches both planned.py and unplanned.py.
        monkeypatch.setattr(harvester, "_git_commits", lambda r, s: [
            {"hash": _commit_hash, "subject": "feat: add planned and unplanned",
             "body": "", "author": "tester", "branch": "main",
             "ts": "2026-06-26T01:00:00Z",
             "files": [
                 {"path": "planned.py", "change_type": "modify"},
                 {"path": "unplanned.py", "change_type": "add"},
             ]},
        ])
        # No transcript file-context rules (fail-soft path).
        monkeypatch.setattr(harvester, "_collect_file_context_rules", lambda d: [])

        db = _FakeDB()
        await harvester.harvest(db, "/repo")

        # Read the friction log redirected by the autouse _isolate_friction_log fixture.
        friction_log = Path(os.environ["WRIT_FRICTION_LOG"])
        events: list[dict] = []
        if friction_log.exists():
            for line in friction_log.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        unplanned_events = [
            e for e in events if e.get("event") == "committed_file_not_in_plan"
        ]

        # Exactly one friction event for the unplanned file.
        assert len(unplanned_events) == 1, (
            f"exactly one committed_file_not_in_plan event must be emitted; "
            f"got {len(unplanned_events)}: {unplanned_events}"
        )
        ev = unplanned_events[0]

        # file_path must be the unplanned file.
        assert ev.get("file_path") == "unplanned.py", (
            f"friction event file_path must be 'unplanned.py'; got {ev.get('file_path')!r}"
        )

        # session_id must be "" (the backfill sentinel -- harvest() passes "" not a
        # live session id to _log_friction_event).
        assert ev.get("session") == "", (
            f"friction event session must be '' (backfill marker); "
            f"got {ev.get('session')!r}"
        )

        # The event must carry the commit hash for traceability.
        assert ev.get("commit_hash") == _commit_hash, (
            f"friction event must carry commit_hash {_commit_hash!r}; "
            f"got {ev.get('commit_hash')!r}"
        )

        # No friction event emitted for the planned file.
        planned_events = [
            e for e in unplanned_events if e.get("file_path") == "planned.py"
        ]
        assert planned_events == [], (
            "planned.py (which has a plan reason) must not generate a friction event"
        )
