"""A gate that has never refused anything has not been shown to be a gate.

Finding 5 of the containment audit (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3).
`writ doctor` reads `subagent_complete`, `hook_execution` and a session cache, and has
never read a single `gate_decision` row, so nothing alarms when a gate stops refusing or
never started. Measured over the live audit stream plus every archive: 23,246 rows across
19 gate names, and three gates that CAN refuse and never have (`debug-code-read` 10,006
decisions, `static-analysis` 2,673, `test-first` 1,039).

TWO ARTIFACTS, AND KEEPING THEM APART IS THE WHOLE DESIGN. This session shipped an
assertion that could not fail twice, both times with the right-hand side computed from
the left, and a check tested against rows it synthesized and read back through the same
reader is exactly that shape. So:

  CAPABILITY comes from the hook SOURCES. `_gate_deny_capability()` scans
  `hooks/scripts/*.sh` for `log_gate_decision "<gate>" "<decision>"` call sites and
  returns a map keyed by gate name.

  BEHAVIOUR comes from the AUDIT STREAM. `_audit_rows("gate_decision")` tallies what
  each gate actually decided.

Neither is computed from the other, and the tests below prove it mechanically: the same
capability with different rows must change the class, and the same rows with different
capability must change the class. `TestTheTwoArtifactsDisagreeAndTheClassificationFollows`
feeds a synthetic hook tree and synthetic rows that DISAGREE, and requires the
classification to follow the disagreement in both directions.

FIVE OUTCOMES, NOT THREE, because the corpus holds two states a three-way split reports
wrongly. `unexercised` exists because four gates with call sites (`handoff`, `design-doc`,
`pending-violations`, `shell-commented-out`) have zero rows: folding them into the alarm
reports a gate nothing has ever asked as a gate that stopped answering. `orphaned` exists
because a renamed gate leaves its old rows in a 365-day stream forever and an alarm on
history is noise.

A REFUSAL IS `deny` OR `ask`, never "anything that is not allow". Three live gates prove
both halves: `irreversible` shows denies and ZERO allows because its arm only logs on
refusal, `bash-egress` and `review-blocking` show only asks because asking IS how they
refuse, and `manual-test-grant` spells `grant`, `error` and `inherit`, none of which is a
refusal. A naive rule misreads all three.

Per TEST-ISOLATE-001: no test here reads this machine's audit stream. Every row is
synthesized by the test that asserts on it, every stream read is against a log root under
`tmp_path`, and the only live artifact any test reads is the tracked hook source tree,
which is the same on every machine.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK_SCRIPTS = REPO / "hooks" / "scripts"

# The five classes, as the plan's table declares them. Literals here rather than imported
# from the check, because a test that read its expected labels out of the code under test
# would agree with any renaming.
REFUSING = "refusing"
NEVER_REFUSED = "never-refused"
UNEXERCISED = "unexercised"
CANNOT_REFUSE = "cannot-refuse"
ORPHANED = "orphaned"
ALL_CLASSES = frozenset({REFUSING, NEVER_REFUSED, UNEXERCISED, CANNOT_REFUSE, ORPHANED})

# Live gates this module names, each for a property that is readable from the hook SOURCE
# alone and is therefore machine-independent.
GATE_IRREVERSIBLE = "irreversible"            # deny only, no allow site
GATE_BASH_EGRESS = "bash-egress"              # ask only
GATE_REVIEW_BLOCKING = "review-blocking"      # ask only
GATE_VENV_SWAP = "bash-venv-swap"             # allow only
GATE_MANUAL_GRANT = "manual-test-grant"       # grant, error, inherit
GATE_DEBUG_READ = "debug-code-read"           # a RUNTIME variable decides
GATE_TEST_FIRST = "test-first"
GATE_STATIC_ANALYSIS = "static-analysis"

# The four gates with call sites and zero rows in the measured corpus. Named because
# `unexercised` exists for them; the rows they are judged against are synthesized here.
UNEXERCISED_GATES = ("handoff", "design-doc", "pending-violations", "shell-commented-out")

# The script that really owns `debug-code-read`, and the one the dispatch attributed it to.
# `writ-read-junk-gate.sh` calls `log_gate_decision` ZERO times: it refuses through
# `emit_deny` and records a `read_blocked` friction event, which is why the fire drill
# declares it with shape="friction_custom". Conflating the two is what made the gate look
# like one that cannot refuse.
DEBUG_READ_OWNER = "writ-debug-code-gate.sh"
NOT_THE_DEBUG_READ_OWNER = "writ-read-junk-gate.sh"


def _doctor():
    from writ.session import doctor

    return doctor


def _seam(name: str):
    """A doctor seam, or a loud skeleton failure naming the plan line that owns it.

    `pytest.fail(..., pytrace=False)` rather than a bare `getattr`, the shape
    `tests/test_corpus_floor.py::_floor` already uses: a missing seam is a missing
    PRODUCTION behaviour and must read as one, not as an AttributeError buried in a
    traceback that looks like a broken test.
    """
    doctor = _doctor()
    fn = getattr(doctor, name, None)
    if fn is None:
        pytest.fail(
            f"skeleton: writ/session/doctor.py has no {name} yet (plan.md ## Files "
            "assigns the gate-refusal-liveness check three new seams there: _stream_rows "
            "as the shared reader _metrics_rows delegates to, _audit_rows for the audit "
            "stream, and _gate_deny_capability for the source-derived capability map)",
            pytrace=False,
        )
    return fn


def _classify(capability: dict, rows: list[dict]) -> dict[str, str]:
    """The pure classifier: two independent artifacts in, one class per gate out."""
    return _seam("_gate_refusal_classes")(capability, rows)


def _capability(scripts_dir: Path) -> dict:
    return _seam("_gate_deny_capability")(scripts_dir=scripts_dir)


def _live_capability() -> dict:
    """The capability map derived from the REAL hook tree.

    Tracked source, identical on every machine, and the only live artifact this module
    reads. The audit stream is never touched.
    """
    cap = _capability(HOOK_SCRIPTS)
    assert cap, (
        "_gate_deny_capability() derived no gate at all from hooks/scripts/, so every "
        "assertion built on it would pass on any tree"
    )
    return cap


def _decisions(capability: dict, gate: str) -> set[str]:
    entry = capability.get(gate)
    assert entry is not None, (
        f"{gate!r} is not in the derived capability map; its call sites are in "
        f"hooks/scripts/ today, so a missing key means the derivation went blind: "
        f"{sorted(capability)}"
    )
    return set(entry["decisions"])


def _scripts(capability: dict, gate: str) -> set[str]:
    return set(capability[gate]["scripts"])


def _row(gate: str, decision: str, **extra) -> dict:
    """One synthetic `gate_decision` row, in the shape common.sh really writes.

    `ts` and nothing else by default, because that is what a REFUSAL row carries.
    MEASURED by triggering four real hooks rather than read off the source: a deny row
    came back as `{"ts": "2026-09-18T00:32:15Z", ..., "decision": "deny"}` with no
    `decided_at` at all, while the buffered allow beside it carried
    `"decided_at": "1789691535"`. `_gd_emit_now` is the synchronous path every deny and
    ask takes (a denial must not wait for a drain), and it writes no decision stamp. So
    `ts` is ISO-8601 and always present, `decided_at` is EPOCH SECONDS AS A STRING and
    present only on the buffered allow path, and a check that required `decided_at`
    would report every refusal in the corpus as dateless.
    """
    row = {
        "event": "gate_decision",
        "gate": gate,
        "decision": decision,
        "reason": "synthetic",
        "target": "",
        "session": "s-synthetic",
        "mode": "work",
        "ts": "2026-01-02T03:04:05Z",
    }
    row.update(extra)
    return row


def _hook(scripts_dir: Path, name: str, *lines: str) -> Path:
    """A synthetic hook script carrying exactly the call sites `lines` spells."""
    scripts_dir.mkdir(parents=True, exist_ok=True)
    path = scripts_dir / name
    body = "\n".join(("#!/usr/bin/env bash", 'source "$WRIT_DIR/bin/lib/common.sh"', *lines))
    path.write_text(body + "\n")
    return path


def _call(gate: str, decision: str) -> str:
    return f'    log_gate_decision "{gate}" "{decision}" "$REASON" "$FILE"'


# --------------------------------------------------------------------------- #
# Capability 2, first half: the capability side comes from the hook SOURCES
# --------------------------------------------------------------------------- #

class TestTheCapabilitySideComesFromTheHookSources:

    def test_a_gate_is_keyed_by_name_with_the_tokens_its_sites_spell(self, tmp_path) -> None:
        _hook(tmp_path, "probe-gate.sh", _call("probe", "deny"), _call("probe", "allow"))
        cap = _capability(tmp_path)
        assert set(cap) == {"probe"}, cap
        assert set(cap["probe"]["decisions"]) == {"deny", "allow"}, cap["probe"]

    def test_the_map_names_the_scripts_the_sites_live_in(self, tmp_path) -> None:
        """A reader triaging an alarm must be able to open the file without re-deriving
        it, and a gate logged by two scripts must name both: the class is about the GATE,
        and one script losing its deny arm while a sibling keeps one is not a defect."""
        _hook(tmp_path, "one.sh", _call("shared", "deny"))
        _hook(tmp_path, "two.sh", _call("shared", "allow"))
        cap = _capability(tmp_path)
        assert set(cap["shared"]["scripts"]) == {"one.sh", "two.sh"}, cap["shared"]

    def test_a_comment_describing_a_deny_is_not_a_call_site(self, tmp_path) -> None:
        """MENTION IS NOT USE, and this one is measured: `writ-debug-code-gate.sh:7`
        says "Emits a deny permissionDecision" in its header, and
        `tests/_inventory.py::_refusal_markers` already classifies on code rather than
        prose for exactly that line."""
        _hook(
            tmp_path, "commented.sh",
            '# log_gate_decision "ghost" "deny" "never runs" ""',
            _call("real", "allow"),
        )
        cap = _capability(tmp_path)
        assert "ghost" not in cap, (
            "a gate name that exists only inside a comment was counted as a call site, "
            f"so the capability side reads prose rather than code: {sorted(cap)}"
        )

    def test_an_unreadable_scripts_directory_is_none_and_an_empty_one_is_a_map(
        self, tmp_path
    ) -> None:
        """The None-versus-empty distinction the plan calls load-bearing, on the
        capability side: None means no source was readable, `{}` means sources were read
        and hold no call site. A check that conflated them would report a machine with
        no hook tree as a machine whose gates all lost their deny arms."""
        empty = tmp_path / "empty"
        empty.mkdir()
        assert _capability(empty) == {}, "a readable but empty tree must be {}, not None"
        assert _capability(tmp_path / "does-not-exist") is None, (
            "an unreadable scripts directory must be None, so the check can say "
            "capability is UNKNOWN rather than claiming every gate lost its deny arm"
        )


class TestTheLiveCapabilityMapIsDerivedFromTheRealHookTree:
    """Anti-vacuity for every live-tree case below, and the derivation that caught the
    dispatch's misattribution."""

    def test_it_holds_the_gates_whose_call_sites_are_in_the_tree_today(self) -> None:
        cap = _live_capability()
        missing = [g for g in (GATE_IRREVERSIBLE, GATE_BASH_EGRESS, GATE_REVIEW_BLOCKING,
                               GATE_VENV_SWAP, GATE_MANUAL_GRANT, GATE_DEBUG_READ,
                               GATE_TEST_FIRST, GATE_STATIC_ANALYSIS)
                   if g not in cap]
        assert missing == [], f"the derivation lost these gates: {missing}"

    def test_debug_code_read_is_owned_by_the_gate_hook_not_the_read_junk_hook(self) -> None:
        """THE DISPATCH'S FIRST CORRECTION, as an executable assertion.
        `writ-read-junk-gate.sh` calls `log_gate_decision` zero times, so attributing
        those 10,006 rows to its observe-defaulting GATE_MODE is what made a live deny
        arm look like a gate that cannot refuse."""
        cap = _live_capability()
        assert DEBUG_READ_OWNER in _scripts(cap, GATE_DEBUG_READ), cap[GATE_DEBUG_READ]
        assert NOT_THE_DEBUG_READ_OWNER not in _scripts(cap, GATE_DEBUG_READ), (
            f"{NOT_THE_DEBUG_READ_OWNER} was credited with a {GATE_DEBUG_READ!r} call "
            f"site it does not have: {cap[GATE_DEBUG_READ]}"
        )

    def test_every_derived_gate_name_is_spelled_at_a_real_call_site(self) -> None:
        """The derivation must read the tree, not a list someone typed."""
        sources = {p.name: p.read_text(errors="replace") for p in HOOK_SCRIPTS.glob("*.sh")}
        for gate, entry in _live_capability().items():
            for script in entry["scripts"]:
                assert f'"{gate}"' in sources.get(script, ""), (
                    f"{gate!r} is credited to {script}, which does not spell it"
                )


