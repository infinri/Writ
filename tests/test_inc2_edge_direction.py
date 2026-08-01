"""INC-2: directed-edge semantic normalization (NRV-1/2).

The schema (writ/graph/schema.py) defines directed edge semantics as the design contract,
but the corpus had drifted to the mirror-opposite direction for ~43 edges, plus a mis-typed
ForbiddenResponse<->Rule GATES cluster (with a bidirectional cycle), plus a missing
ANT-PROC-FINISH-001 referenced in prose. This pins the corrected, canonical model so it
cannot drift back.

All assertions are pure (parse the corpus from disk); they always run.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from writ.graph.ingest import (


    NODE_ID_FIELDS,
    parse_edges_from_file,
    parse_nodes_from_file,
    validate_parsed_node,
)

from tests._bible_guard import requires_bible

pytestmark = requires_bible


WRIT_ROOT = Path(__file__).resolve().parent.parent
METH = WRIT_ROOT / "bible" / "methodology"

# Canonical (source-types -> target-types) per directed edge type -- the truthful contract.
CANONICAL = {
    "GATES": ({"Rule"}, {"Skill", "Playbook"}),
    "TEACHES": ({"Skill", "Playbook", "Technique"}, {"Rule", "Skill", "Playbook", "Technique"}),
    "COUNTERS": ({"AntiPattern", "Rationalization"}, {"Skill", "Playbook", "Rule"}),
    "DEMONSTRATES": (
        {"WorkedExample", "ForbiddenResponse", "Technique", "SubagentRole", "Skill"},
        {"Skill", "Rule", "Playbook", "Technique", "PressureScenario"},
    ),
    "DISPATCHES": ({"Playbook", "Skill"}, {"SubagentRole", "Technique", "Playbook", "Skill"}),
    "CONTAINS": ({"Playbook"}, {"Phase"}),
    "PRESSURE_TESTS": ({"PressureScenario"}, {"Rule", "Skill", "Playbook"}),
    "ATTACHED_TO": ({"Rationalization"}, {"Skill", "Playbook", "Rule"}),
}

# The exact reversed patterns INC-2 eliminates (type, source_type, target_type).
FORBIDDEN_INVERSIONS = {
    ("GATES", "Playbook", "Rule"), ("GATES", "Skill", "Rule"),
    ("TEACHES", "Rule", "Skill"), ("TEACHES", "Rule", "Playbook"),
    ("COUNTERS", "Rule", "AntiPattern"), ("COUNTERS", "Playbook", "AntiPattern"),
    ("DISPATCHES", "SubagentRole", "Playbook"),
    ("DEMONSTRATES", "Skill", "ForbiddenResponse"),
}

_RULE_PREFIXES = {
    "API", "ARCH", "CLEAN", "DRY", "DOC", "ERR", "PERF", "PROC", "PY", "PHP",
    "FW", "SEC", "SOLID", "SCALE", "TEST", "DB", "RESEARCH", "ENF", "RUL",
}


def _node_types() -> dict[str, str]:
    typ: dict[str, str] = {}
    for f in METH.glob("*.md"):
        for n in parse_nodes_from_file(f):
            nt = n.get("node_type", "Rule")
            idf = NODE_ID_FIELDS.get(nt)
            if idf and n.get(idf):
                typ[n[idf]] = nt
    return typ


def _kind(node_id: str, typ: dict[str, str]) -> str:
    if node_id in typ:
        return typ[node_id]
    return "Rule" if node_id.split("-")[0] in _RULE_PREFIXES else f"?{node_id.split('-')[0]}"


def _all_edges() -> list[dict]:
    edges: list[dict] = []
    for f in sorted(METH.glob("*.md")):
        edges.extend(parse_edges_from_file(f))
    return edges


# --- 1. No known-wrong inversions remain -------------------------------------


class TestNoInversions:
    def test_no_reversed_edges(self) -> None:
        typ = _node_types()
        offenders = []
        for e in _all_edges():
            pat = (e.get("type"), _kind(e.get("source", ""), typ), _kind(e.get("target", ""), typ))
            if pat in FORBIDDEN_INVERSIONS:
                offenders.append(f"{e['source']} --{e['type']}--> {e['target']}")
        assert not offenders, "reversed edges still present:\n" + "\n".join(offenders[:60])


# --- 2. FRB GATES re-typed + no same-type cycle ------------------------------


class TestNoMistypeOrCycle:
    def test_no_forbidden_response_in_gates(self) -> None:
        typ = _node_types()
        bad = [
            f"{e['source']} GATES {e['target']}"
            for e in _all_edges()
            if e.get("type") == "GATES"
            and "ForbiddenResponse" in (_kind(e.get("source", ""), typ), _kind(e.get("target", ""), typ))
        ]
        assert not bad, "GATES must not involve a ForbiddenResponse (re-type to DEMONSTRATES):\n" + "\n".join(bad)

    def test_no_bidirectional_same_type_pair(self) -> None:
        seen = defaultdict(set)  # type -> set of (a,b) frozensets seen directionally
        cycles = []
        directed = set()
        for e in _all_edges():
            t = e.get("type"); s = e.get("source", ""); d = e.get("target", "")
            if (t, d, s) in directed:
                cycles.append(f"{s} <--{t}--> {d}")
            directed.add((t, s, d))
        assert not cycles, "contradictory bidirectional same-type edge pairs:\n" + "\n".join(cycles)


# --- 3. ANT-PROC-FINISH-001 exists and is wired ------------------------------


class TestFinishAntiPattern:
    def test_node_exists_and_valid(self) -> None:
        path = METH / "ANT-PROC-FINISH-001.md"
        assert path.exists(), "ANT-PROC-FINISH-001.md is missing"
        nodes = parse_nodes_from_file(path)
        assert len(nodes) == 1 and nodes[0]["node_type"] == "AntiPattern"
        try:
            validate_parsed_node(nodes[0])
        except (ValueError, ValidationError) as e:
            pytest.fail(f"ANT-PROC-FINISH-001 invalid: {e}")

    def test_counters_finish_playbook(self) -> None:
        edges = parse_edges_from_file(METH / "ANT-PROC-FINISH-001.md")
        assert any(
            e.get("type") == "COUNTERS" and e.get("target") == "PBK-PROC-FINISH-001"
            for e in edges
        ), "ANT-PROC-FINISH-001 must COUNTERS PBK-PROC-FINISH-001"


# --- 4. Canonical-direction conformance --------------------------------------


class TestCanonicalConformance:
    def test_every_governed_edge_conforms(self) -> None:
        typ = _node_types()
        violations = []
        for e in _all_edges():
            t = e.get("type")
            if t not in CANONICAL:
                continue
            src_ok, tgt_ok = CANONICAL[t]
            ks = _kind(e.get("source", ""), typ)
            kt = _kind(e.get("target", ""), typ)
            if ks not in src_ok or kt not in tgt_ok:
                violations.append(f"{e['source']}({ks}) --{t}--> {e['target']}({kt})")
        assert not violations, "edges outside the canonical endpoint spec:\n" + "\n".join(violations[:60])


# --- 5. No dangling edge targets (extends the FIX-6 guard) -------------------


class TestNoDanglingEdges:
    def test_every_edge_target_resolves(self) -> None:
        typ = _node_types()
        known = set(typ)
        # rule ids living outside methodology/ are also valid targets
        bad = []
        for e in _all_edges():
            for end in (e.get("source", ""), e.get("target", "")):
                if end in known:
                    continue
                if end.split("-")[0] in _RULE_PREFIXES:
                    continue  # a real rule id (rules live in bible/<domain>/)
                bad.append(f"{e.get('type')}: {end}")
        assert not bad, "edges referencing non-existent nodes:\n" + "\n".join(sorted(set(bad)))
