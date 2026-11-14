"""Authenticated broker ingress (SR-01, D-010). Non-production (D-001).

The kernel trusts `principal.verified_by` because it is pure and holds no keys.
That trust is only sound if nothing but a trusted adapter can submit requests.
This module makes that boundary explicit: a trusted adapter seals the complete
canonical ActionRequest after verifying the PrincipalProof and computing the
operation digest itself, and the broker refuses anything without a valid seal
from a registered adapter key. Any change after sealing (principal, roles,
tenant, session, digest, capabilities) invalidates the seal.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from . import canonical, contracts


class IngressRejected(Exception):
    pass


@dataclass(frozen=True)
class SealedRequest:
    request: dict
    adapter_id: str
    seal: str


def _mac(key: bytes, adapter_id: str, request: dict) -> str:
    body = {"adapter_id": adapter_id, "request": request}
    return hmac.new(key, canonical.canonical_bytes(body), hashlib.sha256).hexdigest()


class IngressSealer:
    """Held only by a trusted adapter. Seals requests that adapter produced."""

    def __init__(self, adapter_id: str, key: bytes):
        self.adapter_id = adapter_id
        self._key = key

    def seal(self, request: dict) -> SealedRequest:
        contracts.validate("action-request", request)
        return SealedRequest(request, self.adapter_id, _mac(self._key, self.adapter_id, request))


class IngressVerifier:
    """Held by the broker: the registry of adapters allowed to submit requests."""

    def __init__(self, trusted_keys: dict[str, bytes]):
        self._keys = dict(trusted_keys)

    def verify(self, sealed: object) -> dict:
        if not isinstance(sealed, SealedRequest):
            raise IngressRejected("request is not sealed")
        key = self._keys.get(sealed.adapter_id)
        if key is None:
            raise IngressRejected("adapter not registered")
        if not isinstance(sealed.request, dict) or contracts.violations("action-request", sealed.request):
            raise IngressRejected("sealed request is malformed")
        if not hmac.compare_digest(_mac(key, sealed.adapter_id, sealed.request), sealed.seal):
            raise IngressRejected("seal does not match request")
        # An adapter vouches only for identities it verified itself.
        if sealed.request["principal"]["verified_by"] != sealed.adapter_id:
            raise IngressRejected("adapter vouches for another issuer's principal")
        return sealed.request