# --------------------------------------------------------------------------- #
# Capability 8: the behaviour side reads the WHOLE corpus
# --------------------------------------------------------------------------- #

def _log_root(tmp_path: Path, *, live: list[dict], archives: dict[str, list[dict]]) -> Path:
    """A synthetic per-project log tree: `audit.jsonl` plus `archive/audit-<date>.jsonl`
    generations, gzipped when the name ends in `.gz`."""
    project = tmp_path / "proj"
    (project / "archive").mkdir(parents=True, exist_ok=True)
    (project / "audit.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in live))
    for name, rows in archives.items():
        body = "".join(json.dumps(r) + "\n" for r in rows)
        path = project / "archive" / name
        if name.endswith(".gz"):
            path.write_bytes(gzip.compress(body.encode()))
        else:
            path.write_text(body)
    return project


def _point_at(monkeypatch, project: Path) -> None:
    doctor = _doctor()
    monkeypatch.setattr(doctor, "stream_path", lambda _project, kind: project / f"{kind}.jsonl")


class TestTheBehaviourSideReadsTheWholeAuditCorpus:

    def test_it_reads_the_live_file_the_archive_and_the_gzipped_generation(
        self, tmp_path, monkeypatch
    ) -> None:
        """A gate whose only refusal sits in a gzipped archive must not read as one that
        never refused. The alarm is "has NEVER refused", so reading less than the whole
        corpus manufactures the alarm it exists to raise."""
        project = _log_root(
            tmp_path,
            live=[_row("live-gate", "allow")],
            archives={
                "audit-2026-01-01.jsonl": [_row("old-gate", "deny")],
                "audit-2026-01-02.jsonl.gz": [_row("ancient-gate", "ask")],
            },
        )
        _point_at(monkeypatch, project)
        rows = _seam("_audit_rows")("gate_decision")
        assert rows is not None, "the fixture stream was not readable at all"
        assert {r["gate"] for r in rows} == {"live-gate", "old-gate", "ancient-gate"}, rows

    def test_a_row_of_another_event_kind_is_not_counted(self, tmp_path, monkeypatch) -> None:
        project = _log_root(
            tmp_path,
            live=[_row("g", "deny"), {"event": "hook_execution", "hook_name": "g"}],
            archives={},
        )
        _point_at(monkeypatch, project)
        rows = _seam("_audit_rows")("gate_decision")
        assert [r["event"] for r in rows] == ["gate_decision"], rows

    def test_no_readable_stream_is_none_and_a_readable_empty_one_is_a_list(
        self, tmp_path, monkeypatch
    ) -> None:
        """The distinction that already cost one cycle. None means no stream was
        readable; `[]` means it was read and held no such row. The check reports those
        two states differently, so the reader is never accused of a dead gate by a
        missing file."""
        project = _log_root(tmp_path, live=[], archives={})
        _point_at(monkeypatch, project)
        assert _seam("_audit_rows")("gate_decision") == []
        _point_at(monkeypatch, tmp_path / "nowhere")
        assert _seam("_audit_rows")("gate_decision") is None

    def test_metrics_rows_keeps_its_one_argument_signature(self) -> None:
        """`tests/test_doctor.py::_patch_all_ok` monkeypatches `_metrics_rows` by name as
        `lambda event: []`. The shared body moves to `_stream_rows(stream, event)` and
        `_metrics_rows` delegates, so that patch keeps working; changing its signature
        would break every registry test in that module instead."""
        import inspect

        doctor = _doctor()
        params = list(inspect.signature(doctor._metrics_rows).parameters)
        assert params == ["event"], params
        shared = _seam("_stream_rows")
        assert list(inspect.signature(shared).parameters) == ["stream", "event"], (
            "the shared reader must take the stream first, so _metrics_rows and "
            "_audit_rows differ only in which stream they name"
        )


# --------------------------------------------------------------------------- #
# Capability 1, 2: five outcomes, and the classification follows the disagreement
# --------------------------------------------------------------------------- #

class TestEveryGateLandsInExactlyOneOfTheFiveClasses:

    def test_the_classifier_returns_one_class_per_gate_from_the_declared_set(
        self, tmp_path
    ) -> None:
        _hook(tmp_path, "probe.sh",
              _call("refuses", "deny"), _call("refuses", "allow"),
              _call("quiet", "deny"),
              _call("cannot", "allow"))
        rows = [_row("refuses", "deny"), _row("refuses", "allow"),
                _row("cannot", "deny"), _row("stale", "deny")]
        classes = _classify(_capability(tmp_path), rows)
        assert set(classes) == {"refuses", "quiet", "cannot", "stale"}, classes
        assert set(classes.values()) <= ALL_CLASSES, classes

    def test_each_of_the_five_classes_is_reachable(self, tmp_path) -> None:
        """A classifier that could never emit one of its classes would make the class
        decorative. One fixture, one gate per outcome, asserted BY NAME."""
        _hook(tmp_path, "probe.sh",
              _call("has-refused", "deny"),
              _call("can-but-has-not", "deny"), _call("can-but-has-not", "allow"),
              _call("never-asked", "deny"),
              _call("allow-only", "allow"))
        rows = [_row("has-refused", "deny"),
                _row("can-but-has-not", "allow"),
                _row("allow-only", "allow"),
                _row("renamed-away", "deny")]
        classes = _classify(_capability(tmp_path), rows)
        assert classes == {
            "has-refused": REFUSING,
            "can-but-has-not": NEVER_REFUSED,
            "never-asked": UNEXERCISED,
            "allow-only": CANNOT_REFUSE,
            "renamed-away": ORPHANED,
        }, classes


class TestTheTwoArtifactsDisagreeAndTheClassificationFollows:
    """CAPABILITY governs one axis and BEHAVIOUR the other, and neither is computed from
    the other. The pair below is the mechanical proof: hold one side fixed, change the
    other, and the class must move.
    """

    CAP_SOURCE = (_call("g", "deny"), _call("g", "allow"))

    def test_holding_the_sources_fixed_the_rows_move_the_class(self, tmp_path) -> None:
        _hook(tmp_path, "probe.sh", *self.CAP_SOURCE)
        cap = _capability(tmp_path)
        assert _classify(cap, [_row("g", "allow")]) == {"g": NEVER_REFUSED}
        assert _classify(cap, [_row("g", "allow"), _row("g", "deny")]) == {"g": REFUSING}
        assert _classify(cap, []) == {"g": UNEXERCISED}

    def test_holding_the_rows_fixed_the_sources_move_the_class(self, tmp_path) -> None:
        rows = [_row("g", "allow")]
        capable = tmp_path / "capable"
        incapable = tmp_path / "incapable"
        _hook(capable, "probe.sh", *self.CAP_SOURCE)
        _hook(incapable, "probe.sh", _call("g", "allow"))
        assert _classify(_capability(capable), rows) == {"g": NEVER_REFUSED}
        assert _classify(_capability(incapable), rows) == {"g": CANNOT_REFUSE}

    def test_rows_alone_never_promote_a_gate_into_the_alarm(self, tmp_path) -> None:
        """The other direction of the disagreement: refusal rows for a gate whose sources
        spell no refusing token do NOT make it refusing, and a gate with no call site at
        all is `orphaned` however many rows it has. Behaviour cannot vouch for
        capability, which is what keeps a renamed gate's 365 days of history out of the
        alarm."""
        _hook(tmp_path, "probe.sh", _call("allow-only", "allow"))
        rows = [_row("allow-only", "deny"), _row("gone", "deny")]
        classes = _classify(_capability(tmp_path), rows)
        assert classes["allow-only"] == CANNOT_REFUSE, classes
        assert classes["gone"] == ORPHANED, classes


# --------------------------------------------------------------------------- #
# Capability 6, 7: the refusing token set, and the declared buckets
# --------------------------------------------------------------------------- #

class TestARefusalIsDenyOrAsk:

    def test_the_refusing_set_is_exactly_deny_and_ask(self) -> None:
        assert set(_seam("REFUSING_DECISIONS")) == {"deny", "ask"}, (
            "the refusing set is the harness's own permissionDecision vocabulary; "
            "widening it silently reclassifies every gate"
        )

    def test_a_deny_only_gate_with_no_allows_reads_as_refusing(self, tmp_path) -> None:
        """`irreversible`'s real shape: 41 denies and ZERO allows, because its arm logs
        only on refusal. A rule keyed on "has allows and no denies" reads it correctly,
        and a rule keyed on "has no allows" calls it broken."""
        _hook(tmp_path, "probe.sh", _call(GATE_IRREVERSIBLE, "deny"))
        classes = _classify(_capability(tmp_path), [_row(GATE_IRREVERSIBLE, "deny")])
        assert classes == {GATE_IRREVERSIBLE: REFUSING}, classes

    @pytest.mark.parametrize("gate", [GATE_BASH_EGRESS, GATE_REVIEW_BLOCKING])
    def test_an_ask_only_gate_reads_as_refusing(self, tmp_path, gate) -> None:
        """Asking IS how these two refuse: a deny needs an override, and any override
        this agent could set re-opens the defect being closed."""
        _hook(tmp_path / gate, "probe.sh", _call(gate, "ask"))
        classes = _classify(_capability(tmp_path / gate), [_row(gate, "ask")])
        assert classes == {gate: REFUSING}, classes

    def test_an_allow_never_counts_however_many_there_are(self, tmp_path) -> None:
        _hook(tmp_path, "probe.sh", _call("g", "deny"), _call("g", "allow"))
        rows = [_row("g", "allow") for _ in range(1000)]
        assert _classify(_capability(tmp_path), rows) == {"g": NEVER_REFUSED}


class TestEveryDecisionTokenInTheHookTreeFallsInADeclaredBucket:
    """A new verb added to a hook must redden HERE rather than being silently counted as
    a non-refusal. The buckets are declared by the check; the population is derived from
    the tree.
    """

    def _buckets(self) -> set[str]:
        return (set(_seam("REFUSING_DECISIONS"))
                | set(_seam("NON_REFUSING_DECISIONS"))
                | {_seam("RUNTIME_DECIDED")})

    def _vocabulary(self, capability: dict) -> set[str]:
        tokens: set[str] = set()
        for entry in capability.values():
            tokens |= set(entry["decisions"])
        assert tokens, "no decision token was derived at all"
        return tokens

    def test_every_token_spelled_anywhere_in_the_live_tree_is_declared(self) -> None:
        undeclared = sorted(self._vocabulary(_live_capability()) - self._buckets())
        assert undeclared == [], (
            f"these decision tokens are spelled at a live call site and fall in no "
            f"declared bucket, so the check is guessing what they mean: {undeclared}"
        )

    def test_the_live_vocabulary_holds_the_verbs_that_are_not_refusals(self) -> None:
        """Anti-vacuity, and it is the case the naive rule gets wrong in the other
        direction: `manual-test-grant` spells `grant`, `error` and `inherit`, and
        `dispatch-discipline` spells a variable defaulting to `unknown`. None is a
        refusal, and none is an allow either."""
        vocabulary = self._vocabulary(_live_capability())
        for token in ("grant", "error", "inherit", "unknown"):
            assert token in vocabulary, (
                f"{token!r} is spelled at a live call site but the derivation did not "
                f"see it: {sorted(vocabulary)}"
            )
            assert token in set(_seam("NON_REFUSING_DECISIONS")), (
                f"{token!r} is neither declared a refusal nor declared a non-refusal"
            )

    def test_a_new_verb_in_a_synthetic_hook_is_undeclared(self, tmp_path) -> None:
        """CONDITIONALITY, proved on a copy rather than by editing the real tree: the
        same predicate that passes on today's sources must fail the moment a hook spells
        a verb nobody bucketed."""
        _hook(tmp_path, "probe.sh", _call("g", "quarantine"))
        undeclared = sorted(self._vocabulary(_capability(tmp_path)) - self._buckets())
        assert undeclared == ["quarantine"], (
            "a verb no bucket declares was accepted silently, so the vocabulary check "
            f"cannot see a new one: {undeclared}"
        )


# --------------------------------------------------------------------------- #
# Capability 3, 4, 5: the three classes that must never alarm
# --------------------------------------------------------------------------- #

class TestAGateThatCannotRefuseIsReportedAndNeverAlarms:
    """Proved on the LIVE capability map, because the property is a fact about the hook
    sources and nothing else: these two gates spell no refusing token anywhere.
    """

    @pytest.mark.parametrize("gate", [GATE_VENV_SWAP, GATE_MANUAL_GRANT])
    def test_the_gate_spells_no_refusing_token_at_any_call_site(self, gate) -> None:
        spelled = _decisions(_live_capability(), gate)
        assert not (spelled & set(_seam("REFUSING_DECISIONS"))), (
            f"{gate} now spells a refusing token ({sorted(spelled)}), so it is no longer "
            f"the control this test uses it as; re-derive the class rather than widening "
            f"the assertion"
        )
        assert _seam("RUNTIME_DECIDED") not in spelled, spelled

    @pytest.mark.parametrize("gate", [GATE_VENV_SWAP, GATE_MANUAL_GRANT])
    def test_it_is_classified_cannot_refuse_and_stays_out_of_the_alarm(self, gate) -> None:
        cap = {gate: _live_capability()[gate]}
        classes = _classify(cap, [_row(gate, next(iter(_decisions(cap, gate))))])
        assert classes == {gate: CANNOT_REFUSE}, classes


class TestARuntimeDecisionCountsAsCapableOfRefusing:
    """THE FAIL-LOUD DIRECTION, and the one that matters: the source cannot prove such an
    arm unreachable, so treating uncertainty as "cannot refuse" would excuse exactly the
    gate the dispatch misread.
    """

    def test_a_variable_decision_is_recorded_as_runtime_decided(self, tmp_path) -> None:
        _hook(tmp_path, "probe.sh",
              '    log_gate_decision "g" "$GATE_DECISION" "$REASON" "$SID"')
        assert _seam("RUNTIME_DECIDED") in _decisions(_capability(tmp_path), "g")

    def test_a_defaulted_variable_records_both_the_variable_and_its_default(
        self, tmp_path
    ) -> None:
        """`writ-dispatch-discipline.sh:179` spells `"${EMITTED_DECISION:-unknown}"`. The
        variable is what makes the gate capable; the default is a token the vocabulary
        check still has to bucket."""
        _hook(tmp_path, "probe.sh",
              '    log_gate_decision "g" "${EMITTED_DECISION:-unknown}" "$R" ""')
        spelled = _decisions(_capability(tmp_path), "g")
        assert _seam("RUNTIME_DECIDED") in spelled, spelled
        assert "unknown" in spelled, spelled

    def test_the_live_debug_code_read_gate_is_capable_of_refusing(self) -> None:
        """Its fast-path allow at :67 is a literal and its real decision at :121 is
        `$GATE_DECISION`, set to deny whenever `can-read-code` says so."""
        spelled = _decisions(_live_capability(), GATE_DEBUG_READ)
        assert _seam("RUNTIME_DECIDED") in spelled, spelled

    def test_it_lands_in_the_alarm_rather_than_being_excused(self) -> None:
        """10,006 decisions, zero refusals. With allows only, the gate belongs in the
        alarm class, which is the verdict the dispatch's source reading would have
        excused."""
        cap = {GATE_DEBUG_READ: _live_capability()[GATE_DEBUG_READ]}
        classes = _classify(cap, [_row(GATE_DEBUG_READ, "allow")])
        assert classes == {GATE_DEBUG_READ: NEVER_REFUSED}, classes


class TestAGateWithNoRowsAtAllIsUnexercised:
    """Folding these into the alarm would report a gate nothing has ever asked as a gate
    that stopped answering. Four gates in the measured corpus have call sites and zero
    rows.
    """

    @pytest.mark.parametrize("gate", UNEXERCISED_GATES)
    def test_the_gate_has_a_call_site_in_the_live_tree(self, gate) -> None:
        assert _decisions(_live_capability(), gate), gate

    def test_zero_decisions_of_any_kind_is_unexercised_not_never_refused(self) -> None:
        live = _live_capability()
        cap = {gate: live[gate] for gate in UNEXERCISED_GATES}
        classes = _classify(cap, [])
        assert classes == {gate: UNEXERCISED for gate in UNEXERCISED_GATES}, classes

    def test_one_allow_row_moves_exactly_that_gate_into_the_alarm(self) -> None:
        """The boundary, asserted BY NAME: `unexercised` and `never-refused` differ by a
        single decision of any kind, so the one gate that decided moves and its three
        silent siblings do not."""
        live = _live_capability()
        cap = {gate: live[gate] for gate in UNEXERCISED_GATES}
        moved, *quiet = UNEXERCISED_GATES
        classes = _classify(cap, [_row(moved, "allow")])
        assert classes[moved] == NEVER_REFUSED, classes
        assert [classes[g] for g in quiet] == [UNEXERCISED] * len(quiet), classes


# --------------------------------------------------------------------------- #
# Capability 1, 10: the check itself, its status and its detail
# --------------------------------------------------------------------------- #

def _run_check(monkeypatch, capability, rows):
    doctor = _doctor()
    check = _seam("check_gate_refusal_liveness")
    monkeypatch.setattr(doctor, "_gate_deny_capability", lambda **_kw: capability)
    monkeypatch.setattr(doctor, "_audit_rows", lambda _event: rows)
    return check(doctor.DoctorOptions())


class TestOnlyTheNeverRefusedClassWarns:

    def test_the_check_is_registered_under_its_own_name(self, monkeypatch) -> None:
        result = _run_check(monkeypatch, {"g": {"decisions": ["deny"], "scripts": ["a.sh"]}},
                            [_row("g", "deny")])
        assert result.name == "gate-refusal-liveness", result

    @pytest.mark.parametrize("decisions,rows_decision,expected_ok", [
        (["deny", "allow"], "deny", True),     # refusing
        (["deny", "allow"], "allow", False),   # never-refused: the alarm
        (["allow"], "allow", True),            # cannot-refuse
    ])
    def test_only_a_capable_gate_with_no_refusal_warns(
        self, monkeypatch, decisions, rows_decision, expected_ok
    ) -> None:
        doctor = _doctor()
        result = _run_check(
            monkeypatch,
            {"g": {"decisions": decisions, "scripts": ["a.sh"]}},
            [_row("g", rows_decision)],
        )
        status = doctor.STATUS_OK if expected_ok else doctor.STATUS_WARN
        assert result.status == status, result

    def test_an_unexercised_or_orphaned_gate_is_ok(self, monkeypatch) -> None:
        doctor = _doctor()
        result = _run_check(
            monkeypatch,
            {"quiet": {"decisions": ["deny"], "scripts": ["a.sh"]}},
            [_row("renamed", "deny")],
        )
        assert result.status == doctor.STATUS_OK, result

    def test_the_detail_names_the_members_of_each_class(self, monkeypatch) -> None:
        """NAMES, NOT COUNTS. A detail reading "3 gates never refused" cannot be acted
        on, and it is the count that goes stale silently when the membership changes
        under it."""
        capability = {
            "alarming": {"decisions": ["deny", "allow"], "scripts": ["a.sh"]},
            "healthy": {"decisions": ["deny", "allow"], "scripts": ["b.sh"]},
            "silent": {"decisions": ["deny"], "scripts": ["c.sh"]},
            "toothless": {"decisions": ["allow"], "scripts": ["d.sh"]},
        }
        rows = [_row("alarming", "allow"), _row("healthy", "deny"),
                _row("toothless", "allow"), _row("history", "deny")]
        result = _run_check(monkeypatch, capability, rows)
        for gate in ("alarming", "healthy", "silent", "toothless", "history"):
            assert gate in result.detail, (
                f"{gate!r} is classified but not named in the detail: {result.detail!r}"
            )

    def test_the_alarm_names_the_owning_script_so_it_can_be_opened(
        self, monkeypatch
    ) -> None:
        result = _run_check(
            monkeypatch,
            {"alarming": {"decisions": ["deny", "allow"], "scripts": ["writ-probe.sh"]}},
            [_row("alarming", "allow")],
        )
        assert "writ-probe.sh" in result.detail, result.detail


class TestTheDetailCarriesTheLastRefusalDateAndTheFireDrill:

    def test_a_refusing_gate_carries_the_date_of_its_last_refusal(
        self, monkeypatch
    ) -> None:
        """A gate that refused six months ago and is inert today is not an alarm (six of
        nineteen gates have fewer than ten decisions in total, so any window short enough
        to notice inertness reports them as inert every time nothing dangerous was
        attempted). It is VISIBLE instead: the reader gets the date and rules.
        """
        rows = [
            _row("g", "deny", ts="2025-11-02T10:00:00Z"),
            _row("g", "deny", ts="2026-03-14T09:30:00Z"),
            _row("g", "allow", ts="2026-08-01T00:00:00Z"),
        ]
        result = _run_check(
            monkeypatch, {"g": {"decisions": ["deny", "allow"], "scripts": ["a.sh"]}}, rows)
        assert "2026-03-14" in result.detail, (
            "the detail does not carry the date of the LAST refusal; the most recent "
            f"ALLOW must not be reported as one either: {result.detail!r}"
        )

    def test_the_date_is_read_from_a_refusal_row_that_carries_only_ts(
        self, monkeypatch
    ) -> None:
        """MEASURED, and it corrects the obvious reading. `decided_at` exists only on the
        BUFFERED path, and `_gd_emit_now` (the branch every deny and ask takes, because
        a denial must not wait for a drain) emits no such field. A check that required
        `decided_at` would report every refusal in the corpus as dateless.
        """
        rows = [_row("g", "deny", ts="2026-05-06T00:00:00Z")]
        assert "decided_at" not in rows[0], (
            "this fixture is meant to carry ts only; a decided_at here would make the "
            "case pass for the wrong reason"
        )
        result = _run_check(
            monkeypatch, {"g": {"decisions": ["deny"], "scripts": ["a.sh"]}}, rows)
        assert "2026-05-06" in result.detail, result.detail

    def test_decided_at_wins_when_a_row_carries_both(self, monkeypatch) -> None:
        """The other half, and the recorded keystone: `ts` is the FLUSH time for a
        buffered row, so where both exist the decision time is the honest one.

        `decided_at` is EPOCH SECONDS AS A STRING, which is what
        `bin/lib/writ-flush-events.py` really writes and what a triggered hook really
        emitted; an ISO value here would be a fixture of a row shape that does not
        exist, and the check would be written against it.
        """
        from datetime import datetime, timezone

        epoch = "1751846400"
        decided = datetime.fromtimestamp(int(epoch), tz=timezone.utc).date().isoformat()
        rows = [_row("g", "ask", ts="2026-09-09T00:00:00Z", decided_at=epoch)]
        result = _run_check(
            monkeypatch, {"g": {"decisions": ["ask"], "scripts": ["a.sh"]}}, rows)
        assert decided in result.detail, (decided, result.detail)
        assert "2026-09-09" not in result.detail, (
            "the drain time was reported as the decision time: " + result.detail
        )

    def test_the_detail_names_the_fire_drill_as_the_positive_control(
        self, monkeypatch
    ) -> None:
        """A quiet gate and a dead gate produce identical rows, so the absence has a
        ready innocent explanation, which is exactly when a metric needs a positive
        signal instead. The positive signal is not in the log: the fire drill triggers
        each refusal for real, and the detail has to say so or the reader will treat the
        log's silence as the answer.
        """
        result = _run_check(
            monkeypatch, {"g": {"decisions": ["deny"], "scripts": ["a.sh"]}},
            [_row("g", "deny")])
        assert "firedrill" in result.detail or "fire drill" in result.detail, result.detail


class TestDegradationIsThreeValued:

    def test_no_readable_audit_stream_reports_unmeasured_and_stays_ok(
        self, monkeypatch
    ) -> None:
        """Listing every gate as never-refused on a machine whose log simply is not there
        is the loudest possible false alarm, and it is the failure cycle I already
        shipped once for `hook_execution`."""
        doctor = _doctor()
        result = _run_check(
            monkeypatch, {"g": {"decisions": ["deny"], "scripts": ["a.sh"]}}, None)
        assert result.status == doctor.STATUS_OK, result
        assert "unmeasured" in result.detail.lower(), result.detail

    def test_a_readable_stream_with_no_rows_reports_none_recorded_yet(
        self, monkeypatch
    ) -> None:
        doctor = _doctor()
        result = _run_check(
            monkeypatch, {"g": {"decisions": ["deny"], "scripts": ["a.sh"]}}, [])
        assert result.status == doctor.STATUS_OK, result
        assert "unmeasured" not in result.detail.lower(), (
            "an empty stream was reported as an unreadable one; the two states are "
            f"different and the reader acts on them differently: {result.detail!r}"
        )

    def test_unreadable_hook_sources_warn_that_capability_is_unknown(
        self, monkeypatch
    ) -> None:
        doctor = _doctor()
        result = _run_check(monkeypatch, None, [_row("g", "allow")])
        assert result.status == doctor.STATUS_WARN, result
        assert "capability" in result.detail.lower(), result.detail

    def test_a_capability_map_that_derived_nothing_is_not_reported_as_healthy(
        self, monkeypatch
    ) -> None:
        """ANTI-VACUITY at the check's own boundary. An empty map with rows present means
        the derivation went blind, and "no gate is in the alarm class" is then true and
        worthless. It must not read as ok."""
        doctor = _doctor()
        result = _run_check(monkeypatch, {}, [_row("g", "allow")])
        assert result.status == doctor.STATUS_WARN, result

    def test_a_check_failure_never_raises_out_of_the_check(self, monkeypatch) -> None:
        """ERR-GRACEFUL-001, and `run_all_checks` already isolates each check; this is the
        cheaper guarantee that a malformed row never gets that far."""
        result = _run_check(
            monkeypatch,
            {"g": {"decisions": ["deny"], "scripts": ["a.sh"]}},
            [{"event": "gate_decision"}, {"event": "gate_decision", "gate": "g"}],
        )
        assert result.name == "gate-refusal-liveness", result


# --------------------------------------------------------------------------- #
# Capability 11: every detector is conditional, proved by mutation
# --------------------------------------------------------------------------- #

class TestEachDetectorIsConditionalByMutation:
    """Every mutation is written to a COPY under tmp_path; the real hook tree is never
    edited. The control comes first, because a fixture that could not produce the class
    in the first place would "prove" any mutation works.
    """

    SOURCE = (_call("g", "deny"), _call("g", "allow"))

    def test_the_unmutated_fixture_produces_the_alarm(self, tmp_path) -> None:
        _hook(tmp_path, "probe.sh", *self.SOURCE)
        assert _classify(_capability(tmp_path), [_row("g", "allow")]) == {"g": NEVER_REFUSED}

    def test_deleting_the_deny_call_site_moves_the_gate_to_cannot_refuse_by_name(
        self, tmp_path
    ) -> None:
        """The regression this check exists to catch: a gate quietly loses its deny path
        and its allow rows keep flowing, so every volume metric stays healthy."""
        _hook(tmp_path, "probe.sh", _call("g", "allow"))
        classes = _classify(_capability(tmp_path), [_row("g", "allow")])
        assert classes == {"g": CANNOT_REFUSE}, (
            "the gate kept its refusing class with no deny call site left, so the class "
            f"is not keyed on the sources at all: {classes}"
        )

    def test_adding_one_refusal_row_moves_the_gate_out_of_the_alarm_by_name(
        self, tmp_path
    ) -> None:
        _hook(tmp_path, "probe.sh", *self.SOURCE)
        cap = _capability(tmp_path)
        before = _classify(cap, [_row("g", "allow")])
        after = _classify(cap, [_row("g", "allow"), _row("g", "deny")])
        assert before == {"g": NEVER_REFUSED} and after == {"g": REFUSING}, (before, after)

    def test_one_gate_losing_its_deny_path_does_not_move_its_siblings(
        self, tmp_path
    ) -> None:
        """BY NAME rather than by arithmetic: the map is keyed by gate, so a regression
        in one gate must be readable without diffing a total."""
        _hook(tmp_path, "probe.sh",
              _call("kept", "deny"), _call("kept", "allow"), _call("lost", "allow"))
        classes = _classify(_capability(tmp_path),
                            [_row("kept", "deny"), _row("lost", "allow")])
        assert classes == {"kept": REFUSING, "lost": CANNOT_REFUSE}, classes
