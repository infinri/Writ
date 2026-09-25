"""Auto-feedback telemetry for the session helper.

POL-6g-3 extracts cmd_auto_feedback (correlate loaded rules with analysis outcomes and POST
the rule feedback to the Writ server in one batch) out of bin/lib/writ-session.py. Imports only
lower layers (cache, config.EXT_TO_DOMAIN) + stdlib; the daemon client is imported inside the
command. Acyclic; the facade re-exports WRIT_FEEDBACK_URL + cmd_auto_feedback.
"""

import json
import os
import sys

from writ.session.cache import _read_cache, mutate_cache
from writ.session.config import EXT_TO_DOMAIN, PREFIX_TO_DOMAIN, UNIVERSAL_DOMAINS


WRIT_FEEDBACK_URL = "http://localhost:8765/feedback"


def cmd_auto_feedback(session_id: str) -> None:
    """Correlate rules-in-context with analysis outcomes, POST feedback to Writ.

    Logic:
    - If files were written and analysis passed: positive feedback for loaded rules
      whose domain matches the file domains.
    - If analysis failed: negative feedback for loaded rules whose domain matches
      the failed file domains (rules were present but didn't prevent the error).
    - Only send feedback once per rule per session (tracked via feedback_sent).
    """
    cache = _read_cache(session_id)
    rules = cache.get("loaded_rule_ids", [])
    results = cache.get("analysis_results", {})
    already_sent = set(cache.get("feedback_sent", []))
    # A batch that was delivered but never answered may have been applied; its rules
    # are never resent (a resend could count them twice), only reported.
    unconfirmed = set(cache.get("feedback_unconfirmed", []))

    if not rules or not results:
        return

    pass_domains, fail_domains = _classify_file_domains(results)
    rule_domain_map = _map_rule_domains(rules)
    feedback_queue = _build_feedback_queue(
        rules, already_sent | unconfirmed, rule_domain_map, pass_domains, fail_domains,
    )
    previously_sent = set(already_sent)
    outcome = _send_feedback(feedback_queue, already_sent)
    newly_sent = already_sent - previously_sent
    newly_unconfirmed = set(outcome["unconfirmed"])

    # Update cache with sent feedback. The correlation + network send above ran
    # unlocked (never hold the per-session lock across a POST); only the write-back
    # takes the lock, merging onto the FRESH cache so no other field is clobbered.
    if newly_sent or newly_unconfirmed:
        with mutate_cache(session_id) as fresh:
            if newly_sent:
                fresh["feedback_sent"] = sorted(set(fresh.get("feedback_sent", [])) | already_sent)
            if newly_unconfirmed:
                fresh["feedback_unconfirmed"] = sorted(
                    set(fresh.get("feedback_unconfirmed", [])) | newly_unconfirmed)

    signals = dict(_dedupe_queue(feedback_queue))
    report = {
        "feedback_sent": len(newly_sent),
        "positive": sum(1 for rid in newly_sent if signals.get(rid) == "positive"),
        "negative": sum(1 for rid in newly_sent if signals.get(rid) == "negative"),
        "skipped_already_sent": len(set(rules) & previously_sent),
        "unconfirmed": len(newly_unconfirmed),
        "not_found": len(outcome["not_found"]),
        "transport": outcome["transport"],
    }
    json.dump(report, sys.stdout)
    sys.stdout.write("\n")


def _classify_file_domains(results: dict) -> tuple[set[str], set[str]]:
    """Map written-file extensions to (pass_domains, fail_domains) hint sets."""
    pass_domains: set[str] = set()
    fail_domains: set[str] = set()
    for filepath, outcome in results.items():
        ext = os.path.splitext(filepath)[1].lower()
        domain = EXT_TO_DOMAIN.get(ext)
        if domain:
            if outcome == "pass":
                pass_domains.add(domain)
            else:
                fail_domains.add(domain)
    return pass_domains, fail_domains


def _map_rule_domains(rules: list[str]) -> dict[str, str]:
    """Heuristic rule-id-prefix -> domain map (only rules with a known prefix)."""
    rule_domain_map: dict[str, str] = {}
    for rid in rules:
        prefix = rid.split("-")[0] if "-" in rid else rid
        mapped = PREFIX_TO_DOMAIN.get(prefix)
        if mapped:
            rule_domain_map[rid] = mapped
    return rule_domain_map


