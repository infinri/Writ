"""Phase 0 Wave D -- deterministic Category migration.

Assigns every corpus node (derived from the bible markdown on disk, NOT Neo4j)
to exactly one Category, authors the 22 Category nodes, resolves the 12
dual-location rules to a single canonical home, and authors the missing
SKL-PROC-DEBUG-001 skill node.

Usage:
    .venv/bin/python scripts/migrate_phase0_categories.py --dry-run
    .venv/bin/python scripts/migrate_phase0_categories.py --apply

--dry-run (default): report only, touch nothing.
--apply: inject `category:`/`**Category**:` fields idempotently, surgically
    delete dual-location RULE START blocks from their domain rules.md copies,
    and write the CAT-*.md + SKL-PROC-DEBUG-001.md node files.

Node enumeration is faithful to the ingester: it uses
writ.graph.ingest.parse_nodes_from_file so the node set, ids and node_types
match exactly what `writ import-markdown` would see.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from writ.graph.ingest import (  # noqa: E402
    NODE_ID_FIELDS,
    discover_rule_files,
    parse_nodes_from_file,
    validate_parsed_node,
)

BIBLE = REPO_ROOT / "bible"
METHODOLOGY = BIBLE / "methodology"

# --- The 22 Category nodes ----------------------------------------------------
# (category_id, human name, routes, parent)
CATEGORY_DEFS: list[tuple[str, str, list[str], str | None]] = [
    ("CAT-CODE-001", "Coding rules", ["semantic"], None),
    ("CAT-CODE-SECURITY-001", "Security", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-ARCH-001", "Architecture", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-QUALITY-001", "Code quality", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-TESTING-001", "Testing", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-PERF-001", "Performance", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-API-001", "API design", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-DOCS-001", "Documentation", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-SCALING-001", "Scaling", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-RESEARCH-001", "Research", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-AIENF-001", "AI enforcement and system dynamics", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-FW-001", "Frameworks", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-FW-MAGENTO-001", "Magento", ["semantic"], "CAT-CODE-FW-001"),
    ("CAT-CODE-LANG-001", "Languages", ["semantic"], "CAT-CODE-001"),
    ("CAT-CODE-LANG-PHP-001", "PHP", ["semantic"], "CAT-CODE-LANG-001"),
    ("CAT-CODE-LANG-PYTHON-001", "Python", ["semantic"], "CAT-CODE-LANG-001"),
    ("CAT-CODE-LANG-SQL-001", "SQL", ["semantic"], "CAT-CODE-LANG-001"),
    ("CAT-PROC-001", "Process and workflow", ["state", "action", "pull"], None),
    ("CAT-PROC-DISPATCH-001", "Dispatch and orchestration", ["state", "action", "pull"], "CAT-PROC-001"),
    ("CAT-COMM-001", "Communication", ["always_on", "action"], None),
    ("CAT-META-001", "Meta-authoring", ["action", "pull"], None),
    ("CAT-DISC-001", "Discipline counters", ["pull"], None),
]

CATEGORY_IDS = {c[0] for c in CATEGORY_DEFS}

# --- The 12 dual-location rules (canonical home = bible/methodology/<id>.md) ---
DUAL_LOCATION_RULES = {
    "ENF-COMMS-001",
    "ENF-META-CONCISE-001",
    "ENF-PROC-BRAIN-001",
    "ENF-PROC-DEBUG-001",
    "ENF-PROC-PLAN-001",
    "ENF-PROC-PRIORITY-001",
    "ENF-PROC-SDD-001",
    "ENF-PROC-TDD-001",
    "ENF-PROC-VERIFY-001",
    "ENF-PROC-WORKTREE-001",
    "META-AUTH-001",
    "META-AUTH-002",
}

# --- Discipline-counter node types -> CAT-DISC-001 ----------------------------
DISCIPLINE_TYPES = {
    "AntiPattern",
    "Rationalization",
    "ForbiddenResponse",
    "PressureScenario",
    "WorkedExample",
}

# --- Instructional/role/phase node types (domain-routed) ----------------------
INSTRUCTIONAL_TYPES = {"Playbook", "Skill", "Technique", "Phase", "SubagentRole"}

# Substrings that mark a node as dispatch-and-orchestration (matched against id).
DISPATCH_SUBSTRINGS = ("DISPATCH", "ORCHESTRATOR", "PARALLEL", "SDD", "AUDIT-FANOUT")


def _normalize_domain(domain: str) -> str:
    """Lowercase + collapse whitespace for case-insensitive domain matching."""
    return re.sub(r"\s+", " ", (domain or "").strip().lower())


# Normalized-domain -> category for Rule nodes.
DOMAIN_TO_CATEGORY: dict[str, str] = {
    "security": "CAT-CODE-SECURITY-001",
    "architecture": "CAT-CODE-ARCH-001",
    "code-quality": "CAT-CODE-QUALITY-001",
    "testing": "CAT-CODE-TESTING-001",
    "performance": "CAT-CODE-PERF-001",
    "api-design": "CAT-CODE-API-001",
    "documentation": "CAT-CODE-DOCS-001",
    "scaling": "CAT-CODE-SCALING-001",
    "research": "CAT-CODE-RESEARCH-001",
    "ai enforcement": "CAT-CODE-AIENF-001",
    "system dynamics": "CAT-CODE-AIENF-001",
    "operations": "CAT-CODE-AIENF-001",
    "enforcement": "CAT-CODE-AIENF-001",
    "frameworks / magento 2": "CAT-CODE-FW-MAGENTO-001",
    "php / coding standards": "CAT-CODE-LANG-PHP-001",
    "php / error handling": "CAT-CODE-LANG-PHP-001",
    "database / sql": "CAT-CODE-LANG-SQL-001",
    "process": "CAT-PROC-001",
    "communication": "CAT-COMM-001",
    "meta-authoring": "CAT-META-001",
}


@dataclass
class NodeRecord:
    node_id: str
    node_type: str
    domain: str
    filepath: Path
    source_format: str  # "front-matter" | "rule-block"
    category: str | None = None
    reason: str = ""


@dataclass
class Plan:
    nodes: list[NodeRecord] = field(default_factory=list)
    unmapped: list[NodeRecord] = field(default_factory=list)
    # (rule_id, rules.md path) blocks that would be deleted on --apply.
    dual_deletes: list[tuple[str, Path]] = field(default_factory=list)


def _is_dispatch(node_id: str, parent_playbook_id: str | None) -> bool:
    """Dispatch-and-orchestration test.

    Primary: id contains a dispatch substring, or is a ROL-* SubagentRole.
    Secondary (Phase nodes): the phase's parent_playbook_id resolves to a
    dispatch playbook (PBK-PROC-AUDIT-FANOUT-001 / PBK-PROC-ORCHESTRATOR-001),
    so the FANOUT/ORCH phase families ride with their parent playbook.
    """
    upper = node_id.upper()
    if upper.startswith("ROL-"):
        return True
    if any(sub in upper for sub in DISPATCH_SUBSTRINGS):
        return True
    if parent_playbook_id:
        pu = parent_playbook_id.upper()
        if any(sub in pu for sub in DISPATCH_SUBSTRINGS):
            return True
    return False


def _categorize(node: dict) -> tuple[str | None, str]:
    """Return (category_id, reason) for a parsed node dict, or (None, reason)."""
    node_type = node.get("node_type", "Rule")
    id_field = NODE_ID_FIELDS.get(node_type, "rule_id")
    node_id = node.get(id_field, "")
    raw_domain = node.get("domain", "")
    domain = _normalize_domain(raw_domain)

    if node_type in DISCIPLINE_TYPES:
        return "CAT-DISC-001", f"discipline-counter node_type={node_type}"

    if node_type in INSTRUCTIONAL_TYPES:
        if domain == "meta-authoring":
            return "CAT-META-001", "instructional domain=meta-authoring"
        if domain == "communication":
            return "CAT-COMM-001", "instructional domain=communication"
        # process (and any other non-meta/non-comm instructional node)
        if _is_dispatch(node_id, node.get("parent_playbook_id")):
            return "CAT-PROC-DISPATCH-001", "instructional dispatch-and-orchestration"
        return "CAT-PROC-001", f"instructional domain={domain or 'process'}"

    if node_type == "Rule":
        cat = DOMAIN_TO_CATEGORY.get(domain)
        # Python / * family -> PYTHON
        if cat is None and domain.startswith("python /"):
            cat = "CAT-CODE-LANG-PYTHON-001"
        if cat is not None:
            return cat, f"rule domain='{raw_domain}'"
        return None, f"rule with unmapped domain='{raw_domain}'"

    return None, f"unhandled node_type={node_type}"


def build_plan() -> Plan:
    """Enumerate every node from the bible markdown, dedup dual-location rules,
    and assign each surviving node a category."""
    plan = Plan()
    files = discover_rule_files(BIBLE)

    # (node_type, node_id) -> index into a working list; prefer the front-matter
    # methodology copy (matches ingest dedup in methodology_ingest.py).
    by_key: dict[tuple[str, str], NodeRecord] = {}
    order: list[tuple[str, str]] = []

    for filepath in files:
        try:
            file_nodes = parse_nodes_from_file(filepath)
        except Exception as exc:  # parse failure surfaces as a pseudo-unmapped row
            plan.unmapped.append(
                NodeRecord(
                    node_id=f"<parse-error:{filepath.name}>",
                    node_type="?",
                    domain="",
                    filepath=filepath,
                    source_format="?",
                    reason=f"parse error: {exc}",
                )
            )
            continue

        for node in file_nodes:
            node_type = node.get("node_type", "Rule")
            id_field = NODE_ID_FIELDS.get(node_type, "rule_id")
            node_id = node.get(id_field, "")
            src_fmt = (
                "front-matter"
                if node.get("_source_format") == "front-matter"
                else "rule-block"
            )
            cat, reason = _categorize(node)
            rec = NodeRecord(
                node_id=node_id,
                node_type=node_type,
                domain=node.get("domain", ""),
                filepath=filepath,
                source_format=src_fmt,
                category=cat,
                reason=reason,
            )
            key = (node_type, node_id)
            if key not in by_key:
                by_key[key] = rec
                order.append(key)
            else:
                # Duplicate id: prefer the front-matter (methodology) copy.
                existing = by_key[key]
                if src_fmt == "front-matter" and existing.source_format != "front-matter":
                    by_key[key] = rec

    for key in order:
        rec = by_key[key]
        plan.nodes.append(rec)
        if rec.category is None:
            plan.unmapped.append(rec)

    # Dual-location deletes: the RULE START block in the domain rules.md copy of
    # each of the 12 canonical-in-methodology rules.
    for rule_id in sorted(DUAL_LOCATION_RULES):
        for filepath in files:
            if "methodology" in filepath.parts:
                continue
            if filepath.name != "rules.md":
                continue
            text = filepath.read_text(encoding="utf-8")
            if re.search(rf"<!--\s*RULE START:\s*{re.escape(rule_id)}\s*-->", text):
                plan.dual_deletes.append((rule_id, filepath))

    return plan


# --- SKL-PROC-DEBUG-001 node text --------------------------------------------

SKL_PROC_DEBUG_001 = """\
---
skill_id: SKL-PROC-DEBUG-001
node_type: Skill
domain: process
severity: high
scope: task
category: CAT-PROC-001
trigger: "When a bug is reported, a test fails unexpectedly, an error is observed at runtime, or a fix attempt has already failed and the agent is reaching for another guess."
statement: "Hold the runtime-debug lens: gather evidence before forming a fix, trace backward from the failure to the exact diverging boundary, and cite root-cause evidence in the same response as any proposed fix. After three failed fixes, stop patching and question the architecture."
rationale: "Symptom-patching is the canonical debug-mode failure: the agent guesses, the guess masks the symptom, and the real cause survives. Naming the discipline as a runtime lens (the Skill sibling of the PBK-PROC-DEBUG-001 playbook and the ENF-PROC-DEBUG-001 advisory rule) lets retrieval surface the evidence-first reminder at the moment a fix is being proposed."
tags: [debugging, evidence-first, process, root-cause, runtime]
confidence: peer-reviewed
authority: human
last_validated: 2026-06-12
staleness_window: 365
evidence: peer-reviewed
always_on: false
source_attribution: "writ-native"
source_commit: null
edges:
  - { target: PBK-PROC-DEBUG-001, type: TEACHES }
  - { target: ENF-PROC-DEBUG-001, type: TEACHES }
  - { target: TEC-PROC-ROOTCAUSE-001, type: DEMONSTRATES }
  - { target: TEC-PROC-HYPOTHESIS-001, type: DEMONSTRATES }
