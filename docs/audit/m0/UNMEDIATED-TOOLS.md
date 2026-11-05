# Unmediated tools and effect paths: Claude Code host (M0-03f)

**Status:** static analysis of the V1 baseline ([hook-registrations.csv](hook-registrations.csv)) plus the documented Claude Code hook contract. Not runtime-verified (M0-01e). Hand-authored; not generated.

## Headline

Under the Claude Code host, the **host** executes tool effects; Writ only observes or vetoes through hooks. That is the threat model's *hook-integrated/advisory* profile. No V1 effect, and no V2 effect routed through a host-executed tool, is *broker-enforced* in the sense of INV-01. Broker enforcement applies only to effects the broker itself performs (for example a Writ-provided MCP or CLI tool whose effect adapter verifies a permit, as in `m0/writ_m0/broker.py`), or to a host inside a broker-launched sandbox.

## Host tools without a V1 pre-effect gate

| Host tool or path | V1 PreToolUse coverage | Consequence |
|---|---|---|
| WebFetch, WebSearch | none (H42 is PostToolUse capture only) | Network egress is never vetoed |
| MCP tools (`mcp__*`) | only H40 (`.*`, tool-failure budget, not a policy gate) | Third-party effects are ungoverned |
| NotebookEdit | H31, H32 only; H33, H37, H38, H39 match Write or Write/Edit | Partial capability match (A-04) |
| Edit | H31, H32, H33; not H37, H38, H39 | Test-file, design-doc and memory-policy checks skipped for Edit |
| Grep, Glob | H30 only (debug gate); H28 read-junk gate matches Read only | Read-side checks inconsistent across read tools |
| Tools added to the host after the baseline | only `.*` registrations | Silently treated as covered by nothing (INV-13) |

## Effect paths no hook can mediate

| Path | Why |
|---|---|
| Effects composed inside one Bash command (pipes, subshells, `eval`, scripts, interpreters, `curl`) | H35 and H36 parse the command string; the threat model rejects universal shell parsing as a security boundary (A-03) |
| Background or detached processes started by Bash | Continue after the hook returns |
| A PreToolUse hook that crashes, times out or exits non-zero other than 2 | Host treats it as a non-blocking error; the tool runs (documented contract; runtime check pending) |
| Daemon unreachable for H27, H28, H29, H32, H36 | Default allow unless `WRIT_STRICT=1` (A-02) |
| Stop-hook "denials" in H15, H16, H17 | Logged as deny but exit 1, which the host does not treat as blocking |
| Subagent tool calls | Whether every hook fires inside subagents is unverified for the host version in use |
| Edits by other processes, editors, git hooks, CI | Outside the host entirely |

## V2 implication

Every Claude Code adapter release must publish this table for its supported host version, distinguish broker-enforced from hook-integrated actions, and map unmapped tools to `host.unmapped` (denied as unknown by the reference kernel; see `m0/tests/test_independence.py::TestHostNormalization::test_unmapped_host_tool_is_not_covered`).
