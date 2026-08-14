"""Static delivery lint for Writ hooks (Task #7C: the "never blind again" guard).

Reads hooks/hooks.json and each wired script, then flags injectors whose rule
text cannot reach the model. A hook on a NON-special event that emits directive/
RAG text to plain stdout without an additionalContext / permissionDecisionReason
wrapper goes only to the CC debug log (see writ.shared.delivery), so the model
never sees those rules -- cost paid, nothing delivered.

This is the STATIC complement to the runtime delivery telemetry in
writ.analysis.friction: the linter catches an inert injector before it ever runs;
the telemetry confirms it from real events. A source whose logged deliveries are
all "debug-log" is the runtime confirmation of a static "inert" flag here.

It also flags the inverse failure (cycle G): a script that emits additionalContext on an
event whose schema does not accept it, where CC's validator discards the ENTIRE reply. That
check runs BEFORE the injector gate, because the script this bug shipped in
(writ-postcompact.sh) matched neither injector heuristic and so was never examined at all.

Conservative by design (the C1 lesson, where a 13-agent audit false-flagged a
working security gate): two-signal matching, an explicit allowlist, comment-stripped
detection, and findings default to WARNINGS rather than hard failures.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from writ.shared.delivery import (
    ADDITIONAL_CONTEXT_EVENTS,
    STDOUT_TO_MODEL_EVENTS,
    reaches_model,
)

# Writ's injection signature: model-facing directive blocks start with "[Writ:"
# or "[WRIT ...". Detected only when rendered to BARE STDOUT (the channel that
# fails to reach the model on a non-special event) -- never when it merely
# appears in source (a comment, a stderr warning, or inside an additionalContext
# JSON string is NOT an inert injection). This precision is the C1 lesson:
# validate-rules.sh emits "[Writ ...]" to stderr and must NOT be flagged.
_INJECT_MARKER = re.compile(r"\[[Ww][Rr][Ii][Tt][ :\]]")
# echo/printf <text>. The marker + redirection are checked against the rest.
_BARE_MARKER_LINE = re.compile(r"^\s*(echo|printf)\b(?P<rest>[^\n]*)")
# cat <<EOF / cat << 'DELIM' / cat <<-DELIM : start of a heredoc whose body
# streams to stdout (unless the same line redirects it away).
_HEREDOC_START = re.compile(r"^(?P<pre>.*?)<<-?\s*['\"]?(?P<delim>\w+)['\"]?")
_REDIRECT = re.compile(r">&2|1>&2|>>?\s*\S")
# If the emitted text is itself a model-facing wrapper, it is NOT bare injection:
# an `echo`/`printf` of a hookSpecificOutput/additionalContext JSON reaches the
# model regardless of event. Skip such emissions.
_MODEL_CHANNEL_TOKENS = ("additionalContext", "hookSpecificOutput", "permissionDecisionReason")

# Scripts known-correct or intentionally debug-log-only. Empty for now: the
# heuristic is precise enough against the current set. Add a basename here (with
# a reason) only after verifying by triggering that it is not inert.
_ALLOWLIST: frozenset[str] = frozenset()


def _resolve_script(command: str, plugin_root: Path) -> Path | None:
    """Resolve a hooks.json command string to a script path under plugin_root."""
    m = re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/(\S+)", command)
    if m:
        return plugin_root / m.group(1)
    for tok in reversed(command.split()):
        if tok.endswith(".sh"):
            return plugin_root / tok.lstrip("/")
    return None


def _strip_comments(src: str) -> str:
    """Drop whole-line shell comments, keeping line count irrelevant.

    Full lines only. A trailing `# ...` is NOT stripped, because a bare `#` inside a
    single-quoted string, a jq filter or a URL fragment is ordinary code and cutting there
    would delete real emissions. Whole-line stripping is enough for the precision case this
    exists for (writ-precompact.sh discusses additionalContext in prose only) and cannot
    misread code as comment.
    """
    return "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )


def _reaches_model(src: str, event: str) -> bool:
    """True if the script has a model-facing channel THAT THIS EVENT ACCEPTS.

    The event argument is the cycle-G correction: this used to be a bare substring test, so
    "the file mentions additionalContext somewhere" read as proof of delivery even on
    PostCompact, where CC's validator discards the whole reply. The answer now comes from
    writ.shared.delivery, the single source both this linter and the runtime telemetry read.
    """
    if "permissionDecisionReason" in src:
        return True
    return "additionalContext" in src and reaches_model(event, "additionalContext")


def _emits_rejected_additional_context(src: str, event: str) -> bool:
    """True if the script emits additionalContext on an event that rejects it.

    Evaluated BEFORE the _is_injector gate, which is the whole point: writ-postcompact.sh
    passed neither _is_injector branch (a python3 heredoc, not `cat`, and no
    log_rag_query_event call), so the event-aware _reaches_model above would never have been
    consulted for it and the bug would have shipped green a second time.
    """
    if event in ADDITIONAL_CONTEXT_EVENTS:
        return False
    return "additionalContext" in _strip_comments(src)


def _emits_marker_to_stdout(src: str) -> bool:
    """True if the script renders the Writ injection marker to BARE stdout, via
    echo/printf or a cat-heredoc, without redirecting it to stderr or a file.

    This is the precise "injection text the model would only see on a special
    event" signal. It deliberately ignores the marker inside additionalContext
    JSON (bible-authoring-push) and on stderr (validate-rules)."""
    lines = src.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _BARE_MARKER_LINE.match(line)
        if m:
            rest = m.group("rest")
            wraps_model_channel = any(t in rest for t in _MODEL_CHANNEL_TOKENS)
            if (not (">&2" in rest or "1>&2" in rest)
                    and not wraps_model_channel
                    and _INJECT_MARKER.search(rest)):
                return True
        hd = _HEREDOC_START.search(line)
        if hd and "cat" in line.split("<<")[0]:
            pre = line.split("<<")[0]
            # Captured by a command substitution (VAR=$(cat <<...) or `cat <<...`)
            # -> the heredoc feeds the substitution, NOT the hook's stdout, so it
            # is not bare injection (it is typically delivered via additionalContext).
            captured = ("$(" in pre) or ("`" in pre)
            redirected = bool(_REDIRECT.search(pre))
            delim = hd.group("delim")
            j = i + 1
            body_has_marker = False
            body_wraps_channel = False
            while j < len(lines) and lines[j].strip() != delim:
                if _INJECT_MARKER.search(lines[j]):
                    body_has_marker = True
                if any(t in lines[j] for t in _MODEL_CHANNEL_TOKENS):
                    body_wraps_channel = True
                j += 1
            if not captured and not redirected and body_has_marker and not body_wraps_channel:
                return True
            i = j
            continue
        i += 1
    return False


def _is_injector(src: str) -> bool:
    """True if the script delivers rules/directives meant for the model: it logs
    an injection (log_rag_query_event) or renders the marker to bare stdout."""
    return ("log_rag_query_event" in src) or _emits_marker_to_stdout(src)


def lint_hooks(hooks_json: Path, plugin_root: Path) -> list[dict]:
    """Return delivery findings for every injector wired to a non-special event.

    Each finding: {severity, script, event, matcher, detail}. severity is
    "rejected" (the script emits additionalContext on an event whose schema does not accept
    it, so CC discards the entire reply), "inert" (high confidence: no model channel this
    event accepts), "review" (a model channel exists but the script ALSO emits injection text
    to bare stdout on this event), or "error" (could not read hooks.json).
    """
    findings: list[dict] = []
    try:
        data = json.loads(hooks_json.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return [{
            "severity": "error", "script": str(hooks_json), "event": "-",
            "matcher": "-", "detail": f"cannot read hooks.json: {e}",
        }]

    hooks = data.get("hooks", data)
    for event, groups in hooks.items():
        # On these events plain stdout reaches the model, so injecting freely is
        # correct -- nothing to flag.
        if event in STDOUT_TO_MODEL_EVENTS:
            continue
        if not isinstance(groups, list):
            continue
        for g in groups:
            matcher = g.get("matcher", "")
            for h in g.get("hooks", []):
                spath = _resolve_script(h.get("command", ""), plugin_root)
                if spath is None or not spath.exists():
                    continue
                name = spath.name
                if name in _ALLOWLIST:
                    continue
                try:
                    src = spath.read_text()
                except OSError:
                    continue
                # BEFORE the injector gate: a rejected payload is a delivery failure whether
                # or not the script looks like an injector to the two heuristics below.
                if _emits_rejected_additional_context(src, event):
                    findings.append({
                        "severity": "rejected", "script": name, "event": event,
                        "matcher": matcher,
                        "detail": (
                            f"emits hookSpecificOutput.additionalContext on {event}, which "
                            "is not in writ.shared.delivery.ADDITIONAL_CONTEXT_EVENTS. CC's "
                            "hook-output validator rejects the payload and discards the "
                            "ENTIRE reply ('(root): Invalid input'), so nothing is "
                            "delivered. Queue the text and emit it on a confirmed channel "
                            "(see writ-postcompact.sh -> post_compact_pending)."
                        ),
                    })
                    continue
                if not _is_injector(src):
                    continue
                if not _reaches_model(src, event):
                    findings.append({
                        "severity": "inert", "script": name, "event": event,
                        "matcher": matcher,
                        "detail": (
                            "injects rule/directive text to plain stdout on a "
                            "non-special event -> CC debug log; the model never "
                            "sees it. Wrap in hookSpecificOutput.additionalContext "
                            "if this event accepts it (see "
                            "writ-bible-authoring-push.sh), otherwise queue the text "
                            "for an event that does."
                        ),
                    })
                elif _emits_marker_to_stdout(src):
                    findings.append({
                        "severity": "review", "script": name, "event": event,
                        "matcher": matcher,
                        "detail": (
                            "has a model channel (additionalContext/"
                            "permissionDecisionReason) but ALSO emits injection "
                            "text to bare stdout on this event (debug-log only); "
                            "verify nothing model-facing rides the bare path."
                        ),
                    })
    # Stable order for deterministic output / tests.
    sev_order = {"error": 0, "rejected": 1, "inert": 2, "review": 3}
    findings.sort(key=lambda f: (sev_order.get(f["severity"], 9), f["event"], f["script"]))
    return findings
