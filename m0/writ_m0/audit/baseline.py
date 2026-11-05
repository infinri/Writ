"""Checksummed manifests of the pinned V1 baseline: tree, corpus and tests."""
from __future__ import annotations

import re
from collections import Counter

from . import cypher, v1source

# Topic tags for V1 test files, matched against the file name. They select
# candidates for golden-fixture mining (M0-01f); they are not a coverage claim.
TEST_TOPICS = {
    "approval": re.compile(r"approv|grant|token"),
    "isolation": re.compile(r"isolat|tenant|cross_project|project_scope|scope"),
    "fail_open": re.compile(r"fail_open|fallback|outage|degrad|strict"),
    "retrieval": re.compile(r"retriev|ranking|rank|bm25|vector|hnsw|query|abstain|abstention"),
    "concurrency": re.compile(r"race|concurren|lock|atomic|idempot"),
    "path_safety": re.compile(r"symlink|path|traversal|canonical"),
    "bash_gate": re.compile(r"bash"),
    "hooks": re.compile(r"hook|gate"),
}
_TEST_DEF = re.compile(r"^\s*(async\s+)?def (test_\w+)", re.MULTILINE)
_MARKER = re.compile(r"pytest\.mark\.(\w+)")
EVIDENCE_FILES = ["hooks/hooks.json", "writ-corpus.cypher", "bin/lib/common.sh", "pyproject.toml"]


def tree_manifest() -> dict:
    entries = v1source.list_tree()
    top = Counter(e.path.split("/", 1)[0] if "/" in e.path else "(root)" for e in entries)
    evidence = {}
    for path in EVIDENCE_FILES + sorted(e.path for e in entries if e.path.startswith("hooks/scripts/")):
        evidence[path] = v1source.sha256_hex(v1source.read_blob(path))
    return {
        "baseline": v1source.commit_metadata(),
        "tracked_files": len(entries),
        "tracked_bytes": sum(e.size for e in entries),
        "files_by_top_level": dict(sorted(top.items())),
        "evidence_sha256": evidence,
    }


def corpus_manifest() -> dict:
    data = v1source.read_blob("writ-corpus.cypher")
    corpus = cypher.parse_corpus(data.decode("utf-8"))
    ids = {n.dump_id for n in corpus.nodes}
    rules = corpus.by_label("Rule")
    rule_ids = sorted(r.props["rule_id"] for r in rules)
    return {
        "source": "writ-corpus.cypher",
        "baseline_commit": v1source.V1_BASELINE_SHA,
        "sha256": v1source.sha256_hex(data),
        "nodes_total": len(corpus.nodes),
        "nodes_by_label": dict(sorted(Counter(n.label for n in corpus.nodes).items())),
        "edges_total": len(corpus.edges),
        "edges_by_type": dict(sorted(Counter(e.rel for e in corpus.edges).items())),
        "dangling_edges": sum(1 for e in corpus.edges if e.src not in ids or e.dst not in ids),
        "duplicate_node_ids": len(corpus.nodes) - len(ids),
        "rules": {
            "count": len(rules),
            "mandatory": sum(1 for r in rules if r.props.get("mandatory") is True),
            "always_on": sum(1 for r in rules if r.props.get("always_on") is True),
            "with_mechanical_enforcement_path": sum(1 for r in rules if r.props.get("mechanical_enforcement_path")),
            "by_domain": dict(sorted(Counter(r.props["domain"] for r in rules).items())),
            "by_severity": dict(sorted(Counter(r.props["severity"] for r in rules).items())),
            "rule_ids_sha256": v1source.sha256_hex("\n".join(rule_ids).encode()),
        },
    }


TEST_COLUMNS = ["path", "sha256", "test_functions", "markers", "topics"]


def test_inventory() -> list[dict[str, str]]:
    rows = []
    for entry in v1source.list_tree():
        name = entry.path.rsplit("/", 1)[-1]
        if not (entry.path.startswith(("tests/", "benchmarks/")) and name.endswith(".py")):
            continue
        data = v1source.read_blob(entry.path)
        text = data.decode("utf-8", "replace")
        tests = _TEST_DEF.findall(text)
        if not tests and not entry.path.startswith("benchmarks/"):
            continue
        lowered = entry.path.lower()
        rows.append({
            "path": entry.path,
            "sha256": v1source.sha256_hex(data),
            "test_functions": str(len(tests)),
            "markers": " ".join(sorted(set(_MARKER.findall(text)) - {"parametrize"})),
            "topics": " ".join(t for t, rx in TEST_TOPICS.items() if rx.search(lowered)),
        })
    return rows
