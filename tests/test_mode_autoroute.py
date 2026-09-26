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
    "rename the OrderService class to OrderManager",
]

# Discussion / questions that must route to NEITHER investigate nor work (no gating).
NON_ROUTING_PROMPTS = [
    "how do I center a div in CSS",
    "let's discuss the overall architecture",
    "what does the checkout function do",
    "add a comment explaining the regex",
    "",
]

# Recall cases taken from the shape of real past prompts: the closed object-noun list in
# _WORK_SIGNALS matched 18 of 808, and each of these was a miss.
WORK_RECALL_PROMPTS = [
    "yes please update readme",
    "data cleanup of orphaned option_ids in magento",
    "remove the deprecated handler",
    "rename the config key",
    "please delete the unused fixtures",
    "Ok, move the helper into bin/lib",
    "can you replace the regex with a parser",
    "bump the version to 1.9.0",
]

# Questions and conversation that must stay unrouted even though some carry a build verb.
QUESTION_PROMPTS = [
    "where is WEB-9059-cosmo-duplicates.csv",
    "what file did you just modify and what exactly did we do?",
    "why do our questions in langfuse not match our eval seed set v2?",
    "approved",
    "what is next?",
    "update me on where the migration stands",
    "let's move on to the next topic",
    "is the rename done?",
    "why did the cleanup of the cache fail?",
    "git push the branch",
    "commit and push",
    # Tracker and pull-request housekeeping: a build verb, but no code changes.
    "this is good please commit and push, update pr message only if needed",
    "update jira ticket",
    "update the ticket status",
    "can you update the pr title",
    "please update the pull request description",
]

# The verbatim regression prompt from session f7fc2b37 (this plan's own root cause): a
# feasibility framing ("id like to see how hard or easy it would be to setup an hook
# system") that _WORK_SIGNALS' "set up ... hook" match routed to work, even though the
# prompt opens "dont push yet," -- an explicit stop, not a build instruction.
REGRESSION_PROMPT = (
    "dont push yet, id like to see how hard or easy it would be to setup an hook "
    "system to improve speed, apperently each hook cost 2 seconds we have over 30 so "
    "we clearly need a scalable system that will fire off once per its appropriate "
    "event and depending on the work that is being done depends on the "
    "\"subhook\"/work that gets done. meaning we might just have a handful of hooks "
    "and the rest is just code"
)

# Feasibility/assessment framings: "how hard/easy/feasible would X be", "I'd like to
# see/know whether", "is it possible to", "what would it take to", "feasibility of".
# Each asks about the DIFFICULTY of a hypothetical build, not for the build itself, so
# none of these may classify as work.
FEASIBILITY_PROMPTS = [
    "how hard would it be to add a cache layer",
    "id like to see how we could build a retry wrapper",
    "is it possible to create a migration for this",
    "what would it take to implement the export endpoint",
    "i'd like to know whether we can refactor the parser",
    "feasibility of adding a queue worker",
]


def _is_non_user_turn(prompt):
    """Import `is_non_user_turn` LAZILY (inside the call, not at module import time).

    `is_non_user_turn` is planned for bin/lib/writ_mode_hint.py and does not exist yet
    on today's code. A module-level `from writ_mode_hint import is_non_user_turn`
    would raise ImportError at COLLECTION time and fail every test in this file, not
    just the ones that exercise it. bin/lib is already on sys.path (writ.session.mode_engine,
    imported above, inserts it before importing classify_mode_hint from the same module),
    so no path manipulation is needed here.
    """
    from writ_mode_hint import is_non_user_turn as fn

    return fn(prompt)


