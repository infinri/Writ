"""Reference capability broker, approval service and SQLite ledger (non-production, D-001).

The broker is the only path to a governed effect:

0. It accepts only requests sealed by a registered trusted adapter (SR-01).
1. It re-runs the kernel decision at use time with the currently loaded policy
   and state, so a stale preview decision is never honored (T4).
2. REQUIRE_APPROVAL proceeds only with a ledger-recorded grant whose binding
   matches the exact tenant, principal, action, resource, digest and policy
   version, consumed atomically (INV-06, INV-07).
3. Idempotency keys are reserved in the same transaction; a reused key with a
   different digest is refused and a duplicate never dispatches twice (INV-09).
4. The effect adapter receives a MAC-protected permit and refuses to act
   without a valid one (INV-01). Receipts move reserved -> dispatched ->
   observed_*; a crash leaves "dispatched" for reconciliation (INV-08).

There is no API that turns text into a grant. Grants are minted only by
ApprovalService.decide for an AuthenticatedHuman produced by a TrustedChannel.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from . import canonical, contracts
from .ingress import IngressRejected, IngressVerifier, SealedRequest
from .decision import KernelState, LoadedBundle, capability_set, decide, iso, parse_time

PERMIT_TTL = timedelta(seconds=30)


def request_fingerprint(request: dict) -> str:
    """Canonical digest of the complete ActionRequest (SR-10, SR-11).

    Binding the whole request, rather than a list of fields, means a field added to
    the contract later is covered without anyone remembering to add it here.
    """
    return canonical.digest(request)


class BrokerRefusal(Exception):
    def __init__(self, code: str, decision: dict | None = None):
        super().__init__(code)
        self.code = code
        self.decision = decision


class UnauthorizedEffect(Exception):
    """Raised by an effect adapter called without a valid broker permit."""


class SimulatedCrash(BaseException):
    """Test hook: models a process crash after the effect, before the receipt update."""


# ----------------------------------------------------------------- ledger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
  grant_id TEXT PRIMARY KEY, body TEXT NOT NULL, uses INTEGER NOT NULL, max_uses INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (
  tenant_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, action_digest TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL, grant_id TEXT,
  attempt_id TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL,
  PRIMARY KEY (tenant_id, idempotency_key));
CREATE TABLE IF NOT EXISTS consumed_permits (
  permit_id TEXT PRIMARY KEY, consumed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, body TEXT NOT NULL);
"""


class Ledger:
    """Operational store. One connection per thread; writes use BEGIN IMMEDIATE."""

    def __init__(self, path: Path):
        self._path = str(path)
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    def transaction(self):
        return _Tx(self._conn())

    def audit(self, kind: str, body: dict) -> None:
        self._conn().execute("INSERT INTO audit(kind, body) VALUES (?, ?)", (kind, json.dumps(body, sort_keys=True)))

    def audit_kinds(self) -> list[str]:
        return [r[0] for r in self._conn().execute("SELECT kind FROM audit ORDER BY seq")]

    def grant_count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM grants").fetchone()[0]

    def receipt(self, tenant_id: str, key: str) -> dict | None:
        row = self._conn().execute(
            "SELECT body FROM receipts WHERE tenant_id=? AND idempotency_key=?", (tenant_id, key)).fetchone()
        return json.loads(row[0]) if row else None


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


# --------------------------------------------------------------- approvals

@dataclass(frozen=True)
class AuthenticatedHuman:
    principal_id: str
    channel_id: str
    _token: object  # only TrustedChannel can produce a matching token


class TrustedChannel:
    """Simulated authenticated approval UI. Credentials never pass through a model."""

    def __init__(self, channel_id: str, credentials: dict[str, str]):
        self.channel_id = channel_id
        self._credentials = dict(credentials)
        self._token = object()

    def authenticate(self, principal_id: str, credential: str) -> AuthenticatedHuman:
        expected = self._credentials.get(principal_id)
        if expected is None or not hmac.compare_digest(expected, credential):
            raise PermissionError("authentication failed")
        return AuthenticatedHuman(principal_id, self.channel_id, self._token)

    def owns(self, human: AuthenticatedHuman) -> bool:
        return human._token is self._token and human.channel_id == self.channel_id


