"""First-pass disposition of the 288 V1 Rule nodes.

The classification is a deterministic, documented heuristic over corpus
metadata and statement text. It is a triage aid, not a semantic review: every
row carries `review_status=automated_first_pass` until a human (or a verified
review pass, task M0-01d) confirms or corrects it.

Precedence (first match wins):
  1. duplicate           statement identical to an earlier rule after normalization
  2. obsolete            statement names a retired V1 mechanism (see V1_MECHANISM)
  3. candidate_enforced  mechanical_enforcement_path resolves to a file at baseline
  4. unverified          claims enforcement (path or mandatory flag) with no live binding
  5. advisory            everything else: retrieval guidance, never an enforcement claim

Even candidate_enforced rules enter V2 unverified (finding A-07): a V1 binding is
evidence of intent, not a V2 validator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import cypher, v1source

CORPUS = "writ-corpus.cypher"

# Terms that only make sense inside V1 Writ's own workflow machinery. A match
# marks the mechanism retired; the rule's intent may still be re-expressed as a
# Capability Pack workflow step.
V1_MECHANISM = re.compile(
    r"\b(handoff|Work mode|Gate \d|TodoWrite|the Bible|Bible's|plan\.md|session state|"
    r"writ-session|ExitPlanMode|test skeletons?|Tier [12]|citation_log|INV-2)\b",
    re.IGNORECASE,
)
# Domains that encode software-engineering practice; used for pack affinity.
SOFTWARE_DOMAINS = {
    "security", "code-quality", "architecture", "testing", "performance", "api-design",
    "frameworks", "scaling", "languages", "database",
}
NEAR_DUPLICATE_THRESHOLD = 0.6
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class RuleRow:
    rule_id: str
    domain: str
    severity: str
    mandatory: bool
    always_on: bool
    disposition: str
    reason: str
    pack_affinity: str
    binding_path: str
    binding_resolves: str
    duplicate_of: str
    statement_digest: str


def _normalize(statement: str) -> str:
    return " ".join(_WORD.findall(statement.lower()))


def _binding_file(raw: str | None) -> str:
    """Extract the file path from metadata like 'bin/x.sh::fn' or 'a.sh:78 (note)'."""
    if not raw:
        return ""
    head = re.split(r"::| \(| \+ |:\d", raw, maxsplit=1)[0].strip()
    return head


def pack_affinity(domain: str) -> str:
    if domain in SOFTWARE_DOMAINS:
        return "software-engineering"
    if domain == "research":
        return "research-investigation"
    if domain in ("enforcement", "process", "meta-authoring"):
        return "workflow-core"
    return "general"


def classify(corpus: cypher.Corpus, baseline_paths: set[str]) -> list[RuleRow]:
    rules = sorted(corpus.by_label("Rule"), key=lambda n: n.props["rule_id"])
    seen: dict[str, str] = {}
    rows = []
    for node in rules:
        p = node.props
        statement = p["statement"]
        norm = _normalize(statement)
        binding = p.get("mechanical_enforcement_path")
        path = _binding_file(binding)
        resolves = "" if not path else ("yes" if path in baseline_paths else "no")
        duplicate_of = seen.get(norm, "")
        seen.setdefault(norm, p["rule_id"])

        if duplicate_of:
            disposition, reason = "duplicate", f"identical normalized statement to {duplicate_of}"
        elif m := V1_MECHANISM.search(statement):
            disposition, reason = "obsolete", f"names retired V1 mechanism {m.group(0)!r}; intent needs manual review"
        elif resolves == "yes":
            disposition, reason = "candidate_enforced", "V1 binding resolves at baseline; must be rebound to a V2 validator"
        elif resolves == "no":
            disposition, reason = "unverified", f"V1 binding path absent at baseline: {path}"
        elif p.get("mandatory") is True:
            disposition, reason = "unverified", "mandatory flag without any enforcement binding"
        else:
            disposition, reason = "advisory", "guidance only; no enforcement claim"

        rows.append(RuleRow(
            rule_id=p["rule_id"],
            domain=p["domain"],
            severity=p["severity"],
            mandatory=bool(p.get("mandatory")),
            always_on=bool(p.get("always_on")),
            disposition=disposition,
            reason=reason,
            pack_affinity=pack_affinity(p["domain"]),
            binding_path=binding or "",
            binding_resolves=resolves,
            duplicate_of=duplicate_of,
            statement_digest=v1source.sha256_hex(statement.encode("utf-8")),
        ))
    return rows


def near_duplicates(corpus: cypher.Corpus) -> list[tuple[str, str, float]]:
    """Statement pairs with word-set Jaccard >= threshold, for manual review."""
    rules = sorted(corpus.by_label("Rule"), key=lambda n: n.props["rule_id"])
    words = [(r.props["rule_id"], set(_WORD.findall(r.props["statement"].lower()))) for r in rules]
    pairs = []
    for i, (a_id, a) in enumerate(words):
        for b_id, b in words[i + 1:]:
            if not a or not b:
                continue
            score = len(a & b) / len(a | b)
            if score >= NEAR_DUPLICATE_THRESHOLD:
                pairs.append((a_id, b_id, round(score, 3)))
    return pairs


COLUMNS = [
    "rule_id", "domain", "severity", "mandatory", "always_on", "disposition", "reason",
    "pack_affinity", "binding_path", "binding_resolves", "duplicate_of",
    "statement_sha256", "conflict_check", "review_status",
]


def build_rows() -> tuple[list[dict[str, str]], list[tuple[str, str, float]]]:
    corpus = cypher.parse_corpus(v1source.read_text(CORPUS))
    baseline_paths = {e.path for e in v1source.list_tree()}
    rows = [{
        "rule_id": r.rule_id,
        "domain": r.domain,
        "severity": r.severity,
        "mandatory": str(r.mandatory).lower(),
        "always_on": str(r.always_on).lower(),
        "disposition": r.disposition,
        "reason": r.reason,
        "pack_affinity": r.pack_affinity,
        "binding_path": r.binding_path,
        "binding_resolves": r.binding_resolves,
        "duplicate_of": r.duplicate_of,
        "statement_sha256": r.statement_digest,
        "conflict_check": "not_performed",
        "review_status": "automated_first_pass",
    } for r in classify(corpus, baseline_paths)]
    return rows, near_duplicates(corpus)
