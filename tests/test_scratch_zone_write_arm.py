"""The scratch-zone allow arm: a pre-approval write to the OS temp directory stops being
refused, without weakening anything else the work gate already decides.

THE DEFECT THIS PINS. Measured live in a work-mode session with no gates approved:
`/tmp/writ-scratch-xyz` and `/tmp/scratch.py` both returned `can_write False` with
`[ENF-GATE-PLAN]`, on both write doors. `in_scratch_zone`
(`writ/session/project_boundary.py:108-126`) already returns True for these paths and always
has -- it only SUPPRESSES the project-boundary refusal inside `_check_project_boundary`, and
that function is never reached before either work-mode gate is approved, so suppressing a
refusal nobody was about to issue created no allow. The fix (`writ/session/gates.py`,
`_check_work_gate`, between the drift return and the `[ENF-GATE-PLAN]` return) is one arm:
resolve the target the same way the boundary resolves it, and allow when it lands in the
zone and the project root does not.

EVERY CAPABILITY IS PINNED AT THE DECISION LEVEL, through `gates._can_write_check`, never on
`in_scratch_zone` in isolation. `in_scratch_zone` already answered True for these paths before
this cycle and the write was still refused, so a green predicate-only test proves nothing
about the defect this file exists to catch.

THE FIXTURE, modeled on `tests/test_write_door_parity.py`'s `doors`: a project root at
`tmp_path/"proj"` carrying a `.git` marker, and a zone at `tmp_path/"osscratch"` that is a
SIBLING of the root and not an ancestor. THE ZONE IS A CACHE VALUE NOW, NOT A PROCESS
ENVIRONMENT: `in_scratch_zone(target, root, zone)` is a pure three-argument predicate
(`writ/session/project_boundary.py` imports no `tempfile` at all), and `gates.py` resolves
`zone` from `cache.get("scratch_zone")`. Every fixture that intends the scratch arm to be
REACHABLE therefore stamps `scratch_zone` explicitly via `_pre_approval_cache`'s now-required
`zone` argument, exactly the way `project_root` was already required; no test here
monkeypatches `tempfile` or `TMPDIR`.

`boundary_root` ABSTAINS ON AN EMPTY RECORDED ROOT (`writ/session/cache.py:274` defaults
`project_root` to `""`), so every fixture below that intends the arm to FIRE stamps a root
explicitly, and the empty/relative-root case is pinned as its own deliberate capability
(`TestNoRecordedProjectAbstains`) rather than left to happen by fixture accident. The
zone-absent case gets the same treatment (`TestAbsentZoneStampDeniesTheZoneShapedTarget`):
a stamped root with NO stamped zone must still deny a zone-shaped target, a decision this
file states rather than a side effect of a fixture that forgot to stamp one.

No test in this file asserts on README, HANDBOOK, ADR or any other doc prose.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def _gates_module():
    from writ.session import gates
    return gates


def _envelope(path: str) -> dict:
    return {"tool_input": {"file_path": path}}


def _friction_rows() -> list[dict]:
    import json
    path = Path(os.environ["WRIT_FRICTION_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _pre_approval_cache(root: str, zone: str, **overrides) -> dict:
    """`zone` is REQUIRED, with no default: a fixture that forgets to stamp it must fail
    loudly (a TypeError at call time) rather than read green through a fail-closed deny
    that happens to match an expected deny."""
    cache = {
        "mode": "work",
        "current_phase": None,
        "gates_approved": [],
        "gates_approved_plan": {},
        "project_root": root,
        "scratch_zone": zone,
        "is_subagent": False,
    }
    cache.update(overrides)
    return cache


def _drifted_cache(root: str, zone: str) -> dict:
    """Both work gates carry a hash that does not match the plan on disk (there is none --
    `plan_md_hash` returns None for a root with no plan.md), which is the state
    `_check_work_gate`'s `drifted` list is computed from."""
    return _pre_approval_cache(
        root, zone,
        gates_approved=["phase-a", "test-skeletons"],
        gates_approved_plan={"phase-a": "stale-hash", "test-skeletons": "stale-hash"},
    )


