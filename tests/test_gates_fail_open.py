"""Cycle H skeletons: two gates that answer yes where they owe no.

From the Maat comparison (2026-08-21), re-verified against today's code. Both are Writ
contradicting itself rather than a new policy.

1. STRICT MODE IS DEFEATED BY A CRASHED EVALUATOR. `bin/lib/common.sh:1662` ends the
   local fallback with `|| echo '{"decision":"allow"}'`. The strict check at
   `common.sh:1670` covers only the arm where no session id was found, so a fallback
   that CRASHES allows the write even with `WRIT_STRICT=1`. Reachable in production:
   `hooks/scripts/writ-pre-write-dispatch.sh:142` passes the session id, so the crash
   arm is the one a real outage lands in. Verified by running the pipeline with a skill
   dir that lacks `bin/lib/writ-session.py`: the helper exits 2 with empty stdout and
   the `||` default fires.

   NOTE ON THE HARNESS. `_writ_session` shifts its arguments (`common.sh:1410-1413`), so
   the body is the THIRD argument. `tests/test_strict_mode.py` passes two on purpose, to
   reach the no-session-id arm; passing two here would test that arm again and never
   touch the crash path. One test below pins the two-argument behaviour so the
   distinction cannot rot.

2. THE WRITE GATE IGNORES THE PLAN ITS APPROVAL NAMED. The advance side counts an
   approval only for the plan it was granted against (`mode_engine.py:141-146`, using
   `plan_md_hash`); the write gate reads `gates_approved` alone (`gates.py:409`). After a
   plan edit the advance is refused for drift while source writes continue against a plan
   that no longer exists.

   TICKING A CAPABILITY BOX IS NOT DRIFT. `plan_md_hash` normalizes `- [x]` to `- [ ]`
   before hashing (`locators.py:315-324`), so finishing a cycle the documented way does
   not re-block. A test pins that, because it is what keeps this liveable.

DEFECT 1 FROM THAT REPORT IS DELIBERATELY NOT FIXED. `cmd_invalidate_gate` records and
escalates rather than revoking, and the user ruled that Writ's way is better: only a human
grants or clears a gate, so a lexical rule-validator must not strip an approval. The last
class pins that decision as behaviour, so a later cycle cannot "fix" it silently.

The docstring correction in `violations.py` has no test here: asserting on prose is
forbidden in this repo, so it is verified by reading the diff.

Per ENF-GATE-007: skeletons written and approved before implementation.
Per TEST-ISOLATE-003: every test owns its cache dir and project root under tmp_path, and
no test touches the developer's real session cache or plan.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
COMMON_SH = SKILL_ROOT / "bin" / "lib" / "common.sh"
DEAD_PORT = "59999"

PLAN_BODY = """# Plan: a fixture

## Files

- `src/thing.py` (modify) -- the fixture's only file

## Analysis

Fixture plan.

## Rules Applied

No matching rules.

## Capabilities

