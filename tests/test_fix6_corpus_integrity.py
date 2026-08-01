"""FIX-6: full corpus integrity + edge densification (audit finding #2).

Two guarantees:
  1. Formatting lock (regression guard, green from the start): every rule block in
     the bible parses, validates, and carries all required fields; every methodology
     node validates through its Pydantic model.
  2. Edge densification: the orphan rule count drops from 223 to <= 20 and the lone
     methodology orphan (SKL-PROC-MODE-001) is connected, WITHOUT inventing edges --
     every RELATED_TO derives from a real rule-ID cross-reference in the source block,
     and every front-matter edge target resolves to a known node.

The orphan computation here is faithful to writ.graph.methodology_ingest: RELATED_TO
edges come only from a Rule node's `_cross_references` that point at other rules; typed
edges (GATES/TEACHES/COUNTERS/PRECEDES/...) come from front-matter `edges:` lists. So a
rule is connected iff it references another rule, is referenced by one, or is the
endpoint of a front-matter edge. This is deterministic from the filesystem (always runs);
TestLiveGraphCrossCheck confirms the live Neo4j graph agrees.
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
    parse_rules_from_file,
    validate_parsed_node,
    validate_parsed_rule,
)

from tests._bible_guard import requires_bible

pytestmark = requires_bible


WRIT_ROOT = Path(__file__).resolve().parent.parent
BIBLE = WRIT_ROOT / "bible"
METHODOLOGY = BIBLE / "methodology"

REQUIRED_RULE_FIELDS = [
    "rule_id", "domain", "severity", "scope", "trigger", "statement",
    "violation", "pass_example", "enforcement", "rationale",
]
ORPHAN_CEILING = 20
LONE_METHODOLOGY_ORPHAN = "SKL-PROC-MODE-001"

# Tokens that match CROSS_REF_PATTERN but are NOT references to corpus nodes: gate
# denial-message labels and intentional authoring-example placeholders. These create no
# edge and are excluded from the dead-reference check.
CROSS_REF_ALLOWLIST = {
    "ENF-GATE-TEST",       # gate-denial message label "[ENF-GATE-TEST]", not a rule id
    "ENF-GATE-PLAN",       # gate-denial message label "[ENF-GATE-PLAN]"
    "RED-GREEN-REFACTOR",  # prose: the TDD cycle (PBK-AUTHOR-001), not a node
    "PSC-FOO-001",         # authoring-example placeholder in META-AUTH-002 / meta-authoring
    "SKL-PROC-FOO-001",    # authoring-example placeholder
}

# Rule-block files live in the domain dirs; methodology/ holds front-matter node files
# (some of which are rule-format ENF companions). Both contribute rules + edges.
_DOMAIN_RULE_FILES = sorted(p for p in BIBLE.rglob("*.md") if "methodology" not in p.parts)
_METHODOLOGY_FILES = sorted(METHODOLOGY.glob("*.md"))


def _collect_corpus() -> dict:
    """Parse the whole corpus once, faithfully to the ingester.

    Returns rule_ids, cross_refs_by_rule (rule -> set of referenced ids),
    parsed_edges (front-matter typed edges), and all_node_ids (rules + methodology).
    """
    rule_ids: set[str] = set()
    cross_refs_by_rule: dict[str, set[str]] = defaultdict(set)
    all_node_ids: set[str] = set()
    parsed_edges: list[dict] = []

    # Domain rule-block files.
    for f in _DOMAIN_RULE_FILES:
        for r in parse_rules_from_file(f):
            rid = r.get("rule_id")
            if rid:
                rule_ids.add(rid)
                all_node_ids.add(rid)
                cross_refs_by_rule[rid] |= set(r.get("_cross_references", []))

    # Methodology node files (rule-format companions + methodology nodes) and their edges.
    for f in _METHODOLOGY_FILES:
        for n in parse_nodes_from_file(f):
            nt = n.get("node_type", "Rule")
            id_field = NODE_ID_FIELDS.get(nt)
            nid = n.get(id_field) if id_field else None
            if nid:
                all_node_ids.add(nid)
            if nt == "Rule" and n.get("rule_id"):
                rule_ids.add(n["rule_id"])
                cross_refs_by_rule[n["rule_id"]] |= set(n.get("_cross_references", []))
        parsed_edges.extend(parse_edges_from_file(f))

    return {
        "rule_ids": rule_ids,
        "cross_refs_by_rule": cross_refs_by_rule,
        "parsed_edges": parsed_edges,
        "all_node_ids": all_node_ids,
    }


def _collect_domain_rule_ids() -> set[str]:
    """Rule IDs found only in bible/<domain>/rules.md (non-methodology) files."""
    ids: set[str] = set()
    for f in _DOMAIN_RULE_FILES:
        for r in parse_rules_from_file(f):
            rid = r.get("rule_id")
            if rid:
                ids.add(rid)
    return ids


def _collect_methodology_rule_ids() -> set[str]:
    """Rule IDs found in bible/methodology/*.md files (node_type=='Rule' blocks)."""
    ids: set[str] = set()
    for f in _METHODOLOGY_FILES:
        for n in parse_nodes_from_file(f):
            if n.get("node_type", "Rule") == "Rule" and n.get("rule_id"):
                ids.add(n["rule_id"])
    return ids


def _compute_orphan_rules(corpus: dict) -> list[str]:
    """Rules with degree 0, computed exactly as methodology_ingest builds edges."""
    rule_ids = corpus["rule_ids"]
    incident: set[str] = set()
    # RELATED_TO from rule cross-references that point at other rules (both endpoints).
    for rid, refs in corpus["cross_refs_by_rule"].items():
        rule_refs = (refs & rule_ids) - {rid}
        if rule_refs:
            incident.add(rid)
            incident |= rule_refs
    # Front-matter typed edges touching a rule.
    for e in corpus["parsed_edges"]:
        if e.get("source") in rule_ids:
            incident.add(e["source"])
        if e.get("target") in rule_ids:
            incident.add(e["target"])
    return sorted(rule_ids - incident)


# --- 1. Formatting lock (regression guard) ------------------------------------


class TestFormattingLock:
    def test_all_rules_parse_validate_and_are_complete(self) -> None:
        problems = []
        count = 0
        # Domain rule-block files: parsed via the RULE-START parser.
        for f in _DOMAIN_RULE_FILES:
            try:
                rules = parse_rules_from_file(f)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{f.name}: parse error {e}")
                continue
            for r in rules:
                count += 1
                rid = r.get("rule_id", "?")
                missing = [k for k in REQUIRED_RULE_FIELDS if not str(r.get(k, "")).strip()]
                if missing:
                    problems.append(f"{rid}: missing {missing}")
                try:
                    validate_parsed_rule(r)
                except (ValueError, ValidationError) as e:
                    problems.append(f"{rid}: {type(e).__name__}: {str(e)[:120]}")
        # The 12 dual-location rules now live in bible/methodology/<id>.md as YAML
        # front-matter (their canonical home after the Phase 0 dual-location
        # resolution). parse_rules_from_file (the RULE-START parser) returns 0 for
        # these, so they must be parsed via parse_nodes_from_file (the front-matter
        # parser) instead. Count and validate them with the SAME per-rule
        # completeness checks so the >=280 guarantee still spans all 285 rules.
        from writ.export import _METHODOLOGY_CANONICAL_RULE_IDS
        _dual_files = [METHODOLOGY / f"{rid}.md" for rid in _METHODOLOGY_CANONICAL_RULE_IDS]
        for f in [p for p in _dual_files if p.exists()]:
            try:
                nodes = parse_nodes_from_file(f)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{f.name}: parse error {e}")
                continue
            for n in nodes:
                if n.get("node_type", "Rule") != "Rule":
                    continue
                count += 1
                rid = n.get("rule_id", "?")
                missing = [k for k in REQUIRED_RULE_FIELDS if not str(n.get(k, "")).strip()]
                if missing:
                    problems.append(f"{rid}: missing {missing}")
                try:
                    validate_parsed_rule(n)
                except (ValueError, ValidationError) as e:
                    problems.append(f"{rid}: {type(e).__name__}: {str(e)[:120]}")
        assert count >= 280, f"expected >=280 rule blocks, found {count}"
        assert not problems, "rule formatting problems:\n" + "\n".join(problems[:40])

    def test_all_methodology_nodes_validate(self) -> None:
        problems = []
        for f in _METHODOLOGY_FILES:
            try:
                for n in parse_nodes_from_file(f):
                    validate_parsed_node(n)
            except (ValueError, ValidationError) as e:
                problems.append(f"{f.name}: {type(e).__name__}: {str(e)[:120]}")
        assert not problems, "methodology validation problems:\n" + "\n".join(problems)


# --- 2. Edge densification (filesystem-faithful, always runs) -----------------


class TestEdgeDensification:
    def test_orphan_rules_within_ceiling(self) -> None:
        corpus = _collect_corpus()
        orphans = _compute_orphan_rules(corpus)
        # Residual standalone rules are reported, not hidden.
        assert len(orphans) <= ORPHAN_CEILING, (
            f"{len(orphans)} orphan rules exceed ceiling {ORPHAN_CEILING}; "
            f"residual: {orphans}"
        )

    def test_lone_methodology_orphan_connected(self) -> None:
        corpus = _collect_corpus()
        edge_endpoints = set()
        for e in corpus["parsed_edges"]:
            edge_endpoints.add(e.get("source"))
            edge_endpoints.add(e.get("target"))
        assert LONE_METHODOLOGY_ORPHAN in edge_endpoints, (
            f"{LONE_METHODOLOGY_ORPHAN} still has no front-matter edge (still orphaned)"
        )

    def test_all_front_matter_edge_targets_resolve(self) -> None:
        """No dangling: every typed-edge endpoint is a known node id."""
        corpus = _collect_corpus()
        known = corpus["all_node_ids"]
        dangling = [
            f"{e.get('source')} --{e.get('type')}--> {e.get('target')}"
            for e in corpus["parsed_edges"]
            if e.get("source") not in known or e.get("target") not in known
        ]
        assert not dangling, "dangling front-matter edges:\n" + "\n".join(dangling[:40])

    def test_no_invented_related_to_edges(self) -> None:
        """Anti-noise (structural): a RELATED_TO can only exist where the source rule
        block actually mentions the target rule id. Since RELATED_TO is derived from
        `_cross_references`, broken/dangling cross-refs to non-existent rules would
        signal a typo'd or invented reference. Every rule-shaped cross-ref token that
        targets the rule namespace must resolve to a real rule id."""
        corpus = _collect_corpus()
        rule_ids = corpus["rule_ids"]
        all_ids = corpus["all_node_ids"]
        broken = []
        for rid, refs in corpus["cross_refs_by_rule"].items():
            for ref in refs:
                # A cross-ref token that resolves to nothing in the corpus is a typo
                # or an invented reference -- it would silently create no edge. Known
                # denial-labels and authoring placeholders are excluded.
                if ref not in all_ids and ref not in CROSS_REF_ALLOWLIST:
                    broken.append(f"{rid} -> {ref} (no such node)")
        assert not broken, "cross-references to non-existent nodes:\n" + "\n".join(broken[:40])


# --- 3. Live graph cross-check (skips when Neo4j unavailable) ------------------


class TestLiveGraphCrossCheck:
    def _live_orphan_rule_count(self):
        """Return (orphan_count, mode_skill_degree) from the live graph, or None if
        Neo4j is unreachable."""
        import asyncio

        async def _q():
            from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
            from writ.graph.db import Neo4jConnection

            db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
            try:
                async with db._driver.session(database=db._database) as s:
                    res = await s.run(
                        "MATCH (r:Rule) WITH r, size([(r)--() | 1]) AS d "
                        "RETURN sum(CASE WHEN d=0 THEN 1 ELSE 0 END) AS orphans, count(r) AS total"
                    )
                    rec = [r.data() async for r in res][0]
                    res2 = await s.run(
                        "MATCH (n {skill_id: $sid}) RETURN size([(n)--() | 1]) AS d",
                        sid=LONE_METHODOLOGY_ORPHAN,
                    )
                    deg_rows = [r.data() async for r in res2]
                    mode_deg = deg_rows[0]["d"] if deg_rows else None
                    return rec["orphans"], rec["total"], mode_deg
            finally:
                await db.close()

        try:
            return asyncio.run(_q())
        except Exception:  # noqa: BLE001
            return None

    def test_live_orphan_count_within_ceiling(self, corpus_ready) -> None:
        # INC-1: corpus_ready guarantees Rule nodes are present, so an empty graph can no
        # longer mask a regression as a skip; only an unreachable Neo4j skips.
        result = self._live_orphan_rule_count()
        if result is None:
            pytest.skip("Neo4j unreachable")
        orphans, total, _ = result
        assert orphans <= ORPHAN_CEILING, f"live graph has {orphans} orphan rules (> {ORPHAN_CEILING})"

    def test_live_mode_skill_connected(self, corpus_ready) -> None:
        result = self._live_orphan_rule_count()
        if result is None:
            pytest.skip("Neo4j unreachable")
        _, total, mode_deg = result
        assert mode_deg and mode_deg > 0, f"{LONE_METHODOLOGY_ORPHAN} is still orphaned in the live graph"


# --- 4. Phase 0 corpus integrity (T0.8/T0.9) ----------------------------------
# RED tests: dual-location rule elimination + SKL-PROC-DEBUG-001 authoring.
# Green after: (a) the 12 ENF-*/META-AUTH rule blocks are removed from
# bible/<domain>/rules.md, leaving them only in bible/methodology/; (b)
# bible/methodology/SKL-PROC-DEBUG-001.md is authored and SKL-PROC-DEBUG-001
# is removed from CROSS_REF_ALLOWLIST.


