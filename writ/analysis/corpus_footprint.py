"""No-API corpus footprint analyzer (WRIT-TOKEN-BLUEPRINT.md lever B / P3 prerequisite).

Static, read-only. Measures the Rule corpus's token FOOTPRINT per rule + per component (bytes/4 floor
estimate -- there is no local Claude tokenizer), surfaces bloat, and RANKS waste cut-candidates.
PROPOSES cuts; never applies them (a content cut changes coverage = efficacy, judged from real-session
evidence before any edit). Reuses the ingest loader so the parse cannot drift from what ingests to the graph.
"""
from __future__ import annotations

from pathlib import Path

from writ.analysis.token_audit import COST_WEIGHTS, render_json  # single source for cost weights + JSON

LABEL = "bytes/4 floor estimate, not a real token count"
_REACH_FIELDS = ("trigger", "applicability_scope", "trigger_keywords")  # never a cut candidate
_BODY_FIELDS = ("violation", "pass_example", "enforcement", "rationale", "edges")

__all__ = [
    "COST_WEIGHTS",
    "CorpusFootprintError",
    "LABEL",
    "always_on_bundle_cost",
    "code_block_share",
    "load_corpus",
    "measure_rule",
    "per_domain_aggregate",
    "rank_cut_candidates",
    "render_json",
    "render_text",
    "scorecard",
]


class CorpusFootprintError(Exception):
    """Malformed/empty corpus -> CLI exit 2 (mirrors token_audit's canary)."""


def _bytes(s) -> int:
    return len(s.encode("utf-8")) if s else 0


def _floor_est(nbytes: int) -> int:
    return nbytes // 4


def _normalize_edges(node: dict) -> None:
    """The ingest loader emits `_declared_edges` (a list) + `_cross_references`, never an `edges`
    string. measure_rule reads the canonical `edges` key (the unit-test contract), so map the
    loader's real edge representation into an `edges` string when `edges` is absent. Keeps both the
    canned-dict tests and the real-corpus run measuring the same bytes."""
    if node.get("edges"):
        return
    declared = node.get("_declared_edges")
    if declared:
        parts = []
        for e in declared:
            if isinstance(e, dict):
                parts.append(" ".join(str(v) for v in e.values() if v))
            else:
                parts.append(str(e))
        node["edges"] = "\n".join(p for p in parts if p)
    else:
        node["edges"] = ""


def load_corpus(bible_dir: str, include_methodology: bool = False) -> list[dict]:
    """Reuse the ingest loader; return only Rule nodes (exclude methodology unless asked)."""
    from writ.graph.ingest import discover_rule_files, parse_nodes_from_file
    rules = []
    for fp in discover_rule_files(Path(bible_dir)):
        if not include_methodology and "methodology" in fp.parts:
            continue
        for node in parse_nodes_from_file(fp):
            if node.get("node_type") == "Rule":
                node["_file"] = str(fp)
                _normalize_edges(node)
                rules.append(node)
    if not rules:
        raise CorpusFootprintError(f"no Rule nodes found under {bible_dir!r}")
    return rules


def measure_rule(rule: dict) -> dict:
    """Per-component bytes + bytes/4 floor est; core (trigger+statement) vs overhead partition."""
    comps = {}
    for field in ("trigger", "statement") + _BODY_FIELDS:
        b = _bytes(rule.get(field))
        comps[field] = {"bytes": b, "tokens_floor_est": _floor_est(b)}
    core = comps["trigger"]["bytes"] + comps["statement"]["bytes"]
    total = sum(c["bytes"] for c in comps.values())
    overhead = total - core
    return {
        "rule_id": rule.get("rule_id", "?"),
        "domain": rule.get("domain", "?"),
        "components": comps,
        "total_bytes": total,
        "total_tokens_floor_est": _floor_est(total),
        "core_bytes": core,
        "overhead_bytes": overhead,
        "overhead_pct": (overhead / total) if total else 0.0,
    }


def always_on_bundle_cost(rules: list[dict], cap: int = 5000) -> dict:
    """The every-turn injection cost: summary render (trigger+statement) of the always_on/mandatory
    set, via the house estimate_tokens, against the budget cap. Mirrors the server's summary render.

    Replicates INJECTION_RULE_WHERE (mandatory or always_on, and not superseded) in Python."""
    from writ.shared.tokens import estimate_tokens
    bundle = [r for r in rules
              if (r.get("mandatory") or r.get("always_on")) and not r.get("superseded")]
    tokens = sum(estimate_tokens(r.get("trigger"), r.get("statement")) for r in bundle)
    return {"rule_count": len(bundle), "tokens_floor_est": tokens, "cap": cap,
            "over_cap": tokens > cap, "basis": LABEL}


def code_block_share(rules: list[dict]) -> dict:
    """Fenced-code bytes inside violation+pass_example as a share of total body bytes (the blueprint's
    ~60%-of-retrieved-cost finding). Reuses the ingest fenced-block regex (single source)."""
    from writ.graph.integrity import _FENCE_RE
    code = body = 0
    for r in rules:
        for field in ("violation", "pass_example"):
            text = r.get(field) or ""
            body += _bytes(text)
            for m in _FENCE_RE.finditer(text):
                code += _bytes(m.group(2))
    return {"code_bytes": code, "body_bytes": body,
            "code_share": (code / body) if body else 0.0}


