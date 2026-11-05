"""Reference decision oracle: a pure function from (request, bundle, state, time) to Decision.

Non-production (D-001). It fixes the semantics the M1 Rust and Go kernels must
reproduce:

- Evaluation order: request validity, request deadline, policy availability and version pin,
  principal issuer trust, state revision, capability knowledge, resource
  canonical form, resource tenant/project scope, then statements.
- Canonical resource ids are '<scheme>:<tenant>/<project>/<path>'; a request
  can never target another tenant's or project's resource (INV-10, T7).
- Statement precedence: forbid > require_approval > permit > default deny.
  A forbid can never be converted into an approval or allow.
- No hidden inputs: no retry counters, workflow mode, model, host or pack
  identity. Identical inputs give identical decisions (INV-03, INV-04).
- Missing or unverifiable policy fails closed as UNAVAILABLE (INV-05, INV-15).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import canonical, contracts

NO_POLICY_SHA = "none"
DECISION_TTL = timedelta(minutes=5)


class BundleError(ValueError):
    pass


@dataclass(frozen=True)
class LoadedBundle:
    document: dict
    sha: str

    @property
    def known_capabilities(self) -> frozenset[str]:
        return frozenset(self.document["known_capabilities"])


@dataclass(frozen=True)
class KernelState:
    revision: int


def bundle_sha(document: dict) -> str:
    """Digest of the bundle content; the publisher signature is not part of it."""
    return canonical.digest({k: v for k, v in document.items() if k != "publisher_signature"})


def load_bundle(document: dict, pinned_sha: str | None = None) -> LoadedBundle:
    try:
        contracts.validate("policy-bundle", document)
    except contracts.ContractError as exc:
        raise BundleError(str(exc)) from None
    ids = [s["statement_id"] for s in document["statements"]]
    if len(ids) != len(set(ids)):
        raise BundleError("duplicate statement_id")
    unknown = {k for s in document["statements"] for k in s["action_kinds"]} - set(document["known_capabilities"])
    if unknown:
        raise BundleError(f"statements reference unknown capabilities: {sorted(unknown)}")
    sha = bundle_sha(document)
    if pinned_sha is not None and sha != pinned_sha:
        raise BundleError("bundle digest does not match pin")
    return LoadedBundle(document, sha)


def _is_canonical_id(resource_id: str) -> bool:
    """Canonical ids are '<scheme>:<tenant>/<project>/<path>' with no dot or empty segments."""
    scheme, sep, rest = resource_id.partition(":")
    if not sep or not scheme or not rest:
        return False
    segments = rest.split("/")
    return len(segments) >= 3 and all(seg not in ("", ".", "..") for seg in segments) and "\\" not in rest


def _in_scope(resource_id: str, tenant_id: str, project_id: str) -> bool:
    """A resource id names its tenant and project; a request may only target its own."""
    if "/" in tenant_id or "/" in project_id:
        return False
    return resource_id.partition(":")[2].startswith(f"{tenant_id}/{project_id}/")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _statement_applies(stmt: dict, request: dict, kinds: set[str]) -> bool:
    if "tenants" in stmt and request["tenant_id"] not in stmt["tenants"]:
        return False
    if "principal_roles" in stmt and not set(stmt["principal_roles"]) & set(request["principal"].get("roles", [])):
        return False
    if not set(stmt["action_kinds"]) & kinds:
        return False
    if "resource_prefixes" in stmt:
        resource = request["action"]["resource"].get("canonical_resource_id") or ""
        if not any(resource.startswith(prefix) for prefix in stmt["resource_prefixes"]):
            return False
    return True


def capability_set(request: dict) -> list[str]:
    """Canonical (sorted, unique) form of the requested capability set (SR-02)."""
    return sorted(set(request["action"].get("requested_capabilities", [])))


def approval_request_id(request: dict, bundle: LoadedBundle) -> str:
    """Deterministic id binding an approval to the exact operation and policy version."""
    binding = {
        "tenant_id": request["tenant_id"],
        "principal_id": request["principal"]["id"],
        "action_kind": request["action"]["kind"],
        "canonical_resource_id": request["action"]["resource"]["canonical_resource_id"],
        "action_digest": request["action"]["action_digest"],
        "requested_capabilities": capability_set(request),
        "policy_bundle_sha": bundle.sha,
    }
    return "apr:" + canonical.digest(binding).removeprefix(canonical.DIGEST_PREFIX)[:32]


def decide(request: dict, bundle: LoadedBundle | None, state: KernelState, now: datetime) -> dict:
    policy_sha = bundle.sha if bundle else NO_POLICY_SHA
    request_id = request.get("request_id") if isinstance(request, dict) else None
    deadline: datetime | None = None

    def result(status: str, reason: str, matched: list[str] = (), approval_ref: str | None = None) -> dict:
        decision = {
            "contract_version": "2.0",
            "decision_id": "dec:" + canonical.digest(
                {"request": str(request_id), "policy": policy_sha, "revision": state.revision,
                 "status": status, "reason": reason, "at": iso(now)}
            ).removeprefix(canonical.DIGEST_PREFIX)[:32],
            "request_id": request_id if isinstance(request_id, str) and request_id else "unknown",
            "status": status,
            "policy_bundle_sha": policy_sha,
            "reason_code": reason,
            "safe_message": None,
            "matched_rule_ids": sorted(matched),
            "state_revision": state.revision,
            "decided_at": iso(now),
            "permit_reference": None,
            "approval_request_reference": approval_ref,
            "expires_at": iso(min([now + DECISION_TTL] + ([deadline] if deadline else [])))
            if status in ("allow", "require_approval") else None,
        }
        return contracts.validate("decision", decision)

    if contracts.violations("action-request", request):
        return result("invalid", "REQUEST_SCHEMA_INVALID")
    if request.get("deadline_at") is not None:
        deadline = parse_time(request["deadline_at"])
        if deadline <= now:
            return result("invalid", "DEADLINE_EXCEEDED")
    if bundle is None:
        return result("unavailable", "POLICY_UNAVAILABLE")
    pinned = request.get("policy_bundle_sha")
    if pinned is not None and pinned != bundle.sha:
        return result("unavailable", "POLICY_VERSION_MISMATCH")
    if request["principal"]["verified_by"] not in bundle.document["trusted_principal_issuers"]:
        return result("deny", "PRINCIPAL_UNVERIFIED")
    expected = request.get("expected_state_revision")
    if expected is not None and expected != state.revision:
        return result("invalid", "STATE_REVISION_CONFLICT")

    action = request["action"]
    kinds = {action["kind"], *action.get("requested_capabilities", [])}
    if not kinds <= bundle.known_capabilities:
        return result("deny", "UNKNOWN_CAPABILITY")
    resource_id = action["resource"].get("canonical_resource_id")
    if not resource_id or not _is_canonical_id(resource_id):
        return result("invalid", "RESOURCE_NOT_CANONICAL")
    if not _in_scope(resource_id, request["tenant_id"], request["project_id"]):
        return result("deny", "RESOURCE_SCOPE_MISMATCH")

    matching = sorted((s for s in bundle.document["statements"] if _statement_applies(s, request, kinds)),
                      key=lambda s: s["statement_id"])
    forbids = [s for s in matching if s["effect"] == "forbid"]
    if forbids:
        return result("deny", forbids[0]["reason_code"], [s["statement_id"] for s in forbids])
    approvals = [s for s in matching if s["effect"] == "require_approval"]
    permits = [s for s in matching if s["effect"] == "permit"]
    if not approvals and not permits:
        return result("deny", "DEFAULT_DENY")
    # Every capability in the request must be covered by some approval or permit
    # statement; one permitted kind cannot carry an unpermitted one along.
    covered = {k for s in approvals + permits for k in s["action_kinds"]}
    if not kinds <= covered:
        return result("deny", "CAPABILITY_NOT_PERMITTED", [s["statement_id"] for s in approvals + permits])
    if approvals:
        return result("require_approval", approvals[0]["reason_code"],
                      [s["statement_id"] for s in approvals], approval_request_id(request, bundle))
    return result("allow", permits[0]["reason_code"], [s["statement_id"] for s in permits])
