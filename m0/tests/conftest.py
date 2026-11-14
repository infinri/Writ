from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from writ_m0 import contracts, decision, ingress

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
STATE = decision.KernelState(revision=5)
# Test-only ingress key for the fixture adapter identity (SR-01).
INGRESS_ADAPTER = "writ-host-adapter:fixture"
INGRESS_KEY = b"fixture-ingress-key"
SEALER = ingress.IngressSealer(INGRESS_ADAPTER, INGRESS_KEY)


def merge_patch(target, patch):
    """RFC 7396 JSON Merge Patch."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


@pytest.fixture(scope="session")
def golden():
    return contracts.load_fixture("decisions/m0-golden.json")


@pytest.fixture(scope="session")
def bundle():
    return decision.load_bundle(contracts.load_fixture("policy/m0-reference.bundle.json"))


@pytest.fixture
def base_request(golden):
    return copy.deepcopy(golden["base_request"])
