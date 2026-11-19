# D-053 scoped criterion waiver

The closed approval context has purpose `writ-human-criterion-waiver/1`. Its
D-006 canonical digest identifies the exact review context and receipt. The existing M4 challenge
separately binds that digest, the waiver operation and verdict, tenant and
project, the eligible actor and credentials, policy digest, workflow revision
and ceremony idempotency key. Waiver authority is a scoped human permission.
The stored challenge is the single-use authorization and has the existing
120-second ceremony limit.

The context includes tenant, project, task, workflow, source and destination
states, expected revision, criterion, immutable snapshot and artifact digest,
reason, qualified requester, normalized exclusions, effective expiry, exact
signed selection and the current broker transition authorization digest. The
signed selection identity includes its scope, D-051 activation revision, full
publication digest, logical authorization digest and acceptance payload digest.
The publisher identities come from registry audit under the selection guard,
mapped to M4 subjects by the configured authority. Artifact and workflow
policy changers and enrolled links come from ledger state. The requester and
agent come from authenticated ingress. Enrollment aliases are resolved in
both directions from every excluded party. Requests that
indicate delegation refuse when the trusted chain is unavailable.

Acceptance policy version 1 carries no waiver permissions. Version 2 adds a
closed rule for each criterion with origin `project` or `invariant` and
`max_waiver_seconds`. Zero declines waiver permission. The disposable
registry has one project-waivable criterion, `waiver-review`. Other criteria
cannot be made waivable by changing their signed origin tag. The existing
required `review` criterion is ineligible. The signed digest includes the
metadata, and the unchanged kernel requirement projection retains every
mandatory criterion.

The D-051 registry guard pins activation, verification trust and publisher
audit. Each operation rechecks the signed pair and current verification keys
under that guard at a single persisted broker time sample, as in M4. A sample
earlier than the initial registry observation refuses. The existing broker
fence pins transition authorization, requires a matching acceptance dependency
and continues to refuse hard denial. The ledger compares the current workflow,
artifact, signed requirement projection and separation inputs in the transaction. The broker verifies the immutable snapshot contents before the
transaction. From the server's persisted request sample, the cap is the
earliest of the signed criterion limit, 600 seconds, the signed policy and key
expiry, and the trusted request deadline. The exact exclusive expiry is
returned in the ceremony's review context and fingerprinted before approval.

Successful recording stores `waived`, the exact binding, approving subject,
credential, consumed ceremony and audit in one transaction. The context digest
is the receipt lookup key after an uncertain commit. Request retry recovers the immutable context from the stable intent.
A receipt describes history and stays readable after expiry and revocation.
The configured broker's `validate_waiver` validates a reference against
current identity, authority, bindings, freshness and revocation status. The
validated result carries zero effect, signoff or publication authority. Protected
promotion continues through the existing acceptance check and considers
a mandatory waived criterion incomplete.

## Negative vectors

- Reject extra or missing context members, other purposes, invalid scope,
  noncanonical exclusion identities, duplicate policy criteria, unknown origin,
  unauthorized origin changes, and signed payload tampering.
- Reject forged or expired assertions and swapped ceremony purposes. Verify
  that the ceremony expires independently of the later waiver expiry.
- Refuse the requester, linked identities, current policy publishers, artifact
  changers, services and subjects missing the scoped waiver permission.
- Refuse changed bindings, including the same policy bytes at a new activation
  revision, and changes to independently fenced transition authorization.
- Refuse on expiry equality, missing trust, revocation of the authorization or
  its provenance, or missing coherent audit. Keep the recorded history.
- Race finishes and inject audit, storage and uncertain-commit failures.
  Recover the exact receipt after reopening and verify at most one record
  and one consumption.
- Verify protected promotion refuses an incomplete criterion and hard denial,
  with no advancement and no evidence consumption. Verify purpose isolation
  in both directions for grants, signoffs and publishers.
