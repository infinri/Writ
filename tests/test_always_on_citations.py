"""The plan gate called the always-on rules it had just injected "hallucinated".

Reproduced live: `## Rules Applied` cited six IDs copied verbatim from the session's own
`=== ALWAYS-ACTIVE RULES ===` block, and the gate answered

    plan.md validation failed: hallucinated rule IDs in ## Rules Applied:
    ENF-COMMS-OUTPUT-001, ENF-CTX-003, ENF-PROC-TDD-001, ENF-PROC-VERIFY-001,
    ENF-SEC-001, ENF-TEST-001. Only cite rules from the injected WRIT RULES block.

A rejection SPENDS the gate token, so this cost the user an extra approval turn. Citing an
always-on rule is the natural thing to do, so it taxes most gated cycles and teaches the
agent to avoid citing the rules the project considers most important.

CAUSE (writ/server/routes/query.py, channel 2): the prompt bundle has three channels.
Broad records its IDs with --add-rules, methodology-companion records its IDs, and always-on
records ONLY a token count. It is the sole channel that injects rules without leaving a
record, and `_validate_phase_a` builds its "legitimate citations" set from that record.

WHY A SEPARATE CACHE FIELD, not `loaded_rule_ids`: that field doubles as the exclude list
for the ranked query. Of the 12 rules in the live work-mode bundle, 7 are `mandatory=true`
(already excluded from the ranked pool at build time, so recording them is inert) but 5 live
in the ranked pool (ENF-COMMS-OUTPUT-001, ENF-COMMS-001, ENF-PROC-DEBUG-001, FRB-COMMS-001,
FRB-COMMS-002). Recording into `loaded_rule_ids` would silently stop those 5 being retrieved
by relevance. `always_on_rule_ids` fixes the citation rejection with zero retrieval impact.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SESSION_HELPER = os.path.join(SKILL_ROOT, "bin", "lib", "writ-session.py")
QUERY_ROUTE = os.path.join(SKILL_ROOT, "writ", "server", "routes", "query.py")

# A real always-on rule and a real ranked rule, for the "widened not disabled" checks.
REAL_ALWAYS_ON = "ENF-PROC-TDD-001"
INVENTED = "TOTALLY-MADE-UP-999"


def _plan(cited: list[str]) -> str:
    """A plan.md that passes every OTHER phase-a check, so only citations are under test."""
    ids = "\n".join(f"- [{rid}] why it applies here." for rid in cited)
    return (
        "# Plan: something\n\n"
        "## Files\n\n"
        "- `writ/example.py` (modify) -- because the thing needs doing.\n\n"
        "## Analysis\n\n"
        "The what and the why, with contracts and integration points.\n\n"
        "## Rules Applied\n\n"
        f"{ids}\n\n"
        "## Capabilities\n\n"
        "- [ ] the behavior is testable\n"
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An isolated project root plus an isolated session cache (TEST-ISOLATE-001)."""
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")  # project-root marker
    return root


@pytest.fixture
def sid() -> str:
    return f"aoc-{uuid.uuid4().hex[:8]}"


def _validate(root, session_id):
    from writ.session.approval_workflow import _validate_phase_a
    return _validate_phase_a(str(root), session_id)


def _write_cache(session_id: str, **fields):
    from writ.session.cache import _read_cache, _write_cache as w
    cache = _read_cache(session_id)
    cache.update(fields)
    w(session_id, cache)


class TestCacheSchema:
    def test_default_cache_carries_the_new_field(self):
        """_default_cache backfills missing keys on read, so a pre-existing session
        must read back an empty list rather than an absent key."""
        from writ.session.cache import _default_cache
        assert _default_cache()["always_on_rule_ids"] == []

    def test_a_session_written_before_the_change_reads_back_the_empty_list(
        self, project, sid
    ):
        from writ.session.cache import _read_cache
        _write_cache(sid, mode="work")
        assert _read_cache(sid)["always_on_rule_ids"] == []