class ApprovalService:
    def __init__(self, ledger: Ledger, channels: list[TrustedChannel]):
        self._ledger = ledger
        self._channels = {c.channel_id: c for c in channels}

    def decide(self, *, request: dict, decision: dict, human: AuthenticatedHuman, approve: bool,
               now: datetime, ttl: timedelta = timedelta(minutes=10), max_uses: int = 1) -> dict:
        channel = self._channels.get(human.channel_id)
        if channel is None or not channel.owns(human):
            raise PermissionError("approval did not come from a trusted channel")
        if decision["status"] != "require_approval" or not decision["approval_request_reference"]:
            raise PermissionError("decision does not request approval")
        if human.principal_id in (request["principal"]["id"], request.get("agent_id")):
            raise PermissionError("requester cannot approve its own action")
        action = request["action"]
        grant = contracts.validate("approval-grant", {
            "contract_version": "2.0",
            "grant_id": "grant:" + uuid.uuid4().hex,
            "approval_request_id": decision["approval_request_reference"],
            "tenant_id": request["tenant_id"],
            "approver_principal_id": human.principal_id,
            "approval_channel": human.channel_id,
            "requester_principal_id": request["principal"]["id"],
            "action_kind": action["kind"],
            "canonical_resource_id": action["resource"]["canonical_resource_id"],
            "action_digest": action["action_digest"],
            "requested_capabilities": capability_set(request),
            "policy_bundle_sha": decision["policy_bundle_sha"],
            "nonce": secrets.token_hex(16),
            "issued_at": iso(now),
            "expires_at": iso(now + ttl),
            "max_uses": max_uses,
            "verdict": "approved" if approve else "denied",
            "channel_signature": None,
        })
        with self._ledger.transaction() as tx:
            tx.execute("INSERT INTO grants VALUES (?, ?, 0, ?)", (grant["grant_id"], json.dumps(grant), max_uses))
        self._ledger.audit("approval_decided", {"grant_id": grant["grant_id"], "verdict": grant["verdict"]})
        return grant


# ------------------------------------------------------------------ broker

class EffectAdapter(Protocol):
    def apply(self, permit: dict, request: dict) -> str: ...


class PermitSigner:
    """Holds the broker key. Effect adapters receive only verify/consume capabilities.

    Permits are single use (SR-07): `consume` records the permit id in the ledger
    before the effect runs, so a captured permit cannot drive a second effect,
    including through a restarted adapter. Recording first means a crash after
    consume and before the effect loses the attempt rather than repeating it;
    the broker receipt (still `dispatched`) is what reconciliation inspects.
    """

    def __init__(self, ledger: Ledger) -> None:
        self._key = secrets.token_bytes(32)
        self._ledger = ledger

    def sign(self, body: dict) -> str:
        return hmac.new(self._key, canonical.canonical_bytes(body), hashlib.sha256).hexdigest()

    def verify(self, permit: dict, now: datetime) -> bool:
        body = {k: v for k, v in permit.items() if k != "broker_mac"}
        if not hmac.compare_digest(self.sign(body), permit.get("broker_mac", "")):
            return False
        return datetime.fromisoformat(permit["expires_at"].replace("Z", "+00:00")) > now

    def consume(self, permit: dict, now: datetime) -> bool:
        if not self.verify(permit, now):
            return False
        with self._ledger.transaction() as tx:
            inserted = tx.execute("INSERT OR IGNORE INTO consumed_permits VALUES (?, ?)",
                                  (permit["permit_id"], iso(now))).rowcount
        return inserted == 1


