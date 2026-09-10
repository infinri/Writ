"""#8: /prompt-bundle moves the three per-prompt channels into the warm daemon.

The bash rag-inject hook used to retrieve + parse + render broad /query, /always-on,
and /methodology-companion with ~28 cold python3 spawns per turn (measured ~646ms).
/prompt-bundle awaits the existing handlers in-process and renders via the pure helpers
here, so the hook drops to one curl + jq extracts (measured ~274ms; python3 28 -> 8).

These tests pin the pure render/parse helpers (deterministic) + the endpoint shape +
the hook wiring. Byte-for-byte output equivalence with the legacy hook was verified by
golden-diff across all five modes during development (the regression oracle).
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

import writ.server as server
import writ.server.routes.query as qroute
from writ.server.models import PromptBundleRequest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

# ROUTE fixtures (Decision 4): no daemon, no socket. Imported explicitly per this
# repo's convention (tests/fixtures/server_routes.py's own module docstring) --
# never registered in a root conftest.
from tests.fixtures.server_routes import isolated_cache, route_db, route_pipeline

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
HOOK_SH = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-rag-inject.sh")


def _imp(name):
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module(name)


# --------------------------------------------------------------------------- #
# 1. render_always_on -- mirror of the ALWAYS_ON_PARSED heredoc
# --------------------------------------------------------------------------- #
class TestRenderAlwaysOn:
    def test_renders_block_tokens_count(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        ao = {"total_tokens": 42, "rules": [
            {"rule_id": "R1", "trigger": "when x", "statement": "do y"},
            {"rule_id": "R2", "trigger": "when a", "statement": "do b"},
        ]}
        block, tokens, count = pb.render_always_on(ao)
        assert tokens == 42 and count == 2
        assert block.startswith("=== ALWAYS-ACTIVE RULES ===")
        assert block.endswith("=== END ALWAYS-ACTIVE RULES ===")
        assert "[R1] WHEN: when x" in block and "  do y" in block

    def test_empty_rules_empty_block(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        block, tokens, count = pb.render_always_on({"rules": [], "total_tokens": 0})
        assert block == "" and count == 0

    def test_skips_incomplete_rules(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        ao = {"rules": [{"rule_id": "R1", "trigger": "", "statement": "y"},  # missing trigger
                        {"rule_id": "R2", "trigger": "t", "statement": "s"}]}
        block, _, count = pb.render_always_on(ao)
        assert count == 2          # count is len(rules), as in the bash version
        assert "[R1]" not in block and "[R2] WHEN: t" in block

    def test_bad_total_tokens_defaults_zero(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        _, tokens, _ = pb.render_always_on({"rules": [], "total_tokens": "nope"})
        assert tokens == 0


# --------------------------------------------------------------------------- #
# 1b. always_on_rule_ids -- what the session must record as injected
#
# The plan gate validates cited rule IDs against what the session recorded as loaded.
# The always-on channel injected rules without recording them, so the gate called its
# own injected rules hallucinated. Recording needs the ID list, and it must be exactly
# the rules that reached the block: recording a filtered-out rule would tell the agent
# it may cite something it never saw.
# --------------------------------------------------------------------------- #
class TestAlwaysOnRuleIds:
    def test_returns_the_rendered_rule_ids_in_bundle_order(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        ao = {"total_tokens": 42, "rules": [
            {"rule_id": "R1", "trigger": "when x", "statement": "do y"},
            {"rule_id": "R2", "trigger": "when a", "statement": "do b"},
        ]}
        assert pb.always_on_rule_ids(ao) == ["R1", "R2"]

    def test_no_rules_means_no_ids(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        assert pb.always_on_rule_ids({"rules": [], "total_tokens": 0}) == []

    def test_it_agrees_with_what_render_put_in_the_block(self):
        """The invariant that keeps the two from drifting: same filter, one definition."""
        pb = _imp("writ.retrieval.prompt_bundle")
        ao = {"rules": [
            {"rule_id": "R1", "trigger": "", "statement": "y"},        # no trigger
            {"rule_id": "R2", "trigger": "t", "statement": "s"},       # renders
            {"rule_id": "", "trigger": "t", "statement": "s"},         # no id
            {"rule_id": "R4", "trigger": "t", "statement": ""},        # no statement
        ]}
        block, _, _ = pb.render_always_on(ao)
        ids = pb.always_on_rule_ids(ao)
        assert ids == ["R2"]
        for rid in ids:
            assert f"[{rid}]" in block
        for skipped in ("R1", "R4"):
            assert f"[{skipped}]" not in block and skipped not in ids

    def test_missing_rules_key_is_tolerated(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        assert pb.always_on_rule_ids({}) == []


# --------------------------------------------------------------------------- #
# 2. compute_nudge
# --------------------------------------------------------------------------- #
class TestComputeNudge:
    def test_no_rules(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        assert pb.compute_nudge({"rules": []}) == "NO_RULES"

    def test_all_low_scores(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        assert pb.compute_nudge({"rules": [{"score": 0.1}, {"score": 0.29}]}) == "LOW_SCORES"

    def test_some_high_score(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        assert pb.compute_nudge({"rules": [{"score": 0.1}, {"score": 0.5}]}) == ""

    def test_threshold_boundary(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        # exactly at threshold counts as NOT-low (the bash used `< threshold`)
        assert pb.compute_nudge({"rules": [{"score": 0.3}]}) == ""


# --------------------------------------------------------------------------- #
# 3. extract_rule_objects + split_format
# --------------------------------------------------------------------------- #
class TestExtractAndSplit:
    def test_extract_rule_objects_fields(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        objs = pb.extract_rule_objects({"rules": [
            {"rule_id": "R1", "trigger": "t", "statement": "s", "violation": "v",
             "pass_example": "p", "enforcement": "e", "domain": "d", "severity": "high"},
        ]})
        assert objs == [{"rule_id": "R1", "trigger": "t", "statement": "s", "violation": "v",
                         "pass_example": "p", "enforcement": "e", "domain": "d", "severity": "high"}]

    def test_extract_empty(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        assert pb.extract_rule_objects({}) == []

    def test_split_format_text_and_meta(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        raw = "line one\nline two\nWRIT_META:{\"rule_ids\": [\"A\", \"B\"], \"cost\": 17}"
        text, meta = pb.split_format(raw)
        assert text == "line one\nline two"
        assert meta == {"rule_ids": ["A", "B"], "cost": 17}

    def test_split_format_no_meta(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        text, meta = pb.split_format("just text")
        assert text == "just text" and meta == {"rule_ids": [], "cost": 0}

    def test_split_format_bad_meta(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        text, meta = pb.split_format("t\nWRIT_META:{not json")
        assert text == "t" and meta == {"rule_ids": [], "cost": 0}


# --------------------------------------------------------------------------- #
# 3b. tag_overlap -- pure helper marking ranked rules already delivered by this
# turn's always-on channel (Cycle A part 1, ranked/always-on field dedup).
#
# Additive, never destructive: writ/server/routes/query.py:284 caches
# extract_rule_objects(qresp) via --add-rule-objects for the compliance-matching
# path, which needs trigger+statement. If tagging stripped those fields off the
# rule dict instead of setting a flag, that cache would be corrupted -- so this
# helper must ADD `already_injected: true` for an overlapping id and touch
# nothing else. The renderer (writ/session/budget_tracking.py:cmd_format,
# tested in tests/test_ranked_alwayson_dedup.py) decides what to do with the
# flag; this helper only computes it.
#
# RED until writ/retrieval/prompt_bundle.py defines tag_overlap.
# --------------------------------------------------------------------------- #
class TestTagOverlap:
    def test_marks_overlapping_rule_ids(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        rules = [
            {"rule_id": "A-001", "trigger": "t", "statement": "s"},
            {"rule_id": "B-001", "trigger": "t2", "statement": "s2"},
        ]
        tagged = pb.tag_overlap(rules, {"A-001"})
        by_id = {r["rule_id"]: r for r in tagged}
        assert by_id["A-001"]["already_injected"] is True
        assert not by_id["B-001"].get("already_injected")

    def test_no_overlap_leaves_every_rule_unmarked(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        rules = [{"rule_id": "A-001", "trigger": "t", "statement": "s"}]
        tagged = pb.tag_overlap(rules, set())
        assert not tagged[0].get("already_injected")

    def test_additive_preserves_every_original_field(self):
        """The property the docstring at query.py:284 protects: tagging must
        never strip trigger/statement (or anything else) off the rule dict."""
        pb = _imp("writ.retrieval.prompt_bundle")
        rule = {"rule_id": "A-001", "trigger": "t", "statement": "s",
                "violation": "v", "pass_example": "p", "score": 0.8}
        tagged = pb.tag_overlap([rule], {"A-001"})[0]
        for key, value in rule.items():
            assert tagged[key] == value

    def test_does_not_mutate_the_input_dicts(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        rule = {"rule_id": "A-001", "trigger": "t", "statement": "s"}
        pb.tag_overlap([rule], {"A-001"})
        assert "already_injected" not in rule

    def test_empty_rules_list_returns_empty_list(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        assert pb.tag_overlap([], {"A-001"}) == []

    def test_rule_missing_rule_id_is_left_unmarked(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        rule = {"trigger": "t", "statement": "s"}
        tagged = pb.tag_overlap([rule], {"A-001"})[0]
        assert not tagged.get("already_injected")

    def test_result_order_and_count_match_input(self):
        pb = _imp("writ.retrieval.prompt_bundle")
        rules = [{"rule_id": f"R-{i}"} for i in range(4)]
        tagged = pb.tag_overlap(rules, {"R-1", "R-3"})
        assert len(tagged) == 4
        assert [r["rule_id"] for r in tagged] == [r["rule_id"] for r in rules]
        marked = {r["rule_id"] for r in tagged if r.get("already_injected")}
        assert marked == {"R-1", "R-3"}


# --------------------------------------------------------------------------- #
# 4. hook wiring: the channels go through /prompt-bundle, friction stays client-side
# --------------------------------------------------------------------------- #
class TestHookWiring:
    def test_hook_calls_prompt_bundle(self):
        src = open(HOOK_SH).read()
        assert "/prompt-bundle" in src

    def test_hook_no_longer_calls_query_directly_in_main_path(self):
        # The legacy main-path /query POST is gone (orchestrator branch keeps its own
        # /methodology-companion call). The broad RAG now flows through /prompt-bundle.
        src = open(HOOK_SH).read()
        assert "$WRIT_URL" not in src  # the old /query URL var is unused/removed

    def test_friction_stays_client_side(self):
        # rag_query/always_on_inject must still be emitted by the hook (cwd-relative
        # log resolution), not the daemon -- the bundle returns *_meta for this.
        src = open(HOOK_SH).read()
        assert "always_on_inject" in src
        assert "broad_meta" in src and "method_meta" in src


# --------------------------------------------------------------------------- #
# 4b. the channel-1-error early return must still skip the always-on Neo4j
# reads entirely (Cycle A part 1's reorder put this at risk: always_on_bundle
# now resolves BEFORE channel 1's render but the error check still runs right
# after channel 1's RETRIEVAL, so the early return has to fire before
# always_on_bundle is ever called, not just before its result is used).
#
# Asserting the returned shape is unchanged is not enough: a version that
# resolves always_on_bundle and THEN returns the (still byte-identical) error
# dict would pass a shape-only check while doing the exact Neo4j reads the
# early return exists to avoid. So this asserts the spy was NEVER CALLED, and
# -- since a spy that is never called because nothing ran is not evidence --
# a sibling test proves the same spy setup DOES register a call when the
# success path actually reaches it, and the faked query_rules itself is
# asserted to have been invoked so a False negative (bailing out earlier, e.g.
# on the server._pipeline is None guard) cannot be mistaken for the early
# return firing correctly.
#
# No daemon, no graph: query_rules and always_on_bundle are monkeypatched as
# module attributes of writ.server.routes.query (the handler calls both as
# bare names, so patching the module attribute is what the handler actually
# sees), mirroring tests/test_query_route_project_scope.py's route-coroutine-
# direct-call style.
# --------------------------------------------------------------------------- #
def _minimal_cache(**overrides):
    cache = {
        "loaded_rule_ids_by_phase": {}, "current_phase": "",
        "loaded_rule_ids": [], "remaining_budget": 1500,
        "last_injected_rule_ids": [], "detected_domain": "",
    }
    cache.update(overrides)
    return cache


class TestPromptBundleErrorPathSkipsAlwaysOn:
    @pytest.mark.asyncio
    async def test_error_response_never_calls_always_on_or_cmd_update(self, monkeypatch):
        monkeypatch.setattr(server, "_pipeline", object())  # sentinel: just not None
        monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: _minimal_cache())

        fake_query_rules = AsyncMock(return_value={"error": "channel 1 blew up"})
        fake_always_on = AsyncMock(return_value={"rules": [], "total_tokens": 0})
        # cmd_update is a plain sync function invoked via asyncio.to_thread in the
        # handler (never awaited directly) -- MagicMock, not AsyncMock, or calling it
        # returns an unawaited coroutine instead of actually recording the call.
        fake_cmd_update = MagicMock()
        monkeypatch.setattr(qroute, "query_rules", fake_query_rules)
        monkeypatch.setattr(qroute, "always_on_bundle", fake_always_on)
        monkeypatch.setattr(server.writ_session, "cmd_update", fake_cmd_update)

        result = await qroute.prompt_bundle(PromptBundleRequest(session_id="s1", prompt="x"))

        # Evidence the handler actually reached (and returned from) the error
        # branch, not some other path -- a MagicMock spy that was never invoked
        # would also be "not called" if the handler bailed out on the
        # server._pipeline guard instead, which would prove nothing about the
        # capability under test.
        fake_query_rules.assert_called_once()
        assert result["error"] is True
        assert result == {
            "always_on_block": "", "rules_text": "", "methodology_block": "",
            "nudge": "", "error": True,
            "broad_meta": None, "ao_meta": None, "method_meta": None,
        }

        # The discriminating assertion: a version that resolves always-on and
        # THEN returns the identical error dict would pass every assertion
        # above while still doing the Neo4j reads the early return exists to
        # skip. Only this catches that.
        fake_always_on.assert_not_called()
        fake_cmd_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_path_does_call_always_on(self, monkeypatch):
        """Anti-vacuity companion to the test above: the SAME spy/monkeypatch
        setup, on a NON-error channel-1 response, must show always_on_bundle
        WAS called. This proves the spy is capable of observing a real call on
        this exact code path -- so the 'not called' result in the error-path
        test is evidence of the early return, not of a spy that never fires."""
        monkeypatch.setattr(server, "_pipeline", object())
        monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: _minimal_cache())

        fake_query_rules = AsyncMock(return_value={"rules": [], "mode": "standard"})
        fake_always_on = AsyncMock(return_value={"rules": [], "total_tokens": 0})
        fake_cmd_update = MagicMock()  # sync, see comment on the test above
        monkeypatch.setattr(qroute, "query_rules", fake_query_rules)
        monkeypatch.setattr(qroute, "always_on_bundle", fake_always_on)
        monkeypatch.setattr(server.writ_session, "cmd_update", fake_cmd_update)
        monkeypatch.setattr(server, "_run_cmd_format_locked", lambda payload: "")

        result = await qroute.prompt_bundle(PromptBundleRequest(session_id="s1", prompt="x"))

        assert result["error"] is False
        fake_always_on.assert_called_once()


# --------------------------------------------------------------------------- #
# 4c. include_ranked: bool = True, a per-channel toggle on THIS endpoint
# (plan dfacff61, Decision 2), so an orchestrator master can suppress the
# ranked channel without a second endpoint or a second render path. Channels
# 2 (always-on) and 3 (methodology) are untouched by the flag on purpose.
#
# `False` is not a valid `error`, `rules_text` or `broad_meta` reading here on
# accident: bool is an int in Python, so every truthiness/identity check below
# is written against the LITERAL (`is False`, `== ""`, `== {"suppressed": True}`),
# never a bare `not x`, which a stray `0` or `True` could satisfy by coincidence.
# --------------------------------------------------------------------------- #
class TestIncludeRankedField:
    def test_field_defaults_true(self):
        req = PromptBundleRequest(session_id="s1")
        assert req.include_ranked is True

    def test_field_accepts_false(self):
        req = PromptBundleRequest(session_id="s1", include_ranked=False)
        assert req.include_ranked is False


class TestPromptBundleSuppressedRankedChannel:
    """Capability 1 (capabilities.md): include_ranked=false returns an empty
    rules_text and broad_meta == {"suppressed": true}, while always_on_block
    and methodology_block are both still non-empty.

    An empty broad_meta (rather than the {"suppressed": True} sentinel) would
    be indistinguishable from a zero-rule rag_query, which is the ABSTENTION
    signal every census that counts retrievals by source relies on, so the
    shape matters, not just the emptiness of rules_text.

    MUTATION (plan.md verification table): defaulting include_ranked to True
    at the call site (accepting the field but never actually wiring it into
    the channel-1 guard) turns this red.
    """

    @pytest.mark.asyncio
    async def test_suppressed_shape(self, monkeypatch):
        monkeypatch.setattr(server, "_pipeline", object())
        monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: _minimal_cache())
        fake_query_rules = AsyncMock(return_value={"rules": [{"rule_id": "R1", "score": 0.9}]})
        fake_always_on = AsyncMock(return_value={
            "total_tokens": 42,
            "rules": [{"rule_id": "AO-1", "trigger": "t", "statement": "s"}],
        })
        # mode="work" below routes channel 3 to the methodology source, so the
        # companion itself must be mocked too, or it would try to hit the
        # real (absent) pipeline through the "_pipeline = object()" sentinel.
        fake_companion = AsyncMock(return_value={"rules": [{"rule_id": "M-1"}]})
        fake_cmd_update = MagicMock()
        monkeypatch.setattr(qroute, "query_rules", fake_query_rules)
        monkeypatch.setattr(qroute, "always_on_bundle", fake_always_on)
        monkeypatch.setattr(qroute, "methodology_companion", fake_companion)
        monkeypatch.setattr(server.writ_session, "cmd_update", fake_cmd_update)
        # Always returns non-empty text regardless of payload: if a correct
        # implementation never calls this for the suppressed channel 1, its
        # return value is irrelevant there; if an incorrect one still calls it
        # for channel 1, rules_text becomes non-empty and the assertion below
        # (rather than this helper) is what turns red.
        monkeypatch.setattr(
            server, "_run_cmd_format_locked",
            lambda payload: 'companion text\nWRIT_META:{"rule_ids": [], "cost": 5}',
        )

        result = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", mode="work", include_ranked=False,
        ))

        assert result["error"] is False
        assert result["rules_text"] == ""
        assert result["broad_meta"] == {"suppressed": True}
        assert result["always_on_block"] != ""
        assert result["methodology_block"] != ""

        # Retrieval itself must not run either: suppression is a request-time
        # decision, not a render-time one that still pays for the Neo4j read
        # and throws the text away.
        fake_query_rules.assert_not_called()

    @pytest.mark.asyncio
    async def test_suppressed_channel_never_runs_the_ranked_cache_update(self, monkeypatch):
        """MUTATION: leaving the channel-1 --add-rules / --set-last-injected-
        rule-ids / --add-rule-objects update running when include_ranked=False
        turns this red, even though the shape assertions above would still
        look right, because the update is a side effect the shape alone
        cannot see. Channel 2's update must still run: only channel 1 is
        suppressed."""
        monkeypatch.setattr(server, "_pipeline", object())
        monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: _minimal_cache())
        fake_query_rules = AsyncMock(return_value={"rules": [{"rule_id": "SHOULD-NOT-RECORD"}]})
        fake_always_on = AsyncMock(return_value={
            "total_tokens": 10, "rules": [{"rule_id": "AO-1", "trigger": "t", "statement": "s"}],
        })
        fake_cmd_update = MagicMock()
        monkeypatch.setattr(qroute, "query_rules", fake_query_rules)
        monkeypatch.setattr(qroute, "always_on_bundle", fake_always_on)
        monkeypatch.setattr(server.writ_session, "cmd_update", fake_cmd_update)

        await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", include_ranked=False,
        ))

        flags_seen: set[str] = set()
        for call in fake_cmd_update.call_args_list:
            flags_seen.update(a for a in call.args[-1] if isinstance(a, str) and a.startswith("--"))
        assert "--add-rules" not in flags_seen, (
            f"channel 1's cache update ran even though it was suppressed: {flags_seen}"
        )
        assert "--add-always-on-rules" in flags_seen, (
            "channel 2's update must still run when only channel 1 is suppressed"
        )


class TestPromptBundleDefaultUnchanged:
    """Capability 2 (capabilities.md), the anti-vacuity control the plan names
    explicitly: a caller that never mentions include_ranked must see EXACTLY
    today's shape. Without this, a change that disabled every channel (or
    quietly defaulted the field to False) could make the suppressed-shape
    test above look like a delivered feature.

    MUTATION (plan.md verification table): making the channel-1 guard
    unconditional (`if False:` around the ranked retrieval, i.e. always
    suppressed) turns this red: a default of True on the field alone is not
    enough if the guard itself does not read it.
    """

    @pytest.mark.asyncio
    async def test_include_ranked_omitted_still_populates_rules_text_and_broad_meta(
        self, monkeypatch
    ):
        monkeypatch.setattr(server, "_pipeline", object())
        monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: _minimal_cache())
        fake_query_rules = AsyncMock(return_value={"rules": [{"rule_id": "R1", "score": 0.9}]})
        fake_always_on = AsyncMock(return_value={"rules": [], "total_tokens": 0})
        fake_cmd_update = MagicMock()
        monkeypatch.setattr(qroute, "query_rules", fake_query_rules)
        monkeypatch.setattr(qroute, "always_on_bundle", fake_always_on)
        monkeypatch.setattr(server.writ_session, "cmd_update", fake_cmd_update)
        monkeypatch.setattr(
            server, "_run_cmd_format_locked",
            lambda payload: 'some rules text\nWRIT_META:{"rule_ids": ["R1"], "cost": 12}',
        )

        req = PromptBundleRequest(session_id="s1", prompt="x")  # include_ranked NOT set
        result = await qroute.prompt_bundle(req)

        fake_query_rules.assert_called_once()
        assert result["error"] is False
        assert result["rules_text"] != ""
        assert result["broad_meta"] is not None
        assert "rule_ids" in result["broad_meta"] and "cost" in result["broad_meta"]


class TestPromptBundleSuppressedNudge:
    """Capability 3 (capabilities.md): include_ranked=false returns
    nudge == "", not "NO_RULES". compute_nudge({"rules": []}) reads
    "NO_RULES", which tells the master to propose a rule to fix an absence
    that is a configuration choice, not a retrieval miss, so the nudge
    computation must not run on the skipped channel at all, not merely be fed
    an empty qresp.

    MUTATION (plan.md verification table): leaving compute_nudge(qresp)
    running on the skipped channel turns this red.
    """

    @pytest.mark.asyncio
    async def test_nudge_is_empty_not_no_rules(self, monkeypatch):
        monkeypatch.setattr(server, "_pipeline", object())
        monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: _minimal_cache())
        # If compute_nudge ran on this, it would read "NO_RULES": the exact
        # failure this test exists to catch.
        fake_query_rules = AsyncMock(return_value={"rules": []})
        fake_always_on = AsyncMock(return_value={"rules": [], "total_tokens": 0})
        monkeypatch.setattr(qroute, "query_rules", fake_query_rules)
        monkeypatch.setattr(qroute, "always_on_bundle", fake_always_on)
        monkeypatch.setattr(server.writ_session, "cmd_update", MagicMock())

        result = await qroute.prompt_bundle(PromptBundleRequest(
            session_id="s1", prompt="x", include_ranked=False,
        ))

        assert result["nudge"] == ""


# --------------------------------------------------------------------------- #
# 5. live endpoint shape -- ROUTE (Decision 4, plan.md
# 2412ba38-51e1-4b73-895b-7b240a3c21d3): no daemon, no socket. route_db and
# route_pipeline install everything /prompt-bundle needs; isolated_cache points
# both the route's server-side cache read/write and this test's own reads at
# the SAME dir. Converts the module's one skip-gated integration test; the
# ~20 direct qroute.prompt_bundle unit tests above are structurally blind to
# the wiring this proves (route registration, corpus preconditions, the
# server-side cache read/write /prompt-bundle actually does).
# --------------------------------------------------------------------------- #
class TestPromptBundleEndpointLive:
    @pytest.mark.asyncio
    async def test_endpoint_returns_rendered_pieces(
        self, isolated_cache, route_db, route_pipeline
    ) -> None:
        from httpx import ASGITransport, AsyncClient
        from writ.session.cache import _read_cache, _write_cache

        sid = f"pbtest-{uuid.uuid4().hex[:8]}"
        cache = _read_cache(sid)
        cache["mode"] = "work"
        before_queries = cache.get("queries", 0)
        _write_cache(sid, cache)

        body = {
            "session_id": sid, "mode": "work",
            "prompt": "refactor the SQL query builder to use parameterized queries",
            "effort": "", "always_on_filter": True,
        }
        async with AsyncClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/prompt-bundle", json=body)
        assert resp.status_code == 200, (
            f"POST /prompt-bundle returned {resp.status_code}, not 200: {resp.text}"
        )
        data = resp.json()
        for key in ("always_on_block", "rules_text", "methodology_block", "nudge",
                    "error", "broad_meta", "ao_meta", "method_meta"):
            assert key in data, key
        assert data["error"] is False
        assert isinstance(data["always_on_block"], str)

        # NON-EMPTY, not merely a string: the corpus precondition route_db
        # already owns (require_injection_population) is what makes this
        # non-vacuous. MUTATION: returning an empty always_on_block while every
        # other key stays present reddens only this assertion, which is why
        # the two travel together (plan.md decision 5, module 2).
        assert data["always_on_block"] != "", (
            "always_on_block must be non-empty against the real injection-rule "
            "population route_db's precondition already proved present"
        )

        # THE IN-RUN POSITIVE: the SERVER's own `queries` counter
        # (writ/server/routes/query.py:321, --inc-queries) must have moved,
        # attributing this response to the request rather than to a cache
        # default.
        after = _read_cache(sid)
        assert after.get("queries", 0) == before_queries + 1, (
            f"the server-side queries counter must increment by exactly one per "
            f"/prompt-bundle call; before={before_queries}, "
            f"after={after.get('queries', 0)}"
        )