class TestRecordingCommand:
    def test_add_always_on_rules_records_the_ids(self, project, sid):
        from writ.session.cache import _read_cache
        from writ.session.budget_tracking import cmd_update
        cmd_update(sid, ["--add-always-on-rules", json.dumps(["B-002", "A-001"])])
        assert _read_cache(sid)["always_on_rule_ids"] == ["A-001", "B-002"]  # sorted

    def test_it_unions_across_turns_without_duplicates(self, project, sid):
        from writ.session.cache import _read_cache
        from writ.session.budget_tracking import cmd_update
        cmd_update(sid, ["--add-always-on-rules", json.dumps(["A-001", "B-002"])])
        cmd_update(sid, ["--add-always-on-rules", json.dumps(["B-002", "C-003"])])
        assert _read_cache(sid)["always_on_rule_ids"] == ["A-001", "B-002", "C-003"]

    def test_it_leaves_loaded_rule_ids_alone(self, project, sid):
        """The whole reason for a separate field: the ranked query's exclude list
        must not grow just because a rule was always-on injected.

        The first assertion is load-bearing: cmd_update SILENTLY IGNORES an unrecognized
        flag, so without checking that the ID actually landed, this test would pass
        against a tree where --add-always-on-rules does not exist at all.
        """
        from writ.session.cache import _read_cache
        from writ.session.budget_tracking import cmd_update
        cmd_update(sid, ["--add-rules", json.dumps(["RANKED-001"])])
        cmd_update(sid, ["--add-always-on-rules", json.dumps(["ENF-COMMS-001"])])
        cache = _read_cache(sid)
        assert cache.get("always_on_rule_ids") == ["ENF-COMMS-001"], (
            "the flag did nothing; an unrecognized flag is ignored without error"
        )
        assert cache["loaded_rule_ids"] == ["RANKED-001"]
        for phase_ids in cache.get("loaded_rule_ids_by_phase", {}).values():
            assert "ENF-COMMS-001" not in phase_ids


class TestGateAcceptsInjectedAlwaysOnRules:
    def test_citing_an_injected_always_on_rule_is_accepted(self, project, sid):
        """The defect, directly. Red before the fix.

        `loaded_rule_ids` is deliberately non-empty: `_validate_citations` returns no
        hallucinations at all when the available set is empty ("absence cannot be proven
        when nothing was captured"). Without a ranked rule here this test would pass
        against the defect, because the detector would never engage.
        """
        _write_cache(
            sid, mode="work",
            loaded_rule_ids=["RANKED-001"],
            always_on_rule_ids=[REAL_ALWAYS_ON],
        )
        (project / "plan.md").write_text(_plan([REAL_ALWAYS_ON]))
        assert _validate(project, sid) is None

    def test_citing_a_ranked_rule_still_works(self, project, sid):
        """No regression on the path that already worked."""
        _write_cache(sid, mode="work", loaded_rule_ids=["RANKED-001"])
        (project / "plan.md").write_text(_plan(["RANKED-001"]))
        assert _validate(project, sid) is None

    def test_citing_both_kinds_together_is_accepted(self, project, sid):
        _write_cache(
            sid, mode="work",
            loaded_rule_ids=["RANKED-001"],
            always_on_rule_ids=[REAL_ALWAYS_ON],
        )
        (project / "plan.md").write_text(_plan(["RANKED-001", REAL_ALWAYS_ON]))
        assert _validate(project, sid) is None


class TestHallucinationIsStillCaught:
    """The available set is WIDENED; the check must not be disabled."""

    def test_an_invented_id_is_still_rejected(self, project, sid):
        _write_cache(sid, mode="work", always_on_rule_ids=[REAL_ALWAYS_ON])
        (project / "plan.md").write_text(_plan([INVENTED]))
        err = _validate(project, sid)
        assert err is not None and "hallucinated" in err
        assert INVENTED in err

    def test_a_mix_names_only_the_invented_one(self, project, sid):
        _write_cache(sid, mode="work", always_on_rule_ids=[REAL_ALWAYS_ON])
        (project / "plan.md").write_text(_plan([REAL_ALWAYS_ON, INVENTED]))
        err = _validate(project, sid)
        assert err is not None
        assert INVENTED in err
        assert REAL_ALWAYS_ON not in err, (
            "an injected always-on rule must never be reported as hallucinated"
        )

    def test_an_always_on_rule_not_injected_this_session_is_still_rejected(
        self, project, sid
    ):
        """Presence in the corpus is not the test; presence in THIS session's
        injection is. Otherwise the check degrades to 'is it a known rule'."""
        _write_cache(sid, mode="work", always_on_rule_ids=["ENF-COMMS-001"])
        (project / "plan.md").write_text(_plan([REAL_ALWAYS_ON]))
        err = _validate(project, sid)
        assert err is not None and REAL_ALWAYS_ON in err


