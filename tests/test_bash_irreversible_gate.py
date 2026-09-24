"""Cycle J skeletons: refuse the destruction that actually happened in this repo.

Maat's item is "an irreversibility class in the Bash gate" for `rm -rf`, `reset --hard`,
`clean -fd`, force pushes and `DROP`/`TRUNCATE`. Neither of the two destructive incidents
here is on that list:

  2026-08-05  invoking `benchmarks/*.py` blind wiped the graph.
  2026-08-08  a `docker exec` at target resolution wiped it again, and the tripwire missed
              it because it watched connections.

46 python files carry `DETACH DELETE` today, `benchmarks/bench_targets.py` among them, and
the gate's only mention of `docker` is `.dockercfg` in a credential path list. So this file
pins the two vectors that leaked, plus git history destruction, which no incident covers but
which is the live exposure while this branch sits 80 commits ahead of `main` and unpushed.

DENY, NOT ASK. Claude Code already prompts for anything outside its allowlist (`ask` and
`deny` are empty in settings.local.json; `allow` has accreted eight-plus `rm` entries). What
that layer cannot do is notice reversibility: one `Bash(git push:*)` entry covers `--force`
forever. The clearance is the user running it themselves with the `!` prefix, which needs no
new grant type and no new state to protect.

MENTION IS NOT USE, and this one is load-bearing for ordinary work: while planning this
cycle I grepped for `DETACH DELETE` three times. A guard that refused those greps would make
the codebase unsearchable, so the read-only escape (`_readonly_inspection`, already in the
gate for the state guard) must apply here too.

FINDING 4 (the containment audit, plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3): the
class above covers git history and the graph, and NOTHING that destroys the record of
what the agent did. Measured against the real hook: `truncate -s 0 var/logs/audit.jsonl`,
`shred -u secrets.txt`, `rm -rf /abs/path` and `psql -c "DROP TABLE users;"` were all
ALLOWED, while the four git forms denied. Two arms close two of the three tiers, and the
third (user-data deletion) is deliberately LEFT ALONE, which is why nothing below asks
for `rm -rf build/` to be refused: across 6,017 real Bash envelopes every `rm` was
scratch cleanup, so a blanket rule is pure false positives.

THE PER-SEGMENT PREDICATE IS THE LOAD-BEARING PIECE. Exactly one real captured command
carries both a destroying verb and a protected artifact: an `rm` on the capture SENTINEL
in one segment while the corpus file is merely LISTED in another. A whole-command AND
refuses it. `TestTheVerbAndTheArtifactMustShareOneSegment` below is the pair that catches
that, and it is reachable ONLY through the irreversibility arm. It cannot be replaced by
tests/test_bash_write_gate.py's `grep writ-grant- var/logs/audit.jsonl | xargs rm`, which
was MEASURED to deny at the gate-state guard about 150 lines earlier and would therefore
pass under the broken whole-command design too.

Per ENF-GATE-007: skeletons written and approved before implementation.
Per TEST-ISOLATE-003: every run gets its own WRIT_FRICTION_LOG and WRIT_CACHE_DIR, and no
test executes any of these commands. They are fed to the hook as strings.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests._inventory import user_directed_phrases
from tests._strace import trace_execve

REPO = Path(__file__).resolve().parent.parent
HOOK_SH = str(REPO / "hooks" / "scripts" / "writ-bash-write-gate.sh")

# Real project files, used because the predicate reads CONTENT rather than matching a path.
DESTRUCTIVE_SCRIPT = "benchmarks/bench_targets.py"   # carries DETACH DELETE
CLEAN_SCRIPT = "writ/graph/migrate.py"               # carries none, and is routine


def _run(cmd: str, tmp_path: Path, sid: str = "irrev-test",
         hook: str = HOOK_SH) -> tuple[dict | None, list[dict]]:
    """Feed one command to the hook. Returns (hookSpecificOutput or None, logged rows).

    `hook` defaults to the REAL gate. The mutation proofs below pass a copy written
    under their own tmp dir; nothing else may override it, and no test writes to the
    real tree.
    """
    friction = tmp_path / "friction.jsonl"
    env = {
        **os.environ,
        "WRIT_CACHE_DIR": str(tmp_path / "cache"),
        "WRIT_FRICTION_LOG": str(friction),
        "WRIT_PORT": "59999",
        "WRIT_NO_AUTOSTART": "1",
    }
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    envelope = json.dumps({"session_id": sid, "tool_name": "Bash",
                           "tool_input": {"command": cmd}})
    proc = subprocess.run(["bash", hook], input=envelope, cwd=str(REPO),
                          capture_output=True, text=True, env=env, timeout=120)
    out = proc.stdout.strip()
    payload = json.loads(out).get("hookSpecificOutput", {}) if out else None
    rows: list[dict] = []
    if friction.exists():
        for line in friction.read_text(errors="replace").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return payload, rows


def _denied(payload: dict | None) -> bool:
    return bool(payload) and payload.get("permissionDecision") == "deny"


def _reason(payload: dict | None) -> str:
    return (payload or {}).get("permissionDecisionReason", "") or ""


# --------------------------------------------------------------------------- #
# Capability 1, 2, 3: incident 2's vector, a container or shell reaching Neo4j
# --------------------------------------------------------------------------- #

class TestGraphDestructionThroughAContainer:

    @pytest.mark.parametrize("cmd", [
        'docker exec writ-neo4j cypher-shell -u neo4j -p x "MATCH (n) DETACH DELETE n"',
        'docker compose exec neo4j cypher-shell "MATCH (n) DETACH DELETE n"',
        'cypher-shell -a bolt://localhost:7687 "MATCH (n) DETACH DELETE n"',
        'docker exec writ-neo4j cypher-shell "DROP CONSTRAINT rule_id_unique"',
        'docker exec writ-neo4j cypher-shell "DROP INDEX rule_text_idx"',
    ])
    def test_a_destructive_cypher_command_is_refused(self, tmp_path, cmd) -> None:
        """THE 2026-08-08 VECTOR. The tripwire in place at the time watched connections,
        so it was structurally blind to this."""
        payload, _rows = _run(cmd, tmp_path)
        assert _denied(payload), f"not refused: {cmd!r} -> {payload!r}"

    def test_lowercase_cypher_is_refused_too(self, tmp_path) -> None:
        """Cypher keywords are case-insensitive, so a guard that only matched uppercase
        would be defeated by typing it in lower case."""
        payload, _rows = _run(
            'docker exec writ-neo4j cypher-shell "match (n) detach delete n"', tmp_path)
        assert _denied(payload)

    def test_an_ordinary_docker_exec_is_allowed(self, tmp_path) -> None:
        """MUST-NOT-REGRESS: the vector is destructive Cypher, not docker."""
        payload, _rows = _run("docker exec writ-neo4j ls /var/lib/neo4j", tmp_path)
        assert not _denied(payload), f"an ordinary docker exec was refused: {payload!r}"

    def test_a_read_only_cypher_query_is_allowed(self, tmp_path) -> None:
        payload, _rows = _run(
            'docker exec writ-neo4j cypher-shell "MATCH (n) RETURN count(n)"', tmp_path)
        assert not _denied(payload), f"a counting query was refused: {payload!r}"


# --------------------------------------------------------------------------- #
# Capability 4, 5, 6: incident 1's vector, a script that wipes when invoked
# --------------------------------------------------------------------------- #

class TestGraphDestructionThroughAScript:

    def test_invoking_a_destructive_project_script_is_refused(self, tmp_path) -> None:
        """THE 2026-08-05 VECTOR, and the reason the predicate reads the FILE: nothing in
        the command text says this script wipes the graph."""
        payload, _rows = _run(f"python3 {DESTRUCTIVE_SCRIPT}", tmp_path)
        assert _denied(payload), f"not refused: {payload!r}"

    @pytest.mark.parametrize("interpreter", ["python", "python3", ".venv/bin/python"])
    def test_every_interpreter_spelling_is_covered(self, tmp_path, interpreter) -> None:
        payload, _rows = _run(f"{interpreter} {DESTRUCTIVE_SCRIPT}", tmp_path)
        assert _denied(payload), f"{interpreter} slipped through: {payload!r}"

    def test_the_same_script_under_pytest_is_allowed(self, tmp_path) -> None:
        """Tests are meant to run, and one of them legitimately wipes the shared graph and
        restores it through migrate.py. Refusing pytest would stop the suite."""
        payload, _rows = _run(f".venv/bin/python -m pytest {DESTRUCTIVE_SCRIPT}", tmp_path)
        assert not _denied(payload), f"a pytest invocation was refused: {payload!r}"

    def test_a_clean_project_script_is_allowed(self, tmp_path) -> None:
        """MUST-NOT-REGRESS, and it is the routine path: migrate.py restores schema and
        carries no destructive statement."""
        payload, _rows = _run(f"python3 {CLEAN_SCRIPT}", tmp_path)
        assert not _denied(payload), f"a clean script was refused: {payload!r}"

    def test_an_inline_interpreter_is_not_double_gated(self, tmp_path) -> None:
        """`python3 -c` has no script argument and is already the gate's third vector; this
        one must not add a second refusal for the same command."""
        payload, _rows = _run('python3 -c "print(1)"', tmp_path)
        assert not _denied(payload), f"python3 -c was refused by this vector: {payload!r}"


# --------------------------------------------------------------------------- #
# Capability 7, 8, 9, 10: git history, the live exposure
# --------------------------------------------------------------------------- #

class TestGitHistoryDestruction:

    @pytest.mark.parametrize("cmd", [
        "git reset --hard HEAD~1",
        "git reset --hard origin/main",
        "git clean -fd",
        "git clean -fdx",
        "git push --force origin fix/bench-budget-scale",
        "git push -f origin fix/bench-budget-scale",
        "git branch -D fix/bench-budget-scale",
        "git tag -d v1.7.0",
    ])
    def test_a_history_destroying_command_is_refused(self, tmp_path, cmd) -> None:
        payload, _rows = _run(cmd, tmp_path)
        assert _denied(payload), f"not refused: {cmd!r} -> {payload!r}"

    @pytest.mark.parametrize("cmd", [
        "git reset --soft HEAD~1",
        "git reset HEAD~1",
        "git clean -n",
        "git push origin fix/bench-budget-scale",
        "git push --force-with-lease origin fix/bench-budget-scale",
        "git branch -d merged-branch",
        "git status",
        "git log --oneline -1",
    ])
    def test_a_reversible_or_ordinary_command_is_allowed(self, tmp_path, cmd) -> None:
        """`--force-with-lease` is the important one: it is the reversible form, and
        refusing it would push people toward the unsafe spelling."""
        payload, _rows = _run(cmd, tmp_path)
        assert not _denied(payload), f"wrongly refused: {cmd!r} -> {payload!r}"


# --------------------------------------------------------------------------- #
# Capability 13, 14: mention is not use, and every refusal is auditable
# --------------------------------------------------------------------------- #

class TestMentionIsNotUse:
    """Load-bearing for ordinary work: planning this cycle needed three greps for
    `DETACH DELETE`. A guard that refused them would make the codebase unsearchable, which
    is the trap the gate-state guard already solved with `_readonly_inspection`.
    """

    @pytest.mark.parametrize("cmd", [
        'grep -rn "DETACH DELETE" writ/',
        'grep -c "MATCH (n) DETACH DELETE n" benchmarks/bench_targets.py',
        'cat benchmarks/bench_targets.py',
        'grep -rn "git reset --hard" docs/',
    ])
    def test_a_read_only_command_naming_destruction_is_allowed(self, tmp_path, cmd) -> None:
        payload, _rows = _run(cmd, tmp_path)
        assert not _denied(payload), f"a read-only inspection was refused: {cmd!r}"

    def test_a_pipeline_that_could_execute_is_still_refused(self, tmp_path) -> None:
        """The escape is for INSPECTION. A command substitution can run anything, which is
        why `_readonly_inspection` rejects `$(`, backticks and `;`."""
        payload, _rows = _run(
            'grep -l "DETACH DELETE" *.py; docker exec writ-neo4j cypher-shell '
            '"MATCH (n) DETACH DELETE n"', tmp_path)
        assert _denied(payload), f"a chained destructive command slipped through: {payload!r}"


class TestEveryRefusalIsAuditableAndActionable:

    DESTRUCTIVE = 'docker exec writ-neo4j cypher-shell "MATCH (n) DETACH DELETE n"'

    def test_the_refusal_names_the_way_through(self, tmp_path) -> None:
        """A refusal the user cannot act on is a deadlock. The clearance here is the user
        running it themselves, so the reason must say so."""
        payload, _rows = _run(self.DESTRUCTIVE, tmp_path)
        assert "!" in _reason(payload), _reason(payload)

    def test_the_refusal_does_not_offer_a_grant(self, tmp_path) -> None:
        """The manual-testing grant means "manual testing", not "destroy this". Offering it
        here would widen a bypass minted for something else."""
        reason = _reason(_run(self.DESTRUCTIVE, tmp_path)[0]).lower()
        assert "manual testing approved" not in reason, reason

    def test_the_refusal_is_logged(self, tmp_path) -> None:
        """A refusal that leaves no record is not auditable, and the fire-drill cycle will
        assert exactly this across every blocking surface."""
        _payload, rows = _run(self.DESTRUCTIVE, tmp_path)
        decisions = [r for r in rows if r.get("event") == "gate_decision"
                     and r.get("decision") == "deny"]
        assert decisions, f"no gate_decision deny row written: {rows!r}"

    def test_the_refusal_names_what_it_matched(self, tmp_path) -> None:
        """An operator has to be able to tell WHY without reading the hook."""
        reason = _reason(_run(self.DESTRUCTIVE, tmp_path)[0])
        assert "DETACH DELETE" in reason or "cypher" in reason.lower(), reason


# =========================================================================== #
# FINDING 4: evidence destruction, and SQL DDL through a database client.
#
# DERIVATION, not a hardcoded list (the shape tests/test_bash_pattern_spelling_
# gate.py already uses for the prefilter matrix): the destroying-verb and
# protected-artifact populations are parsed out of two marker blocks the fix adds
# to the hook, and a probe map keyed by member must equal each derived set, so a
# verb or an artifact added to the hook with no probe reddens instead of sitting
# outside a stale list.
# =========================================================================== #

_VERB_MARK = "DESTROYING VERBS"
_EVIDENCE_MARK = "EVIDENCE DESTRUCTION"

DESTROYING_VERBS_ARRAY = "_IRREV_DESTROYING_VERBS"
PROTECTED_ARTIFACTS_ARRAY = "_IRREV_LOG_ARTIFACTS"

# The DDL alternation the database-client arm carries. Named here rather than
# derived, and the reason is stated instead of hidden: it is a `[[ =~ ]]` regex
# string like `_IRREV_CYPHER_RE`, not a list, so there is no member syntax to
# parse. Its conditionality is proved by MUTATION instead
# (TestEachNewDetectorIsConditionalByMutation narrows it to one branch and the
# other three must go silent), which is the check a derived population would
# have bought.
_DDL_RE_NAME = "_IRREV_DDL_RE"

_BASH_STRING_RE = re.compile(r'"([^"\n]*)"')


def _hook_source() -> str:
    return Path(HOOK_SH).read_text()


def marker_block(source: str, name: str) -> str:
    """The text between `# <name> BEGIN` and `# <name> END`."""
    begin, end = "# %s BEGIN" % name, "# %s END" % name
    assert begin in source, (
        "%r marker not found in hooks/scripts/writ-bash-write-gate.sh; the arm this "
        "population is derived from has not been wrapped with its marker block" % begin
    )
    start = source.index("\n", source.index(begin)) + 1
    assert end in source[start:], "%r marker not found after %r" % (end, begin)
    return source[start:source.index(end, start)]


def declared_members(block: str, array_name: str) -> set[str]:
    """The double-quoted members of `array_name=( ... )` inside `block`."""
    anchor = "%s=(" % array_name
    assert anchor in block, (
        "%r is not declared inside its marker block, so the population cannot be "
        "derived from the hook's own source" % array_name
    )
    start = block.index(anchor) + len(anchor)
    assert ")" in block[start:], "%r is not closed inside its marker block" % array_name
    members = set(_BASH_STRING_RE.findall(block[start:block.index(")", start)]))
    assert members, "%r is declared but holds no member" % array_name
    return members


def destroying_verbs(source: str) -> set[str]:
    return declared_members(marker_block(source, _VERB_MARK), DESTROYING_VERBS_ARRAY)


def protected_artifacts(source: str) -> set[str]:
    return declared_members(marker_block(source, _EVIDENCE_MARK),
                            PROTECTED_ARTIFACTS_ARRAY)


def _derived(fn) -> set[str]:
    """The population, or the EMPTY SET when the marker block is absent.

    IMPORT MUST NOT RAISE (the reason tests/test_bash_pattern_spelling_gate.py gives
    for the same shape): an assertion at module scope becomes a COLLECTION error, which
    aborts the whole session's collection rather than reddening this module. The missing
    block is reported by the derivation tests below, which fail loudly with the same
    message, and by the sentinel parameter every parametrization over an empty
    population yields.
    """
    try:
        return fn(_hook_source())
    except (AssertionError, ValueError):
        return set()


DERIVED_VERBS = _derived(destroying_verbs)
DERIVED_ARTIFACTS = _derived(protected_artifacts)


# One canonical probe per member. Each verb probe names the SAME artifact, so a red
# is about the verb; each artifact probe uses the SAME verb and names exactly one
# artifact token, so a red is about the artifact.
VERB_PROBES: dict[str, str] = {
    "rm": "rm -f var/logs/writ/audit.jsonl",
    "truncate": "truncate -s 0 var/logs/writ/audit.jsonl",
    "shred": "shred -u var/logs/writ/audit.jsonl",
}

ARTIFACT_PROBES: dict[str, str] = {
    "var/logs": "rm -rf var/logs",
    "audit.jsonl": "rm -f /srv/archive/audit.jsonl",
    "friction.jsonl": "rm -f /srv/archive/friction.jsonl",
    "metrics.jsonl": "rm -f /srv/archive/metrics.jsonl",
    "errors.jsonl": "rm -f /srv/archive/errors.jsonl",
    "workflow-friction.log": "rm -f /srv/archive/workflow-friction.log",
    "writ-blackbox.jsonl": "rm -f /srv/archive/writ-blackbox.jsonl",
    "state/writ": "rm -rf ~/.local/state/writ/logs",
}

_UNDERIVED = "<population underived from the hook source>"


def _verb_params() -> list:
    """One parameter per derived verb, never an empty list.

    `_floor_labels()` in tests/test_corpus_floor.py is the shape: a parametrization
    over a population that can be empty must yield a sentinel that FAILS, or the day
    the derivation goes blind every case in the class silently disappears and the
    class reads green having run nothing.
    """
    if not DERIVED_VERBS:
        return [pytest.param(_UNDERIVED, id="destroying-verbs-underived")]
    return [pytest.param(verb, id=verb) for verb in sorted(DERIVED_VERBS)]


def _artifact_params() -> list:
    if not DERIVED_ARTIFACTS:
        return [pytest.param(_UNDERIVED, id="protected-artifacts-underived")]
    return [pytest.param(art, id=art.replace("/", "-")) for art in sorted(DERIVED_ARTIFACTS)]


# The capture SENTINEL and the capture CORPUS, spelled apart because the whole
# design rests on the difference. The bare `writ-blackbox` prefix appears in 14 real
# captured command lines, all of them routine capture on/off, so keying on it would
# red legitimate work; the token must be the exact filename.
CAPTURE_SENTINEL = "~/.claude/writ-blackbox.on"
CAPTURE_CORPUS = "~/.claude/writ-blackbox.jsonl"

# The one real captured command carrying both a destroying verb and a protected
# artifact, reproduced from the blackbox corpus. The `rm` targets the SENTINEL; the
# corpus is only being listed, three segments away.
REAL_CAPTURED_COMMAND = (
    'rm -f "$HOME/.claude/writ-blackbox.on" && echo "capture OFF (sentinel removed)" ; '
    'ls -la "$HOME/.claude/writ-blackbox.jsonl" | awk \'{print $5}\''
)

SEGMENT_SEPARATORS = [";", "&&", "||", "|"]

DB_CLIENTS = ("psql", "mysql", "mariadb", "sqlite3")
DDL_BRANCHES = ("DROP TABLE", "TRUNCATE TABLE", "DROP DATABASE", "DROP SCHEMA")
_DDL_OBJECT = {
    "DROP TABLE": "users",
    "TRUNCATE TABLE": "orders",
    "DROP DATABASE": "staging",
    "DROP SCHEMA": "reporting",
}


def client_command(client: str, branch: str) -> str:
    """One database-client invocation carrying `branch`, in that client's own flag."""
    statement = "%s %s;" % (branch, _DDL_OBJECT[branch])
    if client == "psql":
        return '%s -c "%s"' % (client, statement)
    if client in ("mysql", "mariadb"):
        return '%s -e "%s"' % (client, statement)
    return '%s app.db "%s"' % (client, statement)


