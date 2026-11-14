"""Prepare the M0-01d human semantic review of the 288 V1 rules in batches.

    python -m writ_m0.audit.rule_review      # writes docs/audit/m0/rule-review/ once

Unlike the drift-checked audit artifacts, these files are meant to be edited by
the reviewer, so the generator refuses to overwrite an existing review directory.
Every review column is left blank: nothing here marks a rule as reviewed.
"""
from __future__ import annotations

import csv
import io
import re
import sys
from collections import defaultdict

from . import cypher, rules, v1source

OUT_DIR = v1source.REPO_ROOT / "docs" / "audit" / "m0" / "rule-review"
MAX_BATCH = 25
CODE_PREFIXES = ("hooks/", "bin/", "writ/")
TEST_PREFIX = "tests/"
MAX_LISTED_PATHS = 5
CONTEXT_EDGES = {"RELATED_TO", "DEPENDS_ON", "SUPPLEMENTS"}
REVIEW_COLUMNS = ["rule_id", "proposed_disposition", "reviewed_disposition", "intent_survives_in_v2",
                  "v2_destination", "enforcement_verified", "conflicts_or_duplicates", "corrections",
                  "reviewer", "reviewed_at", "rationale"]


def _references(rule_ids: list[str]) -> dict[str, dict[str, list[str]]]:
    """Where each rule id is mentioned in V1 code and tests at the pinned baseline."""
    pattern = re.compile("|".join(re.escape(r) for r in sorted(rule_ids, key=len, reverse=True)))
    refs: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"code": [], "tests": []})
    for entry in v1source.list_tree():
        kind = "code" if entry.path.startswith(CODE_PREFIXES) else "tests" if entry.path.startswith(TEST_PREFIX) else None
        if kind is None:
            continue
        text = v1source.read_blob(entry.path).decode("utf-8", "replace")
        for rule_id in set(pattern.findall(text)):
            refs[rule_id][kind].append(entry.path)
    return refs


def _batches(nodes: list[cypher.Node]) -> list[list[cypher.Node]]:
    by_domain: dict[str, list[cypher.Node]] = defaultdict(list)
    for node in sorted(nodes, key=lambda n: (n.props["domain"], n.props["rule_id"])):
        by_domain[node.props["domain"]].append(node)
    chunks = [group[i:i + MAX_BATCH] for _, group in sorted(by_domain.items()) for i in range(0, len(group), MAX_BATCH)]
    merged: list[list[cypher.Node]] = []
    for chunk in chunks:
        if merged and len(merged[-1]) + len(chunk) <= MAX_BATCH:
            merged[-1] = merged[-1] + chunk
        else:
            merged.append(chunk)
    return merged


def build() -> dict[str, str]:
    corpus = cypher.parse_corpus(v1source.read_text(rules.CORPUS))
    nodes = corpus.by_label("Rule")
    rule_ids = {n.props["rule_id"] for n in nodes}
    proposed = {r.rule_id: r for r in rules.classify(corpus, {e.path for e in v1source.list_tree()})}
    refs = _references(sorted(rule_ids))
    related: dict[str, set[str]] = defaultdict(set)
    for edge in corpus.edges:
        if edge.rel in CONTEXT_EDGES and edge.src in rule_ids and edge.dst in rule_ids:
            related[edge.src].add(edge.dst)
            related[edge.dst].add(edge.src)

    files: dict[str, str] = {}
    index_rows = []
    for number, batch in enumerate(_batches(nodes), start=1):
        name = f"batch-{number:02d}"
        domains = sorted({n.props["domain"] for n in batch})
        md = [f"# Rule review {name}: {', '.join(domains)} ({len(batch)} rules)", "",
              f"Source: `writ-corpus.cypher` at `{v1source.V1_BASELINE_SHA}`. Proposed dispositions are the "
              "automated first pass (D-004), not verdicts. Record decisions in "
              f"[{name}.csv]({name}.csv).", ""]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=REVIEW_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for node in batch:
            p = node.props
            rid = p["rule_id"]
            row = proposed[rid]
            code, tests = sorted(refs[rid]["code"]), sorted(refs[rid]["tests"])
            md += [f"## {rid}", "",
                   f"- **Domain / severity / mandatory:** {p['domain']} / {p['severity']} / {str(bool(p.get('mandatory'))).lower()}",
                   f"- **Statement:** {p['statement']}",
                   f"- **Trigger:** {p.get('trigger', '')}",
                   f"- **V1 enforcement text:** {p.get('enforcement', '')}",
                   f"- **V1 mechanical path:** {p.get('mechanical_enforcement_path') or 'none'}"
                   + (f" (resolves at baseline: {row.binding_resolves})" if row.binding_resolves else ""),
                   f"- **Rule id referenced in V1 code:** {len(code)} file(s)"
                   + (": " + ", ".join(f"`{c}`" for c in code[:MAX_LISTED_PATHS]) + (" ..." if len(code) > MAX_LISTED_PATHS else "") if code else ""),
                   f"- **Rule id referenced in V1 tests:** {len(tests)} file(s)",
                   f"- **Related rules (conflict candidates):** {', '.join(sorted(related[rid])) or 'none'}",
                   f"- **Proposed disposition:** {row.disposition} ({row.reason}); pack affinity: {row.pack_affinity}",
                   "- **Reviewer verdict:** _not reviewed_", ""]
            writer.writerow({"rule_id": rid, "proposed_disposition": row.disposition})
        files[f"{name}.md"] = "\n".join(md)
        files[f"{name}.csv"] = buf.getvalue()
        index_rows.append(f"| [{name}]({name}.md) | {', '.join(domains)} | {len(batch)} | 0 | not started |")

    files["README.md"] = "\n".join([
        "# M0-01d: semantic review of the 288 V1 rules", "",
        "**Status:** prepared; **0 of 288 rules reviewed.** Generated once by `python -m writ_m0.audit.rule_review` "
        "(run from `m0/`); edit the CSV files directly. The generator refuses to overwrite this directory.", "",
        "## What to decide per rule", "",
        "- `reviewed_disposition`: advisory, obsolete, duplicate, conflicting, candidate_enforced or unverified.",
        "- `intent_survives_in_v2`: yes/no. Obsolete V1 mechanisms often carry intent worth keeping.",
        "- `v2_destination`: retrieval registry (advisory), a Capability Pack (name it), a policy statement, or discard.",
        "- `enforcement_verified`: yes only if you confirmed a live V1 enforcement point actually blocks; otherwise no or n/a. "
        "A mechanical path or code reference is not proof (finding A-07).",
        "- `conflicts_or_duplicates`: rule ids, checked against the related-rule list and your own reading.",
        "- `reviewer`, `reviewed_at`, `rationale`: required for every reviewed row.", "",
        "Claude must not fill reviewer columns. Batches are domain-grouped, at most 25 rules each.", "",
        "| Batch | Domains | Rules | Reviewed | Status |", "|---|---|---|---|---|", *index_rows, ""])
    return files


def main() -> int:
    if OUT_DIR.exists():
        print(f"Refusing to overwrite {OUT_DIR.relative_to(v1source.REPO_ROOT)}: it may hold review work.", file=sys.stderr)
        return 1
    files = build()
    OUT_DIR.mkdir(parents=True)
    for name, content in files.items():
        (OUT_DIR / name).write_text(content, encoding="utf-8")
    print(f"Wrote {len(files)} files to {OUT_DIR.relative_to(v1source.REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
