"""tests/test_approval_tiers.py

Cycle 1 (plan.md "The design"): a three-way `classify()` (exact / embedded / none)
replaces the bare `is_approval()` boolean the hook used to gate advancing on. Today a
genuine approval buried in a longer sentence -- "ok remember we want to fix all our
findings, approved" -- gets silence, costing a turn; the embedded tier turns that into a
question instead, WITHOUT widening what can advance a gate (only `exact` mints/advances).

RED today: `classify` does not exist on bin/lib/approval_match.py, so every
TestClassify* test below raises ImportError at call time -- `_tier()` imports it
LOCALLY, not at module scope, so that ImportError is scoped to each calling test and
does not block collection of TestIsApprovalUnchanged (a regression guard) or the
hook-integration classes (which never call classify() directly).

TestIsApprovalUnchanged is a REGRESSION guard, not a RED test for this cycle: it imports
only is_approval and passes against the code on disk today. It exists to catch a
regression the classify() work might introduce (capability 5's second half: "is_approval
returns False for every case it returns False for today"), not to prove classify()
exists.

Per TEST-TDD-001 / SKL-PROC-WRIT-FAILURE-001: skeletons approved before implementation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin", "lib"))

from approval_match import is_approval  # noqa: E402

# autouse (see the fixture's own docstring): a hook-spawning test here reads mode via a
# `mode get` subprocess fallback, which never calls `mode set`/`mode init` itself and so
# never clears .claude/gates/*.approved -- but sandbox_cwd is imported anyway per the
# blanket safety rule (any subprocess run without an explicit cwd= inherits pytest's
# process cwd, and this fixture is the one guaranteed-safe way to pin that).
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
HOOK_PATH = os.path.join(SKILL_ROOT, "hooks", "scripts", "auto-approve-gate.sh")


def _tier(prompt: str) -> str:
    """Mirror the hook's lower+strip normalization before classify() sees the prompt.

    Imports classify() LOCALLY (not at module scope) so its absence today raises
    ImportError inside each calling test, not at collection time -- mirrors
    tests/test_advance_phase_token_claim.py's TestClaimGateTokenPrimitive convention.
    A module-scope import would fail collection for the WHOLE file, including
    TestIsApprovalUnchanged (a regression guard that must keep running today) and the
    hook-integration classes below (which never call classify() directly).
    """
    from approval_match import classify

    return classify(prompt.lower().strip())


# ---------------------------------------------------------------------------
# Capability 1 (classification half): exact tier == is_approval's accept set
# ---------------------------------------------------------------------------


class TestClassifyExactTier:
    """classify() must agree with is_approval on every prompt is_approval already
    accepts. The mint+advance mechanics for the exact tier are exercised end to end by
    tests/test_pol6f_approval_workflow_extraction.py and
    tests/test_advance_phase_token_claim.py; this pins only the classification decision
    that has to precede them, so introducing classify() cannot narrow the exact set.
    """

    @pytest.mark.parametrize("prompt", [
        "approved", "approve", "lgtm", "proceed", "go ahead", "yes", "continue",
        "approved!", "approved.", "ok proceed with remaining work", "sure, go ahead",
        "yeah approved, continue with implementation", "approved and push",
        "approved, ship it", "approved then commit", "approve and merge",
    ])
    def test_is_approval_true_prompts_classify_as_exact(self, prompt):
        assert is_approval(prompt) is True, f"fixture drift: {prompt!r} is no longer exact"
        assert _tier(prompt) == "exact"


# ---------------------------------------------------------------------------
# Capability 2: the exact prompt that failed on 2026-08-10
# ---------------------------------------------------------------------------


class TestClassifyEmbeddedTierTheMissedPrompt:
    def test_the_missed_prompt_classifies_as_embedded(self):
        prompt = "ok remember we want to fix all our findings, approved"
        assert is_approval(prompt) is False, "fixture drift: this must NOT be an exact match"
        assert _tier(prompt) == "embedded"


class TestClassifyEmbeddedTierNarrowVocabulary:
    """The embedded set is deliberately narrow: approved/approve/lgtm/ship it/go ahead
    as a standalone word or phrase, in a prompt under 200 chars, that is not itself an
    exact match. It excludes the exact tier's weaker words (ok/good/go/yes/continue) --
    see TestClassifyEmbeddedTierExcludesWeakVocabulary below."""

    @pytest.mark.parametrize("prompt", [
        "so i think that is approved, one more thing though",
        "ship it once the tests pass",
        "lgtm just double check the migration",
        "i approve of this design, nice work",
    ])
    def test_prompt_with_a_strong_approval_word_classifies_as_embedded(self, prompt):
        assert _tier(prompt) == "embedded"


# ---------------------------------------------------------------------------
# Capability 3: the old substring-scan misses classify as none
# ---------------------------------------------------------------------------


class TestClassifyNoneTierOldSubstringMisses:
    """These two prompts only ever tripped auto-approve-gate.sh's now-deleted
    LOOKS_LIKE_APPROVAL scan (a bare substring test for 'go'/'ok'/'good'/etc, which
    fired on the 'ok' opening "ok how about" and had no authority to advance anything).
    classify() must not resurrect that false-positive surface under a new tier name.
    """

    @pytest.mark.parametrize("prompt", [
        "ok how about for writ, should we tag stable releases?",
        "how does the tax know when to show up",
    ])
    def test_old_substring_scan_misses_classify_as_none(self, prompt):
        assert _tier(prompt) == "none"


# ---------------------------------------------------------------------------
# Capability 4: weak vocabulary excluded from the embedded tier
# ---------------------------------------------------------------------------


class TestClassifyEmbeddedTierExcludesWeakVocabulary:
    def test_ok_good_work_now_do_x_is_none(self):
        """The exact tier's vocabulary (ok/good) admitted as embedded would make an
        ordinary instruction ask for a gate confirmation -- the plan's stated reason
        the embedded set is narrow."""
        assert _tier("ok good work, now do X") == "none"

    @pytest.mark.parametrize("prompt", [
        "ok let's continue with the migration",
        "yes let's go with option two",
    ])
    def test_other_weak_vocabulary_prompts_are_none(self, prompt):
        assert _tier(prompt) == "none"


# ---------------------------------------------------------------------------
# Capability 5: negation / question / interrogative-lead guards
# ---------------------------------------------------------------------------


class TestClassifyGuards:
    """The three named guard cases from capabilities.md, verbatim."""

    @pytest.mark.parametrize("prompt", [
        "is this approved",
        "not approved",
        "how do i get this approved",
    ])
    def test_the_named_guard_cases_stay_none(self, prompt):
        assert _tier(prompt) == "none"

    @pytest.mark.parametrize("prompt", [
        "does this look approved to you",
        "should i mark this as approved",
        "can we call this approved",
        "what if this gets approved",
        "why is this marked approved",
        "did you say this was approved",
    ])
    def test_interrogative_lead_stays_none(self, prompt):
        assert _tier(prompt) == "none"

    def test_a_trailing_question_mark_stays_none(self):
        assert _tier("this is fully approved right?") == "none"

    def test_a_negated_approval_word_stays_none(self):
        assert _tier("this change is unapproved") == "none"


class TestIsApprovalUnchanged:
    """Capability 5's regression half: is_approval() must return False for every
    prompt it returns False for today. This class imports only is_approval and passes
    against the CURRENT code -- it is not RED for this cycle. It exists so a future
    change to classify() that accidentally touches is_approval's own regexes (they
    share the module) is caught here rather than discovered as a widened exact tier.
    """

    @pytest.mark.parametrize("prompt", [
        "how do I get this approved?",
        "the proceed function needs to handle errors",
        "add a continue statement in the loop",
        "",
        "refactor the database module",
        "is it ok to delete the old migration files?",
        "where does this function go in the architecture?",
        "approve the design before merging",
        "approved changes need review",
        "is this approved?",
        "not approved",
        "ok remember we want to fix all our findings, approved",
        "ok how about for writ, should we tag stable releases?",
        "how does the tax know when to show up",
        "ok good work, now do X",
    ])
    def test_is_approval_still_false(self, prompt):
        assert is_approval(prompt.lower().strip()) is False


# ---------------------------------------------------------------------------
# Hook structure: dispatch on classify(), and the deleted substring heuristic
# ---------------------------------------------------------------------------


class TestHookBranchesOnTier:
    """auto-approve-gate.sh must dispatch on classify()'s three-way result, not on a
    reinstated boolean, and the LOOKS_LIKE_APPROVAL substring scan -- the mechanism
    that produced today's approval_pattern_miss false-positive telemetry -- must be
    gone along with its second `python3 -c` interpreter start (plan.md "Interpreter
    budget": worst case is one FEWER python start per prompt than today).
    """

    @staticmethod
    def _hook_source() -> str:
        with open(HOOK_PATH) as f:
            return f.read()

    def test_hook_calls_classify(self):
        assert "classify" in self._hook_source(), (
            "the hook must call approval_match.classify() to obtain the tier; "
            "calling only is_approval() cannot distinguish embedded from none"
        )

    def test_looks_like_approval_heuristic_is_deleted(self):
        src = self._hook_source()
        assert "LOOKS_LIKE_APPROVAL" not in src, (
            "the substring heuristic (and its second interpreter start) must be "
            "removed, not kept running alongside classify()"
        )


# ---------------------------------------------------------------------------
# Hook integration: capability 1 (exact still mints, now a BOUND token) and
# capability 2 (the missed prompt, driven through the real hook)
# ---------------------------------------------------------------------------


def _envelope(session_id: str, prompt: str) -> str:
    return json.dumps({"session_id": session_id, "prompt": prompt})


def _seed_session(cache_dir: Path, session_id: str, **fields) -> Path:
    cache_file = cache_dir / f"writ-session-{session_id}.json"
    cache_file.write_text(json.dumps(fields))
    return cache_file


def _run_hook(envelope: str, cache_dir: str) -> subprocess.CompletedProcess:
    """Unreachable port so the hook fails open without a live daemon; cache dir
    pinned. Mirrors tests/test_session_cache_dir_parity.py's _run_hook: NO cwd=
    override -- the sandbox_cwd fixture already chdir'd the test process, and this
    subprocess inherits that, which is what keeps a work-mode auto-route (if one ever
    fired here) from touching this repo's own .claude/gates/. Argument LIST, not a
    shell string (SEC-INJ-CMD-001): nothing here is interpolated into a shell command.
    """
    env = {**os.environ, "WRIT_CACHE_DIR": cache_dir, "WRIT_PORT": "19999", "WRIT_HOST": "localhost"}
    return subprocess.run(
        ["bash", HOOK_PATH], input=envelope, capture_output=True, text=True, env=env, timeout=25,
    )


class TestExactTierStillMintsABoundToken:
    """Capability 1: introducing the tier split (and, in the same cycle, the token
    binding) must not stop an exact approval from minting -- it must mint the NEW
    three-line bound format, not today's bare one-line secrets.token_hex(16). If the
    hook still minted the old format after this cycle, capability 6's byte-parity
    contract would be meaningless in production even though the unit tests for it
    passed.
    """

    def test_an_exact_approval_mints_a_token_bound_to_the_pending_gate(self, tmp_path):
        from writ.session.gate_token import gate_token_path

        sid = f"tier-exact-{uuid.uuid4().hex[:8]}"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _seed_session(cache_dir, sid, mode="work", current_phase="planning", gates_approved=[])

        token_path = gate_token_path(sid)
        try:
            r = _run_hook(_envelope(sid, "approved"), str(cache_dir))
            assert os.path.exists(token_path), f"no token file minted; hook stdout:\n{r.stdout}"
            with open(token_path) as f:
                lines = f.read().split("\n")
            assert len(lines) >= 3 and lines[1] == "phase-a", (
                "an exact approval on the planning gate must mint a token bound to "
                f"phase-a on line 2; token file lines: {lines!r}"
            )
        finally:
            try:
                os.remove(token_path)
            except OSError:
                pass


class TestEmbeddedTierHookIntegration:
    """Capability 2, end to end through the real hook: the exact prompt that failed
    on 2026-08-10 must classify as embedded, which means no mint and no advance, only
    a directive naming the pending gate.

    test_no_token_is_minted and test_no_gate_advances also pass against TODAY's code
    (the old LOOKS_LIKE_APPROVAL scan logs a miss and exits before minting anything
    either), so they are not RED on their own; they are pinned here as regression
    guards alongside test_the_directive_names_the_pending_gate, which IS RED today
    (today's exit prints nothing -- no directive is emitted at all for this prompt).
    """

    MISSED_PROMPT = "ok remember we want to fix all our findings, approved"

    def test_no_token_is_minted(self, tmp_path):
        from writ.session.gate_token import gate_token_path

        sid = f"tier-embed-notoken-{uuid.uuid4().hex[:8]}"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _seed_session(cache_dir, sid, mode="work", current_phase="planning", gates_approved=[])
        token_path = gate_token_path(sid)
        try:
            _run_hook(_envelope(sid, self.MISSED_PROMPT), str(cache_dir))
            assert not os.path.exists(token_path), "the embedded tier must never mint a gate token"
        finally:
            try:
                os.remove(token_path)
            except OSError:
                pass

    def test_no_gate_advances(self, tmp_path):
        sid = f"tier-embed-noadvance-{uuid.uuid4().hex[:8]}"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cache_file = _seed_session(cache_dir, sid, mode="work", current_phase="planning", gates_approved=[])
        _run_hook(_envelope(sid, self.MISSED_PROMPT), str(cache_dir))
        assert json.loads(cache_file.read_text()).get("gates_approved") == []

    def test_the_directive_names_the_pending_gate(self, tmp_path):
        sid = f"tier-embed-directive-{uuid.uuid4().hex[:8]}"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _seed_session(cache_dir, sid, mode="work", current_phase="planning", gates_approved=[])
        r = _run_hook(_envelope(sid, self.MISSED_PROMPT), str(cache_dir))
        assert "phase-a" in r.stdout, (
            "the embedded-tier directive must name the pending gate (phase-a); "
            f"stdout:\n{r.stdout!r}"
        )