def _is_local_command_echo(text):
    """Import `is_local_command_echo` LAZILY, for the same reason `_is_non_user_turn`
    does above: it is planned for bin/lib/writ_mode_hint.py and does not exist yet on
    today's code, so a module-level import would fail collection of this whole file."""
    from writ_mode_hint import is_local_command_echo as fn

    return fn(text)


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

    @pytest.mark.parametrize("prompt", WORK_RECALL_PROMPTS)
    def test_imperative_build_requests_route_to_work(self, prompt):
        assert classify_mode_hint(prompt) == "work", prompt

    @pytest.mark.parametrize("prompt", QUESTION_PROMPTS)
    def test_questions_and_conversation_do_not_route(self, prompt):
        assert classify_mode_hint(prompt) is None, prompt

    def test_exact_regression_prompt_does_not_route_to_work(self):
        """The verbatim prompt from session f7fc2b37: a feasibility framing that opens
        with an explicit stop ('dont push yet,') must not route to work just because
        its hypothetical clause mentions 'setup an hook system'."""
        assert classify_mode_hint(REGRESSION_PROMPT) is None, REGRESSION_PROMPT

    @pytest.mark.parametrize("prompt", FEASIBILITY_PROMPTS)
    def test_feasibility_framing_does_not_route_to_work(self, prompt):
        """A question about how HARD a build would be is not a request to build it."""
        assert classify_mode_hint(prompt) is None, prompt

    def test_build_instruction_before_a_feasibility_sentence_still_routes_to_work(self):
        """A build instruction outside the framing's own sentence still routes: the
        framing strip is scoped to its sentence, not the whole prompt."""
        assert (
            classify_mode_hint("implement the export endpoint. how hard would a cache be?")
            == "work"
        )

    def test_feasibility_noun_in_a_build_request_still_routes_to_work(self):
        """'feasibility of' is the only accepted framing shape; the bare noun in a build
        request ('the feasibility check endpoint') must not be stripped."""
        assert classify_mode_hint("implement the feasibility check endpoint") == "work"


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


# ===========================================================================
# 1a: non-user turns (sub-agent hand-backs, task notifications) never auto-route.
#
# A hand-back or task-notification prompt fires UserPromptSubmit exactly like a typed
# prompt, so today it is classified as if the user typed it -- flipping a session's
# mode mid-cycle on the strength of a sub-agent's own report. `is_non_user_turn`
# detects the envelope by its opening text; `writ-prompt-parse.py` and the transcript
# fallback both key off it so no auto-route path can act on a non-user turn.
# ===========================================================================

# The exact envelope shapes Claude Code emits for the two non-user turn sources
# (see the plan's capabilities.md): a sub-agent hand-back and a background task
# notification. Both bodies deliberately contain a work-shaped request
# ("implement the export endpoint from the approved plan"), which is the whole
# point of the regression: that text must never reach the classifier as if the
# user had typed it.
HANDBACK_ENVELOPE_PROMPT = (
    'Another Claude session sent a message:\n'
    '<agent-message from="a32b4b449d3a0cffb">\n'
    '[Subagent hand-back] The text below is the final report of a subagent that was '
    'dispatched to implement the export endpoint from the approved plan.\n'
    '</agent-message>'
)
TASK_NOTIFICATION_ENVELOPE_PROMPT = (
    '<task-notification>\n'
    '<task-id>a292afb65f1560d76</task-id>\n'
    '<tool-use-id>tu_deadbeef</tool-use-id>\n'
    '<status>completed</status>\n'
    'implement the export endpoint from the approved plan\n'
    '</task-notification>'
)
SYSTEM_NOTIFICATION_PREAMBLE_PROMPT = (
    '[SYSTEM NOTIFICATION - NOT USER INPUT]\n'
    'A background task you dispatched has completed.\n'
    '<task-notification>\n'
    '<task-id>a292afb65f1560d76</task-id>\n'
    '<status>completed</status>\n'
    'implement the export endpoint from the approved plan\n'
    '</task-notification>'
)
BARE_AGENT_MESSAGE_PROMPT = (
    '<agent-message from="a32b4b449d3a0cffb">\n'
    '[Subagent hand-back] implement the export endpoint from the approved plan\n'
    '</agent-message>'
)


