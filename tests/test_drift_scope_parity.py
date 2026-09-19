"""Both write doors must answer the same way under plan drift.

WHAT PROMPTED THIS DID NOT REPRODUCE, and that is recorded rather than quietly
dropped. A live observation during the 18a cycle had the Bash gate allowing
`writ/gate_probe_tmp.py` under real drift. Measured here against a drifted
project, EVERY write idiom (shell redirect, python write_text with an absolute
path, the same with a relative path, and open()) is DENIED for an in-project
non-excluded target, and the predicate denies it too. The live observation has no
explanation yet; it is not this.

WHAT DID REPRODUCE is a divergence in the OPPOSITE direction. Under drift the
Bash gate DENIES a path the category exclusions make writable, while
`_can_write_check` ALLOWS it. docs/reference/session-and-gates.md:40 states the
exclusion list stays writable when a gate has not been approved, so the predicate
matches the documented policy and the Bash gate is STRICTER than documented. That
is an over-refusal, not a bypass, which is why it is recorded as a strict xfail
and fixed in its own cycle rather than patched here from a guess.

The only pre-existing drift test (test_project_boundary.py::
TestEscapeIsNotSelfGrantable) uses an OUT-OF-PROJECT target, so the in-project
case had no coverage at either layer, which is how this could exist with no red
test.

BOTH SIDES ARE REAL CALLERS. Layer A is the `_can_write_check` predicate the Write
tool uses; layer B runs the actual hook script in a subprocess. Neither is a model
of the other (TEST-ASSERT-001). The tree, plan and cache come from
test_project_boundary's own constructors, so this module cannot drift away from
the state that file defines, and nothing reads this machine's real session
(TEST-ISOLATE-001).
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
HOOK_SH = SKILL_ROOT / "hooks" / "scripts" / "writ-bash-write-gate.sh"


def _imp(name):
    if str(SKILL_ROOT) not in sys.path:
        sys.path.insert(0, str(SKILL_ROOT))
    return importlib.import_module(name)


@pytest.fixture()
def drifted(tmp_path):
    """A project whose plan.md was edited after its gates were approved.

    The cache and hash come from test_project_boundary's own constructors rather
    than a second hand-rolled copy: two models of one state is exactly how the
    readers in this repo drifted apart before.
    """
    sys.path.insert(0, str(SKILL_ROOT / "tests"))
    tb = importlib.import_module("test_project_boundary")

    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    (root / ".git").mkdir()

    plan = tb._plan_text("- `tests/test_fixture.py` (create) -- baseline")
    sid = "drift-parity"
    cache = tb._bound_post_approval_cache(root, sid, plan)

    # DRIFT: edit plan.md with no fresh approval.
    (root / "plan.md").write_text(plan + "\n- `src/widened.py` (create) -- widened\n")
    return sid, root, cache


def _predicate_allows(sid, cache, path) -> bool:
    tb = importlib.import_module("test_project_boundary")
    gates = tb._gates_module()
    return bool(gates._can_write_check(sid, tb._envelope(str(path)), "", cache)["can_write"])


def _bash_gate_allows(sid, root, path) -> bool:
    envelope = json.dumps({"session_id": sid, "tool_name": "Bash",
                           "tool_input": {"command": f"echo x > {path}"}})
    p = subprocess.run(["bash", str(HOOK_SH)], input=envelope, cwd=str(root),
                       capture_output=True, text=True)
    out = p.stdout.strip()
    if not out:
        return True  # no decision emitted is the allow path
    decision = json.loads(out).get("hookSpecificOutput", {}).get("permissionDecision")
    return decision not in ("deny", "ask")


class TestBothDoorsAgreeUnderDrift:
    """One case per path class, so a divergence names the class it belongs to
    rather than disappearing into an aggregate (TEST-EDGE-001)."""

    def test_the_two_doors_agree_for_a_non_excluded_path(self, drifted):
        sid, root, cache = drifted
        target = root / "src" / "not_excluded.py"
        a = _predicate_allows(sid, cache, target)
        b = _bash_gate_allows(sid, root, target)
        assert a == b is False, f"predicate allows={a}, bash gate allows={b}"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "MEASURED 2026-09-18: under plan drift the Bash gate DENIES a path the "
            "category exclusions make writable, while _can_write_check ALLOWS it. "
            "session-and-gates.md:40 says the exclusion list stays writable, so the "
            "Bash gate is stricter than the documented policy. Over-refusal, not a "
            "bypass. Deferred to its own cycle; strict so the fix reports itself here."
        ),
    )
    def test_the_two_doors_agree_for_an_excluded_path(self, drifted):
        sid, root, cache = drifted
        target = root / "tests" / "excluded_by_category.py"
        a = _predicate_allows(sid, cache, target)
        b = _bash_gate_allows(sid, root, target)
        assert a == b, (
            f"the two write doors disagree for an excluded path under drift: "
            f"_can_write_check allows={a}, the Bash gate allows={b}."
        )

    @pytest.mark.parametrize("cmd_tpl", [
        "echo x > {t}",
        "python3 -c \"import pathlib; pathlib.Path('{t}').write_text('x')\"",
        "python3 -c \"open('{t}','w').write('x')\"",
    ])
    def test_every_write_idiom_is_refused_for_a_non_excluded_path(self, drifted, cmd_tpl):
        """The live observation that prompted this module claimed one idiom slipped
        through. It does not: all three are denied."""
        sid, root, cache = drifted
        target = root / "src" / "not_excluded.py"
        envelope = json.dumps({"session_id": sid, "tool_name": "Bash",
                               "tool_input": {"command": cmd_tpl.format(t=target)}})
        p = subprocess.run(["bash", str(HOOK_SH)], input=envelope, cwd=str(root),
                           capture_output=True, text=True)
        assert p.stdout.strip(), f"no decision emitted for: {cmd_tpl}"
        d = json.loads(p.stdout).get("hookSpecificOutput", {})
        assert d.get("permissionDecision") in ("deny", "ask"), d


class TestTheInProjectDriftCaseIsCoveredAtAll:
    """The coverage gap itself, independent of the parity question: before this
    module, no test asked either layer about an in-project target under drift."""

    def test_predicate_refuses_an_in_project_non_excluded_path(self, drifted):
        sid, root, cache = drifted
        assert _predicate_allows(sid, cache, root / "src" / "not_excluded.py") is False

    def test_predicate_still_allows_the_documented_exclusion(self, drifted):
        sid, root, cache = drifted
        assert _predicate_allows(sid, cache, root / "tests" / "t.py") is True
