"""Per-turn injection footprint: what Writ actually renders into one prompt.

corpus_footprint.py measures the STATIC corpus (what a rule weighs on disk).
This measures the RENDERED per-turn artifact (what a turn actually pays), because those
are different numbers and only the second one is the cost the user sees. It reports
bytes per channel, per rule and per field, plus the always-on/ranked OVERLAP: the set of
rules delivered twice in the same turn, once by the always-on block and again by the
ranked block a few hundred bytes lower in the same context window.

Everything here goes through the SAME renderers the request path uses:

- ranked channel: writ.session.budget_tracking.cmd_format, which the live route reaches
  via server._run_cmd_format_locked (a stdin/stdout swap around this same function, so
  the two produce byte-identical text). cmd_format is called directly here rather than
  through the server helper so a measurement does not drag the FastAPI app into an
  analysis module.
- always-on channel: writ.retrieval.prompt_bundle.render_always_on, unchanged.
- overlap: writ.retrieval.prompt_bundle.always_on_rule_ids intersected with the ranked
  ids. COMPUTED, never a hardcoded list. The five-id list that circulated in review
  notes was true for a 12-rule bundle that is now 10; a literal would have gone stale
  silently, which is the whole failure this instrument exists to make visible.

NO NUMBER HERE IS RECONSTRUCTED FROM FIELD LENGTHS. A raw len() of a field misses its
label ("VIOLATION: ") and its newline, so every per-field figure is a DIFF of two
production renders: the rule as given, and the rule with that one field blanked. A
reimplementation of the renderer would be measuring itself.

FAILURE IS LOUD. A stopped daemon raises InjectionFootprintError; it never returns a
zero-byte report. Zero bytes from a dead daemon reading as "we got it down to zero" is
the absence-is-the-only-signal failure, so absence is refused instead of reported.

The instrument mutates nothing: /always-on is a GET, /query is POSTed WITHOUT a
session_id (which would add a telemetry row and make a measurement look like a turn),
and no session cache is read or written.
"""
from __future__ import annotations

import io
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

from writ.analysis.token_audit import render_json  # single source for JSON rendering
from writ.session.config import DEFAULT_SESSION_BUDGET  # the budget a fresh turn passes

RANKED_BASIS = (
    "exact utf-8 bytes of the block cmd_format renders (the request path's renderer); "
    "per-field bytes are a diff of two production renders, never a len() of the field"
)
ALWAYS_ON_BASIS = (
    "exact utf-8 bytes of the block render_always_on renders (the request path's "
    "renderer); per-rule bytes are a singleton re-render through the same call"
)

# The four fields cmd_format renders for a ranked hit in standard mode. trigger and
# statement are the two the always-on channel also carries, so they are the two the
# field dedup suppresses; violation and pass_example are the ranked channel's own
# contribution.
RANKED_FIELDS = ("trigger", "statement", "violation", "pass_example")
_DEDUPED_FIELDS = ("trigger", "statement")
_RANKED_ONLY_FIELDS = ("violation", "pass_example")

_DEFAULT_TIMEOUT = 10.0

# cmd_format reads sys.stdin and writes sys.stdout, which are process-global. The daemon
# serializes its own swap behind server._FORMAT_STREAM_LOCK for exactly this reason (the
# C1 interleaved-output bug); this module is a single-threaded CLI diagnostic today, and
# the lock is here so that stays true if it is ever called from anywhere else.
_RENDER_LOCK = threading.Lock()

__all__ = [
    "ALWAYS_ON_BASIS",
    "RANKED_BASIS",
    "RANKED_FIELDS",
    "InjectionFootprintError",
    "build_report",
    "compute_overlap",
    "measure_always_on_channel",
    "measure_ranked_channel",
    "render_json",
    "render_text",
]


class InjectionFootprintError(Exception):
    """Unreachable daemon or malformed response -> CLI exit 2 (mirrors the
    corpus_footprint / token_audit canary convention)."""


def _bytes(text: str) -> int:
    return len(text.encode("utf-8")) if text else 0