def _mutant_hook(root: Path, source: str) -> str:
    """Write `source` as a RUNNABLE copy of the gate under `root`. Returns its path.

    The gate resolves `WRIT_DIR` as `$(dirname $0)/../..` and sources
    `$WRIT_DIR/bin/lib/common.sh`, so a bare copy in a tmp dir cannot start. Every
    sibling of the gate, and every sibling of `hooks/`, is SYMLINKED into the tmp
    tree; only the gate itself is a real file. Nothing under the real tree is
    written, which is the constraint mutation proofs in this repo carry.
    """
    hooks = root / "hooks"
    scripts = hooks / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    gate_name = Path(HOOK_SH).name
    for entry in Path(REPO).iterdir():
        if entry.name != "hooks":
            (root / entry.name).symlink_to(entry)
    for entry in (Path(REPO) / "hooks").iterdir():
        if entry.name != "scripts":
            (hooks / entry.name).symlink_to(entry)
    for entry in (Path(REPO) / "hooks" / "scripts").iterdir():
        if entry.name != gate_name:
            (scripts / entry.name).symlink_to(entry)
    gate = scripts / gate_name
    gate.write_text(source)
    gate.chmod(0o755)
    return str(gate)


def _empty_array(source: str, array_name: str) -> str:
    """`array_name=( ...members... )` becomes `array_name=()`."""
    anchor = "%s=(" % array_name
    assert anchor in source, "%s is not declared in the hook source" % array_name
    start = source.index(anchor)
    end = source.index(")", start + len(anchor))
    return source[:start] + anchor + ")" + source[end + 1:]


