"""Phase 0 methodology retrieval benchmark.

Release blockers (plan Section 5.3):
- MRR@5 >= 0.78
- hit rate >= 0.90
- bundle completeness >= 0.85
- p95 latency <= 5ms

Fails loudly when any blocker misses; the maintainer writes docs/phase-0-report.md
and escalates per plan Section 0.5 failure-mode policy. Do NOT lower thresholds.

POL-2: the benchmark mechanics (rrf_fuse / retrieve / bundle_for / benchmark_metrics + the
blocker constants) now live in tests/fixtures/benchmark_harness.py, and the corpus / index /
encode are session-scoped conftest fixtures (methodology_*) shared with the INC absorption tests.
"""
from __future__ import annotations

import pytest

# onnxruntime is the embedding backend used by these benchmark tests.
# It is not a hard repo dependency (Phase 0 benchmark only); skip the
# whole module when absent rather than erroring at collection time.
pytest.importorskip("onnxruntime")

from tests.fixtures.benchmark_harness import (  # noqa: E402


    BLOCKER_COMPLETENESS,
    BLOCKER_HIT_RATE,
    BLOCKER_MRR,
    BLOCKER_P95_MS,
    benchmark_metrics,
)

from tests._bible_guard import requires_bible

pytestmark = requires_bible



@pytest.fixture(scope="session")
def benchmark_results(
    methodology_ground_truth: dict,
    methodology_kindex,
    methodology_model,
    methodology_node_vectors: dict,
    methodology_adjacency: dict,
) -> dict:
    """Run every ground-truth query once via the shared harness. Aggregate metrics + detail."""
    return benchmark_metrics(
        methodology_ground_truth,
        methodology_kindex,
        methodology_model,
        methodology_node_vectors,
        methodology_adjacency,
    )


class TestPhase0Blockers:
    def test_mrr_at_5(self, benchmark_results: dict) -> None:
        m = benchmark_results["mrr_at_5"]
        assert m >= BLOCKER_MRR, (
            f"MRR@5 = {m:.4f} below blocker {BLOCKER_MRR}. "
            f"Halt Phase 0, write docs/phase-0-report.md, escalate. "
            f"Do NOT lower the threshold."
        )

    def test_hit_rate(self, benchmark_results: dict) -> None:
        h = benchmark_results["hit_rate"]
        assert h >= BLOCKER_HIT_RATE, (
            f"hit_rate = {h:.4f} below blocker {BLOCKER_HIT_RATE}"
        )

    def test_bundle_completeness(self, benchmark_results: dict) -> None:
        c = benchmark_results["bundle_completeness"]
        assert c >= BLOCKER_COMPLETENESS, (
            f"bundle_completeness = {c:.4f} below blocker {BLOCKER_COMPLETENESS}"
        )

    def test_p95_latency(self, benchmark_results: dict) -> None:
        # Blocker measures the retrieval pipeline (post-encode), mirroring Writ's
        # published p95 definition. Encode latency is tracked separately in
        # p95_encode_ms for visibility but does not gate Phase 0.
        p = benchmark_results["p95_retrieval_ms"]
        assert p <= BLOCKER_P95_MS, (
            f"p95_retrieval_ms = {p:.2f}ms above blocker {BLOCKER_P95_MS}ms"
        )
