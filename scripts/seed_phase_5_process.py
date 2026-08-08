"""Phase 5 of the public rulebook expansion: Development Lifecycle & Process.

Seeds 10 new PROC-* rules into Neo4j (0 mandatory).

PROC-PLAN-001 and PROC-TEST-001 articulate the workflow policy. The
existing ENF-PROC-PLAN-001, ENF-PROC-TDD-001, and ENF-PROC-BRAIN-001 rules
are retained as the mechanical-enforcement specifics (the HOW) while the
new PROC rules document the WHY at the public-rulebook level.

Idempotent. Re-runs MERGE existing rules with the same rule_id.

Per the public rulebook source: out-of-the-box-rules.md section 11.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _seed_helpers import connect, upsert_rule

TODAY = date.today().isoformat()


def _rule(
    rid: str,
    severity: str,
    scope: str,
    trigger: str,
    statement: str,
    violation: str,
    pass_example: str,
    enforcement: str,
    rationale: str,
) -> dict:
    return {
        "rule_id": rid,
        "domain": "process",
        "severity": severity,
        "scope": scope,
        "trigger": trigger,
        "statement": statement,
        "violation": violation,
        "pass_example": pass_example,
        "enforcement": enforcement,
        "rationale": rationale,
        "mandatory": False,
        "mechanical_enforcement_path": None,
        "confidence": "production-validated",
        "authority": "human",
        "times_seen_positive": 0,
        "times_seen_negative": 0,
        "last_validated": TODAY,
        "evidence": "doc:public-rulebook-2026-05",
        "staleness_window": 365,
        "always_on": False,
        "body": "",
        "source_attribution": "out-of-the-box-rules.md section 11",
        "source_commit": "",
    }


RULES = [
    _rule("PROC-PLAN-001", "high", "component",
        "When starting any task that involves modifying code.",
        "Work mode requires a written plan before any production code is written. The plan covers files to be touched, an analysis of constraints, the rules that apply, and the capabilities to be tested. ENF-PROC-PLAN-001 and ENF-PROC-BRAIN-001 mechanically enforce this for the Writ workflow; the policy itself is universal.",
        "```\n# Developer starts editing main.py directly; no plan written. Code lands\n# scope-creeping into adjacent concerns; reviewer cannot tell what the\n# author intended versus what they touched incidentally.\n```",
        "```\n# plan.md written first. Files: orders/service.py, orders/repo.py.\n# Analysis: existing patterns for tenant scoping; constraint: no schema\n# change. Rules applied: SEC-AUTHZ-TENANT-001, ARCH-LAYER-001.\n# Capabilities: list orders for current tenant; reject cross-tenant access.\n```",
        "ENF-PROC-PLAN-001 (format check) + ENF-PROC-BRAIN-001 (design before code) gates. Code review.",
        "A written plan exposes scope creep before code lands. The plan is also the artifact a reviewer reads first to know what to evaluate against.",
    ),
    _rule("PROC-TEST-001", "high", "component",
        "When implementing production code that is not a one-shot script.",
        "Test skeletons are written and approved before the implementation. Tests carry the contract; the implementation conforms to them. ENF-PROC-TDD-001 mechanically enforces this in the Writ workflow.",
        "```\n# Implementation lands first; tests added afterwards to match what was\n# built. Tests validate the implementation, not the intent.\n```",
        "```\n# tests/test_orders_cancellation.py written first with failing tests.\n# Implementation iterates until tests pass. Tests document intent.\n```",
        "ENF-PROC-TDD-001 (failing test before src/ write) gate. Code review.",
        "Test-first ensures tests describe the contract independent of the implementation. Test-after risks tests that codify whatever the implementation happens to do.",
    ),
    _rule("PROC-REVIEW-001", "medium", "component",
        "When merging a change to a shared branch.",
        "Code is reviewed by at least one other person or a reviewer agent before merge. Auto-merge on green CI is permitted only for trivial mechanical changes (dependency updates, formatter pushes) and is configured per-repo.",
        "```\n# Author merges their own PR moments after opening.\n```",
        "```\n# PR requires a reviewer approval; reviewer reads the diff and the plan;\n# explicitly approves or requests changes.\n```",
        "Repository branch protection rule.",
        "A second pair of eyes catches scope errors, missing tests, and security oversights that the author normalized to. The check is cheap relative to the cost of fixing in production.",
    ),
    _rule("PROC-COMMIT-001", "medium", "component",
        "When writing commit messages.",
        "Commit messages follow a conventional format: a short subject line (`type: subject` or just a clear summary), and a body for non-trivial changes describing the why. Single-line drive-by messages on substantive changes are violations.",
        "```\nfix stuff\n```",
        "```\nfix: reject negative quantities in /api/orders\n\nNegative quantities bypassed the existing balance check and produced\ndouble-credit refunds. Adds SEC-VAL-RANGE-001 guard plus regression test.\n```",
        "Code review.",
        "Commit messages are read by every future engineer and during every incident. The 30 seconds spent writing a clear message saves hours later.",
    ),
    _rule("PROC-BRANCH-001", "low", "component",
        "When creating a feature branch.",
        "Feature branches are named with a ticket/issue reference: `bug/ORD-1234-negative-quantity`, `feat/ORD-1500-tenant-scoping`. Ad-hoc branch names without a tracking reference are violations.",
        "```\nbranch: quick-fix-2\n```",
        "```\nbranch: fix/ORD-1421-tenant-scoping\n```",
        "Repository branch-naming convention. PR template checks.",
        "Traceable branch names link code to issue tracker to release notes. Untraceable names break the audit trail.",
    ),
    _rule("PROC-CHANGELOG-001", "medium", "component",
        "When releasing user-facing changes.",
        "User-facing changes are documented in a changelog or release-notes file. The audience is end users / API consumers / customers, not engineers. Behavior changes, new features, breaking changes, deprecations are all logged.",
        "```\n# CHANGELOG.md last updated 6 months ago; production has shipped 50 features since.\n```",
        "```\n# CHANGELOG.md:\n# ## v2.5.0 (2026-05-10)\n# - Added: tenant scoping on /api/orders.\n# - Fixed: negative-quantity bypass on order creation.\n# - Deprecated: /v1/legacy-orders (sunset 2026-09-01).\n```",
        "Release-process tooling (changesets, conventional-changelog, knope) generates from commits.",
        "Changelogs are the contract with consumers: they can plan integrations from a written record, not from reverse-engineering deploys.",
    ),
    _rule("PROC-DEPLOY-001", "high", "component",
        "When deploying code to production.",
        "Production deploys go through a CI/CD pipeline. Manual deploys (scp, ssh, kubectl apply) are forbidden for production. The pipeline runs tests, builds artifacts, applies migrations, and rolls out via the deployment strategy.",
        "```\n# Engineer SSHes into prod and runs `git pull && systemctl restart`.\n# No record, no rollback path, no test gate.\n```",
        "```\n# `git push origin main` triggers CI; CI runs tests, builds image, applies\n# migrations, deploys to staging, promotes to prod after smoke tests pass.\n```",
        "CI/CD platform (GitHub Actions, GitLab CI, ArgoCD, Spinnaker).",
        "Pipelined deploys are reproducible, gated, and auditable. Manual deploys produce drift, skip checks, and have no rollback.",
    ),
    _rule("PROC-ROLLBACK-001", "high", "component",
        "When designing the deployment system.",
        "Deployment strategy supports rollback within minutes: blue-green, canary, feature flag, or `helm rollback` / `kubectl rollout undo`. A deploy that cannot be reverted produces extended outages on every bad change.",
        "```\n# Deployment overwrites the previous image; rollback requires rebuilding\n# the prior commit, taking minutes-to-hours.\n```",
        "```\n# Blue-green: both versions running; LB switches; rollback is an LB flip.\n# Or: image versions in registry; rollback redeploys the prior tag.\n```",
        "Deployment-platform config review.",
        "Rollback is the most important property of the deployment system. Without it, every deploy is a one-way bet.",
    ),
    _rule("PROC-ENV-001", "medium", "component",
        "When handling production credentials.",
        "Production credentials are never shared in code, chat (Slack, Teams, email), tickets, or other text artifacts. They live exclusively in the secret management service. Sharing a credential is a security incident; the credential is rotated.",
        "```\nSlack DM: 'here is the prod DB password: hunter2'\n```",
        "```\nSlack DM: 'request access via 1Password vault: production-database'\n```",
        "Slack DLP scanning. Security training. Incident response on credential exposure.",
        "Once a credential is in a chat scroll, it lives forever in every chat client, every backup, and every export. Secret managers eliminate the durable exposure.",
    ),
    _rule("PROC-INCIDENT-001", "medium", "component",
        "When an incident occurs in production.",
        "Post-incident review produces action items with owners and deadlines. Action items are tracked to completion; the incident is not closed until they are. Blameless: the review focuses on systemic causes, not individual error.",
        "```\n# Incident retro: 'we'll do better next time'; no tracked actions; same incident next month.\n```",
        "```\n# Postmortem doc: timeline, root cause, action items.\n# Action: 'Add retry budget to upstream call (@alice, due 2026-05-24)'.\n# Tracked in ticket system; reviewed weekly.\n```",
        "Postmortem template + tracking. Incident-management platform (PagerDuty, Incident.io, FireHydrant).",
        "Untracked retro actions guarantee the incident repeats. Tracked actions are the difference between learning and re-learning.",
    ),
]


async def main() -> None:
    db = connect()
    try:
        async with db._driver.session(database=db._database) as session:
            created = updated = 0
            for rule in RULES:
                existed = await upsert_rule(session, rule)
                if existed:
                    updated += 1
                    print(f"UPDATED {rule['rule_id']:30s} {rule['severity']}")
                else:
                    created += 1
                    print(f"CREATED {rule['rule_id']:30s} {rule['severity']}")

            print()
            print(f"Summary: {created} created, {updated} updated.")

            r = await session.run("MATCH (r:Rule) RETURN count(r) AS n")
            print(f"Total rules: {(await r.single())['n']}")
            r = await session.run("MATCH (r:Rule) WHERE r.mandatory = true RETURN count(r) AS n")
            print(f"Mandatory: {(await r.single())['n']}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
