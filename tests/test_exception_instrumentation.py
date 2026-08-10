"""Converted exception handlers record the failure AND keep their fallback.

RED PHASE: the call sites below currently swallow their exception silently, so
every "emits an errors row" assertion fails until they are converted. The paired
"still returns its fallback" assertion passes both before and after -- that is
deliberate: it pins the behavior the conversion must NOT change.

Scope note (narrowed from plan.md after reading each site): the plan listed 28
sites. Reading them showed most are intended fallbacks rather than hidden
defects -- embeddings.py's four OSError handlers are temp-file cleanups inside
blocks that re-raise, cache.py's four are a documented resolution chain whose
third candidate raises FileNotFoundError on every normal turn, and
approval_workflow.py's two skip unreadable files during a glob scan. Converting
those would emit noise, not signal. What remains is 12 sites where silence
genuinely hides a defect; three of them need the anomalous case separated from
the routine one rather than a blanket convert.

Also deferred: pipeline.py:806/:831 (HNSW cache miss and save failure). They
already call `_logger` at debug/warning level, so they are the least silent of
the set, and reaching them requires a full build_pipeline run rather than a unit
call. Tracked as a follow-up rather than tested badly here.

Hermetic: WRIT_LOG_ROOT + WRIT_CACHE_DIR are redirected to tmp_path; no live
Neo4j, no daemon.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from writ.shared.logging import stream_path


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("WRIT_LOG_PROJECT", "instrproj")
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
    return tmp_path


def _errors(project: str = "instrproj") -> list[dict]:
    path = stream_path(project, "errors")
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _components() -> list[str]:
    return [r.get("component") for r in _errors()]


# --- session cache: corrupt cache silently becomes a blank session -----------
# cache.py:203. This is the highest-value site: _default_cache() presents as a
# session that lost its mode and gates, which is the mode=None symptom PR #37
# chased. It must stay fail-soft, but it must say so.


def test_corrupt_session_cache_emits_an_errors_row(tmp_path):
    from writ.session.cache import _cache_path, _read_cache

    Path(_cache_path("sid-corrupt")).write_text("{ this is not json")
    _read_cache("sid-corrupt")
    assert any("cache" in (c or "") for c in _components()), (
        "a corrupt session cache must record why it fell back"
    )


def test_corrupt_session_cache_still_returns_the_default_cache(tmp_path):
    from writ.session.cache import _cache_path, _read_cache

    Path(_cache_path("sid-corrupt2")).write_text("{ this is not json")
    cache = _read_cache("sid-corrupt2")
    assert cache["mode"] is None
    assert cache["gates_approved"] == []


def test_absent_session_cache_emits_no_errors_row(tmp_path):
    """A missing cache is the normal first-turn state, not a failure."""
    from writ.session.cache import _read_cache

    _read_cache("sid-never-written")
    assert _errors() == []


# --- gate categories: the gate silently loses its exclusion list -------------
# gates.py:129


def test_unreadable_gate_categories_emits_an_errors_row(tmp_path):
    from writ.session.gates import _load_categories

    bad = tmp_path / "gate-categories.json"
    bad.write_text("{ not json")
    _load_categories(str(bad))
    assert any("categor" in (c or "") for c in _components()), (
        "losing the exclusion list must be recorded; the gate keeps running without it"
    )


def test_unreadable_gate_categories_still_returns_empty_config(tmp_path):
    from writ.session.gates import _load_categories

    bad = tmp_path / "gate-categories.json"
    bad.write_text("{ not json")
    config = _load_categories(str(bad))
    assert config == {"exclusions": [], "categories": [], "framework_detection": {}}


# --- read gate: a crash is indistinguishable from a legitimate allow ---------
# gates.py:595


def test_can_read_classification_failure_emits_an_errors_row(monkeypatch):
    """Patch inside the guarded block: the gate returns early unless the lens is
    runtime, so the failure has to come from a call the try actually reaches."""
    import writ.session.gates as gates

    def explode(*_a, **_k):
        raise RuntimeError("lens resolution blew up")

    monkeypatch.setattr(gates, "_effective_source_type", explode)
    gates._can_read_code_check(
        "sid-r1", {"tool_name": "Read", "tool_input": {"file_path": "/x/y.py"}}, ""
    )
    assert any("read" in (c or "") for c in _components())


def test_can_read_classification_failure_still_fails_open(monkeypatch):
    import writ.session.gates as gates

    def explode(*_a, **_k):
        raise RuntimeError("classifier blew up")

    monkeypatch.setattr(gates, "_effective_source_type", explode)
    result = gates._can_read_code_check(
        "sid-r2", {"tool_name": "Read", "tool_input": {"file_path": "/x/y.py"}}, ""
    )
    assert result == {"can_read": True, "reason": None}


# --- gate token: anomalous failures only ------------------------------------
# gate_token.py:31 and :44 fire on every normal turn (no token outstanding), so
# only the NON-FileNotFoundError case is a defect worth recording.


def test_absent_gate_token_emits_no_errors_row(tmp_path, monkeypatch):
    from writ.session.gate_token import read_gate_token

    monkeypatch.setattr("writ.session.gate_token.gate_token_path",
                        lambda _sid: str(tmp_path / "no-such-token"))
    assert read_gate_token("sid-t1") == ""
    assert _errors() == [], "an absent token is the normal state, not an error"


def test_unreadable_gate_token_emits_an_errors_row(tmp_path, monkeypatch):
    from writ.session.gate_token import read_gate_token

    token_dir = tmp_path / "token-is-a-dir"
    token_dir.mkdir()
    monkeypatch.setattr("writ.session.gate_token.gate_token_path", lambda _sid: str(token_dir))
    assert read_gate_token("sid-t2") == ""
    assert any("token" in (c or "") for c in _components()), (
        "a present-but-unreadable token is anomalous and must be recorded"
    )


def test_claimed_token_read_failure_emits_an_errors_row(tmp_path, monkeypatch):
    """gate_token.py:74 -- the rename just succeeded, so a read failure is real."""
    import writ.session.gate_token as gt

    # A BOUND token body (secret, gate, plan fingerprint). claim_gate_token checks the
    # binding before it claims, so an unbound one-line file would be refused without ever
    # opening the renamed file -- the read failure this test exists for would not happen.
    src = tmp_path / "writ-gate-token-sid-t3"
    src.write_text("tok\nphase-a\nplanhash123\n")
    monkeypatch.setattr(gt, "gate_token_path", lambda _sid: str(src))

    real_open = open

    def flaky_open(path, *a, **k):
        if ".claiming-" in str(path):
            raise OSError("vanished mid-claim")
        return real_open(path, *a, **k)

    # No monkeypatch.undo() here: pytest shares one monkeypatch instance across a
    # test's fixtures, so undo() would also revert the autouse _hermetic env vars
    # and _components() would then read the wrong log root. flaky_open only fails
    # on .claiming-* paths, so the errors write itself is unaffected.
    monkeypatch.setattr("builtins.open", flaky_open)
    assert gt.claim_gate_token("sid-t3", "tok", gate="phase-a", plan_hash="planhash123") is False
    assert any("token" in (c or "") for c in _components())


def test_absent_gate_token_claim_emits_no_errors_row(tmp_path, monkeypatch):
    """A losing racer is normal contention, not a defect."""
    import writ.session.gate_token as gt

    monkeypatch.setattr(gt, "gate_token_path", lambda _sid: str(tmp_path / "absent-token"))
    assert gt.claim_gate_token("sid-t4", "tok", gate="phase-a", plan_hash="planhash123") is False
    assert _errors() == []


# --- server routes: an error reads downstream as "no rules matched" ---------
# query.py:350 returns 0 and :371 returns {} on any exception, which is
# indistinguishable from a genuinely empty corpus.


class _ExplodingDB:
    """Minimal Neo4jConnection stand-in whose session() raises on use."""

    _database = "neo4j"

    class _Driver:
        def session(self, **_kwargs):
            raise RuntimeError("neo4j unreachable")

    _driver = _Driver()


def test_count_categories_failure_emits_an_errors_row():
    import asyncio

    from writ.server.routes.query import _count_categories

    asyncio.run(_count_categories(_ExplodingDB()))
    assert any("categor" in (c or "") for c in _components()), (
        "a graph failure must not be silently reported as a count of 0"
    )


def test_count_categories_failure_still_returns_zero():
    import asyncio

    from writ.server.routes.query import _count_categories

    assert asyncio.run(_count_categories(_ExplodingDB())) == 0


def test_route_distribution_failure_emits_an_errors_row():
    import asyncio

    from writ.server.routes.query import _route_distribution

    asyncio.run(_route_distribution(_ExplodingDB()))
    assert any("route" in (c or "") for c in _components()), (
        "a graph failure must not be silently reported as an empty distribution"
    )


def test_route_distribution_failure_still_returns_empty_dict():
    import asyncio

    from writ.server.routes.query import _route_distribution

    assert asyncio.run(_route_distribution(_ExplodingDB())) == {}
