# SR-06 sandbox readiness record, 2026-11-06

**Status: SANDBOX PREPARED / LIVE VERIFICATION NOT EXECUTED. Sandbox torn down 2026-11-06.** None of the 69 priority experiments in [SR-06-TRIAL-PLAN](../../SR-06-TRIAL-PLAN.md) has run. No model credential was provisioned. Preflight success is evidence about this container only; it is not a production security certification and not SR-06 verification.

Operator: Claude (Claude Code session), supervised by the project owner. Sanitization before publication: `evidence/preflight.txt` had host snapshot paths, the host disk device name and the LAN gateway address redacted (placeholders in angle brackets); nothing else was altered. Integrity: `SHA256SUMS` (computed after sanitization) covers every file under `evidence/` and `sandbox-build/`; `V1-TAR.sha256` records the V1 archive (not copied; regenerate with `git archive ebdafe5450b201339f81e717ed104e6bcfd3fe3e`).

## Environment

| Item | Value |
|---|---|
| Platform | Container fallback (no hypervisor installed on the workstation; installing one needs host sudo) |
| Docker | 29.1.2, **rootful and shared** with unrelated projects on the same daemon; AppArmor and seccomp enabled |
| Claude Code | 2.1.318, npm `@anthropic-ai/claude-code`, registry integrity verified by npm; `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` |
| Sandbox image | `writ-sr06-sandbox:2.1.318` `sha256:c7ab87d34c916d56441f7fc1b4bf2c8d034ea5ad1d4b40c36dabff8892d9ae43`, base `node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d` |
| Proxy image | `writ-sr06-proxy:1` `sha256:660b3bbf749b835285fcff6b1c721a38172d092a575bcc431619ccfcb4092c88` (tinyproxy, allowlist `^api\.anthropic\.com$`, CONNECT 443 only) |
| Neo4j | `neo4j@sha256:42ef3ebfd671ebd01fdebb6dcbe80cf7b45eef2aa3d346ef4132f7ef31e67d2f`, data and logs on tmpfs, random throwaway password, no published ports |
| Networks | `writ-sr06-int` 10.231.61.0/24 internal (sandbox .10, proxy .2, Neo4j .3); `writ-sr06-egress` 10.231.62.0/24 (proxy only) |
| Sandbox container | user `tester` uid 1500, no sudo, all capabilities dropped, `no-new-privileges`, no mounts, pids limit 1024, memory 6 GiB |

## Verified facts

| Check | Result | Evidence |
|---|---|---|
| PF-1..PF-8 | PASS (all eight) | `evidence/preflight.txt` |
| V1 `hooks/hooks.json` sha256 | `bbbdcf1f...c2040d1`, equals baseline manifest | build log; `evidence/preflight.txt` context |
| Arm C2 | 14 PreToolUse registrations carry `onFailure: "block"`; 0 elsewhere; 0 in the original; all 1,138 other files byte-identical | `evidence/c2-identity.txt`, `evidence/c2-hooks.diff`, independent in-image recheck |
| V1 daemon | corpus import 1,737 statements; `/health` healthy, 288 rules, 32 mandatory | session output (not separately logged) |
| V1 packaging | imports `PyYAML` and `tokenizers` without declaring them; installed into the arm venvs at build time, V1 unchanged | `evidence/writ-v1-freeze.txt` |
| Hook observability | `--debug=hooks --debug-file` logged `Hooks: SessionStart:startup [/opt/probes/probe.sh OBS-0 allow] finished with status 0 (90ms)` | `evidence/readiness/obs-debug.log`, `markers.log` |
| Plugin validation | `claude plugin validate` passes for `/opt/writ-v1` and `/opt/writ-v1-onfailure` | session output |
| `--bare` | skips all hooks; must never be used in trials | `claude --help` |

## Limitations (must stay attached to any use of this record)

1. Shared rootful Docker daemon and shared kernel: a container escape is root on the host. Not a VM.
2. Outbound CONNECT attempts from the sandbox to `mobile.events.data.microsoft.com:443` were refused by the proxy. **Original note (superseded by the investigation below):** two attempts, coinciding with Claude Code CLI invocations, originating process not identified. **Investigation (same day, before any credential):** 16 attempts in total; none caused by any Claude Code command; they stop when `writ serve` (the V1 daemon) is stopped and resume about 10 s after it restarts; the hostname occurs only in the `onnxruntime` 1.31.1 native library the daemon loads. Most likely ONNX Runtime telemetry from the V1 daemon; not proven at packet level; not assumed malicious; all blocked. A controlled restart with `ORT_DISABLE_TELEMETRY=1` produced 0 requests in a 10-minute window (previously the first request came within about 10 s), consistent with ONNX Runtime telemetry; single run, no packet capture. Evidence: `evidence/telemetry-attribution.txt`, `evidence/proxy-log-full.txt`.
3. Only a SessionStart hook with exit 0 was observed; the debug format for non-zero exits, timeouts and invalid output is unobserved.
4. No V1 hook has run in this sandbox; no host enforcement behavior has been observed.
5. No `auditd`; host debug output is the only independent hook-execution evidence.