def _narrow_regex(source: str, name: str) -> tuple[str, str]:
    """Keep only the FIRST alternative of `name='a|b|c'`. Returns (source, kept)."""
    anchor = "%s='" % name
    assert anchor in source, (
        "%s is not declared in the hook source, so the database-client arm's regex "
        "cannot be narrowed to prove it conditional" % name
    )
    start = source.index(anchor) + len(anchor)
    end = source.index("'", start)
    branches = source[start:end].split("|")
    assert len(branches) > 1, (
        "%s carries a single alternative, so narrowing it proves nothing: %r"
        % (name, source[start:end])
    )
    return source[:start] + branches[0] + source[end:], branches[0]


# --------------------------------------------------------------------------- #
# Capability 1: a destroying verb naming a Writ log artifact is refused
# --------------------------------------------------------------------------- #

class TestADestroyingVerbNamingALogArtifactIsRefused:
    """MEASURED ALLOW TODAY on all three verbs, which is the gap: the gate-state guard
    protects `var/session`, and nothing in the hook names `var/logs` at all. A
    governance runtime whose record of what the agent did can be erased by the agent is
    publishing a claim, not a log.
    """

    @pytest.mark.parametrize("verb", sorted(VERB_PROBES), ids=sorted(VERB_PROBES))
    def test_the_verb_is_refused_with_enf_irreversible(self, tmp_path, verb) -> None:
        payload, _rows = _run(VERB_PROBES[verb], tmp_path)
        assert _denied(payload), (
            "not refused: %r -> %r" % (VERB_PROBES[verb], payload)
        )
        assert "ENF-IRREVERSIBLE" in _reason(payload), _reason(payload)

    @pytest.mark.parametrize("verb", sorted(VERB_PROBES), ids=sorted(VERB_PROBES))
    def test_the_reason_names_the_artifact_it_matched(self, tmp_path, verb) -> None:
        """An operator has to be able to tell WHICH protected file stopped the command
        without reading the hook, the same contract the git and Neo4j arms already
        carry."""
        reason = _reason(_run(VERB_PROBES[verb], tmp_path)[0])
        assert "audit.jsonl" in reason, reason


# --------------------------------------------------------------------------- #
# Capability 2: the destroying-verb population is derived, not listed
# --------------------------------------------------------------------------- #

class TestTheDestroyingVerbPopulationIsDerived:

    def test_the_derived_population_is_non_empty(self) -> None:
        """Reddened by removing the `# DESTROYING VERBS BEGIN/END` markers, or by
        emptying the array inside them. Fails here rather than at import, so running
        the suite in chunks does not abort on an unrelated file."""
        assert DERIVED_VERBS, (
            "no destroying verb derived from the DESTROYING VERBS block of "
            "hooks/scripts/writ-bash-write-gate.sh: either the markers are absent or "
            "%s is no longer declared inside them" % DESTROYING_VERBS_ARRAY
        )

    def test_the_probe_map_key_set_equals_the_derived_population(self) -> None:
        """Reddened by adding a verb to the hook's block with no probe here, which is
        the failure a hardcoded list cannot see."""
        assert set(VERB_PROBES) == DERIVED_VERBS, (set(VERB_PROBES), DERIVED_VERBS)

    @pytest.mark.parametrize("verb", _verb_params())
    def test_every_derived_verb_has_a_probe(self, tmp_path, verb) -> None:
        assert verb != _UNDERIVED, (
            "the destroying-verb population derived nothing, so this class drives no "
            "verb at all; see test_the_derived_population_is_non_empty"
        )
        assert verb in VERB_PROBES, (
            "%r is declared in the hook's DESTROYING VERBS block with no probe" % verb
        )
        payload, _rows = _run(VERB_PROBES[verb], tmp_path)
        assert _denied(payload), (VERB_PROBES[verb], payload)

    def test_the_array_scan_is_precise_against_synthetic_source(self) -> None:
        """The detector's own conditionality, proved on a synthetic string rather than
        by editing the real tree."""
        synthetic = (
            "# DESTROYING VERBS BEGIN\n"
            '_IRREV_DESTROYING_VERBS=("rm" "truncate")\n'
            "# DESTROYING VERBS END\n"
        )
        assert destroying_verbs(synthetic) == {"rm", "truncate"}

    def test_the_scan_reddens_when_the_block_declares_no_member(self) -> None:
        """A DECAYED hook (the array emptied, the markers left behind) must not read
        green."""
        synthetic = (
            "# DESTROYING VERBS BEGIN\n"
            "_IRREV_DESTROYING_VERBS=()\n"
            "# DESTROYING VERBS END\n"
        )
        with pytest.raises(AssertionError):
            destroying_verbs(synthetic)


# --------------------------------------------------------------------------- #
# Capability 3: every protected artifact is proved by its own deny
# --------------------------------------------------------------------------- #

class TestEveryProtectedArtifactIsProvedByItsOwnDeny:

    def test_the_derived_population_is_non_empty(self) -> None:
        assert DERIVED_ARTIFACTS, (
            "no protected artifact derived from the EVIDENCE DESTRUCTION block of "
            "hooks/scripts/writ-bash-write-gate.sh: either the markers are absent or "
            "%s is no longer declared inside them" % PROTECTED_ARTIFACTS_ARRAY
        )

    def test_the_probe_map_key_set_equals_the_derived_population(self) -> None:
        assert set(ARTIFACT_PROBES) == DERIVED_ARTIFACTS, (
            set(ARTIFACT_PROBES), DERIVED_ARTIFACTS
        )

    def test_each_probe_names_exactly_one_protected_artifact(self) -> None:
        """Without this, a probe naming two artifacts would keep passing after the
        member it was written for was dropped, and the per-member deny below would be
        proving something about a different member."""
        overlaps = {
            artifact: [other for other in ARTIFACT_PROBES
                       if other != artifact and other in command]
            for artifact, command in ARTIFACT_PROBES.items()
        }
        assert not any(overlaps.values()), overlaps

    @pytest.mark.parametrize("artifact", sorted(ARTIFACT_PROBES),
                             ids=[a.replace("/", "-") for a in sorted(ARTIFACT_PROBES)])
    def test_the_member_is_refused_and_named(self, tmp_path, artifact) -> None:
        payload, _rows = _run(ARTIFACT_PROBES[artifact], tmp_path)
        assert _denied(payload), (
            "not refused: %r -> %r" % (ARTIFACT_PROBES[artifact], payload)
        )
        reason = _reason(payload)
        assert "ENF-IRREVERSIBLE" in reason, reason
        assert artifact in reason, (artifact, reason)

    @pytest.mark.parametrize("artifact", _artifact_params())
    def test_every_derived_artifact_has_a_probe(self, artifact) -> None:
        assert artifact != _UNDERIVED, (
            "the protected-artifact population derived nothing, so this class drives no "
            "artifact at all; see test_the_derived_population_is_non_empty"
        )
        assert artifact in ARTIFACT_PROBES, (
            "%r is declared in the hook's EVIDENCE DESTRUCTION block with no probe"
            % artifact
        )


# --------------------------------------------------------------------------- #
# Capability 4: the verb and the artifact must share ONE segment
# --------------------------------------------------------------------------- #

class TestTheVerbAndTheArtifactMustShareOneSegment:
    """THE PAIR THE WHOLE ARM TURNS ON, and the only thing that separates the shipped
    design from the one real false positive in seven weeks of capture.

    A whole-command AND passes the one-segment case below and FAILS the two-segment
    cases, which is exactly the bug this pair exists to catch. Neither half can be
    replaced by tests/test_bash_write_gate.py's `| xargs rm` case: that command names
    `writ-grant-`, so it denies at the gate-state guard about 150 lines earlier, and
    `test_the_write_gate_pipeline_case_answers_at_the_gate_state_guard` below pins
    that measurement so nobody cites it as proof again.
    """

    @pytest.mark.parametrize("separator", SEGMENT_SEPARATORS, ids=SEGMENT_SEPARATORS)
    def test_the_same_two_tokens_in_two_segments_are_allowed(
        self, tmp_path, separator
    ) -> None:
        cmd = "rm -f %s %s ls -la %s" % (CAPTURE_SENTINEL, separator, CAPTURE_CORPUS)
        payload, _rows = _run(cmd, tmp_path)
        assert not _denied(payload), (
            "the verb and the artifact are in different segments, so this is the "
            "capture-off command the corpus actually contains: %r -> %r" % (cmd, payload)
        )

    def test_the_same_two_tokens_in_one_segment_are_refused(self, tmp_path) -> None:
        """The other half of the pair: identical tokens, one segment."""
        cmd = "rm -f %s %s" % (CAPTURE_SENTINEL, CAPTURE_CORPUS)
        payload, _rows = _run(cmd, tmp_path)
        assert _denied(payload), "not refused: %r -> %r" % (cmd, payload)
        reason = _reason(payload)
        assert "ENF-IRREVERSIBLE" in reason, reason
        assert "writ-blackbox.jsonl" in reason, reason

    def test_the_one_segment_deny_comes_from_the_irreversibility_arm(
        self, tmp_path
    ) -> None:
        """Reachability, asserted rather than assumed: a deny from the gate-state guard
        would satisfy the case above while proving nothing about the segment split."""
        cmd = "rm -f %s %s" % (CAPTURE_SENTINEL, CAPTURE_CORPUS)
        payload, rows = _run(cmd, tmp_path)
        reason = _reason(payload)
        assert "ENF-GATE-STATE" not in reason, reason
        gates = [r.get("gate") for r in rows if r.get("event") == "gate_decision"]
        assert gates == ["irreversible"], gates

    def test_the_write_gate_pipeline_case_answers_at_the_gate_state_guard(
        self, tmp_path
    ) -> None:
        """MEASURED, and pinned so the citation cannot be made again: the command
        tests/test_bash_write_gate.py already feeds through the gate denies because it
        names `writ-grant-`, not because anything about deletion was seen. It would pass
        under a broken whole-command design too.

        MUST-NOT-REGRESS as well: this deny is the pre-existing one and must survive.
        """
        payload, _rows = _run("grep writ-grant- var/logs/audit.jsonl | xargs rm", tmp_path)
        assert _denied(payload), payload
        assert "ENF-GATE-STATE" in _reason(payload), _reason(payload)


