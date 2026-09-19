"""Shared session-state pytest fixtures and can-write caller (Wave-5 Cycle 5.3c).

Consolidates the `session_id` and `project_root` fixtures plus the can-write
envelope/stdin/parse dance formerly duplicated in `test_mode_infrastructure.py`
and `test_phase3_centralization.py`. These are imported EXPLICITLY into each
consuming test module (`from tests.fixtures.session_state import session_id,
project_root`), never registered in a root conftest, so they cannot shadow the
many other files that define their own divergent `session_id`/`project_root`
fixtures.

`call_can_write` takes `writ_session` and `skill_dir` as arguments rather than
recomputing them, because each consumer loads `writ-session.py` via its own
`importlib` spec and computes `SKILL_DIR` relative to its own `__file__`.
"""

from __future__ import annotations

import io
import json

import pytest


def module_session_id(module_name: str) -> str:
    """The session id for ONE consuming module, derived from that module's own name.

    ONE DEFINITION, TWO READERS. The `session_id` fixture below calls it to build the
    default id, and a consuming module that also declares GATE_TOKEN_SESSION_PREFIX calls
    it with its own `__name__` to declare exactly the namespace it mints into. Spelling
    the value twice is how the sweeper would come to sweep a namespace nothing writes.

    THE TRAILING SEPARATOR IS DELIBERATE. The value doubles as a sweep prefix, and
    `bin/lib/analyzers-regex.sh` (NAMESPACE_PREFIX, line 379) exempts a lowercase
    hyphen-or-underscore value that ENDS in a bare separator from the credential-literal
    finding. Ending it here means the value is writable as a plain literal anywhere,
    including in another test's expectation map, instead of being refused at write time by
    the pre-write scanner and pushing the next author toward one of the four evasions
    docs/adr/ADR-gate-token-leak-guard.md lines 264 to 266 already names and rejects.
    """
    stem = module_name.rpartition(".")[2]
    return f"test-session-{stem.replace('_', '-')}-"


@pytest.fixture()
def session_id(tmp_path, monkeypatch, request):
    """Provide a session ID and redirect cache to tmp_path.

    The DEFAULT is derived from the requesting module, so two modules importing this
    fixture can never mint the same `/tmp/writ-gate-token-<sid>` file again. Before this,
    tests/test_mode_infrastructure.py and tests/test_phase3_centralization.py both got the
    literal "test-session" and both wrote one shared path; a per-module sweeper cannot own
    a namespace two modules write into. `request.param` still wins, so a consuming file
    that overrides the id by indirect parametrization is unaffected.
    """
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    return getattr(request, "param", module_session_id(request.module.__name__))


