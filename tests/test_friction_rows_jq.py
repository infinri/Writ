"""friction-rows.jq must emit the same rows as the python builder it replaces.

These are AUDIT records (rag_query, always_on_inject) written to the friction/audit stream
with 365-day retention, so a divergence here is not a cosmetic difference, it is a
corrupted trail that nothing would flag.

PARITY IS ON THE PARSED OBJECTS, not the text. python's json.dumps preserves insertion
order and jq's object construction does not always agree, and the consumer is json.loads
in the drain, so key order carries no meaning. Asserting on text would pin something that
is not the contract and block harmless filter edits.

The python arm is not a paraphrase: it is copied from writ-rag-inject.sh, which still runs
it whenever jq is absent. If that block changes, this copy must change with it, which is
why the test names the source explicitly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JQ_FILTER = REPO / "bin" / "lib" / "friction-rows.jq"
HOOK = REPO / "hooks" / "scripts" / "writ-rag-inject.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed; the python arm is the fallback"
)

# Copied from writ-rag-inject.sh's fallback arm. See the module docstring.
PY_BUILDER = r"""
import json, os, sys
try:
    b = json.load(sys.stdin)
except Exception:
    sys.exit(0)
sid = os.environ.get('WRIT_SID', '')
mode = os.environ.get('WRIT_MODE', '') or None
effort = os.environ.get('WRIT_EFFORT', '')
def rag(src, meta):
    e = {'session': sid, 'mode': mode, 'event': 'rag_query', 'query_source': src,
         'tokens_injected': int(meta.get('cost', 0)),
         'rules_returned_count': len(meta.get('rule_ids', [])), 'rule_ids': meta.get('rule_ids', [])}
    if effort:
        e['effort'] = effort
    e['event_name'] = 'UserPromptSubmit'; e['mechanism'] = 'stdout'
    return e
lines = []
bm = b.get('broad_meta')
if bm is not None:
    lines.append(rag('broad', bm))
ao = b.get('ao_meta')
if ao is not None and int(ao.get('tokens', 0)) > 0:
    lines.append({'session': sid, 'mode': mode, 'event': 'always_on_inject',
                  'tokens': int(ao.get('tokens', 0)), 'rule_count': int(ao.get('count', 0)),
                  'rule_ids': ao.get('rule_ids') or [],
                  'event_name': 'UserPromptSubmit', 'mechanism': 'stdout'})
mm = b.get('method_meta')
if mm is not None:
    lines.append(rag(mm.get('query_source', ''), mm))
for e in lines:
    print(json.dumps(e))