# --------------------------------------------------------------------------- #
# Capability 5: the capture SENTINEL is not the capture CORPUS
# --------------------------------------------------------------------------- #

class TestTheCaptureSentinelIsNotTheCorpus:
    """The bare `writ-blackbox` prefix appears in 14 real captured command lines, all
    of them turning capture on or off. A rule keyed on the prefix reds every one."""

    def test_the_real_captured_command_stays_allowed(self, tmp_path) -> None:
        payload, _rows = _run(REAL_CAPTURED_COMMAND, tmp_path)
        assert not _denied(payload), (
            "the one real command in the corpus carrying both a destroying verb and a "
            "protected artifact was refused: %r" % payload
        )

    def test_removing_the_sentinel_alone_stays_allowed(self, tmp_path) -> None:
        payload, _rows = _run("rm -f %s" % CAPTURE_SENTINEL, tmp_path)
        assert not _denied(payload), (
            "turning capture off is routine work and names no corpus file: %r" % payload
        )

    def test_the_bare_prefix_is_not_a_protected_artifact(self) -> None:
        """The population itself, checked against the token that would red 14 real
        commands. Reddened by declaring `writ-blackbox` instead of the filename."""
        assert "writ-blackbox" not in DERIVED_ARTIFACTS, (
            "the bare prefix is declared as a protected artifact; it appears in 14 real "
            "captured command lines, all routine capture on/off"
        )

    def test_removing_the_corpus_itself_is_refused(self, tmp_path) -> None:
        """Anti-vacuity partner for the two allows above: if nothing named
        writ-blackbox.jsonl were ever refused, they would pass on any tree."""
        payload, _rows = _run("rm -f %s" % CAPTURE_CORPUS, tmp_path)
        assert _denied(payload), payload
        assert "writ-blackbox.jsonl" in _reason(payload), _reason(payload)


# --------------------------------------------------------------------------- #
# Capability 6: reading the trail stays allowed
# --------------------------------------------------------------------------- #

class TestReadingTheTrailStaysAllowed:
    """`_readonly_inspection` is consulted FIRST inside `_irreversible_reason`, and it
    has to keep working: a guard that refused a `grep` of the audit log would make the
    record it protects undiagnosable, which is the trap the gate-state guard already
    solved once."""

    @pytest.mark.parametrize("cmd", [
        "grep -c writ var/logs/writ/audit.jsonl",
        "cat var/logs/writ/audit.jsonl",
        "tail -5 var/logs/writ/audit.jsonl",
        "wc -l var/logs/writ/audit.jsonl",
        "grep gate_decision var/logs/writ/audit.jsonl | tail -20 | wc -l",
    ])
    def test_a_read_only_pipeline_naming_the_audit_log_is_allowed(
        self, tmp_path, cmd
    ) -> None:
        payload, _rows = _run(cmd, tmp_path)
        assert not _denied(payload), "a read of the trail was refused: %r -> %r" % (
            cmd, payload)

    @pytest.mark.parametrize("cmd", [
        "grep -h writ-manual-test-grant var/logs/audit.jsonl",
        "grep writ-grant- var/logs/audit.jsonl | tail -5 | wc -l",
    ])
    def test_the_write_gate_modules_allowed_audit_commands_still_pass(
        self, tmp_path, cmd
    ) -> None:
        """MUST-NOT-REGRESS, driven here rather than trusted: these two are pinned as
        ALLOWS in tests/test_bash_write_gate.py::TestGateStateNameGuard and they name
        `var/logs`, which this cycle makes a protected artifact."""
        payload, _rows = _run(cmd, tmp_path)
        assert not _denied(payload), (cmd, payload)

    @pytest.mark.parametrize("cmd", [
        "grep writ-grant- var/logs/audit.jsonl > /tmp/out",
        "grep writ-grant- var/logs/audit.jsonl | xargs rm",
    ])
    def test_the_write_gate_modules_denied_audit_commands_still_deny_the_same_way(
        self, tmp_path, cmd
    ) -> None:
        """The other two of the four. Both must keep denying AT THE GATE-STATE GUARD:
        a deny that moved to the irreversibility arm would still read green in
        tests/test_bash_write_gate.py, which only asserts ENF-GATE-STATE is in the
        reason, but it would mean this cycle quietly took over a refusal it does not
        own."""
        payload, _rows = _run(cmd, tmp_path)
        assert _denied(payload), (cmd, payload)
        assert "ENF-GATE-STATE" in _reason(payload), (cmd, _reason(payload))


# --------------------------------------------------------------------------- #
# Capability 7: user-data deletion is LEFT ALONE
# --------------------------------------------------------------------------- #

class TestADestroyingVerbOutsideTheLogTreeStaysAllowed:
    """THE HONEST "NOT WORTH IT", pinned as behaviour so a later cycle cannot quietly
    widen the rule. All 16 distinct `rm` invocations in 6,017 real Bash envelopes were
    scratch cleanup: /tmp probe dirs, pytest basetemp, scratchpad files, one build
    cleanup. A blanket rule would have fired on every one of them and on nothing
    destructive, and Claude Code's own layer already denies the `rm -rf <abs path>`
    spelling.
    """

    @pytest.mark.parametrize("cmd", [
        "rm -rf build/",
        "rm -rf /tmp/probe-basetemp",
        "rm -r build/",
        "find . -name '*.py' -delete",
        "rm\t-rf build/",
        '"rm" -rf build/',
        "rm  -rf  build/",
    ])
    def test_the_command_is_not_refused(self, tmp_path, cmd) -> None:
        payload, _rows = _run(cmd, tmp_path)
        assert not _denied(payload), "wrongly refused: %r -> %r" % (cmd, payload)

    def test_a_prose_mention_of_the_log_tree_is_not_a_destroying_verb(
        self, tmp_path
    ) -> None:
        """The word-boundary contamination the measurement had to remove: `rm ` with no
        boundary matches "Confi**rm** ", which inflated the corpus count almost
        fourfold. A guard that matched the same way would refuse this sentence, which
        names the log tree and destroys nothing."""
        payload, _rows = _run('echo "Confirm the rotation of var/logs"', tmp_path)
        assert not _denied(payload), payload


class TestTheVerbMatchSeesTheSpellingsAShellWouldRun:
    """The arm reads the NORMALIZED copy for the reason every phrase match in
    `_irreversible_reason` does: this site has no second layer, so a missed spelling is
    a complete pass-through. The glob pair (`"<verb> "*` and `*" <verb> "*`) is also
    what keeps a wrapper prefix visible."""

    @pytest.mark.parametrize("cmd", [
        "rm\t-f var/logs/writ/audit.jsonl",
        '"rm" -f var/logs/writ/audit.jsonl',
        "rm  -f  var/logs/writ/audit.jsonl",
        "sudo rm -rf var/logs",
    ])
    def test_the_spelling_is_still_refused(self, tmp_path, cmd) -> None:
        payload, _rows = _run(cmd, tmp_path)
        assert _denied(payload), "slipped through: %r -> %r" % (cmd, payload)
        assert "ENF-IRREVERSIBLE" in _reason(payload), _reason(payload)


# --------------------------------------------------------------------------- #
# Capability 8, 9, 10: SQL DDL through a database client
# --------------------------------------------------------------------------- #

class TestSqlDdlThroughADatabaseClientIsRefused:
    """MEASURED TRUE-POSITIVE COST: zero, and that is disclosed rather than hidden. No
    database client is invoked anywhere in the corpus, in either direction. This arm
    earns its place on MECHANISM IDENTITY, not incident history: arm 1 already refuses a
    graph-reaching verb carrying a destructive statement, and `psql -c "DROP TABLE
    users;"` is that same mechanism in a different dialect.

    THE CROSS PRODUCT IS THE POINT. Each branch is an independent alternative of one
    regex, so narrowing the regex to the first alternative must red the other three
    rather than being averaged away, and each client is an independent glob.
    """

    @pytest.mark.parametrize("client", DB_CLIENTS)
    @pytest.mark.parametrize("branch", DDL_BRANCHES,
                             ids=[b.replace(" ", "-").lower() for b in DDL_BRANCHES])
    def test_the_branch_carried_by_the_client_is_refused(
        self, tmp_path, client, branch
    ) -> None:
        cmd = client_command(client, branch)
        payload, _rows = _run(cmd, tmp_path)
        assert _denied(payload), "not refused: %r -> %r" % (cmd, payload)
        reason = _reason(payload)
        assert "ENF-IRREVERSIBLE" in reason, reason
        assert branch.lower() in reason.lower(), (branch, reason)

    def test_a_lowercase_statement_is_refused_too(self, tmp_path) -> None:
        """SQL keywords are case-insensitive, so a guard matching only uppercase would
        be defeated by typing it in lower case."""
        payload, _rows = _run('psql -c "drop table users;"', tmp_path)
        assert _denied(payload), payload


