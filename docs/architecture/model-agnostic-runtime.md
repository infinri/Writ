# Model-agnostic runtime and agent integration

**Status:** DECIDED target boundary; provider integrations not yet implemented.

## Model, agent, host and tool are different things

| Element | Owns | Doesn't own |
|---|---|---|
| ModelAdapter | Provider capabilities, inference, structured output, stream/tool-call proposals, usage, cancellation | Authorization or executing tool effects |
| AgentRunner | Optional Writ-managed task loop, planner, retry/budget and prompts | Trusted permission decisions |
| HostAdapter | Authenticated host session, host event translation, tool interception and mediation coverage | Provider inference, independent policy copy |
| ToolProvider | Typed tool descriptors and broker-mediated execution connector | Granting itself capabilities |
| Execution kernel | State transitions, policy decision | Model calls, prompt templates, domains |
| Capability Pack | Domain workflow, artifacts, evaluators, source and memory needs | Kernel or organization policy override |

A model with tool-calling does not automatically become an enforceable host. An agent can propose a tool call; the trusted broker executes only allowed effects.

## Two equal integration modes

**External host:** Claude Code first; then other agent hosts, such as Codex, Gemini CLI and custom MCP/SDK agents, subject to real adapter support. Their host retains the model conversation, planning loop and UI. Writ supplies context, policy, Recall, scoped tools and validation. **No ModelAdapter is required** if the host already calls its own model.

**Managed runner:** optional AgentRunner uses a ModelAdapter to request inference and process model events. It can execute coding, research, review and operations tasks specified by Capability Packs. It must call the broker for tool effects. The kernel remains independent of provider uptime, context length and pricing.

## ModelAdapter design

~~~text
ModelAdapter.describe(model_id) -> ModelCapabilities
ModelAdapter.generate(ModelRequest, cancellation) -> async ModelEvent stream
ModelAdapter.estimate_tokens(request) -> estimate + precision
ModelAdapter.health() -> availability metadata
~~~

Versioned capabilities include streaming, tool-call proposals, native structured output, cancellation, context/output limits and optional multimodal features. Availability can depend on model, account, region, version or price, and is negotiated rather than assumed. Writ validates all outputs locally, including when a provider advertises JSON schema enforcement.

A ModelRequest contains scoped tenant/project, model selection, messages/content parts, proposed tool descriptions, output constraints, evidence references, token/cost limits, privacy and egress preferences, tracing and deadline. Tools offered to a model are *descriptions*, not permission grants.

ModelEvent kinds include text deltas, proposed tool calls (stable call ID and arguments), structured output, usage, completion and error. Provider-native metadata may be kept in private redacted logs if allowed. Provider requests must never carry broker approval secrets. The adapter never directly executes proposed actions.

## Routing, fallback and reproducibility

Model choice can account for capability requirements, tenant privacy, approved egress, data residency, token budget and expected cost. A fallback must never weaken policy or silently change privacy jurisdiction. A provider lacking required tools/structured outputs returns an explicit unsupported/unavailable result. Record provider/model/version in evaluation evidence; multi-model agreement is not proof of truth.

## Tests and implementation order

- Stub two providers with different supported features and failure modes.
- Validate events across streaming chunk boundaries and malformed proposed tool arguments.
- Prove provider/tool-call proposals cannot skip the broker.
- Test cancellation, rate limits, timeouts, privacy-preserving fallback and unknown capabilities.
- Prove external-agent integration requires no Writ-managed model call.
- Add real provider adapters only after stub contracts and data retention controls pass.

See [Capability Packs](capability-packs.md), [AgentRunner](agent-orchestration.md), [host adapters](host-adapters.md), [contracts](../contracts/README.md) and [M0](../implementation/M0-START-HERE.md).