class Broker:
    def __init__(self, *, ledger: Ledger, bundle_provider: Callable[[], LoadedBundle | None],
                 state_provider: Callable[[], KernelState], signer: PermitSigner, ingress: IngressVerifier,
                 principal_id: str = "writ-broker:local"):
        self._ledger = ledger
        self._ingress = ingress
        self._bundle = bundle_provider
        self._state = state_provider
        self._signer = signer
        self._principal_id = principal_id

    def execute(self, sealed: SealedRequest, effect: EffectAdapter, now: datetime,
                grant_id: str | None = None) -> dict:
        # SR-01: only requests sealed by a registered trusted adapter reach the kernel.
        try:
            request = self._ingress.verify(sealed)
        except IngressRejected as exc:
            self._ledger.audit("refused", {"reason": "INGRESS_UNAUTHENTICATED", "detail": str(exc)})
            raise BrokerRefusal("INGRESS_UNAUTHENTICATED") from None
        bundle = self._bundle()
        decision = decide(request, bundle, self._state(), now)
        status = decision["status"]
        if status not in ("allow", "require_approval"):
            self._ledger.audit("refused", {"request_id": decision["request_id"], "reason": decision["reason_code"]})
            raise BrokerRefusal(decision["reason_code"], decision)
        if status == "require_approval" and grant_id is None:
            self._ledger.audit("refused", {"request_id": request["request_id"], "reason": "APPROVAL_REQUIRED"})
            raise BrokerRefusal("APPROVAL_REQUIRED", decision)

        tenant, key, digest = request["tenant_id"], request["idempotency_key"], request["action"]["action_digest"]
        with self._ledger.transaction() as tx:
            fingerprint = request_fingerprint(request)
            # The grant that actually authorizes this execution; ignored for allow decisions.
            effective_grant = grant_id if status == "require_approval" else None
            existing = tx.execute("SELECT request_fingerprint, grant_id, body FROM receipts "
                                  "WHERE tenant_id=? AND idempotency_key=?", (tenant, key)).fetchone()
            if existing:
                # SR-11: the original receipt belongs only to the identical request under the
                # same grant; anything else reusing the key is a conflict, not a success.
                if (existing[0], existing[1]) != (fingerprint, effective_grant):
                    raise BrokerRefusal("IDEMPOTENCY_CONFLICT", decision)
                return json.loads(existing[2])
            if status == "require_approval":
                self._consume_grant(tx, grant_id, request, decision, now)
            attempt_id = "attempt:" + uuid.uuid4().hex
            receipt = self._receipt(request, decision, attempt_id, "reserved", now)
            tx.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (tenant, key, digest, fingerprint, effective_grant,
                        attempt_id, "reserved", json.dumps(receipt)))

        permit = self._permit(request, decision, grant_id, now)
        self._update(receipt, "dispatched", now)
        try:
            result_digest = effect.apply(permit, request)
        except (SimulatedCrash, KeyboardInterrupt):
            raise  # receipt stays "dispatched"; reconcile() resolves it
        except Exception as exc:  # the effect's own failure is recorded, never swallowed
            return self._update(receipt, "observed_failure", now, notes=type(exc).__name__)
        return self._update(receipt, "observed_success", now, result_digest=result_digest)

    def reconcile(self, now: datetime) -> list[dict]:
        """Mark receipts stranded in 'dispatched' as unknown_reconcile (effect may have happened)."""
        conn = self._ledger._conn()
        rows = conn.execute("SELECT body FROM receipts WHERE status='dispatched'").fetchall()
        return [self._update(json.loads(r[0]), "unknown_reconcile", now,
                             notes="dispatched without observed outcome") for r in rows]

    def _consume_grant(self, tx: sqlite3.Connection, grant_id: str, request: dict, decision: dict,
                       now: datetime) -> None:
        row = tx.execute("SELECT body FROM grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None:
            raise BrokerRefusal("GRANT_UNKNOWN", decision)
        grant = json.loads(row[0])
        # A stored grant that cannot prove what it covers is refused, never partially honored.
        if contracts.violations("approval-grant", grant):
            raise BrokerRefusal("GRANT_MALFORMED", decision)
        action = request["action"]
        expected = {
            "verdict": "approved",
            "approval_request_id": decision["approval_request_reference"],
            "tenant_id": request["tenant_id"],
            "requester_principal_id": request["principal"]["id"],
            "action_kind": action["kind"],
            "canonical_resource_id": action["resource"]["canonical_resource_id"],
            "action_digest": action["action_digest"],
            "requested_capabilities": capability_set(request),
            "policy_bundle_sha": decision["policy_bundle_sha"],
        }
        mismatched = sorted(k for k, v in expected.items() if grant[k] != v)
        if mismatched:
            raise BrokerRefusal("GRANT_MISMATCH:" + ",".join(mismatched), decision)
        if datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00")) <= now:
            raise BrokerRefusal("GRANT_EXPIRED", decision)
        updated = tx.execute("UPDATE grants SET uses = uses + 1 WHERE grant_id=? AND uses < max_uses",
                             (grant_id,)).rowcount
        if updated != 1:
            raise BrokerRefusal("GRANT_EXHAUSTED", decision)

    def _permit(self, request: dict, decision: dict, grant_id: str | None, now: datetime) -> dict:
        action = request["action"]
        body = {
            "contract_version": "2.0",
            "permit_id": "permit:" + uuid.uuid4().hex,
            "decision_id": decision["decision_id"],
            "request_id": request["request_id"],
            "tenant_id": request["tenant_id"],
            "principal_id": request["principal"]["id"],
            "action_kind": action["kind"],
            "canonical_resource_id": action["resource"]["canonical_resource_id"],
            "action_digest": action["action_digest"],
            "request_fingerprint": request_fingerprint(request),
            "policy_bundle_sha": decision["policy_bundle_sha"],
            "issued_at": iso(now),
            # A permit never outlives the request it serves (SR-03).
            "expires_at": iso(min([now + PERMIT_TTL] + (
                [parse_time(request["deadline_at"])] if request.get("deadline_at") else []))),
            "approval_grant_id": grant_id,
        }
        return contracts.validate("broker-permit", body | {"broker_mac": self._signer.sign(body)})

    def _receipt(self, request: dict, decision: dict, attempt_id: str, status: str, now: datetime) -> dict:
        return {
            "contract_version": "2.0",
            "tenant_id": request["tenant_id"],
            "attempt_id": attempt_id,
            "request_id": request["request_id"],
            "decision_id": decision["decision_id"],
            "idempotency_key": request["idempotency_key"],
            "action_digest": request["action"]["action_digest"],
            "request_fingerprint": request_fingerprint(request),
            "status": status,
            "resource_id": request["action"]["resource"]["canonical_resource_id"],
            "external_operation_id": None,
            "observed_at": iso(now),
            "result_digest": None,
            "reconciliation_notes": None,
        }

    def _update(self, receipt: dict, status: str, now: datetime, *, result_digest: str | None = None,
                notes: str | None = None) -> dict:
        receipt = receipt | {"status": status, "observed_at": iso(now),
                             "result_digest": result_digest or receipt.get("result_digest"),
                             "reconciliation_notes": notes}
        contracts.validate("effect-receipt", receipt)
        with self._ledger.transaction() as tx:
            tx.execute("UPDATE receipts SET status=?, body=? WHERE tenant_id=? AND idempotency_key=?",
                       (status, json.dumps(receipt), receipt["tenant_id"], receipt["idempotency_key"]))
        return receipt


class RecordingEffect:
    """Effect adapter that verifies the permit, then records the operation."""

    def __init__(self, signer: PermitSigner, clock: Callable[[], datetime]):
        self._consume = signer.consume
        self._clock = clock
        self.applied: list[str] = []
        self.crash_after_apply = False

    def apply(self, permit: dict | None, request: dict) -> str:
        if not permit:
            raise UnauthorizedEffect("missing broker permit")
        action = request["action"]
        # Match the complete request before consuming, so a mismatched attempt cannot
        # burn a legitimate permit (SR-10). The MAC check inside consume() authenticates
        # the fingerprint compared here.
        if permit.get("request_fingerprint") != request_fingerprint(request):
            raise UnauthorizedEffect("permit does not match the requested operation")
        if not self._consume(permit, self._clock()):
            raise UnauthorizedEffect("invalid, expired or already used broker permit")
        self.applied.append(action["action_digest"])
        if self.crash_after_apply:
            raise SimulatedCrash()
        return canonical.digest({"applied": action["action_digest"]})