class TestAnOrdinaryDatabaseClientInvocationIsAllowed:
    """The false-positive surface this arm really has is in ANOTHER repo: truncating
    Magento index tables is routine work, and the same capture shows Magento sessions.
    That is why the refusal must name the way out, and why reading stays free."""

    @pytest.mark.parametrize("cmd", [
        'psql -c "SELECT 1"',
        'mysql -e "SHOW TABLES"',
        'mariadb -e "SELECT COUNT(*) FROM orders"',
        "sqlite3 app.db .tables",
    ])
    def test_an_ordinary_client_invocation_is_not_refused(self, tmp_path, cmd) -> None:
        payload, _rows = _run(cmd, tmp_path)
        assert not _denied(payload), "wrongly refused: %r -> %r" % (cmd, payload)

    def test_a_statement_with_no_client_verb_is_allowed(self, tmp_path) -> None:
        """BOTH halves are required, exactly as arm 1 requires both: writing prose that
        merely contains the statement must not be caught."""
        payload, _rows = _run('echo "drop table users"', tmp_path)
        assert not _denied(payload), payload


class TestAContainerReachingADatabaseClient:

    def test_a_container_carrying_a_destructive_statement_is_refused(
        self, tmp_path
    ) -> None:
        payload, _rows = _run('docker exec db mysql -e "DROP TABLE users"', tmp_path)
        assert _denied(payload), payload
        assert "ENF-IRREVERSIBLE" in _reason(payload), _reason(payload)

    def test_an_ordinary_docker_exec_stays_allowed(self, tmp_path) -> None:
        """MUST-NOT-REGRESS, and the sibling of the same assertion in
        TestGraphDestructionThroughAContainer above: the vector is the statement, not
        docker."""
        payload, _rows = _run("docker exec writ-neo4j ls /var/lib/neo4j", tmp_path)
        assert not _denied(payload), payload


# --------------------------------------------------------------------------- #
# Capability 11: every new refusal names the way out, and mints no new phrase
# --------------------------------------------------------------------------- #

NEW_REFUSAL_TRIGGERS = {
    "evidence": "rm -f var/logs/writ/audit.jsonl",
    "database-client": 'psql -c "DROP TABLE users;"',
}

# The pre-existing git refusal, used as the ORACLE for the clearance sentence rather
# than a literal copied into this file.
_EXISTING_IRREVERSIBLE_TRIGGER = "git reset --hard HEAD~1"


def _clearance_sentence(tmp_path: Path) -> str:
    """The `!`-prefix sentence the three existing irreversibility refusals already
    carry, lifted out of one that the gate REALLY emitted.

    Read from the artifact, not restated here, because the whole point of capability 11
    is that the new refusals reuse this sentence rather than inventing one.
    """
    reason = _reason(_run(_EXISTING_IRREVERSIBLE_TRIGGER, tmp_path / "oracle")[0])
    anchor = "ask the user to run it themselves"
    assert anchor in reason, (
        "the existing git refusal no longer carries the clearance sentence this cycle "
        "reuses, so there is nothing to compare the new refusals against: %r" % reason
    )
    start = reason.index(anchor)
    end = reason.index(".", start)
    return reason[start:end + 1]


class TestEveryNewRefusalNamesTheWayOut:
    """A refusal naming no action is a deadlock, and this repo shipped that defect one
    cycle ago (finding 1). The clearance here is a HARNESS action, the user running the
    command themselves with a leading `!`, which nothing has to mint.

    THE SENTENCE IS REUSED, NOT INVENTED, and that is mechanical rather than stylistic:
    `tests/_inventory.py::user_directed_phrase_sites` derives every "reply <phrase>"
    directive from hook source and `tests/firedrill/test_grant_phrase_refusals.py`
    judges each one against the live minting predicates. A newly invented "reply X"
    sentence registers a new site, and a dead phrase there is the deadlock finding 1
    just fixed.
    """

    @pytest.mark.parametrize("arm", sorted(NEW_REFUSAL_TRIGGERS))
    def test_the_refusal_reuses_the_existing_clearance_sentence(
        self, tmp_path, arm
    ) -> None:
        sentence = _clearance_sentence(tmp_path)
        reason = _reason(_run(NEW_REFUSAL_TRIGGERS[arm], tmp_path / arm)[0])
        assert sentence in reason, (
            "the %s refusal does not carry the clearance sentence the existing "
            "irreversibility refusals emit (%r): %r" % (arm, sentence, reason)
        )

    @pytest.mark.parametrize("arm", sorted(NEW_REFUSAL_TRIGGERS))
    def test_the_refusal_introduces_no_user_directed_phrase(self, tmp_path, arm) -> None:
        """The derivation already accepts this refusal precisely because it tells the
        user to type NOTHING: the way out is a harness action. Reddened by writing the
        clearance as a `reply "..."` directive, which would register a phrase site that
        the live minting predicates reject.

        THE DENY IS ASSERTED FIRST, and that is the difference between a control and a
        vacuous pass: an allowed command emits no reason at all, and the empty string
        names no phrase either, so without this precondition the case would read green
        on today's tree for the wrong reason.
        """
        payload, _rows = _run(NEW_REFUSAL_TRIGGERS[arm], tmp_path / arm)
        assert _denied(payload), payload
        reason = _reason(payload)
        assert user_directed_phrases(reason) == [], (
            "the %s refusal tells the user to type %r; the phrase population in "
            "tests/_inventory.py judges every such directive against the live minting "
            "predicates, and this one mints nothing"
            % (arm, user_directed_phrases(reason))
        )

    @pytest.mark.parametrize("arm", sorted(NEW_REFUSAL_TRIGGERS))
    def test_the_refusal_names_the_route_for_prose(self, tmp_path, arm) -> None:
        """The arm refuses a mention inside an argument the same way the rest of this
        file does, so it must say how to write ABOUT the pattern."""
        reason = _reason(_run(NEW_REFUSAL_TRIGGERS[arm], tmp_path / arm)[0])
        assert "git commit -F" in reason, reason

    @pytest.mark.parametrize("arm", sorted(NEW_REFUSAL_TRIGGERS))
    def test_the_refusal_offers_no_grant(self, tmp_path, arm) -> None:
        """The manual-testing grant means "manual testing", not "destroy this".

        Same precondition as the phrase case above, and for the same reason: an absence
        asserted against an empty reason proves nothing.
        """
        payload, _rows = _run(NEW_REFUSAL_TRIGGERS[arm], tmp_path / arm)
        assert _denied(payload), payload
        assert "manual testing approved" not in _reason(payload).lower(), _reason(payload)

    def test_the_evidence_refusal_names_the_reversible_route(self, tmp_path) -> None:
        reason = _reason(_run(NEW_REFUSAL_TRIGGERS["evidence"], tmp_path)[0]).lower()
        assert "grep" in reason, reason

    def test_the_database_refusal_names_the_reversible_route(self, tmp_path) -> None:
        reason = _reason(_run(NEW_REFUSAL_TRIGGERS["database-client"], tmp_path)[0])
        assert "SELECT" in reason or "select" in reason, reason


# --------------------------------------------------------------------------- #
# Capability 12: exactly one gate_decision deny row, under gate `irreversible`
# --------------------------------------------------------------------------- #

class TestEveryNewRefusalIsRecordedExactlyOnce:

    @pytest.mark.parametrize("arm", sorted(NEW_REFUSAL_TRIGGERS))
    def test_one_gate_decision_deny_row_under_the_irreversible_gate(
        self, tmp_path, arm
    ) -> None:
        payload, rows = _run(NEW_REFUSAL_TRIGGERS[arm], tmp_path / arm)
        assert _denied(payload), payload
        denies = [r for r in rows
                  if r.get("event") == "gate_decision" and r.get("decision") == "deny"]
        assert len(denies) == 1, (
            "a refusal that leaves no record is not auditable, and two records for one "
            "command make the audit trail lie about how often the gate fired: %r" % rows
        )
        assert denies[0].get("gate") == "irreversible", denies[0]
        assert denies[0].get("reason"), denies[0]


# --------------------------------------------------------------------------- #
# Capability 13: the per-call process cost does not move
# --------------------------------------------------------------------------- #

BASELINE_REV = "ad01ca5"

_EXTERNAL_FILTERS = ("tr", "sed", "awk", "perl", "python", "python3", "grep", "egrep",
                     "fgrep", "cut", "xargs", "jq", "find", "cat", "printf")

_QUOTED_SPAN_RE = re.compile(r"\"[^\"]*\"|'[^']*'", re.S)


def _code_only(block: str) -> str:
    """`block` with every quoted span blanked.

    The refusal MESSAGE lives inside this block and names `grep`, `cat` and `tail` as
    the reversible route, so a raw scan for filter names would red on a correct
    implementation. Blanking quoted spans also removes `IFS='|'`, which leaves a real
    pipeline operator visible while a pipe used as a separator character is not.
    """
    return _QUOTED_SPAN_RE.sub(" ", block)


def _baseline_hook_source() -> str:
    proc = subprocess.run(
        ["git", "show", "%s:hooks/scripts/writ-bash-write-gate.sh" % BASELINE_REV],
        cwd=str(REPO), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        "cannot read the pre-cycle gate out of %s: %r" % (BASELINE_REV, proc.stderr))
    return proc.stdout


def _execve_count(hook: str, tmp_path: Path) -> int:
    env = {
        **os.environ,
        "WRIT_CACHE_DIR": str(tmp_path / "cache"),
        "WRIT_LOG_ROOT": str(tmp_path / "logs"),
        "WRIT_PORT": "59999",
        "WRIT_NO_AUTOSTART": "1",
    }
    env.pop("WRIT_FRICTION_LOG", None)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    envelope = json.dumps({"session_id": "irrev-cost", "tool_name": "Bash",
                           "tool_input": {"command": "git status"}})
    trace = trace_execve(["bash", hook], input=envelope, cwd=str(REPO), env=env)
    return trace.count("execve(")


