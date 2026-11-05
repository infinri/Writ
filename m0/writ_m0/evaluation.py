"""Domain-neutral evidence evaluation producing EvaluationRecord documents.

The core knows nothing about code, research or documents. It:

1. checks scope (tenant, project and task) of every evidence item,
2. resolves each item's source through a SourceCatalog port scoped to the
   task's tenant and project (SR-05) and verifies the
   content digest and verbatim excerpt,
3. asks the pack-supplied check for each evidence kind the item claims,
4. marks each TaskSpec evidence requirement satisfied only by items that
   passed 1-3, and
5. treats `human_signoff` as satisfiable only by an authenticated grant.

Producer-claimed `verification_status` is ignored: a model or agent labeling
its own evidence "verified" changes nothing (INV-25). Unavailable verification
yields incomplete or needs_human_review, never pass.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Protocol

from . import canonical, contracts

HUMAN_SIGNOFF = "human_signoff"
# Reviewer types whose verdicts count as verification. Model-assisted review is
# advisory and can never raise an aggregate status to pass.
VERIFYING_REVIEWERS = {"mechanical", "evidence_engine", "human"}


@dataclass(frozen=True)
class Source:
    ref: str
    content: str
    published_at: str | None


class SourceCatalog(Protocol):
    def resolve(self, source_ref: str, tenant_id: str, project_id: str) -> Source | None: ...


@dataclass(frozen=True)
class SignoffGrant:
    """A human signoff already verified by the approval service (see broker.ApprovalService)."""
    grant_id: str
    tenant_id: str
    approver_principal_id: str
    artifact_digest: str


# A pack check returns None when the claim holds, else a short problem string.
KindCheck = Callable[[dict, Source], "str | None"]


class FixtureCatalog:
    """SourceCatalog over a fixture sources.json, visible to exactly one tenant and project."""

    def __init__(self, sources: Mapping[str, Mapping[str, str]], tenant_id: str, project_id: str):
        self._scope = (tenant_id, project_id)
        self._sources = {ref: Source(ref, s["content"], s.get("published_at"))
                         for ref, s in sources.items()}

    def resolve(self, source_ref: str, tenant_id: str, project_id: str) -> Source | None:
        if (tenant_id, project_id) != self._scope:
            return None
        return self._sources.get(source_ref)


def content_digest(text: str) -> str:
    return canonical.DIGEST_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_item(item: dict, task: dict, catalog: SourceCatalog,
                kind_checks: Mapping[str, KindCheck]) -> list[str]:
    problems = []
    if (item["tenant_id"], item["project_id"], item["task_id"]) != (
            task["tenant_id"], task["project_id"], task["task_id"]):
        return ["evidence scope does not match task tenant/project/task"]
    source = catalog.resolve(item["source_ref"], task["tenant_id"], task["project_id"])
    if source is None:
        return ["source does not resolve"]
    if item.get("content_digest") and item["content_digest"] != content_digest(source.content):
        problems.append("content digest mismatch")
    excerpt = item.get("quoted_excerpt")
    if not excerpt:
        problems.append("no verbatim excerpt")
    elif excerpt not in source.content:
        problems.append("excerpt not found in source")
    for kind in item.get("evidence_kinds", []):
        check = kind_checks.get(kind)
        if check is None:
            problems.append(f"no registered check for evidence kind {kind!r}")
            continue
        problem = check(item, source)
        if problem:
            problems.append(f"{kind}: {problem}")
    return problems


def evaluate(*, task: dict, artifact: dict, evidence: Iterable[dict], catalog: SourceCatalog,
             kind_checks: Mapping[str, KindCheck], rubric_id: str, rubric_version: str,
             evaluation_id: str, signoffs: Iterable[SignoffGrant] = ()) -> dict:
    contracts.validate("task-spec", task)
    contracts.validate("artifact", artifact)
    items = [contracts.validate("evidence", e) for e in evidence]

    criteria = []
    verified_kinds: set[str] = set()
    for item in sorted(items, key=lambda e: e["evidence_id"]):
        problems = _check_item(item, task, catalog, kind_checks)
        criteria.append({
            "criterion_id": f"evidence:{item['evidence_id']}",
            "status": "fail" if problems else "pass",
            "evidence_refs": [item["evidence_id"]],
        })
        if not problems:
            verified_kinds.update(item.get("evidence_kinds", []))

    artifact_digest = canonical.digest(artifact)
    signed = any(g.tenant_id == task["tenant_id"] and g.artifact_digest == artifact_digest
                 for g in signoffs)
    for requirement in task["evidence_requirements"]:
        if requirement == HUMAN_SIGNOFF:
            status = "pass" if signed else "unknown"
        else:
            status = "pass" if requirement in verified_kinds else "unknown"
        criteria.append({"criterion_id": f"requirement:{requirement}", "status": status,
                         "evidence_refs": []})

    statuses = {c["status"] for c in criteria}
    if "fail" in statuses:
        overall = "fail"
    elif any(c["status"] == "unknown" and c["criterion_id"] != f"requirement:{HUMAN_SIGNOFF}"
             for c in criteria):
        overall = "incomplete"
    elif "unknown" in statuses:
        overall = "needs_human_review"
    else:
        overall = "pass"

    record = {
        "contract_version": "2.0",
        "evaluation_id": evaluation_id,
        "task_id": task["task_id"],
        "artifact_ref": artifact["artifact_id"],
        "pack_id": task["pack_id"],
        "rubric_id": rubric_id,
        "rubric_version": rubric_version,
        "status": overall,
        "criteria": criteria,
        "evidence_refs": sorted(e["evidence_id"] for e in items),
        "reviewer_type": "evidence_engine",
        "reviewer_id": None,
        "explanation": None,
    }
    return contracts.validate("evaluation-record", record)


def aggregate_status(records: Iterable[dict]) -> str:
    """Combine evaluator records; model-assisted passes never count as verification."""
    records = list(records)
    if any(r["status"] == "fail" for r in records):
        return "fail"
    verifying = [r for r in records if r.get("reviewer_type") in VERIFYING_REVIEWERS]
    if not verifying:
        return "needs_human_review"
    for status in ("incomplete", "needs_human_review"):
        if any(r["status"] == status for r in verifying):
            return status
    return "pass"