def per_domain_aggregate(measured: list[dict]) -> dict:
    agg: dict = {}
    for m in measured:
        d = agg.setdefault(m["domain"], {"rule_count": 0, "tokens_floor_est": 0, "overhead_pct_sum": 0.0})
        d["rule_count"] += 1
        d["tokens_floor_est"] += m["total_tokens_floor_est"]
        d["overhead_pct_sum"] += m["overhead_pct"]
    for d in agg.values():
        d["mean_overhead_pct"] = d.pop("overhead_pct_sum") / d["rule_count"]
    return agg


def rank_cut_candidates(measured: list[dict], top: int = 20) -> list[dict]:
    """Rank by overhead_pct * tokens (the bloated-prose waste signal). Tag WASTE vs COVERAGE.
    REACH fields are structurally absent from the score; low retrieval is NOT a signal here."""
    scored = []
    for m in measured:
        worst_field = max(_BODY_FIELDS, key=lambda f: m["components"][f]["bytes"])
        scored.append({
            "rule_id": m["rule_id"], "domain": m["domain"],
            "score": m["overhead_pct"] * m["total_tokens_floor_est"],
            "overhead_pct": m["overhead_pct"], "tokens_floor_est": m["total_tokens_floor_est"],
            "largest_component": worst_field,
            "largest_component_bytes": m["components"][worst_field]["bytes"],
            "tag": "WASTE",  # bloated prose in a body component is proposable waste
        })
    scored.sort(key=lambda x: (-x["score"], x["rule_id"]))  # deterministic
    return scored[:top]


def scorecard(bible_dir: str, top: int = 20, domain: str | None = None,
              include_methodology: bool = False) -> dict:
    rules = load_corpus(bible_dir, include_methodology)
    if domain:
        rules = [r for r in rules if r.get("domain") == domain]
        if not rules:
            raise CorpusFootprintError(f"no rules in domain {domain!r}")
    measured = [measure_rule(r) for r in rules]
    return {
        "rule_count": len(rules),
        "total_tokens_floor_est": sum(m["total_tokens_floor_est"] for m in measured),
        "always_on_bundle": always_on_bundle_cost(rules),
        "code_block_share": code_block_share(rules),
        "per_domain": per_domain_aggregate(measured),
        "cut_candidates": rank_cut_candidates(measured, top),
        "basis": LABEL,
        "measure_only": "Cut candidates are PROPOSALS. Cutting rule content is efficacy-affecting -> "
                        "judge it from real-session evidence before any edit. "
                        "Reach fields are never candidates.",
    }


def render_text(card: dict) -> str:
    """Human table: measure-only banner first, then total + always-on bundle (vs cap) + code share +
    per-domain + the ranked top-N. The bytes/4 label rides the header; every candidate names rule_id,
    tag, score, largest_component."""
    lines: list[str] = []
    lines.append("=== corpus-footprint (PROPOSE-ONLY, measure-only) ===")
    lines.append(f"basis: {card['basis']}")
    lines.append(f"NOTE: {card['measure_only']}")
    lines.append("")
    lines.append(f"rules measured: {card['rule_count']}")
    lines.append(f"total footprint: {card['total_tokens_floor_est']} tokens ({LABEL})")
    lines.append("")

    ab = card["always_on_bundle"]
    flag = "OVER CAP" if ab["over_cap"] else "within cap"
    lines.append(
        f"always-on bundle (mandatory OR always_on): {ab['rule_count']} rules, "
        f"{ab['tokens_floor_est']} tokens vs cap {ab['cap']} [{flag}]"
    )

    cs = card["code_block_share"]
    lines.append(
        f"code-block share (violation+pass): {cs['code_bytes']}/{cs['body_bytes']} body bytes = "
        f"{cs['code_share'] * 100:.1f}%"
    )
    lines.append("")

    lines.append("per-domain (rules / tokens / mean overhead%):")
    for dom in sorted(card["per_domain"]):
        d = card["per_domain"][dom]
        lines.append(
            f"  {dom:<24} {d['rule_count']:>4} rules  {d['tokens_floor_est']:>7} tok  "
            f"{d['mean_overhead_pct'] * 100:>5.1f}% overhead"
        )
    lines.append("")

    cands = card["cut_candidates"]
    lines.append(f"top {len(cands)} bloat cut-candidates (rank = overhead% x tokens; WASTE = proposable):")
    lines.append(f"  {'rank':>4} {'rule_id':<22} {'tag':<8} {'score':>9} {'tok':>6} "
                 f"{'ovr%':>6}  largest_component")
    for i, c in enumerate(cands, 1):
        lines.append(
            f"  {i:>4} {c['rule_id']:<22} {c['tag']:<8} {c['score']:>9.1f} "
            f"{c['tokens_floor_est']:>6} {c['overhead_pct'] * 100:>5.1f}%  "
            f"{c['largest_component']} ({c['largest_component_bytes']}B)"
        )
    return "\n".join(lines)
