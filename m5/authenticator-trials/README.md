# Real approval authenticators (M5b-09, M5b-10, CO-M4X-02)

**Checklist prepared. Both hardware runs are pending.** D-045 M5-D13 requires owner-performed trials with a roaming FIDO2 key and a platform passkey using disposable state. Existing browser CI covers the production verifier with virtual authentication and simulated user verification. Real-device evidence stays open. [M5 plan](../../docs/implementation/M5-START-PLAN.md#6-task-breakdown-tests-first).

## Prepare the guest

1. Use a fresh, owner-designated Ubuntu 24.04 guest with a clean rollback point, separate guest users for the service, agent, job and human desktop, and a fresh browser profile under the human account. Use a distinct guest from live-host sessions and containment CI. Keep approval on the guest loopback only. Keep the workstation browser, personal profiles, platform accounts and their credentials outside the trial. [Reference provisioning](../reference-vm/PROVISIONING.md).
2. Record the reviewed Writ SHA, image digest, guest/hypervisor and exact browser version. Treat USB passthrough or a platform authenticator as an owner-operated prerequisite. Verify that the intended device is available to the human browser and that the agent cannot reach the desktop, its approval state or the authenticator interface, as required by P-AGENT-DESKTOP. Record the device class and version without serial numbers or personal metadata. No model credential is required for this checklist.
3. Follow the [reference setup and service commands](../reference-vm/README.md#commands). The approval listener is optional and requires `--approval-port`. The example below uses `8844` and the browser origin `http://localhost:8844`. Confirm the service runs with the production `PasskeyVerifier` and the socket is held by systemd. Keep one ledger owner.
4. Prepare a reviewed bootstrap and fixture procedure before enrolling a user. `writ-serviced --init` creates the ledger and snapshot store only. The serving listener requires an existing operator for `/enroll` and has no HTTP bootstrap route. The one-shot `BootstrapSession` must open the empty ledger under the ownership lock with the approved verifier, with the service stopped, and close before the service opens the ledger. The existing browser fixture creates and seeds its own test ledger with a virtual browser driver. It does not provide a reviewed operator workflow for bootstrapping this service ledger with a real key. The bootstrap workflow and controlled staging of pending approval and signoff records are open implementation gaps. [Enrollment ceremony](../../docs/implementation/M4-ENROLLMENT-CEREMONY.md#2-bootstrap-the-first-administrator) · [Service startup](../../service/src/lib.rs) · [Browser fixture](../../approval-server/README.md#end-to-end-browser-suite-approvalbrowser-tests).

Use synthetic subjects and tenant/project scopes. Stage separate pending request IDs for approve, deny, and each refusal, a promotion with a pending signoff, a second scope, and an unaffected operator credential for revocation. The service owns the ledger during the trial. Use an approved readback to verify outcomes and relevant event provenance. The production listener exposes scoped views, so a separate trusted readback is needed wherever the ledger outcome cannot be verified in the browser. A non-shipping test harness cannot substitute for the reviewed service/fixture path.

## Listener and host boundary

From within the designated guest, with the service running and the configured port set, check live addresses and status codes without cookies, response bodies or raw browser logs.

```bash
set -euo pipefail
port=8844
ss -ltn "sport = :$port"
origin="http://localhost:$port"
good_status=$(curl --noproxy '*' --max-time 5 --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --resolve "localhost:$port:127.0.0.1" "$origin/")
test "$good_status" = 200
wrong_host_status=$(curl --noproxy '*' --max-time 5 --silent --show-error --output /dev/null --write-out '%{http_code}' \
  "http://127.0.0.1:$port/")
test "$wrong_host_status" = 421
wrong_origin_status=$(curl --noproxy '*' --max-time 5 --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --resolve "localhost:$port:127.0.0.1" --header "Origin: http://localhost:8845" "$origin/")
test "$wrong_origin_status" = 403
printf 'expected origin %s, wrong Host %s, wrong Origin %s\n' "$good_status" "$wrong_host_status" "$wrong_origin_status"
```

- The sole listening address must be `127.0.0.1:<port>`. From a separate isolated machine or guest, confirm the Writ port is unreachable on the trial guest's non-loopback IP, with no port forwarding configured. Record which host or network control accounts for the observation.
- Stop the service and verify systemd retains the loopback socket, then restart and verify the address remains the same. Record the approved agent/desktop isolation observations from the full M5a checklist. A closed host boundary check alone does not establish device authentication. [M5a gate checklist](../../docs/implementation/M5A-GATE-REVIEW.md#4-verify-in-vm-checklist-all-open).

## Owner actions for each real device

Start from a fresh disposable profile and ledger for each device class. For mutations, compare the page's displayed subject, operation, scope and fingerprint with the staged case, complete a fresh user-verified assertion, and record both UI and trusted stored outcome.

| ID | Owner action | Evidence required |
|---|---|---|
| RA-01 | Enroll a new disposable subject through the operator's `/enroll` flow. Confirm registration start with the operator's key, create the new credential on the device under test, then confirm enrollment with the operator's key. | Both operator ceremonies and the subject's registration complete, and the active credential is scoped to the intended subject. |
| RA-02 | Sign in on `/` with the enrolled device and open the staged `/request/<id>`. Then use a clean browser context and a different-scope request to check refusal. | Read session can read only the allowed scope. Both refused reads show the generic not-found page and disclose no target context. |
| RA-03 | Approve the first disposable request. | Correct request is approved after fresh user verification. The read session alone leaves the request pending. |
| RA-04 | Deny a different disposable request. | Correct request is denied. No approval grant is issued for it. |
| RA-05 | Use a separately scoped signoff authority to sign the staged `/signoff/<id>`. Compare the displayed and stored context and effective expiry. | Stored signoff matches the displayed context and expiry, with a fresh assertion under the signoff authority. |
| RA-06 | Cancel or decline user verification on a fresh request. If an owner-approved physical authenticator can assert without UV, record that case too. | No approval or grant. Distinguish no assertion sent from observed verifier refusal of a non-UV assertion. Leave the verifier case open if the real browser or hardware cannot deliver it. |
| RA-07 | Check wrong HTTP Host and Origin as above. From an alternate disposable browser origin, confirm the approval operation cannot complete for the production RP configuration. | Refusal source is recorded. An origin rejected by the browser or HTTP guard leaves the separate WebAuthn verifier-origin case open unless an approved test actually reaches the verifier. |
| RA-08 | With a read session open, make the enrolled credential unavailable to the user. Disconnect the roaming key or make the disposable platform authenticator unavailable, then try a fresh ceremony. Revoke the credential separately through `/revoke` using an authorized revoker and fresh UV. | Physical removal or unavailability prevents the fresh assertion. After server-side revocation, the open read session and new login are refused and the credential's revocation is recorded. |
| RA-09 | Repeat the authorized read with an unaffected credential and record cleanup. | The independent credential remains functional. Guest profile, disposable credentials and trial disk are removed after evidence export. |

A device, software integration or UI that cannot complete a row leaves that row blocked or inconclusive. Record the boundary that prevented it and the remaining owner or engineering action. Keep real device evidence distinct from the existing virtual browser suite. Use the [evidence template](EVIDENCE.md) and add the sanitized record to TEST-EVIDENCE when the named owner has run it.
