"""The last three cross-session bleeds: debug.md, post-commit, and the mode carry.

Three defects, three DIFFERENT fixes, which is why they share a module: they are the
remainder of one audit finding and each one's shape is only clear next to the others.

  * debug.md WAS THE plan.md BUG ONE ARTIFACT LATER. _find_debug_md resolves a project
    root and checks debug.md, docs/debug.md, .claude/debug.md there. Two gates re-read it:
    the debug write gate (which unblocks source edits once a root cause is populated) and
    the runtime read lens (which opens reading code once evidence is narrowed). So one
    session recording a root cause opened the OTHER session's gates, and one session
    overwriting the file closed them. A .claude/debug/<session_id>/ tier fixes it the way
    .claude/plans/<session_id>/ fixed plans. Note .claude/debug.md was ALREADY a tier, so
    the scoped location sits beside an existing one rather than being invented.

  * post-commit CANNOT HAVE A SESSION ID, and that was never the bug. Its own comment says
    "Git launches this hook, so it has no Claude Code session id", and cache.py documents
    the candidate env vars as unreachable from a bash hook. The pointer stays because it is
    the only bridge. What was missing is the guard rotation.py already performs with the
    same untrustworthy pointer: compare the named session's declared project against this
    one, and ignore a mismatch. Prompts vastly outnumber commits across concurrent
    sessions, so cross-project attribution was the common case, not a race.

  * THE CARRY-FORWARD CANNOT IDENTIFY A PREDECESSOR. Rotation issues a new id and nothing
    records the link, so "my previous id" is not a knowable fact; the pointer names whoever
    prompted last. The existing project guard passes for same-project siblings, which is
    exactly the leak. Lineage would mean inventing a record only the harness could write
    honestly, so the guard becomes a count: more than one live cache claiming the project
    means refuse, and the user runs `mode set`.

THE POINTER PATH IS PASSED IN, NOT ENV-OVERRIDDEN. The guard lives in a common.sh helper
taking the pointer path as an explicit optional argument, defaulting to the hardcoded
production path. That is a deliberate choice over an env var: this audit found that
env-derived paths hand path selection to whatever sets the environment, which is the wrong
property for a file that decides whose session a commit is credited to. An explicit
argument gives the test its seam without giving anything else a lever. It also means NO test
here writes the real /tmp/writ-current-session, which no existing test does either.

One capability is deliberately untested: the operational two-session debug check needs two
real sessions.
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON = REPO / "bin" / "lib" / "common.sh"


def _sid(label: str) -> str:
    return f"dps-{label}-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """An isolated session-cache dir, so nothing here reads or writes live state."""
    d = tmp_path / "cache"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WRIT_CACHE_DIR", str(d))
    from writ.session import cache as cache_mod
    monkeypatch.setattr(cache_mod, "_DEFAULT_CACHE_DIR", str(d), raising=False)
    return d


def _seed_session(cache_dir: Path, sid: str, **fields) -> None:
    (cache_dir / f"writ-session-{sid}.json").write_text(json.dumps(fields))


def _write_debug(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _marked_project(tmp_path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("")
    return root


def _pointer_session(project_root: str, session_id: str, cache_dir: Path) -> str:
    """Call the real comparison directly.

    The helper takes a session ID, not a pointer path: the pointer read stays in
    post-commit, the one file the pointer-read scan exempts by name. So this test needs no
    pointer file at all, which also means it cannot touch live machine state.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pointer_session", REPO / "bin" / "lib" / "pointer_session.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    os.environ["WRIT_CACHE_DIR"] = str(cache_dir)
    return module.session_matches_project(session_id, project_root)


# --------------------------------------------------------------------------- #
# 1. debug.md resolution
# --------------------------------------------------------------------------- #
class TestDebugPathConstruction:
    def test_debug_path_is_the_session_scoped_file(self, tmp_path):
        from writ.session import locators
        sid = _sid("dp")
        expected = os.path.join(str(tmp_path), ".claude", "debug", sid, "debug.md")
        assert locators.debug_path(str(tmp_path), sid) == expected

    @pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "with space"])
    def test_invalid_session_component_has_no_path(self, tmp_path, bad):
        """Same contract as plan_dir and gate_dir: "" means no path, never join to it."""
        from writ.session import locators
        assert locators.debug_path(str(tmp_path), bad) == ""
        assert locators.debug_dir(str(tmp_path), bad) == ""

    def test_empty_project_root_has_no_path(self):
        from writ.session import locators
        assert locators.debug_path("", _sid("er")) == ""


