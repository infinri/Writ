"""Skeletons for defect 2: Grep, Glob and ExitPlanMode never reach the seeder at all.

Plan: .claude/plans/2412ba38-51e1-4b73-895b-7b240a3c21d3/plan.md.

THE CHAIN THIS PINS. The seeder is only ever called from `load_hook_env`
(`bin/lib/common.sh`), and the one hooks.json entry matching Grep or Glob points at
`writ-debug-code-gate.sh`, which cannot call it: that script already runs its own
`STDIN_DATA=$(cat)` before it ever reaches `common.sh`, so `load_hook_env` is
unusable there (stdin is already consumed). `validate-exit-plan.sh` holds the same
shape for ExitPlanMode. So a sub-agent confined to Grep, Glob or ExitPlanMode never
seeds a cache at all, no matter how many times it calls those tools.

RED UNTIL, per the plan:
  - `bin/lib/common.sh` gains `writ_cache_already_seeded <cache_file>` (the jq fast
    path) and `writ_seed_subagent_from_fields <agent_id> <parent_session_id>
    <agent_type>` (the entry point taking fields instead of reading stdin), with
    `_writ_seed_subagent_cache` becoming a one-line wrapper over the latter.
  - `hooks/scripts/writ-debug-code-gate.sh` prints three lines from its existing
    `STDIN_DATA` parse (agent_id, session_id, agent_type) and calls
    `writ_seed_subagent_from_fields` with them, before its `[ -n "$SID" ] || exit 0`
    early exit.
  - `hooks/scripts/validate-exit-plan.sh` calls `parsed_fields` on its own
    `STDIN_JSON` for the same three fields and calls the same entry point, above its
    `writ_require_session ... || exit 0` line.
  - `tests/_inventory.py` gains `pretooluse_tool_scripts(manifest_path=...)` and
    `seeder_reaching_scripts(scripts_dir=...)`, the two source-derived populations
    `TestSeederReachabilityCoverage` consumes.

Every test below fails today: the bash functions above do not exist yet (a
`bash -c` call to them exits 127), the two hooks do not call any seeding entry point
(nothing is ever written to the child's cache), and the two `tests._inventory`
functions this module needs are not attributes of that module yet.

WHAT WOULD MAKE ONE OF THESE PASS VACUOUSLY, and why this module refuses it:

  - NON-NEGOTIABLE (populations from artifacts): `TestSeederReachabilityCoverage` and
    `TestCoverageDetectorIsConditionalByMutation` never hardcode a list of hook or
    tool names. The covered-tool population comes from `tests._inventory.
    pretooluse_tool_scripts()` (derived from `hooks/hooks.json`) and
    `seeder_reaching_scripts()` (derived from hook source), imported as the MODULE
    (`import tests._inventory as inv`) rather than as bare names, so this file still
    collects while those two functions do not exist yet.
  - NON-NEGOTIABLE (positive control beside every negative): the jq-corpus capability
    is the model. `TestCacheAlreadySeededParityWithJQ` and
    `TestCacheAlreadySeededWithoutJQ` share ONE corpus (`CACHE_DOC_LABELS`) run
    through BOTH the shell predicate (`writ_cache_already_seeded`, a real subprocess)
    and the python predicate (`already_seeded`, a real import), and each class
    carries an explicit assertion that the corpus contains at least one document each
    arm calls seeded -- otherwise a `WRIT_NO_JQ` arm that answers "not seeded"
    unconditionally would pass by construction, not by having actually deferred on an
    uncertain case.
  - NON-NEGOTIABLE (conditional by mutation): `TestCoverageDetectorIsConditionalByMutation`
    proves the coverage detector over a SYNTHETIC manifest and script tree, not the
    repo's own live registrations: commenting out the one reaching call must flip a
    tool from covered to uncovered, and restoring it must flip it back, in the same
    class, as two paired tests.
  - ENF-SYS-005 (cannot be mocked): every claim about the exec boundary (a hook that
    has already consumed stdin) and about the file-locked cache is driven through a
    REAL subprocess. `_call_writ_cache_already_seeded` runs `bash -c '... source
    common.sh ...'`; `TestGrepGateSeedsThroughTheRealHook`,
    `TestGrepGateRowIdentity`, `TestGrepGateRefusesHostileEnvelopes` and
    `TestSeedingFaultsNeverFailAHook` all run the real
    `hooks/scripts/writ-debug-code-gate.sh` as a subprocess with a real stdin
    envelope, against a real, file-locked `WRIT_CACHE_DIR`. Patching `seed_subagent_
    cache` in-process, or asserting against a hand-built envelope-parsing function
    instead of the shipped script, would prove nothing about whether the REAL hook,
    which has ALREADY read stdin by the time this code runs, can still reach the
    seeder.

ISOLATION. Every test that touches a cache directory gets its own tmp tree via the
`cache_dir` / `sinks` fixtures below; nothing here reads the operator's real session
cache, the real friction/metrics streams, or the repo's own blackbox capture log.

WHAT THIS MODULE DOES NOT COVER. The python-only half of this cycle (defect 1: a
denial artifact disarming the seeder, and the `already_seeded` predicate's own
seeding-authority consequences) is `tests/test_subagent_seed_after_denial.py`'s job.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

import tests._inventory as inv

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"
GREP_GATE_HOOK = REPO / "hooks" / "scripts" / "writ-debug-code-gate.sh"
EXIT_PLAN_HOOK = REPO / "hooks" / "scripts" / "validate-exit-plan.sh"
HOOKS_JSON = REPO / "hooks" / "hooks.json"

PARENT = "33333333-4444-5555-6666-777777777777"
AGENT = "c0123456789abcdef"

PARENT_STATE = {
    "mode": "work",
    "current_phase": "implementation",
    "gates_approved": ["phase-a", "test-skeletons"],
    "project_root": "",
}

# The named corpus (plan Test design section): eight document shapes, none of them a
# hook or tool name, so hardcoding this list does not run afoul of the
# no-hardcoded-population rule. Both `TestCacheAlreadySeededParityWithJQ` and
# `TestCacheAlreadySeededWithoutJQ` run the SAME list through both predicates.
CACHE_DOC_LABELS = [
    "denial_created",
    "lazy_seeded",
    "start_seeded",
    "legacy_is_subagent",
    "empty_file",
    "non_object_json",
    "unparseable_bytes",
    "missing_path",
]

# Which of the labels above a correct implementation must call ALREADY SEEDED. Used
# by the positive-control assertions in both parity classes so "the corpus contains a
# seeded document" is checked against a real, named expectation rather than merely
# "at least one of the eight happened to come back true".
EXPECTED_SEEDED_LABELS = {"lazy_seeded", "start_seeded", "legacy_is_subagent"}


def _require_jq() -> None:
    """The jq arm cannot be exercised on a host without jq, and a silently skipped arm
    would make the parity claim vacuous rather than merely unrun."""
    if not shutil.which("jq"):
        pytest.skip("jq is not installed: the jq arm of writ_cache_already_seeded "
                    "cannot be exercised on this host")


def _require(module, *names) -> None:
    """Fail with the missing attribute names, rather than an AttributeError, when a
    skeleton's target function has not been written yet."""
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


