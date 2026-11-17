# HostAdapter draft fixtures (M5b-01 and M5b-04)

The proposed schemas cover [advisory host observations](../../docs/contracts/proposed/host-event.schema.json), [coverage evidence](../../docs/contracts/proposed/host-coverage.schema.json) and [decision mapping](../../docs/contracts/proposed/host-outcome.schema.json). D-045 M5-D7/M5-D9/M5-D10 and M5-INTERFACES J-2/J-6 govern the boundary. These are synthetic contract fixtures. They establish neither host coverage nor portability acceptance.

## Observation and effect paths

The service derives the mode from the operation. A `host.event` is advisory. The registered protected effect path is a separate `effect.request` operation. Its typed inputs must be normalized service-side and checked through the broker. The observation schema cannot be used for effect ingress. Event IDs correlate observations and carry no session or request authority.

Before parsing, enforce the J-2 64 KiB control frame limit, measured in bytes. Use the kernel's strict JSON parser, then schema and operation-specific validation. The schema's character lengths are structural bounds. For identity labels enforce the service's 256-byte limit. Diagnostic input is untrusted data. A registered handler explicitly reads and validates the fields it needs. It never passes the input object through as an ActionRequest, grant or approval. Invalid framing may result in a generic error and a closed connection before an outcome exists.

Before sending any request, clients verify the configured service UID with J-2 peer credentials. Keep connected descriptors private to the client. The service obtains scope, principal, role and adapter identity from the configured connection binding. It generates a connection session ID and keeps authoritative agent and parent session IDs null until trusted delegation exists. Copy host claims only into `host_asserted`, with the event's host version and tool name recorded there as diagnostic values. The closed ActionRequest 2.0 contract currently keeps these labels alongside the sealed request with their digest. A change to request or proof encoding needs versioned review.

Clients may display advisory outcomes only as observations. The label for an unregistered tool is `ungoverned`. For an effect request, registration misses deny with no dispatch. Malformed and oversized inputs and unavailable hosts also fail closed. `continue_in_broker` denotes that only the broker may continue its protected execution, including its use-time checks. A native permission prompt or host approval never satisfies Writ's authenticated approval. Workflow transitions continue to require the dedicated transition path and current acceptance evidence. A claimed identity never selects a grant, establishes delegation or satisfies separation-dependent checks.

## Coverage and portability

A coverage row is scoped to the host version and environment of its test. Record what the interceptor actually observed or prevented, and include intermittent, unsupported and bypassed cases. Report duplicate observations or conflicting rows during review. Coverage rows never participate in operation routing or permission decisions. Promote fixture rows to observed evidence only after the pinned-host trial verifies them.

The portability matrix is a set of independent adapter acceptance cases. Feed the same operation through each adapter and compare only the ActionRequest and Decision under the same test-owned service connection, trusted clock, state, and broker policy. Record host diagnostics separately. Preserve Unicode path bytes and use the adapter's approved descriptor-based target resolution for real filesystem checks. No adapter attempts a side effect to establish equivalence.

Run the focused contract checks with `python3 -m unittest discover -s docs/contracts/proposed -p test_host_adapter_contract.py`.

The fixture test has two independent write-observation projections. It checks the resulting event, write intent and exact relative path for matched inputs, including non-ASCII paths and permission prompts. Claude-shaped fixtures use the documented hook field layout. The generic CLI shape is local to the fixture. These prototypes consume predecoded, bounded fixture values and have no I/O, sealing or dispatch behavior. [Claude Code hooks reference](https://code.claude.com/docs/en/hooks#pretooluse-input)
