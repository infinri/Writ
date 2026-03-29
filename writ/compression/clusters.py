"""Rule clustering by embedding similarity.

Per ARCH-ORG-001: clustering logic is separate from abstraction generation.
Per PERF-BIGO-001: HDBSCAN is O(n^2) worst case, bounded by domain rule count (~45).

Algorithm decision: HDBSCAN (sklearn.cluster.HDBSCAN) is preferred because it
auto-discovers cluster count. k-means requires predetermined k. Both are evaluated
during `writ compress` and results logged for comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from sklearn.cluster import HDBSCAN, KMeans
from sklearn.metrics import silhouette_score

if TYPE_CHECKING:
    from numpy.typing import NDArray

# Per ARCH-CONST-001: named constants.
HDBSCAN_MIN_CLUSTER_SIZE = 2
HDBSCAN_MIN_SAMPLES = 1
KMEANS_DEFAULT_K = 8
KMEANS_MAX_K = 15
KMEANS_RANDOM_STATE = 42


@dataclass
class ClusterResult:
    """Output of a clustering run."""

    clusters: dict[int, list[str]]  # cluster_id -> list of rule_ids
    ungrouped: list[str]  # rule_ids not assigned to any cluster
    centroid_indices: dict[int, int]  # cluster_id -> index of centroid-nearest rule
    algorithm: str
    silhouette: float


@dataclass
class ComparisonResult:
    """Side-by-side evaluation of HDBSCAN vs k-means."""

    hdbscan: ClusterResult
    kmeans: ClusterResult
    chosen: str  # "hdbscan" or "kmeans"
    reason: str


def cluster_hdbscan(
    rule_ids: list[str],
    embeddings: NDArray[np.float32],
) -> ClusterResult:
    """Cluster rules using HDBSCAN. Auto-discovers cluster count."""
    if len(rule_ids) < HDBSCAN_MIN_CLUSTER_SIZE:
        return ClusterResult(
            clusters={}, ungrouped=list(rule_ids),
            centroid_indices={}, algorithm="hdbscan", silhouette=-1.0,
        )

    model = HDBSCAN(
        min_cluster_size=HDBSCAN_MIN_CLUSTER_SIZE,
        min_samples=HDBSCAN_MIN_SAMPLES,
        metric="euclidean",
    )
    labels = model.fit_predict(embeddings)
    return _build_result(rule_ids, embeddings, labels, "hdbscan")


def cluster_kmeans(
    rule_ids: list[str],
    embeddings: NDArray[np.float32],
    k: int | None = None,
) -> ClusterResult:
    """Cluster rules using k-means. Requires predetermined k."""
    if len(rule_ids) < 2:
        return ClusterResult(
            clusters={}, ungrouped=list(rule_ids),
            centroid_indices={}, algorithm="kmeans", silhouette=-1.0,
        )

    max_k = min(KMEANS_MAX_K, len(rule_ids) - 1)
    if k is None:
        k = min(KMEANS_DEFAULT_K, max_k)
    k = max(2, min(k, max_k))

    model = KMeans(n_clusters=k, random_state=KMEANS_RANDOM_STATE, n_init=10)
    labels = model.fit_predict(embeddings)
    return _build_result(rule_ids, embeddings, labels, "kmeans")


def evaluate_both(
    rule_ids: list[str],
    embeddings: NDArray[np.float32],
) -> ComparisonResult:
    """Run both algorithms and choose the better one.

    Selection criteria: higher silhouette score wins. If HDBSCAN produces
    no clusters (all noise), k-means wins by default.
    """
    hdbscan_result = cluster_hdbscan(rule_ids, embeddings)
    kmeans_result = cluster_kmeans(rule_ids, embeddings)

    if not hdbscan_result.clusters:
        chosen, reason = "kmeans", "HDBSCAN produced no clusters (all noise)"
    elif hdbscan_result.silhouette >= kmeans_result.silhouette:
        chosen = "hdbscan"
        reason = (
            f"HDBSCAN silhouette {hdbscan_result.silhouette:.3f} >= "
            f"k-means {kmeans_result.silhouette:.3f}"
        )
    else:
        chosen = "kmeans"
        reason = (
            f"k-means silhouette {kmeans_result.silhouette:.3f} > "
            f"HDBSCAN {hdbscan_result.silhouette:.3f}"
        )

    return ComparisonResult(
        hdbscan=hdbscan_result,
        kmeans=kmeans_result,
        chosen=chosen,
        reason=reason,
    )


def _build_result(
    rule_ids: list[str],
    embeddings: NDArray[np.float32],
    labels: NDArray[np.int64],
    algorithm: str,
) -> ClusterResult:
    """Build ClusterResult from label assignments."""
    clusters: dict[int, list[str]] = {}
    ungrouped: list[str] = []

    for i, label in enumerate(labels):
        label_int = int(label)
        if label_int == -1:
            ungrouped.append(rule_ids[i])
        else:
            clusters.setdefault(label_int, []).append(rule_ids[i])

    # Remove singleton clusters -> move to ungrouped (INV-SINGLETON).
    singleton_keys = [k for k, v in clusters.items() if len(v) < 2]
    for k in singleton_keys:
        ungrouped.extend(clusters.pop(k))

    # Find centroid-nearest rule for each cluster.
    centroid_indices = _find_centroid_nearest(clusters, rule_ids, embeddings)

    # Compute silhouette score if we have at least 2 clusters.
    sil = -1.0
    if len(clusters) >= 2:
        # Build label array for silhouette (excluding ungrouped).
        cluster_rule_set = {rid for members in clusters.values() for rid in members}
        sil_labels = []
        sil_embeddings = []
        for i, rid in enumerate(rule_ids):
            if rid in cluster_rule_set:
                for cid, members in clusters.items():
                    if rid in members:
                        sil_labels.append(cid)
                        sil_embeddings.append(embeddings[i])
                        break
        if len(set(sil_labels)) >= 2:
            sil = float(silhouette_score(sil_embeddings, sil_labels))

    return ClusterResult(
        clusters=clusters,
        ungrouped=sorted(ungrouped),
        centroid_indices=centroid_indices,
        algorithm=algorithm,
        silhouette=sil,
    )


def _find_centroid_nearest(
    clusters: dict[int, list[str]],
    rule_ids: list[str],
    embeddings: NDArray[np.float32],
) -> dict[int, int]:
    """For each cluster, find the index (in rule_ids) of the rule nearest to centroid."""
    rid_to_idx = {rid: i for i, rid in enumerate(rule_ids)}
    centroid_indices: dict[int, int] = {}

    for cid, members in clusters.items():
        member_indices = [rid_to_idx[rid] for rid in members]
        member_embeds = embeddings[member_indices]
        centroid = member_embeds.mean(axis=0)
        # Find member closest to centroid via cosine distance.
        dists = np.linalg.norm(member_embeds - centroid, axis=1)
        nearest_local = int(np.argmin(dists))
        centroid_indices[cid] = member_indices[nearest_local]

    return centroid_indices
