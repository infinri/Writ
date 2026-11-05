# Writ V2 versioned draft contracts

**Status:** design contracts; not stable public APIs and not yet implemented.

## Execution and authority

- [Action Request](action-request.schema.json), [Decision](decision.schema.json), [Effect Receipt](effect-receipt.schema.json), [Audit Event](audit-event.schema.json), [Policy Manifest](policy-manifest.schema.json).

## Model and domain independence

- [Model Capabilities](model-capabilities.schema.json), [Model Request](model-request.schema.json), [Model Event](model-event.schema.json).
- [Capability Pack](capability-pack.schema.json), [TaskSpec](task-spec.schema.json), [Artifact](artifact.schema.json), [Evidence](evidence.schema.json), [EvaluationRecord](evaluation-record.schema.json).
- Three [TaskSpec fixtures](fixtures/) demonstrate coding, research and noncode review without core file-specific fields.

An identity field is *not* authentication proof. Tool descriptions and pack manifests are *not* broker permits. Model-provided JSON must be validated; provider-native structured output support is advisory until locally checked. The example `writ://` artifact references are not an implemented registry.

Before production usage, define canonical digests, principal proof, broker permit, human grant and revocation, independent JSON Schema validation, error taxonomy and stable versioning.
