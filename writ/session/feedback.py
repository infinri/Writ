"""Auto-feedback telemetry for the session helper.

POL-6g-3 extracts cmd_auto_feedback (correlate loaded rules with analysis outcomes and POST
the rule feedback to the Writ server in one batch) out of bin/lib/writ-session.py. Imports only
lower layers (cache, config.EXT_TO_DOMAIN) + stdlib; the daemon client is imported inside the
command. Acyclic; the facade re-exports WRIT_FEEDBACK_URL + cmd_auto_feedback.
"""

import hashlib
import json
import os
import sys
import time

from writ.session.cache import _read_cache, mutate_cache
from writ.session.config import EXT_TO_DOMAIN, PREFIX_TO_DOMAIN, UNIVERSAL_DOMAINS


WRIT_FEEDBACK_URL = "http://localhost:8765/feedback"

# A stored pending batch older than this is not resent: the server keeps its batch
# record for FEEDBACK_BATCH_TTL_DAYS (30), so a resend inside this age is always
# answered as a replay rather than applied again.
FEEDBACK_RESEND_MAX_AGE_DAYS = 7


def cmd_auto_feedback(session_id: str) -> None:
    """Correlate rules-in-context with analysis outcomes, POST feedback to Writ.

    Logic:
    - If files were written and analysis passed: positive feedback for loaded rules
      whose domain matches the file domains.
    - If analysis failed: negative feedback for loaded rules whose domain matches
      the failed file domains (rules were present but didn't prevent the error).
    - Only send feedback once per rule per session (tracked via feedback_sent).
    - A batch delivered but never answered is stored in feedback_pending_batches and
      resent, with the same batch_id and signals, before any new queue.
    """
    cache = _read_cache(session_id)
    rules = cache.get("loaded_rule_ids", [])
    results = cache.get("analysis_results", {})
    already_sent = set(cache.get("feedback_sent", []))
    # A batch that was delivered but never answered may have been applied; its rules
    # never join a new queue. They are resent only as the same stored batch (the server
    # answers a replayed batch_id without applying it again), or only reported.
    unconfirmed = set(cache.get("feedback_unconfirmed", []))
    pending = list(cache.get("feedback_pending_batches", []))

    if not rules or not results:
        return

    pass_domains, fail_domains = _classify_file_domains(results)
    rule_domain_map = _map_rule_domains(rules)
    feedback_queue = _build_feedback_queue(
        rules, already_sent | unconfirmed, rule_domain_map, pass_domains, fail_domains,
    )
    previously_sent = set(already_sent)
    client = None
    resend = {"resolved": set(), "done": set(), "expired": set(), "stop": False,
              "delivered": False}
    if pending:
        client = _daemon_client()
        resend = _resend_pending(client, pending, time.time())
    if resend["stop"]:
        outcome = {"unconfirmed": [], "not_found": [], "transport": "none", "pending": None}
    else:
        outcome = _send_feedback(feedback_queue, already_sent, session_id, client)
    if outcome["transport"] == "none" and resend["delivered"]:
        outcome["transport"] = "batch"
    newly_sent = already_sent - previously_sent
    newly_unconfirmed = set(outcome["unconfirmed"])
    resolved = set(resend["resolved"])
    dropped = resend["done"] | resend["expired"]
    new_pending = outcome["pending"]

    # Update cache with sent feedback. The correlation + network send above ran
    # unlocked (never hold the per-session lock across a POST); only the write-back
    # takes the lock, merging onto the FRESH cache so no other field is clobbered.
    final_unconfirmed = (unconfirmed | newly_unconfirmed) - resolved
    if newly_sent or newly_unconfirmed or resolved or dropped or new_pending:
        with mutate_cache(session_id) as fresh:
            if newly_sent or resolved:
                fresh["feedback_sent"] = sorted(
                    set(fresh.get("feedback_sent", [])) | already_sent | resolved)
            if newly_unconfirmed or resolved:
                fresh["feedback_unconfirmed"] = sorted(
                    (set(fresh.get("feedback_unconfirmed", [])) | newly_unconfirmed) - resolved)
            if dropped or new_pending:
                kept = [b for b in fresh.get("feedback_pending_batches", [])
                        if b.get("batch_id") not in dropped]
                if new_pending and all(b.get("batch_id") != new_pending["batch_id"]
                                       for b in kept):
                    kept.append(new_pending)
                fresh["feedback_pending_batches"] = kept
            final_unconfirmed = set(fresh.get("feedback_unconfirmed", []))

    signals = dict(_dedupe_queue(feedback_queue))
    report = {
        "feedback_sent": len(newly_sent),
        "positive": sum(1 for rid in newly_sent if signals.get(rid) == "positive"),
        "negative": sum(1 for rid in newly_sent if signals.get(rid) == "negative"),
        "skipped_already_sent": len(set(rules) & previously_sent),
        "unconfirmed": len(final_unconfirmed),
        "resolved": len(resolved),
        "not_found": len(outcome["not_found"]),
        "transport": outcome["transport"],
    }
    json.dump(report, sys.stdout)
    sys.stdout.write("\n")