@pytest.fixture()
def project_root(tmp_path):
    """Create a minimal project root with .git marker and gates dir."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".claude" / "gates").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def sandbox_cwd(tmp_path, monkeypatch):
    """Move the process cwd to a throwaway project for every test in the importing module.

    `mode set` / `mode init` stamp cache["project_root"] from the process cwd, and clearing
    gate state DELETES <project_root>/.claude/gates/*.approved. So a test that calls
    cmd_mode in-process, or spawns `writ-session.py mode set` or the UserPromptSubmit hook
    without pinning cwd, inherits pytest's own cwd (the real repo root) and destroys this
    repo's approval artifacts as a side effect of running the suite. A sentinel probe
    found 26 modules doing exactly that. Subprocesses inherit the chdir, so this one
    fixture covers the in-process and the spawned shapes alike; a subprocess that passes an
    explicit `cwd=` must drop it to be covered.

    autouse, but imported per module rather than registered in a root conftest, for the
    reason this file's header gives: an autouse chdir in a conftest would silently reroute
    EVERY test in the suite, including the many that resolve paths against the repo root.

    The sandbox carries a .git marker so a project-root walk stops inside it instead of
    climbing back out to a real project, and a .claude/gates directory so the cleanup has a
    real (empty) directory to act on rather than a missing-path no-op.
    """
    sandbox = tmp_path / "cwd-sandbox"
    (sandbox / ".claude" / "gates").mkdir(parents=True, exist_ok=True)
    (sandbox / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(sandbox)
    return sandbox


def write_bound_gate_token(
    session_id: str, token: str | None = None, gate: str | None = None, rule_id: str = "",
) -> str:
    """Mint the gate token a genuine approval would mint for `session_id`; return it.

    The token file binds what the approval authorizes: line 1 the secret, line 2 the gate,
    line 3 the plan fingerprint. Both gate paths (the CLI cmd_advance_phase and the HTTP
    advance route) refuse a token whose binding does not match the gate now pending, and
    refuse an unbound one-line file outright. So `open(path, "w").write(token)` no longer
    simulates an approval -- it simulates the pre-binding format both paths now reject.

    The binding is DERIVED from the session cache, exactly as the production mint derives
    it: cmd_current_phase reports next_gate and plan_hash out of that cache, the approval
    hook writes those two lines, and the claim recomputes both from the same cache. A test
    therefore keeps seeding the cache and gets the binding right by construction, instead
    of hardcoding a gate name that a later seed change would silently invalidate. Call it
    AFTER the cache is seeded, and once per advance (claiming consumes the file).

    `gate` OVERRIDES the derived gate, for the approvals whose gate is not a member of the
    mode's phase sequence and therefore cannot be derived from the cache at all:
    gate_token.REPLAN_GATE, which authorizes re-opening planning and which
    `_next_pending_gate` never returns by design. Default None keeps the derivation, so
    every existing caller mints exactly the bytes it minted before. The alternative was a
    second hand-rolled copy of the token format inside the replan tests, which is the
    drift this helper exists to prevent.

    `rule_id` binds a rule-promotion approval to the one rule `writ review <rule_id>
    --session-id <sid>` surfaced (line 5, added for the approval-integrity cycle).
    Default "" keeps every existing caller minting exactly the bytes it minted before
    that line existed; a caller exercising the rule-promotion binding passes the rule id
    explicitly, the same way `gate=gate_token.REPLAN_GATE` is passed explicitly above.

    One helper rather than one per test module on purpose: this is the third comparison of
    the same binding in the codebase, and the last time two call sites answered one
    security question separately they drifted (see gate_token.py's module docstring).
    """
    from writ.session.cache import _read_cache
    from writ.session.gate_token import mint_gate_token
    from writ.session.locators import plan_md_hash
    from writ.session.mode_engine import _next_pending_gate

    cache = _read_cache(session_id)
    # session_id is threaded into BOTH derivations because the production mint does the
    # same: cmd_current_phase reports next_gate and plan_hash from a cache read that
    # carries the session, so a SESSION-SCOPED plan (.claude/plans/<sid>/plan.md) is what
    # the claim later fingerprints. Deriving without it produced a token bound to the
    # shared-root plan (usually absent, so an empty line 3) while the claim compared the
    # scoped plan's real digest, and the advance was refused as plan drift.
    derived_gate = gate if gate is not None else (_next_pending_gate(cache, session_id) or "")
    # rule_id is passed to mint_gate_token ONLY when a caller actually asks for a rule
    # binding. This helper is shared by a dozen other test modules
    # (tests/test_advance_phase_token_claim.py, tests/test_mode_engine.py, ... none of
    # them in this cycle's scope) that call it with no opinion about rule_id at all;
    # an unconditional rule_id=rule_id keyword would pass rule_id="" to every one of
    # them, and mint_gate_token has no such parameter until this cycle's production
    # change lands, which would TypeError every caller of this helper repo-wide rather
    # than just the ones this cycle's tests actually exercise.
    extra = {"rule_id": rule_id} if rule_id else {}
    return mint_gate_token(
        session_id,
        gate=derived_gate,
        plan_hash=plan_md_hash(cache.get("project_root"), session_id) or "",
        token=token,
        **extra,
    )


# ---------------------------------------------------------------------------
# Approval-evidence transcript fixtures (bin/lib/approval_evidence.py, the exact-tier
# evidence gate). One helper family instead of six hand-rolled JSONL writers across the
# six test modules the evidence requirement touches (plan.md ## Files).
# ---------------------------------------------------------------------------


def assistant_text_row(text: str) -> dict:
    """One assistant transcript row carrying a single text content block, the shape
    hooks/scripts/writ-comms-output-gate.sh already reads (`message.content` a list of
    `{"type": "text", "text": ...}` blocks joined)."""
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def assistant_tool_call_row() -> dict:
    """An assistant row whose content carries NO text block at all, a tool-call-only
    turn. Used to prove a trailing one of these does not erase evidence found in the
    text turn before it (plan.md capability: "a trailing tool-call-only assistant row
    does not erase evidence")."""
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_fixture_1", "name": "Bash", "input": {"command": "echo hi"}},
        ]},
    }


def user_row(text: str = "ok") -> dict:
    """A user transcript row. At most ONE of these may follow the evidence turn and
    still count (plan.md's tolerance for either transcript-append ordering); two or
    more mean the request belongs to an earlier exchange."""
    return {"type": "user", "message": {"content": text}}


def write_transcript_jsonl(tmp_path, rows: list) -> str:
    """Write a JSONL transcript fixture from already-shaped rows (dicts, JSON-
    serialized here) or raw strings (written verbatim, the way to construct a
    garbled/unparseable line). Returns the path as a string. A `tmp_path`-unique
    filename lets a single test build more than one transcript without collision."""
    import uuid as _uuid
    from pathlib import Path as _Path

    path = _Path(tmp_path) / f"transcript-{_uuid.uuid4().hex[:8]}.jsonl"
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return str(path)


def write_evidence_transcript(tmp_path, marker: str = "Say approved to proceed.") -> str:
    """The common case every OTHER test module needs (test_approval_tiers.py,
    test_replan_reopen_planning.py, test_phase3b_approval_rewrap.py,
    test_no_tool_prereqs.py, test_pol5b3b_smaller_hook_redundancy.py): one assistant
    turn asking for approval with a marker phrase, so an exact `approved` on the next
    turn has evidence in front of it and the existing mint/advance assertions in those
    files keep testing what they were written for instead of the new ask-directive
    path. Returns the transcript path, ready to drop into a hook envelope's
    `transcript_path` field."""
    return write_transcript_jsonl(tmp_path, [assistant_text_row(marker)])


def call_can_write(writ_session, session_id, file_path, monkeypatch, capsys, skill_dir=None):
    """Call cmd_can_write with a synthetic tool envelope and return the JSON result."""
    capsys.readouterr()  # clear any prior output
    envelope = json.dumps({"tool_input": {"file_path": file_path}})
    monkeypatch.setattr("sys.stdin", io.StringIO(envelope))
    writ_session.cmd_can_write(session_id, skill_dir)
    out = capsys.readouterr().out.strip()
    return json.loads(out)
