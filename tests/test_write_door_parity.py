"""The two write doors must decide alike: Write/Edit and Bash, one predicate, one verdict.

THE DEFECT THIS PINS (cycle L, D2). Commit e881c6d put a project boundary in
writ/session/project_boundary.py and wired it into _can_write_check, so the Write tool began
refusing out-of-project targets. The Bash write gate's classifier emitted a row only for a
target under its cwd, so an out-of-repo target produced no row, reached no gate, and left no
audit line: measured live, a heredoc write to ~/.claude/projects/<encoded>/memory succeeded
through Bash minutes after the Write tool refused the same path.

WHY A PARITY PROPERTY AND NOT TWO UNIT TESTS. A per-door test of either door reads green
while the doors disagree, and the DISAGREEMENT was the defect. So each row computes both
verdicts for one target in one session state and asserts they agree, in the pre-approval and
the post-approval state, and the mutation tests at the bottom show the property going red
when either half of the fix is removed.

THE HARNESS IS REAL ON BOTH SIDES. The Write door is _can_write_check in process; the Bash
door is the actual hook script driven with a real PreToolUse envelope and a real write
command (the hook only classifies, so nothing is written). WRIT_PORT points at a dead port on
purpose, so the hook takes its documented daemon-unreachable fallback (the same local
can-write the Write tool uses) and these tests never skip on "no daemon".
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
HOOK_SH = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-bash-write-gate.sh")

# The table's keys, as a constant the parametrization walks. `_targets` builds the paths from
# the fixture; the non-vacuity test asserts the two sets are identical, so a row cannot be
# dropped from one side without a failure.
_TARGET_LABELS = (
    "in the repo",
    "os scratch zone",
    "real /tmp",
    "this project's memory dir",
    "another project's memory dir",
    "global claude settings",
    "plainly outside everything",
)

# The two mutations, as the exact shipped text. `_mutant_hook` asserts the replacement
# changed the source, so if either block is reworded these pins fail loudly instead of
# passing against an unmutated script.
_PRODUCER_ROW = '    else:\n        print(f"outside\\t{ap}")\n'
_CONSUMER_TEST = (
    '    case "$kind" in\n'
    '        local|outside) ;;\n'
    '        *) continue ;;\n'
    '    esac\n'
)


def _imp(name):
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module(name)


@pytest.fixture()
def doors(tmp_path, monkeypatch) -> SimpleNamespace:
    """One project, one HOME, one scratch zone, and two doors that read all three alike."""
    cache, logs = tmp_path / "cache", tmp_path / "logs"
    root, home = tmp_path / "proj", tmp_path / "home"
    zone, other, elsewhere = tmp_path / "osscratch", tmp_path / "other-proj", tmp_path / "elsewhere"
    for d in (cache, logs, root / "src", home / ".claude", zone, other, elsewhere):
        d.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()

    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache))
    monkeypatch.setenv("WRIT_LOG_ROOT", str(logs))
    # The suite's autouse fixture funnels every stream into one friction file; drop it so
    # this test reads the real typed audit stream the hook writes in production.
    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
    monkeypatch.setenv("WRIT_PORT", "19999")
    monkeypatch.setenv("WRIT_NO_AUTOSTART", "1")
    monkeypatch.setenv("HOME", str(home))
    # BOTH spellings of the scratch zone, still: the Bash door's subprocess writes its own
    # temp files under this directory and `tmp_path` (used by the "plainly outside
    # everything" / memory-dir targets elsewhere in this fixture) must stay outside it, so
    # both spellings still matter for THAT. What no longer matters is which zone
    # `in_scratch_zone` itself resolves: it is a pure three-argument predicate now
    # (`writ/session/project_boundary.py` imports no `tempfile`), and both doors read the
    # zone from `cache["scratch_zone"]` (stamped below by `_pre_approval`/`_post_approval`)
    # instead of resolving `tempfile.gettempdir()` live. Before this cycle, this double
    # spelling was the ONLY thing that stopped the two doors reading different zones for
    # this fixture's "os scratch zone" row -- that is what the stamp does now.
    # `tests/test_scratch_zone_process_parity.py` is the module that measures the
    # divergence this neutralization exists to avoid, with two REAL processes and two REAL
    # different `TMPDIR` values.
    monkeypatch.setenv("TMPDIR", str(zone))
    monkeypatch.setattr(tempfile, "tempdir", str(zone))

    locators = _imp("writ.session.locators")
    memory = locators._project_transcript_dir(
        os.path.realpath(str(root)), home / ".claude") / "memory"
    other_memory = locators._project_transcript_dir(
        os.path.realpath(str(other)), home / ".claude") / "memory"
    for d in (memory, other_memory):
        d.mkdir(parents=True, exist_ok=True)

    return SimpleNamespace(root=root, home=home, zone=zone, elsewhere=elsewhere,
                           logs=logs, memory=memory, other_memory=other_memory,
                           env=dict(os.environ))


def _targets(d: SimpleNamespace) -> dict[str, str]:
    """One path per label. `real /tmp` is a plain out-of-repo path here, because TMPDIR
    moves the exempt zone: the row exists so the brief's spelling is covered and so the
    exemption cannot be mistaken for "anything called /tmp"."""
    return {
        "in the repo": str(d.root / "src" / "app.py"),
        "os scratch zone": str(d.zone / "scratch.py"),
        "real /tmp": "/tmp/writ-parity-probe.py",
        "this project's memory dir": str(d.memory / "MEMORY.md"),
        "another project's memory dir": str(d.other_memory / "MEMORY.md"),
        "global claude settings": str(d.home / ".claude" / "settings.json"),
        "plainly outside everything": str(d.elsewhere / "thing.py"),
    }


def _seed(sid: str, payload: dict) -> None:
    cache = _imp("writ.session.cache")
    cache._write_cache(sid, payload)


def _pre_approval(root: str, zone: str) -> dict:
    return {"mode": "work", "current_phase": None, "gates_approved": [],
            "gates_approved_plan": {}, "project_root": root, "scratch_zone": zone,
            "is_subagent": False}


def _post_approval(root: str, zone: str) -> dict:
    """Both gates approved, bound to "no plan.md at all": plan_md_hash is None on both sides
    of approved_gates_for_plan's comparison, which its docstring calls genuinely equal, so
    the state needs no plan file. Same shape as tests/test_project_boundary.py's fixture."""
    return {"mode": "work", "current_phase": "implementation",
            "gates_approved": ["phase-a", "test-skeletons"],
            "gates_approved_plan": {"phase-a": None, "test-skeletons": None},
            "project_root": root, "scratch_zone": zone, "is_subagent": False}


