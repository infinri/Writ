"""Phase 5.2b: rules export fidelity regression lock.

This test re-exports the derived Rule nodes from the live graph to a tmp dir and
field-level diffs the exported files against the committed bible/ source files.

IMPORTANT: this test is a REGRESSION LOCK, not a RED-on-current-code test. It
should PASS immediately against the current export code -- it pins the current
correct behavior and goes RED only if a future change to export.py or
_build_file_content corrupts field-level fidelity. This is intentional and correct
for a lock test.

Why no Neo4j skip masking: follows the INC-1 anti-masking contract from _corpus.py.
A reachable but empty graph raises an assertion, not a skip. Only a truly unreachable
Neo4j (connection refused) skips.

RUNTIME_OR_RENDER_EXEMPT mirrors export.py's GRAPH_ONLY_FIELDS plus provenance.
The implementer MUST verify this set against _build_file_content at implementation
time -- it is the exact set of fields export.py omits when rendering Markdown.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from tests._corpus import ensure_corpus, neo4j_reachable
from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.export import DERIVED_EDGE_TYPES, export_rules_to_markdown
from writ.graph.db import Neo4jConnection
from writ.graph.ingest import parse_edges_from_file, parse_rules_from_file

from tests._bible_guard import requires_bible

pytestmark = requires_bible


BIBLE = Path(__file__).resolve().parent.parent / "bible"


def _declared_edges_by_source(md_file: Path) -> dict[str, set[tuple[str, str]]]:
    """Map each rule_id in `md_file` to its set of DECLARED outgoing edges as
    (type, target) tuples, excluding the derived RELATED_TO / BELONGS_TO types.

    parse_edges_from_file already collects RULE-START `### Edges` declarations
    (the only declared-edge surface in a domain rules.md). Derived edges are
    never written to an `### Edges` section, so excluding them keeps the
    comparison to genuinely-declared edges -- which is exactly the set that must
    survive export.
    """
    out: dict[str, set[tuple[str, str]]] = {}
    for edge in parse_edges_from_file(md_file):
        etype = edge.get("type")
        if etype in DERIVED_EDGE_TYPES:
            continue
        out.setdefault(edge["source"], set()).add((etype, edge["target"]))
    return out

# Fields that export.py omits from the rendered Markdown (runtime-only or
# graph-only). The test skips these when comparing exported vs committed fields.
# Source: export.py GRAPH_ONLY_FIELDS + provenance (excluded per export.py:54-55).
# The implementer MUST confirm this set against _build_file_content at impl time.
RUNTIME_OR_RENDER_EXEMPT: frozenset[str] = frozenset({
    "confidence",
    "evidence",
    "staleness_window",
    "last_validated",
    "authority",
    "times_seen_positive",
    "times_seen_negative",
    "last_seen",
    "source_origin",
    "provenance",
})


@pytest.fixture()
def db():
    """Synchronous setup: skip if Neo4j unreachable, heal if corpus incomplete.

    Stays sync because neo4j_reachable()/ensure_corpus() each call asyncio.run()
    internally and so cannot run inside an async fixture's loop. The connection is
    created lazily by the async test on its own loop; teardown closes it in a worker
    thread (fresh loop) -- asyncio.run(conn.close()) on the main thread after the test
    loop is torn down raises 'Event loop is closed' because the driver is bound to that
    dead loop.
    """
    if not neo4j_reachable():
        pytest.skip("Neo4j unreachable")
    ensure_corpus()
    conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    yield conn
    import asyncio
    import concurrent.futures

    def _close() -> None:
        asyncio.run(conn.close())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        try:
            ex.submit(_close).result(timeout=15)
        except Exception:  # noqa: BLE001
            pass


@pytest.mark.asyncio
async def test_exported_rules_match_committed_source(db: Neo4jConnection, tmp_path: Path) -> None:
    """Re-export rules to tmp_path and assert they field-level match the committed bible/.

    Compares PARSED rule dicts (not raw text) so the diff is insensitive to whitespace
    and section-ordering inside a file. Diffs are keyed by rule_id then field name.
    """
    # Re-export rules from the live graph.
    stats = await export_rules_to_markdown(db, output_dir=tmp_path, bible_dir=BIBLE)
    assert stats["files_written"] > 0, (
        "export_rules_to_markdown wrote 0 files -- corpus may be empty or export broken"
    )

    mismatches: list[str] = []

    for exported_file in sorted(tmp_path.rglob("*.md")):
        rel_path = exported_file.relative_to(tmp_path)
        committed_file = BIBLE / rel_path
        if not committed_file.exists():
            # New file created by export (no committed counterpart yet) -- skip.
            continue

        # Parse both files with the same parser so the comparison is on structured dicts.
        try:
            exported_rules = {r["rule_id"]: r for r in parse_rules_from_file(exported_file)}
            committed_rules = {r["rule_id"]: r for r in parse_rules_from_file(committed_file)}
        except Exception as exc:
            mismatches.append(f"{rel_path}: parse error -- {exc}")
            continue

        # Declared-edge diff (regression lock for BUG 2: export silently dropped
        # the `### Edges` section). Compares the set of DECLARED outgoing edges
        # (type+target, derived RELATED_TO/BELONGS_TO excluded) per rule between
        # the committed source and the re-export. A future export-drops-edges
        # regression goes RED here even when every field matches.
        exported_edges = _declared_edges_by_source(exported_file)
        committed_edges = _declared_edges_by_source(committed_file)

        # Field-level diff for each rule present in the export.
        for rule_id in sorted(exported_rules):
            if rule_id not in committed_rules:
                # Rule present in export but not committed source; skip (new rule).
                continue
            exp = exported_rules[rule_id]
            com = committed_rules[rule_id]
            # Compare only the intersection of non-exempt fields.
            all_keys = (set(exp) | set(com)) - RUNTIME_OR_RENDER_EXEMPT - {"rule_id"}
            for field in sorted(all_keys):
                exp_val = exp.get(field)
                com_val = com.get(field)
                if exp_val != com_val:
                    mismatches.append(
                        f"{rel_path} | {rule_id} | {field}: "
                        f"exported={exp_val!r} vs committed={com_val!r}"
                    )

            exp_edges = exported_edges.get(rule_id, set())
            com_edges = committed_edges.get(rule_id, set())
            if exp_edges != com_edges:
                mismatches.append(
                    f"{rel_path} | {rule_id} | declared_edges: "
                    f"exported={sorted(exp_edges)!r} vs committed={sorted(com_edges)!r}"
                )

    assert not mismatches, (
        f"Export fidelity failures ({len(mismatches)}):\n"
        + "\n".join(mismatches[:20])
        + ("\n  ... (truncated)" if len(mismatches) > 20 else "")
    )


@pytest.mark.asyncio
async def test_export_covers_all_committed_rule_ids(db: Neo4jConnection, tmp_path: Path) -> None:
    """Every rule_id in committed bible/ files must appear in the re-export.

    Regression lock: if export.py accidentally drops a rule_id the test goes RED.
    Methodology-canonical rule IDs (dual-location) are intentionally excluded from the
    domain rules.md export -- they are NOT in tmp_path output -- so skip them.
    """
    from writ.export import _METHODOLOGY_CANONICAL_RULE_IDS

    stats = await export_rules_to_markdown(db, output_dir=tmp_path, bible_dir=BIBLE)
    assert stats["rules_exported"] > 0

    # Collect all rule_ids the committed bible/ source declares (domain rules.md files only).
    committed_ids: set[str] = set()
    for md_file in BIBLE.rglob("*.md"):
        # Skip methodology/ files -- they are the canonical dual-location homes.
        if "methodology" in md_file.parts:
            continue
        for rule in parse_rules_from_file(md_file):
            committed_ids.add(rule["rule_id"])

    # Collect exported rule_ids.
    exported_ids: set[str] = set()
    for md_file in tmp_path.rglob("*.md"):
        for rule in parse_rules_from_file(md_file):
            exported_ids.add(rule["rule_id"])

    # Rules in committed source that are NOT in the export and are NOT methodology-canonical.
    missing_from_export = (committed_ids - exported_ids) - _METHODOLOGY_CANONICAL_RULE_IDS
    assert not missing_from_export, (
        f"Rule IDs present in committed bible/ but absent from export: {sorted(missing_from_export)}"
    )
