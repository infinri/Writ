"""Two hardening fixes found by the documentation review (2026-10-08).

1. A credential-path refusal never escalates to "ask". /pre-write-check turns a refusal into
   a permission prompt once any gate has refused twice in the session; the credential arm
   ("refused in every mode, before every exemption") must stay a hard refusal, matching the
   Bash gate, which never prompts for a credential path.
2. Gate-token secrets are compared with a timing-safe comparison.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from writ.server import app
from writ.session import gates


def _cache(denial_counts: dict[str, int]) -> dict[str, Any]:
    return {
        "mode": "work",
        "current_phase": "planning",
        "gates_approved": [],
        "denial_counts": denial_counts,
        "remaining_budget": 8000,
        "loaded_rule_ids": [],
    }


def _session(denial_counts: dict[str, int]) -> MagicMock:
    mock = MagicMock()
    mock._read_cache = MagicMock(return_value=_cache(denial_counts))
    mock._write_cache = MagicMock(return_value=None)
    mock.DEFAULT_SESSION_BUDGET = 8000
    mock._can_write_check = MagicMock(side_effect=gates._can_write_check)
    return mock


@pytest_asyncio.fixture()
async def escalated_client():
    """A session whose plan gate has already refused three times."""
    with patch("writ.server.writ_session", _session({"phase-a": 3})):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac


def _payload(file_path: str) -> dict[str, Any]:
    return {
        "session_id": "hardening-credential-sid",
        "file_path": file_path,
        "tool_input": {"file_path": file_path, "content": "x"},
        "skill_dir": "/nonexistent-skill-dir",
    }


class TestCredentialRefusalNeverEscalates:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/tmp/proj/.env", "/tmp/proj/server.key", "/home/u/.ssh/id_rsa"])
    async def test_a_credential_write_is_denied_after_repeated_gate_refusals(self, escalated_client, path):
        body = (await escalated_client.post("/pre-write-check", json=_payload(path))).json()
        assert body["decision"] == "deny", body
        assert body["reason"].startswith("[SEC-CREDENTIAL-WRITE]")

    @pytest.mark.asyncio
    async def test_other_refusals_still_escalate_to_ask(self):
        session = _session({"phase-a": 3})
        session._can_write_check = MagicMock(
            return_value={"can_write": False, "reason": "[ENF-GATE-PLAN] Gate not approved"})
        with patch("writ.server.writ_session", session):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                body = (await ac.post("/pre-write-check", json=_payload("/tmp/proj/src/app.py"))).json()
        assert body["decision"] == "ask", body


class TestTimingSafeSecretComparison:
    def test_gate_token_valid_uses_hmac_compare_digest(self):
        import hmac

        from writ.session.gate_token import gate_token_valid

        real = hmac.compare_digest
        calls: list[tuple[str, str]] = []

        def spy(a, b):
            calls.append((a, b))
            return real(a, b)

        with patch("hmac.compare_digest", side_effect=spy):
            assert gate_token_valid("abc123", "abc123") is True
            assert gate_token_valid("abc123", "abc124") is False
        assert calls, "gate_token_valid must compare secrets with hmac.compare_digest"

    @pytest.mark.parametrize("token,expected", [("", ""), ("", "x"), ("x", ""), ("x", "y"), ("é", "abc")])
    def test_empty_or_different_secrets_never_match(self, token, expected):
        from writ.session.gate_token import gate_token_valid

        assert gate_token_valid(token, expected) is False
