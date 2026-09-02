"""A project-boundary predicate on writes: skeletons for plan.md / capabilities.md.

THE FEATURE. Today the write gate is path-blind once both work-mode gates are
approved: an approved plan for this repo currently authorizes a write to any
absolute path on the filesystem (measured: `/etc/passwd-probe.txt` and
`<repo>/x.py` both ALLOW post-approval). This cycle adds one predicate,
`writ/session/project_boundary.py`, wired into `writ/session/gates.py` at three
call sites, that can only convert an existing ALLOW into a DENY and never the
reverse. RED until that module and those call sites exist: every test below
either fails to import `project_boundary` or observes today's path-blind ALLOW
where a DENY is now required.

THE VACUITY TRAP THIS FILE IS BUILT AROUND. pytest's `tmp_path` lives inside
`tempfile.gettempdir()`, and the predicate's one exemption is exactly that OS
scratch zone -- but ONLY when the project root itself is NOT already inside it.
A naive fixture that builds "the project" and "outside the project" both loosely
under `tmp_path` would have every "outside" case pass for the wrong reason (the
scratch exemption, not real containment logic), because the real OS puts
`tmp_path` under `/tmp`. Every fixture below therefore roots "the project" (a
directory under `tmp_path` carrying a project-root marker) so it is itself
inside the real scratch zone -- which is exactly the condition the plan's own
guard uses to switch the exemption OFF -- so an "outside" sibling directory
under the SAME `tmp_path` denies purely on containment, with the scratch
exemption already inert by construction. Capabilities 16 and 17, which test the
scratch-zone mechanism ITSELF, instead monkeypatch `tempfile.gettempdir()`
directly so the zone's location is under explicit test control in both
directions, independent of where the real OS happens to put `/tmp`.

HARD CONSTRAINT 1 (no write outside tmp_path). No fixture below ever calls
`open(..., "w")` or creates a file/symlink outside a path rooted at the test's
own `tmp_path`. Several tests reference real, non-tmp_path absolute paths (an
`/etc/...`-style string, `Path.home() / ".claude" / ...`) as ENVELOPE VALUES
only -- `_can_write_check` is a pure decision function that never opens the
file it is asked about, matching the existing convention in
tests/test_role_write_scope.py's OUT_OF_TREE_DIR. See
TestFixturesNeverNameARealProjectRoot for the one guard this file adds, and its
docstring for why a full runtime-instrumentation version (mirroring
test_role_write_scope.py's fetcher-raises test) is not cleanly expressible here.

Capability map (capabilities.md, 26 items; the last lives in
tests/test_bash_write_gate.py):
  1  TestPostApprovalBoundaryDeniesOutsideAllowsInside   (mutation i)
  2  TestPreApprovalBaselineUnchanged
  3  TestPreApprovalExclusionGuardedByBoundary
  4  TestDeclaredAbsoluteFileEscapesTheBoundary,
     TestDeclarationIsTheOnlyVariable                  (mutations vi, vii)
  5  TestEscapeIsNotSelfGrantable
  6  TestReasonlessBulletDoesNotWiden
  7  TestBulletOutsideFilesSectionDoesNotWiden            (mutation iv)
  8  TestRelativeBulletWidensNothing
  9  TestDotDotTraversal                                  (mutation ii)
  10 TestSymlinkDirectionality
  11 TestSymlinkedProjectRoot
  12 TestPrefixSiblingDirectory                           (mutation iii)
  13 TestRecordedRootNarrowerThanMarkerRootWidens
  14 TestRelativeEnvelopePathResolvesAgainstRootNotCwd
  15 TestDegenerateRoots, TestBoundaryRootPureFunction
  16 TestScratchZoneGuardBothDirections
  17 TestCredentialDenyPrecedesBoundary
  18 TestSubagentUndeclaredScopeConfinedToParentRoot
  19 TestSubagentDeclaredScopeIgnoresBoundary,
     TestBoundaryArmIgnoresRoleWriteScope             (its structural half)
  20 TestSubagentAbsentOrEmptyParentRootAbstains
  21 TestLazySeedAbstainsLikeNoCache                      (mutation v)
  22 TestRefusalsNameTheirAction
  23 TestBoundaryDenyFrictionRowNotGateDenial
  24 TestCanWriteRouteMatchesDirectCall
  25 TestNoExtraPlanReadOnTheCommonPath
  26 (tests/test_bash_write_gate.py)
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# ruff: noqa: F811 -- imported fixtures consumed as test-method parameters, mirroring
# tests/test_decision_memory_capture.py's identical import of this same fixture pair.
from tests.fixtures.server_routes import client, isolated_cache  # noqa: F401

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------------- #
# Shared helpers. Fail loudly on missing production code, never skip (ENF-GATE-007).
# --------------------------------------------------------------------------------- #


def _pb_module():
    try:
        from writ.session import project_boundary
    except ImportError as exc:  # pragma: no cover - RED path
        pytest.fail(f"skeleton: writ/session/project_boundary.py does not exist yet ({exc})")
    return project_boundary


def _gates_module():
    from writ.session import gates
    return gates


def _envelope(path: str) -> dict:
    return {"tool_input": {"file_path": path}}


def _write_cache_for(session_id: str, payload: dict) -> None:
    from writ.session.cache import _write_cache
    _write_cache(session_id, payload)


def _friction_rows() -> list[dict]:
    path = Path(os.environ["WRIT_FRICTION_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _post_approval_cache(root: str, **overrides) -> dict:
    """Both work-mode gates approved, bound to 'no plan.md at all' (current_plan is
    None on both sides of the comparison, which plan_md_hash's own docstring calls
    genuinely equal), so tests that do not care about the plan-declared escape set
    never need to create a plan.md file."""
    cache = {
        "mode": "work",
        "current_phase": "implementation",
        "gates_approved": ["phase-a", "test-skeletons"],
        "gates_approved_plan": {"phase-a": None, "test-skeletons": None},
        "project_root": root,
        "is_subagent": False,
    }
    cache.update(overrides)
    return cache


def _pre_approval_cache(root: str, **overrides) -> dict:
    cache = {
        "mode": "work",
        "current_phase": None,
        "gates_approved": [],
        "gates_approved_plan": {},
        "project_root": root,
        "is_subagent": False,
    }
    cache.update(overrides)
    return cache


def _plan_text(files_section: str, analysis_section: str = "Fixture analysis text.") -> str:
    return (
        "# Plan: project-boundary fixture\n\n"
        "## Files\n\n"
        f"{files_section}\n\n"
        "## Analysis\n\n"
        f"{analysis_section}\n\n"
        "## Rules Applied\n\n"
        "No matching rules.\n\n"
        "## Capabilities\n\n"
        "- [ ] fixture capability\n"
    )


def _bound_post_approval_cache(root: Path, session_id: str, plan_text: str) -> dict:
    from writ.session.locators import plan_md_hash
    (root / "plan.md").write_text(plan_text)
    current_hash = plan_md_hash(str(root), session_id)
    return _post_approval_cache(
        str(root), gates_approved_plan={"phase-a": current_hash, "test-skeletons": current_hash}
    )


@pytest.fixture()
def fake_project(tmp_path):
    """A project root and a genuinely non-scratch sibling, BOTH under tmp_path.

    `root` carries a marker (.git), so resolve_project_root stops there instead of
    walking further up. Because tmp_path lives inside the real tempfile.gettempdir(),
    `root` is itself inside the OS scratch zone -- which switches the plan's scratch
    exemption OFF (see module docstring), so `outside` denies on containment alone.
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return root, outside


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """Isolated WRIT_CACHE_DIR for tests that persist a session cache to disk
    (parent/child sub-agent lookups, route parity, denial_counts round-trip).
    Friction-log isolation is already handled by conftest's autouse fixture."""
    d = tmp_path / "session-cache"
    d.mkdir()
    monkeypatch.setenv("WRIT_CACHE_DIR", str(d))
    return d


