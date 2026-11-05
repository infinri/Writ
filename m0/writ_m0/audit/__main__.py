"""Generate or verify the M0 V1 baseline audit artifacts.

    python -m writ_m0.audit generate   # rewrite docs/audit/m0/*
    python -m writ_m0.audit check      # exit 1 if any artifact is stale

Outputs are deterministic (sorted, no wall-clock timestamps) so `check` can run
in CI and detect drift between the generators and the committed evidence.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from collections import Counter
from pathlib import Path

from . import baseline, hooks, rules, v1source

OUT_DIR = v1source.REPO_ROOT / "docs" / "audit" / "m0"


def _csv(columns: list[str], rows: list[dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _json(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _summary(hook_rows, rule_rows, near_dups, corpus, tree, tests) -> str:
    cls = Counter(r["coverage_class"] for r in hook_rows)
    events = Counter(r["event"] for r in hook_rows)
    findings = Counter(f for r in hook_rows for f in r["findings"].split())
    disp = Counter(r["disposition"] for r in rule_rows)
    topics = Counter(t for r in tests for t in r["topics"].split())
    lines = [
        "# M0 V1 baseline audit summary",
        "",
        "**Generated** by `python -m writ_m0.audit generate` (run from `m0/`). Do not edit by hand;",
        "`python -m writ_m0.audit check` fails when this file drifts from the generators.",
        "",
        f"**Baseline commit:** `{tree['baseline']['commit']}` (committed {tree['baseline']['committed_at']}).",
        "All inputs are read from Git objects at that commit, not from the working tree.",
        "",
        "## Evidence status",
        "",
        "Every figure below is a static, reproducible observation of the baseline. None is a",
        "runtime observation of a live Claude Code host. Host lifecycle semantics are taken from",
        "the documented hook contract and still need isolated host tests (M0-01e). Rule",
        "dispositions are an automated first pass awaiting semantic review (M0-01d).",
        "",
        "## Tree",
        "",
        f"- Tracked files: {tree['tracked_files']}",
        f"- Hook scripts checksummed: {sum(1 for p in tree['evidence_sha256'] if p.startswith('hooks/scripts/'))}",
        "",
        "## Hook registrations ([CSV](hook-registrations.csv))",
        "",
        f"- Registrations: {len(hook_rows)} across {len(events)} lifecycle events",
        f"- Distinct scripts registered: {len({r['script'] for r in hook_rows})}",
        "",
        "| Coverage class | Count |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in sorted(cls.items())],
        "",
        "| Mechanical finding | Registrations |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in sorted(findings.items())],
        "",
        "Pre-effect registrations that consult the daemon default to allow when it is unreachable",
        "(unless `WRIT_STRICT=1`): "
        + ", ".join(r["reg_id"] for r in hook_rows if "fail_open_default" in r["failure_posture_static"]) + ".",
        "",
        "## Rule corpus ([manifest](v1-corpus-manifest.json), [dispositions](rule-disposition.csv))",
        "",
        f"- Nodes: {corpus['nodes_total']}; edges: {corpus['edges_total']}; dangling edges: {corpus['dangling_edges']}",
        f"- Rules: {corpus['rules']['count']} (mandatory {corpus['rules']['mandatory']}, "
        f"with mechanical_enforcement_path {corpus['rules']['with_mechanical_enforcement_path']})",
        "",
        "| Disposition (first pass) | Rules |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in sorted(disp.items())],
        "",
        f"- Near-duplicate statement pairs (word Jaccard >= {rules.NEAR_DUPLICATE_THRESHOLD}): {len(near_dups)}"
        " ([CSV](rule-near-duplicates.csv))",
        "- Conflict analysis: not performed (requires semantic review).",
        "",
        "## V1 tests and benchmarks ([CSV](v1-test-inventory.csv))",
        "",
        f"- Python test/benchmark files: {len(tests)}; test functions: {sum(int(r['test_functions']) for r in tests)}",
        "",
        "| Topic tag (file name match) | Files |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in sorted(topics.items())],
        "",
    ]
    return "\n".join(lines)


def build() -> dict[str, str]:
    hook_rows = hooks.build_rows()
    rule_rows, near_dups = rules.build_rows()
    corpus = baseline.corpus_manifest()
    tree = baseline.tree_manifest()
    tests = baseline.test_inventory()
    return {
        "hook-registrations.csv": _csv(hooks.COLUMNS, hook_rows),
        "rule-disposition.csv": _csv(rules.COLUMNS, rule_rows),
        "rule-near-duplicates.csv": _csv(
            ["rule_a", "rule_b", "jaccard"],
            [{"rule_a": a, "rule_b": b, "jaccard": str(s)} for a, b, s in near_dups]),
        "v1-corpus-manifest.json": _json(corpus),
        "v1-baseline-manifest.json": _json(tree),
        "v1-test-inventory.csv": _csv(baseline.TEST_COLUMNS, tests),
        "SUMMARY.md": _summary(hook_rows, rule_rows, near_dups, corpus, tree, tests),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("generate", "check"):
        print(__doc__, file=sys.stderr)
        return 2
    outputs = build()
    if argv[0] == "generate":
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for name, content in outputs.items():
            (OUT_DIR / name).write_text(content, encoding="utf-8")
        print(f"Wrote {len(outputs)} artifacts to {OUT_DIR.relative_to(v1source.REPO_ROOT)}")
        return 0
    stale = [n for n, c in outputs.items()
             if not (OUT_DIR / n).is_file() or (OUT_DIR / n).read_text(encoding="utf-8") != c]
    for name in stale:
        print(f"STALE: docs/audit/m0/{name}", file=sys.stderr)
    if stale:
        return 1
    print(f"M0 audit artifacts current: {len(outputs)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