def _render_ranked_block(rules: list[dict], mode: str) -> str:
    """The ranked block as cmd_format renders it, WRIT_META stripped by split_format.

    Byte-identical to what /prompt-bundle puts in `rules_text`, because it is the same
    two functions in the same order.
    """
    from writ.retrieval.prompt_bundle import split_format
    from writ.session.budget_tracking import cmd_format

    payload = json.dumps({"rules": rules, "mode": mode})
    with _RENDER_LOCK:
        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(payload)
        sys.stdout = buf = io.StringIO()
        try:
            cmd_format()
        except SystemExit:
            # cmd_format exits 0 on an empty rule list; that is a zero-byte block, not
            # an error.
            pass
        finally:
            sys.stdout = old_stdout
            sys.stdin = old_stdin
        raw = buf.getvalue()
    text, _meta = split_format(raw)
    return text


def _field_bytes(rule: dict, field: str, mode: str) -> int:
    """What one field costs in the rendered block, label and newline included.

    Measured as render(rule) - render(rule with `field` blanked), so an empty field
    measures 0 and a populated one carries its "WHEN: " / "VIOLATION: " prefix.
    """
    blanked = dict(rule)
    blanked[field] = ""
    return _bytes(_render_ranked_block([rule], mode)) - _bytes(
        _render_ranked_block([blanked], mode)
    )


def _rule_entry_bytes(rule: dict, mode: str) -> int:
    """The whole entry one rule costs: its header line, its field lines, and the blank
    line that separates it from the next rule.

    Measured as render([rule, rule]) - render([rule]), the marginal cost of one more
    copy, which isolates the rule's own chunk from the block's "--- WRIT RULES ---"
    wrapper without attributing that wrapper to whichever rule happens to be measured
    first. Both renders carry a single-digit rule count, so the header line is the same
    length in each and cancels. Measured, never summed from the field figures: the sum
    would miss the header line and the separating blank line.
    """
    one = _bytes(_render_ranked_block([rule], mode))
    two = _bytes(_render_ranked_block([rule, rule], mode))
    return two - one


def measure_ranked_channel(qresp: dict) -> dict:
    """Byte accounting for the ranked block, from a /query response.

    `qresp` is the exact shape /query returns ({"rules": [...], "mode": ...}); the mode
    matters because it selects which fields cmd_format renders at all.
    """
    rules = list(qresp.get("rules") or [])
    mode = qresp.get("mode") or "standard"
    block = _render_ranked_block(rules, mode)

    per_rule: dict[str, dict] = {}
    for rule in rules:
        rid = rule.get("rule_id") or ""
        if not rid:
            continue
        per_rule[rid] = {
            "total_bytes": _rule_entry_bytes(rule, mode),
            "fields": {f: _field_bytes(rule, f, mode) for f in RANKED_FIELDS},
        }

    return {
        "block_bytes": _bytes(block),
        "rule_count": len(rules),
        "rule_ids": [r.get("rule_id") for r in rules if r.get("rule_id")],
        "mode": mode,
        "per_rule": per_rule,
        "basis": RANKED_BASIS,
    }


def measure_always_on_channel(ao_json: dict) -> dict:
    """Byte accounting for the always-on block, from an /always-on response.

    `rendered_rule_ids` is what actually reached the agent: always_on_rule_ids applies
    the same renderable filter render_always_on does, so a rule missing its trigger or
    statement is absent from both the block and this report rather than being invented
    into it.
    """
    from writ.retrieval.prompt_bundle import always_on_rule_ids, render_always_on

    block, total_tokens, eligible_count = render_always_on(ao_json)
    rendered_ids = always_on_rule_ids(ao_json)

    per_rule_bytes: dict[str, int] = {}
    for rule in ao_json.get("rules") or []:
        singleton = {"total_tokens": 0, "rules": [rule]}
        ids = always_on_rule_ids(singleton)
        if not ids:
            continue  # dropped by the renderable filter; it costs the block nothing
        singleton_block, _, _ = render_always_on(singleton)
        per_rule_bytes[ids[0]] = _bytes(singleton_block)

    return {
        "block_bytes": _bytes(block),
        "rule_count": len(rendered_ids),
        # The gap between these two is the renderable filter's drop count, reported
        # rather than hidden: a rule that is eligible but unrenderable is invisible in
        # every other artifact.
        "eligible_rule_count": eligible_count,
        "rendered_rule_ids": rendered_ids,
        "per_rule_bytes": per_rule_bytes,
        "total_tokens": total_tokens,
        "basis": ALWAYS_ON_BASIS,
    }


