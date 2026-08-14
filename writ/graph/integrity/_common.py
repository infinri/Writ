"""Shared surface for the integrity check mixins (Wave 2 split).

Holds the module-level imports/re-exports from the lower graph layers
(writ.graph.db / .ingest / .methodology_ingest / .predicates / .schema), the
shared constants (EXPECTED_FLOORS, KNOWN_ACTIONS, _FENCE_RE, _ALWAYS_ON_CAP, ...),
and the pure helpers lint_rule_examples / _normalized_code_blocks / _python_blocks.
The check methods that use these live in the per-domain *_checks.py mixins; the
`__all__` below marks the names those mixins and the facade pull from here.

Per ARCH-DI-001: IntegrityChecker receives its db connection via constructor injection.
Per PY-ASYNC-001: all check operations are async.
"""

from __future__ import annotations

import ast
import re
from datetime import date, timedelta
from pathlib import Path

from writ.graph.db import (
    _GRAPH_ID_COALESCE,
    _coerce_neo4j_value,
    read_live_edges,
    read_live_nodes_with_keys,
)
from writ.shared.tokens import estimate_tokens
from writ.graph.ingest import parse_nodes_from_file, parse_rules_from_file
from writ.graph.methodology_ingest import (
    ORACLE_BLIND_LABELS,
    compute_expected_graph,
    expected_managed_props,
    parse_source,
    read_oracle_blind_node_ids,
)
from writ.graph.predicates import INJECTION_RULE_WHERE, RANKED_INCLUDE_WHERE
from writ.graph.schema import (
    MANAGED_PROP_NAMES,
    NODE_ID_FIELDS,
    PARITY_EXEMPT_PROVENANCE,
    REDUNDANCY_SIMILARITY_THRESHOLD,
    VALID_DOMAINS,
    WIRED_ROUTES,
)

# The always-on injection budget cap (mirrors server.py:/always-on and
# session/config DEFAULT_ALWAYS_ON_CAP). Kept a literal here to avoid importing
# the session layer into the graph layer.
_ALWAYS_ON_CAP = 5000

# Invariant A (1.7): the Appendix-B methodology mode-floor fixture. `floor_modes`
# (node-declared) is the RUNTIME source; this central map is the completeness
# check a missing tag would not otherwise trip (distributed data, centrally
# checkable -- the 0.3 category-reachability move applied to floor membership).
# Methodology nodes only: the rule/FRB floor members (ENF-COMMS-001, FRB-COMMS-*)
# stay CHANNEL-1 always_on per D1 and are not floor_modes-routed.
_UNIVERSAL_FLOOR = {
    "SKL-PROC-BRAIN-001", "SKL-PROC-PLAN-001", "SKL-PROC-VERIFY-001", "PBK-PROC-PLAN-001",
    # The one CRITICAL-severity AntiPattern in CAT-DISC-001: its counter
    # (SKL-PROC-VERIFY-001) is already universal, so keying the warning to a typed
    # keyword while the remedy is unconditional inverted the pair. Floored in the
    # same five modes as the counter; its trigger_keywords stay as the pull channel.
    "ANT-PROC-VERIFY-001",
}
EXPECTED_FLOORS: dict[str, set[str]] = {
    "conversation": _UNIVERSAL_FLOOR | {"SKL-PROC-METHODOLOGY-CHECK-001", "SKL-PROC-MODE-001"},
    "debug": _UNIVERSAL_FLOOR | {
        "PBK-PROC-DEBUG-001", "SKL-PROC-INVESTIGATE-001",
        "TEC-PROC-ROOTCAUSE-001", "TEC-PROC-HYPOTHESIS-001",
    },
    "review": _UNIVERSAL_FLOOR | {"SKL-PROC-REVRECV-001"},
    "work": _UNIVERSAL_FLOOR | {
        "PBK-PROC-WORK-WORKFLOW-001", "SKL-PROC-MODE-001",
        "SKL-PROC-WRIT-FAILURE-001", "PBK-PROC-TDD-001",
    },
    "investigate": _UNIVERSAL_FLOOR | {
        "SKL-PROC-INVESTIGATE-001", "PBK-PROC-RESEARCH-001",
        "PBK-PROC-AUDIT-FANOUT-001", "TEC-PROC-SOURCE-EVAL-001",
    },
}
_FLOOR_NODE_LABELS = "['Skill','Playbook','Technique','AntiPattern']"

# 1.8: the action vocabulary the live push path can emit (the wired actions, D-B).
# The action analog of EXPECTED_FLOORS' mode set: an `action_triggers` value
# outside this set tags a node no push can ever reach -- a silent no-op gate, the
# 29-stranded-mandatory failure class. 'dispatch' is the already-wired reference;
# 'plan' is absent (PLAN nodes are always_on-floored everywhere -> a plan push is
# a pure no-op, dropped at design). 'Stop' was dropped likewise: its only pusher
# (SKL-PROC-VERIFY-001) is always_on + work-floored, so the Stop re-push was a
# redundant no-op that current Claude Code treated as a turn-block loop.
KNOWN_ACTIONS: frozenset[str] = frozenset({
    "dispatch", "gate-denial", "review-feedback", "worktree",
    "bible-authoring", "finish",
})