def _test_skeletons_pending_cache(root: str, zone: str) -> dict:
    """`phase-a` approved and bound to "no plan.md at all" (None on both sides of the
    comparison, genuinely equal); `test-skeletons` never granted."""
    return _pre_approval_cache(
        root, zone,
        gates_approved=["phase-a"],
        gates_approved_plan={"phase-a": None},
    )


@pytest.fixture()
def zone(tmp_path) -> SimpleNamespace:
    """A project root and a sibling OS-scratch zone. `in_scratch_zone(target, root, zone)`
    is a pure three-argument predicate now (no `tempfile` import in
    `project_boundary.py`), so the zone travels as an explicit cache value
    (`_pre_approval_cache`'s `zone` argument) rather than as a monkeypatched
    `tempfile.gettempdir`/`TMPDIR`."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    z = tmp_path / "osscratch"
    z.mkdir()
    return SimpleNamespace(root=root, zone=z)


# --------------------------------------------------------------------------------- #
# The measured defect: pre-approval, a scratch write allows.
# --------------------------------------------------------------------------------- #


class TestPreApprovalScratchWriteIsAllowed:
    """Pre-approval, work mode, the project root outside the zone: a write directly in the
    zone and one nested a directory deeper must both allow, and the allow must leave a
    countable `write_attempt` row -- an allow that logs nothing is indistinguishable from an
    arm that never ran."""

    def test_a_file_directly_in_the_zone_is_allowed(self, zone) -> None:
        gates = _gates_module()
        cache = _pre_approval_cache(str(zone.root), str(zone.zone))
        target = str(zone.zone / "scratch.py")
        result = gates._can_write_check("sza-1a", _envelope(target), "", cache)
        assert result["can_write"] is True
        assert result["reason"] is None

    def test_a_file_nested_a_directory_deeper_is_allowed(self, zone) -> None:
        gates = _gates_module()
        cache = _pre_approval_cache(str(zone.root), str(zone.zone))
        target = str(zone.zone / "nested" / "scratch.py")
        result = gates._can_write_check("sza-1b", _envelope(target), "", cache)
        assert result["can_write"] is True

    def test_the_allow_emits_one_write_attempt_row_tagged_scratch_zone(self, zone) -> None:
        gates = _gates_module()
        cache = _pre_approval_cache(str(zone.root), str(zone.zone))
        target = str(zone.zone / "scratch.py")
        result = gates._can_write_check("sza-1c", _envelope(target), "", cache)
        assert result["can_write"] is True

        rows = _friction_rows()
        matches = [
            r for r in rows
            if r.get("event") == "write_attempt" and r.get("file_path") == target
        ]
        assert matches, f"no write_attempt row named {target}: {rows}"
        assert matches[-1].get("result") == "allow"
        assert matches[-1].get("gate_status") == "scratch_zone"


# --------------------------------------------------------------------------------- #
# The placement decision: drift stays strictly stronger than the new arm.
# --------------------------------------------------------------------------------- #


class TestDriftStillRefusesAScratchWrite:
    """The arm sits AFTER the drift check, on purpose (plan.md's "Why this placement"): a
    drifted session must still refuse a scratch write with `[ENF-GATE-DRIFT]`, or a later
    reader moves the arm up for symmetry and takes the drift stop with it."""

    def test_drifted_session_denies_a_zone_target_with_enf_gate_drift(self, zone) -> None:
        gates = _gates_module()
        cache = _drifted_cache(str(zone.root), str(zone.zone))
        target = str(zone.zone / "scratch.py")
        result = gates._can_write_check("sza-2", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-GATE-DRIFT" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# The in-scope behavior change beyond the measured defect: the test-skeletons window.
# --------------------------------------------------------------------------------- #


class TestTestSkeletonsWindowAlsoAllows:
    """The arm sits ahead of `[ENF-GATE-TEST]` too, not only `[ENF-GATE-PLAN]`: with
    `phase-a` approved and `test-skeletons` still pending, a scratch write must allow rather
    than refuse."""

    def test_zone_target_allows_with_phase_a_approved_and_test_skeletons_pending(
        self, zone
    ) -> None:
        gates = _gates_module()
        cache = _test_skeletons_pending_cache(str(zone.root), str(zone.zone))
        target = str(zone.zone / "scratch.py")
        result = gates._can_write_check("sza-3", _envelope(target), "", cache)
        assert result["can_write"] is True
        assert result["reason"] is None


# --------------------------------------------------------------------------------- #
# The credential guard runs far ahead of _check_work_gate and must not be overtaken.
# --------------------------------------------------------------------------------- #


class TestCredentialInsideTheZoneStillDenies:
    """`_is_credential_path` denies inside `_can_write_check`, before `_check_work_gate` (and
    therefore this arm) is ever reached.

    HONEST SCOPE, because "non-vacuous by construction" overstated it: `_check_work_gate`
    has exactly ONE caller and the credential deny runs unconditionally above it, so NO
    mutation confined to this arm or to that function can turn these red. They are not
    coverage of the scratch arm. What they pin is `_can_write_check`'s own top-level
    ORDERING, and they fail only under a refactor that hoists a gate arm above the
    credential check. Kept for that reason, and labelled so nobody counts them twice."""

    @pytest.mark.parametrize("basename", [".env", "id_rsa", "x.pem"])
    def test_credential_basename_in_the_zone_denies_with_sec_credential_write(
        self, zone, basename
    ) -> None:
        gates = _gates_module()
        cache = _pre_approval_cache(str(zone.root), str(zone.zone))
        target = str(zone.zone / basename)
        result = gates._can_write_check(f"sza-4-{basename}", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "SEC-CREDENTIAL-WRITE" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# The root-inside-the-zone guard, honored for free by reusing in_scratch_zone unchanged.
# --------------------------------------------------------------------------------- #


class TestRootInsideTheZoneStillRefusesSiblings:
    """`in_scratch_zone` switches the exemption OFF when the project root is itself inside
    the stamped zone (`project_boundary.py`'s `in_scratch_zone`), so a sibling under that
    same directory must still deny pre-approval. This is the pre-approval direction of the
    guard `tests/test_project_boundary.py:737-749` already pins post-approval."""

    def test_a_sibling_under_the_same_zone_denies_with_enf_gate_plan(
        self, tmp_path
    ) -> None:
        gates = _gates_module()
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        # tmp_path is stamped AS the zone, and root sits inside it -- exactly the
        # condition that switches the exemption off.
        cache = _pre_approval_cache(str(root), str(tmp_path))
        target = str(tmp_path / "scratch-write.py")
        result = gates._can_write_check("sza-5", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-GATE-PLAN" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# The arm is keyed on the ZONE, not on merely being outside the project.
# --------------------------------------------------------------------------------- #


class TestNonScratchOutOfRepoStillRefused:
    """A path outside the project but ALSO outside the scratch zone must still deny
    pre-approval: this is what proves the new arm is keyed on the zone and not on any
    out-of-project target."""

    def test_a_directory_outside_the_project_and_the_zone_denies(self, zone) -> None:
        gates = _gates_module()
        cache = _pre_approval_cache(str(zone.root), str(zone.zone))
        target = str(zone.root.parent / "elsewhere" / "thing.py")
        result = gates._can_write_check("sza-6a", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-GATE-PLAN" in (result["reason"] or "")

    def test_a_real_absolute_path_outside_the_zone_denies(self, zone) -> None:
        # Envelope value only, never opened -- _can_write_check is a pure decision function,
        # matching this suite's hard constraint (tests/test_project_boundary.py's module
        # docstring, HARD CONSTRAINT 1).
        gates = _gates_module()
        cache = _pre_approval_cache(str(zone.root), str(zone.zone))
        target = "/etc/writ-scratch-arm-probe.txt"
        result = gates._can_write_check("sza-6b", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-GATE-PLAN" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# A path spelled inside the zone whose RESOLVED target is outside it is refused.
# --------------------------------------------------------------------------------- #


class TestZoneEscapesAreNotInTheZone:
    """A spelling that leaves the zone is judged where the OS will actually write, not where
    the raw string appears to point.

    Only the SYMLINK test discriminates `realpath` from `normpath`; a plain `..` collapses
    to the same resolved path under either, so the `..` case pins the escape but says
    nothing about which resolver is used. Both are kept: `..` is the spelling a caller is
    most likely to pass, and the symlink is the one that would slip past a lexical
    normalizer. Only files and symlinks under the test's own `tmp_path` are created."""

    def test_a_symlink_in_the_zone_pointing_outside_denies(self, zone) -> None:
        gates = _gates_module()
        elsewhere = zone.zone.parent / "elsewhere"
        elsewhere.mkdir()
        link = zone.zone / "escape_link"
        os.symlink(elsewhere, link, target_is_directory=True)
        cache = _pre_approval_cache(str(zone.root), str(zone.zone))
        target = str(link / "x.py")
        result = gates._can_write_check("sza-7a", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-GATE-PLAN" in (result["reason"] or "")

    def test_a_dot_dot_spelling_out_of_the_zone_denies(self, zone) -> None:
        gates = _gates_module()
        cache = _pre_approval_cache(str(zone.root), str(zone.zone))
        target = os.path.join(str(zone.zone), "..", "elsewhere", "x.py")
        result = gates._can_write_check("sza-7b", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-GATE-PLAN" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# The empty-root abstain is a decision, not an accident.
# --------------------------------------------------------------------------------- #


class TestNoRecordedProjectAbstains:
    """`boundary_root` abstains ("") on an empty or relative recorded root
    (`cache.py:274` defaults `project_root` to `""`), and the arm requires a non-empty
    `scratch_root` before it will fire. A session with nothing recorded must therefore keep
    denying a zone target with `[ENF-GATE-PLAN]`, not have the arm allow against no root."""

    @pytest.mark.parametrize("root", ["", "relative/path"])
    def test_empty_or_relative_root_still_denies_a_zone_target(self, zone, root) -> None:
        # The zone STAYS stamped here, on purpose: varying only the root isolates the
        # ROOT abstain, so this class does not silently become a second test of the
        # zone-absent abstain (TestAbsentZoneStampDeniesTheZoneShapedTarget, below).
        gates = _gates_module()
        cache = _pre_approval_cache(root, str(zone.zone))
        target = str(zone.zone / "scratch.py")
        sid = f"sza-8-{root or 'empty'}".replace("/", "-")
        result = gates._can_write_check(sid, _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-GATE-PLAN" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# The zone-absent abstain is a decision, not an accident (fail-closed on the OTHER key).
# --------------------------------------------------------------------------------- #


class TestAbsentZoneStampDeniesTheZoneShapedTarget:
    """The mirror of `TestNoRecordedProjectAbstains`, on the OTHER key: a stamped root
    with NO stamped zone must deny a zone-shaped target with `[ENF-GATE-PLAN]`, not fall
    back to resolving a zone live. `scratch_zone("")` returns `""`, and
    `in_scratch_zone(target, root, "")` is False for every target -- there is no live
    `tempfile.gettempdir()` left to fall back to at all."""

    def test_a_stamped_root_with_no_stamped_zone_denies_a_zone_shaped_target(
        self, zone
    ) -> None:
        gates = _gates_module()
        cache = _pre_approval_cache(str(zone.root), "")
        target = str(zone.zone / "scratch.py")
        result = gates._can_write_check("sza-10", _envelope(target), "", cache)
        assert result["can_write"] is False
        assert "ENF-GATE-PLAN" in (result["reason"] or "")


# --------------------------------------------------------------------------------- #
# Conditionality at the gates.in_scratch_zone seam.
# --------------------------------------------------------------------------------- #


class TestTheArmIsConditional:
    """With `gates.in_scratch_zone` disabled at the module-level bound name
    (`gates.py:23-34` imports it into that namespace, which is the seam), the same
    pre-approval zone write must deny with `[ENF-GATE-PLAN]`, while the unmutated call in the
    same session state allows -- so this pin cannot pass against an already-broken arm."""

    def test_unmutated_allows_and_mutated_denies_the_same_zone_target(
        self, zone, monkeypatch
    ) -> None:
        gates = _gates_module()
        cache = _pre_approval_cache(str(zone.root), str(zone.zone))
        target = str(zone.zone / "scratch.py")

        baseline = gates._can_write_check("sza-9-baseline", _envelope(target), "", cache)
        assert baseline["can_write"] is True, (
            "the unmutated arm must allow first, or this pin cannot distinguish the "
            "mutation from an arm that never worked in the first place"
        )

        monkeypatch.setattr(gates, "in_scratch_zone", lambda target, root, zone: False)
        mutated = gates._can_write_check("sza-9-mutated", _envelope(target), "", cache)
        assert mutated["can_write"] is False
        assert "ENF-GATE-PLAN" in (mutated["reason"] or "")