class TestDebugResolutionTiers:
    def test_scoped_file_wins(self, tmp_path):
        from writ.session.locators import _find_debug_md
        root = _marked_project(tmp_path, "proj")
        sid = _sid("wins")
        _write_debug(root, "debug.md", "root\n")
        scoped = _write_debug(root, f".claude/debug/{sid}/debug.md", "scoped\n")
        assert _find_debug_md(str(root / "src.py"), sid) == str(scoped)

    @pytest.mark.parametrize("rel", ["debug.md", "docs/debug.md", ".claude/debug.md"])
    def test_each_existing_tier_still_resolves(self, tmp_path, rel):
        from writ.session.locators import _find_debug_md
        root = _marked_project(tmp_path, "p" + rel.replace("/", "-").replace(".", ""))
        expected = _write_debug(root, rel, "x")
        assert _find_debug_md(str(root / "s.py"), _sid("tier")) == str(expected)

    def test_root_file_beats_the_other_two(self, tmp_path):
        """Tier order is unchanged below the new one."""
        from writ.session.locators import _find_debug_md
        root = _marked_project(tmp_path, "order")
        expected = _write_debug(root, "debug.md", "root")
        _write_debug(root, "docs/debug.md", "docs")
        _write_debug(root, ".claude/debug.md", "claude")
        assert _find_debug_md(str(root / "s.py"), _sid("o")) == str(expected)

    def test_omitted_session_id_resolves_as_before(self, tmp_path):
        """The unscoped call is the pre-change contract and must not move."""
        from writ.session.locators import _find_debug_md
        root = _marked_project(tmp_path, "proj")
        expected = _write_debug(root, "debug.md", "root\n")
        _write_debug(root, f".claude/debug/{_sid('ignored')}/debug.md", "scoped\n")
        assert _find_debug_md(str(root / "src.py")) == str(expected)

    def test_none_when_no_debug_file_exists(self, tmp_path):
        from writ.session.locators import _find_debug_md
        root = _marked_project(tmp_path, "empty")
        assert _find_debug_md(str(root / "src.py"), _sid("none")) is None


class TestTwoSessionsHoldIndependentDebugGates:
    def test_one_sessions_root_cause_does_not_unblock_the_other(self, tmp_path, cache_dir):
        """The defect, as an assertion: A's evidence must not open B's write gate."""
        from writ.session.gates import _can_write_check

        root = _marked_project(tmp_path, "proj")
        sid_a, sid_b = _sid("a"), _sid("b")
        _write_debug(root, f".claude/debug/{sid_a}/debug.md", _ROOT_CAUSE_BODY)
        cache = {"mode": "debug", "project_root": str(root), "source_type": "runtime"}
        target = str(root / "src" / "thing.py")

        a = _can_write_check(sid_a, {"tool_input": {"file_path": target}}, cache=dict(cache))
        b = _can_write_check(sid_b, {"tool_input": {"file_path": target}}, cache=dict(cache))
        assert a.get("can_write") is True, "A recorded a root cause and must be unblocked"
        assert b.get("can_write") is False, "B recorded nothing and must stay blocked"

    def test_the_runtime_lens_opens_only_for_the_recording_session(self, tmp_path, cache_dir):
        from writ.session.gates import _can_read_code_check

        root = _marked_project(tmp_path, "proj")
        sid_a, sid_b = _sid("ra"), _sid("rb")
        _write_debug(root, f".claude/debug/{sid_a}/debug.md", _EVIDENCE_BODY)
        for sid in (sid_a, sid_b):
            _seed_session(cache_dir, sid, mode="debug", project_root=str(root),
                          source_type="runtime")

        envelope = {"tool_name": "Read", "tool_input": {"file_path": str(root / "s.py")}}
        assert _can_read_code_check(sid_a, envelope).get("can_read") is True
        assert _can_read_code_check(sid_b, envelope).get("can_read") is False


# --------------------------------------------------------------------------- #
# 2. the post-commit project guard
# --------------------------------------------------------------------------- #
class TestPointerProjectGuard:
    def test_a_same_project_session_is_accepted(self, tmp_path, cache_dir):
        repo = _marked_project(tmp_path, "repo")
        sid = _sid("same")
        _seed_session(cache_dir, sid, project_root=str(repo), mode="work")
        assert _pointer_session(str(repo), sid, cache_dir) == sid

    def test_a_different_project_session_is_rejected(self, tmp_path, cache_dir):
        """The reported defect: prompts outnumber commits, so this was the common case."""
        repo = _marked_project(tmp_path, "repo")
        other = _marked_project(tmp_path, "elsewhere")
        sid = _sid("other")
        _seed_session(cache_dir, sid, project_root=str(other), mode="work")
        assert _pointer_session(str(repo), sid, cache_dir) == "", (
            "a session that declared a different project must not be credited with "
            "this commit"
        )

    def test_an_empty_session_id_yields_empty(self, tmp_path, cache_dir):
        """An absent pointer reaches the helper as an empty id, and must stay empty."""
        repo = _marked_project(tmp_path, "repo")
        assert _pointer_session(str(repo), "", cache_dir) == ""

    def test_a_session_with_no_recorded_project_is_rejected(self, tmp_path, cache_dir):
        """An unknown project cannot be shown to match, so it fails closed."""
        repo = _marked_project(tmp_path, "repo")
        sid = _sid("noproj")
        _seed_session(cache_dir, sid, mode="work")
        assert _pointer_session(str(repo), sid, cache_dir) == ""