def _build_feedback_queue(
    rules: list[str], already_sent: set[str], rule_domain_map: dict[str, str],
    pass_domains: set[str], fail_domains: set[str],
) -> list[tuple[str, str]]:
    """Correlate each not-yet-sent loaded rule with its domain's file outcomes: positive if
    its domain had any passing files; negative if its domain had ONLY failing files.
    Universal-domain rules are relevant to every written file. Returns [(rule_id, signal)]."""
    feedback_queue: list[tuple[str, str]] = []  # (rule_id, signal)
    for rid in rules:
        if rid in already_sent:
            continue
        domain = rule_domain_map.get(rid)
        if not domain:
            continue

        # Check if this rule's domain is relevant to files that were written
        is_universal = domain in UNIVERSAL_DOMAINS
        relevant_to_pass = is_universal or domain in pass_domains
        relevant_to_fail = is_universal or domain in fail_domains

        if not relevant_to_pass and not relevant_to_fail:
            continue  # rule domain doesn't match any written files

        if relevant_to_pass and pass_domains:
            # Rule's domain had files that passed -- positive signal.
            # Even if some files failed, the rule helped on the passing ones.
            feedback_queue.append((rid, "positive"))
        elif relevant_to_fail and fail_domains and not relevant_to_pass:
            # Rule's domain ONLY had failing files -- negative signal.
            # Rules were in context but didn't prevent errors.
            feedback_queue.append((rid, "negative"))
    return feedback_queue


def _dedupe_queue(feedback_queue: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """One signal per rule id, the first one queued wins."""
    seen: dict[str, str] = {}
    for rid, signal in feedback_queue:
        seen.setdefault(rid, signal)
    return list(seen.items())


def _daemon_client():
    # Through the shared client (bin/lib/writ_daemon_client.py), which prefers the
    # daemon's unix socket and falls back to WRIT_FEEDBACK_URL's TCP endpoint. One of
    # three python call sites the E2a transport census exposed; a grep over curl sites
    # could not have found any of them.
    _lib = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "bin", "lib",
    )
    if _lib not in sys.path:
        sys.path.insert(0, _lib)
    import writ_daemon_client

    return writ_daemon_client


def _send_feedback(feedback_queue: list[tuple[str, str]], already_sent: set[str]) -> dict:
    """POST the de-duplicated queue as ONE /feedback/batch, marking confirmed rules in
    `already_sent` (mutated in place).

    Only an answer that lists what was recorded confirms anything: recorded plus
    not_found are marked sent. An error body, 422 or 5xx marks nothing (the server
    applied nothing, the next run resends). Not delivered marks nothing. Delivered with
    no answer returns every queued id as unconfirmed, never sent: the batch may have
    been applied, so resending could count it twice. 404/405 is a daemon that predates
    the route, and gets the per-rule /feedback loop.

    Returns {"unconfirmed": [ids], "not_found": [ids], "transport": batch|legacy|none}.
    """
    queue = _dedupe_queue(feedback_queue)
    result: dict = {"unconfirmed": [], "not_found": [], "transport": "none"}
    if not queue:
        return result
    client = _daemon_client()
    status, text, delivered = client.post_json_outcome(
        "/feedback/batch",
        {"signals": [{"rule_id": rid, "signal": signal} for rid, signal in queue]},
        timeout=1.0,
    )
    if status in (404, 405):
        result["transport"] = "legacy"
        _send_feedback_per_rule(client, queue, already_sent)
        return result
    if delivered:
        result["transport"] = "batch"
    if status == 0:
        if delivered:
            result["unconfirmed"] = [rid for rid, _ in queue]
        return result
    if status != 200:
        return result
    try:
        body = json.loads(text)
    except ValueError:
        return result
    if not isinstance(body, dict) or not isinstance(body.get("recorded"), list):
        return result
    queued = {rid for rid, _ in queue}
    not_found = [rid for rid in body.get("not_found") or [] if rid in queued]
    already_sent.update(rid for rid in body["recorded"] if rid in queued)
    already_sent.update(not_found)
    result["not_found"] = not_found
    return result


def _send_feedback_per_rule(client, queue: list[tuple[str, str]], already_sent: set[str]) -> None:
    """The pre-batch path, for an old daemon: POST each (rule_id, signal) to /feedback,
    marking every answered rule sent. Stops on the first connection error."""
    for rid, signal in queue:
        status, _body = client.post_json(
            "/feedback", {"rule_id": rid, "signal": signal}, timeout=0.2
        )
        if status == 0:
            break  # Neither transport answered, stop trying
        already_sent.add(rid)