def _seed_module():
    try:
        from writ.session import subagent_seed
    except ImportError as exc:  # pragma: no cover - RED path
        pytest.fail(f"skeleton: writ/session/subagent_seed.py does not exist yet ({exc})")
    return subagent_seed


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """A fresh `WRIT_CACHE_DIR` under `tmp_path`, for the in-process python arm of the
    parity tests. Bash subprocess calls in this module take their own cache directory
    explicitly, via `WRIT_CACHE_DIR` in the child's env, never inherited from this
    fixture alone."""
    d = tmp_path / "cache"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WRIT_CACHE_DIR", str(d))
    return d


@pytest.fixture()
def sinks(tmp_path):
    """(cache_dir, friction_log_path) for a real hook subprocess: two files under
    `tmp_path`, never the operator's real `~/.claude` tree."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache, tmp_path / "friction.jsonl"


SEEDER_ENTRY_POINT = "writ_seed_subagent_from_fields"
SYNTHETIC_TOOL = "SyntheticTool"
SYNTHETIC_SCRIPT = "synthetic-gate.sh"


def _cache_file(cache_dir: Path, session_id: str) -> Path:
    return cache_dir / f"writ-session-{session_id}.json"


def _doc_session(label: str) -> str:
    """A distinct, `_VALID_ID`-shaped session id per corpus label, so the eight
    documents can coexist under one cache directory without overwriting each other."""
    return f"{AGENT}-{label}"


def _write_parent_cache(cache_dir: Path, session_id: str = PARENT,
                        state: dict | None = None) -> Path:
    """Write a parent session's cache file directly: the governance state a real
    Grep-gate dispatch should inherit through the seeder."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_file(cache_dir, session_id)
    path.write_text(json.dumps(PARENT_STATE if state is None else state))
    return path


