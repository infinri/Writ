"""Simulated host adapters (external-agent mode). Non-production (D-001).

A HostAdapter turns a host-specific tool event into a normalized ActionRequest:

- Identity comes only from a PrincipalProof signed by a trusted issuer and
  bound to the event's session. Principal, tenant or approval claims inside
  the tool input are ignored (T1, INV-02, INV-19).
- Host tool names are mapped per host to capability kinds; an unmapped tool
  becomes `host.unmapped`, which the kernel denies as unknown (INV-13).
- Targets are canonicalized at the adapter: file paths are resolved
  (symlinks included) and confined to the workspace, then expressed as
  `workspace:<tenant>/<project>/<relpath>` (INV-14).
- The operation digest covers the canonical target and content, so two hosts
  describing the same operation produce the same digest (P-01).

This module never imports a model provider: external hosts run their own model
loop and need no Writ inference (INV-22).
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import canonical, contracts
from .decision import iso

UNMAPPED_KIND = "host.unmapped"


class UnverifiedPrincipal(Exception):
    pass


class NormalizationError(ValueError):
    pass


class IssuerKeyring:
    """Trusted authentication boundary (e.g. OS peer credentials) that signs proofs."""

    def __init__(self, issuer: str, key: bytes):
        self.issuer = issuer
        self._key = key

    def _sign(self, body: dict) -> str:
        return hmac.new(self._key, canonical.canonical_bytes(body), hashlib.sha256).hexdigest()

    def issue(self, *, proof_id: str, tenant_id: str, principal_id: str, session_id: str, roles: list[str],
              issued_at: datetime, expires_at: datetime, authn_method: str = "os_peer_credential") -> dict:
        body = {
            "contract_version": "2.0", "proof_id": proof_id, "tenant_id": tenant_id,
            "principal_id": principal_id, "issuer": self.issuer, "authn_method": authn_method,
            "session_id": session_id, "roles": roles, "issued_at": iso(issued_at), "expires_at": iso(expires_at),
        }
        return contracts.validate("principal-proof", body | {"signature": self._sign(body)})

    def verify(self, proof: dict, session_id: str, now: datetime) -> dict:
        if contracts.violations("principal-proof", proof):
            raise UnverifiedPrincipal("malformed proof")
        body = {k: v for k, v in proof.items() if k != "signature"}
        if proof["issuer"] != self.issuer or not hmac.compare_digest(self._sign(body), proof["signature"]):
            raise UnverifiedPrincipal("bad issuer or signature")
        if proof["session_id"] != session_id:
            raise UnverifiedPrincipal("proof bound to a different session")
        if datetime.fromisoformat(proof["expires_at"].replace("Z", "+00:00")) <= now:
            raise UnverifiedPrincipal("proof expired")
        return proof


@dataclass(frozen=True)
class Operation:
    """Host-independent description of what a tool call would do."""
    kind: str
    params: dict


# Each extractor maps one host's raw tool input to an Operation.
Extractor = Callable[[str, dict], Operation]


def _claude_like(tool: str, args: dict) -> Operation:
    if tool == "Write":
        return Operation("file.write", {"path": args.get("file_path"), "content": args.get("content")})
    if tool == "Read":
        return Operation("file.read", {"path": args.get("file_path")})
    if tool == "Bash":
        return Operation("process.execute", {"command": args.get("command")})
    if tool == "WebFetch":
        return Operation("network.egress", {"url": args.get("url")})
    return Operation(UNMAPPED_KIND, {"tool": tool})


def _alt_host(tool: str, args: dict) -> Operation:
    if tool == "fs.write_file":
        return Operation("file.write", {"path": args.get("path"), "content": args.get("data")})
    if tool == "fs.read_file":
        return Operation("file.read", {"path": args.get("path")})
    if tool == "shell.run":
        return Operation("process.execute", {"command": " ".join(args.get("argv", []))})
    if tool == "http.get":
        return Operation("network.egress", {"url": args.get("url")})
    return Operation(UNMAPPED_KIND, {"tool": tool})


CLAUDE_LIKE: Extractor = _claude_like
ALT_HOST: Extractor = _alt_host


class HostAdapter:
    def __init__(self, *, name: str, keyring: IssuerKeyring, workspace: Path, project_id: str,
                 extractor: Extractor):
        self.name = name
        self._keyring = keyring
        self._workspace = workspace.resolve()
        self._project_id = project_id
        self._extract = extractor

    def normalize(self, *, tool: str, args: dict, call_id: str, session_id: str, proof: dict,
                  now: datetime) -> dict:
        verified = self._keyring.verify(proof, session_id, now)
        tenant = verified["tenant_id"]
        op = self._extract(tool, args)
        resource_id, uri, payload = self._canonical(op, tenant)
        request = {
            "contract_version": "2.0",
            "request_id": f"{session_id}:{call_id}",
            "trace_id": session_id,
            "tenant_id": tenant,
            "principal": {"id": verified["principal_id"], "verified_by": verified["issuer"],
                          "authn_method": verified["authn_method"], "roles": verified.get("roles", [])},
            "project_id": self._project_id,
            "session_id": session_id,
            "agent_id": None,
            "parent_session_id": None,
            "idempotency_key": f"{session_id}:{call_id}",
            "expected_state_revision": None,
            "policy_bundle_sha": None,
            "deadline_at": None,
            "action": {
                "kind": op.kind,
                "resource": {"uri": uri, "canonical_resource_id": resource_id},
                "action_digest": canonical.digest({"kind": op.kind, "resource": resource_id, "operation": payload}),
                "attributes": {"host": self.name},
                "requested_capabilities": [],
            },
        }
        return contracts.validate("action-request", request)

    def _scope(self, scheme: str, tenant: str, suffix: str) -> str:
        return f"{scheme}:{tenant}/{self._project_id}/{suffix}"

    def _canonical(self, op: Operation, tenant: str) -> tuple[str | None, str, dict]:
        if op.kind in ("file.write", "file.read"):
            rel = self._confine(op.params.get("path"))
            payload = {"path": rel}
            if op.kind == "file.write":
                content = op.params.get("content")
                if not isinstance(content, str):
                    raise NormalizationError("file.write requires string content")
                payload["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return self._scope("workspace", tenant, rel), f"file://{self._workspace / rel}", payload
        if op.kind == "process.execute":
            command = op.params.get("command")
            if not isinstance(command, str) or not command.strip():
                raise NormalizationError("process.execute requires a command")
            key = canonical.digest({"command": command}).removeprefix(canonical.DIGEST_PREFIX)[:24]
            return self._scope("process", tenant, key), "process:" + key, {"command": command}
        if op.kind == "network.egress":
            parts = urlsplit(op.params.get("url") or "")
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise NormalizationError("network.egress requires an http(s) URL")
            return self._scope("net", tenant, parts.hostname.lower()), parts.geturl(), {"url": parts.geturl()}
        # Unknown tools get no canonical resource; the kernel denies them as unknown first.
        return None, f"host-tool:{op.params.get('tool')}", {"tool": op.params.get("tool")}

    def _confine(self, raw: object) -> str:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise NormalizationError("invalid path")
        candidate = Path(raw) if os.path.isabs(raw) else self._workspace / raw
        resolved = Path(os.path.realpath(candidate))
        try:
            rel = resolved.relative_to(self._workspace)
        except ValueError:
            raise NormalizationError("path escapes the workspace") from None
        if str(rel) == ".":
            raise NormalizationError("path names the workspace root")
        return rel.as_posix()