def _write_door(sid: str, target: str) -> bool:
    """The Write/Edit door's verdict: True allow, False deny. This is the function BOTH
    transports call (the /session/<id>/can-write route and the CLI fallback), so it is the
    door and not a stand-in for it."""
    gates = _imp("writ.session.gates")
    result = gates._can_write_check(sid, {"tool_input": {"file_path": target}}, SKILL_ROOT)
    return bool(result["can_write"])


def _bash_door(env: dict, sid: str, cwd: str, target: str,
               script: str = HOOK_SH) -> tuple[bool, dict | None]:
    """The Bash door's verdict for a real redirect naming `target` (the vector the live
    bypass used). Silence is an allow; deny and ask are both refusals."""
    envelope = json.dumps({
        "session_id": sid, "tool_name": "Bash", "hook_event_name": "PreToolUse",
        "tool_input": {"command": f"echo x > {target}"},
    })
    p = subprocess.run(["bash", script], input=envelope, capture_output=True,
                       text=True, env=env, cwd=cwd, timeout=60)
    assert p.returncode == 0, f"hook must exit 0; got {p.returncode}\n{p.stderr[-2000:]}"
    out = p.stdout.strip()
    if not out:
        return True, None
    decision = json.loads(out)["hookSpecificOutput"]
    return decision.get("permissionDecision") not in ("deny", "ask"), decision


