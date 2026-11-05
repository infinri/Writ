"""Inventory of V1 Claude Code hook registrations at the pinned baseline.

Every column is derived mechanically from hooks/hooks.json and the hook script
sources at the baseline commit. Static signals are line-referenced so a
reviewer can confirm each one. They are NOT runtime observations: whether a
signal fires, and how the host reacts, still requires isolated host tests
(task M0-01e).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import v1source

HOOKS_JSON = "hooks/hooks.json"
SCRIPT_PREFIX = "${CLAUDE_PLUGIN_ROOT}/"
COMMON_LIB = "bin/lib/common.sh"

# Host tool name -> normalized V2 capability. Unlisted tool names (MCP tools,
# future host tools) map to "unknown" so they are never treated as covered
# (INV-13).
TOOL_CAPABILITIES = {
    "Write": "file.write",
    "Edit": "file.write",
    "NotebookEdit": "file.write",
    "Read": "file.read",
    "Grep": "file.read",
    "Glob": "file.read",
    "Bash": "process.execute",
    "Task": "agent.dispatch",
    "Agent": "agent.dispatch",
    "WebFetch": "network.egress",
    "WebSearch": "network.egress",
    "ExitPlanMode": "workflow.transition",
}
CAPABILITY_TOOLS: dict[str, list[str]] = {}
for _tool, _cap in TOOL_CAPABILITIES.items():
    CAPABILITY_TOOLS.setdefault(_cap, []).append(_tool)

# What each lifecycle event can do to the host, per Claude Code's documented hook
# contract. This is the documented contract, not a runtime-verified fact for any
# specific host version.
EVENT_SEMANTICS = {
    "PreToolUse": ("pre_effect", "can_deny_tool"),
    "PostToolUse": ("post_effect", "feedback_only"),
    "PostToolUseFailure": ("post_effect", "feedback_only"),
    "UserPromptSubmit": ("prompt", "can_block_prompt"),
    "Stop": ("turn_end", "can_block_stop"),
    "SubagentStop": ("turn_end", "can_block_stop"),
    "SubagentStart": ("lifecycle", "context_only"),
    "SessionStart": ("lifecycle", "context_only"),
    "SessionEnd": ("lifecycle", "none"),
    "PreCompact": ("lifecycle", "context_only"),
    "PostCompact": ("lifecycle", "context_only"),
    "CwdChanged": ("lifecycle", "none"),
}

_SIGNALS = {
    "deny": re.compile(r"\bemit_deny\b|\"permissionDecision\"\s*:\s*\"deny\"|permissionDecision.{0,20}deny"),
    "ask": re.compile(r"\bemit_ask\b|\"permissionDecision\"\s*:\s*\"ask\""),
    "exit2": re.compile(r"\bexit 2\b"),
    "block": re.compile(r"\"decision\"\s*:\s*\"block\"|decision['\"]?\s*[:=]\s*['\"]block"),
    "context": re.compile(r"additionalContext|systemMessage"),
    "daemon": re.compile(r"\bcurl\b|\bwrit_http_(get|post)\b|\b_writ_curl_with_fallback\b|\b_writ_session\b"),
    "strict_mode": re.compile(r"WRIT_STRICT"),
    "logged_deny": re.compile(r"log_gate_decision\s+\S+\s+\"deny\""),
    "exit1": re.compile(r"\bexit 1\b"),
    "mint": re.compile(r"\bmint(s|ed|ing)?\b", re.IGNORECASE),
}


@dataclass(frozen=True)
class Registration:
    reg_id: str
    event: str
    matcher: str
    order_in_event: int
    script: str
    timeout: int | None


def load_registrations() -> list[Registration]:
    config = json.loads(v1source.read_text(HOOKS_JSON))
    regs: list[Registration] = []
    for event, groups in config["hooks"].items():
        position = 0
        for group in groups:
            for hook in group["hooks"]:
                if hook.get("type") != "command":
                    raise ValueError(f"unexpected hook type in {event}: {hook.get('type')}")
                position += 1
                regs.append(Registration(
                    reg_id=f"H{len(regs) + 1:02d}",
                    event=event,
                    matcher=group.get("matcher", "") or "",
                    order_in_event=position,
                    script=_script_path(hook["command"]),
                    timeout=hook.get("timeout"),
                ))
    return regs


def _script_path(command: str) -> str:
    match = re.search(r'"\$\{CLAUDE_PLUGIN_ROOT\}/([^"]+)"', command)
    if not match:
        raise ValueError(f"unparseable hook command: {command}")
    return match.group(1)


def matcher_tools(matcher: str) -> list[str]:
    """Known host tools selected by a matcher regex (Claude Code full-match semantics)."""
    if matcher in ("", ".*", "*"):
        return ["*"]
    pattern = re.compile(matcher)
    return [tool for tool in TOOL_CAPABILITIES if pattern.fullmatch(tool)]


def normalized_capabilities(event: str, matcher: str) -> list[str]:
    if event not in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        return [f"lifecycle.{event}"]
    tools = matcher_tools(matcher)
    if tools == ["*"]:
        return ["*"]
    return sorted({TOOL_CAPABILITIES[t] for t in tools})


def capability_gaps(event: str, matcher: str) -> list[str]:
    """Host tools sharing a capability with a matched tool but not matched (finding A-04)."""
    if event not in ("PreToolUse", "PostToolUse"):
        return []
    tools = matcher_tools(matcher)
    if tools == ["*"]:
        return []
    gaps = set()
    for cap in {TOOL_CAPABILITIES[t] for t in tools}:
        gaps.update(t for t in CAPABILITY_TOOLS[cap] if t not in tools)
    return sorted(gaps)


def scan_signals(source: str) -> dict[str, list[int]]:
    hits: dict[str, list[int]] = {name: [] for name in _SIGNALS}
    for lineno, line in enumerate(source.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for name, pattern in _SIGNALS.items():
            if pattern.search(line):
                hits[name].append(lineno)
    return hits


def coverage_class(event: str, signals: dict[str, list[int]]) -> str:
    stage, power = EVENT_SEMANTICS[event]
    refuses = bool(signals["deny"] or signals["ask"] or signals["exit2"] or signals["block"])
    if stage == "pre_effect" and refuses:
        return "preventive_candidate"
    if stage == "post_effect":
        return "post_effect_advisory"
    if power in ("can_block_prompt", "can_block_stop") and refuses:
        return "workflow_gate"
    # UserPromptSubmit stdout is added to model context by the host contract.
    if signals["context"] or event == "UserPromptSubmit":
        return "context_injection"
    return "observability_or_state"


def failure_posture(event: str, signals: dict[str, list[int]]) -> str:
    """Static description of what happens when the hook itself cannot decide.

    Claude Code treats a non-zero exit other than 2 as a non-blocking error, so a
    crashing pre-effect hook lets the tool proceed. Hooks that consult the daemon
    via common.sh fall back to allow unless WRIT_STRICT=1 (finding A-02).
    """
    stage, _power = EVENT_SEMANTICS[event]
    if stage != "pre_effect":
        return "not_applicable_no_effect_gate"
    if signals["daemon"]:
        return "fail_open_default; daemon_dependent; host_error_nonblocking"
    return "host_error_nonblocking"


def findings(reg: Registration, signals: dict[str, list[int]], script_registrations: int) -> list[str]:
    notes = []
    refuses = signals["deny"] or signals["ask"] or signals["exit2"] or signals["block"]
    if signals["logged_deny"] and not refuses:
        # Logged as a denial, but the only host-visible outcome is a non-blocking exit.
        notes.append("LOGS_DENY_WITHOUT_HOST_BLOCK")
    if capability_gaps(reg.event, reg.matcher):
        notes.append("PARTIAL_CAPABILITY_MATCH")
    if reg.event == "UserPromptSubmit" and signals["mint"]:
        notes.append("MINTS_AUTHORITY_FROM_PROMPT_TEXT")
    if EVENT_SEMANTICS[reg.event][0] == "post_effect" and (signals["exit2"] or signals["logged_deny"]):
        notes.append("POST_EFFECT_REFUSAL_CANNOT_PREVENT")
    if script_registrations > 1:
        notes.append("SCRIPT_REGISTERED_MULTIPLE_TIMES")
    return notes


COLUMNS = [
    "reg_id", "event", "matcher", "order_in_event", "script", "script_sha256",
    "script_lines", "lifecycle_stage", "host_power", "normalized_capabilities",
    "same_capability_tools_not_matched", "coverage_class", "failure_posture_static",
    "deny_lines", "ask_lines", "exit2_lines", "block_lines", "context_lines",
    "daemon_lines", "strict_mode_lines", "logged_deny_lines", "exit1_lines",
    "mint_lines", "findings", "evidence_basis",
]


def _lines(values: list[int]) -> str:
    return " ".join(str(v) for v in values)


def build_rows() -> list[dict[str, str]]:
    rows = []
    cache: dict[str, tuple[str, str]] = {}
    regs = load_registrations()
    per_script = {r.script: sum(1 for x in regs if x.script == r.script) for r in regs}
    for reg in regs:
        if reg.script not in cache:
            data = v1source.read_blob(reg.script)
            cache[reg.script] = (data.decode("utf-8"), v1source.sha256_hex(data))
        source, digest = cache[reg.script]
        signals = scan_signals(source)
        stage, power = EVENT_SEMANTICS[reg.event]
        rows.append({
            "reg_id": reg.reg_id,
            "event": reg.event,
            "matcher": reg.matcher,
            "order_in_event": str(reg.order_in_event),
            "script": reg.script,
            "script_sha256": digest,
            "script_lines": str(len(source.splitlines())),
            "lifecycle_stage": stage,
            "host_power": power,
            "normalized_capabilities": " ".join(normalized_capabilities(reg.event, reg.matcher)),
            "same_capability_tools_not_matched": " ".join(capability_gaps(reg.event, reg.matcher)),
            "coverage_class": coverage_class(reg.event, signals),
            "failure_posture_static": failure_posture(reg.event, signals),
            "deny_lines": _lines(signals["deny"]),
            "ask_lines": _lines(signals["ask"]),
            "exit2_lines": _lines(signals["exit2"]),
            "block_lines": _lines(signals["block"]),
            "context_lines": _lines(signals["context"]),
            "daemon_lines": _lines(signals["daemon"]),
            "strict_mode_lines": _lines(signals["strict_mode"]),
            "logged_deny_lines": _lines(signals["logged_deny"]),
            "exit1_lines": _lines(signals["exit1"]),
            "mint_lines": _lines(signals["mint"]),
            "findings": " ".join(findings(reg, signals, per_script[reg.script])),
            "evidence_basis": "static_scan; host_semantics_documented_not_runtime_verified",
        })
    return rows
