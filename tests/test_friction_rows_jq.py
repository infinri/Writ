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

THAT COUPLING IS NOW EXECUTABLE rather than a promise in a docstring:
tests/test_effort_hop_removed.py::TestTheCopiedPythonBuilderIsTheBlockTheHookRuns asserts
PY_BUILDER is byte-equal to the block the hook really runs, derived from the hook source by
tests/_inventory.py::rag_inject_python_blocks as a map keyed by the shell variable each
inline block fills. A drifted copy would otherwise leave the parity oracle below comparing
the filter against a fiction.
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
def rag(src, meta):
    e = {'session': sid, 'mode': mode, 'event': 'rag_query', 'query_source': src,
         'tokens_injected': int(meta.get('cost', 0)),
         'rules_returned_count': len(meta.get('rule_ids', [])), 'rule_ids': meta.get('rule_ids', [])}
    e['event_name'] = 'UserPromptSubmit'; e['mechanism'] = 'stdout'
    return e
lines = []
bm = b.get('broad_meta')
if bm is not None:
    # A suppressed ranked channel (include_ranked=false) is NOT a zero-rule
    # rag_query: a zero-rule rag_query is the abstention signal every census
    # that counts retrievals by source relies on, so recording the
    # suppression that way would be indistinguishable from a real retrieval
    # that came back empty.
    if bm.get('suppressed'):
        lines.append({'session': sid, 'mode': mode, 'event': 'rag_channel_suppressed',
                      'channel': 'broad', 'event_name': 'UserPromptSubmit', 'mechanism': 'stdout'})
    else:
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
    # Appended, not inserted: every index above is asserted on by literal
    # position elsewhere in this file (e.g. BUNDLES[2] in
    # test_a_zero_token_always_on_inject_is_dropped), so a new shape goes at
    # the end to avoid shifting them. The suppressed-ranked-channel shape
    # (plan dfacff61, capability "For a bundle whose broad_meta is
    # {suppressed: true} ..."): a work-mode master with the ranked channel off.
    json.dumps({"broad_meta": {"suppressed": True},
                "ao_meta": {"tokens": 1220, "count": 12, "rule_ids": ["X"] * 12}}),
]

# Named separately from BUNDLES[-1] so TestSuppressedRankedChannelRow reads
# without counting list positions.
SUPPRESSED_BUNDLE = BUNDLES[-1]

ENVS = [
    {"WRIT_SID": "s1", "WRIT_MODE": "work"},
    {"WRIT_SID": "s2", "WRIT_MODE": ""},
    {"WRIT_SID": "", "WRIT_MODE": "review"},
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

    def test_a_zero_token_always_on_inject_is_dropped(self) -> None:
        """The python builder's `> 0` test. Recording a zero-token inject would add rows
        the historical stream does not contain."""
        assert _jq_rows(BUNDLES[2], ENVS[0]) == []

    def test_malformed_input_yields_no_rows_and_does_not_fail(self) -> None:
        """python exits 0 silently on a bad body. jq without -R -s would fail instead,
        which under `set -euo pipefail` would abort the hook."""
        for bad in ("not json", ""):
            assert _jq_rows(bad, ENVS[0]) == []


class TestSuppressedRankedChannelRow:
    """plan.md capability: 'For a bundle whose broad_meta is {"suppressed":
    true}, the jq row builder and the python fallback emit identical parsed
    rows'. Neither arm may emit a rag_query row for the suppressed channel,
    because a zero-rule rag_query is the abstention signal every census that
    counts retrievals by source relies on.

    This class asserts the SPECIFIC shape on top of the generic
    TestRowParity.test_jq_matches_python parametrization above (which, now
    that SUPPRESSED_BUNDLE is appended to BUNDLES, already proves the two
    arms AGREE with each other on this input): agreement alone would also
    hold if BOTH arms regressed to emitting a plain rag_query for a
    suppressed channel, so this checks each arm against the capability
    itself, not only against the other arm.

    MUTATION (plan.md verification table, "suppression row"): emitting a
    zero-rule rag_query for 'broad' instead of the suppression row turns this
    red. In the jq filter today it turns THIS red without needing a
    deliberate mutation, since the production filter does not yet know the
    "suppressed" key at all.
    """

    def test_jq_and_python_agree_on_the_suppressed_shape(self) -> None:
        assert _jq_rows(SUPPRESSED_BUNDLE, ENVS[0]) == _py_rows(SUPPRESSED_BUNDLE, ENVS[0])

    def test_the_suppressed_row_replaces_a_zero_rule_broad_query(self) -> None:
        rows = _jq_rows(SUPPRESSED_BUNDLE, ENVS[0])
        broad_queries = [
            r for r in rows if r.get("event") == "rag_query" and r.get("query_source") == "broad"
        ]
        suppressed = [r for r in rows if r.get("event") == "rag_channel_suppressed"]
        assert broad_queries == [], (
            f"the suppressed ranked channel produced a rag_query row: {broad_queries}"
        )
        assert len(suppressed) == 1, rows
        assert suppressed[0].get("channel") == "broad", suppressed[0]

    def test_the_always_on_row_is_unaffected_by_the_suppression(self) -> None:
        """The suppression is per-channel: channel 2's row must still appear,
        proving the suppressed branch did not swallow the whole bundle."""
        rows = _jq_rows(SUPPRESSED_BUNDLE, ENVS[0])
        always_on = [r for r in rows if r.get("event") == "always_on_inject"]
        assert len(always_on) == 1, rows


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
