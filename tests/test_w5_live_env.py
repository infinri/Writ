"""RED guard for Wave-5 Cycle 5.2 -- live-environment coupling + hardcoded-path
portability defects (test suite).

Cycle 5.2 removes two defect classes from the test suite:

- Group A: three test modules hardcode `SERVER = "http://localhost:8765"`,
  bypassing the suite's test-port isolation (`tests/conftest.py` pins
  `WRIT_PORT=8799`) and silently targeting the operator's live interactive
  daemon instead. The fix routes each through the canonical
  `tests._daemon._port()` helper (`from tests._daemon import _port` +
  `SERVER = f"http://localhost:{_port()}"`), mirroring the existing pattern
  in `tests/test_pol5a_statusline.py`.
- Group B: `tests/test_validate_rules.py` writes/passes hardcoded `/tmp/*.php`
  paths instead of the pytest `tmp_path` fixture, leaking uncleaned files
  across runs. `tests/test_pol5a_statusline.py` and
  `tests/test_pol5c_removal.py` read the OPERATOR's real
  `~/.claude/settings.json` unconditionally (crashing when absent) and derive
  their repo-root `SKILL_DIR` from `Path.home()` (machine-specific). The fix
  is a `skipif(not GLOBAL_SETTINGS.exists())` guard (NOT a synthetic
  monkeypatched file, which would make the live-install assertion vacuous)
  plus a `Path(__file__)`-relative `SKILL_DIR`.
- Group C: `tests/plugin/conftest.py`'s `REPO_ROOT` and
  `tests/test_methodology_ingest.py`'s `bible_dir` bake this machine's
  absolute home path in, defeating portability (and, worse, silently
  skipping the dual-location-dedup invariant off this machine). The fix
  resolves both via `Path(__file__).resolve()` hops to the checkout root.

SUPERSEDED EXCEPTION (corrected 2026-09-09): this file used to carry
`test_advance_phase_token_gate_keeps_documented_8765`, a survivor /
over-correction guard asserting that `tests/test_advance_phase_token_gate.py`
must KEEP hardcoding `localhost:8765`, on the premise that it was a
documented security-integration test of the DEPLOYED daemon. That premise is
reversed by a measured reason: the old form appended one
`agent_self_approval_blocked` row per run into the operator's real
`var/logs/github.com/infinri/Writ/audit.jsonl` (1518 rows before, 1519
after), because `bin/lib/writ_daemon_client.py`'s `post_json` prefers an
EXISTING unix socket over `base_url` and that module never passed an
explicit `socket_path` -- so the port literal was never even the mechanism
that chose the destination. The survivor guard is retired (an assertion
that a future sweep must NOT fix the file this cycle fixes cannot be left
standing alongside the fix) and replaced below by a MECHANISM guard over a
DERIVED population: no module under `tests/` -- named or not yet
written -- may bind a module-level constant to the production daemon port,
and `test_advance_phase_token_gate.py` must import the isolated-daemon
helper rather than target :8765 directly.

This guard is FULLY HERMETIC: it is a pure source-text scan. It does NOT
import, execute, or collect fixtures from any target file, does NOT touch a
daemon or Neo4j, and does NOT read the operator's real
`~/.claude/settings.json` (it only source-scans the TEST files that
reference that path).

RED today (2026-07-16, pre-implementation):
- test_phase_advance_unified.py still hardcodes `localhost:8765` and contains
  no `_port` reference -> both asserts in `_assert_uses_port_helper` fail.
  (The two sibling per-file guards named here originally, over
  test_phase6_promote_route_token.py and test_advance_populates_gates_approved.py,
  were removed 2026-09-09: the first module was deleted outright and the second
  lost its HTTP client, so `_read`'s existence assert and the `_port` assert had
  no subject left. Their real property, "no module binds a constant to the
  production daemon port", is carried for every module by the Group D mechanism
  guard below over a derived population.)
- test_validate_rules.py still contains `/tmp/` literals and no `tmp_path`
  reference -> both asserts fail.
- test_pol5a_statusline.py and test_pol5c_removal.py contain no
  `GLOBAL_SETTINGS.exists()` skip guard, and their `SKILL_DIR` assignment
  lines use `Path.home()` (not `__file__`) -> all four asserts fail.
- tests/plugin/conftest.py's `REPO_ROOT` line uses `Path.home()` (not
  `__file__`) -> both asserts fail.
- test_methodology_ingest.py still contains a hardcoded
  `/home/<username>` path -> assert fails.

GREEN only once each corresponding fix in plan.md Cycle 5.2 lands.

GREEN today (2026-09-09) for the new Group D mechanism guard below, unlike
the still-RED Group A/B/C guards above: `test_advance_phase_token_gate.py`'s
own source-level migration off the hardcoded `localhost:8765` literal and
onto `tests._daemon.start_isolated_daemon` lands together with this file's
guard in the same testing-phase pass, so
`test_no_module_binds_a_constant_to_the_production_daemon_port` and
`test_advance_phase_token_gate_imports_the_isolated_daemon_helper` both pass
against today's tree. Confirmed as a genuine (non-vacuous) regression pin by
mutation: reintroducing a `SERVER = "http://localhost:8765"` constant, or
removing the `start_isolated_daemon` import, reddens the corresponding
guard. What remains RED is runtime, not source text:
`test_advance_phase_token_gate.py` still fails to COLLECT, because
`tests/_daemon.py` does not yet define `start_isolated_daemon` /
`stop_isolated_daemon` (assigned to the implementation phase) -- see that
module's own docstring.

WIDENED 2026-09-22 (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3, cause 2): the
Group D guard above closed the module-level-constant SHAPE of this defect but
missed the MECHANISM. `tests/test_daemon_enforcement.py:428` hands the exact
same production port to the exact same client call, `client.get_json(...,
base_url='http://localhost:8765')`, as an inline keyword argument rather than a
named constant, and `_PORT_CONSTANT_RE`'s `^[A-Z_]+ = "..."` anchor never
matches a keyword argument sitting mid-call. That module has carried the
defect for roughly a month while this guard passed the whole time, which is
the failure mode this rewrite closes: a guard keyed to how the literal was
SPELLED rather than to what it DOES (get handed to `base_url=`, which
`writ_daemon_client.py`'s `_request` dials with `http.client.HTTPConnection`
whenever no live unix socket answers first). `test_no_module_binds_a_constant_to_the_production_daemon_port`
keeps its original name (a sibling module's comment at
`test_daemon_skip_ownership.py:165` names it by that string, and this cycle
touches only this file) but its body and docstring below now scan for BOTH
the constant form and the keyword-argument form, because both are the same
defect wearing a different spelling.

RED today (2026-09-22, pre-widening) against the tree above:
`test_no_module_binds_a_constant_to_the_production_daemon_port` fails,
naming `test_daemon_enforcement.py` and its offending line, once the second
regex (`_PORT_KWARG_RE`) is added to the scan. Every OTHER `8765` occurrence
under `tests/` was read and classified by hand before this regex was chosen
(see the docstring on `_PORT_KWARG_RE` below for the full partition): curl
command strings and refusal regexes used as egress/review-gate test DATA, an
integer port constant compared for inequality, a dict/list value fed to a
pure discriminator function, an UPPERCASE constant name compared with `==`,
and one deferred out-of-scope live target
(`tests/plugin/test_fresh_install_smoke.py:112`) that is a bare positional
string with no `base_url=`/`url=` binding at all. None of those trip
`_PORT_KWARG_RE`; only a literal bound to `base_url=` or `url=` does.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def _read(filename: str) -> str:
    """Read `filename`'s source text relative to this tests/ dir.

    Never imports or executes the file -- pure text read, so this guard
    cannot itself touch a daemon, Neo4j, or the operator's settings.json.
    """
    path = TESTS_DIR / filename
    assert path.exists(), f"expected {path} to exist"
    return path.read_text()


# ---------------------------------------------------------------------------
# Group A -- hardcoded interactive-daemon port 8765 routed through _port()
# ---------------------------------------------------------------------------


# `_assert_uses_port_helper` and its one caller,
# `test_phase_advance_unified_uses_port_helper`, were removed here (plan.md
# 2412ba38-51e1-4b73-895b-7b240a3c21d3, decision 6): the cycle-3 sweep deletes
# `test_phase_advance_unified.py`'s only `_port`-referencing test along with
# `SERVER` and the `from tests._daemon import _port` import, so this guard's
# second assertion (`"_port" in src`) would go FALSE while the file still
# EXISTS -- RED, not skipped, because `_read` only asserts `path.exists()`.
# Nothing is lost: the module now names no daemon address at all, and the
# derived Group D guard below (`test_no_module_binds_a_constant_to_the_
# production_daemon_port`) carries the "must not hardcode :8765" half of the
# property for every module under tests/, named or not yet written -- the
# Group A consolidation this file's own docstring queued.


# ---------------------------------------------------------------------------
# Group D -- the production daemon port is a MECHANISM, not a literal
# (retired 2026-09-09 survivor: test_advance_phase_token_gate_keeps_documented_8765
# asserted the OPPOSITE of this cycle's fix and could not be left standing
# alongside it)
# ---------------------------------------------------------------------------

# `^[A-Z_]+ = "http://localhost:8765"` -- a module-level constant bound to the
# production daemon's default port, the exact shape SERVER used to take in
# test_advance_phase_token_gate.py before this cycle.
_PORT_CONSTANT_RE = re.compile(r'^[A-Z_][A-Z0-9_]*\s*=\s*"http://localhost:8765"', re.M)

# Widened 2026-09-22: the constant-assignment shape above is one SPELLING of the
# defect, not the defect itself. The mechanism is a literal naming the production
# daemon port handed to the parameter that actually dials it. Every call this
# guard cares about (`writ_daemon_client.py`'s `post_json`/`get_json`/`_request`,
# and any future caller shaped like them) resolves its destination through a
# keyword literally named `base_url` (see bin/lib/writ_daemon_client.py:76,116,131);
# `url=` is included too since that is the conventional name for the same role on
# stdlib-adjacent HTTP callers (`requests.get(url=...)`) and nothing under tests/
# uses that spelling yet, but a guard keyed to today's one caller is exactly the
# kind of narrow-by-name gap this rewrite exists to close.
#
# This is deliberately NOT "any string containing 8765 under tests/": that would
# also flag curl command strings that are egress/review-gate test DATA (e.g.
# test_bash_egress_gate.py, test_daemon_authorization.py, test_review_blocking.py),
# refusal regexes that source-scan for those same curl strings
# (test_daemon_report_truth.py:255-256), an inequality assertion on the bare port
# number (test_daemon_test_port.py:17), a dict/list value fed to a pure
# discriminator function with no network call in sight
# (test_daemon_transport.py:287), and an UPPERCASE constant compared with `==`
# rather than bound with `=` (test_pol6g3_remaining_clusters_extraction.py:182-183).
# Anchoring on the keyword name plus a single `=` (not `==`) and requiring the
# name be lowercase (`base_url`/`url`, never `BASE_URL`/`URL`) is what keeps all of
# those out without an allowlist naming any of those files: an allowlist keyed on
# filename over-grants the moment one of them gains a real defect of its own.
_PORT_KWARG_RE = re.compile(
    r'\b(?:base_url|url)\s*=\s*f?"http://(?:localhost|127\.0\.0\.1):8765(?:/[^"]*)?"'
)


def _all_test_modules() -> list[Path]:
    """Every .py file under tests/ -- the DERIVED population this guard scans.

    A hardcoded file list is exactly the blind spot this replaces: the
    retired survivor guard named ONE file by hand and asserted the opposite
    of this cycle's fix, so a population that has to be re-typed per new
    test module is a population that silently stops covering the next one.
    """
    return sorted(TESTS_DIR.rglob("*.py"))


def test_population_is_non_empty() -> None:
    """The population the mechanism guard below scans must not be empty.

    Reddened by a glob that matches nothing (a typo'd pattern, or pointing
    at the wrong directory): a population of zero would make the guard below
    pass vacuously on every file it never actually looked at.
    """
    modules = _all_test_modules()
    assert len(modules) > 0, f"expected at least one .py file under {TESTS_DIR}"


def test_no_module_binds_a_constant_to_the_production_daemon_port() -> None:
    """No module under tests/ may hand the production daemon port to something
    that will DIAL it, whether as `SOMENAME = "http://localhost:8765"` (a
    module-level constant) or as `base_url='http://localhost:8765'` /
    `url='http://localhost:8765'` (an inline keyword argument at the call
    site).

    Reddened by adding either shape to any test module, including a new one
    this guard has never seen before: THE PORT WAS NEVER THE MECHANISM that
    routed a request onto the operator's live daemon --
    `bin/lib/writ_daemon_client.py`'s `post_json`/`get_json` prefer an
    EXISTING unix socket over `base_url`, so naming `:8765` in either shape is
    not merely unclean, it is the literal mechanism that let
    `test_advance_phase_token_gate.py` write a security-shaped
    `agent_self_approval_blocked` row into the operator's real
    `audit.jsonl` on every run, and that let
    `test_daemon_enforcement.py::test_the_client_falls_back_to_tcp_when_the_socket_is_absent`
    assert `status == 200` against a port nothing in CI or an idle developer
    machine answers on. The keyword-argument shape was invisible to the
    original constant-only regex for a month: the guard passed the whole
    time the second live call sat right next to a sibling that was already
    fixed to use `_free_port()`.

    This supersedes the retired
    `test_advance_phase_token_gate_keeps_documented_8765`, which asserted the
    opposite of this cycle's fix.
    """
    offenders = []
    for path in _all_test_modules():
        src = path.read_text()
        rel = str(path.relative_to(TESTS_DIR))
        for pattern in (_PORT_CONSTANT_RE, _PORT_KWARG_RE):
            match = pattern.search(src)
            if match is not None:
                line_no = src.count("\n", 0, match.start()) + 1
                offenders.append(f"{rel}:{line_no}: {match.group(0).strip()}")
    assert offenders == [], (
        "these test modules hand the production daemon port "
        f"(http://localhost:8765) to something that will dial it: {offenders}. "
        "Route through tests._daemon._port() (Group A modules), or, for "
        "test_advance_phase_token_gate.py, an OS-assigned free port via "
        "tests._daemon.start_isolated_daemon, instead of a literal -- for an "
        "inline base_url=/url= keyword argument, the sibling fix is "
        "tests._daemon._free_port() bound at the call site, the same pattern "
        "test_the_client_prefers_a_live_socket already uses two tests above."
    )


def test_advance_phase_token_gate_imports_the_isolated_daemon_helper() -> None:
    """tests/test_advance_phase_token_gate.py must start and own its own
    daemon (tests._daemon.start_isolated_daemon) rather than targeting the
    deployed daemon directly.

    Reddened by removing that import (e.g. reverting to
    `SERVER = "http://localhost:8765"` plus a bare urllib POST) -- exactly
    the regression the retired survivor guard used to protect against in the
    opposite direction.
    """
    src = _read("test_advance_phase_token_gate.py")
    assert "from tests._daemon import" in src and "start_isolated_daemon" in src, (
        "test_advance_phase_token_gate.py must import start_isolated_daemon "
        "from tests._daemon so it starts and owns its own daemon instead of "
        "targeting the operator's deployed :8765 singleton"
    )
    assert "localhost:8765" not in src, (
        "test_advance_phase_token_gate.py must no longer hardcode "
        "'localhost:8765'; the port must come from an OS-assigned free port "
        "(tests.fixtures.net.free_port(), via "
        "tests._daemon.start_isolated_daemon), not a literal"
    )


# ---------------------------------------------------------------------------
# Group B -- real-environment reads (tmp_path isolation; skipif live-install)
# ---------------------------------------------------------------------------


def test_validate_rules_uses_tmp_path() -> None:
    """tests/test_validate_rules.py must adopt tmp_path, dropping /tmp/ literals."""
    src = _read("test_validate_rules.py")
    assert "/tmp/" not in src, (
        "test_validate_rules.py must no longer hardcode /tmp/*.php path "
        "literals; throwaway .php paths must come from the pytest tmp_path "
        "fixture so each test gets an isolated, auto-cleaned directory "
        "instead of leaking files into the shared /tmp"
    )
    assert "tmp_path" in src, (
        "test_validate_rules.py must adopt the pytest `tmp_path` fixture for "
        "its throwaway .php file paths/writes"
    )


_SKIPIF_GLOBAL_SETTINGS_RE = re.compile(r"GLOBAL_SETTINGS\.exists\(\)")


def _assert_skipif_guard_present(filename: str, src: str) -> None:
    assert _SKIPIF_GLOBAL_SETTINGS_RE.search(src) is not None, (
        f"{filename} must guard its GLOBAL_SETTINGS-reading tests with "
        "pytest.mark.skipif(not GLOBAL_SETTINGS.exists(), ...) so they skip "
        "(rather than crash via _load_settings's open()) when the "
        "operator's real settings.json is absent"
    )


def _assert_skill_dir_is_file_relative(filename: str, src: str) -> None:
    """Isolate the SKILL_DIR assignment LINE and assert on it only.

    GLOBAL_SETTINGS legitimately keeps Path.home() elsewhere in these files
    (it intentionally points at the operator's real settings file), so the
    "no Path.home()" requirement must be scoped to the SKILL_DIR line, not
    asserted against the whole file.
    """
    match = re.search(r"^\s*SKILL_DIR\s*=.*$", src, re.M)
    assert match is not None, (
        f"{filename} must define a `SKILL_DIR = ...` assignment line"
    )
    line = match.group(0)
    assert "__file__" in line, (
        f"{filename}'s SKILL_DIR assignment line must be derived from "
        f"Path(__file__) for portability across machines/checkouts; found: {line!r}"
    )
    assert "Path.home()" not in line, (
        f"{filename}'s SKILL_DIR assignment line must not use Path.home() "
        f"(a machine-specific repo-root hardcode); found: {line!r}"
    )


def test_pol5a_statusline_guards_and_relativizes() -> None:
    """test_pol5a_statusline.py: skipif guard on GLOBAL_SETTINGS.exists()
    plus a __file__-relative SKILL_DIR (GLOBAL_SETTINGS itself keeps
    Path.home() -- it is intentionally the operator's real file).
    """
    src = _read("test_pol5a_statusline.py")
    _assert_skipif_guard_present("test_pol5a_statusline.py", src)
    _assert_skill_dir_is_file_relative("test_pol5a_statusline.py", src)


def test_pol5c_removal_guards_and_relativizes() -> None:
    """test_pol5c_removal.py: skipif guard on GLOBAL_SETTINGS.exists()
    (repo-file PLUGIN_HOOKS assertions stay unconditional) plus a
    __file__-relative SKILL_DIR.
    """
    src = _read("test_pol5c_removal.py")
    _assert_skipif_guard_present("test_pol5c_removal.py", src)
    _assert_skill_dir_is_file_relative("test_pol5c_removal.py", src)


# ---------------------------------------------------------------------------
# Group C -- portability (hardcoded absolute paths to __file__-relative)
# ---------------------------------------------------------------------------


def test_plugin_conftest_repo_root_relative() -> None:
    """tests/plugin/conftest.py's REPO_ROOT must resolve via __file__, not
    Path.home(), so the fresh-install portability suite works on any machine.
    """
    src = _read("plugin/conftest.py")
    match = re.search(r"^\s*REPO_ROOT\s*=.*$", src, re.M)
    assert match is not None, (
        "tests/plugin/conftest.py must define a `REPO_ROOT = ...` assignment line"
    )
    line = match.group(0)
    assert "__file__" in line, (
        "tests/plugin/conftest.py's REPO_ROOT assignment line must be "
        f"derived from Path(__file__) for portability; found: {line!r}"
    )
    assert "Path.home()" not in line, (
        "tests/plugin/conftest.py's REPO_ROOT assignment line must not use "
        f"Path.home() (machine-specific); found: {line!r}"
    )


def test_methodology_ingest_bible_dir_relative() -> None:
    """tests/test_methodology_ingest.py must not bake any machine's
    username-specific absolute path into bible_dir; it must resolve via
    Path(__file__).resolve().parent.parent / "bible" so the
    dual-location-dedup invariant runs on any checkout instead of silently
    skipping off this machine.
    """
    src = _read("test_methodology_ingest.py")
    assert re.search(r"/home/[^/\s\"']+/", src) is None, (
        "test_methodology_ingest.py must not hardcode a username-baked "
        "absolute path; bible_dir must resolve via "
        'Path(__file__).resolve().parent.parent / "bible" so the '
        "dual-location-dedup invariant runs on any checkout"
    )
