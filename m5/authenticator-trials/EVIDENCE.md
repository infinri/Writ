# Real authenticator evidence template

Use one record per device class. All rows begin pending. Use pseudonyms for disposable subjects and stable local case labels for requests. Export observations and outcome codes only. Keep raw cookies, credentials, identifiers from WebAuthn responses, challenge material, personal accounts, biometric information and device serials out of evidence.

| Field | Value |
|---|---|
| Named owner, run ID, date/time and timezone | Pending |
| Reviewed Writ SHA, build digest and package/tool versions | Pending |
| Designated guest, image SHA-256, hypervisor and guest kernel | Pending |
| Browser release, disposable profile and authenticator class | Pending |
| Roaming key model and firmware, or platform provider and OS version | Pending |
| Origin, RP ID and address binding observations | Pending |
| Agent/desktop separation evidence and physical device access observations | Pending |
| Reviewed bootstrap/fixture revision and trusted readback source | Pending |
| Synthetic tenant/project and operator, approver, signer and revoker case labels | Pending |
| Sanitized archive digest, profile/credential removal and guest destruction confirmation | Pending |

## Case results

Repeat by device class when more than one run is needed. Include request context and refusal source only as sanitized case labels and reason codes. Record fresh UV observed, assertion delivery (yes, no or unknown), and the confirmed stored state separately.

| Case | Device class | Observed UI and refusal source | Verifier and stored outcome | Evidence reference | Status |
|---|---|---|---|---|---|
| RA-01 Enroll | Pending | Pending | Pending | Pending | Pending |
| RA-02 Read and scope | Pending | Pending | Pending | Pending | Pending |
| RA-03 Approve | Pending | Pending | Pending | Pending | Pending |
| RA-04 Deny | Pending | Pending | Pending | Pending | Pending |
| RA-05 Signoff | Pending | Pending | Pending | Pending | Pending |
| RA-06 UV refusal | Pending | Pending | Pending | Pending | Pending |
| RA-07 Origin and Host refusal | Pending | Pending | Pending | Pending | Pending |
| RA-08 Device unavailable and credential revoked | Pending | Pending | Pending | Pending | Pending |
| RA-09 Unaffected credential and cleanup | Pending | Pending | Pending | Pending | Pending |

Record pass, fail, blocked or inconclusive per case after observation. Name any browser-only refusal, missing device or evidence gap. CO-M4X-02 remains open until the device and host-boundary evidence has been reviewed.