def _batch_id(session_id: str, queue: list[tuple[str, str]]) -> str:
    """Content-addressed id of one batch: sha256 over the session id and the sorted
    rule_id:signal pairs, hex. The session id is hashed in so two sessions that send
    identical feedback are not answered as each other's replay."""
    parts = [session_id] + sorted(f"{rid}:{signal}" for rid, signal in queue)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


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


def _post_batch(client, batch_id: str, queue: list[tuple[str, str]]) -> tuple[int, str, bool]:
    return client.post_json_outcome(
        "/feedback/batch",
        {"batch_id": batch_id,
         "signals": [{"rule_id": rid, "signal": signal} for rid, signal in queue]},
        timeout=1.0,
    )


def _recorded_ids(status: int, text: str, queued: set[str]) -> tuple[list, list] | None:
    """(recorded, not_found) ids of `queued` from an answer that lists what was recorded,
    or None for anything else (error body, 422, 5xx, no answer): nothing was applied."""
    if status != 200:
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("recorded"), list):
        return None
    recorded = [rid for rid in body["recorded"] if rid in queued]
    not_found = [rid for rid in body.get("not_found") or [] if rid in queued]
    return recorded, not_found


def _resend_pending(client, pending: list[dict], now: float) -> dict:
    """Resend each stored batch, oldest first, as its own POST with its stored batch_id
    and signals. An answer listing what was recorded resolves it; any other answer keeps
    it pending, and so does 404/405 (per-rule sends have no idempotency). Not delivered
    keeps it pending and stops this run's sending. A batch older than the resend age is
    dropped unsent (its server record may be pruned) and its ids stay unconfirmed.

    Returns {"resolved": ids, "done": batch_ids, "expired": batch_ids, "stop": bool,
    "delivered": bool}.
    """
    out = {"resolved": set(), "done": set(), "expired": set(), "stop": False,
           "delivered": False}
    max_age = FEEDBACK_RESEND_MAX_AGE_DAYS * 86400
    for batch in sorted(pending, key=lambda b: b.get("queued_at") or 0):
        batch_id = batch.get("batch_id")
        queue = [(rid, signal) for rid, signal in batch.get("signals") or []]
        if not batch_id or not queue:
            continue
        if now - float(batch.get("queued_at") or 0) > max_age:
            out["expired"].add(batch_id)
            continue
        status, text, delivered = _post_batch(client, batch_id, queue)
        if not delivered:
            out["stop"] = True
            break
        out["delivered"] = True
        answered = _recorded_ids(status, text, {rid for rid, _ in queue})
        if answered is None:
            continue
        recorded, not_found = answered
        out["resolved"].update(recorded)
        out["resolved"].update(not_found)
        out["done"].add(batch_id)
    return out


def _send_feedback(feedback_queue: list[tuple[str, str]], already_sent: set[str],
                   session_id: str = "", client=None) -> dict:
    """POST the de-duplicated queue as ONE /feedback/batch, marking confirmed rules in
    `already_sent` (mutated in place).

    Only an answer that lists what was recorded confirms anything: recorded plus
    not_found are marked sent. An error body, 422 or 5xx marks nothing (the server
    applied nothing, the next run resends). Not delivered marks nothing. Delivered with
    no answer returns every queued id as unconfirmed, never sent, plus the batch itself
    as `pending`, so a later run resends it under the same batch_id and the server
    answers the replay without applying it twice. 404/405 is a daemon that predates the
    route, and gets the per-rule /feedback loop.

    Returns {"unconfirmed": [ids], "not_found": [ids], "transport": batch|legacy|none,
    "pending": {"batch_id", "signals", "queued_at"} | None}.
    """
    queue = _dedupe_queue(feedback_queue)
    result: dict = {"unconfirmed": [], "not_found": [], "transport": "none", "pending": None}
    if not queue:
        return result
    if client is None:
        client = _daemon_client()
    batch_id = _batch_id(session_id, queue)
    status, text, delivered = _post_batch(client, batch_id, queue)
    if status in (404, 405):
        result["transport"] = "legacy"
        _send_feedback_per_rule(client, queue, already_sent)
        return result
    if delivered:
        result["transport"] = "batch"
    if status == 0:
        if delivered:
            result["unconfirmed"] = [rid for rid, _ in queue]
            result["pending"] = {"batch_id": batch_id,
                                 "signals": [[rid, signal] for rid, signal in queue],
                                 "queued_at": time.time()}
        return result
    answered = _recorded_ids(status, text, {rid for rid, _ in queue})
    if answered is None:
        return result
    recorded, not_found = answered
    already_sent.update(recorded)
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