# --------------------------------------------------------------------------------- #
# Capability 1: post-approval boundary denies outside, still allows and logs inside.
# --------------------------------------------------------------------------------- #


class TestPostApprovalBoundaryDeniesOutsideAllowsInside:
    """Mutation (i): delete the boundary call from the both-approved arm of
    _check_work_gate; this capability must go red."""

    def test_outside_root_is_refused_with_the_boundary_tag(self, fake_project) -> None:
        root, outside = fake_project
        gates = _gates_module()
        cache = _post_approval_cache(str(root))
        target = str(outside / "thing.py")
        result = gates._can_write_check("pb-1-a", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-PROJECT-BOUNDARY" in (result["reason"] or "")

    def test_inside_root_still_allows_and_still_logs_all_approved(self, fake_project) -> None:
        root, outside = fake_project
        gates = _gates_module()
        cache = _post_approval_cache(str(root))
        target = str(root / "thing.py")
        result = gates._can_write_check("pb-1-b", _envelope(target), "", cache)
        assert result["can_write"] is True
        rows = _friction_rows()
        assert any(r.get("gate_status") == "all_approved" for r in rows), rows


# --------------------------------------------------------------------------------- #
# Capability 2: pre-approval baseline is unchanged for every measured envelope.
# --------------------------------------------------------------------------------- #


class TestPreApprovalBaselineUnchanged:
    """No named mutation targets this capability directly; it is the 'never
    preempts an existing deny' half of the plan's Honest Consequence paragraph.
    All three measured pre-approval envelopes must keep denying with
    [ENF-GATE-PLAN]: the boundary predicate must never be consulted before either
    allow arm it guards is reached, and pre-approval nothing (non-excluded)
    reaches them."""

    @pytest.mark.parametrize("which", ["in_root", "sibling", "unrelated_absolute"])
    def test_still_denies_with_enf_gate_plan(self, fake_project, which) -> None:
        root, outside = fake_project
        gates = _gates_module()
        cache = _pre_approval_cache(str(root))
        targets = {
            "in_root": str(root / "app.py"),
            "sibling": str(outside / "app.py"),
            "unrelated_absolute": "/etc/passwd-probe.txt",
        }
        result = gates._can_write_check(f"pb-2-{which}", _envelope(targets[which]), "", cache)
        assert result["can_write"] is False
        assert "ENF-GATE-PLAN" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# Capability 3: pre-approval exclusion allow is guarded by the boundary too.
# --------------------------------------------------------------------------------- #


class TestPreApprovalExclusionGuardedByBoundary:
    """No single named mutation in Verification step 4; the guard itself is
    described in plan.md's Integration section: `_matches_any` lets `*` span `/`,
    so `*/tests/*` is satisfied by a path under an entirely different project, and
    would otherwise be a one-line bypass of the whole feature. The exact refusal
    tag for this pre-approval case is not named by capabilities.md, so only the
    DECISION is pinned, not a specific [ENF-...] string."""

    def test_excluded_path_outside_root_is_refused(self, fake_project) -> None:
        root, outside = fake_project
        gates = _gates_module()
        cache = _pre_approval_cache(str(root))
        target = str(outside / "tests" / "test_x.py")
        result = gates._can_write_check("pb-3-a", _envelope(target), "", cache)
        assert result["can_write"] is False

    def test_same_shaped_path_inside_root_still_allows_and_logs_excluded(
        self, fake_project
    ) -> None:
        root, outside = fake_project
        gates = _gates_module()
        cache = _pre_approval_cache(str(root))
        target = str(root / "tests" / "test_x.py")
        result = gates._can_write_check("pb-3-b", _envelope(target), "", cache)
        assert result["can_write"] is True
        rows = _friction_rows()
        assert any(r.get("gate_status") == "excluded" for r in rows), rows


# --------------------------------------------------------------------------------- #
# Capability 4: a declared absolute ## Files bullet escapes the boundary.
# --------------------------------------------------------------------------------- #


class TestDeclaredAbsoluteFileEscapesTheBoundary:
    def test_a_fully_annotated_absolute_bullet_is_allowed(self, fake_project) -> None:
        root, outside = fake_project
        escape = outside / "shared" / "asset.py"
        plan = _plan_text(f"- `{escape}` (create) -- integrates with an external asset pipeline")
        sid = "pb-4"
        cache = _bound_post_approval_cache(root, sid, plan)
        gates = _gates_module()
        result = gates._can_write_check(sid, _envelope(str(escape)), "", cache)
        assert result["can_write"] is True


# --------------------------------------------------------------------------------- #
# Capabilities 1 + 4, held on ONE path: the declaration is the only variable.
# --------------------------------------------------------------------------------- #


class TestDeclarationIsTheOnlyVariable:
    """THE SAME-PATH PAIR. Capability 4 asserts that a DECLARED out-of-root path
    allows and capability 1 asserts that an UNDECLARED one denies, but they use
    different paths at different depths (`outside/shared/asset.py` versus
    `outside/thing.py`). A predicate that allowed deeper paths for a reason having
    nothing to do with the plan would satisfy BOTH and neither would notice: the
    deep path would allow because it is deep, and the shallow one would deny
    because it is shallow, and the pair would read as "the escape works". Here ONE
    path, at ONE depth, varies ONE thing: whether the approved plan's ## Files
    section declares it. Both halves bind the plan hash and both approve both
    gates; the widened plan differs from the baseline plan by exactly one bullet.

    Mutation (vi), which reds the ALLOW half only: delete the
    `declared_absolute_paths` arm from `_check_project_boundary`. The DENY half
    stays green and this half goes red, and that ASYMMETRY is the proof that the
    declaration is what flips the verdict rather than something incidental to the
    path.

    Mutation (vii), which reds the DENY half only: make the escape depth-driven
    instead of declaration-driven, e.g. return None from `_check_project_boundary`
    whenever the target sits more than one directory below the root's parent. Both
    halves of the capability-1 / capability-4 split stay GREEN under that mutation
    (their paths sit at different depths, on the two sides of the cut); this half
    goes red, because the same path allows with no bullet present.
    """

    #: The one path both halves judge. A module-level constant would need tmp_path,
    #: so it is a single expression used by both, never retyped per test.
    def _escape(self, outside: Path) -> Path:
        return outside / "shared" / "asset.py"

    def _baseline_bullet(self) -> str:
        return "- `tests/test_fixture.py` (create) -- baseline plan"

    def _verdict(self, root: Path, outside: Path, sid: str, declared: bool) -> dict:
        escape = self._escape(outside)
        bullets = self._baseline_bullet()
        if declared:
            bullets += (
                f"\n- `{escape}` (create) -- integrates with an external asset pipeline"
            )
        cache = _bound_post_approval_cache(root, sid, _plan_text(bullets))
        gates = _gates_module()
        return gates._can_write_check(sid, _envelope(str(escape)), "", cache)

    def test_the_same_path_denies_when_the_plan_does_not_declare_it(
        self, fake_project
    ) -> None:
        root, outside = fake_project
        result = self._verdict(root, outside, "pb-4b-undeclared", declared=False)
        assert result["can_write"] is False
        assert "ENF-PROJECT-BOUNDARY" in (result["reason"] or "")

    def test_the_same_path_allows_when_the_plan_declares_it(self, fake_project) -> None:
        root, outside = fake_project
        result = self._verdict(root, outside, "pb-4b-declared", declared=True)
        assert result["can_write"] is True
        assert result["reason"] is None


# --------------------------------------------------------------------------------- #
# Capability 5: the escape is not self-grantable without a fresh approval.
# --------------------------------------------------------------------------------- #


class TestEscapeIsNotSelfGrantable:
    """No single named mutation; executes D1's own load-bearing claim: adding a
    bullet to ## Files changes the digest, drops both gates, and
    [ENF-GATE-DRIFT] blocks every write until the user approves again."""

    def test_adding_the_bullet_without_a_fresh_approval_still_denies(
        self, fake_project
    ) -> None:
        root, outside = fake_project
        escape = outside / "shared" / "asset.py"
        original_plan = _plan_text("- `tests/test_fixture.py` (create) -- baseline plan")
        sid = "pb-5"
        cache = _bound_post_approval_cache(root, sid, original_plan)
        gates = _gates_module()

        baseline = gates._can_write_check(sid, _envelope(str(escape)), "", cache)
        assert baseline["can_write"] is False, "escape must not pre-exist before it is declared"

        widened_plan = _plan_text(
            "- `tests/test_fixture.py` (create) -- baseline plan\n"
            f"- `{escape}` (create) -- integrates with an external asset pipeline"
        )
        (root / "plan.md").write_text(widened_plan)  # edited with no fresh approval

        result = gates._can_write_check(sid, _envelope(str(escape)), "", cache)
        assert result["can_write"] is False
        assert "ENF-GATE-DRIFT" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# Capability 6: a reason-less ## Files bullet does not widen the boundary.
# --------------------------------------------------------------------------------- #


class TestReasonlessBulletDoesNotWiden:
    def test_a_bare_path_bullet_with_no_reason_does_not_widen(self, fake_project) -> None:
        root, outside = fake_project
        escape = outside / "shared" / "asset.py"
        plan = _plan_text(f"- `{escape}`")
        sid = "pb-6"
        cache = _bound_post_approval_cache(root, sid, plan)
        gates = _gates_module()
        result = gates._can_write_check(sid, _envelope(str(escape)), "", cache)
        assert result["can_write"] is False


# --------------------------------------------------------------------------------- #
# Capability 7: an absolute path named outside ## Files does not widen the boundary.
# --------------------------------------------------------------------------------- #


class TestBulletOutsideFilesSectionDoesNotWiden:
    """Mutation (iv): replace declared_absolute_paths's section-scoped parse with
    plan_harvest._extract_files; this capability must go red. That function scans
    prose bullets ANYWHERE in the document (Shape 2), including outside ## Files,
    which is exactly what the fixture below plants in ## Analysis."""

    def test_an_absolute_path_bullet_in_analysis_does_not_widen(self, fake_project) -> None:
        root, outside = fake_project
        escape = outside / "shared" / "asset.py"
        plan = _plan_text(
            "- `tests/test_fixture.py` (create) -- baseline plan",
            analysis_section=f"- `{escape}` - mentioned here but never declared as a Files bullet",
        )
        sid = "pb-7"
        cache = _bound_post_approval_cache(root, sid, plan)
        gates = _gates_module()
        result = gates._can_write_check(sid, _envelope(str(escape)), "", cache)
        assert result["can_write"] is False


# --------------------------------------------------------------------------------- #
# Capability 8: a relative ## Files bullet widens nothing.
# --------------------------------------------------------------------------------- #


class TestRelativeBulletWidensNothing:
    def test_relative_bullet_allows_in_root_and_still_refuses_a_different_root(
        self, fake_project
    ) -> None:
        root, outside = fake_project
        plan = _plan_text("- `src/extra.py` (create) -- an ordinary in-root file")
        sid = "pb-8"
        cache = _bound_post_approval_cache(root, sid, plan)
        gates = _gates_module()

        in_root = gates._can_write_check(sid, _envelope(str(root / "src" / "extra.py")), "", cache)
        assert in_root["can_write"] is True

        foreign = gates._can_write_check(
            sid, _envelope(str(outside / "src" / "extra.py")), "", cache
        )
        assert foreign["can_write"] is False


# --------------------------------------------------------------------------------- #
# Capability 9: `..` traversal, plain and through a symlinked directory.
# --------------------------------------------------------------------------------- #


class TestDotDotTraversal:
    """Mutation (ii): replace os.path.realpath with os.path.normpath in
    resolve_target; the symlinked-`..` case here must go red (normpath would
    collapse the `..` lexically back inside root and allow it)."""

    def test_plain_dot_dot_traversal_out_of_root_is_refused(self, fake_project) -> None:
        root, outside = fake_project
        (root / "src").mkdir()
        gates = _gates_module()
        cache = _post_approval_cache(str(root))
        target = os.path.join(str(root), "src", "..", "..", "outside-escape.py")
        result = gates._can_write_check("pb-9-a", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-PROJECT-BOUNDARY" in (result["reason"] or "")

    def test_dot_dot_through_a_symlinked_directory_inside_root_is_refused(
        self, fake_project
    ) -> None:
        root, outside = fake_project
        (outside / "real-dir").mkdir()
        link = root / "link"
        os.symlink(outside / "real-dir", link, target_is_directory=True)
        gates = _gates_module()
        cache = _post_approval_cache(str(root))
        # normpath would lexically collapse this to <root>/x.py and allow it;
        # realpath follows the symlink first and lands in <outside>/x.py.
        target = os.path.join(str(root), "link", "..", "x.py")
        result = gates._can_write_check("pb-9-b", _envelope(target), "", cache)
        assert result["can_write"] is False


# --------------------------------------------------------------------------------- #
# Capability 10: symlink directionality (inside pointing out; outside pointing in).
# --------------------------------------------------------------------------------- #


class TestSymlinkDirectionality:
    def test_symlink_inside_root_pointing_out_is_refused(self, fake_project) -> None:
        root, outside = fake_project
        real_secret = outside / "secret.py"
        real_secret.write_text("# secret\n")
        link = root / "evil_link.py"
        os.symlink(real_secret, link)
        gates = _gates_module()
        cache = _post_approval_cache(str(root))
        result = gates._can_write_check("pb-10-a", _envelope(str(link)), "", cache)
        assert result["can_write"] is False

    def test_symlink_outside_root_whose_target_is_inside_is_allowed(self, fake_project) -> None:
        root, outside = fake_project
        real_target = root / "real.py"
        real_target.write_text("# real\n")
        link = outside / "entry_link.py"
        os.symlink(real_target, link)
        gates = _gates_module()
        cache = _post_approval_cache(str(root))
        result = gates._can_write_check("pb-10-b", _envelope(str(link)), "", cache)
        assert result["can_write"] is True


# --------------------------------------------------------------------------------- #
# Capability 11: a project root recorded by its symlink spelling still works.
# --------------------------------------------------------------------------------- #


class TestSymlinkedProjectRoot:
    def test_a_path_under_the_symlink_spelling_of_root_is_allowed(self, tmp_path) -> None:
        real_proj = tmp_path / "real_proj"
        real_proj.mkdir()
        (real_proj / ".git").mkdir()
        link_proj = tmp_path / "link_proj"
        os.symlink(real_proj, link_proj, target_is_directory=True)
        gates = _gates_module()
        cache = _post_approval_cache(str(link_proj))
        target = str(link_proj / "src" / "thing.py")
        result = gates._can_write_check("pb-11", _envelope(target), "", cache)
        assert result["can_write"] is True


# --------------------------------------------------------------------------------- #
# Capability 12: a prefix-sibling directory of the root is refused.
# --------------------------------------------------------------------------------- #


class TestPrefixSiblingDirectory:
    """Mutation (iii): drop `+ os.sep` from is_contained's prefix comparison;
    this capability must go red."""

    def test_prefix_sibling_of_root_is_refused(self, tmp_path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        sibling = tmp_path / "proj-evil"
        sibling.mkdir()
        gates = _gates_module()
        cache = _post_approval_cache(str(root))
        target = str(sibling / "x.py")
        result = gates._can_write_check("pb-12", _envelope(target), "", cache)
        assert result["can_write"] is False

    def test_is_contained_rejects_a_prefix_sibling_directly(self) -> None:
        mod = _pb_module()
        assert mod.is_contained("/home/user/proj-evil/x.py", "/home/user/proj") is False
        assert mod.is_contained("/home/user/proj/x.py", "/home/user/proj") is True


# --------------------------------------------------------------------------------- #
# Capability 13: a recorded sub-root does not narrow the marker-walked boundary.
# --------------------------------------------------------------------------------- #


class TestRecordedRootNarrowerThanMarkerRootWidens:
    def test_write_elsewhere_under_the_marker_root_is_allowed(self, tmp_path) -> None:
        marker_root = tmp_path / "proj"
        marker_root.mkdir()
        (marker_root / ".git").mkdir()
        sub = marker_root / "app" / "code"
        sub.mkdir(parents=True)
        gates = _gates_module()
        # cache["project_root"] recorded the SUBDIRECTORY, mirroring
        # mode_engine.py:358's os.getcwd()-with-no-marker-walk recording.
        cache = _post_approval_cache(str(sub))
        target = str(marker_root / "other" / "file.py")
        result = gates._can_write_check("pb-13", _envelope(target), "", cache)
        assert result["can_write"] is True


# --------------------------------------------------------------------------------- #
# Capability 14: relative envelope paths resolve against root, not the process cwd.
# --------------------------------------------------------------------------------- #


class TestRelativeEnvelopePathResolvesAgainstRootNotCwd:
    def test_relative_in_root_path_allowed_with_cwd_pointed_elsewhere(
        self, tmp_path, monkeypatch
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        gates = _gates_module()
        cache = _post_approval_cache(str(root))
        result = gates._can_write_check("pb-14-a", _envelope("src/thing.py"), "", cache)
        assert result["can_write"] is True

    def test_relative_dot_dot_path_refused_with_cwd_pointed_elsewhere(
        self, tmp_path, monkeypatch
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        gates = _gates_module()
        cache = _post_approval_cache(str(root))
        result = gates._can_write_check("pb-14-b", _envelope("../outside-escape.py"), "", cache)
        assert result["can_write"] is False


# --------------------------------------------------------------------------------- #
# Capability 15: degenerate roots (empty/relative keeps today's allow; "/" contains
# everything).
# --------------------------------------------------------------------------------- #


class TestDegenerateRoots:
    @pytest.mark.parametrize("recorded_root", ["", "relative/path"])
    def test_empty_or_relative_root_keeps_todays_allow(self, fake_project, recorded_root) -> None:
        _root, outside = fake_project
        gates = _gates_module()
        cache = _post_approval_cache(recorded_root)
        target = str(outside / "anything.py")
        sid = f"pb-15-{recorded_root or 'empty'}".replace("/", "-")
        result = gates._can_write_check(sid, _envelope(target), "", cache)
        assert result["can_write"] is True

    def test_a_root_of_slash_contains_everything(self) -> None:
        gates = _gates_module()
        cache = _post_approval_cache("/")
        result = gates._can_write_check(
            "pb-15-slash", _envelope("/etc/anywhere-writ-fixture.py"), "", cache
        )
        assert result["can_write"] is True


class TestBoundaryRootPureFunction:
    def test_empty_recorded_root_yields_empty_string(self) -> None:
        mod = _pb_module()
        assert mod.boundary_root("") == ""

    def test_relative_recorded_root_yields_empty_string(self) -> None:
        mod = _pb_module()
        assert mod.boundary_root("relative/path") == ""

    def test_root_of_slash_is_contained_for_any_absolute_path(self) -> None:
        mod = _pb_module()
        assert mod.is_contained("/etc/anywhere.py", "/") is True


# --------------------------------------------------------------------------------- #
# Capability 16: the scratch-zone exemption is guarded both directions.
# --------------------------------------------------------------------------------- #


class TestScratchZoneGuardBothDirections:
    """The vacuity-trap capability. No single named mutation in Verification step
    4, but Verification step 1 names the failure mode directly: if this guard is
    missing, EVERY other 'outside is refused' assertion in this file would flip
    to allow the moment the real OS tempdir happens to contain tmp_path -- which
    it always does -- so this class isolates the mechanism with an explicitly
    monkeypatched zone, independent of that coincidence.

    SEAM CONTRACT for the implementer: `in_scratch_zone` must call
    `tempfile.gettempdir()` as a qualified attribute access (matching this
    module's own `import tempfile` usage), not a name bound once at import time
    via `from tempfile import gettempdir` -- these tests monkeypatch
    `tempfile.gettempdir` itself, which only takes effect if the callee re-reads
    it through the shared module object.
    """

    def test_exempt_when_root_is_not_inside_the_zone(self, tmp_path, monkeypatch) -> None:
        mod = _pb_module()
        root = tmp_path / "proj"
        root.mkdir()
        zone = tmp_path / "os-scratch"  # sibling of root, not an ancestor
        zone.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(zone))
        target = zone / "scratch-file.py"
        assert mod.in_scratch_zone(str(target), str(root)) is True

    def test_not_exempt_when_root_is_inside_the_zone(self, tmp_path, monkeypatch) -> None:
        mod = _pb_module()
        root = tmp_path / "proj"
        root.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))  # ancestor of root
        target = tmp_path / "scratch-file.py"  # inside the redefined zone too
        assert mod.in_scratch_zone(str(target), str(root)) is False

    def test_end_to_end_write_allowed_when_root_outside_the_redefined_zone(
        self, tmp_path, monkeypatch
    ) -> None:
        gates = _gates_module()
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        zone = tmp_path / "os-scratch"
        zone.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(zone))
        cache = _post_approval_cache(str(root))
        target = str(zone / "scratch-write.py")
        result = gates._can_write_check("pb-16-a", _envelope(target), "", cache)
        assert result["can_write"] is True

    def test_end_to_end_write_denied_when_root_inside_the_redefined_zone(
        self, tmp_path, monkeypatch
    ) -> None:
        gates = _gates_module()
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        cache = _post_approval_cache(str(root))
        target = str(tmp_path / "scratch-write.py")
        result = gates._can_write_check("pb-16-b", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-PROJECT-BOUNDARY" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# Capability 17: the credential deny still precedes the boundary.
# --------------------------------------------------------------------------------- #


class TestCredentialDenyPrecedesBoundary:
    """No named mutation in Verification step 4; this pins an ORDERING property a
    future reorder inside _can_write_check could silently break. The root is
    placed OUTSIDE the redefined scratch zone deliberately: if the ordering
    regressed and exempt/boundary logic ran before the credential guard, the
    scratch exemption WOULD grant this write, flipping the assertion from deny to
    allow -- non-vacuous, not merely 'still denies for an unrelated reason'."""

    def test_credential_file_inside_the_scratch_zone_is_denied_not_exempted(
        self, tmp_path, monkeypatch
    ) -> None:
        gates = _gates_module()
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        zone = tmp_path / "os-scratch"
        zone.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(zone))
        cache = _post_approval_cache(str(root))
        target = str(zone / ".env")
        result = gates._can_write_check("pb-17", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "SEC-CREDENTIAL-WRITE" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# Capability 18: an undeclared-scope sub-agent is confined to the parent's root.
# --------------------------------------------------------------------------------- #


class TestSubagentUndeclaredScopeConfinedToParentRoot:
    def _cache(self, parent_sid: str, **overrides) -> dict:
        base = {
            "is_subagent": True,
            "cache_source": "subagent_start",
            "agent_type": "writ-implementer",
            "role_source": "envelope",
            "role_write_scope": None,
            "parent_session_id": parent_sid,
        }
        base.update(overrides)
        return base

    def test_write_inside_the_parents_root_is_allowed(self, cache_dir, fake_project) -> None:
        root, _outside = fake_project
        parent_sid = "pb-18-parent-a"
        _write_cache_for(parent_sid, {"project_root": str(root)})
        gates = _gates_module()
        cache = self._cache(parent_sid)
        target = str(root / "src" / "thing.py")
        result = gates._can_write_check("pb-18-child-a", _envelope(target), "", cache)
        assert result["can_write"] is True

    def test_write_outside_the_parents_root_is_refused(self, cache_dir, fake_project) -> None:
        root, outside = fake_project
        parent_sid = "pb-18-parent-b"
        _write_cache_for(parent_sid, {"project_root": str(root)})
        gates = _gates_module()
        cache = self._cache(parent_sid)
        target = str(outside / "src" / "thing.py")
        result = gates._can_write_check("pb-18-child-b", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-PROJECT-BOUNDARY" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# Capability 19: a declared role scope is judged alone; the boundary never runs.
# --------------------------------------------------------------------------------- #


class TestSubagentDeclaredScopeIgnoresBoundary:
    """Regression guard, no named mutation: the role-scope arm runs BEFORE the new
    project-boundary arm in _check_exempt_write, and once it decides, the
    boundary must never run. Restates two cases tests/test_role_write_scope.py
    already pins, here to prove the new arm's placement did not quietly move
    ahead of it."""

    def _cache(self, parent_sid: str, scope) -> dict:
        return {
            "is_subagent": True,
            "cache_source": "subagent_start",
            "agent_type": "writ-test-writer",
            "role_source": "envelope",
            "role_write_scope": scope,
            "parent_session_id": parent_sid,
        }

    def test_an_out_of_project_declared_pattern_still_allows(self, cache_dir, fake_project) -> None:
        root, outside = fake_project
        parent_sid = "pb-19-parent-a"
        _write_cache_for(parent_sid, {"project_root": str(root)})
        gates = _gates_module()
        cache = self._cache(parent_sid, ["*/tests/*"])
        target = str(outside / "tests" / "test_x.py")
        result = gates._can_write_check("pb-19-a", _envelope(target), "", cache)
        assert result["can_write"] is True

    def test_an_empty_declared_scope_still_refuses_with_role_scope(
        self, cache_dir, fake_project
    ) -> None:
        root, _outside = fake_project
        parent_sid = "pb-19-parent-b"
        _write_cache_for(parent_sid, {"project_root": str(root)})
        gates = _gates_module()
        cache = self._cache(parent_sid, [])
        target = str(root / "src" / "thing.py")  # in-root, but scope is empty
        result = gates._can_write_check("pb-19-b", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-ROLE-SCOPE" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# Capability 19's structural half: the boundary arm never reads role_write_scope.
# --------------------------------------------------------------------------------- #


class TestBoundaryArmIgnoresRoleWriteScope:
    """The boundary arm's verdict does not depend on `role_write_scope` AT ALL.

    WHAT THIS IS AND IS NOT. Capability 19 pins the composition rule for the states a
    dispatch can actually produce: a role that DECLARES a scope is judged by
    `path_in_scope` and the boundary never runs, because `_check_role_scope_write` runs
    first and returns non-None. This class pins the STRUCTURAL reason that holds, which is
    that the boundary arm does not read the field, so the deferral is arm ORDER rather than
    a second opinion about the same data. It is the mirror image of the property
    tests/test_role_write_scope.py pins on the role-scope arm (four cache fields, never the
    parent's state): this arm reads the parent's project, never the role's scope.

    An `isinstance(role_write_scope, list) -> abstain` guard was written into this arm
    first and removed. In the ONE state where it changed anything it deferred to a judge
    that had already abstained, so nothing judged the path and a confinement became an
    unconditional allow.

    THIS IS A CONTRACT TEST OVER THE FUNCTION'S INPUT DOMAIN, NOT A REACHABILITY CLAIM.
    The cache below carries `agent_type="unknown"` (the resolver's own output for an
    unresolved role) together with a declared list, and the shipped corpus cannot produce
    that pairing: `subagent_seed._declared_scope` stamps None for an unknown role, no
    `SubagentRole` node is named `unknown`, and `fetch_declared_scope` strips before
    either guard sees the value. Nothing is patched here and nothing is asserted about
    reachability. The edge case's BEHAVIOUR is deliberately left unpinned, because
    specifying the symptom of the raw-versus-stripped guard mismatch as intended behaviour
    would outlive and discourage fixing it; field-independence is the property that stays
    true and stays desirable whatever `subagent_seed` later does.

    Mutation: re-add `if isinstance(cache.get("role_write_scope"), list): return None` to
    `_check_subagent_boundary`. The list case then abstains while the `None` case denies,
    the verdicts stop agreeing, and this class goes red.
    """

    SCOPES = (None, [], ["*/tests/*"])

    def _cache(self, parent_sid: str, scope) -> dict:
        return {
            "is_subagent": True,
            "cache_source": "subagent_start",
            "agent_type": "unknown",
            "role_source": "envelope",
            "role_write_scope": scope,
            "parent_session_id": parent_sid,
        }

    def test_the_role_scope_arm_abstains_for_every_scope_value(
        self, cache_dir, fake_project
    ) -> None:
        """The anti-vacuity precondition. If the role-scope arm DECIDED for any of these,
        the boundary arm would never be consulted and the comparison below would be
        comparing that arm's answers instead of this one's."""
        root, _outside = fake_project
        parent_sid = "pb-19b-pre"
        _write_cache_for(parent_sid, {"project_root": str(root)})
        gates = _gates_module()
        for scope in self.SCOPES:
            verdict = gates._check_role_scope_write(
                "pb-19b-pre-child", "work", str(root / "x.py"), self._cache(parent_sid, scope)
            )
            assert verdict is None, f"role-scope arm decided for scope={scope!r}: {verdict}"

    def test_outside_the_parent_root_every_scope_value_gives_one_verdict(
        self, cache_dir, fake_project
    ) -> None:
        root, outside = fake_project
        parent_sid = "pb-19b-out"
        _write_cache_for(parent_sid, {"project_root": str(root)})
        gates = _gates_module()
        target = str(outside / "tests" / "test_x.py")

        verdicts = [
            gates._check_subagent_boundary(
                f"pb-19b-out-{i}", "work", target, self._cache(parent_sid, scope)
            )
            for i, scope in enumerate(self.SCOPES)
        ]
        assert all(v is not None for v in verdicts), verdicts
        assert all(v["can_write"] is False for v in verdicts), verdicts
        reasons = {v["reason"] for v in verdicts}
        assert len(reasons) == 1, f"role_write_scope changed the boundary refusal: {reasons}"
        assert "ENF-PROJECT-BOUNDARY" in reasons.pop()

    def test_inside_the_parent_root_every_scope_value_gives_one_verdict(
        self, cache_dir, fake_project
    ) -> None:
        root, _outside = fake_project
        parent_sid = "pb-19b-in"
        _write_cache_for(parent_sid, {"project_root": str(root)})
        gates = _gates_module()
        target = str(root / "tests" / "test_x.py")

        verdicts = [
            gates._check_subagent_boundary(
                f"pb-19b-in-{i}", "work", target, self._cache(parent_sid, scope)
            )
            for i, scope in enumerate(self.SCOPES)
        ]
        assert verdicts == [None, None, None], (
            f"an in-project path must be no opinion for every scope value: {verdicts}"
        )


# --------------------------------------------------------------------------------- #
# Capability 20: no parent, or a parent with an empty root, keeps today's allow.
# --------------------------------------------------------------------------------- #


class TestSubagentAbsentOrEmptyParentRootAbstains:
    def test_no_parent_session_id_keeps_todays_allow(self, cache_dir, fake_project) -> None:
        _root, outside = fake_project
        gates = _gates_module()
        cache = {
            "is_subagent": True,
            "cache_source": "subagent_start",
            "agent_type": "writ-implementer",
            "role_source": "envelope",
            "role_write_scope": None,
            "parent_session_id": "",
        }
        target = str(outside / "src" / "thing.py")
        result = gates._can_write_check("pb-20-a", _envelope(target), "", cache)
        assert result["can_write"] is True

    def test_parent_with_empty_project_root_keeps_todays_allow(
        self, cache_dir, fake_project
    ) -> None:
        _root, outside = fake_project
        parent_sid = "pb-20-parent"
        _write_cache_for(parent_sid, {"project_root": ""})
        gates = _gates_module()
        cache = {
            "is_subagent": True,
            "cache_source": "subagent_start",
            "agent_type": "writ-implementer",
            "role_source": "envelope",
            "role_write_scope": None,
            "parent_session_id": parent_sid,
        }
        target = str(outside / "src" / "thing.py")
        result = gates._can_write_check("pb-20-b", _envelope(target), "", cache)
        assert result["can_write"] is True


# --------------------------------------------------------------------------------- #
# Capability 21: a lazy_seed cache abstains exactly like no cache at all.
# --------------------------------------------------------------------------------- #


class TestLazySeedAbstainsLikeNoCache:
    """Mutation (v): drop the cache_source == 'subagent_start' condition from the
    new sub-agent arm; this capability must go red.

    Today (and under a correctly scoped arm), a lazy_seed cache never reaches the
    new arm at all: is_lazily_seeded() already forces mode=None via
    _authority_mode, so BOTH sides of this comparison land on the same
    pre-existing [ENF-GATE-MODE] deny, unrelated to this cycle. If the mutation
    drops the cache_source guard, the new arm fires for the lazy_seed side (it
    still has a resolvable parent root) and produces a DIFFERENT reason
    ([ENF-PROJECT-BOUNDARY]) -- same can_write, different reason, which is what
    this comparison catches. PARTIALLY VACUOUS TODAY, DOCUMENTED: with no new arm
    in gates.py yet, both sides already agree for a reason unrelated to this
    cycle; it becomes a real regression fence once the arm exists.
    """

    def test_decision_and_reason_match_the_no_cache_baseline(self, cache_dir, fake_project) -> None:
        root, outside = fake_project
        parent_sid = "pb-21-parent"
        _write_cache_for(parent_sid, {"project_root": str(root)})
        gates = _gates_module()
        from writ.session.cache import _read_cache

        target = str(outside / "src" / "thing.py")
        no_cache = _read_cache("pb-21-no-such-session")
        reference = gates._can_write_check("pb-21-ref", _envelope(target), "", no_cache)

        lazy_cache = {
            "is_subagent": True,
            "cache_source": "lazy_seed",
            "agent_type": "writ-implementer",
            "role_source": "envelope",
            "parent_session_id": parent_sid,
        }
        combo = gates._can_write_check("pb-21-lazy", _envelope(target), "", lazy_cache)
        assert (combo["can_write"], combo["reason"]) == (
            reference["can_write"], reference["reason"]
        ), f"a lazy_seed cache diverged from the no-cache baseline: {combo} vs {reference}"


# --------------------------------------------------------------------------------- #
# Capability 22: every refusal names its action.
# --------------------------------------------------------------------------------- #


class TestRefusalsNameTheirAction:
    """No single named mutation in Verification step 4; this is plan.md D3's own
    requirement (HARD CONSTRAINT 6): a refusal naming no action is a deadlock this
    repo has already shipped once. Checked on substance, not exact prose."""

    def test_post_approval_refusal_names_files_and_approved(self, fake_project) -> None:
        root, outside = fake_project
        gates = _gates_module()
        cache = _post_approval_cache(str(root))
        target = str(outside / "thing.py")
        result = gates._can_write_check("pb-22-a", _envelope(target), "", cache)
        reason = result["reason"] or ""
        assert result["can_write"] is False
        assert "## Files" in reason
        assert "approved" in reason

    def test_dispatch_refusal_names_the_orchestrator(self, cache_dir, fake_project) -> None:
        root, outside = fake_project
        parent_sid = "pb-22-parent"
        _write_cache_for(parent_sid, {"project_root": str(root)})
        gates = _gates_module()
        cache = {
            "is_subagent": True,
            "cache_source": "subagent_start",
            "agent_type": "writ-implementer",
            "role_source": "envelope",
            "role_write_scope": None,
            "parent_session_id": parent_sid,
        }
        target = str(outside / "thing.py")
        result = gates._can_write_check("pb-22-b", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "orchestrator" in (result["reason"] or "").lower()


# --------------------------------------------------------------------------------- #
# Capability 23: exactly one write_attempt row; denial_counts is untouched.
# --------------------------------------------------------------------------------- #


class TestBoundaryDenyFrictionRowNotGateDenial:
    """No named mutation; pins the deliberate design choice (plan.md Integration
    section) that this refusal is NOT routed through _log_gate_denial, which
    would manufacture a fake 'approve the pending gate' remedy and would also
    increment denial_counts. Uses real on-disk cache/friction files, not an
    in-memory dict passed by reference, so a future switch to _log_gate_denial is
    caught by the denial_counts side effect itself."""

    def test_exactly_one_write_attempt_row_and_denial_counts_unchanged(
        self, cache_dir, fake_project
    ) -> None:
        root, outside = fake_project
        from writ.session.cache import _read_cache

        sid = "pb-23"
        _write_cache_for(sid, _post_approval_cache(str(root)))
        before = _read_cache(sid).get("denial_counts", {})

        gates = _gates_module()
        target = str(outside / "thing.py")
        result = gates._can_write_check(sid, _envelope(target))
        assert result["can_write"] is False

        after = _read_cache(sid).get("denial_counts", {})
        assert after == before, f"denial_counts changed on a boundary deny: {before} -> {after}"

        rows = _friction_rows()
        write_attempts = [r for r in rows if r.get("event") == "write_attempt"]
        assert len(write_attempts) == 1, write_attempts
        assert write_attempts[0].get("result") == "deny"
        assert write_attempts[0].get("gate_status") == "project_boundary_deny"
        assert not any(r.get("event") in ("gate_denial", "repeated_denial") for r in rows), rows


# --------------------------------------------------------------------------------- #
# Capability 24: the can-write route and the direct call agree.
# --------------------------------------------------------------------------------- #


class TestCanWriteRouteMatchesDirectCall:
    """ENF-SYS-005: this must run the REAL FastAPI route and the REAL gate, not a
    mocked _can_write_check -- a mocked route would replay whatever the test
    constructed and prove nothing about the two surfaces actually agreeing."""

    def test_same_decision_and_tag_through_route_and_direct_call(
        self, client, isolated_cache, tmp_path
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        sid = "pb-24"
        _write_cache_for(sid, _post_approval_cache(str(root)))
        target = str(outside / "thing.py")

        gates = _gates_module()
        from writ.session.cache import _read_cache

        direct = gates._can_write_check(sid, _envelope(target), "", cache=_read_cache(sid))

        response = client.post(
            f"/session/{sid}/can-write", json={"tool_input": {"file_path": target}}
        )
        assert response.status_code == 200
        routed = response.json()

        assert routed["can_write"] == direct["can_write"] is False
        assert routed["reason"] == direct["reason"]
        assert "ENF-PROJECT-BOUNDARY" in (routed["reason"] or "")


# --------------------------------------------------------------------------------- #
# Capability 25: no extra plan read on the common (in-project) path.
# --------------------------------------------------------------------------------- #


class TestNoExtraPlanReadOnTheCommonPath:
    """Mirrors plan.md's own Verification step 4 / PERF-QBUDGET-001 measurement:
    approved_gates_for_plan already needs exactly one _find_plan_md call (to
    fingerprint the plan for gate approval), so a SECOND call would be the
    boundary predicate's own escape-set parser reading the plan again. Patched to
    allow the first call through and raise on any further call, so this proves
    'no MORE than the one already-required read', not the stronger and
    structurally impossible 'zero calls at all'."""

    def test_in_project_write_allows_with_a_second_plan_read_forbidden(
        self, fake_project
    ) -> None:
        root, _outside = fake_project
        sid = "pb-25"
        cache = _post_approval_cache(str(root))
        gates = _gates_module()
        from writ.session import locators

        real_find_plan_md = locators._find_plan_md
        calls: list[tuple] = []

        def _guarded(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) > 1:
                raise AssertionError("a second plan.md read happened on the common path")
            return real_find_plan_md(*args, **kwargs)

        with mock.patch.object(locators, "_find_plan_md", side_effect=_guarded):
            target = str(root / "src" / "thing.py")
            result = gates._can_write_check(sid, _envelope(target), "", cache)

        assert result["can_write"] is True
        assert len(calls) <= 1, f"_find_plan_md was called {len(calls)} times: {calls}"


# --------------------------------------------------------------------------------- #
# HARD CONSTRAINT 1's guard.
# --------------------------------------------------------------------------------- #


class TestFixturesNeverNameARealProjectRoot:
    """A full runtime-instrumentation guard (mirroring
    tests/test_role_write_scope.py's fetcher-raises test) is NOT cleanly
    expressible for this predicate: role_scope.py's fetch_declared_scope is one
    function every dispatch funnels through, but project_boundary's
    project_root argument flows from dozens of independently-constructed cache
    dicts across this whole file, with no single seam to monkeypatch and assert
    against. This is the honest, weaker substitute the orchestrator's
    instructions allow for when the strong form is not expressible: a static
    check on this file's OWN source, so a future fixture that names REPO or the
    real HOME as a project_root is caught by grep rather than by execution.
    """

    def test_no_project_root_literal_names_repo_or_home(self) -> None:
        # Scan only the fixture-defining text ABOVE this class: the forbidden
        # needles below are deliberately literal, so scanning the whole file
        # would have this very list match itself and fail unconditionally.
        source, _, _ = Path(__file__).read_text().partition(
            "class TestFixturesNeverNameARealProjectRoot"
        )
        forbidden = (
            "_post_approval_cache(str(REPO",
            "_pre_approval_cache(str(REPO",
            "_post_approval_cache(str(Path.home()",
            "_pre_approval_cache(str(Path.home()",
            '"project_root": str(REPO',
            '"project_root": str(Path.home()',
        )
        hits = [needle for needle in forbidden if needle in source]
        assert hits == [], f"a fixture named a real, non-tmp_path root: {hits}"
