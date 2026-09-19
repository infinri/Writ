"""Auto-feedback telemetry for the session helper.

POL-6g-3 extracts cmd_auto_feedback (correlate loaded rules with analysis outcomes and POST
per-rule feedback to the Writ server) out of bin/lib/writ-session.py. Imports only lower layers
(cache, config.EXT_TO_DOMAIN) + stdlib; urllib is imported inside the command. Acyclic; the
facade re-exports WRIT_FEEDBACK_URL + cmd_auto_feedback.
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

    if not rules or not results:
        return

    pass_domains, fail_domains = _classify_file_domains(results)
    rule_domain_map = _map_rule_domains(rules)
    feedback_queue = _build_feedback_queue(
        rules, already_sent, rule_domain_map, pass_domains, fail_domains,
    )
    sent_count = _send_feedback(feedback_queue, already_sent)

    # Update cache with sent feedback. The correlation + network send above ran
    # unlocked (never hold the per-session lock across a POST); only the write-back
    # takes the lock, merging onto the FRESH cache so no other field is clobbered.
    if sent_count > 0:
        cache["feedback_sent"] = sorted(already_sent)  # keep local dict for the report below
        with mutate_cache(session_id) as fresh:
            fresh["feedback_sent"] = sorted(set(fresh.get("feedback_sent", [])) | already_sent)

    report = {
        "feedback_sent": sent_count,
        "positive": sum(1 for _, s in feedback_queue[:sent_count] if s == "positive"),
        "negative": sum(1 for _, s in feedback_queue[:sent_count] if s == "negative"),
        "skipped_already_sent": len([r for r in rules if r in set(cache.get("feedback_sent", [])) - already_sent]),
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


def _send_feedback(feedback_queue: list[tuple[str, str]], already_sent: set[str]) -> int:
    """POST each (rule_id, signal) to the Writ feedback endpoint, marking sent rules in
    `already_sent` (mutated in place). Stops on the first connection error (server down).
    Returns the count actually sent."""
    import urllib.error
    # Through the shared client (bin/lib/writ_daemon_client.py), which prefers the
    # daemon's unix socket and falls back to WRIT_FEEDBACK_URL's TCP endpoint. One of
    # three python call sites the E2a transport census exposed; a grep over curl sites
    # could not have found any of them.
    import os
    import sys

    _lib = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "bin", "lib",
    )
    if _lib not in sys.path:
        sys.path.insert(0, _lib)
    import writ_daemon_client

    sent_count = 0
    for rid, signal in feedback_queue:
        status, _body = writ_daemon_client.post_json(
            "/feedback", {"rule_id": rid, "signal": signal}, timeout=0.2
        )
        if status == 0:
            break  # Neither transport answered, stop trying
        already_sent.add(rid)
        sent_count += 1
    return sent_count