class TestThePerCallProcessCostDoesNotMove:
    """PERF-QBUDGET-001: the dispatch's constraint was not to make the existing cost
    worse, so the budget is asserted, not claimed. Two independent checks, because
    either one alone can be satisfied while the other is violated: a structural pin
    over the new block's own text, and a MEASUREMENT of the execve count for the
    commonest Bash call there is.
    """

    @pytest.mark.parametrize("name", [_VERB_MARK, _EVIDENCE_MARK])
    def test_the_block_exists_and_is_not_empty(self, name) -> None:
        assert marker_block(_hook_source(), name).strip()

    @pytest.mark.parametrize("name", [_VERB_MARK, _EVIDENCE_MARK])
    def test_the_block_contains_no_command_substitution(self, name) -> None:
        code = _code_only(marker_block(_hook_source(), name))
        assert "$(" not in code, code
        assert "`" not in code, code

    @pytest.mark.parametrize("name", [_VERB_MARK, _EVIDENCE_MARK])
    def test_the_block_pipes_into_no_external_filter(self, name) -> None:
        code = _code_only(marker_block(_hook_source(), name))
        offender = re.search(r"\|\s*(?:\S*/)?(%s)\b" % "|".join(_EXTERNAL_FILTERS), code)
        assert offender is None, (
            "the new block pipes into %r; the single existing exec in this path (the "
            "printf|grep inside _readonly_inspection) is untouched by this cycle and "
            "no second one is budgeted" % (offender.group(1) if offender else None)
        )

    @pytest.mark.parametrize("name", [_VERB_MARK, _EVIDENCE_MARK])
    def test_the_block_starts_no_line_with_an_external_filter(self, name) -> None:
        code = _code_only(marker_block(_hook_source(), name))
        offenders = [line.strip() for line in code.splitlines()
                     if line.strip().split(" ")[0].lstrip("(").split("/")[-1]
                     in _EXTERNAL_FILTERS]
        assert not offenders, offenders

    def test_the_evidence_block_holds_the_whole_arm(self) -> None:
        """Without this the marker block could shrink to the array declaration alone
        and the three structural pins above would be measuring nothing. The refusal
        string is the arm's last statement, so requiring it inside the block puts the
        segment split and every per-segment glob inside the pinned region too."""
        block = marker_block(_hook_source(), _EVIDENCE_MARK)
        assert "[ENF-IRREVERSIBLE]" in block, (
            "the EVIDENCE DESTRUCTION block does not contain its own refusal, so the "
            "code whose cost this class pins sits outside the pinned region"
        )

    def test_the_execve_count_for_git_status_matches_the_baseline_revision(
        self, tmp_path
    ) -> None:
        """MEASURED, both sides, through the same tmp-tree runner so the comparison is
        of the two hook SOURCES and not of two different invocation shapes. `git status`
        is chosen because it reaches the irreversibility pass and is refused by nothing,
        which is the ordinary path every Bash call takes."""
        baseline = _mutant_hook(tmp_path / "baseline-tree", _baseline_hook_source())
        live = _mutant_hook(tmp_path / "live-tree", _hook_source())
        before = _execve_count(baseline, tmp_path / "baseline-run")
        after = _execve_count(live, tmp_path / "live-run")
        assert before > 0, "no execve traced at all, so this measurement is vacuous"
        assert after == before, (
            "the per-call process cost moved: %d execve at %s, %d now"
            % (before, BASELINE_REV, after)
        )


# --------------------------------------------------------------------------- #
# Capability 14: each new detector is conditional, proved by mutation
# --------------------------------------------------------------------------- #

class TestEachNewDetectorIsConditionalByMutation:
    """Every mutation runs against a COPY under tmp_path. The real tree is never
    edited, and the control below is what makes the three mutants mean anything: a
    copy that could not refuse in the first place would "prove" any mutation works.
    """

    def test_the_unmutated_copy_still_refuses(self, tmp_path) -> None:
        """ANTI-VACUITY CONTROL for the whole class, and a self-diagnosing one.

        The first assertion drives a PRE-EXISTING refusal through the copy, so a red
        there means the tmp-tree runner itself is broken (a missing symlink, a WRIT_DIR
        that does not resolve) rather than anything about this cycle. The second drives
        this cycle's own probe: a red there with the first one green is the missing
        production behaviour, which is the expected skeleton state.
        """
        hook = _mutant_hook(tmp_path / "tree", _hook_source())
        existing, _rows = _run(_EXISTING_IRREVERSIBLE_TRIGGER, tmp_path / "existing",
                               hook=hook)
        assert _denied(existing), (
            "a copy of the gate could not even produce a refusal that already exists "
            "on this tree, so the tmp-tree runner is broken and every mutation below "
            "would pass for the wrong reason: %r" % existing
        )
        payload, _rows = _run(VERB_PROBES["rm"], tmp_path / "run", hook=hook)
        assert _denied(payload), (
            "an unmodified copy of the gate did not refuse the evidence-destruction "
            "probe: %r" % payload
        )

    def test_emptying_the_artifact_list_turns_a_deny_into_an_allow(
        self, tmp_path
    ) -> None:
        mutated = _empty_array(_hook_source(), PROTECTED_ARTIFACTS_ARRAY)
        hook = _mutant_hook(tmp_path / "tree", mutated)
        payload, _rows = _run(VERB_PROBES["rm"], tmp_path / "run", hook=hook)
        assert not _denied(payload), (
            "the refusal survived an EMPTY protected-artifact list, so it is not keyed "
            "on that list at all: %r" % payload
        )

    def test_emptying_the_verb_list_turns_a_deny_into_an_allow(self, tmp_path) -> None:
        mutated = _empty_array(_hook_source(), DESTROYING_VERBS_ARRAY)
        hook = _mutant_hook(tmp_path / "tree", mutated)
        payload, _rows = _run(VERB_PROBES["rm"], tmp_path / "run", hook=hook)
        assert not _denied(payload), (
            "the refusal survived an EMPTY destroying-verb list: %r" % payload
        )

    def test_narrowing_the_ddl_regex_silences_every_other_branch(self, tmp_path) -> None:
        mutated, kept = _narrow_regex(_hook_source(), _DDL_RE_NAME)
        hook = _mutant_hook(tmp_path / "tree", mutated)
        survivors = []
        for branch in DDL_BRANCHES:
            if branch.lower().replace(" ", "") in kept.lower().replace(" ", ""):
                continue
            cmd = client_command("psql", branch)
            payload, _rows = _run(cmd, tmp_path / branch.replace(" ", "-"), hook=hook)
            if _denied(payload):
                survivors.append(branch)
        assert not survivors, (
            "narrowing %s to its first alternative (%r) left %r still refused, so those "
            "branches are matched by something other than the alternation this cycle "
            "adds" % (_DDL_RE_NAME, kept, survivors)
        )

    def test_the_kept_branch_still_denies_under_the_narrowed_regex(
        self, tmp_path
    ) -> None:
        """The other half: a mutant that allowed EVERYTHING would satisfy the case
        above while proving the regex is dead rather than narrowed."""
        mutated, kept = _narrow_regex(_hook_source(), _DDL_RE_NAME)
        hook = _mutant_hook(tmp_path / "tree", mutated)
        branch = next(
            (b for b in DDL_BRANCHES
             if b.lower().replace(" ", "") in kept.lower().replace(" ", "")), None)
        assert branch, (
            "the first alternative of %s (%r) matches none of the declared branches, so "
            "the narrowing proof above cannot be read" % (_DDL_RE_NAME, kept))
        payload, _rows = _run(client_command("psql", branch), tmp_path / "run", hook=hook)
        assert _denied(payload), (branch, kept, payload)


# --------------------------------------------------------------------------- #
# Capability 16: the disclosed residue, pinned rather than described
# --------------------------------------------------------------------------- #

class TestTheDisclosedResidueIsPinned:
    """Same class of residue the arm already discloses for git (`git -C /tmp reset
    --hard`): this is phrase matching over segments, not an argument walk. Asserts the
    CLOSED behaviour and is xfail(strict=True), so a later cycle that closes it has to
    update this disclosure rather than leave it stale.
    """

    @pytest.mark.xfail(strict=True, reason=(
        "an artifact arriving through a PIPE is not seen: the verb sits in segment 2 "
        "and the artifact in segment 1, which is the same split that keeps the one real "
        "captured command allowed. Closing it needs a per-verb argument walk plus "
        "dataflow across a pipe, which is the tokenizer the extractor already owns"))
    def test_an_artifact_reaching_the_verb_through_a_pipe_would_deny(
        self, tmp_path
    ) -> None:
        payload, _rows = _run('grep -l "" var/logs/writ/audit.jsonl | xargs rm', tmp_path)
        assert _denied(payload), payload
        assert "ENF-IRREVERSIBLE" in _reason(payload), _reason(payload)


# --------------------------------------------------------------------------- #
# Capability 17: the deliberate over-refusal, pinned rather than discovered
# --------------------------------------------------------------------------- #

class TestTheDeliberateOverRefusalIsPinned:
    """RULED ON, not a defect to fix. Normalization flattens newlines to spaces before
    the segment split sees the command, so a two-line command is ONE segment and a
    first-line `rm` paired with a second-line log READ is refused. Measured cost in
    6,017 real Bash envelopes: zero. The direction is fail-closed.
    """

    def test_a_two_line_command_is_one_segment_and_is_refused(self, tmp_path) -> None:
        payload, _rows = _run("rm -rf /tmp/probe-dir\ntail -5 var/logs/writ/audit.jsonl",
                              tmp_path)
        assert _denied(payload), (
            "the newline was NOT flattened before the segment split, so this cycle's "
            "cost is different from the one that was ruled on: %r" % payload
        )
        assert "ENF-IRREVERSIBLE" in _reason(payload), _reason(payload)

    def test_the_same_two_commands_joined_by_a_semicolon_are_allowed(
        self, tmp_path
    ) -> None:
        """The control that makes the over-refusal specifically about the NEWLINE: the
        identical pair separated by `;` is two segments and stays allowed."""
        payload, _rows = _run("rm -rf /tmp/probe-dir ; tail -5 var/logs/writ/audit.jsonl",
                              tmp_path)
        assert not _denied(payload), payload


# =========================================================================== #
# FINDING 6: invoking a whole-graph-wiping benchmark, under either spelling.
#
# The 2026-08-05 incident class, and the two spellings the arm in place today
# misses. Arm 2 reads the invoked file's body for `_IRREV_CYPHER_RE` alone, and
# `benchmarks/run_benchmarks.py` carries no Cypher at all: it wipes through
# `await db.clear_all()`. So the predicate widens from the Cypher regex to a
# WHOLE-GRAPH WIPE regex, and a second arm sees the pytest spelling that
# `_irrev_script_target` deliberately skips (`-m` disqualifies the whole command
# so the suite can run).
#
# THE PREDICATE IS THE WIPE MECHANISM, NOT A PATH LIST, and that distinction is
# what keeps this cycle's allow side green. A path-only arm would also refuse
# `git add benchmarks/run_benchmarks.py`, which this very cycle needs, and a
# pytest arm that ignored the file's mechanism would refuse
# `pytest tests/test_retrieval.py`: 64 python files call the wipe, most of them
# under tests/, and a scoped delete in a test is normal work.
# =========================================================================== #

_BENCH_MARK = "BENCHMARK ENTRYPOINT"

# The whole-graph wipe predicate and the path glob that gates it. Named rather
# than derived, for the reason `_DDL_RE_NAME` gives: both are `[[ ]]` match
# strings, not arrays, so there is no member syntax to parse. Their
# conditionality is proved by MUTATION below instead.
_WIPE_RE_NAME = "_IRREV_WIPE_RE"
_BENCH_GLOB_NAME = "_IRREV_BENCH_GLOB"