class TestCorpusIntegrity:
    def test_no_dual_location_rule_ids(self) -> None:
        """Every rule id must live in exactly one location: either a domain
        rules.md file or a bible/methodology/*.md file, never both.

        RED today: 12 ids are present in both locations --
        ENF-COMMS-001, ENF-META-CONCISE-001,
        ENF-PROC-{BRAIN,DEBUG,PLAN,PRIORITY,SDD,TDD,VERIFY,WORKTREE}-001,
        META-AUTH-001, META-AUTH-002.

        GREEN after T0.8: those 12 duplicate blocks are removed from the domain
        rules.md files so only the bible/methodology/ copies remain.
        """
        domain_ids = _collect_domain_rule_ids()
        methodology_ids = _collect_methodology_rule_ids()
        dual_location = sorted(domain_ids & methodology_ids)
        assert dual_location == [], (
            f"{len(dual_location)} rule id(s) appear in both a domain rules.md and "
            f"bible/methodology/: {dual_location}"
        )

    def test_skl_proc_debug_001_exists_and_validates(self) -> None:
        """bible/methodology/SKL-PROC-DEBUG-001.md must exist, parse to exactly
        one node with node_type=='Skill' and skill_id=='SKL-PROC-DEBUG-001',
        and that node must pass validate_parsed_node.

        Additionally, once the file is authored the id is a real corpus node and
        must be removed from CROSS_REF_ALLOWLIST (keeping it there would suppress
        a dead-reference error that should become a corpus health signal).

        RED today because:
          - bible/methodology/SKL-PROC-DEBUG-001.md does not exist (FileNotFoundError).
          - 'SKL-PROC-DEBUG-001' is still listed in CROSS_REF_ALLOWLIST as an
            illustrative placeholder, which is only valid while the node is absent.

        GREEN after T0.9: file authored + CROSS_REF_ALLOWLIST entry removed.
        """
        skill_file = METHODOLOGY / "SKL-PROC-DEBUG-001.md"
        # Assert the file exists before trying to parse it so the failure message
        # is unambiguous (FileNotFoundError from parse_nodes_from_file would be
        # less clear than an explicit assertion).
        assert skill_file.exists(), (
            f"bible/methodology/SKL-PROC-DEBUG-001.md does not exist; "
            "author it as part of T0.9 before this test can go green"
        )

        nodes = list(parse_nodes_from_file(skill_file))
        assert len(nodes) == 1, (
            f"expected exactly 1 node in SKL-PROC-DEBUG-001.md, got {len(nodes)}"
        )
        node = nodes[0]
        assert node.get("node_type") == "Skill", (
            f"expected node_type=='Skill', got {node.get('node_type')!r}"
        )
        assert node.get("skill_id") == "SKL-PROC-DEBUG-001", (
            f"expected skill_id=='SKL-PROC-DEBUG-001', got {node.get('skill_id')!r}"
        )

        # Pydantic validation must pass.
        try:
            validate_parsed_node(node)
        except (ValueError, ValidationError) as exc:
            raise AssertionError(
                f"SKL-PROC-DEBUG-001 node failed validation: {exc}"
            ) from exc

        # Once the node exists it is a real corpus member, so it must NOT appear
        # in CROSS_REF_ALLOWLIST (that entry is a placeholder exemption for a
        # missing node; keeping it after authoring silences a legitimate health check).
        assert "SKL-PROC-DEBUG-001" not in CROSS_REF_ALLOWLIST, (
            "'SKL-PROC-DEBUG-001' is still in CROSS_REF_ALLOWLIST; "
            "remove it now that the node is a real corpus member"
        )