class TestRouteWiring:
    """A unit-passing recording command is worthless if channel 2 never calls it."""

    def test_channel_two_records_the_injected_ids(self):
        src = open(QUERY_ROUTE).read()
        assert "--add-always-on-rules" in src, (
            "the always-on channel must record its rule IDs, not only its token count"
        )

    def test_channel_two_returns_the_ids_for_the_hook_to_log(self):
        src = open(QUERY_ROUTE).read()
        ao = src.split("Channel 2: always-on")[1].split("Channel 3")[0]
        assert "rule_ids" in ao, "ao_meta must carry rule_ids, as broad_meta and method_meta do"


class TestFrictionEventCarriesRuleIds:
    def test_always_on_inject_logs_which_rules_fired(self):
        """Only a count was recorded, so 'which always-on rules fired' was
        unanswerable from the logs.

        Anchored on the event-dict literal, not on the bare name: the name also appears
        in a comment above the builder, and splitting on it landed this assertion in
        prose instead of the code.
        """
        src = open(os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-rag-inject.sh")).read()
        marker = "'event': 'always_on_inject'"
        assert marker in src, "the always_on_inject event builder moved"
        event = src[src.index(marker):][:400]
        assert "'rule_ids'" in event, (
            "the always_on_inject event must carry the injected rule IDs, not only a count"
        )


class TestEndToEnd:
    """The acceptance check: real injection, then a real gate decision.

    Driven through the actual recording path. A test that writes always_on_rule_ids
    itself would pass even with the wiring absent, which is the gap that let this ship.
    """

    def _daemon_up(self) -> bool:
        try:
            from tests._daemon import _daemon_health
            return _daemon_health() is not None
        except Exception:
            return False

    def test_a_plan_citing_an_injected_rule_passes_the_gate(self, tmp_path):
        if not self._daemon_up():
            pytest.skip("test daemon not running on test port")
        from tests._daemon import _port

        session = f"aoe2e-{uuid.uuid4().hex[:8]}"
        subprocess.run([sys.executable, SESSION_HELPER, "mode", "set", "work", session],
                       capture_output=True)
        try:
            body = json.dumps({
                "session_id": session, "mode": "work",
                "prompt": "add a parameterized query builder",
                "effort": "", "always_on_filter": True,
            })
            r = subprocess.run(
                ["curl", "-s", "-X", "POST", f"http://localhost:{_port()}/prompt-bundle",
                 "-H", "Content-Type: application/json", "-d", body],
                capture_output=True, text=True,
            )
            data = json.loads(r.stdout)
            assert data.get("error") is False, data
            injected = (data.get("ao_meta") or {}).get("rule_ids") or []
            assert injected, "the always-on channel returned no rule IDs"

            from writ.session.cache import _read_cache
            recorded = _read_cache(session).get("always_on_rule_ids") or []
            assert set(injected) <= set(recorded), (
                f"injected but not recorded: {sorted(set(injected) - set(recorded))}"
            )

            root = tmp_path / "e2e"
            root.mkdir()
            (root / "pyproject.toml").write_text("[project]\nname='x'\n")
            (root / "plan.md").write_text(_plan([injected[0]]))
            from writ.session.approval_workflow import _validate_phase_a
            assert _validate_phase_a(str(root), session) is None
        finally:
            subprocess.run([sys.executable, SESSION_HELPER, "clear", session],
                           capture_output=True)