# The alternative this cycle ADDS to the body predicate, named so the narrowing
# mutation can drop it by name rather than by position: dropping "the first
# branch" would prove nothing if the implementer happened to write it first.
_WIPE_MECHANISM = "clear_all"

_NO_SUCH_MECHANISM = "__writ_no_such_wipe_mechanism__"
_NO_SUCH_BENCH_DIR = "no-such-benchmark-dir/"

# `benchmarks/_corpus_safety.py` names the wipe only inside docstrings and is the
# module that REFUSES an unsafe one. The hook's predicate is text and matches that
# prose, so invoking it is refused. Pinned as a DELIBERATE over-refusal rather than
# discovered later: nobody invokes the safety module, and fail-closed is the right
# direction at a gate. `tests/_inventory.py::wiping_benchmark_entrypoints()` parses
# instead, so the structural pins are not confused by the same prose.
SAFETY_MODULE = "benchmarks/_corpus_safety.py"


def wiping_benchmarks() -> list[str]:
    """The ast-derived wiping-benchmark population, or [] when it is underivable.

    Import must not raise, for the reason `_derived` above gives: an assertion at
    module scope becomes a COLLECTION error rather than a red module.
    """
    try:
        from tests._inventory import wiping_benchmark_entrypoints

        return sorted(wiping_benchmark_entrypoints())
    except Exception:  # noqa: BLE001
        return []


WIPING_BENCHMARKS = wiping_benchmarks()


def _bench_params() -> list:
    if not WIPING_BENCHMARKS:
        return [pytest.param(_UNDERIVED, id="wiping-benchmarks-underived")]
    return [pytest.param(rel, id=rel.replace("/", "-")) for rel in WIPING_BENCHMARKS]


def _make_bench_command() -> str:
    """`make bench`'s exact command, READ FROM THE MAKEFILE rather than restated.

    The one command in this repo that runs a benchmark on purpose. A copy typed into
    this file would go stale the day the recipe changes, and the regression it exists
    to catch is precisely a gate that starts refusing the recipe.
    """
    text = (REPO / "Makefile").read_text()
    python = re.search(r"^PYTHON\s*\?=\s*(.+)$", text, re.M)
    recipe = re.search(r"^bench:.*\n\t(.+)$", text, re.M)
    assert python and recipe, "cannot read the bench recipe out of the Makefile"
    return recipe.group(1).strip().replace("$(PYTHON)", python.group(1).strip())


def _repoint(source: str, name: str, value: str) -> str:
    """`name='...'` or `name="..."` rewritten to carry `value`. Returns the source."""
    anchor = "%s=" % name
    assert anchor in source, (
        "%s is not declared in the hook source, so the benchmark arm cannot be proved "
        "conditional on it" % name
    )
    start = source.index(anchor) + len(anchor)
    quote = source[start]
    assert quote in ("'", '"'), (
        "%s is not assigned a quoted literal (%r), so this mutation cannot rewrite it"
        % (name, source[start:start + 20])
    )
    end = source.index(quote, start + 1)
    return source[:start + 1] + value + source[end:]


def _declared_value(source: str, name: str) -> str:
    anchor = "%s=" % name
    assert anchor in source, "%s is not declared in the hook source" % name
    start = source.index(anchor) + len(anchor)
    quote = source[start]
    return source[start + 1:source.index(quote, start + 1)]


def _drop_branch(source: str, name: str, needle: str) -> str:
    """Delete every alternative of `name`'s alternation that contains `needle`.

    THE DECAY MUTATION. Repointing the whole regex proves the deny is keyed on it;
    this proves the deny is keyed on the alternative this cycle ADDS, rather than on
    the pre-existing Cypher one that already catches `bench_targets.py`.
    """
    value = _declared_value(source, name)
    branches = value.split("|")
    kept = [b for b in branches if needle not in b]
    assert len(kept) < len(branches), (
        "%s carries no alternative naming %r, so the wipe mechanism this cycle adds is "
        "not in the predicate at all: %r" % (name, needle, value)
    )
    assert kept, (
        "%s consists of nothing but the %r alternative, so dropping it leaves an empty "
        "regex, which matches everything and would prove the opposite: %r"
        % (name, needle, value)
    )
    return _repoint(source, name, "|".join(kept))


# --------------------------------------------------------------------------- #
# Capability 14, 15: both spellings of invoking a wiping benchmark are refused
# --------------------------------------------------------------------------- #

class TestInvokingAWipingBenchmarkIsRefused:
    """MEASURED ALLOW TODAY for `python3 benchmarks/run_benchmarks.py`: the file's
    wipe is a python call, and the arm in place reads the body for Cypher only.
    """

    @pytest.mark.parametrize("rel", _bench_params())
    def test_the_interpreter_spelling_is_refused(self, tmp_path, rel) -> None:
        assert rel != _UNDERIVED, (
            "the wiping-benchmark population is underived, so this matrix ran against "
            "no member at all"
        )
        payload, _rows = _run("python3 %s" % rel, tmp_path)
        assert _denied(payload), "not refused: python3 %s -> %r" % (rel, payload)
        assert "ENF-IRREVERSIBLE" in _reason(payload), _reason(payload)

    @pytest.mark.parametrize("rel", _bench_params())
    def test_the_pytest_spelling_is_refused(self, tmp_path, rel) -> None:
        """THE SPELLING THE INTERPRETER WALK SKIPS, and the one `run_benchmarks.py`'s
        own docstring recommends: "Run with: pytest benchmarks/run_benchmarks.py -v".
        `_irrev_script_target` disqualifies any command carrying `-m`, so this needs a
        second arm rather than a wider walk."""
        assert rel != _UNDERIVED, "population underived"
        payload, _rows = _run(".venv/bin/python -m pytest %s" % rel, tmp_path)
        assert _denied(payload), "not refused: pytest %s -> %r" % (rel, payload)

    @pytest.mark.parametrize("cmd", [
        "python3 benchmarks/scale_benchmark.py --run",
        "pytest benchmarks/run_benchmarks.py",
        "pytest benchmarks/run_benchmarks.py -v",
    ])
    def test_the_spellings_named_in_the_plan_are_refused(self, tmp_path, cmd) -> None:
        """The bare `pytest` spelling as well as the venv one: the arm keys on an
        executing verb, not on the interpreter path that happens to precede it."""
        payload, _rows = _run(cmd, tmp_path)
        assert _denied(payload), "not refused: %r -> %r" % (cmd, payload)

    def test_the_refusal_names_the_file_and_the_mechanism(self, tmp_path) -> None:
        payload, _rows = _run("python3 benchmarks/run_benchmarks.py", tmp_path)
        reason = _reason(payload)
        assert "run_benchmarks.py" in reason, reason
        assert "graph" in reason.lower(), reason


# --------------------------------------------------------------------------- #
# Capability 16, 17: the allow side, which is as important as the deny side
# --------------------------------------------------------------------------- #

class TestTheBenchmarkArmsAllowSideMustNotRegress:
    """A path-only arm was REJECTED on exactly these cases. Each one is routine work
    that a wider rule would stop, and the last two are the reason the predicate is the
    wipe MECHANISM: 64 python files call it, most of them under tests/.
    """

    def test_the_make_bench_command_stays_allowed(self, tmp_path) -> None:
        cmd = _make_bench_command()
        payload, _rows = _run(cmd, tmp_path)
        assert not _denied(payload), (
            "`make bench`'s own command was refused, so the one benchmark this repo "
            "runs on purpose can no longer be run: %r -> %r" % (cmd, payload)
        )

    @pytest.mark.parametrize("cmd", [
        "pytest tests/test_retrieval.py",
        ".venv/bin/python -m pytest tests/test_retrieval.py",
    ])
    def test_a_test_module_that_wipes_stays_allowed(self, tmp_path, cmd) -> None:
        """`tests/test_retrieval.py` really does call the wipe and restores through
        migrate.py. Refusing it would stop the suite, which is why the arm's target walk
        is confined to `benchmarks/` by a glob tested before any file is read."""
        payload, _rows = _run(cmd, tmp_path)
        assert not _denied(payload), "wrongly refused: %r -> %r" % (cmd, payload)

    @pytest.mark.parametrize("cmd", [
        "python3 writ/graph/migrate.py",
        "cat benchmarks/run_benchmarks.py",
        "grep -n assert_safe_to_wipe benchmarks/run_benchmarks.py",
        "head -20 benchmarks/scale_benchmark.py",
    ])
    def test_reading_or_running_a_non_wiping_file_stays_allowed(self, tmp_path, cmd) -> None:
        payload, _rows = _run(cmd, tmp_path)
        assert not _denied(payload), "wrongly refused: %r -> %r" % (cmd, payload)

    @pytest.mark.parametrize("cmd", [
        "git add benchmarks/run_benchmarks.py",
        "git diff benchmarks/scale_benchmark.py",
        "ls -la benchmarks/",
    ])
    def test_naming_a_benchmark_with_no_executing_verb_stays_allowed(
        self, tmp_path, cmd
    ) -> None:
        """THE CASE THAT REJECTED THE PATH-ONLY ARM, and this cycle needs it: the
        commit that adds the guard has to stage the file it guards."""
        payload, _rows = _run(cmd, tmp_path)
        assert not _denied(payload), (
            "a command naming a benchmark path with no executing verb was refused, "
            "which is the path-only arm this cycle rejected: %r -> %r" % (cmd, payload)
        )


class TestTheDeliberateBenchmarkOverRefusalIsPinned:
    """RULED ON, not a defect to fix. The hook's predicate is text and matches the wipe
    named inside `_corpus_safety.py`'s docstrings, so invoking the safety module is
    refused. Measured cost: nobody invokes it. Pinned here rather than discovered later,
    and the ast derivation keeps the structural pins free of the same confusion.
    """

    def test_invoking_the_safety_module_is_refused(self, tmp_path) -> None:
        payload, _rows = _run("python3 %s" % SAFETY_MODULE, tmp_path)
        assert _denied(payload), (
            "the text-matched over-refusal of %s is no longer happening; that is a "
            "CHANGE to the ruled-on cost, not an improvement to be absorbed silently: "
            "%r" % (SAFETY_MODULE, payload)
        )

    def test_the_safety_module_is_not_in_the_derived_population(self) -> None:
        """The other half of the disclosure: the population the structural pins read is
        ast-derived, so the over-refusal stays confined to the hook's own text match."""
        assert SAFETY_MODULE not in WIPING_BENCHMARKS, WIPING_BENCHMARKS