def _write_cache_document(cache_dir: Path, session_id: str, label: str) -> Path | None:
    """Materialize one of `CACHE_DOC_LABELS` under `cache_dir` as
    `writ-session-<session_id>.json`, or write nothing at all for `"missing_path"`.

    Each shape is built through the REAL production path where one exists
    (`denial_created` via the real `gates._log_gate_denial`; `lazy_seeded` and
    `legacy_is_subagent` via the real `seed_mod.seed_subagent_cache`; `start_seeded`
    with `cache_source=CACHE_SOURCE_START`), and by direct byte writes only for the
    three shapes that have no real producer to call (`empty_file`, `non_object_json`,
    `unparseable_bytes`). Returns the path written, or None for `"missing_path"`.
    """
    path = _cache_file(cache_dir, session_id)
    if label == "missing_path":
        return None
    if label == "empty_file":
        path.write_bytes(b"")
        return path
    if label == "non_object_json":
        path.write_text('["cache_source", "is_subagent"]')
        return path
    if label == "unparseable_bytes":
        path.write_bytes(b'{"cache_source": "lazy_seed"')
        return path
    if label == "denial_created":
        from writ.session import gates
        from writ.session.cache import _read_cache

        gates._log_gate_denial(session_id, _read_cache(session_id), "test-skeletons",
                               "/home/lucio.saldivar/workspaces/ai-stack/src/thing.py",
                               "[ENF-GATE-MODE] No mode declared.")
        return path
    mod = _seed_module()
    _write_parent_cache(cache_dir)
    source = mod.CACHE_SOURCE_START if label == "start_seeded" else mod.CACHE_SOURCE_LAZY
    assert mod.seed_subagent_cache(session_id, PARENT, cache_source=source,
                                   envelope_agent_type="writ-planner") is True
    if label == "legacy_is_subagent":
        # The one shape with no producer left: `cache_source` did not exist when these
        # caches were written, so the real seeder's output is stripped of that one key
        # rather than hand-built around it.
        doc = json.loads(path.read_text())
        doc.pop("cache_source")
        path.write_text(json.dumps(doc))
    return path


