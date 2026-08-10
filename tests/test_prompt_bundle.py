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

import pytest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
# The `mode set work` call below sits behind a daemon-liveness skip, which is why the
# sentinel probe that found the other 26 modules reported this one clean: with no daemon
# listening the test skipped and never reached the deletion.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
HOOK_SH = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-rag-inject.sh")


def _imp(name):
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module(name)


def _test_daemon_up() -> bool:
    try:
        from tests._daemon import _daemon_health
        return _daemon_health() is not None
    except Exception:
        return False


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
# 5. live endpoint shape (skips when no daemon on the suite's test port)
# --------------------------------------------------------------------------- #
class TestPromptBundleEndpointLive:
    def test_endpoint_returns_rendered_pieces(self):
        if not _test_daemon_up():
            pytest.skip("test daemon not running on test port")
        import subprocess
        import uuid
        helper = os.path.join(SKILL_ROOT, "bin", "lib", "writ-session.py")
        sid = f"pbtest-{uuid.uuid4().hex[:8]}"
        subprocess.run([sys.executable, helper, "mode", "set", "work", sid], capture_output=True)
        from tests._daemon import _port
        body = json.dumps({
            "session_id": sid, "mode": "work",
            "prompt": "refactor the SQL query builder to use parameterized queries",
            "effort": "", "always_on_filter": True,
        })
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", f"http://localhost:{_port()}/prompt-bundle",
             "-H", "Content-Type: application/json", "-d", body],
            capture_output=True, text=True,
        )
        subprocess.run([sys.executable, helper, "clear", sid], capture_output=True)
        data = json.loads(r.stdout)
        for key in ("always_on_block", "rules_text", "methodology_block", "nudge",
                    "error", "broad_meta", "ao_meta", "method_meta"):
            assert key in data, key
        assert data["error"] is False
        assert isinstance(data["always_on_block"], str)
