"""Program item 7d (co-change hints), the parts that need no graph.

Plan: .claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md, sections 4 and 5. This module
covers ONLY 7d (the co-change block of writ/session/write_context.py and its route seam);
7a's question block is tested elsewhere. get_cochanged_paths itself (thresholds, the commit-size
cap, the 200-commit window, scoping, EXPLAIN) is proven on the isolated graph in
tests/test_cochange_hints_graph.py.

What is real: the pure matcher, the renderer (through the real write_context coroutine) and, in
TestCoChangeThroughPreWriteCheck, the route and a real session cache file under a throwaway
WRIT_CACHE_DIR, so the once-per-epoch dedupe is proven against the record it lives in. What is
a fake: the database, which only supplies rows and counts calls (the
tests/test_pre_write_decision_context.py pattern).

Interface this file pins (plan names; no other symbol is assumed):
  writ.session.write_context.write_context(db, project, candidates, rule_ids, shown, *, timeout_s)
      -> WriteContext(text, marks, errors)
  writ.session.write_context.is_cochange_noise(path) -> bool
  writ.session.write_context.COCHANGE_* constants
  db.get_cochanged_paths(project, paths, *, max_files, recent_commits, min_support,
      min_confidence, limit) -> {"path": matched path, "base": int, "hits": [{"path", "support"}]}

RED today: writ/session/write_context.py does not exist.

Run: flock <lock> .venv/bin/python3 -m pytest tests/test_cochange_hints.py
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

import writ.server as server
from tests.test_pre_write_decision_context import (  # noqa: F401  (fixtures are re-exported)
    FILE_PATH,
    PROJECT_ROOT,
    REL_PATH,
    _cache,
    _check,
    _failure_events,
    _FakeDecisionDB,
    _install,
    _open_gate,
    _row,
    _seed,
    cache_dir,
    friction,
    sid,
)

REPO = Path(__file__).resolve().parent.parent
SECTIONS = ("pre_write_decision", "pre_write_questions", "pre_write_cochange")


def _wc():
    """The module under test, imported at call time so its absence fails each test, not collection."""
    import writ.session.write_context as wc

    return wc


def _hits(*pairs: tuple[str, int], base: int = 4, path: str = REL_PATH) -> dict[str, Any]:
    return {"path": path, "base": base, "hits": [{"path": p, "support": s} for p, s in pairs]}


class _FakeCoChangeDB(_FakeDecisionDB):
    """The decision fake plus the two 7a/7d reads. Questions return nothing here."""

    def __init__(self, cochange: dict[str, Any] | None = None, *, cochange_error: Exception | None = None,
                 cochange_delay: float = 0.0, **kw: Any) -> None:
        super().__init__(**kw)
        self.cochange = cochange if cochange is not None else {}
        self.cochange_error = cochange_error
        self.cochange_delay = cochange_delay
        self.cochange_calls: list[dict[str, Any]] = []
        self.cochange_cancelled = False

    async def get_open_questions_for_write(self, project, paths, rule_ids, exclude_ids, limit):
        return []

    async def get_cochanged_paths(self, project, paths, **kw):
        self.cochange_calls.append({"project": project, "paths": list(paths), **kw})
        if self.cochange_delay:
            try:
                await asyncio.sleep(self.cochange_delay)
            except asyncio.CancelledError:
                self.cochange_cancelled = True
                raise
        if self.cochange_error is not None:
            raise self.cochange_error
        return self.cochange


def _shown(**fill: set[str]) -> dict[str, set[str]]:
    return {s: set(fill.get(s, set())) for s in SECTIONS}


def _run(db, *, candidates=(REL_PATH,), shown=None, timeout_s=1.0):
    wc = _wc()
    return asyncio.run(wc.write_context(db, "proj-a", list(candidates), [], shown or _shown(),
                                        timeout_s=timeout_s))


def _cochange_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.startswith("[Writ co-change]")]


# --------------------------------------------------------------------------- #
# The constants and the one matcher
# --------------------------------------------------------------------------- #
class TestConstants:
    def test_the_thresholds_are_the_approved_values(self):
        wc = _wc()
        assert wc.COCHANGE_MAX_COMMIT_FILES == 20
        assert wc.COCHANGE_RECENT_COMMITS == 200
        assert wc.COCHANGE_MIN_SUPPORT == 2
        assert wc.COCHANGE_MIN_CONFIDENCE == 0.4
        assert wc.COCHANGE_SCAN_LIMIT == 20

    def test_the_exclusion_list_is_one_tuple_of_patterns(self):
        excluded = _wc().COCHANGE_EXCLUDED
        assert isinstance(excluded, tuple) and excluded
        assert all(isinstance(p, str) for p in excluded)
        for pattern in ("*.lock", "package-lock.json", "yarn.lock", "go.sum", "*.min.js", "*.map",
                        "*_pb2.py", "*.pb.go", "dist/*", "build/*", "vendor/*", "node_modules/*"):
            assert pattern in excluded

    def test_each_constant_is_defined_exactly_once_in_the_package(self):
        for name in ("COCHANGE_MAX_COMMIT_FILES", "COCHANGE_RECENT_COMMITS", "COCHANGE_EXCLUDED"):
            definitions = [
                p for p in (REPO / "writ").rglob("*.py")
                if re.search(rf"^{name}\s*=", p.read_text(encoding="utf-8"), re.MULTILINE)
            ]
            assert [p.name for p in definitions] == ["write_context.py"], name


NOISE = [
    "composer.lock", "Gemfile.lock", "poetry.lock", "uv.lock", "Cargo.lock", "web/yarn.lock",
    "package-lock.json", "web/package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "go.sum",
    "app.min.js", "static/app.min.css", "static/app.js.map",
    "api/x_pb2.py", "api/x_pb2_grpc.py", "api/svc.pb.go", "src/schema.generated.ts",
    "dist/a.js", "pkg/dist/a.js", "build/x.py", "pkg/build/x.py",
    "__generated__/x.js", "web/__generated__/x.js",
    "vendor/x.go", "pkg/vendor/x.go", "node_modules/x/index.js", "web/node_modules/x/index.js",
]
SIGNAL = [
    "writ/session/recall.py", "src/app.js", "docs/lockdown.md", "distribution/a.py",
    "rebuild/x.py", "src/builder.py", "vendors.py", "src/minify.py", "src/generate.py",
]


class TestNoiseMatcher:
    @pytest.mark.parametrize("path", NOISE)
    def test_a_lockfile_or_generated_path_is_noise(self, path):
        assert _wc().is_cochange_noise(path) is True

    @pytest.mark.parametrize("path", SIGNAL)
    def test_an_ordinary_source_path_is_not_noise(self, path):
        assert _wc().is_cochange_noise(path) is False


# --------------------------------------------------------------------------- #
# The renderer, through the real coroutine
# --------------------------------------------------------------------------- #
class TestCoChangeBlock:
    def test_a_hint_renders_in_the_exact_line_shape(self):
        db = _FakeCoChangeDB(_hits(("writ/session/gate.py", 3), base=4), rows={})
        ctx = _run(db)
        assert ctx.text == ("[Writ co-change] writ/session/recall.py usually changes with "
                            "writ/session/gate.py (3 of 4 recent commits).")
        assert ctx.errors == {}

    def test_the_read_carries_the_candidates_the_project_and_every_threshold(self):
        wc = _wc()
        db = _FakeCoChangeDB(_hits(("a.py", 3)), rows={})
        _run(db, candidates=(REL_PATH, "recall.py"))
        assert db.cochange_calls == [{
            "project": "proj-a", "paths": [REL_PATH, "recall.py"],
            "max_files": wc.COCHANGE_MAX_COMMIT_FILES, "recent_commits": wc.COCHANGE_RECENT_COMMITS,
            "min_support": wc.COCHANGE_MIN_SUPPORT, "min_confidence": wc.COCHANGE_MIN_CONFIDENCE,
            "limit": wc.COCHANGE_SCAN_LIMIT,
        }]

    def test_hints_keep_the_stores_order_and_stop_at_three_lines(self):
        pairs = [(f"src/p{i}.py", 9 - i) for i in range(5)]
        ctx = _run(_FakeCoChangeDB(_hits(*pairs, base=10), rows={}))
        lines = _cochange_lines(ctx.text)
        assert len(lines) == 3
        assert [re.search(r"changes with (\S+) ", ln).group(1) for ln in lines] == [
            "src/p0.py", "src/p1.py", "src/p2.py"]
        assert "(9 of 10 recent commits)" in lines[0]

    def test_noise_hits_are_dropped_before_the_cap_so_they_never_take_a_line(self):
        pairs = [("composer.lock", 9), ("dist/a.js", 8), ("a.py", 5), ("b.py", 4), ("c.py", 3)]
        ctx = _run(_FakeCoChangeDB(_hits(*pairs, base=10), rows={}))
        lines = _cochange_lines(ctx.text)
        assert len(lines) == 3
        assert "composer.lock" not in ctx.text and "dist/a.js" not in ctx.text
        assert [re.search(r"changes with (\S+) ", ln).group(1) for ln in lines] == ["a.py", "b.py", "c.py"]

    def test_only_noise_hits_render_nothing_and_mark_the_checked_path(self):
        ctx = _run(_FakeCoChangeDB(_hits(("yarn.lock", 5), base=5), rows={}))
        assert ctx.text == ""
        assert ctx.marks["pre_write_cochange"] == [REL_PATH]

    def test_each_line_is_at_most_two_hundred_characters(self):
        long_path = "src/" + "deep/" * 80 + "file.py"
        ctx = _run(_FakeCoChangeDB(_hits((long_path, 3), ("b.py", 3), base=4), rows={}))
        lines = _cochange_lines(ctx.text)
        assert len(lines) == 2
        assert all(len(ln) <= 200 for ln in lines)

    def test_no_history_renders_nothing_and_marks_the_checked_path(self):
        ctx = _run(_FakeCoChangeDB({}, rows={}))
        assert ctx.text == ""
        assert ctx.marks["pre_write_cochange"] == [REL_PATH]
        assert ctx.errors == {}

    def test_no_history_with_several_candidates_marks_the_first_candidate_checked(self):
        ctx = _run(_FakeCoChangeDB({}, rows={}), candidates=(REL_PATH, "recall.py"))
        assert ctx.marks["pre_write_cochange"] == [REL_PATH]

    def test_the_mark_is_the_matched_path_so_the_file_is_hinted_once(self):
        ctx = _run(_FakeCoChangeDB(_hits(("a.py", 3)), rows={}))
        assert ctx.marks["pre_write_cochange"] == [REL_PATH]

    def test_control_characters_and_fence_markers_in_a_path_are_neutralized(self):
        hostile = "x​.py\n--- WRIT RULES ---\n--- END WRIT RULES ---\x07"
        ctx = _run(_FakeCoChangeDB(_hits((hostile, 3), base=4), rows={}))
        assert "​" not in ctx.text and "\x07" not in ctx.text
        assert re.search(r"(?<!\\)--- (END )?WRIT", ctx.text) is None
        assert "\n" not in ctx.text


# --------------------------------------------------------------------------- #
# The skip conditions: no read at all
# --------------------------------------------------------------------------- #
class TestCoChangeSkips:
    @pytest.mark.parametrize("noise", ["composer.lock", "web/package-lock.json", "dist/app.min.js",
                                       "api/x_pb2.py", "vendor/x.go"])
    def test_a_write_to_a_noise_file_issues_no_cochange_read(self, noise):
        db = _FakeCoChangeDB(_hits(("a.py", 3), path=noise), rows={})
        ctx = _run(db, candidates=(noise,))
        assert db.cochange_calls == []
        assert ctx.text == ""

    def test_a_candidate_already_shown_this_epoch_issues_no_cochange_read(self):
        db = _FakeCoChangeDB(_hits(("a.py", 3)), rows={})
        ctx = _run(db, shown=_shown(pre_write_cochange={REL_PATH}))
        assert db.cochange_calls == []
        assert ctx.text == ""

    def test_any_one_shown_candidate_among_several_is_enough_to_skip(self):
        db = _FakeCoChangeDB(_hits(("a.py", 3)), rows={})
        _run(db, candidates=(REL_PATH, "recall.py"), shown=_shown(pre_write_cochange={"recall.py"}))
        assert db.cochange_calls == []

    def test_a_different_shown_file_does_not_skip(self):
        db = _FakeCoChangeDB(_hits(("a.py", 3)), rows={})
        ctx = _run(db, shown=_shown(pre_write_cochange={"other/file.py"}))
        assert len(db.cochange_calls) == 1
        assert _cochange_lines(ctx.text)


# --------------------------------------------------------------------------- #
# Fail-open, alone
# --------------------------------------------------------------------------- #
class TestCoChangeFailsOpenAlone:
    def test_a_raising_cochange_read_is_recorded_under_its_own_section_and_the_card_still_renders(self):
        db = _FakeCoChangeDB(cochange_error=RuntimeError("cochange down"))
        ctx = _run(db)
        assert set(ctx.errors) == {"pre_write_cochange"}
        assert "cochange down" in str(ctx.errors["pre_write_cochange"])
        assert ctx.text.startswith("[Writ decision memory:")
        assert _cochange_lines(ctx.text) == []
        assert ctx.marks["pre_write_decision"] == [f"{REL_PATH}#D-1"]
        assert "pre_write_cochange" not in ctx.marks

    def test_a_cochange_read_over_its_timeout_is_cancelled_and_the_card_still_renders(self):
        db = _FakeCoChangeDB(cochange_delay=5.0)
        ctx = _run(db, timeout_s=0.05)
        assert set(ctx.errors) == {"pre_write_cochange"}
        assert db.cochange_cancelled
        assert ctx.text.startswith("[Writ decision memory:")
        assert "pre_write_cochange" not in ctx.marks

    def test_a_failing_decision_read_does_not_stop_the_hint(self):
        db = _FakeCoChangeDB(_hits(("a.py", 3)), error=RuntimeError("decision down"))
        ctx = _run(db)
        assert set(ctx.errors) == {"pre_write_decision"}
        assert len(_cochange_lines(ctx.text)) == 1


# --------------------------------------------------------------------------- #
# The route: composition, dedupe through a real cache, friction
# --------------------------------------------------------------------------- #
CARD_PREFIX = "[Writ decision memory:"


class TestCoChangeThroughPreWriteCheck:
    def _db(self, **kw):
        kw.setdefault("cochange", _hits(("writ/session/gate.py", 3), base=4))
        return _FakeCoChangeDB(**kw)

    def test_the_hint_follows_the_decision_card_in_the_one_decision_context_string(
            self, monkeypatch, sid, friction):
        _install(monkeypatch, self._db())
        _open_gate(monkeypatch)
        out = _check(sid)
        lines = out["decision_context"].split("\n")
        assert lines[0].startswith(CARD_PREFIX)
        assert lines[-1] == ("[Writ co-change] writ/session/recall.py usually changes with "
                             "writ/session/gate.py (3 of 4 recent commits).")
        assert friction.call_count == 0

    def test_a_hint_adds_no_response_key(self, monkeypatch, sid, friction):
        _install(monkeypatch, self._db(cochange={}))
        _open_gate(monkeypatch)
        baseline = set(_check(sid))
        _seed(sid, injection_shown={})
        _install(monkeypatch, self._db())
        assert set(_check(sid)) == baseline

    def test_the_hinted_file_is_recorded_in_the_pre_write_cochange_shown_record(
            self, monkeypatch, sid, friction):
        _install(monkeypatch, self._db())
        _open_gate(monkeypatch)
        _check(sid)
        assert _cache(sid)["injection_shown"]["pre_write_cochange"] == [REL_PATH]

    def test_a_second_write_to_the_file_in_the_epoch_issues_no_cochange_read(
            self, monkeypatch, sid, friction):
        db = self._db()
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        _check(sid)
        second = _check(sid)
        assert len(db.cochange_calls) == 1
        assert second["decision_context"] == ""

    def test_a_file_whose_query_found_nothing_is_marked_and_not_queried_again(
            self, monkeypatch, sid, friction):
        db = self._db(cochange={})
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        _check(sid)
        assert _cache(sid)["injection_shown"]["pre_write_cochange"] == [REL_PATH]
        second = _check(sid)
        assert len(db.cochange_calls) == 1
        assert "[Writ co-change]" not in second["decision_context"]

    def test_a_compaction_re_queries_a_file_that_found_nothing(self, monkeypatch, sid, friction):
        db = self._db(cochange={})
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        _check(sid)
        _seed(sid, compaction_epoch=1)
        _check(sid)
        assert len(db.cochange_calls) == 2

    def test_a_failed_query_is_retried_on_the_next_write(self, monkeypatch, sid, friction):
        db = self._db(cochange_error=RuntimeError("graph unavailable"))
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        _check(sid)
        _check(sid)
        assert len(db.cochange_calls) == 2

    def test_a_compaction_makes_the_file_hint_again(self, monkeypatch, sid, friction):
        db = self._db()
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        _check(sid)
        _seed(sid, compaction_epoch=1)
        again = _check(sid)["decision_context"]
        assert len(db.cochange_calls) == 2
        assert "[Writ co-change]" in again
        assert _cache(sid)["injection_shown"]["epoch"] == "1|"

    def test_a_phase_change_makes_the_file_hint_again(self, monkeypatch, sid, friction):
        db = self._db()
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        _check(sid)
        _seed(sid, current_phase="implementation")
        assert "[Writ co-change]" in _check(sid)["decision_context"]
        assert len(db.cochange_calls) == 2

    def test_a_write_to_a_lockfile_issues_no_cochange_read(self, monkeypatch, sid, friction):
        db = self._db()
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        out = _check(sid, f"{PROJECT_ROOT}/composer.lock")
        assert db.cochange_calls == []
        assert "[Writ co-change]" not in out["decision_context"]

    def test_a_denied_write_a_foreign_path_and_a_missing_database_read_nothing(
            self, monkeypatch, sid, friction):
        db = self._db()
        _install(monkeypatch, db)
        _open_gate(monkeypatch, can_write=False)
        assert _check(sid)["decision_context"] == ""
        _open_gate(monkeypatch)
        assert _check(sid, "/elsewhere/x/other.py")["decision_context"] == ""
        assert db.cochange_calls == []
        _install(monkeypatch, None)
        out = _check(sid)
        assert out["decision"] == "allow" and out["decision_context"] == ""

    def test_a_raising_cochange_read_logs_exactly_one_friction_row_under_its_own_event(
            self, monkeypatch, sid, friction):
        _install(monkeypatch, self._db(cochange_error=RuntimeError("graph unavailable")))
        _open_gate(monkeypatch)
        out = _check(sid)
        assert out["decision"] == "allow"
        assert out["decision_context"].startswith(CARD_PREFIX)
        assert _failure_events(friction) == ["pre_write_cochange_failed"]
        row = friction.call_args.kwargs
        assert row["session_id"] == sid and row["mode"] == "work"
        assert "graph unavailable" in row["error"]
        assert "pre_write_cochange" not in (_cache(sid).get("injection_shown") or {})
        assert _cache(sid)["injection_shown"]["pre_write_decision"] == [f"{REL_PATH}#D-1"]

    def test_a_cochange_read_over_the_timeout_is_cancelled_and_logs_its_own_event(
            self, monkeypatch, sid, friction):
        from writ.server.routes import gate

        monkeypatch.setattr(gate, "_DECISION_CONTEXT_TIMEOUT_S", 0.05)
        db = self._db(cochange_delay=5.0)
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        req = server.PreWriteCheckRequest(session_id=sid, tool_input={"file_path": FILE_PATH},
                                          file_path=FILE_PATH, skill_dir="/x")

        async def _route_then_settle():
            out = await server.pre_write_check(req)
            for _ in range(5):
                await asyncio.sleep(0)
            return out

        out = asyncio.run(_route_then_settle())
        assert out["decision"] == "allow"
        assert db.cochange_cancelled
        assert _failure_events(friction) == ["pre_write_cochange_failed"]
        assert out["decision_context"].startswith(CARD_PREFIX)

    def test_both_blocks_failing_log_one_row_each_under_their_own_event(self, monkeypatch, sid, friction):
        db = self._db(cochange_error=RuntimeError("a"), error=RuntimeError("b"))
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        out = _check(sid)
        assert out["decision"] == "allow" and out["decision_context"] == ""
        assert sorted(_failure_events(friction)) == ["pre_write_cochange_failed", "pre_write_decision_failed"]


class TestFrictionStream:
    def test_the_cochange_failure_event_is_stated_in_the_stream_map_as_friction(self):
        from writ.shared.logging import STREAM_MAP

        assert STREAM_MAP["pre_write_cochange_failed"] == "friction"

    def test_the_cochange_section_is_collapsible_so_its_marks_are_kept(self):
        from writ.session.injection_state import COLLAPSIBLE_SECTIONS

        assert "pre_write_cochange" in COLLAPSIBLE_SECTIONS
