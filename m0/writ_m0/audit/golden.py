"""Export V1 retrieval golden sets as language-neutral fixtures with lineage.

V1's benchmarked ranking quality is a preserve item (finding A-09). The V2
retriever (M7) must be measured against the same queries, so they are exported
from the pinned baseline with source digests and checked against the corpus.
"""
from __future__ import annotations

import json

from . import cypher, v1source

SOURCES = {
    "rules": "tests/fixtures/ground_truth_queries.json",
    "abstain": "tests/fixtures/ground_truth_negatives.json",
    "methodology": "tests/fixtures/ground_truth_proc.json",
    "documents": "tests/fixtures/document_queries.json",
}


def build() -> dict:
    corpus = cypher.parse_corpus(v1source.read_text("writ-corpus.cypher"))
    node_ids = {n.dump_id for n in corpus.nodes}
    lineage, cases = [], []
    for name, path in SOURCES.items():
        raw = v1source.read_blob(path)
        lineage.append({"set": name, "path": path, "sha256": v1source.sha256_hex(raw)})
        data = json.loads(raw)
        if name == "rules":
            for q in data["queries"]:
                cases.append({"case_id": f"v1:{q['id']}", "set": f"rules/{q['set']}", "query": q["query"],
                              "expect": "rank", "expected_ids": [q["expected_rule_id"]]})
        elif name == "abstain":
            for q in data["negatives"]:
                cases.append({"case_id": f"v1:{q['id']}", "set": f"abstain/{q['flavor']}", "query": q["query"],
                              "expect": "abstain", "expected_ids": []})
        elif name == "methodology":
            for q in data["queries"]:
                cases.append({"case_id": f"v1:{q['id']}", "set": f"methodology/{q['set']}", "query": q["query"],
                              "expect": "rank", "expected_ids": q["expected_node_ids"]})
        else:
            for q in data["queries"]:
                # Targets V1 docs/adr files, which this branch removed (ADR-0005).
                cases.append({"case_id": f"v1:{q['id']}", "set": "documents", "query": q["prompt"],
                              "expect": "document", "expected_ids": [q["document"]],
                              "note": "depends on V1 docs corpus at baseline"})
    for case in cases:
        if case["expect"] == "rank":
            case["expected_ids_in_corpus"] = all(i in node_ids for i in case["expected_ids"])
    return {
        "description": "V1 retrieval golden sets at the pinned baseline, for M7 quality comparison. Generated.",
        "baseline_commit": v1source.V1_BASELINE_SHA,
        "lineage": lineage,
        "cases": cases,
    }