---

# Skill: Runtime debugging lens

Natural language: when something is broken at runtime, do not reach for a fix. Reach for evidence first. This skill is the runtime sibling of the systematic-debugging playbook (`PBK-PROC-DEBUG-001`) and the advisory evidence-cite rule (`ENF-PROC-DEBUG-001`).

## When this applies

A bug is reported. A test fails unexpectedly. An error or wrong value is observed at runtime. A fix attempt has already failed and you are about to guess again. The skill applies the moment a fix is being *proposed*, not only when one is being written.

## The lens

1. **Evidence before fix.** Read the failure: the exception, the traceback, the wrong value, the reproducer. State what the evidence shows before naming a cause.
2. **Trace backward.** From the failure point, walk up the call stack one boundary at a time. Find the exact boundary where expected and actual diverge. That boundary is the locus, not the symptom site.
3. **Cite in the same response.** Any proposed fix carries its root-cause evidence in the same message. "Fix X" without "because the evidence shows Y" is a guess.

## Red flag thoughts (indicators of violation)

- "Let me just try changing X and see."
- "Quick fix for now, investigate later."
- "It's probably this."
- "Emergency, skip the process."

## The 3-fix rule

Three failed fix attempts means the problem is architectural, not tactical. Stop patching. Re-examine the design before attempting fix number four.
"""


# --- Field injection (idempotent, --apply only) ------------------------------

def _inject_frontmatter_category(text: str, category: str) -> tuple[str, bool]:
    """Add `category: <CAT>` to a YAML front-matter block if absent.

    Inserts the line just before the closing `---` of the front-matter so it
    stays inside the block. Returns (new_text, changed).
    """
    m = re.match(r"^(---\n)(.*?\n)(---\n)(.*)", text, re.DOTALL)
    if not m:
        return text, False
    open_d, body, close_d, rest = m.group(1), m.group(2), m.group(3), m.group(4)
    if re.search(r"^category:\s*\S", body, re.MULTILINE):
        return text, False
    body = body + f"category: {category}\n"
    return open_d + body + close_d + rest, True


def _inject_rule_block_category(text: str, rule_id: str, category: str) -> tuple[str, bool]:
    """Add `**Category**: <CAT>` inside a RULE START block if absent.

    Inserts after the `**Domain**:` line of that block (idempotent). Returns
    (new_text, changed).
    """
    start = re.search(rf"<!--\s*RULE START:\s*{re.escape(rule_id)}\s*-->", text)
    if not start:
        return text, False
    end = re.search(rf"<!--\s*RULE END:\s*{re.escape(rule_id)}\s*-->", text[start.end():])
    if not end:
        return text, False
    block_start = start.end()
    block_end = block_start + end.start()
    block = text[block_start:block_end]
    if re.search(r"^\*\*Category\*\*:", block, re.MULTILINE):
        return text, False
    dom = re.search(r"^(\*\*Domain\*\*:.*)$", block, re.MULTILINE)
    if dom:
        new_block = (
            block[: dom.end()]
            + f"\n**Category**: {category}"
            + block[dom.end():]
        )
    else:
        new_block = f"\n**Category**: {category}\n" + block
    return text[:block_start] + new_block + text[block_end:], True


def _delete_rule_block(text: str, rule_id: str) -> tuple[str, bool]:
    """Surgically remove one RULE START..RULE END block (plus a trailing `---`
    separator and surrounding blank lines) from a rules.md file, leaving every
    other block intact. Returns (new_text, changed)."""
    start = re.search(rf"<!--\s*RULE START:\s*{re.escape(rule_id)}\s*-->", text)
    if not start:
        return text, False
    end = re.search(rf"<!--\s*RULE END:\s*{re.escape(rule_id)}\s*-->", text)
    if not end or end.start() < start.start():
        return text, False
    block_end = end.end()
    # Absorb a following separator line (--- and surrounding blank lines).
    trailing = re.match(r"[ \t]*\n(?:[ \t]*\n)*[ \t]*---[ \t]*\n", text[block_end:])
    if trailing:
        block_end += trailing.end()
    else:
        # Absorb a single trailing newline so we don't leave a blank gap.
        nl = re.match(r"[ \t]*\n", text[block_end:])
        if nl:
            block_end += nl.end()
    # Trim a redundant leading blank line left behind.
    block_start = start.start()
    return text[:block_start] + text[block_end:], True


def _category_node_text(category_id: str, name: str, routes: list[str], parent: str | None) -> str:
    routes_yaml = "[" + ", ".join(routes) + "]"
    parent_yaml = parent if parent is not None else "null"
    return (
        "---\n"
        f"category_id: {category_id}\n"
        "node_type: Category\n"
        f"name: \"{name}\"\n"
        f"routes: {routes_yaml}\n"
        f"parent: {parent_yaml}\n"
        f"description: \"{name} category.\"\n"
        "domain: routing\n"
        "scope: global\n"
        f"trigger: \"Routing metadata for the {name} category; not retrievable.\"\n"
        f"statement: \"Members of {category_id} surface via routes {routes_yaml}.\"\n"
        "rationale: \"Categories carry routing metadata so retrieval routing is graph data, not hardcoded exclusion (Phase 0 Move-B).\"\n"
        "confidence: peer-reviewed\n"
        "authority: human\n"
        "last_validated: 2026-06-12\n"
        "staleness_window: 365\n"
        "evidence: peer-reviewed\n"
        f"# Auto-generated Category node (Phase 0 Wave D).\n"
        "---\n\n"
        f"# Category: {name}\n\n"
        f"Routing category `{category_id}`. Routes: {', '.join(routes)}. "
        f"Parent: {parent if parent else '(root)'}.\n"
    )


def apply_plan(plan: Plan) -> None:
    """Mutate disk: write Category + SKL nodes, inject categories, delete dual blocks."""
    # 1. Author the 22 Category node files.
    for category_id, name, routes, parent in CATEGORY_DEFS:
        path = METHODOLOGY / f"{category_id}.md"
        path.write_text(_category_node_text(category_id, name, routes, parent), encoding="utf-8")
        print(f"[write] {path.relative_to(REPO_ROOT)}")

    # 2. Author SKL-PROC-DEBUG-001.
    skl_path = METHODOLOGY / "SKL-PROC-DEBUG-001.md"
    skl_path.write_text(SKL_PROC_DEBUG_001, encoding="utf-8")
    print(f"[write] {skl_path.relative_to(REPO_ROOT)}")

    # 3. Inject category into the canonical copy of every mapped node.
    #    Front-matter methodology files: edit frontmatter. Rule-block (rules.md)
    #    nodes that are NOT dual-location: edit the block. Dual-location rules
    #    are handled by their methodology frontmatter copy (the rules.md block
    #    is deleted in step 4), so skip injecting into the to-be-deleted block.
    cat_by_id = {rec.node_id: rec.category for rec in plan.nodes if rec.category}
    fm_edits: dict[Path, str] = {}
    block_edits: dict[Path, str] = {}

    # Dual-location rules: their canonical surviving home is the methodology
    # front-matter file (the rules.md block is deleted in step 4) -- regardless
    # of which copy dedup kept. Inject category into that front-matter file
    # explicitly, so the survivor always carries a category.
    for rule_id in DUAL_LOCATION_RULES:
        cat = cat_by_id.get(rule_id)
        if cat is None:
            continue
        mpath = METHODOLOGY / f"{rule_id}.md"
        if not mpath.exists():
            continue
        cur = fm_edits.get(mpath)
        if cur is None:
            cur = mpath.read_text(encoding="utf-8")
        new_text, _ = _inject_frontmatter_category(cur, cat)
        fm_edits[mpath] = new_text

    for rec in plan.nodes:
        if rec.category is None or rec.node_id in DUAL_LOCATION_RULES:
            continue  # dual-location handled above
        if rec.source_format == "front-matter":
            cur = fm_edits.get(rec.filepath)
            if cur is None:
                cur = rec.filepath.read_text(encoding="utf-8")
            new_text, _ = _inject_frontmatter_category(cur, rec.category)
            fm_edits[rec.filepath] = new_text
        else:
            cur = block_edits.get(rec.filepath)
            if cur is None:
                cur = rec.filepath.read_text(encoding="utf-8")
            new_text, _ = _inject_rule_block_category(cur, rec.node_id, rec.category)
            block_edits[rec.filepath] = new_text

    for path, new_text in fm_edits.items():
        path.write_text(new_text, encoding="utf-8")
        print(f"[category] {path.relative_to(REPO_ROOT)}")
    for path, new_text in block_edits.items():
        path.write_text(new_text, encoding="utf-8")
        print(f"[category] {path.relative_to(REPO_ROOT)}")

    # 4. Surgically delete dual-location RULE START blocks from rules.md copies.
    del_by_file: dict[Path, list[str]] = {}
    for rule_id, path in plan.dual_deletes:
        del_by_file.setdefault(path, []).append(rule_id)
    for path, rule_ids in del_by_file.items():
        text = path.read_text(encoding="utf-8")
        for rule_id in rule_ids:
            text, changed = _delete_rule_block(text, rule_id)
            if changed:
                print(f"[delete-block] {rule_id} from {path.relative_to(REPO_ROOT)}")
        path.write_text(text, encoding="utf-8")


# --- Reporting ----------------------------------------------------------------

def report(plan: Plan) -> None:
    total = len(plan.nodes)
    mapped = sum(1 for n in plan.nodes if n.category is not None)
    print("=" * 70)
    print("PHASE 0 WAVE D -- CATEGORY MIGRATION DRY RUN")
    print("=" * 70)
    print(f"(a) Total nodes seen: {total}   mapped: {mapped}   unmapped: {total - mapped}")
    print()

    print("(b) Per-category count table:")
    counts: dict[str, int] = {}
    for rec in plan.nodes:
        key = rec.category or "<UNMAPPED>"
        counts[key] = counts.get(key, 0) + 1
    # Stable order: defined categories first, then any extras.
    ordered = [c[0] for c in CATEGORY_DEFS] + ["<UNMAPPED>"]
    for cat in ordered:
        if cat in counts:
            print(f"    {cat:<28} {counts[cat]:>4}")
    for cat in sorted(counts):
        if cat not in ordered:
            print(f"    {cat:<28} {counts[cat]:>4}  (UNEXPECTED)")
    print(f"    {'TOTAL':<28} {sum(counts.values()):>4}")
    print()

    print(f"(c) UNMAPPED nodes: {len(plan.unmapped)}")
    for rec in plan.unmapped:
        print(f"    {rec.node_id} [{rec.node_type}] domain='{rec.domain}' "
              f"({rec.filepath.relative_to(REPO_ROOT)}) -- {rec.reason}")
    print()

    print(f"(d) Dual-location RULE START blocks that --apply would DELETE: {len(plan.dual_deletes)}")
    for rule_id, path in plan.dual_deletes:
        print(f"    {rule_id:<24} <- {path.relative_to(REPO_ROOT)}")
    print()

    # (e) SKL-PROC-DEBUG-001 parse + validate (write to a temp, parse, validate).
    print("(e) SKL-PROC-DEBUG-001 parse/validate:")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "SKL-PROC-DEBUG-001.md"
        tmp.write_text(SKL_PROC_DEBUG_001, encoding="utf-8")
        try:
            parsed = parse_nodes_from_file(tmp)
            assert len(parsed) == 1, f"expected 1 node, got {len(parsed)}"
            node = parsed[0]
            model = validate_parsed_node(node)
            print(f"    PARSES: yes (node_type={node['node_type']}, "
                  f"skill_id={node.get('skill_id')})")
            print(f"    VALIDATES: yes ({type(model).__name__})")
        except Exception as exc:
            print(f"    FAILED: {exc}")

    # Also validate every Category node text parses/validates.
    cat_ok = 0
    cat_fail: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        for category_id, name, routes, parent in CATEGORY_DEFS:
            tmp = Path(td) / f"{category_id}.md"
            tmp.write_text(_category_node_text(category_id, name, routes, parent), encoding="utf-8")
            try:
                parsed = parse_nodes_from_file(tmp)
                validate_parsed_node(parsed[0])
                cat_ok += 1
            except Exception as exc:
                cat_fail.append(f"{category_id}: {exc}")
    print(f"    Category nodes parse/validate: {cat_ok}/{len(CATEGORY_DEFS)}")
    for f in cat_fail:
        print(f"      FAIL {f}")
    print()

    # (f) CROSS_REF_ALLOWLIST location.
    print("(f) CROSS_REF_ALLOWLIST location:")
    hits = []
    for py in (REPO_ROOT / "writ").rglob("*.py"):
        if "CROSS_REF_ALLOWLIST" in py.read_text(encoding="utf-8"):
            hits.append(py)
    for py in (REPO_ROOT / "scripts").rglob("*.py"):
        if py.name == "migrate_phase0_categories.py":
            continue
        if "CROSS_REF_ALLOWLIST" in py.read_text(encoding="utf-8"):
            hits.append(py)
    test_hits = []
    for py in (REPO_ROOT / "tests").rglob("*.py"):
        if "CROSS_REF_ALLOWLIST" in py.read_text(encoding="utf-8"):
            test_hits.append(py)
    if hits:
        print("    Lives in SOURCE files (--apply would remove the entry):")
        for h in hits:
            print(f"      {h.relative_to(REPO_ROOT)}")
    else:
        print("    Lives ONLY in test file(s); NOT a source file.")
        print("    --apply will NOT touch tests. The SKL-PROC-DEBUG-001 entry must")
        print("    be removed from the test allowlist by hand:")
        for h in test_hits:
            print(f"      {h.relative_to(REPO_ROOT)}")
    print()
    print("=" * 70)
    print("DRY RUN COMPLETE -- no files modified. Run with --apply to write.")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 0 Wave D category migration.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="report only (default)")
    group.add_argument("--apply", action="store_true", help="write changes to disk")
    args = parser.parse_args()

    plan = build_plan()

    if args.apply:
        apply_plan(plan)
        print("\nAPPLY COMPLETE.")
    else:
        report(plan)


if __name__ == "__main__":
    main()
