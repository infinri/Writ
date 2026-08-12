"""Mode auto-routing: an audit/explore/research-shaped prompt should route to investigate.

The original A.S.E. failure was a security-audit request handled in `conversation` mode,
with no audit ever starting. Nothing classified the prompt, and the mode directive didn't
even offer `investigate`. This adds a pure `classify_mode_hint(prompt)` (precision-biased:
investigate or None) that the UserPromptSubmit hook calls when no mode is set, auto-setting
investigate (gate-light) and announcing it.

Per TEST-REGRESSION-001: the classifier cases drive the function; the hook structural test
guards the wiring.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from writ.session.mode_engine import classify_mode_hint

HOOK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "hooks", "scripts", "writ-rag-inject.sh")
)
HELPER = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
)
# The prompt parser + mode classifier was extracted from the hook into this standalone
# bin/lib file (Wave 2 rag-inject split); classify_mode_hint now lives there, not inline.
PARSE_PY = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-prompt-parse.py")
)

# The user's actual A.S.E. prompt (excerpt) -- long, mixes "build" with audit/security
# signals. It must route to investigate on the strength of "security process" + "CVE",
# NOT be excluded just because it contains the word "build".
ASE_PROMPT = (
    "Our CTO is asking about our security process, we have sansec running that sends us "
    "emails but that is when the offender is inside. We want a tool that will notify us of "
    "any CVE or high risk issues only when they are relevant. What would be the best thing "
    "to build that will give the best security protection?"
)

INVESTIGATE_PROMPTS = [
    "audit our composer dependencies for known CVEs",
    "audit the codebase for security issues",
    "do a security review of the auth module",
    "what is our security posture right now",
    "explore the codebase and find where input validation happens",
    "investigate how the session cache is keyed",
    "research the current best practice for rate limiting",
    "assess the security of our login flow",
    "run a threat model on the checkout flow",
    ASE_PROMPT,
]

# Build/implementation prompts that route to WORK (the full gated workflow). Auto-routing
# work was added deliberately because agent self-classification proved unreliable. Note the
# audit-as-noun cases ("audit log feature", "audit trail") are BUILD tasks -> work, NOT
# investigate (investigate is checked first and matches audit-as-verb only).
# v1 is keyword-based (precision over recall): it catches the common implementation
# phrasings; edge phrasings (e.g. "add an audit trail to the users table", where the code
# object sits many words from the verb) fall back to the mode directive + manual set. The
# reliability upgrade is transcript/permission_mode classification (v2, from the black box).
WORK_PROMPTS = [
    "build an audit log feature for the orders module",
    "implement the export endpoint from the approved plan",
    "fix the failing test in test_orders.py",
    "refactor the payment module to use composition",
    "create a migration for the orders table",
    "write unit tests for the parser",
]

# Discussion / questions that must route to NEITHER investigate nor work (no gating).
NON_ROUTING_PROMPTS = [
    "how do I center a div in CSS",
    "let's discuss the overall architecture",
    "rename the OrderService class to OrderManager",
    "what does the checkout function do",
    "add a comment explaining the regex",
    "",
]


class TestClassifyModeHint:
    @pytest.mark.parametrize("prompt", INVESTIGATE_PROMPTS)
    def test_audit_explore_research_routes_to_investigate(self, prompt):
        assert classify_mode_hint(prompt) == "investigate", prompt

    @pytest.mark.parametrize("prompt", WORK_PROMPTS)
    def test_build_routes_to_work(self, prompt):
        assert classify_mode_hint(prompt) == "work", prompt

    @pytest.mark.parametrize("prompt", NON_ROUTING_PROMPTS)
    def test_chat_and_questions_do_not_route(self, prompt):
        assert classify_mode_hint(prompt) is None, prompt

    def test_none_input_is_safe(self):
        assert classify_mode_hint(None) is None  # type: ignore[arg-type]

    def test_audit_as_noun_is_work_not_investigate(self):
        """'audit log' / 'audit trail' are build NOUNS: 'create an audit log table' is a build
        task -> work (never investigate). audit-as-VERB still routes to investigate."""
        assert classify_mode_hint("create an audit log table") == "work"
        assert classify_mode_hint("the audit trail should record changes") is None
        assert classify_mode_hint("audit the codebase for issues") == "investigate"


class TestHookAutoRouteWiring:
    """Structural guard: the UserPromptSubmit hook must call the classifier when no mode
    is set, auto-set investigate, and offer investigate in the mode directive."""

    HOOK = HOOK

    def _body(self):
        with open(self.HOOK) as f:
            return f.read()

    def test_hook_imports_classifier(self):
        # classify_mode_hint now lives in the extracted bin/lib/writ-prompt-parse.py; the
        # hook auto-routes by INVOKING that file. Dual-file form (cf. run-analysis split):
        # the classifier string must be in the extracted file, and the hook must invoke it.
        with open(PARSE_PY) as f:
            parse_body = f.read()
        assert "classify_mode_hint" in parse_body, (
            "writ-prompt-parse.py must call classify_mode_hint to auto-route audit prompts"
        )
        assert "writ-prompt-parse.py" in self._body(), (
            "hook must invoke writ-prompt-parse.py (which classifies the prompt)"
        )

    def test_hook_auto_sets_hinted_mode(self):
        body = self._body()
        # The hook auto-routes whatever classify_mode_hint returned (investigate OR
        # work) via $MODE_HINT, using `mode init` (set-only-if-unset) so a re-fire
        # never resets a live cycle (gate-reset bug, 2026-06-29).
        assert re.search(r'mode\s+init\s+"\$MODE_HINT"', body), (
            "hook must auto-route the classified mode via `mode init` ($MODE_HINT)"
        )

    def test_hook_reroutes_mid_session_via_switch(self):
        """The mid-session path must use `mode switch`, never `mode set`.

        `mode set` runs _apply_mode_set, which clears gates_approved and
        paused_work_state; routing a misclassified prompt through it would destroy an
        approved plan and approved tests. `mode switch` saves them instead, so a false
        positive costs a detour rather than the approvals.
        """
        body = self._body()
        assert re.search(r'mode\s+switch\s+"\$MODE_HINT"', body), (
            "hook must re-route a live session via `mode switch` ($MODE_HINT)"
        )
        assert not re.search(r'mode\s+set\s+"\$MODE_HINT"', body), (
            "auto-route must never call `mode set`: it wipes approved gates"
        )

    def test_directive_offers_investigate(self):
        body = self._body()
        # The 'set mode' directive must list investigate as an option.
        assert "investigate" in body and "set <conversation" in body
        assert re.search(r"set <conversation\|debug\|review\|work\|investigate>", body), (
            "the mode directive must offer investigate alongside the other modes"
        )


class TestHookAutoRouteBehavior:
    """End-to-end: run the UserPromptSubmit hook (system python, dead daemon port -> file
    fallback) and assert it actually sets the mode. The hook keys off the RAW prompt."""

    def _run(self, tmp_path, prompt, seed_mode=None, sid="autoroute-e2e"):
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(tmp_path)
        env["WRIT_PORT"] = "59997"  # dead port -> curl fails fast -> file-direct fallback
        env["WRIT_HOST"] = "localhost"
        env["WRIT_FRICTION_LOG"] = str(tmp_path / "friction.log")
        env["WRIT_NO_AUTOSTART"] = "1"  # do not let the hook spawn a daemon on the dead port
        # cwd must stay inside tmp_path: `mode set` stamps cache["project_root"] from the
        # process cwd, and clearing gate state deletes <project_root>/.claude/gates/
        # *.approved, so inheriting pytest's cwd deleted the REAL repo's approval files.
        sandbox = tmp_path / "sandbox"
        (sandbox / ".claude" / "gates").mkdir(parents=True, exist_ok=True)
        (sandbox / ".git").mkdir(exist_ok=True)
        if seed_mode:
            subprocess.run(
                [sys.executable, HELPER, "mode", "set", seed_mode, sid],
                env=env, check=True, capture_output=True, text=True, cwd=str(sandbox),
            )
        r = subprocess.run(
            ["bash", HOOK], input=json.dumps({"session_id": sid, "prompt": prompt}),
            capture_output=True, text=True, env=env, timeout=30, cwd=str(sandbox),
        )
        mode = subprocess.run(
            [sys.executable, HELPER, "mode", "get", sid],
            env=env, capture_output=True, text=True,
        ).stdout.strip()
        return r, mode

    def test_audit_prompt_unset_autoroutes_investigate(self, tmp_path):
        r, mode = self._run(tmp_path, ASE_PROMPT)
        assert r.returncode == 0, r.stderr
        assert mode == "investigate", f"expected investigate, got {mode!r}"
        assert "investigate mode set automatically" in r.stdout

    def test_build_prompt_autoroutes_work(self, tmp_path):
        r, mode = self._run(tmp_path, "implement the export endpoint from the approved plan")
        assert r.returncode == 0, r.stderr
        assert mode == "work", f"a build task must auto-route to work, got {mode!r}"
        assert "investigate mode set automatically" not in r.stdout
        assert "work mode set automatically" in r.stdout

    def test_midsession_reroute_preserves_work_state(self, tmp_path):
        """Contract change (mode-switch cycle): the auto-route MAY re-route a live
        session between work and investigate, because the original once-per-session
        behavior meant a mid-work discovery could never start an investigation. What
        it must never do is destroy work state, so the property asserted here moved
        from "the mode cannot change" to "the approved gates survive the change".

        `mode switch` (not `mode set`) is what makes that true: it saves phase and
        gates into paused_work_state. Depth cases live in
        tests/test_mode_switch_midsession.py.
        """
        sid = "autoroute-e2e"
        r, mode = self._run(tmp_path, ASE_PROMPT, seed_mode="work", sid=sid)
        assert r.returncode == 0, r.stderr
        assert mode == "investigate", (
            "a mid-work investigate-shaped prompt must now re-route, not be ignored"
        )
        with open(os.path.join(str(tmp_path), f"writ-session-{sid}.json")) as f:
            cache = json.load(f)
        assert cache["paused_work_state"] is not None, (
            "re-routing out of work must save the work state, never discard it"
        )

    def test_specialist_mode_not_overridden(self, tmp_path):
        """The half of the old contract that survives: only work and investigate are
        auto-routed between. An explicitly chosen debug session stays debug, because
        flipping it to work on a guess would fire the debug-to-work root-cause handoff
        as a side effect."""
        r, mode = self._run(tmp_path, ASE_PROMPT, seed_mode="debug")
        assert r.returncode == 0, r.stderr
        assert mode == "debug", "auto-route must not touch an explicit specialist mode"


# ===========================================================================
# Part 6: mode_source provenance ("explicit" vs "auto")
#
# from_mode is null on BOTH the explicit and the automatic first-set path
# (mode_engine.py:371 guards that the auto path's old_mode is always None), so
# nothing before this could tell a human's `mode set` apart from the
# classifier's `mode init`. mode_source is the only field that can, and the
# mode_init no-op guarantee (the thing that keeps a re-firing classifier from
# ever wiping a live gate cycle) must survive its addition unweakened.
# ===========================================================================

class TestModeInitNeverResetsAnExistingMode:
    """Regression guard on mode_init's core guarantee (mode_engine.py:335-351):
    a session with ANY mode already recorded -- however it got there -- is left
    untouched by a later mode_init call. mode_source must not weaken this: an
    auto-routed session is exactly as protected as an explicit one, and an
    unset session is exactly as routable as it always was.
    """

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        sandbox = tmp_path / "cwd-sandbox"
        (sandbox / ".claude" / "gates").mkdir(parents=True, exist_ok=True)
        (sandbox / ".git").mkdir(exist_ok=True)
        monkeypatch.chdir(sandbox)

    def test_mode_init_is_a_noop_on_an_explicitly_set_session(self):
        from writ.session import mode_engine
        from writ.session.cache import _read_cache

        sid = "regression-explicit-noop"
        mode_engine._mode_set(sid, "work")
        mode_engine._mode_init(sid, "investigate")

        cache = _read_cache(sid)
        assert cache["mode"] == "work", "mode_init must never override an explicit mode"
        assert cache["mode_source"] == "explicit"

    def test_mode_init_is_a_noop_on_an_already_auto_routed_session(self):
        from writ.session import mode_engine
        from writ.session.cache import _read_cache

        sid = "regression-auto-noop"
        mode_engine._mode_init(sid, "investigate")
        mode_engine._mode_init(sid, "work")

        cache = _read_cache(sid)
        assert cache["mode"] == "investigate", (
            "mode_init must never override an already-routed mode, even with a "
            "second call to itself"
        )
        assert cache["mode_source"] == "auto"

    def test_mode_init_still_routes_a_session_with_no_mode_at_all(self):
        """The other half of the guard: an unset session is exactly as routable
        as it always was."""
        from writ.session import mode_engine
        from writ.session.cache import _read_cache

        sid = "regression-unset-still-routes"
        mode_engine._mode_init(sid, "work")

        cache = _read_cache(sid)
        assert cache["mode"] == "work"
        assert cache["mode_source"] == "auto"


class TestHookAutoRouteStampsModeSource:
    """The hook's auto-route call (`mode init`) must stamp mode_source == "auto"
    through the real subprocess path, not just when mode_engine is called
    in-process -- the hook is a thin wrapper around it, not a second contract.
    """

    def _run(self, tmp_path, prompt, sid="autoroute-source-e2e"):
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(tmp_path)
        env["WRIT_PORT"] = "59997"
        env["WRIT_HOST"] = "localhost"
        env["WRIT_FRICTION_LOG"] = str(tmp_path / "friction.log")
        env["WRIT_NO_AUTOSTART"] = "1"
        sandbox = tmp_path / "sandbox"
        (sandbox / ".claude" / "gates").mkdir(parents=True, exist_ok=True)
        (sandbox / ".git").mkdir(exist_ok=True)
        r = subprocess.run(
            ["bash", HOOK], input=json.dumps({"session_id": sid, "prompt": prompt}),
            capture_output=True, text=True, env=env, timeout=30, cwd=str(sandbox),
        )
        with open(os.path.join(str(tmp_path), f"writ-session-{sid}.json")) as f:
            cache = json.load(f)
        return r, cache

    def test_first_autoroute_stamps_auto_source(self, tmp_path):
        r, cache = self._run(tmp_path, ASE_PROMPT)
        assert r.returncode == 0, r.stderr
        assert cache["mode"] == "investigate"
        assert cache["mode_source"] == "auto", (
            "the hook's own `mode init` call must stamp the same provenance as "
            "calling mode_engine._mode_init directly"
        )
