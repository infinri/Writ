"""Cycle 8: resolve the two latent ranking levers.

Authority-preference re-ranking gets a real config path (default off) and the
sticky-tiebreak coupling is fixed by ordering the passes; the bundle-cohesion
weight is deleted because its design premise (methodology nodes ranked by the
production pipeline) was superseded by the deterministic trigger index, and a
gold-set sweep found no do-no-harm setting.

Per TEST-TDD-001: skeletons approved before implementation. Imports are LOCAL to
each test on purpose: a module-level import of the not-yet-existing reader would
skip the file, and a skipped test is not RED evidence.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml(tmp_path, body: str) -> str:
    path = tmp_path / "writ.toml"
    path.write_text(body)
    return str(path)


def _meta(authority: str) -> dict:
    return {
        "node_type": "Rule",
        "routes": ["semantic"],
        "domain": "testing",
        "severity": "high",
        "confidence": "production-validated",
        "authority": authority,
        "statement": "Synthetic statement.",
        "trigger": "when testing pass composition",
    }


# Ten synthetic candidates. Reciprocal-rank normalization fixes the score ladder
# regardless of the raw stub scores, so the positions below are chosen from the
# MEASURED ladder rather than guessed:
#
#   R0 0.9455  R1 0.5495  R2 0.4175  R3 0.3514  R4 0.3119
#   R5 0.2855  AI-PROV-001 0.2666  HUMAN-001 0.2525  R8 0.2414  R9 0.2327
#
# The ai-provisional rule sits in the TAIL, where adjacent gaps (0.0141, 0.0087)
# are inside STICKY_TIEBREAK_THRESHOLD (0.02). That placement is what makes the
# two passes actually interact: an authority swap there leaves the list
# non-descending exactly where the tiebreak computes its groups.
LADDER_IDS = ["R0", "R1", "R2", "R3", "R4", "R5", "AI-PROV-001", "HUMAN-001", "R8", "R9"]
LADDER_METADATA = {
    rid: _meta("ai-provisional" if rid.startswith("AI-PROV") else "human")
    for rid in LADDER_IDS
}
# Threshold that reaches the AI-PROV/HUMAN gap (0.0141) and the gaps to the two
# rules below it, but not the 0.0395+ gaps higher up the ladder.
SWAP_THRESHOLD = 0.05

# Same ladder, but the ai-provisional rule is LAST (0.2327). The bottom three
# scores (0.2525, 0.2414, 0.2327) are all within 0.02 of each other, so they form
# one sticky-tiebreak group: preferring the ai-provisional rule asks the tiebreak
# to lift it above two human rules, which is what the authority pass must undo.
TAIL_PROVISIONAL_IDS = [f"R{i}" for i in range(9)] + ["AI-TAIL-001"]
TAIL_PROVISIONAL_METADATA = {
    rid: _meta("ai-provisional" if rid.startswith("AI-TAIL") else "human")
    for rid in TAIL_PROVISIONAL_IDS
}


def _make_stub_pipeline(metadata: dict, authority_preference_threshold: float = 0.0):
    """RetrievalPipeline over synthetic stubs so query() runs without Neo4j/ONNX.

    Mirrors _make_stub_pipeline in tests/test_retrievable_filter.py. Every
    candidate is semantic-routed so the default filter admits it; the caller
    supplies the authority values under test.
    """
    from unittest.mock import MagicMock

    from writ.retrieval.embeddings import ScoredResult
    from writ.retrieval.pipeline import RetrievalPipeline
    from writ.retrieval.traversal import AdjacencyCache

    ids = list(metadata)
    keyword_stub = MagicMock()
    keyword_stub.search.return_value = [
        {"rule_id": rid, "score": 0.90 - i * 0.01} for i, rid in enumerate(ids)
    ]
    vector_stub = MagicMock()
    vector_stub.search.return_value = [
        ScoredResult(rule_id=rid, score=0.90 - i * 0.01) for i, rid in enumerate(ids)
    ]
    encoder_stub = MagicMock()
    encoder_stub.encode.return_value = np.zeros(384, dtype=np.float32)

    return RetrievalPipeline(
        keyword_index=keyword_stub,
        vector_store=vector_stub,
        adjacency_cache=AdjacencyCache(),
        embedding_model=encoder_stub,
        rule_metadata=metadata,
        authority_preference_threshold=authority_preference_threshold,
    )


# ---------------------------------------------------------------------------
# 1. Config reader -- the threshold stops being a hardcoded 0.0
# ---------------------------------------------------------------------------


class TestAuthorityPreferenceConfig:
    """[retrieval] authority_preference_threshold gets a live writ/config.py reader."""

    def test_missing_file_returns_zero(self, tmp_path) -> None:
        from writ.config import get_authority_preference_threshold

        assert get_authority_preference_threshold(str(tmp_path / "absent.toml")) == 0.0

    def test_no_retrieval_section_returns_zero(self, tmp_path) -> None:
        from writ.config import get_authority_preference_threshold

        path = _write_toml(tmp_path, '[neo4j]\nuri = "bolt://x:7687"\n')
        assert get_authority_preference_threshold(path) == 0.0

    def test_section_present_but_key_missing_returns_zero(self, tmp_path) -> None:
        from writ.config import get_authority_preference_threshold

        path = _write_toml(tmp_path, "[retrieval]\n")
        assert get_authority_preference_threshold(path) == 0.0

    def test_configured_value_is_returned(self, tmp_path) -> None:
        from writ.config import get_authority_preference_threshold

        path = _write_toml(tmp_path, "[retrieval]\nauthority_preference_threshold = 0.05\n")
        assert get_authority_preference_threshold(path) == pytest.approx(0.05)

    def test_integer_value_is_coerced_to_float(self, tmp_path) -> None:
        """Uses a NON-ZERO integer on purpose: 0 equals the fallback default, so it
        cannot distinguish real coercion from a silent fall-through."""
        from writ.config import get_authority_preference_threshold

        path = _write_toml(tmp_path, "[retrieval]\nauthority_preference_threshold = 3\n")
        result = get_authority_preference_threshold(path)
        assert isinstance(result, float)
        assert result == 3.0

    def test_boolean_value_falls_back_to_zero(self, tmp_path) -> None:
        """bool is an int subclass; `true` must not become 1.0."""
        from writ.config import get_authority_preference_threshold

        path = _write_toml(tmp_path, "[retrieval]\nauthority_preference_threshold = true\n")
        assert get_authority_preference_threshold(path) == 0.0

    def test_negative_value_falls_back_to_zero(self, tmp_path) -> None:
        from writ.config import get_authority_preference_threshold

        path = _write_toml(tmp_path, "[retrieval]\nauthority_preference_threshold = -0.5\n")
        assert get_authority_preference_threshold(path) == 0.0

    @pytest.mark.parametrize("literal", ["nan", "inf", "1e400"])
    def test_non_finite_value_falls_back_to_zero(self, tmp_path, literal: str) -> None:
        """TOML has literal nan/inf, and an overflowing exponent parses to inf.

        Each is a float and none is negative, so without an explicit finite check
        they reach apply_authority_preference, where `threshold <= 0.0` is False
        and every `gap > threshold` is False, swapping EVERY adjacent
        authority-mismatched pair regardless of score distance.
        """
        from writ.config import get_authority_preference_threshold

        path = _write_toml(
            tmp_path, f"[retrieval]\nauthority_preference_threshold = {literal}\n"
        )
        assert get_authority_preference_threshold(path) == 0.0

    def test_non_finite_threshold_would_swap_distant_pairs(self) -> None:
        """Why the guard above matters: proves the downstream blast radius."""
        from writ.retrieval.ranking import apply_authority_preference

        rules = [
            {"rule_id": "AI", "score": 0.9, "authority": "ai-provisional"},
            {"rule_id": "HUMAN", "score": 0.1, "authority": "human"},
        ]
        swapped = apply_authority_preference(list(rules), float("nan"))
        assert [r["rule_id"] for r in swapped] == ["HUMAN", "AI"], (
            "a non-finite threshold reorders rules 0.8 apart; the config reader "
            "must never let one through"
        )

    def test_non_numeric_value_falls_back_to_zero(self, tmp_path) -> None:
        """A malformed optional tuning key must not stop the daemon from starting."""
        from writ.config import get_authority_preference_threshold

        path = _write_toml(tmp_path, '[retrieval]\nauthority_preference_threshold = "high"\n')
        assert get_authority_preference_threshold(path) == 0.0

    def test_default_constant_is_off(self) -> None:
        """Shipped default stays 0.0: the sweep found no gain from enabling it."""
        from writ.config import DEFAULT_AUTHORITY_PREFERENCE_THRESHOLD

        assert DEFAULT_AUTHORITY_PREFERENCE_THRESHOLD == 0.0


# ---------------------------------------------------------------------------
# 2. writ.toml.example -- the key is discoverable and spelled the same
# ---------------------------------------------------------------------------


class TestTomlExampleRetrievalSection:
    """The example file documents the key the reader actually reads.

    SECURITY: parses writ.toml.example only, never writ.toml (gitignored secrets).
    """

    def test_example_parses_a_retrieval_section(self) -> None:
        import tomllib

        with (REPO_ROOT / "writ.toml.example").open("rb") as f:
            cfg = tomllib.load(f)
        assert "retrieval" in cfg

    def test_example_key_name_matches_the_reader(self) -> None:
        import tomllib

        with (REPO_ROOT / "writ.toml.example").open("rb") as f:
            cfg = tomllib.load(f)
        assert "authority_preference_threshold" in cfg["retrieval"]

    def test_example_ships_the_feature_off(self) -> None:
        import tomllib

        with (REPO_ROOT / "writ.toml.example").open("rb") as f:
            cfg = tomllib.load(f)
        assert float(cfg["retrieval"]["authority_preference_threshold"]) == 0.0


# ---------------------------------------------------------------------------
# 3. Startup wiring -- the configured value reaches the live pipeline
# ---------------------------------------------------------------------------


class TestServerStartupWiring:
    """lifespan() must source the threshold from config, not from the default.

    Structural (AST) rather than behavioral: lifespan opens a Neo4j connection
    and pre-warms every index, so calling it in a unit test would need the graph.
    The pin is that the kwarg is passed and its value comes from the getter.
    """

    def _lifespan_call(self) -> ast.Call:
        src = (REPO_ROOT / "writ" / "server" / "__init__.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name == "build_pipeline":
                    return node
        raise AssertionError("no build_pipeline call found in writ/server/__init__.py")

    def test_startup_passes_the_threshold_kwarg(self) -> None:
        call = self._lifespan_call()
        assert "authority_preference_threshold" in {kw.arg for kw in call.keywords}

    def test_threshold_kwarg_comes_from_the_config_getter(self) -> None:
        call = self._lifespan_call()
        kw = next(k for k in call.keywords if k.arg == "authority_preference_threshold")
        assert isinstance(kw.value, ast.Call), (
            "the threshold must be read from config at startup, not hardcoded"
        )
        fname = getattr(kw.value.func, "id", None) or getattr(kw.value.func, "attr", None)
        assert fname == "get_authority_preference_threshold"


# ---------------------------------------------------------------------------
# 4. Pass composition -- the sticky-tiebreak coupling the review flagged
# ---------------------------------------------------------------------------


class TestPassComposition:
    """apply_authority_preference must run AFTER _apply_sticky_tiebreak.

    The tiebreak's grouping assumes descending-sorted input (it takes the first
    element of each group as the group max). The authority pass deliberately
    breaks descending order. Ordering the passes the other way restores the
    precondition and gives the hard preference the final word.
    """

    def _spy_on_tiebreak(self, monkeypatch) -> list[list[float]]:
        import writ.retrieval.pipeline as pipeline_mod

        seen: list[list[float]] = []
        original = pipeline_mod._apply_sticky_tiebreak

        def _spy(scored_rules, prefer_rule_ids):
            seen.append([r["score"] for r in scored_rules])
            return original(scored_rules, prefer_rule_ids)

        monkeypatch.setattr(pipeline_mod, "_apply_sticky_tiebreak", _spy)
        return seen

    def test_tiebreak_input_is_descending_when_a_swap_happened(self, monkeypatch) -> None:
        """The core regression: an authority swap must not reach the tiebreak.

        Measured RED before the fix: the tiebreak's input ends
        [..., 0.2525, 0.2414, 0.2327, 0.2666], because the authority pass sinks
        the 0.2666 ai-provisional rule past three lower-scored human rules
        BEFORE the tiebreak computes its groups.
        """
        seen = self._spy_on_tiebreak(monkeypatch)
        pipe = _make_stub_pipeline(LADDER_METADATA, authority_preference_threshold=SWAP_THRESHOLD)
        result = pipe.query(
            "pass composition", budget_tokens=100000, prefer_rule_ids=["R9"]
        )

        assert seen, "_apply_sticky_tiebreak was never called"
        # Guard against a vacuous pass: the fixture must actually trigger a swap.
        final_ids = [r["rule_id"] for r in result["rules"]]
        assert final_ids.index("HUMAN-001") < final_ids.index("AI-PROV-001"), (
            "fixture no longer exercises an authority swap; the assertion below "
            "would pass for the wrong reason"
        )
        assert seen[0] == sorted(seen[0], reverse=True), (
            "the sticky tiebreak groups by treating the first element of each run "
            "as the group max, which is only valid on descending input"
        )

    def test_preferred_rule_is_promoted_within_its_tie_group(self, monkeypatch) -> None:
        """The tiebreak still does its job once the passes are reordered."""
        pipe = _make_stub_pipeline(LADDER_METADATA, authority_preference_threshold=SWAP_THRESHOLD)
        result = pipe.query(
            "pass composition", budget_tokens=100000, prefer_rule_ids=["R9"]
        )
        ids = [r["rule_id"] for r in result["rules"]]
        assert ids.index("R9") < ids.index("R8"), (
            "R9 and R8 are within STICKY_TIEBREAK_THRESHOLD, so preferring R9 "
            "must lift it above R8"
        )

    def test_preferred_ai_provisional_rule_cannot_be_promoted_above_humans(self) -> None:
        """Regression on the OUTCOME: the old order let the tiebreak defeat the
        preference outright.

        With the authority pass running first, it had nothing to do (the
        ai-provisional rule was already last), and the sticky tiebreak then
        promoted that rule to the top of its tie group, above human rules the
        preference exists to protect. Running the preference last sinks it back.

        Measured over an exhaustive 12,960-configuration comparison of the two
        pass orders, 4,923 diverge; this is the shape of the divergence that
        matters.
        """
        pipe = _make_stub_pipeline(
            TAIL_PROVISIONAL_METADATA, authority_preference_threshold=SWAP_THRESHOLD
        )
        result = pipe.query(
            "pass composition", budget_tokens=100000, prefer_rule_ids=["AI-TAIL-001"]
        )
        ids = [r["rule_id"] for r in result["rules"]]
        # R7 and R8 are the human rules sharing AI-TAIL-001's tie group.
        assert ids.index("AI-TAIL-001") > ids.index("R7")
        assert ids.index("AI-TAIL-001") > ids.index("R8")

    def test_authority_pass_is_the_last_reorder_in_query(self) -> None:
        """Source-order pin: the authority call follows the tiebreak call."""
        from writ.retrieval.pipeline import RetrievalPipeline

        src = inspect.getsource(RetrievalPipeline.query)
        assert "apply_authority_preference" in src
        assert "_apply_sticky_tiebreak" in src
        assert src.index("_apply_sticky_tiebreak") < src.index("apply_authority_preference"), (
            "the hard authority preference must be applied after the sticky tiebreak"
        )

    def test_default_threshold_leaves_order_untouched(self) -> None:
        """Shipped default (0.0) must not change any ordering."""
        off = _make_stub_pipeline(LADDER_METADATA, authority_preference_threshold=0.0)
        result = off.query("pass composition", budget_tokens=100000)
        scores = [r["score"] for r in result["rules"]]
        assert scores == sorted(scores, reverse=True)
        ids = [r["rule_id"] for r in result["rules"]]
        assert ids.index("AI-PROV-001") < ids.index("HUMAN-001"), (
            "with the preference off, the higher-scored ai-provisional rule keeps "
            "its position"
        )


# ---------------------------------------------------------------------------
# 5. Bundle-cohesion removal
# ---------------------------------------------------------------------------


class TestBundleCohesionRemoved:
    """The weight, its parameter, and its pipeline stage are gone."""

    def test_ranking_weights_has_no_bundle_field(self) -> None:
        from writ.retrieval.ranking import RankingWeights

        assert not hasattr(RankingWeights(), "w_bundle_cohesion")

    def test_default_weights_still_sum_to_one(self) -> None:
        from writ.retrieval.ranking import RankingWeights

        RankingWeights().validate()

    def test_literal_weights_still_sum_to_one(self) -> None:
        from writ.retrieval.ranking import RankingWeights

        RankingWeights.literal().validate()

    def test_compute_score_rejects_the_bundle_argument(self) -> None:
        from writ.retrieval.ranking import compute_score

        assert "bundle_cohesion" not in inspect.signature(compute_score).parameters

    def test_pipeline_has_no_bundle_stage(self) -> None:
        from writ.retrieval.pipeline import RetrievalPipeline

        assert not hasattr(RetrievalPipeline, "_compute_bundle_cohesion")

    def test_no_source_file_references_the_removed_weight(self) -> None:
        """Repo scan: a stale reference would be a NameError at import or call time."""
        offenders: list[str] = []
        for root in ("writ", "scripts", "benchmarks"):
            for path in (REPO_ROOT / root).rglob("*.py"):
                text = path.read_text()
                if "w_bundle_cohesion" in text or "_compute_bundle_cohesion" in text:
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == []

    def test_removed_test_file_is_gone(self) -> None:
        assert not (REPO_ROOT / "tests" / "test_bundle_cohesion.py").exists()