"""

# The first entry is the shape a live /prompt-bundle actually returns (captured
# 2026-08-07); the rest are the degenerate shapes the endpoint or a truncated read can
# produce. cost/tokens/count arrive as integers, which is why the filter's n0 only has to
# survive a MISSING field rather than reproduce python's float truncation.
BUNDLES = [
    json.dumps({"broad_meta": {"rule_ids": ["A", "B"], "cost": 600},
                "ao_meta": {"tokens": 1220, "count": 12, "rule_ids": ["X"] * 12},
                "method_meta": {"rule_ids": ["M1"], "cost": 320,
                                "query_source": "methodology"}}),
    json.dumps({"broad_meta": {"rule_ids": [], "cost": 0}}),
    json.dumps({"ao_meta": {"tokens": 0, "count": 3, "rule_ids": ["a"]}}),
    json.dumps({"ao_meta": {"tokens": 5}}),
    json.dumps({"method_meta": {"cost": 1}}),
    json.dumps({"broad_meta": {}, "ao_meta": {}, "method_meta": {}}),
    json.dumps({}),
    json.dumps({"broad_meta": None, "ao_meta": None, "method_meta": None}),
    json.dumps({"ao_meta": {"tokens": 7, "count": 1, "rule_ids": None}}),
    json.dumps({"broad_meta": {"rule_ids": ["Q"], "cost": 5,
                               "note": 'quotes " and $(cmd) and \\ backslash'}}),
    "not json",
    "",
]

ENVS = [
    {"WRIT_SID": "s1", "WRIT_MODE": "work", "WRIT_EFFORT": "high"},
    {"WRIT_SID": "s2", "WRIT_MODE": "", "WRIT_EFFORT": ""},
    {"WRIT_SID": "", "WRIT_MODE": "review", "WRIT_EFFORT": ""},
]


def _py_rows(body: str, env: dict[str, str]) -> list[dict]:
    proc = subprocess.run([sys.executable, "-c", PY_BUILDER], input=body,
                          capture_output=True, text=True, env=dict(env), timeout=60)
    return [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]


def _jq_rows(body: str, env: dict[str, str]) -> list[dict]:
    proc = subprocess.run(
        ["jq", "-R", "-s", "-r",
         "--arg", "sid", env["WRIT_SID"],
         "--arg", "mode", env["WRIT_MODE"],
         "--arg", "effort", env["WRIT_EFFORT"],
         "-f", str(JQ_FILTER)],
        input=body, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"jq failed: {proc.stderr[:200]}"
    return [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]


class TestRowParity:
    @pytest.mark.parametrize("env", ENVS, ids=[e["WRIT_SID"] or "no-sid" for e in ENVS])
    @pytest.mark.parametrize("body", BUNDLES, ids=range(len(BUNDLES)))
    def test_jq_matches_python(self, body: str, env: dict[str, str]) -> None:
        assert _jq_rows(body, env) == _py_rows(body, env)

    def test_the_real_shape_produces_three_rows(self) -> None:
        """Anti-vacuity for the whole class: if the filter emitted nothing for every
        input, every comparison above would pass against a python arm that also emitted
        nothing on a bug."""
        rows = _jq_rows(BUNDLES[0], ENVS[0])
        assert len(rows) == 3
        assert [r["event"] for r in rows] == ["rag_query", "always_on_inject", "rag_query"]

    def test_an_empty_mode_becomes_null_not_empty_string(self) -> None:
        """python writes `os.environ.get('WRIT_MODE','') or None`, so an unset mode is
        JSON null. Emitting "" instead would change what lands in the audit stream."""
        rows = _jq_rows(BUNDLES[0], ENVS[1])
        assert rows and all(r["mode"] is None for r in rows)

    def test_effort_is_omitted_when_empty(self) -> None:
        """`if effort:` means the key is absent, not empty. A row carrying effort="" would
        differ from every historical row for the same event."""
        rows = _jq_rows(BUNDLES[0], ENVS[1])
        rag = [r for r in rows if r["event"] == "rag_query"]
        assert rag and all("effort" not in r for r in rag)

    def test_effort_is_present_when_set(self) -> None:
        rag = [r for r in _jq_rows(BUNDLES[0], ENVS[0]) if r["event"] == "rag_query"]
        assert rag and all(r["effort"] == "high" for r in rag)

    def test_a_zero_token_always_on_inject_is_dropped(self) -> None:
        """The python builder's `> 0` test. Recording a zero-token inject would add rows
        the historical stream does not contain."""
        assert _jq_rows(BUNDLES[2], ENVS[0]) == []

    def test_malformed_input_yields_no_rows_and_does_not_fail(self) -> None:
        """python exits 0 silently on a bad body. jq without -R -s would fail instead,
        which under `set -euo pipefail` would abort the hook."""
        for bad in ("not json", ""):
            assert _jq_rows(bad, ENVS[0]) == []


class TestTheHookKeepsBothArms:
    def test_the_python_fallback_is_still_present(self) -> None:
        """The WRIT_NO_JQ seam: absence of jq must change speed, never behaviour. If the
        fallback is deleted, a machine without jq silently stops recording these audit
        rows."""
        source = HOOK.read_text()
        assert "_FRICTION_ROWS_OK" in source
        assert "WRIT_SID=" in source and "always_on_inject" in source, (
            "the python row builder is gone from the hook; a jq-less machine would "
            "record no rag_query or always_on_inject rows at all"
        )

    def test_the_fallback_is_chosen_on_jq_exit_status_not_empty_output(self) -> None:
        """A bundle with no metadata legitimately produces ZERO rows. Treating empty
        output as failure would spawn python to rediscover that there is nothing to
        emit, which is the pattern this cycle removed twice."""
        source = HOOK.read_text()
        assert 'if [ -z "$_FRICTION_ROWS_OK" ]; then' in source, (
            "the fallback no longer keys off jq's exit status"
        )