- [ ] the fixture behaves
"""


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #

def _write_plan(project_root: Path, session_id: str, body: str = PLAN_BODY) -> Path:
    """Create the session-scoped plan.md that `plan_md_hash` fingerprints."""
    directory = project_root / ".claude" / "plans" / session_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plan.md"
    path.write_text(body)
    return path


def _work_cache(project_root: Path, plan_hash: str | None) -> dict:
    """A work-mode cache with both gates approved, bound to `plan_hash`.

    `plan_hash=None` leaves `gates_approved_plan` empty, which is the unfingerprinted
    case a session whose state predates the binding is in.
    """
    bound = {} if plan_hash is None else {
        "phase-a": plan_hash, "test-skeletons": plan_hash,
    }
    return {
        "mode": "work",
        "current_phase": "implementation",
        "gates_approved": ["phase-a", "test-skeletons"],
        "gates_approved_plan": bound,
        "project_root": str(project_root),
        "denial_counts": {},
    }


def _envelope(path: str) -> dict:
    return {"tool_input": {"file_path": path}}


# --------------------------------------------------------------------------- #
# Capability 1, 2, 3: strict mode survives a crashed evaluator
# --------------------------------------------------------------------------- #

class TestStrictModeSurvivesAnEvaluatorCrash:
    """The crash arm, driven through bash with three arguments the way production does."""

    @staticmethod
    def _pre_write_check(session_id: str, body: str, *, strict: bool,
                         skill_dir: str, socket: str) -> dict:
        env = {
            **os.environ,
            "WRIT_PORT": DEAD_PORT,
            "WRIT_NO_AUTOSTART": "1",
            "WRIT_SOCKET": socket,
            "SKILL_DIR": skill_dir,
        }
        if strict:
            env["WRIT_STRICT"] = "1"
        else:
            env.pop("WRIT_STRICT", None)
        script = f'source "{COMMON_SH}"; _writ_session pre-write-check "$1" "$2"'
        proc = subprocess.run(
            ["bash", "-c", script, "bash", session_id, body],
            capture_output=True, text=True, env=env, timeout=60,
        )
        out = proc.stdout.strip().splitlines()
        if not out:
            pytest.fail(f"no decision emitted; stderr={proc.stderr[-400:]}")
        return json.loads(out[-1])

    @staticmethod
    def _broken_skill_dir(tmp_path: Path) -> str:
        """A skill dir with no `bin/lib/writ-session.py`, so the helper exits non-zero
        with empty stdout. This is a partial or broken install, not a contrived state."""
        broken = tmp_path / "broken-skill"
        broken.mkdir()
        return str(broken)

    @staticmethod
    def _dead_socket(tmp_path: Path) -> str:
        return str(tmp_path / "no-such.sock")

    _BODY = json.dumps({"session_id": "cycle-h-crash",
                        "tool_input": {"file_path": "/proj/src/foo.py"}})

    def test_a_crashed_evaluator_denies_under_strict(self, tmp_path) -> None:
        """THE DEFECT. An operator who opted into failing closed gets an allow."""
        result = self._pre_write_check(
            "cycle-h-crash", self._BODY, strict=True,
            skill_dir=self._broken_skill_dir(tmp_path),
            socket=self._dead_socket(tmp_path),
        )
        assert result["decision"] == "deny", (
            "a crashed local fallback allowed the write with WRIT_STRICT=1: " + repr(result)
        )

    def test_the_strict_denial_names_the_reason_and_the_way_out(self, tmp_path) -> None:
        result = self._pre_write_check(
            "cycle-h-crash", self._BODY, strict=True,
            skill_dir=self._broken_skill_dir(tmp_path),
            socket=self._dead_socket(tmp_path),
        )
        reason = result.get("reason") or ""
        assert "ENF-STRICT-001" in reason, reason
        assert "WRIT_STRICT" in reason, reason

    def test_a_crashed_evaluator_still_allows_by_default(self, tmp_path) -> None:
        """MUST-NOT-REGRESS. Fail-open on outage is the documented default and this
        cycle does not change it; strict is the opt-in that flips it."""
        result = self._pre_write_check(
            "cycle-h-crash", self._BODY, strict=False,
            skill_dir=self._broken_skill_dir(tmp_path),
            socket=self._dead_socket(tmp_path),
        )
        assert result["decision"] == "allow", repr(result)

    def test_a_working_fallback_still_answers_locally_under_strict(self, tmp_path) -> None:
        """MUST-NOT-REGRESS, and the discriminator: with a real skill dir the local gate
        evaluates the write, so strict must NOT short-circuit to ENF-STRICT-001. A fix
        that denied whenever the daemon was down would pass the first test and destroy
        the fallback that is the actual security win."""
        result = self._pre_write_check(
            "cycle-h-crash", self._BODY, strict=True,
            skill_dir=str(SKILL_ROOT),
            socket=self._dead_socket(tmp_path),
        )
        assert "ENF-STRICT-001" not in (result.get("reason") or ""), (
            "the local fallback was skipped and strict answered instead: " + repr(result)
        )

    def test_the_no_session_id_arm_is_unchanged(self, tmp_path) -> None:
        """MUST-NOT-REGRESS: `tests/test_strict_mode.py` pins the two-argument call,
        where no session id is available and no local fallback is possible."""
        env = {**os.environ, "WRIT_PORT": DEAD_PORT, "WRIT_NO_AUTOSTART": "1",
               "WRIT_STRICT": "1", "WRIT_SOCKET": self._dead_socket(tmp_path)}
        script = f'source "{COMMON_SH}"; _writ_session pre-write-check "$1"'
        proc = subprocess.run(
            ["bash", "-c", script, "bash",
             '{"tool_input": {"file_path": "/proj/src/foo.py"}}'],
            capture_output=True, text=True, env=env, timeout=60,
        )
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["decision"] == "deny", repr(result)
        assert "ENF-STRICT-001" in (result.get("reason") or "")


# --------------------------------------------------------------------------- #
# Capability 4 to 9: the write gate honors the plan its approval named
# --------------------------------------------------------------------------- #

class TestTheWriteGateHonorsThePlanItWasApprovedAgainst:

    SID = "cycle-h-plan-drift"

    def _hash(self, project_root: Path) -> str:
        from writ.session.locators import plan_md_hash

        digest = plan_md_hash(str(project_root), self.SID)
        assert digest, "the fixture plan produced no hash"
        return digest

    def _check(self, project_root: Path, cache: dict, path: str) -> dict:
        from writ.session import gates

        return gates._can_write_check(self.SID, _envelope(path), str(SKILL_ROOT), cache)

    def test_a_source_write_proceeds_when_the_hash_matches(self, tmp_path) -> None:
        """MUST-NOT-REGRESS: the ordinary approved path stays open."""
        _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, self._hash(tmp_path))
        result = self._check(tmp_path, cache, str(tmp_path / "src" / "thing.py"))
        assert result["can_write"] is True, result

    def test_a_source_write_is_refused_when_the_plan_changed(self, tmp_path) -> None:
        """THE DEFECT. The advance side already refuses here; the write gate does not."""
        plan = _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, self._hash(tmp_path))
        plan.write_text(PLAN_BODY + "\n- `src/other.py` (create) -- a pivot\n")
        result = self._check(tmp_path, cache, str(tmp_path / "src" / "thing.py"))
        assert result["can_write"] is False, (
            "a source write was permitted against a plan that no longer exists"
        )

    def test_the_refusal_names_plan_drift_and_re_approval(self, tmp_path) -> None:
        """A refusal the user cannot act on is a deadlock. It must say what changed and
        that one re-approval clears it."""
        plan = _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, self._hash(tmp_path))
        plan.write_text(PLAN_BODY + "\nA pivot.\n")
        reason = (self._check(tmp_path, cache,
                              str(tmp_path / "src" / "thing.py"))["reason"] or "").lower()
        assert "plan" in reason, reason
        assert "approv" in reason, reason

    def test_an_unfingerprinted_approval_counts_as_unapproved(self, tmp_path) -> None:
        """Matching `mode_engine.py:136-140`: when we cannot prove what an approval
        covered, ask again. Costs one re-approval for a pre-binding session."""
        _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, None)
        result = self._check(tmp_path, cache, str(tmp_path / "src" / "thing.py"))
        assert result["can_write"] is False, (
            "an approval with no recorded plan hash still authorized a write"
        )

    def test_ticking_a_capability_box_does_not_re_block(self, tmp_path) -> None:
        """What keeps this liveable. `plan_md_hash` normalizes tick state away
        (`locators.py:315-324`), so finishing a cycle the documented way is not a pivot."""
        plan = _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, self._hash(tmp_path))
        plan.write_text(PLAN_BODY.replace("- [ ] the fixture behaves",
                                          "- [x] the fixture behaves"))
        result = self._check(tmp_path, cache, str(tmp_path / "src" / "thing.py"))
        assert result["can_write"] is True, (
            "ticking a capability box was read as a plan pivot: " + repr(result)
        )

    def test_capabilities_md_stays_writable_after_drift(self, tmp_path) -> None:
        """MUST-NOT-REGRESS: capabilities.md is allowed in every mode and phase
        (`gates.py:337`), and drift must not change that."""
        plan = _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, self._hash(tmp_path))
        plan.write_text(PLAN_BODY + "\nA pivot.\n")
        result = self._check(tmp_path, cache, str(tmp_path / "capabilities.md"))
        assert result["can_write"] is True, result

    def test_plan_md_stays_blocked_during_implementation(self, tmp_path) -> None:
        """MUST-NOT-REGRESS, and it corrects this plan's own premise. plan.md is ALREADY
        refused during implementation by ENF-GATE-PLAN (`gates.py:344-350`), so the way
        out of a drift refusal is NOT editing the plan. This cycle leaves that rule
        exactly as it is."""
        plan = _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, self._hash(tmp_path))
        plan.write_text(PLAN_BODY + "\nA pivot.\n")
        result = self._check(tmp_path, cache, str(tmp_path / "plan.md"))
        assert result["can_write"] is False, (
            "plan.md became writable during implementation; that is a separate rule "
            "this cycle must not touch"
        )
        assert "ENF-GATE-PLAN" in (result["reason"] or "")

    def test_no_refusal_advertises_invalidation_as_the_way_out(self, tmp_path) -> None:
        """THE CONTRADICTION THIS CYCLE MUST NOT SHIP. Invalidation deliberately does not
        clear a gate (only a human does), so any refusal telling the user to invalidate
        names an escape that does nothing. The existing ENF-GATE-PLAN message does exactly
        that, and the new drift refusal must not repeat it: both must point at
        re-approval."""
        plan = _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, self._hash(tmp_path))
        plan.write_text(PLAN_BODY + "\nA pivot.\n")
        offenders = []
        for path in (tmp_path / "plan.md", tmp_path / "src" / "thing.py"):
            reason = (self._check(tmp_path, cache, str(path))["reason"] or "").lower()
            if "invalidate" in reason:
                offenders.append((path.name, reason))
        assert not offenders, (
            "these refusals tell the user to invalidate, which no longer clears "
            f"anything: {offenders}"
        )

    def test_re_approving_against_the_new_plan_restores_writes(self, tmp_path) -> None:
        plan = _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, self._hash(tmp_path))
        plan.write_text(PLAN_BODY + "\nA pivot.\n")
        assert self._check(tmp_path, cache,
                           str(tmp_path / "src" / "thing.py"))["can_write"] is False
        fresh = self._hash(tmp_path)
        cache["gates_approved_plan"] = {"phase-a": fresh, "test-skeletons": fresh}
        result = self._check(tmp_path, cache, str(tmp_path / "src" / "thing.py"))
        assert result["can_write"] is True, result

    def test_an_excluded_path_stays_writable_after_drift(self, tmp_path) -> None:
        """MUST-NOT-REGRESS: test paths stay writable before and after approval, which
        is what lets skeletons be written at all."""
        plan = _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, self._hash(tmp_path))
        plan.write_text(PLAN_BODY + "\nA pivot.\n")
        result = self._check(tmp_path, cache, str(tmp_path / "tests" / "test_thing.py"))
        assert result["can_write"] is True, result


# --------------------------------------------------------------------------- #
# Capability 10: one definition of "approved", read the same way twice
# --------------------------------------------------------------------------- #

class TestTheTwoApprovedSetReadersAgree:
    """The defect in one sentence: two readers, two answers, same cache. Whatever the
    fix looks like, these two must not diverge again.
    """

    SID = "cycle-h-parity"

    def _pending(self, cache: dict) -> str | None:
        from writ.session.mode_engine import _next_pending_gate

        return _next_pending_gate(cache, self.SID)

    def _write_allowed(self, cache: dict, project_root: Path) -> bool:
        from writ.session import gates

        return gates._can_write_check(
            self.SID, _envelope(str(project_root / "src" / "thing.py")),
            str(SKILL_ROOT), cache,
        )["can_write"]

    def test_they_agree_when_the_plan_matches(self, tmp_path) -> None:
        from writ.session.locators import plan_md_hash

        _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, plan_md_hash(str(tmp_path), self.SID))
        assert self._pending(cache) is None, "a gate was pending on a matching plan"
        assert self._write_allowed(cache, tmp_path) is True

    def test_they_agree_when_the_plan_drifted(self, tmp_path) -> None:
        """THE DIVERGENCE: advance says a gate is pending, the write gate says yes."""
        from writ.session.locators import plan_md_hash

        plan = _write_plan(tmp_path, self.SID)
        cache = _work_cache(tmp_path, plan_md_hash(str(tmp_path), self.SID))
        plan.write_text(PLAN_BODY + "\nA pivot.\n")
        pending = self._pending(cache)
        allowed = self._write_allowed(cache, tmp_path)
        assert not (pending is not None and allowed), (
            f"advance reports '{pending}' pending while the write gate allows the write"
        )


# --------------------------------------------------------------------------- #
# The ruling, pinned: invalidation RECORDS, it does not revoke
# --------------------------------------------------------------------------- #

class TestInvalidationRecordsRatherThanRevokes:
    """Writ's way, chosen deliberately over Maat's. Only a human grants or clears a
    gate, so a lexical rule-validator records a violation and escalates instead of
    stripping an approval a person gave. These tests exist so the decision is visible
    as behaviour and cannot be reversed by accident.
    """

    SID = "cycle-h-invalidate"

    def _seed(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        from writ.session.cache import _write_cache

        _write_cache(self.SID, _work_cache(tmp_path, "deadbeefcafe"))

    def _invalidate(self, cycles: int = 1) -> None:
        from writ.session.violations import cmd_invalidate_gate

        for _ in range(cycles):
            cmd_invalidate_gate(self.SID, ["phase-a", "--rule", "RULE-X",
                                           "--file", "src/thing.py"])

    def test_the_approval_is_left_standing(self, tmp_path, monkeypatch) -> None:
        self._seed(tmp_path, monkeypatch)
        self._invalidate()
        from writ.session.cache import _read_cache

        assert "phase-a" in _read_cache(self.SID).get("gates_approved", []), (
            "invalidation removed a human's approval; only the human clears a gate"
        )

    def test_the_violation_is_recorded(self, tmp_path, monkeypatch) -> None:
        self._seed(tmp_path, monkeypatch)
        self._invalidate()
        from writ.session.cache import _read_cache

        history = _read_cache(self.SID).get("invalidation_history", {})
        assert history.get("phase-a"), f"nothing was recorded: {history}"

    def test_it_escalates_at_the_threshold(self, tmp_path, monkeypatch) -> None:
        self._seed(tmp_path, monkeypatch)
        from writ.session.violations import MAX_CYCLES_BEFORE_ESCALATION
        from writ.session.cache import _read_cache

        self._invalidate(cycles=MAX_CYCLES_BEFORE_ESCALATION)
        escalation = _read_cache(self.SID).get("escalation") or {}
        assert escalation.get("needed") is True, (
            f"the threshold passed without escalating: {escalation}"
        )
