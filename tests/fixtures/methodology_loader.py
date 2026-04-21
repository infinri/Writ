"""Parse synthetic_methodology corpus into node dicts for Phase 0 benchmarks.

Phase 0 harness reads this corpus without ingesting into Neo4j (plan Section 5.5
says Phase 0 is read-only to production). Phase 1 ingest replaces this loader
with the real parser.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tantivy
import yaml

from writ.retrieval.keyword import _TANTIVY_RESERVED, _TANTIVY_SPECIAL  # type: ignore[attr-defined]

FIXTURE_DIR = Path(__file__).parent / "synthetic_methodology"
GROUND_TRUTH_PATH = Path(__file__).parent / "ground_truth_proc.json"

RETRIEVABLE_TYPES = {"Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse"}
ID_FIELDS = (
    "rule_id", "skill_id", "playbook_id", "technique_id", "antipattern_id",
    "forbidden_id", "phase_id", "rationalization_id", "scenario_id",
    "example_id", "role_id",
)
FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)


@dataclass
class MethodologyNode:
    node_id: str
    node_type: str
    front_matter: dict[str, Any]
    body: str
    tags: list[str] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)

    @property
    def trigger(self) -> str:
        return self.front_matter.get("trigger", "") or ""

    @property
    def statement(self) -> str:
        return self.front_matter.get("statement", "") or ""

    @property
    def is_retrievable(self) -> bool:
        # Rule and the 5 retrievable new types participate in Stages 1-3 ranking.
        return self.node_type == "Rule" or self.node_type in RETRIEVABLE_TYPES


def load_corpus(fixture_dir: Path | None = None) -> list[MethodologyNode]:
    """Load all synthetic fixtures into MethodologyNode objects."""
    d = fixture_dir or FIXTURE_DIR
    nodes: list[MethodologyNode] = []
    for f in sorted(d.glob("*.md")):
        content = f.read_text()
        m = FRONT_MATTER_RE.match(content)
        if not m:
            continue
        fm = yaml.safe_load(m.group(1))
        body = m.group(2)
        node_type = fm.get("node_type", "Rule" if "rule_id" in fm else None)
        node_id = next((fm[k] for k in ID_FIELDS if k in fm), None)
        if not node_id or not node_type:
            continue
        nodes.append(MethodologyNode(
            node_id=node_id,
            node_type=node_type,
            front_matter=fm,
            body=body,
            tags=fm.get("tags") or [],
            edges=fm.get("edges") or [],
        ))
    return nodes


def to_keyword_index_format(nodes: list[MethodologyNode]) -> list[dict[str, Any]]:
    """Convert retrievable nodes into the dict shape KeywordIndex.build expects.

    Maps `<type>_id` to `rule_id` because the existing KeywordIndex schema is
    keyed on rule_id. Phase 1 ingest will unify naming.
    """
    return [
        {
            "rule_id": n.node_id,
            "trigger": n.trigger,
            "statement": n.statement,
            "tags": " ".join(n.tags),
            "mandatory": False,
        }
        for n in nodes if n.is_retrievable
    ]


def load_ground_truth(path: Path | None = None) -> dict[str, Any]:
    """Load candidate ground-truth queries."""
    p = path or GROUND_TRUTH_PATH
    return json.loads(p.read_text())


def build_adjacency(nodes: list[MethodologyNode]) -> dict[str, list[tuple[str, str]]]:
    """node_id -> [(target_id, edge_type), ...] for bundle-completeness measurement."""
    return {n.node_id: [(e["target"], e["type"]) for e in n.edges] for n in nodes}


# --- Phase 0 methodology keyword index -----------------------------------------------------------
# Mirrors writ/retrieval/keyword.py KeywordIndex but extends the schema with a `body` field at 0.5x
# effective weight per plan Section 3.2 ("body-field indexing at 0.5× weight for Skill/Playbook/
# Technique bodies to avoid generic methodology vocabulary swamping specific matches"). Also
# includes forbidden_phrases for ForbiddenResponse nodes so lexical queries matching those phrases
# route to the correct node. Phase 1's KeywordIndex will replace this local class.

_TRIGGER_BOOST = 2.0  # match writ.retrieval.keyword.TRIGGER_BOOST


class MethodologyIndex:
    """Tantivy BM25 index over trigger (2× boost), statement, tags, body (0.5× via token dilution)."""

    def __init__(self) -> None:
        sb = tantivy.SchemaBuilder()
        sb.add_text_field("rule_id", stored=True)
        sb.add_text_field("trigger", stored=True)
        sb.add_text_field("statement", stored=True)
        sb.add_text_field("tags", stored=True)
        sb.add_text_field("body", stored=True)
        self._schema = sb.build()
        self._index = tantivy.Index(self._schema)

    def build(self, nodes: list[MethodologyNode]) -> int:
        writer = self._index.writer()
        count = 0
        for n in nodes:
            if not n.is_retrievable:
                continue
            trigger_boosted = " ".join([n.trigger] * int(_TRIGGER_BOOST))
            # 0.5× effective weight on body via every-other-token dilution. Imperfect but matches
            # the plan's "at 0.5× weight" specification without needing per-field query-time weights.
            body_text = self._collect_body_text(n)
            body_tokens = body_text.split()
            body_halved = " ".join(body_tokens[::2])
            writer.add_document(tantivy.Document(
                rule_id=n.node_id,
                trigger=trigger_boosted,
                statement=n.statement,
                tags=" ".join(n.tags),
                body=body_halved,
            ))
            count += 1
        writer.commit()
        self._index.reload()
        return count

    @staticmethod
    def _collect_body_text(n: MethodologyNode) -> str:
        """Body text plus forbidden_phrases (for FRB-* nodes) plus what_to_say_instead."""
        parts: list[str] = [n.body]
        fm = n.front_matter
        if n.node_type == "ForbiddenResponse":
            phrases = fm.get("forbidden_phrases") or []
            parts.extend(phrases)
            wts = fm.get("what_to_say_instead")
            if wts:
                parts.append(wts)
        # AntiPattern named_in + counter_nodes list as extra signal
        if n.node_type == "AntiPattern":
            named = fm.get("named_in")
            if named:
                parts.append(named)
        return " ".join(p for p in parts if p)

    def search(self, query_text: str, limit: int = 50) -> list[dict]:
        searcher = self._index.searcher()
        sanitized = _TANTIVY_SPECIAL.sub(" ", query_text)
        sanitized = _TANTIVY_RESERVED.sub(lambda m: m.group(0).lower(), sanitized).strip()
        if not sanitized:
            return []
        try:
            query = self._index.parse_query(sanitized, ["trigger", "statement", "tags", "body"])
        except ValueError:
            return []
        hits = searcher.search(query, limit).hits
        return [{"rule_id": searcher.doc(addr)["rule_id"][0], "score": score} for score, addr in hits]


def build_methodology_index(nodes: list[MethodologyNode]) -> MethodologyIndex:
    """Build the Phase 0 keyword index over retrievable nodes."""
    idx = MethodologyIndex()
    idx.build(nodes)
    return idx