def _flush_events(env: dict, sid: str, cwd: str) -> None:
    """Drain the session's buffered rows through the DOCUMENTED flush entry point.

    `log_gate_decision` splits on the decision value (bin/lib/common.sh:1611,
    `if [ "$_gd_dec" != "allow" ]`): anything that is not an allow is emitted
    synchronously and lands in the log root immediately, while an ALLOW is appended to
    `$WRIT_CACHE_DIR/writ-events-<sid>.buf` and released only by
    `writ_event_buffer_flush`, which runs at Stop and SessionEnd. One `PreToolUse`
    invocation never calls it, so an allow row genuinely exists but has not landed yet,
    and asserting on the log without draining first reads exactly like "the gate never
    saw it".

    THE DRAIN GOES THROUGH THE SHELL FUNCTION, not the buffer file. The capability being
    pinned says the write "leaves a gate_decision audit row", the log root is the
    artifact a human audits, and a test that read `writ-events-<sid>.buf` directly would
    stay green if the flush silently broke, which is the failure most worth catching
    here.
    """
    script = f'. "{SKILL_ROOT}/bin/lib/common.sh"; writ_event_buffer_flush "{sid}"'
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=env, cwd=cwd, timeout=60)
    assert p.returncode == 0, (
        f"the flush entry point must exit 0; got {p.returncode}\n{p.stderr[-2000:]}"
    )


def _audit_rows(logs: Path) -> list[dict]:
    rows: list[dict] = []
    for f in Path(logs).rglob("*.jsonl"):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("event") == "gate_decision":
                rows.append(row)
    return rows


def _mutant_hook(tmp_path: Path, name: str, mutate) -> str:
    """A parallel WRIT_DIR whose only difference is one mutated block in the gate script.

    The script derives WRIT_DIR from its own location ($HOOK_DIR/../..), so the copy needs
    that shape; `bin` and `writ` are symlinked and common.sh resolves its own skill dir
    through the link, so the mutant runs the real libraries and the real can-write fallback.
    """
    root = tmp_path / f"mutant-{name}"
    (root / "hooks" / "scripts").mkdir(parents=True, exist_ok=True)
    for link in ("bin", "writ"):
        dst = root / link
        if not dst.exists():
            dst.symlink_to(Path(SKILL_ROOT) / link)
    original = Path(HOOK_SH).read_text()
    mutated = mutate(original)
    assert mutated != original, f"mutation {name!r} matched nothing; this pin has decayed"
    script = root / "hooks" / "scripts" / "writ-bash-write-gate.sh"
    script.write_text(mutated)
    script.chmod(0o755)
    return str(script)


def test_the_scratch_zone_is_allowed_pre_approval_on_both_doors(doors) -> None:
    """THE DIRECTIONAL PIN. `test_the_two_doors_agree[pre_approval-os scratch zone]` above is
    SYMMETRIC -- it only asserts the two doors match each other, and before this cycle's arm
    both doors denied the zone target together, so that row is already green and cannot
    detect the fix's absence. This test asserts the DIRECTION as well as the agreement: in
    the pre-approval state, the `os scratch zone` target must ALLOW on both doors, and
    `plainly outside everything` must still REFUSE on both, driving the real hook subprocess
    for the Bash half exactly as `_bash_door` does everywhere else in this file.
    """
    sid = f"parity-{uuid.uuid4().hex[:8]}"
    _seed(sid, _pre_approval(str(doors.root), str(doors.zone)))
    targets = _targets(doors)

    zone_target = targets["os scratch zone"]
    zone_bash_allow, zone_decision = _bash_door(doors.env, sid, str(doors.root), zone_target)
    zone_write_allow = _write_door(sid, zone_target)
    assert zone_bash_allow is True, (
        f"Bash door refused the pre-approval scratch-zone target {zone_target!r}: "
        f"{zone_decision!r}"
    )
    assert zone_write_allow is True, (
        f"Write door refused the pre-approval scratch-zone target {zone_target!r}"
    )

    outside_target = targets["plainly outside everything"]
    outside_bash_allow, outside_decision = _bash_door(
        doors.env, sid, str(doors.root), outside_target
    )
    outside_write_allow = _write_door(sid, outside_target)
    assert outside_bash_allow is False, (
        f"Bash door allowed the pre-approval out-of-zone target {outside_target!r}: "
        f"{outside_decision!r}"
    )
    assert outside_write_allow is False, (
        f"Write door allowed the pre-approval out-of-zone target {outside_target!r}"
    )