def compute_overlap(ao_json: dict, qresp: dict) -> set[str]:
    """The rules delivered by BOTH channels this turn.

    Built from the RENDERED always-on ids, not the eligible ones. A rule the renderable
    filter dropped never reached the agent, so a ranked hit sharing its id is not a
    duplicate; calling it one would suppress the ranked text and point the reader at a
    block that never contained it, with no exception and no visible symptom.
    """
    from writ.retrieval.prompt_bundle import always_on_rule_ids

    rendered = set(always_on_rule_ids(ao_json))
    ranked = {r.get("rule_id") for r in (qresp.get("rules") or []) if r.get("rule_id")}
    return rendered & ranked


def _get_json(url: str, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as e:
        raise InjectionFootprintError(
            f"daemon unreachable at {url}: {e}. Refusing to report zero bytes for a "
            f"channel that was never measured; start the daemon and re-run."
        ) from e
    except ValueError as e:
        raise InjectionFootprintError(f"malformed JSON from {url}: {e}") from e


def _post_json(url: str, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as e:
        raise InjectionFootprintError(
            f"daemon unreachable at {url}: {e}. Refusing to report zero bytes for a "
            f"channel that was never measured; start the daemon and re-run."
        ) from e
    except ValueError as e:
        raise InjectionFootprintError(f"malformed JSON from {url}: {e}") from e


def _overlap_detail(
    overlap: set[str], ao_measured: dict, ranked_measured: dict
) -> list[dict]:
    """Per double-injected rule: what the second delivery costs against what it adds.

    `repeated_bytes` is the trigger and statement the ranked entry re-renders even
    though they are already above it; `new_bytes` is the violation and pass_example the
    always-on channel never carries. The gap between them is the price of the
    duplication, and it is measured per run rather than quoted.
    """
    detail = []
    for rid in sorted(overlap):
        entry = ranked_measured["per_rule"].get(rid, {})
        fields = entry.get("fields", {})
        detail.append({
            "rule_id": rid,
            "always_on_bytes": ao_measured["per_rule_bytes"].get(rid, 0),
            "ranked_bytes": entry.get("total_bytes", 0),
            "repeated_bytes": sum(fields.get(f, 0) for f in _DEDUPED_FIELDS),
            "new_bytes": sum(fields.get(f, 0) for f in _RANKED_ONLY_FIELDS),
        })
    return detail


def build_report(
    base_url: str,
    mode: str,
    probe_prompt: str,
    budget_tokens: int = DEFAULT_SESSION_BUDGET,
    at: str = "prompt",
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict:
    """Measure one probe turn against a live daemon and return the full report.

    Read-only end to end: /always-on is a GET, and /query is POSTed with no session_id
    (so no retrieval_result row is attributed to a measurement) and an explicit empty
    exclude list (so the numbers do not depend on whatever a real session happened to
    have loaded already).

    `budget_tokens` defaults to the fresh-session budget, because that is the budget the
    per-prompt path passes and it is what selects standard mode over summary; measuring
    at a different budget measures a different renderer.
    """
    base = (base_url or "").rstrip("/")
    ao_url = base + "/always-on?" + urllib.parse.urlencode(
        {"mode": mode or "universal", "at": at, "context": probe_prompt}
    )
    ao_json = _get_json(ao_url, timeout)
    if "error" in ao_json:
        raise InjectionFootprintError(f"/always-on returned an error: {ao_json['error']}")

    qresp = _post_json(
        base + "/query",
        {
            "query": probe_prompt,
            "budget_tokens": budget_tokens,
            "exclude_rule_ids": [],
        },
        timeout,
    )
    if "error" in qresp:
        raise InjectionFootprintError(f"/query returned an error: {qresp['error']}")

    ao_measured = measure_always_on_channel(ao_json)
    ranked_measured = measure_ranked_channel(qresp)
    overlap = compute_overlap(ao_json, qresp)
    detail = _overlap_detail(overlap, ao_measured, ranked_measured)

    return {
        "probe": {
            "base_url": base,
            "mode": mode,
            "prompt": probe_prompt,
            "budget_tokens": budget_tokens,
            "at": at,
        },
        "always_on": ao_measured,
        "ranked": ranked_measured,
        "overlap": {
            "rule_ids": sorted(overlap),
            "count": len(overlap),
            "per_rule": detail,
            "repeated_bytes_total": sum(d["repeated_bytes"] for d in detail),
            "new_bytes_total": sum(d["new_bytes"] for d in detail),
            "ranked_bytes_total": sum(d["ranked_bytes"] for d in detail),
        },
        "total_block_bytes": ao_measured["block_bytes"] + ranked_measured["block_bytes"],
        "basis": "exact utf-8 bytes of production-rendered blocks; no estimate, no tokenizer",
    }


def render_text(report: dict) -> str:
    """Human table. Channel totals first, then the overlap, which is the finding."""
    probe = report["probe"]
    ao = report["always_on"]
    ranked = report["ranked"]
    overlap = report["overlap"]

    lines: list[str] = []
    lines.append("=== injection-footprint (one probe turn, read-only) ===")
    lines.append(f"basis: {report['basis']}")
    lines.append(
        f"probe: mode={probe['mode']} at={probe['at']} "
        f"budget={probe['budget_tokens']} url={probe['base_url']}"
    )
    lines.append(f"prompt: {probe['prompt']!r}")
    lines.append("")

    lines.append(
        f"always-on channel: {ao['block_bytes']} bytes, {ao['rule_count']} rules rendered "
        f"of {ao['eligible_rule_count']} eligible"
    )
    lines.append(
        f"ranked channel:    {ranked['block_bytes']} bytes, {ranked['rule_count']} rules "
        f"({ranked['mode']} mode)"
    )
    lines.append(f"per-turn total:    {report['total_block_bytes']} bytes")
    lines.append("")

    lines.append(f"double-injected rules (in BOTH channels): {overlap['count']}")
    if overlap["count"]:
        lines.append(
            f"  {'rule_id':<24} {'always-on':>10} {'ranked':>8} {'repeated':>9} {'new':>7}"
        )
        for d in overlap["per_rule"]:
            lines.append(
                f"  {d['rule_id']:<24} {d['always_on_bytes']:>10} {d['ranked_bytes']:>8} "
                f"{d['repeated_bytes']:>9} {d['new_bytes']:>7}"
            )
        lines.append(
            f"  totals: {overlap['ranked_bytes_total']} ranked bytes deliver "
            f"{overlap['new_bytes_total']} bytes of new content and repeat "
            f"{overlap['repeated_bytes_total']} bytes already in context"
        )
    lines.append("")
    lines.append("per always-on rule (bytes):")
    for rid in ao["rendered_rule_ids"]:
        lines.append(f"  {rid:<32} {ao['per_rule_bytes'].get(rid, 0):>6}")
    lines.append("")
    lines.append("per ranked rule (bytes: total / trigger / statement / violation / pass):")
    for rid in ranked["rule_ids"]:
        entry = ranked["per_rule"].get(rid, {})
        f = entry.get("fields", {})
        lines.append(
            f"  {rid:<32} {entry.get('total_bytes', 0):>6} / {f.get('trigger', 0):>4} / "
            f"{f.get('statement', 0):>4} / {f.get('violation', 0):>4} / "
            f"{f.get('pass_example', 0):>4}"
        )
    return "\n".join(lines)