class TestNonUserTurnDetection:
    """`is_non_user_turn` (bin/lib/writ_mode_hint.py) is a pure, anchored-regex
    detector: a prompt that OPENS (after leading whitespace) with one of the known
    non-user envelope markers is a non-user turn; a prompt that merely mentions one
    of those markers partway through is not."""

    def test_handback_with_lead_line_is_non_user(self):
        assert _is_non_user_turn(HANDBACK_ENVELOPE_PROMPT) is True

    def test_task_notification_is_non_user(self):
        assert _is_non_user_turn(TASK_NOTIFICATION_ENVELOPE_PROMPT) is True

    @pytest.mark.parametrize("leading_ws", ["  ", "\n\n", "\t "], ids=["spaces", "newlines", "tab"])
    def test_leading_whitespace_before_a_handback_is_still_non_user(self, leading_ws):
        assert _is_non_user_turn(leading_ws + HANDBACK_ENVELOPE_PROMPT) is True

    @pytest.mark.parametrize("leading_ws", ["  ", "\n\n", "\t "], ids=["spaces", "newlines", "tab"])
    def test_leading_whitespace_before_a_task_notification_is_still_non_user(self, leading_ws):
        assert _is_non_user_turn(leading_ws + TASK_NOTIFICATION_ENVELOPE_PROMPT) is True

    def test_system_notification_preamble_before_task_notification_is_non_user(self):
        assert _is_non_user_turn(SYSTEM_NOTIFICATION_PREAMBLE_PROMPT) is True

    def test_bare_agent_message_opening_with_no_lead_line_is_non_user(self):
        assert _is_non_user_turn(BARE_AGENT_MESSAGE_PROMPT) is True

    def test_none_is_not_a_non_user_turn(self):
        assert _is_non_user_turn(None) is False

    def test_empty_string_is_not_a_non_user_turn(self):
        assert _is_non_user_turn("") is False

    def test_an_ordinary_typed_prompt_is_not_a_non_user_turn(self):
        assert _is_non_user_turn("implement the export endpoint from the approved plan") is False

    def test_a_typed_prompt_that_only_mentions_the_markers_later_is_not_a_non_user_turn(self):
        """A user who pastes or quotes one of these markers mid-prompt is still
        classified normally -- only an OPENING marker means non-user."""
        assert _is_non_user_turn(
            "can you explain what <task-notification> tags look like in our hooks"
        ) is False
        assert _is_non_user_turn(
            "I saw a <agent-message> tag appear in the transcript, what generates that"
        ) is False


LOCAL_COMMAND_STDOUT_TEXT = (
    "<local-command-stdout>implement the export endpoint from the approved plan"
    "</local-command-stdout>"
)
BASH_STDOUT_TEXT = "<bash-stdout>implement the export endpoint from the approved plan</bash-stdout>"
COMMAND_NAME_TEXT = "<command-name>/usage</command-name>\n<command-message>usage</command-message>"
BASH_INPUT_TEXT = "<bash-input>make implement the export endpoint</bash-input>"


class TestLocalCommandEchoDetection:
    """Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, item 3: `is_local_command_echo`
    (bin/lib/writ_mode_hint.py) is a pure, anchored-regex detector for the four
    Claude Code local-command envelope tags. RED at HEAD: the function does not
    exist yet."""

    @pytest.mark.parametrize("text", [
        COMMAND_NAME_TEXT, LOCAL_COMMAND_STDOUT_TEXT, BASH_INPUT_TEXT, BASH_STDOUT_TEXT,
    ], ids=["command-name", "local-command-stdout", "bash-input", "bash-stdout"])
    def test_each_tag_at_the_start_is_a_local_command_echo(self, text):
        assert _is_local_command_echo(text) is True, text

    @pytest.mark.parametrize("leading_ws", ["  ", "\n\n", "\t "], ids=["spaces", "newlines", "tab"])
    def test_leading_whitespace_before_the_tag_is_still_detected(self, leading_ws):
        assert _is_local_command_echo(leading_ws + LOCAL_COMMAND_STDOUT_TEXT) is True

    def test_none_is_not_a_local_command_echo(self):
        assert _is_local_command_echo(None) is False

    def test_empty_string_is_not_a_local_command_echo(self):
        assert _is_local_command_echo("") is False

    def test_a_mid_text_tag_is_not_a_local_command_echo(self):
        assert _is_local_command_echo(
            "I saw a <bash-stdout> tag appear in the transcript, what generates that"
        ) is False

    def test_ordinary_text_is_not_a_local_command_echo(self):
        assert _is_local_command_echo(
            "implement the export endpoint from the approved plan"
        ) is False

    def test_is_non_user_turn_contract_is_unchanged_for_a_command_name_text(self):
        """`is_non_user_turn`'s own contract (sub-agent hand-backs, peer messages,
        task notifications) must not widen to cover this unrelated envelope shape."""
        assert _is_non_user_turn(COMMAND_NAME_TEXT) is False