# --------------------------------------------------------------------------- #
# Capability 21: the new refusal is recorded once, names the way out, mints nothing
# --------------------------------------------------------------------------- #

BENCHMARK_REFUSAL_TRIGGERS = {
    "benchmark-interpreter": "python3 benchmarks/run_benchmarks.py",
    "benchmark-pytest": ".venv/bin/python -m pytest benchmarks/run_benchmarks.py",
}


class TestTheBenchmarkRefusalIsAuditableAndActionable:
    """Kept apart from `NEW_REFUSAL_TRIGGERS` above rather than folded into it, and the
    reason is one assertion: that population requires the "git commit -F" route, because
    those two arms refuse a MENTION inside an argument. This arm does not: it needs an
    executing verb, so `git commit -m "fix benchmarks/run_benchmarks.py"` is allowed and
    a prose route would be an instruction nobody needs.
    """

    @pytest.mark.parametrize("arm", sorted(BENCHMARK_REFUSAL_TRIGGERS))
    def test_it_reuses_the_existing_clearance_sentence(self, tmp_path, arm) -> None:
        sentence = _clearance_sentence(tmp_path)
        reason = _reason(_run(BENCHMARK_REFUSAL_TRIGGERS[arm], tmp_path / arm)[0])
        assert sentence in reason, (
            "the %s refusal does not carry the clearance sentence the existing "
            "irreversibility refusals emit (%r): %r" % (arm, sentence, reason)
        )

    @pytest.mark.parametrize("arm", sorted(BENCHMARK_REFUSAL_TRIGGERS))
    def test_it_introduces_no_user_directed_phrase(self, tmp_path, arm) -> None:
        payload, _rows = _run(BENCHMARK_REFUSAL_TRIGGERS[arm], tmp_path / arm)
        assert _denied(payload), payload
        assert user_directed_phrases(_reason(payload)) == [], _reason(payload)

    @pytest.mark.parametrize("arm", sorted(BENCHMARK_REFUSAL_TRIGGERS))
    def test_it_offers_no_grant(self, tmp_path, arm) -> None:
        payload, _rows = _run(BENCHMARK_REFUSAL_TRIGGERS[arm], tmp_path / arm)
        assert _denied(payload), payload
        assert "manual testing approved" not in _reason(payload).lower(), _reason(payload)

    @pytest.mark.parametrize("arm", sorted(BENCHMARK_REFUSAL_TRIGGERS))
    def test_one_deny_row_under_the_irreversible_gate(self, tmp_path, arm) -> None:
        payload, rows = _run(BENCHMARK_REFUSAL_TRIGGERS[arm], tmp_path / arm)
        assert _denied(payload), payload
        denies = [r for r in rows
                  if r.get("event") == "gate_decision" and r.get("decision") == "deny"]
        assert len(denies) == 1, (
            "two records for one command make the audit trail lie about how often the "
            "gate fired: %r" % rows
        )
        assert denies[0].get("gate") == "irreversible", denies[0]

    def test_it_names_the_safe_way_to_run_the_benchmark(self, tmp_path) -> None:
        """The refusal has to say what a legitimate benchmark run looks like, or the
        next person works around it. The consent surface already exists:
        `WRIT_TEST_GRAPH` against a separate instance, which `how_to_run_safely()`
        spells out."""
        reason = _reason(_run(BENCHMARK_REFUSAL_TRIGGERS["benchmark-pytest"], tmp_path)[0])
        assert "WRIT_TEST_GRAPH" in reason or "disposable" in reason.lower(), reason


# --------------------------------------------------------------------------- #
# Capability 20, 22: the benchmark arm is conditional, and costs no new process
# --------------------------------------------------------------------------- #

class TestTheBenchmarkArmIsConditionalByMutation:
    """Every mutation runs against a COPY under tmp_path; the real tree is never edited.
    The control comes first, because a copy that could not refuse would "prove" any
    mutation works.
    """

    PROBE = "python3 benchmarks/run_benchmarks.py"
    # The pre-existing Cypher deny, used as the control that the mutations below are
    # specific rather than global: `bench_targets.py` carries a scoped DETACH DELETE and
    # no wipe call, so dropping the wipe alternative must leave it refused.
    CYPHER_PROBE = "python3 benchmarks/bench_targets.py"

    def test_the_unmutated_copy_still_refuses(self, tmp_path) -> None:
        hook = _mutant_hook(tmp_path / "tree", _hook_source())
        existing, _rows = _run(_EXISTING_IRREVERSIBLE_TRIGGER, tmp_path / "existing",
                               hook=hook)
        assert _denied(existing), (
            "a copy of the gate could not produce a refusal that already exists on this "
            "tree, so the tmp-tree runner is broken and every mutation below would pass "
            "for the wrong reason: %r" % existing
        )
        payload, _rows = _run(self.PROBE, tmp_path / "run", hook=hook)
        assert _denied(payload), (
            "an unmodified copy of the gate did not refuse the benchmark probe: %r"
            % payload
        )

    def test_repointing_the_wipe_regex_turns_the_deny_into_an_allow(
        self, tmp_path
    ) -> None:
        """REPOINTED, not emptied, and the difference is not cosmetic: `[[ $x =~ '' ]]`
        matches every string, so an emptied regex would refuse MORE rather than less and
        would prove the opposite of what this case claims."""
        mutated = _repoint(_hook_source(), _WIPE_RE_NAME, _NO_SUCH_MECHANISM)
        hook = _mutant_hook(tmp_path / "tree", mutated)
        payload, _rows = _run(self.PROBE, tmp_path / "run", hook=hook)
        assert not _denied(payload), (
            "the refusal survived a wipe regex that matches nothing, so it is not keyed "
            "on the file's mechanism at all: %r" % payload
        )

    def test_dropping_the_wipe_alternative_leaves_the_cypher_deny_standing(
        self, tmp_path
    ) -> None:
        """NARROWED TO ITS OTHER BRANCHES, by name rather than by position. This is the
        case that proves the widening is what catches the benchmarks: with the wipe
        alternative gone, `run_benchmarks.py` (no Cypher anywhere) goes allowed while
        `bench_targets.py` (a scoped DETACH DELETE) stays refused by the pre-existing
        predicate."""
        mutated = _drop_branch(_hook_source(), _WIPE_RE_NAME, _WIPE_MECHANISM)
        hook = _mutant_hook(tmp_path / "tree", mutated)
        widened, _rows = _run(self.PROBE, tmp_path / "wipe", hook=hook)
        assert not _denied(widened), (
            "%r survived the removal of the %r alternative, so the deny comes from "
            "something other than the mechanism this cycle adds: %r"
            % (self.PROBE, _WIPE_MECHANISM, widened)
        )
        cypher, _rows = _run(self.CYPHER_PROBE, tmp_path / "cypher", hook=hook)
        assert _denied(cypher), (
            "the mutant allows the pre-existing Cypher deny too, so it is a hook that "
            "refuses nothing rather than one with a narrowed predicate: %r" % cypher
        )

    def test_repointing_the_benchmark_glob_turns_the_deny_into_an_allow(
        self, tmp_path
    ) -> None:
        """The glob is a precondition AND the token selector: no `benchmarks/` token, no
        file read. Repointing it at a directory this tree does not have is what removes
        the arm; blanking it would make it match every path and refuse the suite."""
        mutated = _repoint(_hook_source(), _BENCH_GLOB_NAME, _NO_SUCH_BENCH_DIR)
        hook = _mutant_hook(tmp_path / "tree", mutated)
        payload, _rows = _run(".venv/bin/python -m pytest benchmarks/run_benchmarks.py",
                              tmp_path / "run", hook=hook)
        assert not _denied(payload), (
            "the pytest-spelling refusal survived a glob that matches no path in this "
            "tree, so the arm is not confined to benchmarks/ and the cost measured for "
            "`pytest tests/...` does not apply: %r" % payload
        )


class TestTheBenchmarkBlockCostsNoNewProcess:
    """PERF-QBUDGET-001, the budget written before the code: one builtin glob test for a
    `benchmarks/` token, and only when it matches, a token walk plus one `$(<file)`
    builtin read. Zero new execve, which
    `TestThePerCallProcessCostDoesNotMove::test_the_execve_count_for_git_status_matches
    _the_baseline_revision` measures for the hook as a whole.
    """

    def test_the_block_exists_and_is_not_empty(self) -> None:
        assert marker_block(_hook_source(), _BENCH_MARK).strip()

    def test_the_block_holds_its_own_refusal(self) -> None:
        """Without this the marker block could shrink to the two declarations and the
        pins below would be measuring nothing."""
        assert "[ENF-IRREVERSIBLE]" in marker_block(_hook_source(), _BENCH_MARK)

    def test_the_only_command_substitution_is_the_builtin_file_read(self) -> None:
        """`$(<file)` is a bash builtin read and forks nothing, which is why it is the
        one form budgeted here. Any other `$(` or a backtick is a process."""
        code = _code_only(marker_block(_hook_source(), _BENCH_MARK))
        assert "`" not in code, code
        offenders = [m for m in re.finditer(r"\$\(", code)
                     if code[m.end():m.end() + 1] != "<"]
        assert not offenders, (
            "the benchmark block opens a command substitution that is not the builtin "
            "file read: %r" % [code[m.start():m.start() + 40] for m in offenders]
        )

    def test_the_block_pipes_into_no_external_filter(self) -> None:
        code = _code_only(marker_block(_hook_source(), _BENCH_MARK))
        offender = re.search(r"\|\s*(?:\S*/)?(%s)\b" % "|".join(_EXTERNAL_FILTERS), code)
        assert offender is None, offender.group(1) if offender else None

    def test_the_block_starts_no_line_with_an_external_filter(self) -> None:
        code = _code_only(marker_block(_hook_source(), _BENCH_MARK))
        offenders = [line.strip() for line in code.splitlines()
                     if line.strip().split(" ")[0].lstrip("(").split("/")[-1]
                     in _EXTERNAL_FILTERS]
        assert not offenders, offenders

    def test_the_glob_is_tested_before_the_file_is_read(self) -> None:
        """The budget's own ordering: an ordinary Bash call must pay one glob test and
        nothing else, so the `benchmarks/` test has to precede the read in the source."""
        block = marker_block(_hook_source(), _BENCH_MARK)
        assert _BENCH_GLOB_NAME in block, block
        glob_at = block.index(_BENCH_GLOB_NAME)
        read_at = block.find('$(<')
        assert read_at == -1 or glob_at < read_at, (
            "the file read appears before the benchmarks/ glob, so every Bash command "
            "pays it"
        )
