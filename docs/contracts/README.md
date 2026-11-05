# V2 contracts (draft)

These JSON Schemas describe the first normalized boundary contracts: [action request](action-request.schema.json), [decision](decision.schema.json), [effect receipt](effect-receipt.schema.json), [audit event](audit-event.schema.json), and [policy manifest](policy-manifest.schema.json).

**Status:** SPECIFICATION; not compiled into clients or guaranteed as a public stable API. A caller-supplied principal.verified_by string is **not** an authentication credential; trusted adapter code must derive identity independently. 'format: date-time' requires schema tooling with format assertion configured if validation is desired.

## Required evolution rules

Version via explicit contract_version/manifest_version and per-event payload schema; no silent widening of acceptance for privileged actions. Provide positive and negative fixtures, maximum field/input sizes, canonical JSON serialization for digests, and language-independent conformance tests before M2.

Missing permit_reference on an allow is not a broker permission; issuing and verifying broker permits is a separate protocol. Likewise a stored policy manifest does not make policy enforced: the evaluator_binding must resolve to registered executable policy and passing tests.

## Follow-up schemas

M2/M3 must add: normalized principal proof envelope, canonical target algorithm, authorization permit, human approval request/grant/revocation, immutable state transition command, tenant policy publication and retrieval evidence record. The absence of these files is deliberate; do not assume unspecified details.