@pytest.mark.parametrize("state", ["pre_approval", "post_approval"])
@pytest.mark.parametrize("label", _TARGET_LABELS)
def test_the_two_doors_agree(doors, state, label) -> None:
    sid = f"parity-{uuid.uuid4().hex[:8]}"
    _seed(sid, _pre_approval(str(doors.root), str(doors.zone)) if state == "pre_approval"
          else _post_approval(str(doors.root), str(doors.zone)))
    target = _targets(doors)[label]
    # Bash first: the Write door's deny path mutates the cache (denial_counts), and running
    # the doors in this order keeps the Bash verdict independent of that write.
    bash_allow, decision = _bash_door(doors.env, sid, str(doors.root), target)
    write_allow = _write_door(sid, target)
    assert bash_allow == write_allow, (
        f"{state} / {label}: the Bash door {'allowed' if bash_allow else 'refused'} while "
        f"the Write door {'allowed' if write_allow else 'refused'} {target!r}. "
        f"Bash returned {decision!r}"
    )


def test_the_harness_produces_both_verdicts(doors) -> None:
    """Anti-vacuity. Agreement is trivially satisfiable if every row denies, so this shows
    the same harness yielding one genuine allow and one genuine deny, and pins the label
    list the parametrization walks to the table's own keys."""
    assert set(_targets(doors)) == set(_TARGET_LABELS)
    sid = f"parity-{uuid.uuid4().hex[:8]}"
    _seed(sid, _post_approval(str(doors.root), str(doors.zone)))
    inside = str(doors.root / "src" / "app.py")
    outside = str(doors.elsewhere / "thing.py")
    assert _bash_door(doors.env, sid, str(doors.root), inside)[0] is True
    assert _write_door(sid, inside) is True
    assert _bash_door(doors.env, sid, str(doors.root), outside)[0] is False
    assert _write_door(sid, outside) is False


def test_an_out_of_repo_target_reaches_the_gate_and_records_it(doors) -> None:
    """The POSITIVE signal. Before the fix an out-of-repo Bash write was silent in stdout
    AND in the audit stream, and silence is exactly what an allow looks like, so only a row
    naming the target proves the gate ran at all.

    The row is read from the LOG ROOT and only after `_flush_events`, because this target
    earns an ALLOW and an allow is the one decision `log_gate_decision` buffers instead of
    writing through. See that helper for why the drain runs the real flush function rather
    than reading the buffer file.
    """
    sid = f"parity-{uuid.uuid4().hex[:8]}"
    _seed(sid, _post_approval(str(doors.root), str(doors.zone)))
    target = str(doors.memory / "MEMORY.md")
    allow, _decision = _bash_door(doors.env, sid, str(doors.root), target)
    assert allow is True
    _flush_events(doors.env, sid, str(doors.root))
    rows = [r for r in _audit_rows(doors.logs) if r.get("target") == target]
    assert rows, f"no gate_decision row named {target}; the gate never saw it"
    assert rows[-1]["decision"] == "allow"
    assert rows[-1]["gate"] == "bash-write"


@pytest.mark.parametrize("name, mutate", [
    ("no-outside-row", lambda s: s.replace(_PRODUCER_ROW, "")),
    ("local-only-consumer",
     lambda s: s.replace(_CONSUMER_TEST, '    [ "$kind" = "local" ] || continue\n')),
])
def test_the_parity_property_goes_red_without_the_fix(doors, tmp_path, name, mutate) -> None:
    """Conditionality, one half of the fix at a time: removing the row and narrowing the kind
    test each restore the pre-fix silence. The row used is another project's memory dir,
    which the Write door refuses through the BOUNDARY rather than through the work gate, so
    the disagreement is attributable to this fix and not to mode state."""
    sid = f"parity-{uuid.uuid4().hex[:8]}"
    _seed(sid, _post_approval(str(doors.root), str(doors.zone)))
    target = str(doors.other_memory / "MEMORY.md")
    script = _mutant_hook(tmp_path, name, mutate)
    bash_allow, _decision = _bash_door(doors.env, sid, str(doors.root), target, script=script)
    assert _write_door(sid, target) is False
    assert bash_allow is True, (
        "the mutant still refused, so this pin no longer distinguishes the fix from its "
        "absence"
    )
