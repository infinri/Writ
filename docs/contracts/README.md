# Writ V2 versioned draft contracts

**Status:** design contracts; not stable public APIs and not yet implemented.

## Execution and authority

- [Action Request](action-request.schema.json), [Decision](decision.schema.json), [Effect Receipt](effect-receipt.schema.json), [Audit Event](audit-event.schema.json), [Policy Manifest](policy-manifest.schema.json).
- M0-03a drafts: [Principal Proof](principal-proof.schema.json), [Approval Grant](approval-grant.schema.json), [Broker Permit](broker-permit.schema.json), [Policy Bundle](policy-bundle.schema.json). Digests use the canonical JSON form defined in `m0/writ_m0/canonical.py` ([D-006](../implementation/DECISION-LOG.md)). ApprovalGrant binds the sorted `requested_capabilities` set (SR-02). The M0 broker accepts ActionRequests only when sealed by a registered adapter ([D-010](../implementation/DECISION-LOG.md)); permits are single use (SR-07). BrokerPermit and EffectReceipt bind `request_fingerprint`, the canonical digest of the complete request (SR-10, SR-11, D-012). Evidence and Artifact require `project_id`; sources resolve per tenant and project, and evaluation fails for artifacts or signoffs outside the task's tenant, project and task (SR-05, SR-12, D-013).

## Model and domain independence

- [Model Capabilities](model-capabilities.schema.json), [Model Request](model-request.schema.json), [Model Event](model-event.schema.json).
- [Capability Pack](capability-pack.schema.json), [TaskSpec](task-spec.schema.json), [Artifact](artifact.schema.json), [Evidence](evidence.schema.json), [EvaluationRecord](evaluation-record.schema.json).
- Three [TaskSpec fixtures](fixtures/) demonstrate coding, research and noncode review without core file-specific fields. [Domain fixtures](fixtures/domains/) add artifacts, evidence and source catalogs for each, plus negative cases (fabricated citation, missing counterevidence). Evidence gained optional `evidence_kinds`, `stance`, `source_published_at` and `quoted_excerpt` so evaluators can verify claims instead of trusting producer status.

An identity field is *not* authentication proof. Tool descriptions and pack manifests are *not* broker permits. Model-provided JSON must be validated; provider-native structured output support is advisory until locally checked. The example `writ://` artifact references are not an implemented registry.

Before production usage, define canonical digests, principal proof, broker permit, human grant and revocation, independent JSON Schema validation, error taxonomy and stable versioning.
