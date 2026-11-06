# SR-06 disposable sandbox setup (revision 2)

**Status: instructions only; nothing has been built or run.** Applies to every experiment in [SR-06-TRIAL-PLAN](SR-06-TRIAL-PLAN.md). Commands marked *illustrative* must be replaced by the exact commands actually used, recorded verbatim.

## 1. Platform

**Preferred: a dedicated virtual machine**, created for one run and destroyed after. A VM gives kernel process auditing (`auditd`) for non-invasive evidence that hooks ran, can run a disposable Neo4j natively if V1's daemon needs one, and needs no container runtime inside it.

**Acceptable fallback: a single rootless container** with the controls in section 2. Without `auditd` the plan relies on host debug output and transcripts (trial plan section 4). Docker-in-Docker and any container-runtime socket are forbidden; if V1 needs Neo4j it runs as a native process inside the same container or the VM path is used.

## 2. Controls and their verification

Each control is verified by a pre-flight check (section 4) whose output goes into the run record. A failed check stops the run.

| Control | Requirement | Pre-flight check |
|---|---|---|
| No host source mounts | No bind mounts of any host directory, including the Writ repository and the operator's HOME. V1 enters as a tar copied in; evidence leaves by copy-out (`docker cp` or VM file copy) after the run | PF-1 |
| No runtime socket | No Docker, containerd or Podman socket inside the sandbox; no `--privileged`; container capabilities dropped (`--cap-drop ALL`, `--security-opt no-new-privileges`) | PF-2 |
| Dedicated non-root user | User `tester`, HOME `/home/tester`; no sudo, no passwordless privilege escalation | PF-3 |
| No production credentials | Only a dedicated, short-lived model API credential with a spend cap, injected as an environment variable when the session starts, never written to disk in the image. No SSH keys, git credential helpers, cloud credentials, tokens or `.env` files with real values | PF-4 |
| No real remotes | Workspace remote is `/home/tester/remote.git` only | PF-5 |
| Network restricted and verified | Egress allowed only to the model API endpoint (allowlisting proxy or firewall) plus loopback. Dependencies are installed during image build, **before** the restriction and before any credential exists | PF-6 |
| Dummy data only | `ws` contains generated dummy files; secrets content is `FAKE` | PF-7 |
| Probes contained | Injection and code-execution payloads only create markers under `/home/tester/ws/`; no network | review of each payload before the run |
| Credential revocation | Revoke the API credential at run end; record revocation time and credential id (not the secret) | teardown record |
| Complete teardown | Destroy the VM or container and its disks or volumes; verify it no longer exists | teardown record |

## 3. Build steps (network open, no credential present)

1. **Base image.** Minimal Linux; record distribution and kernel. Install bash, git, python3, jq, curl, `auditd` (VM), a small HTTP server for `127.0.0.1:18080`, and the Node runtime the pinned Claude Code needs. Record versions.
2. **Claude Code, pinned.** Install one exact version through the official installer or package (*illustrative*: `npm install -g @anthropic-ai/claude-code@<version>`). Disable automatic updates if the version offers a setting. Record `claude --version`; re-check at run end. A change voids the run.
3. **V1 source.** On the workstation: `git -C <writ-repo> archive ebdafe5450b201339f81e717ed104e6bcfd3fe3e -o writ-v1-ebdafe5.tar`. Copy only the tar in; extract to `/opt/writ-v1`; verify `sha256sum /opt/writ-v1/hooks/hooks.json` equals `evidence_sha256["hooks/hooks.json"]` in [v1-baseline-manifest.json](v1-baseline-manifest.json); `chmod -R a-w /opt/writ-v1`.
4. **Arm C2 copy.** `cp -r /opt/writ-v1 /opt/writ-v1-onfailure`; add `"onFailure": "block"` to the 14 PreToolUse registrations only; verify every script file is byte-identical to `/opt/writ-v1` (`diff -r` lists only `hooks/hooks.json`); record that diff and both sha256 values.
5. **Probe hooks (arms B, C1).** Scripts in `/opt/probes/` that append `{ts, exp, tool_name, agent_id, session_id}` to `$RUN/markers.log` and then perform the experiment's behavior. Probes never source V1 code.
6. **V1 runtime for arms A and C2.** Install V1's daemon and Python dependencies from `/opt/writ-v1` into a venv under `/home/tester/.writ-venv` (record each command), plus a disposable Neo4j only if the daemon requires it. Never point at a non-disposable graph.
7. **Workspace.** `/home/tester/ws`: `git init`; dummy `src/app.py`, `src/app2.py`, `src/app3.py` (test-less), `seed`, `secrets/FAKE` (content `FAKE`); `git init --bare /home/tester/remote.git` as `origin`.
8. **Local services.** HTTP probe server on `127.0.0.1:18080` logging to `$RUN/http.log`; local test MCP server (one marker tool, one filesystem-write tool) logging to `$RUN/mcp.log`; for E5, a listener on the Writ daemon port that accepts and never replies; `/etc/hosts` entry `127.0.0.1 probe.sandbox.test`. These run outside the Claude Code process environment so E1/E2 do not affect them.
9. **Evidence capture.** Enable host debug or verbose output for every session and keep transcript JSONL. In a VM, add `auditd` rules for `execve` of `/usr/bin/bash` and of files under `/opt/writ-v1*/hooks/scripts/` and `/opt/probes/`. Record whether host debug output reports hook execution and exit codes; that decides the evidence path in trial plan section 4.
10. **Permissions.** `settings.json` allows exactly the tools each session needs, with no deny or ask rules; record the permission mode. Exactly one hook configuration (arm A, B, C1 or C2) active per session; user and project settings otherwise empty.
11. **Lock down.** Apply the network restriction. Only now inject the model credential for the session.

## 4. Pre-flight checks (record output; any failure stops the run)

| Check | Command (*illustrative*) | Pass condition |
|---|---|---|
| PF-1 mounts | `findmnt -rno TARGET,SOURCE` inside; container inspect `Mounts` outside | no host directories mounted |
| PF-2 socket and privilege | `ls -l /var/run/docker.sock /run/containerd/containerd.sock 2>&1`; `capsh --print` | sockets absent; no extra capabilities |
| PF-3 user | `id`; `sudo -n true` | uid not 0; sudo fails |
| PF-4 credentials | `ls -a ~ ~/.ssh ~/.aws ~/.config 2>&1`; `git config --list --show-origin`; `env` with values masked | no keys, helpers or tokens beyond the one masked model credential variable |
| PF-5 remotes | `git -C /home/tester/ws remote -v` | only `/home/tester/remote.git` |
| PF-6 network | `curl -sS -m5 https://example.com`; `curl -sS -m5 https://github.com`; model API reachability per the host's own check | first two fail; model API reachable |
| PF-7 data | `cat /home/tester/ws/secrets/FAKE` | prints `FAKE` |
| PF-8 version | `claude --version` | matches the pinned version |

## 5. Run record

Run id; operator (named human); date and timezone; platform (VM or container) and image; `claude --version` at start and end; Node, Python, jq, bash versions; arm per session; hooks file sha256 per arm; `settings.json`; permission mode; network allowlist; pre-flight outputs PF-1..PF-8; credential id and revocation time; teardown time and verification.

## 6. Teardown

Copy evidence out as plain files (no executable configuration, no hooks). Stop all sessions. Revoke the credential. Destroy the VM or container and its disks and volumes. Verify absence (VM list or `docker ps -a` and `docker volume ls`). Record times.