## Owner position on adversarial experiments (2026-11-06)

The owner does not authorize the command-injection (P4) and adversarial bypass (P5) experiments, nor approval-provenance probes (P3), on this workstation's shared rootful Docker daemon, which also runs other valuable projects. Those require a disposable VM or a separate test machine. Whether the probe-only host-contract experiments (P0, P1, P1-NC, P2) and V1 fault-posture runs (P6) may use this container is an open owner decision.

## Corrections derived during preparation (plan revision, not historical evidence)

Source: official hooks documentation, <https://code.claude.com/docs/en/hooks>, read 2026-11-06.

- Default command-hook timeout is **600 s** on PreToolUse (30 s on UserPromptSubmit), not the 60 s assumed by the static hook re-audit (contract C4). Trial H-4 must exceed 600 s.
- Output counts as unparseable JSON only if it starts with `{` and ends with `}`; plain text is not a failure. The H-6 probe now prints `{not json}` (earlier draft printed `{not json`, which the host could treat as plain text).
- `onFailure` (`"continue"` default, `"block"`) applies to command and HTTP hooks from v2.1.295; covers spawn failure, exit codes other than 0 or 2, timeout and invalid or schema-invalid output; has no effect on Stop and SubagentStop hooks.

Earlier test evidence (E-001..E-012) is unaffected by these corrections.

## Teardown (executed 2026-11-06, owner-authorized)

Targets only objects carrying label `sr06=1` or the exact names and IDs below. Nothing else on the shared daemon is touched.

| Object | Name | ID prefix |
|---|---|---|
| container | `writ-sr06-sandbox` | `f588ea021a5d` |
| container | `writ-sr06-neo4j` | `9b8b870f32f9` |
| container | `writ-sr06-proxy` | `a03de03eb471` |
| network | `writ-sr06-int` | `69a5d34157a0` |
| network | `writ-sr06-egress` | `2bd7c1a96eea` |
| image (optional) | `writ-sr06-sandbox:2.1.318`, `writ-sr06-proxy:1` | `c7ab87d34c91`, `660b3bbf749b` |

Procedure:

1. Confirm the evidence above is preserved and `SHA256SUMS` verifies (`sha256sum -c SHA256SUMS`).
2. List targets and confirm exactly three containers and two networks: `docker ps -a --filter label=sr06=1 --format '{{.ID}} {{.Names}}'` and `docker network ls --filter label=sr06=1 --format '{{.ID}} {{.Name}}'`.
3. Remove containers by explicit name with anonymous volumes: `docker rm -f -v writ-sr06-sandbox writ-sr06-neo4j writ-sr06-proxy` (Neo4j data is tmpfs and disappears with the container).
4. Remove networks by explicit name: `docker network rm writ-sr06-int writ-sr06-egress`.
5. Optionally remove the two SR-06 images by tag. Do **not** run `docker system prune`, `docker network prune`, `docker volume prune` or image prunes; they would affect unrelated projects.
6. Verify absence: both filtered listings in step 2 return nothing; `ip -4 route | grep 10.231.6` returns nothing.
7. Credential revocation: none required (none was provisioned). If one is provisioned later, revoke it before or during teardown and record the time.
8. Record teardown time and verification output here.

**Executed 2026-11-06 about 22:53Z** after the telemetry diagnostic and hash regeneration: `docker rm -f -v writ-sr06-sandbox writ-sr06-neo4j writ-sr06-proxy`; `docker network rm writ-sr06-int writ-sr06-egress`. Verification: label `sr06=1` lists 0 containers and 0 networks; each of the five names absent; no routes in 10.231.6x. Unrelated objects: containers 46 to 43 and networks 23 to 21 (exactly the SR-06 objects), volumes 530 unchanged, images 76 unchanged. No prune command run. The two SR-06 images remain (removal not requested). No credential existed, so none was revoked.