class TestPromptParseSkipsNonUserTurns:
    """`bin/lib/writ-prompt-parse.py` must emit an empty mode-hint line for a non-user
    envelope, and must not let `permission_mode == 'plan'` or the transcript fallback
    upgrade that empty hint -- while the session_id and prompt lines pass through
    exactly as they do for any other prompt."""

    def _run(self, envelope):
        return subprocess.run(
            [sys.executable, PARSE_PY], input=json.dumps(envelope),
            capture_output=True, text=True, timeout=15,
        )

    @pytest.mark.parametrize(
        "prompt", [HANDBACK_ENVELOPE_PROMPT, TASK_NOTIFICATION_ENVELOPE_PROMPT],
        ids=["handback", "task-notification"],
    )
    def test_non_user_envelope_emits_empty_hint_but_session_and_prompt_pass_through(self, prompt):
        r = self._run({"session_id": "parse-nonuser", "prompt": prompt})
        assert r.returncode == 0, r.stderr
        sid_line, agent_line, hint_line, rest = r.stdout.split("\n", 3)
        assert sid_line == "parse-nonuser"
        assert hint_line == "", (
            f"a hand-back/task-notification prompt must not classify; got hint={hint_line!r}"
        )
        assert rest.rstrip("\n") == prompt, "the raw prompt line must be emitted unchanged"

    @pytest.mark.parametrize(
        "prompt", [HANDBACK_ENVELOPE_PROMPT, TASK_NOTIFICATION_ENVELOPE_PROMPT],
        ids=["handback", "task-notification"],
    )
    def test_non_user_envelope_ignores_the_permission_mode_plan_upgrade(self, prompt):
        r = self._run({
            "session_id": "parse-nonuser-plan", "prompt": prompt, "permission_mode": "plan",
        })
        assert r.returncode == 0, r.stderr
        _, _, hint_line, _ = r.stdout.split("\n", 3)
        assert hint_line == "", (
            "permission_mode == 'plan' must not upgrade a non-user turn's empty hint to 'work'"
        )

    def test_non_user_envelope_does_not_fall_through_to_the_transcript_fallback(self, tmp_path):
        """Even when the transcript tail alone would classify as work, a non-user
        CURRENT turn must not trigger that fallback read at all."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps({
            "type": "user",
            "message": {"content": "implement the export endpoint from the approved plan"},
        }) + "\n")
        r = self._run({
            "session_id": "parse-nonuser-fallback",
            "prompt": BARE_AGENT_MESSAGE_PROMPT,
            "transcript_path": str(transcript),
        })
        assert r.returncode == 0, r.stderr
        _, _, hint_line, _ = r.stdout.split("\n", 3)
        assert hint_line == "", (
            "a non-user turn must not fall through to the transcript fallback even "
            "when the transcript tail alone would classify as 'work'"
        )


class TestTranscriptFallbackFiltersNonUserEntries:
    """The transcript fallback (used when the CURRENT prompt yields no hint of its
    own) must not treat a peer hand-back or task-notification recorded in the
    transcript tail as one of the "recent user messages" it re-classifies against.
    A typed 'ok' carries no hint on its own, so whatever the fallback returns here
    came entirely from the transcript tail."""

    WORK_TEXT = "implement the export endpoint from the approved plan"

    def _hint_for_transcript(self, tmp_path, rows):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        r = subprocess.run(
            [sys.executable, PARSE_PY],
            input=json.dumps({
                "session_id": "parse-transcript-filter",
                "prompt": "ok",
                "transcript_path": str(transcript),
            }),
            capture_output=True, text=True, timeout=15,
        )
        assert r.returncode == 0, r.stderr
        _, _, hint_line, _ = r.stdout.split("\n", 3)
        return hint_line

    @pytest.mark.parametrize("origin_kind", ["peer", "task-notification"])
    def test_is_meta_entry_is_ignored_regardless_of_origin_kind(self, tmp_path, origin_kind):
        rows = [{
            "type": "user", "isMeta": True,
            "message": {"content": self.WORK_TEXT},
            "origin": {"kind": origin_kind},
        }]
        assert self._hint_for_transcript(tmp_path, rows) == "", (
            "isMeta: true entries must not feed the transcript fallback"
        )

    @pytest.mark.parametrize("origin_kind", ["peer", "task-notification"])
    def test_non_human_origin_kind_is_ignored(self, tmp_path, origin_kind):
        rows = [{
            "type": "user",
            "message": {"content": self.WORK_TEXT},
            "origin": {"kind": origin_kind},
        }]
        assert self._hint_for_transcript(tmp_path, rows) == "", (
            f"an origin.kind: {origin_kind!r} entry must not feed the transcript fallback"
        )

    def test_human_origin_kind_still_yields_work(self, tmp_path):
        rows = [{
            "type": "user",
            "message": {"content": self.WORK_TEXT},
            "origin": {"kind": "human"},
        }]
        assert self._hint_for_transcript(tmp_path, rows) == "work", (
            "an origin.kind: 'human' entry must still feed the transcript fallback"
        )

    def test_entry_with_no_origin_field_still_yields_work(self, tmp_path):
        """Older transcripts (written before the origin field existed) behave
        exactly as before: absence of the field is not treated as non-human."""
        rows = [{"type": "user", "message": {"content": self.WORK_TEXT}}]
        assert self._hint_for_transcript(tmp_path, rows) == "work"

    # -----------------------------------------------------------------------
    # Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, item 3: local-command entries
    # (<command-name>, <local-command-stdout>, <bash-input>, <bash-stdout>) must
    # not contribute to the fallback hint either, even when their own text is
    # work-shaped. RED at HEAD: writ-prompt-parse.py's fallback filters only
    # `is_non_user_turn`, so these entries' text still feeds the reclassification.
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize("text", [
        COMMAND_NAME_TEXT, LOCAL_COMMAND_STDOUT_TEXT, BASH_INPUT_TEXT, BASH_STDOUT_TEXT,
    ], ids=["command-name", "local-command-stdout", "bash-input", "bash-stdout"])
    def test_a_local_command_entry_does_not_feed_the_fallback(self, tmp_path, text):
        rows = [{"type": "user", "message": {"content": text}}]
        assert self._hint_for_transcript(tmp_path, rows) == "", (
            f"a local-command envelope must not contribute to the transcript "
            f"fallback hint: {text!r}"
        )

    def test_leading_whitespace_before_the_tag_still_does_not_feed_the_fallback(
        self, tmp_path
    ):
        rows = [{"type": "user", "message": {"content": "  \n" + LOCAL_COMMAND_STDOUT_TEXT}}]
        assert self._hint_for_transcript(tmp_path, rows) == ""

    def test_a_slash_command_entry_still_classifies_as_work(self, tmp_path):
        """A typed slash command is not a local-command echo -- it must keep
        classifying normally through the fallback."""
        rows = [{"type": "user", "message": {"content": "/writ-approve then implement the plan"}}]
        assert self._hint_for_transcript(tmp_path, rows) == "work"

    def test_a_local_command_entry_after_a_real_work_entry_does_not_erase_the_hint(
        self, tmp_path
    ):
        rows = [
            {"type": "user", "message": {"content": self.WORK_TEXT}},
            {"type": "user", "message": {"content": LOCAL_COMMAND_STDOUT_TEXT}},
        ]
        assert self._hint_for_transcript(tmp_path, rows) == "work"


class TestHookIgnoresNonUserTurns:
    """End-to-end guard: the real UserPromptSubmit hook must never auto-route on a
    sub-agent hand-back or task notification, however work-shaped its body reads.
    Mirrors TestHookAutoRouteBehavior's subprocess setup (dead daemon port -> file
    fallback; cwd pinned to a throwaway sandbox, never the real repo)."""

    def _run(self, tmp_path, prompt, seed_mode=None, sid="hook-nonuser-e2e"):
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(tmp_path)
        env["WRIT_PORT"] = "59997"
        env["WRIT_HOST"] = "localhost"
        env["WRIT_FRICTION_LOG"] = str(tmp_path / "friction.log")
        env["WRIT_NO_AUTOSTART"] = "1"
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
        return r, mode, env["WRIT_FRICTION_LOG"]

    def _switch_rows(self, friction_log):
        if not os.path.exists(friction_log):
            return []
        with open(friction_log) as f:
            events = [json.loads(line) for line in f if line.strip()]
        return [
            e for e in events
            if e.get("event") == "mode_change" and e.get("change_type") == "switch"
        ]

    def test_handback_with_a_work_request_does_not_reroute_an_investigate_session(self, tmp_path):
        r, mode, friction_log = self._run(
            tmp_path, HANDBACK_ENVELOPE_PROMPT, seed_mode="investigate"
        )
        assert r.returncode == 0, r.stderr
        assert mode == "investigate", (
            f"a hand-back must never auto-route the session; got mode={mode!r}"
        )
        assert "mode set automatically" not in r.stdout
        assert "restored automatically" not in r.stdout
        assert self._switch_rows(friction_log) == [], (
            "a hand-back must never write a switch mode_change row"
        )

    def test_task_notification_with_a_work_request_leaves_an_unset_session_unset(self, tmp_path):
        r, mode, friction_log = self._run(
            tmp_path, TASK_NOTIFICATION_ENVELOPE_PROMPT, seed_mode=None
        )
        assert r.returncode == 0, r.stderr
        assert mode == "", f"an unset session must stay unset; got mode={mode!r}"
        assert "mode set automatically" not in r.stdout
        assert "restored automatically" not in r.stdout
        assert self._switch_rows(friction_log) == []
