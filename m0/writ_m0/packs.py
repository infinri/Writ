"""Capability Pack registry verification and task instantiation. Non-production (D-001).

A manifest is untrusted data until verified. Verification can only reject;
it never produces authority. Requested tool capabilities are checked for being
known to the policy bundle, not granted: the kernel still decides every action
on policy alone, and the decision function takes no pack input (INV-23, P-16).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import canonical, contracts
from .decision import LoadedBundle

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
# Inputs that would let a task request redirect its own scope or identity.
SCOPE_KEYS = {"tenant_id", "project_id", "principal", "principal_id", "pack_id", "grant", "approval"}


class PackRejected(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class TrustPolicy:
    trusted_publishers: frozenset[str]
    # Publishers whose unsigned manifests are accepted for design-fixture use only.
    unsigned_fixture_publishers: frozenset[str]
    known_model_features: frozenset[str]
    registered_evaluators: frozenset[str]


@dataclass(frozen=True)
class VerifiedPack:
    manifest: dict
    digest: str


def verify(manifest: dict, trust: TrustPolicy, bundle: LoadedBundle) -> VerifiedPack:
    found = contracts.violations("capability-pack", manifest)
    if found:
        raise PackRejected([f"schema: {v}" for v in found])
    reasons = []
    if not SEMVER.match(manifest["version"]):
        reasons.append("version is not semantic")
    publisher = manifest["publisher"]
    if manifest.get("signature_ref") is None:
        if publisher not in trust.unsigned_fixture_publishers:
            reasons.append("unsigned pack from a publisher not allowed unsigned")
    elif publisher not in trust.trusted_publishers:
        reasons.append("publisher not trusted")
    unknown_features = sorted(set(manifest.get("required_model_features", [])) - trust.known_model_features)
    if unknown_features:
        reasons.append(f"unknown model features: {unknown_features}")
    unknown_caps = sorted(set(manifest["tool_capabilities"]) - bundle.known_capabilities)
    if unknown_caps:
        reasons.append(f"unknown tool capabilities: {unknown_caps}")
    unregistered = sorted(e["evaluator_id"] for e in manifest["evaluators"]
                          if e["evaluator_id"] not in trust.registered_evaluators)
    if unregistered:
        reasons.append(f"evaluators without implementation: {unregistered}")
    stray = sorted({w["task_type"] for w in manifest["workflows"]} - set(manifest["task_types"]))
    if stray:
        reasons.append(f"workflows for undeclared task types: {stray}")
    if reasons:
        raise PackRejected(reasons)
    return VerifiedPack(manifest, canonical.digest(manifest))


def instantiate_task(pack: VerifiedPack, *, verified_proof: dict, project_id: str, task_id: str,
                     task_type: str, goal: str, inputs: dict, evidence_requirements: list[str],
                     max_duration_seconds: int) -> dict:
    """Build a TaskSpec whose scope comes from the verified actor, never from the pack or input."""
    manifest = pack.manifest
    if task_type not in manifest["task_types"]:
        raise PackRejected([f"task type not declared by pack: {task_type}"])
    smuggled = sorted(SCOPE_KEYS & set(inputs))
    if smuggled:
        raise PackRejected([f"inputs may not set scope or authority fields: {smuggled}"])
    limits = manifest["limits"]
    task = {
        "contract_version": "2.0",
        "task_id": task_id,
        "tenant_id": verified_proof["tenant_id"],
        "project_id": project_id,
        "pack_id": manifest["pack_id"],
        "task_type": task_type,
        "goal": goal,
        "inputs": inputs,
        "required_artifacts": sorted(a["artifact_type"] for a in manifest["artifacts"]),
        "evidence_requirements": evidence_requirements,
        "limits": {
            "max_duration_seconds": min(max_duration_seconds, limits["max_duration_seconds"]),
            "max_model_calls": limits.get("max_model_calls", 0),
        },
        "workflow_version": manifest["version"],
        "principal_ref": verified_proof["principal_id"],
    }
    return contracts.validate("task-spec", task)