def _load_document(path: Path | None) -> dict | None:
    """The document at `path` as the python predicate's caller would hold it: the
    parsed object when it is a JSON object, and None for every state that yields no
    usable cache document (absent, empty, unparseable, or not an object)."""
    if path is None or not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _call_writ_cache_already_seeded(cache_file: Path, *, no_jq: bool = False) -> bool:
    """Run the REAL bash function `writ_cache_already_seeded <cache_file>` as a
    subprocess (`bash -c 'source common.sh; writ_cache_already_seeded "$1"'`), and
    translate its exit code into a verdict.

    Per the plan: exit 0 means a POSITIVE reading (the file declares a non-empty
    `cache_source` or `is_subagent: true`) and is returned as True; exit 1 means
    everything else, including every state the fast path cannot resolve, and is
    returned as False. Any other exit code is a skeleton failure in its own right
    (`pytest.fail`), never silently folded into True or False.

    `no_jq=True` sets `WRIT_NO_JQ=1` in the child's environment, the seam capability
    7 exercises.
    """
    env = {**os.environ}
    if no_jq:
        env["WRIT_NO_JQ"] = "1"
    else:
        env.pop("WRIT_NO_JQ", None)
    script = (
        f"source {shlex.quote(str(COMMON_SH))}; "
        "writ_cache_already_seeded \"$1\""
    )
    result = subprocess.run(["bash", "-c", script, "bash", str(cache_file)],
                            capture_output=True, text=True, env=env, timeout=60)
    if result.returncode not in (0, 1):
        pytest.fail(
            f"writ_cache_already_seeded exited {result.returncode} for {cache_file}: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return result.returncode == 0


def _call_already_seeded(cache: dict | None) -> bool:
    """The python arm of the same parity question: `seed_mod.already_seeded(cache)`,
    called on a dict read straight off the same file `_call_writ_cache_already_
    seeded` was pointed at (never a re-derived or hand-built dict), so the two arms
    are compared over identical bytes.
    """
    mod = _seed_module()
    _require(mod, "already_seeded")
    return mod.already_seeded(cache)


def _run_hook(script: Path, *, cache: Path, friction: Path, stdin: str,
              extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a real hook script (`hooks/scripts/*.sh`) as a subprocess with `stdin` on
    its standard input and an isolated `WRIT_CACHE_DIR` / `WRIT_FRICTION_LOG`.

    THE EXEC BOUNDARY IS THE POINT (ENF-SYS-005): this is the one place in the
    codebase where the Grep-gate's inability to call `load_hook_env` (stdin already
    consumed) is real rather than assumed, so every test in
    `TestGrepGateSeedsThroughTheRealHook`, `TestGrepGateRowIdentity`,
    `TestGrepGateRefusesHostileEnvelopes` and `TestSeedingFaultsNeverFailAHook` goes
    through this function rather than calling a python stand-in for the bash script.
    """
    env = {
        **os.environ,
        "WRIT_CACHE_DIR": str(cache),
        "WRIT_FRICTION_LOG": str(friction),
        "WRIT_PORT": "59999",
        "WRIT_NO_AUTOSTART": "1",
        "WRIT_DIR": str(REPO),
        "SKILL_DIR": str(REPO),
        **(extra_env or {}),
    }
    cache.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["bash", str(script)], input=stdin, capture_output=True,
                          text=True, env=env, timeout=180)


def _grep_envelope(agent: str = AGENT, session: str = PARENT,
                   agent_type: str = "writ-planner") -> str:
    """The PreToolUse envelope Claude Code hands the Grep gate inside a sub-agent."""
    return json.dumps({"session_id": session, "agent_id": agent,
                       "agent_type": agent_type, "tool_name": "Grep",
                       "tool_input": {"pattern": "seed"},
                       "hook_event_name": "PreToolUse"})


def _cache_files(cache_dir: Path) -> set[str]:
    return {p.name for p in cache_dir.glob("writ-session-*.json")}


def _row_identities(cache_dir: Path) -> set[str]:
    """The ids the gate's OWN telemetry was filed under, read off the real artifact:
    `log_gate_decision` and `hook_instrument`'s exit trap both buffer to
    `writ-events-<session>.buf` under the session cache directory, and that session is
    exactly the `SID` the gate resolved for itself."""
    prefix, suffix = "writ-events-", ".buf"
    return {p.name[len(prefix):-len(suffix)] for p in cache_dir.glob(f"{prefix}*{suffix}")}


def _child_cache(cache_dir: Path, agent_id: str = AGENT) -> dict | None:
    """The child's cache dict as it exists on disk right now, or None."""
    path = _cache_file(cache_dir, agent_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _friction_rows(friction: Path) -> list[dict]:
    """Every JSON row appended to a real `WRIT_FRICTION_LOG` file, in order."""
    if not friction.exists():
        return []
    return [json.loads(line) for line in friction.read_text().splitlines() if line.strip()]


def _make_cache_dir_unwritable(cache_dir: Path) -> None:
    """Remove write permission from `cache_dir` itself (never a subprocess's HOME or
    any path outside `cache_dir`), so a real seed attempt against it fails exactly
    the way a read-only mount or a permissions mistake would in production."""
    if os.geteuid() == 0:
        pytest.skip("running as root: directory permissions do not refuse a write, so "
                    "the cache-write fault cannot be reproduced here")
    os.chmod(cache_dir, 0o500)


def _run_hook_against_unwritable(cache: Path, friction: Path) -> subprocess.CompletedProcess:
    """One real Grep dispatch against a cache directory nothing may write into, with the
    permission restored afterwards so `tmp_path` teardown can still remove it."""
    _make_cache_dir_unwritable(cache)
    try:
        return _run_hook(GREP_GATE_HOOK, cache=cache, friction=friction,
                         stdin=_grep_envelope())
    finally:
        os.chmod(cache, 0o700)


def _synthetic_pretooluse_tree(tmp_path: Path, *, tool: str, reaching: bool) -> tuple[Path, Path]:
    """Build a synthetic `hooks.json` naming exactly one PreToolUse tool, plus a
    matching one-script `hooks/scripts/` tree, and return
    `(manifest_path, scripts_dir)` for `tests._inventory.pretooluse_tool_scripts` and
    `.seeder_reaching_scripts` to be called against.

    `reaching=True` writes a script whose non-comment source calls
    `writ_seed_subagent_from_fields`; `reaching=False` writes the identical script
    with that one call commented out, which is the exact mutation
    `TestCoverageDetectorIsConditionalByMutation` pins as the difference between a
    tool reading covered and uncovered.
    """
    root = tmp_path / ("reaching" if reaching else "commented")
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    call = f'{SEEDER_ENTRY_POINT} "$AGENT_ID" "$SESSION_ID" "$AGENT_TYPE"'
    (scripts_dir / SYNTHETIC_SCRIPT).write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "STDIN_DATA=$(cat)\n"
        f"{call if reaching else '# ' + call}\n"
        "exit 0\n"
    )
    manifest_path = root / "hooks.json"
    manifest_path.write_text(json.dumps({"hooks": {"PreToolUse": [{
        "matcher": tool,
        "hooks": [{"type": "command",
                   "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/"
                              + SYNTHETIC_SCRIPT}],
    }]}}))
    return manifest_path, scripts_dir


def _jq_seeded_labels(cache_dir: Path) -> set[str]:
    """Which labels the jq arm of the REAL bash predicate calls already seeded, over the
    whole corpus materialized once under one cache directory."""
    seeded = set()
    for label in CACHE_DOC_LABELS:
        session = _doc_session(label)
        path = _write_cache_document(cache_dir, session, label)
        target = path if path is not None else _cache_file(cache_dir, session)
        if _call_writ_cache_already_seeded(target):
            seeded.add(label)
    return seeded


def _direct_seeder_callers() -> dict[str, str]:
    """`{script_name: source_text}` for every real hook script under
    `hooks/scripts/` whose non-comment source calls `writ_seed_subagent_from_fields`
    directly (not merely `load_hook_env`, which already guarantees call-site order by
    construction). Used by `TestSeederCallPrecedesEarlyExit`, which is why this reads
    the REPO's real scripts rather than a synthetic tree: call-site order within the
    shipped files is exactly what that capability pins.
    """
    out: dict[str, str] = {}
    for path in sorted((REPO / "hooks" / "scripts").glob("*.sh")):
        # Comment lines are BLANKED, not dropped, so the line indices this returns are the
        # file's own: a mention inside a comment can neither register the script as a
        # caller nor shift the position of the real call against the first early exit.
        source = "\n".join(
            "" if line.lstrip().startswith("#") else line
            for line in path.read_text(encoding="utf-8", errors="replace").split("\n")
        )
        if SEEDER_ENTRY_POINT in source:
            out[path.name] = source
    return out


# --------------------------------------------------------------------------- #
# Capabilities 6-7: writ_cache_already_seeded / already_seeded parity.
# --------------------------------------------------------------------------- #

class TestCacheAlreadySeededParityWithJQ:
    """Capability 6. With jq available, the bash fast path and the python predicate
    must agree on every document in `CACHE_DOC_LABELS`. Both arms are REAL callers
    (a subprocess, an import): this class asserts an equality of two independent
    implementations' output, never one re-implemented in terms of the other.
    """

    @pytest.mark.parametrize("label", CACHE_DOC_LABELS)
    def test_bash_and_python_agree_on_every_document(self, cache_dir, label) -> None:
        _require_jq()
        session = _doc_session(label)
        path = _write_cache_document(cache_dir, session, label)
        target = path if path is not None else _cache_file(cache_dir, session)
        shell = _call_writ_cache_already_seeded(target)
        python = _call_already_seeded(_load_document(path))
        assert shell == python, (
            f"the two spellings of already-seeded disagree on {label}: "
            f"bash={shell} python={python}"
        )
        assert shell is (label in EXPECTED_SEEDED_LABELS), (
            f"{label} was judged {shell}, which is not what a correct predicate answers"
        )

    def test_the_corpus_contains_both_seeded_and_unseeded_documents(self, cache_dir) -> None:
        """POSITIVE CONTROL for this class and for `TestCacheAlreadySeededWithoutJQ`
        below: at least one label in `CACHE_DOC_LABELS` (`EXPECTED_SEEDED_LABELS`)
        must actually read as seeded under the jq arm, and at least one must not, or
        every agreement above could be explained by a corpus that never exercises
        the seeded branch of either predicate at all."""
        _require_jq()
        seeded = _jq_seeded_labels(cache_dir)
        assert seeded == EXPECTED_SEEDED_LABELS, (
            f"the corpus no longer exercises both branches: seeded={sorted(seeded)}"
        )
        assert set(CACHE_DOC_LABELS) - seeded, "no document in the corpus reads unseeded"


class TestCacheAlreadySeededWithoutJQ:
    """Capability 7. With `WRIT_NO_JQ` set, the fast path has no python fallback arm
    by design (the plan's own "no fallback arm" decision): every document, resolved
    or not, must answer "not seeded" and defer to the real seeder, which is the
    authority and declines cheaply.
    """

    @pytest.mark.parametrize("label", CACHE_DOC_LABELS)
    def test_every_document_reads_as_not_seeded_without_jq(self, cache_dir, label) -> None:
        session = _doc_session(label)
        path = _write_cache_document(cache_dir, session, label)
        target = path if path is not None else _cache_file(cache_dir, session)
        assert _call_writ_cache_already_seeded(target, no_jq=True) is False, (
            f"the fast path answered 'seeded' for {label} with no jq to read it, which "
            "would skip the seeder on an unresolved state"
        )

    def test_the_jq_arm_calls_the_same_corpus_seeded_at_least_once(self, cache_dir) -> None:
        """POSITIVE CONTROL, restated for this class specifically: the "always
        answers not seeded" assertion above is meaningful only if the SAME documents
        are provably seeded under the jq arm (`_call_writ_cache_already_seeded(...,
        no_jq=False)`), which is what this test pins. Without it, an unconditional
        `return False` in the WRIT_NO_JQ arm would look identical to a correct
        fail-safe defer."""
        _require_jq()
        assert _jq_seeded_labels(cache_dir) == EXPECTED_SEEDED_LABELS, (
            "the WRIT_NO_JQ arm's 'always not seeded' claim is vacuous: the jq arm does "
            "not call these same documents seeded"
        )


# --------------------------------------------------------------------------- #
# Capability 8: the real Grep gate seeds end to end.
# --------------------------------------------------------------------------- #

class TestGrepGateSeedsThroughTheRealHook:
    """Capability 8. `hooks/scripts/writ-debug-code-gate.sh`, run as a real
    subprocess against a real cache directory, must seed the child cache for a Grep
    dispatch, and must still behave exactly as it does today from the caller's
    point of view: exit 0, nothing on stdout.
    """

    def test_a_grep_dispatch_seeds_the_child_cache(self, sinks) -> None:
        mod = _seed_module()
        cache, friction = sinks
        _write_parent_cache(cache)
        _run_hook(GREP_GATE_HOOK, cache=cache, friction=friction, stdin=_grep_envelope())
        child = _child_cache(cache)
        assert child is not None, (
            "a Grep dispatch inside a sub-agent seeded nothing: the only hook registered "
            "for Grep still cannot reach the seeder"
        )
        assert child["mode"] == PARENT_STATE["mode"]
        assert child["gates_approved"] == PARENT_STATE["gates_approved"]
        assert child["parent_session_id"] == PARENT
        assert child["cache_source"] == mod.CACHE_SOURCE_LAZY

    def test_the_gate_still_exits_zero_and_emits_nothing_on_stdout(self, sinks) -> None:
        """The seeding call must sit inside the existing `type ... >/dev/null 2>&1`
        guard and must never itself print to stdout, which is a model channel on
        this event; a stray print here would leak into Claude Code's tool-result
        stream."""
        cache, friction = sinks
        _write_parent_cache(cache)
        result = _run_hook(GREP_GATE_HOOK, cache=cache, friction=friction,
                           stdin=_grep_envelope())
        assert result.returncode == 0, f"the gate failed: {result.stderr!r}"
        assert result.stdout == "", (
            f"the gate printed on a model channel: {result.stdout!r}"
        )
        assert _child_cache(cache) is not None, (
            "silence was bought by the seeding call never running"
        )


# --------------------------------------------------------------------------- #
# Capability 9: the gate's own row identity is unchanged.
# --------------------------------------------------------------------------- #

class TestGrepGateRowIdentity:
    """Capability 9. Widening the gate's existing single-line parse to three lines
    must not change which id its OWN telemetry (SID, and therefore every
    `log_gate_decision` row) is filed under: agent_id first, session_id only when
    agent_id is absent, exactly as the pre-widening one-line parse already behaved.
    """

    def test_a_payload_carrying_both_ids_keys_on_agent_id(self, sinks) -> None:
        cache, friction = sinks
        _write_parent_cache(cache)
        _run_hook(GREP_GATE_HOOK, cache=cache, friction=friction, stdin=_grep_envelope())
        assert _row_identities(cache) == {AGENT}, (
            "the widened parse moved this gate's own rows off the agent id, so a "
            "sub-agent's reads would be filed under its parent"
        )

    def test_a_payload_carrying_only_session_id_keys_on_session_id(self, sinks) -> None:
        cache, friction = sinks
        _write_parent_cache(cache)
        envelope = json.loads(_grep_envelope())
        del envelope["agent_id"]
        _run_hook(GREP_GATE_HOOK, cache=cache, friction=friction,
                  stdin=json.dumps(envelope))
        assert _row_identities(cache) == {PARENT}, (
            "a payload with no agent_id no longer falls back to session_id, which is "
            "how this gate's rows came to be filed under the literal id 'unknown'"
        )


# --------------------------------------------------------------------------- #
# Capability 10: hostile envelopes seed nothing and the gate still exits 0.
# --------------------------------------------------------------------------- #

class TestGrepGateRefusesHostileEnvelopes:
    """Capability 10. An id carrying a newline could forge a second line and shift
    the widened parse's positional split; a payload that is not a JSON object has no
    fields to widen at all. Both must seed nothing and leave the gate's own contract
    (exit 0) unchanged.
    """

    def test_ids_carrying_newlines_seed_nothing(self, sinks) -> None:
        """THE ATTACK, spelled out: `agent_id` carries a newline and a valid-looking
        parent after it, and the envelope names no real session. Unstripped, the
        forged tail becomes line 2 and is read as the parent, so the gate would seed
        a child from an id the payload invented. Stripped, `agent_id` is one mangled
        token, the parent is empty, and the seeder declines before any process."""
        cache, friction = sinks
        _write_parent_cache(cache)
        before = _cache_files(cache)
        result = _run_hook(GREP_GATE_HOOK, cache=cache, friction=friction,
                           stdin=json.dumps({
                               "agent_id": f"{AGENT}\n{PARENT}", "session_id": "",
                               "agent_type": "writ-planner", "tool_name": "Grep",
                               "tool_input": {"pattern": "seed"},
                               "hook_event_name": "PreToolUse"}))
        assert result.returncode == 0
        assert _cache_files(cache) == before, (
            "a forged line created a session cache: the positional split shifted"
        )

    def test_a_non_object_envelope_seeds_nothing(self, sinks) -> None:
        cache, friction = sinks
        _write_parent_cache(cache)
        before = _cache_files(cache)
        result = _run_hook(GREP_GATE_HOOK, cache=cache, friction=friction,
                           stdin='["agent_id", "session_id", "agent_type"]')
        assert result.returncode == 0
        assert _cache_files(cache) == before

    def test_a_well_formed_envelope_still_seeds_through_the_same_probe(self, sinks) -> None:
        """POSITIVE CONTROL for both negatives above, run through the identical
        subprocess call shape (`_run_hook` against `GREP_GATE_HOOK`): a well-formed
        envelope in the same harness must still seed, so "seeds nothing" for a
        hostile envelope cannot be explained by the harness never seeding at all."""
        cache, friction = sinks
        _write_parent_cache(cache)
        before = _cache_files(cache)
        result = _run_hook(GREP_GATE_HOOK, cache=cache, friction=friction,
                           stdin=_grep_envelope())
        assert result.returncode == 0
        assert _cache_files(cache) - before == {_cache_file(cache, AGENT).name}


# --------------------------------------------------------------------------- #
# Capability 11: every PreToolUse tool has a script that reaches the seeder.
# --------------------------------------------------------------------------- #

class TestSeederReachabilityCoverage:
    """Capability 11. The population is DERIVED, never hardcoded: every tool named by
    a PreToolUse matcher in the real `hooks/hooks.json`
    (`inv.pretooluse_tool_scripts()`) must have at least one registered script whose
    non-comment source calls `load_hook_env` or `writ_seed_subagent_from_fields`
    (`inv.seeder_reaching_scripts()`). A wildcard matcher counts as covering every
    named tool, matching `pretooluse_tool_scripts`'s own documented treatment of
    `""`/`*`/`.*`.
    """

    def test_every_pretooluse_tool_has_a_reaching_script(self) -> None:
        _require(inv, "pretooluse_tool_scripts", "seeder_reaching_scripts")
        tools = inv.pretooluse_tool_scripts()
        reaching = set(inv.seeder_reaching_scripts())
        assert tools, "no PreToolUse tool was derived from hooks.json"
        assert reaching, "no hook script was derived as reaching the seeder"
        uncovered = {tool: scripts for tool, scripts in tools.items()
                     if not set(scripts) & reaching}
        assert uncovered == {}, (
            "a sub-agent confined to these tools can never be governed, because no "
            f"script registered for them reaches the seeder: {uncovered}"
        )


# --------------------------------------------------------------------------- #
# Capability 12: the coverage detector is conditional by mutation.
# --------------------------------------------------------------------------- #

class TestCoverageDetectorIsConditionalByMutation:
    """Capability 12. Proof that the detector is not vacuously green: over a
    SYNTHETIC manifest and script tree (`_synthetic_pretooluse_tree`) built by this
    module, not the repo's own registrations, commenting out the one reaching call
    must flip the tool from covered to uncovered, and restoring it must flip it
    back. The two tests are paired deliberately: either one passing alone would
    leave the OTHER direction of the mutation unproven.
    """

    def test_a_tool_reads_uncovered_when_its_reaching_call_is_commented_out(
        self, tmp_path
    ) -> None:
        _require(inv, "pretooluse_tool_scripts", "seeder_reaching_scripts")
        manifest, scripts_dir = _synthetic_pretooluse_tree(
            tmp_path, tool=SYNTHETIC_TOOL, reaching=False)
        tools = inv.pretooluse_tool_scripts(manifest_path=manifest)
        reaching = set(inv.seeder_reaching_scripts(scripts_dir=scripts_dir))
        assert tools == {SYNTHETIC_TOOL: [SYNTHETIC_SCRIPT]}
        assert reaching == set(), (
            "a call that exists only inside a comment was counted as reaching the "
            "seeder, which is exactly how the pre-fix Grep gate would have read covered"
        )
        assert not set(tools[SYNTHETIC_TOOL]) & reaching

    def test_restoring_the_call_flips_the_same_tool_back_to_covered(self, tmp_path) -> None:
        _require(inv, "pretooluse_tool_scripts", "seeder_reaching_scripts")
        manifest_off, scripts_off = _synthetic_pretooluse_tree(
            tmp_path, tool=SYNTHETIC_TOOL, reaching=False)
        manifest_on, scripts_on = _synthetic_pretooluse_tree(
            tmp_path, tool=SYNTHETIC_TOOL, reaching=True)
        off = set(inv.seeder_reaching_scripts(scripts_dir=scripts_off))
        on = set(inv.seeder_reaching_scripts(scripts_dir=scripts_on))
        tools = inv.pretooluse_tool_scripts(manifest_path=manifest_on)
        assert inv.pretooluse_tool_scripts(manifest_path=manifest_off) == tools
        assert not set(tools[SYNTHETIC_TOOL]) & off
        assert set(tools[SYNTHETIC_TOOL]) & on == {SYNTHETIC_SCRIPT}, (
            "uncommenting the one reaching call did not flip the tool to covered, so "
            "the detector answers the same thing either way"
        )


# --------------------------------------------------------------------------- #
# Capability 13: the seeding call precedes the first early exit.
# --------------------------------------------------------------------------- #

class TestSeederCallPrecedesEarlyExit:
    """Capability 13. For every REAL hook script that calls
    `writ_seed_subagent_from_fields` directly (not through `load_hook_env`), the
    call's own line index must be below the index of that script's first line
    containing `exit 0`, so a future edit to the early-exit guard cannot silently
    drop seeding ahead of it.
    """

    def test_every_direct_caller_invokes_the_seeder_before_its_first_early_exit(
        self,
    ) -> None:
        callers = _direct_seeder_callers()
        assert callers, (
            f"no hook script calls {SEEDER_ENTRY_POINT} directly, so the tools whose "
            "only hook has already consumed stdin still reach no seeder"
        )
        for name, source in callers.items():
            lines = source.split("\n")
            # The LAST mention, not the first: a `type <name>` guard sits above the call
            # in one caller, and taking the earliest index would let a guard that precedes
            # the early exit hide a call that follows it.
            mentions = [i for i, line in enumerate(lines) if SEEDER_ENTRY_POINT in line]
            exits = [i for i, line in enumerate(lines) if "exit 0" in line]
            assert exits, f"{name} has no early exit for the seeding call to precede"
            assert max(mentions) < exits[0], (
                f"{name} reaches an early exit at line {exits[0] + 1} before its seeding "
                f"call at line {max(mentions) + 1}, so the agent can leave ungoverned"
            )


# --------------------------------------------------------------------------- #
# Capability 14: seeding faults never fail a hook.
# --------------------------------------------------------------------------- #

class TestSeedingFaultsNeverFailAHook:
    """Capability 14. A sub-agent that cannot inherit governance is a gap to report,
    never a hook failure. An unwritable cache directory must not turn the Grep
    gate's own decision into a broken tool call, and the fault must still be
    recorded exactly once, in the bounded shape `log_seed_failure` already writes.
    """

    def test_an_unwritable_cache_directory_still_exits_zero_with_no_stdout(
        self, sinks
    ) -> None:
        cache, friction = sinks
        _write_parent_cache(cache)
        result = _run_hook_against_unwritable(cache, friction)
        assert result.returncode == 0, (
            f"a seeding fault failed the gate: {result.stderr!r}"
        )
        assert result.stdout == "", (
            f"a seeding fault printed on a model channel: {result.stdout!r}"
        )
        assert _child_cache(cache) is None, (
            "the cache was written after all, so no fault was reproduced"
        )

    def test_exactly_one_subagent_seed_failed_row_is_written(self, sinks) -> None:
        cache, friction = sinks
        _write_parent_cache(cache)
        _run_hook_against_unwritable(cache, friction)
        rows = [r for r in _friction_rows(friction)
                if r.get("event") == "subagent_seed_failed"]
        assert len(rows) == 1, (
            f"expected exactly one subagent_seed_failed row, got {len(rows)}: "
            f"{[r.get('event') for r in _friction_rows(friction)]}"
        )
        assert rows[0].get("cache_source") == _seed_module().CACHE_SOURCE_LAZY
        assert rows[0].get("session") == AGENT


# Documents that no production path writes today: `writ/session/cache.py` defaults
# `cache_source` to "" and `is_subagent` to False, and the seeder writes a
# CACHE_SOURCE_* string and True. They exist here because the two predicates are
# deliberately NOT spelled the same, and the direction of that difference is the
# safety property.
NONCANONICAL_DOCS = {
    "cache_source_is_a_number": {"cache_source": 5},
    "cache_source_is_a_list": {"cache_source": ["lazy_seed"]},
    "is_subagent_is_a_truthy_string": {"is_subagent": "true"},
    "is_subagent_is_one": {"is_subagent": 1},
}


class TestTheShellVerdictIsASubsetOfThePythonVerdict:
    """THE FAIL DIRECTION, pinned as a property rather than left true by construction.

    `already_seeded` uses python truthiness; the jq arm answers seeded only for a
    non-empty STRING `cache_source` or a literal `true` `is_subagent`. So the two
    disagree on documents like `{"cache_source": 5}`. That is safe in exactly one
    direction: shell-seeded must IMPLY python-seeded, so the shell can only ever skip
    a seed that python would also have skipped. If someone later loosens the jq arm or
    tightens the python one, the invariant flips and the seeder starts skipping work it
    should do, which is the defect this whole cycle exists to fix.

    The parity corpus cannot carry these documents: it asserts the two arms AGREE, and
    these are the documents where they must be allowed to differ.
    """

    @pytest.mark.parametrize("label", sorted(NONCANONICAL_DOCS))
    def test_shell_seeded_implies_python_seeded(self, cache_dir, label) -> None:
        _require_jq()
        doc = NONCANONICAL_DOCS[label]
        path = _cache_file(cache_dir, AGENT)
        path.write_text(json.dumps(doc))
        shell = _call_writ_cache_already_seeded(path)
        python = _call_already_seeded(doc)
        assert not (shell and not python), (
            f"{label}: the shell fast path called this cache seeded while python did "
            f"not, so the seeder would skip a cache python considers unseeded"
        )

    def test_the_control_document_is_seeded_by_both(self, cache_dir) -> None:
        """POSITIVE CONTROL. Every assertion above is satisfied by a shell arm that
        always answers 'not seeded', so this pins that it can still answer yes."""
        _require_jq()
        doc = {"cache_source": "lazy_seed", "is_subagent": True}
        path = _cache_file(cache_dir, AGENT)
        path.write_text(json.dumps(doc))
        assert _call_writ_cache_already_seeded(path) is True
        assert _call_already_seeded(doc) is True
