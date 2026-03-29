"""Abstraction node generation from rule clusters.

Per ARCH-SSOT-001: Abstraction nodes stored in Neo4j are the canonical source.
Per INV-SUMMARY: summary = statement of rule nearest to cluster centroid.
No LLM dependency. Deterministic and offline.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from writ.compression.clusters import ClusterResult
    from writ.graph.db import Neo4jConnection

# Per ARCH-CONST-001
ABSTRACTION_ID_PREFIX = "ABS"
APPROX_TOKENS_PER_CHAR = 0.25  # conservative estimate for English text


def generate_abstractions(
    cluster_result: ClusterResult,
    rules: list[dict],
) -> list[dict]:
    """Generate Abstraction dicts from cluster result.

    Each abstraction has: abstraction_id, summary, rule_ids, domain, compression_ratio.
    Summary is the statement of the rule nearest to the cluster centroid (INV-SUMMARY).
    """
    rid_to_rule = {r["rule_id"]: r for r in rules}
    rule_ids_list = [r["rule_id"] for r in rules]
    abstractions: list[dict] = []

    for cid, member_ids in sorted(cluster_result.clusters.items()):
        centroid_idx = cluster_result.centroid_indices.get(cid)
        if centroid_idx is None or centroid_idx >= len(rule_ids_list):
            continue

        centroid_rule_id = rule_ids_list[centroid_idx]
        centroid_rule = rid_to_rule.get(centroid_rule_id, {})
        summary = centroid_rule.get("statement", "")

        domain = _derive_domain(member_ids, rid_to_rule)
        compression_ratio = _compute_compression_ratio(member_ids, rid_to_rule, summary)
        abs_id = f"{ABSTRACTION_ID_PREFIX}-{domain.upper().replace(' ', '-')}-{cid:03d}"

        abstractions.append({
            "abstraction_id": abs_id,
            "summary": summary,
            "rule_ids": sorted(member_ids),
            "domain": domain,
            "compression_ratio": round(compression_ratio, 2),
        })

    return abstractions


async def write_abstractions_to_graph(
    db: Neo4jConnection,
    abstractions: list[dict],
) -> int:
    """Write Abstraction nodes and ABSTRACTS edges to Neo4j.

    Deletes existing abstractions first for clean recompression (INV-IDEMPOTENT).
    Returns count of abstractions written.
    """
    await db.delete_abstractions()

    for abst in abstractions:
        node_data = {
            "abstraction_id": abst["abstraction_id"],
            "summary": abst["summary"],
            "domain": abst["domain"],
            "compression_ratio": abst["compression_ratio"],
            "rule_count": len(abst["rule_ids"]),
        }
        await db.create_abstraction(node_data)
        for rid in abst["rule_ids"]:
            await db.create_abstracts_edge(abst["abstraction_id"], rid)

    return len(abstractions)


def _derive_domain(member_ids: list[str], rid_to_rule: dict[str, dict]) -> str:
    """Most common domain among cluster members."""
    domains = [rid_to_rule.get(rid, {}).get("domain", "Unknown") for rid in member_ids]
    if not domains:
        return "Unknown"
    counter = Counter(domains)
    return counter.most_common(1)[0][0]


def _compute_compression_ratio(
    member_ids: list[str],
    rid_to_rule: dict[str, dict],
    summary: str,
) -> float:
    """Ratio of total member text tokens to summary tokens."""
    member_tokens = 0
    for rid in member_ids:
        rule = rid_to_rule.get(rid, {})
        text = f"{rule.get('statement', '')} {rule.get('trigger', '')}"
        member_tokens += len(text) * APPROX_TOKENS_PER_CHAR

    summary_tokens = max(len(summary) * APPROX_TOKENS_PER_CHAR, 1)
    return member_tokens / summary_tokens
