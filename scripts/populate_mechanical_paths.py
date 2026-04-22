"""Populate mechanical_enforcement_path on existing mandatory rules.

Phase 1 added the mechanical_enforcement_path field to the Rule model with a
None default. Existing rules in Neo4j predate the field and have it unset.
This script applies the path citations from docs/mandatory-rule-audit.md
to every 'has path' rule so the Phase 2 blocker 'zero mandatory without
mechanical path' is met.

Run AFTER scripts/demote_mandatory_rules.py, which strips the 12 no-viable-
path rules. Remaining 21 has-path + 2 could-have-path rules get their path
populated here.

Usage: .venv/bin/python scripts/populate_mechanical_paths.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from writ.graph.db import Neo4jConnection

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"

# Citations from docs/mandatory-rule-audit.md. Each entry is the shortest
# useful reference the auditor verified.
PATHS: dict[str, str] = {
    # has-path rules
    "ENF-GATE-001":    ".claude/hooks/validate-exit-plan.sh:15-120 (ExitPlanMode deny)",
    "ENF-GATE-002":    "bin/lib/writ-session.py:1372-1523 (advance-phase state machine, Phase B gate)",
    "ENF-GATE-003":    "bin/lib/writ-session.py:1372-1523 (advance-phase state machine, Phase C gate)",
    "ENF-GATE-005":    ".claude/hooks/validate-exit-plan.sh (Phase D output gate)",
    "ENF-GATE-007":    "bin/lib/writ-session.py:1125-1370 (can_write test-skeleton gate)",
    "ENF-GATE-FINAL":  ".claude/hooks/enforce-final-gate.sh",
    "ENF-POST-001":    ".claude/hooks/enforce-final-gate.sh (completion matrix)",
    "ENF-POST-002":    ".claude/hooks/enforce-final-gate.sh (completion matrix)",
    "ENF-POST-003":    "bin/run-analysis.sh (PHPStan level 8)",
    "ENF-POST-004":    "bin/lib/writ-session.py:1125-1370 (test-skeleton gate)",
    "ENF-POST-007":    ".claude/hooks/enforce-final-gate.sh (PHPCS/PHPStan integration)",
    "ENF-PRE-001":     "bin/lib/writ-session.py:1372-1523 (Phase A gate state machine)",
    "ENF-PRE-002":     "bin/lib/writ-session.py:1372-1523 (Phase B gate state machine)",
    "ENF-PRE-003":     "bin/lib/writ-session.py:1372-1523 (Phase C gate state machine)",
    "ENF-PRE-004":     "bin/lib/writ-session.py:1372-1523 (Phase C API-safety state machine)",
    "ENF-CTX-003":     "bin/run-analysis.sh (PHPCS lint)",
    "ENF-CTX-004":     ".claude/hooks/enforce-final-gate.sh (gate-final.approved check)",
    "ENF-SEC-001":     "bin/run-analysis.sh:78 (PHPStan ownership check)",
    "ENF-SYS-001":     ".claude/hooks/enforce-final-gate.sh (concurrency-model completion matrix)",
    "ENF-SYS-004":     ".claude/hooks/enforce-final-gate.sh (hardcoded-values completion matrix)",
    "ENF-SYS-005":     "bin/lib/writ-session.py:1125-1370 (integration-test-stubs gate)",
    # could-have-path rules — hypothetical hook is Phase 2.5 work
    "ENF-GATE-006":    "Phase 2.5 candidate: hook that parses diff and flags multi-layer changes requiring per-slice approval",
    "ENF-POST-005":    "Phase 2.5 candidate: test-file-review hook validates review-comment engagement",
}


async def main(dry_run: bool) -> int:
    db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    try:
        populated = 0
        skipped = 0
        for rid, path in PATHS.items():
            async with db._driver.session(database=db._database) as session:
                check = await session.run(
                    "MATCH (r:Rule {rule_id: $rid}) RETURN r.mandatory AS m, r.mechanical_enforcement_path AS p",
                    rid=rid,
                )
                rec = await check.single()
                if rec is None:
                    skipped += 1
                    print(f"  skip (not in graph): {rid}")
                    continue
                existing = rec["p"]
                if existing and existing.strip():
                    skipped += 1
                    print(f"  skip (already set): {rid} = {existing[:60]}")
                    continue
                if dry_run:
                    print(f"  [DRY RUN] would populate: {rid} = {path}")
                else:
                    await session.run(
                        "MATCH (r:Rule {rule_id: $rid}) SET r.mechanical_enforcement_path = $p",
                        rid=rid, p=path,
                    )
                    print(f"  populated: {rid}")
                populated += 1
        print()
        print(f"Populated: {populated}  Skipped: {skipped}")

        if not dry_run:
            async with db._driver.session(database=db._database) as session:
                q = """
                    MATCH (r:Rule)
                    WHERE r.mandatory = true
                    AND (r.mechanical_enforcement_path IS NULL
                         OR r.mechanical_enforcement_path = '')
                    RETURN r.rule_id AS rule_id ORDER BY rule_id
                """
                result = await session.run(q)
                violations = [rec["rule_id"] async for rec in result]
                print()
                if violations:
                    print(f"BLOCKER NOT MET: {len(violations)} mandatory rules still lack mechanical_enforcement_path:")
                    for v in violations:
                        print(f"  - {v}")
                    return 1
                print("Phase 2 release-blocker 'zero mandatory without mechanical': MET.")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run)))
