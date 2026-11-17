# SR-06 live-host preparation (M5b-07, M5b-08, M5-SR06)

**Draft. Live trials pending.** The plan's 69 priority experiments and its full acceptance matrix remain open. This checklist prepares the environment, records observed behavior and preserves evidence. [SR-06 trial plan](../../docs/audit/m0/SR-06-TRIAL-PLAN.md).

## Readiness

| Requirement | Needed for | State |
|---|---|---|
| Working VM tooling and an owner-designated disposable guest with rollback | M5a reference verification and later live-host trials | Blocked. Ordinary-user KVM access works. QEMU, `qemu-img` and `cloud-localds` are missing. |
| Isolated single-job containment runner | M5a-12 and M5a gate | Blocked separately on the runner host, provisioner and private GitHub control path. |
| Dedicated, revocable model credential through the approved procedure | Later M5b-08 live Claude Code sessions | Needed after guest preflight and adapter readiness. Keep the credential out of evidence. |
| Owner-operated roaming FIDO2 key and platform passkey | Later M5b-10 approval trials | Tracked separately in the real-authenticator checklist. |

[VM provisioning](../reference-vm/PROVISIONING.md) · [Containment CI](../containment-ci/README.md) · [Real authenticator trials](../authenticator-trials/README.md)

## Preparation checklist

1. Assign a named human operator and record the reviewed Writ SHA. Use a fresh disposable SR-06 guest and a clean rollback point. The owner designates the guest before trials. Keep the SR-06 guest separate from containment CI and any reference or authenticator guest that held keys or earlier trial state. The historical sandbox instructions describe disposable test data, Git and arm fixtures. Use their isolation requirements in the VM. [Historical sandbox setup](../../docs/audit/m0/SR-06-SANDBOX-SETUP.md).
2. Stage public source and approved fixtures while the guest has no model credential. Preserve the pinned V1 tree and original registration bytes for arm A. Keep separate arm directories and record each expected settings digest. Use synthetic content, a guest-local Git remote, and the documented unprivileged trial identities. V2 adapter observations use the dedicated agent UID, with the service, job and human identities separate.
3. Stage the candidate Claude Code version `2.1.318`, the version referenced by the revision 3 trial plan. Before installation, obtain the exact artifact and verification material through an official Anthropic distribution path and record the platform, URL and checksum. Availability and installation in the guest are pending. Freeze automatic updates for the trial under the approved host configuration. If the exact verified artifact is unavailable or the effective settings differ, stop preparation until the replacement version and revised trial plan are reviewed. [Official setup](https://code.claude.com/docs/en/setup) · [Official settings](https://code.claude.com/docs/en/settings).
4. Review the external egress policy before injecting any credential. Allow only the model endpoint and any explicitly approved essential dependencies, with private LAN, host management, metadata services and public Git endpoints unreachable. Apply the policy outside the guest. The provisioning boot with unrestricted QEMU user networking is for package staging only. A restricted reference boot has no model egress. The isolated, allow-listed SR-06 network path remains a provisioning gap.
5. Repeat PF-1 through PF-8 from the historical sandbox setup in the actual designated VM. Record the operator, timing, and observed status of each check. Confirm guest mounts against the hypervisor definition and confirm negative network checks against the egress policy. Review credential absence without dumping environment values or complete Git configuration. Repeat the checks after any change of VM, version, identity or network profile.
6. After M5a verification and M5b-03 integration and review, stage the reviewed V2 adapter for the same SHA. Current host contracts and portability fixtures do not provide the governed Claude Code adapter, its live hook/MCP wiring or a live-host trial harness. Resolve those gaps before claiming M5b-07 complete. Perform only syntax and planning checks until the designated guest and reviewed executable path exist.

Read-only identity and version checks for the trial user inside the prepared guest:

```bash
set -euo pipefail
expected_claude_version=2.1.318
test "$(id -u)" -ne 0
id
systemd-detect-virt --vm
test ! -S /var/run/docker.sock
test ! -S /run/containerd/containerd.sock
test ! -S /run/podman/podman.sock
findmnt -rno TARGET,SOURCE
actual_claude_version=$(claude --version)
printf '%s\n' "$actual_claude_version"
test "${actual_claude_version%% *}" = "$expected_claude_version"
node --version
python3 --version
jq --version
bash --version
```

Treat each failed or absent check as a stop. Confirm the exact effective settings and loaded arm before every session. The model credential is provisioned only after the non-credential preflight passes, by the owner through the approved secure procedure. The credential must be dedicated and revocable. Record its non-secret issuance reference and reachability outcome, with the credential value out of environment listings, shell history, prompts and evidence.

## Trial sequence and evidence

1. Use one arm per session and record the loaded hooks digest, settings digest and permission mode. Record hook invocation and tool/effect observation independently. Keep the baseline for both host and policy behavior. When instrumentation changes the behavior, treat the result as VOID and follow the plan's rerun rule.
2. Account for every case in section 6 of the [SR-06 trial plan](../../docs/audit/m0/SR-06-TRIAL-PLAN.md#6-priority-matrix) using the [evidence template](EVIDENCE.md). Preserve counts per group and any conditional checks. A permission prompt, OS isolation rule or absent tool attempt does not establish hook prevention. Record the control that actually blocked the effect.
3. Complete all section 9 criteria after the priority run, including deferred cases and host contract assumptions. Record uncovered effects in [UNMEDIATED-TOOLS](../../docs/audit/m0/UNMEDIATED-TOOLS.md) for the pinned version based on observations. Update the adapter requirement from observed hook semantics and keep advisory coverage accurately labelled.
4. Export only sanitized evidence and stop guest sessions. The owner revokes the trial credential. Destroy the guest and writable disks, verify removal on the hypervisor, and record the cleanup evidence. Use a new guest for another credentialed run. Record failures and incomplete cases before destruction.
5. The named operator records reviewed results under `docs/audit/m0/live-host/<run-id>/RESULTS.md` and in `docs/implementation/TEST-EVIDENCE.md`. Both the priority count and the complete acceptance matrix are required for the live-host gate. All experiments remain pending.