# --------------------------------------------------------------------------- #
# 3. carry-forward: refuse when the predecessor is unknowable
# --------------------------------------------------------------------------- #
class TestCarryForwardMultiSessionRefusal:
    def test_a_single_live_session_still_carries(self, tmp_path, cache_dir):
        from writ.session import cache as cache_mod
        from writ.session.rotation import carry_forward_mode

        root = str(_marked_project(tmp_path, "proj"))
        prev, new = _sid("prev"), _sid("new")
        _seed_session(cache_dir, prev, mode="debug", project_root=root, mode_source="user")
        carry_forward_mode(new, root, prev, "resume")
        assert cache_mod._read_cache(new).get("mode") == "debug"

    def test_two_live_sessions_on_one_project_refuse_to_carry(self, tmp_path, cache_dir, capsys):
        """The pointer names whoever prompted last, which may be a peer, not a predecessor."""
        from writ.session import cache as cache_mod
        from writ.session.rotation import carry_forward_mode

        root = str(_marked_project(tmp_path, "proj"))
        prev, sibling, new = _sid("prev"), _sid("sib"), _sid("new")
        _seed_session(cache_dir, prev, mode="debug", project_root=root, mode_source="user")
        _seed_session(cache_dir, sibling, mode="work", project_root=root, mode_source="user")
        carry_forward_mode(new, root, prev, "resume")
        assert not cache_mod._read_cache(new).get("mode"), (
            "with a sibling in the same project the predecessor is unknowable, so no "
            "mode may be carried"
        )
        assert "mode set" in capsys.readouterr().err

    def test_a_sibling_in_another_project_does_not_block_the_carry(self, tmp_path, cache_dir):
        """The count is per-project: an unrelated session must not veto a valid carry."""
        from writ.session import cache as cache_mod
        from writ.session.rotation import carry_forward_mode

        root = str(_marked_project(tmp_path, "proj"))
        other = str(_marked_project(tmp_path, "other"))
        prev, elsewhere, new = _sid("prev"), _sid("else"), _sid("new")
        _seed_session(cache_dir, prev, mode="debug", project_root=root, mode_source="user")
        _seed_session(cache_dir, elsewhere, mode="work", project_root=other,
                      mode_source="user")
        carry_forward_mode(new, root, prev, "resume")
        assert cache_mod._read_cache(new).get("mode") == "debug"

    def test_a_different_project_predecessor_still_refuses(self, tmp_path, cache_dir):
        """Unchanged behaviour: the existing project guard must keep working."""
        from writ.session import cache as cache_mod
        from writ.session.rotation import carry_forward_mode

        root = str(_marked_project(tmp_path, "proj"))
        other = str(_marked_project(tmp_path, "other"))
        prev, new = _sid("prev"), _sid("new")
        _seed_session(cache_dir, prev, mode="debug", project_root=other, mode_source="user")
        carry_forward_mode(new, root, prev, "resume")
        assert not cache_mod._read_cache(new).get("mode")


_ROOT_CAUSE_BODY = (
    "# Debug\n\n## Root Cause Evidence\n\n"
    "The failure is in the parser: it splits on the first tab, and a line with no tab\n"
    "returns the whole line, so every field reads the same value. Observed at\n"
    "parser.py:88 with input 'a b c'.\n"
)

# The runtime lens requires BOTH '## Evidence' and '## Narrowing' with real
# (non-placeholder) content: gates.py::_validate_evidence_narrowing.
_EVIDENCE_BODY = (
    "# Debug\n\n"
    "## Evidence\n\n"
    "Reproduced against the running service: POST /orders returns 500 with\n"
    "KeyError 'user_id' raised at handler.py:42, logged at 11:04:07Z.\n\n"
    "## Narrowing\n\n"
    "The deserializer drops user_id when the body is form-encoded; a JSON body with the\n"
    "same fields succeeds, so the fault is in form parsing, not the handler.\n"
)