# 3.2: example lint. Every ```python block in a rule's Violation/Pass section
# must parse, and a PASS example (normative "do this" code) must not teach
# deprecated Pydantic-v1 API on the current stack (Pydantic 2.x). Violation
# blocks are exempt from the deprecated-API scan: they may show old/bad API to
# illustrate the anti-pattern. The check reads the already-sectioned
# `violation`/`pass_example` fields, so PASS-scoping is structural, not a
# re-parse of markdown. Two evidence-backed checks were DROPPED (2026-06-14):
# prose-only blocks (37, a deliberate authoring style -- comments parse fine)
# and {placeholder}-in-string (over-fires on framework route patterns).
_FENCE_RE = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
_PY_FENCE_LANGS = frozenset({"python", "py"})
# Pydantic-v1 idioms with a clean v2 replacement (verified deprecated on 2.12.5;
# a closed allowlist -- new deprecations are added deliberately, not inferred).
_DEPRECATED_PASS_API: dict[str, str] = {
    "from_orm(": "Pydantic v1 from_orm -> model_validate(obj, from_attributes=True)",
    ".dict()": "Pydantic v1 .dict() -> .model_dump()",
    ".parse_obj(": "Pydantic v1 parse_obj -> model_validate",
    "@validator": "Pydantic v1 @validator -> @field_validator",
    "constr(": "Pydantic v1 constr() -> Field(..., max_length=)/Annotated",
    "conint(": "Pydantic v1 conint() -> Field(..., ge=/le=)/Annotated",
    "confloat(": "Pydantic v1 confloat() -> Field(..., ge=/le=)/Annotated",
}


def resolve_parity_oracle(
    bible_dir: Path | None, default_bible_dir: Path | None
) -> tuple[Path | None, str | None]:
    """Resolve the markdown corpus the four parity detectors compare against.

    Returns (dir_to_use, skip_reason). Exactly one is non-None: a usable oracle,
    or the reason there is none. The empty-corpus half is the load-bearing one.
    reconcile already refuses an empty oracle ("refusing to reconcile against an
    empty oracle ... this would delete the graph"); the parity detectors had no
    such guard, so pointing them at an absent or empty bible/ would report every
    live node and edge as drift and recommend reconcile for all of it.
    """
    resolved = bible_dir if bible_dir is not None else default_bible_dir
    if resolved is None:
        return None, "no markdown corpus configured (pass --bible-dir)"
    path = Path(resolved)
    if not path.exists():
        return None, f"markdown corpus {path} does not exist"
    if not any(path.rglob("*.md")):
        return None, f"markdown corpus {path} contains no *.md files"
    return path, None


def _python_blocks(section: str | None) -> list[str]:
    """The code of every ```python (or ```py) fenced block in a section."""
    if not section:
        return []
    return [
        m.group(2)
        for m in _FENCE_RE.finditer(section)
        if (m.group(1) or "").lower() in _PY_FENCE_LANGS
    ]


def _normalized_code_blocks(section: str | None, min_len: int = 40) -> list[str]:
    """Whitespace-normalized fenced code blocks (any language) >= min_len chars.

    Used by detect_shared_code_example (3.3): two rules carrying the same
    normalized block are a dedup signal the cosine-0.95 gate misses.
    """
    if not section:
        return []
    out: list[str] = []
    for m in _FENCE_RE.finditer(section):
        code = re.sub(r"\s+", " ", m.group(2)).strip()
        if len(code) >= min_len:
            out.append(code)
    return out


def lint_rule_examples(
    rule_id: str, violation: str | None, pass_example: str | None
) -> list[dict]:
    """Lint one rule's code examples. Pure (no graph); returns findings.

    - syntax: a ```python block (in either field) that fails ast.parse.
    - deprecated_api: a ```python PASS block containing a deprecated-v1 token.
    Each finding: {rule_id, field, kind, detail}.
    """
    findings: list[dict] = []
    for field, section in (("violation", violation), ("pass_example", pass_example)):
        for code in _python_blocks(section):
            try:
                ast.parse(code)
            except SyntaxError as exc:
                findings.append({
                    "rule_id": rule_id, "field": field, "kind": "syntax",
                    "detail": f"{exc.msg} (line {exc.lineno})",
                })
                continue
            if field == "pass_example":
                for token, why in _DEPRECATED_PASS_API.items():
                    if token in code:
                        findings.append({
                            "rule_id": rule_id, "field": field,
                            "kind": "deprecated_api", "detail": why,
                        })
    return findings

__all__ = [
    "EXPECTED_FLOORS",
    "INJECTION_RULE_WHERE",
    "KNOWN_ACTIONS",
    "MANAGED_PROP_NAMES",
    "NODE_ID_FIELDS",
    "ORACLE_BLIND_LABELS",
    "PARITY_EXEMPT_PROVENANCE",
    "RANKED_INCLUDE_WHERE",
    "REDUNDANCY_SIMILARITY_THRESHOLD",
    "VALID_DOMAINS",
    "WIRED_ROUTES",
    "_ALWAYS_ON_CAP",
    "_DEPRECATED_PASS_API",
    "_FENCE_RE",
    "_FLOOR_NODE_LABELS",
    "_GRAPH_ID_COALESCE",
    "_PY_FENCE_LANGS",
    "_UNIVERSAL_FLOOR",
    "_coerce_neo4j_value",
    "_normalized_code_blocks",
    "_python_blocks",
    "compute_expected_graph",
    "date",
    "estimate_tokens",
    "expected_managed_props",
    "lint_rule_examples",
    "parse_nodes_from_file",
    "parse_rules_from_file",
    "parse_source",
    "read_live_edges",
    "read_live_nodes_with_keys",
    "read_oracle_blind_node_ids",
    "resolve_parity_oracle",
    "timedelta",
]
