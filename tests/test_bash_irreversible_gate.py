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

Per ENF-GATE-007: skeletons written and approved before implementation.
Per TEST-ISOLATE-003: every run gets its own WRIT_FRICTION_LOG and WRIT_CACHE_DIR, and no
test executes any of these commands. They are fed to the hook as strings.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK_SH = str(REPO / "hooks" / "scripts" / "writ-bash-write-gate.sh")

# Real project files, used because the predicate reads CONTENT rather than matching a path.
DESTRUCTIVE_SCRIPT = "benchmarks/bench_targets.py"   # carries DETACH DELETE
CLEAN_SCRIPT = "writ/graph/migrate.py"               # carries none, and is routine


def _run(cmd: str, tmp_path: Path, sid: str = "irrev-test") -> tuple[dict | None, list[dict]]:
    """Feed one command to the hook. Returns (hookSpecificOutput or None, logged rows)."""
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
    proc = subprocess.run(["bash", HOOK_SH], input=envelope, cwd=str(REPO),
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
