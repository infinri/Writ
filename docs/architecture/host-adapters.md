# Host adapters and normalized capabilities

**DECIDED:** Writ core does not depend on Claude Code event JSON, tool names, or stdout semantics. **OPEN:** the exact set of actions each host can truly mediate.

## Adapter responsibilities

1. Authenticate the host origin and bind principal, project, tenant and session without trusting raw caller text.
2. Parse a bounded versioned host event. Reject malformed/ambiguous input.
3. Normalize tool variants into a declared capability + canonical resource.
4. Submit an ActionRequest to the local kernel and map the Decision back to the host.
5. If broker-mediated, invoke the broker rather than the host's unmediated executor.
6. For advisory-only hosts, disclose gaps clearly and never claim security prevention.
7. Forward context/RAG independently from authorization, with injection sanitization and caps.
8. Emit trace/receipt references without leaking approval credentials in logs.

## Initial capability registry

| Capability | Examples | Prevention only if |
|---|---|---|
| file.read | Read, Grep, Glob, filesystem API | host routes read through controlled adapter or sandbox |
| file.write | Write, Edit, NotebookEdit, file patch | host routes every file write through broker/sandbox |
| process.execute | shell, subprocess, interpreter | broker launches process under enforced capabilities/sandbox |
| network.egress | HTTP, WebFetch, sockets | network egress controlled at actual network boundary |
| repository.publish | git commit/push, PR create | Git credentials/API requests controlled by broker |
| policy.publish | rule promotion, org policy update | trusted admin publisher, separate role and signed bundle |
| memory.write | add/update/delete Recall fact | scoped memory port with provenance and consent checks |
| workflow.transition | advance plan, request approval | authenticated transition + expected state revision |

A hook rule matching only Write does not cover Edit; a text parser of shell syntax does not mediate all process effects.

## Claude Code bridge profile

Minimum supported event mappings, subject to host-integration verification:

- UserPromptSubmit: advisory context and high-level intent only; does not prove user authentication for security approval by itself.
- PreToolUse: translate known tools, apply policy, return compatible host deny/ask/allow output. The broker-controlled profile requires tools be routed through controlled executor or sandbox; plain hooks alone are interception, not full mediation.
- PostToolUse/Failure: observe and verify receipts/feedback; cannot prevent an already completed effect.
- Session/Subagent/Stop lifecycle: manage state and context without inventing a global current-session pointer, treating a child as its parent or adding a second authorization decision.

V1 hook inventory is a **migration evidence source**, not a target layout. The target Claude integration should be a small launcher plus one normalized event-handler executable, not 52 independent scripts. Separate stdout-format and host-size constraints from policy semantics.

## Interoperability

Define HostAdapter and BrokerExecutor ports independent of language. MCP-hosted tools may advertise capabilities, but tool metadata is not proof of permission; broker validates actual operation/target before executing. SDK adapters require transport authentication and tenant-bound principal mapping. Standard JSON Schemas under [contracts](../contracts/) are the source for generated clients.

## Coverage matrix and unsupported calls

Each adapter publishes a machine-readable matrix: event name, tool variants, canonical capability, interceptor location, can_prevent boolean, can_observe boolean, test ID and host version. Any unknown action is DENY for a governed broker profile; advisory profile labels it ungoverned. Release docs must not silently fill coverage gaps using assumptions.

## Portability acceptance

At M5, two independent adapters shall pass identical ActionRequest and Decision fixtures. Differential tests include Unicode paths, symlinks, huge payloads, host failures, nested subagents and permission-prompt semantics. No adapter may introduce a host-specific rule bypass.

## External agent hosts versus model providers

A HostAdapter integrates with an agent runtime/tool execution environment; a ModelAdapter integrates with model inference APIs. Claude Code, Codex and Gemini CLI are potential **host** integrations, not merely model aliases. Claude/GPT/Gemini/local models are **inference providers** when Writ manages its own AgentRunner. External host mode must function without Writ model credentials. A model proposing a tool call never grants its execution. [Full design](model-agnostic-runtime.md).
