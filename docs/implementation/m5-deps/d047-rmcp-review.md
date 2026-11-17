# D-047 MCP candidate refresh (M5b-02)

**2026-11-17, recovery base `482129d7a6b05a89451d576f94da31d4fe23f857`. Adoption blocked.** D-047 approves Apache-2.0 for `rmcp` 3.9.0 specifically. It leaves the exact dependency gate, notices, SBOM and advisory requirements in force.

## Package and source

The live crates.io sparse index reports 3.9.0 as published 2026-11-05 16:07:10 UTC and non-yanked. Its checksum matches the cached registry archive. The archive includes Apache-2.0 `LICENSE` and identifies upstream commit `5596a8a0bba2a1ec5519cc855cdadf23e4dcecdc`. [Registry index](https://index.crates.io/rm/cp/rmcp) · [Upstream source](https://github.com/modelcontextprotocol/rust-sdk/tree/5596a8a0bba2a1ec5519cc855cdadf23e4dcecdc/crates/rmcp)

- Candidate declaration is `rmcp = { version = "=3.9.0", default-features = false, features = ["server", "transport-io"] }`. The exact activated rmcp features were `schemars,server,transport-async-rw,transport-io,uuid`. The server feature also enables the compile-time `pastey` macro internally. The candidate excludes `rmcp-macros`, child-process, HTTP, OAuth, SSE and client features.
- `build.rs` checks for both `.githooks` and `.git` two levels above the manifest. Only that branch executes `git config core.hooksPath .githooks` there. A normal registry cache meets neither condition. Build only the reviewed crates.io artifact using the established registry layout. The plain `stdio()` uses Tokio's blocking handles. The separate Linux `stdio_pipes()` implementation reopens descriptors through `/proc/self/fd` using `OpenOptions` with `O_NONBLOCK` and checks that they are FIFOs. The `fcntl` in that source file is a test helper.
- A scratch addition to the exact workspace lock built with Rust 1.99.1 and no production call sites. Its normal/build closure was 61 registry packages on the x86_64 Linux host and 85 across all targets. Every archive checksum matched its exact lock entry. The lock retained existing exact versions and added the four registry packages below. Review the newly built and dev-to-runtime edges as well as the new lock entries. The Linux delta identified in the earlier review is in [task2c-rmcp390-ws-review-linux.txt](task2c-rmcp390-ws-review-linux.txt).

| New registry package | License | SHA-256 |
| --- | --- | --- |
| rmcp 3.9.0 | Apache-2.0 | `d24a7e92f4bcaa36b97fa93c4fa7ca8f3e669eced949345b3462dba333c7f909` |
| pastey 0.2.3 | MIT OR Apache-2.0 | `2ee67f1008b1ba2321834326597b8e186293b049a023cdef258527550b9935b4` |
| schemars_derive 1.2.3 | MIT | `e1c3a92094fa7d61aa124645844facb6b554dfc797136d0f5fd1f890e2bffc69` |
| serde_derive_internals 0.30.0 | MIT OR Apache-2.0 | `f852137cce035d6a4df67ccce505ff6b3e9fd3a10e3e52b24dc71e650bb1a9bd` |

## Current advisory results

OSV batch queries for the exact 85 packages returned one hit, **Chrono 0.4.45, RUSTSEC-2026-0543 (informational, unmaintained)**. The advisory lists no fixed version. Chrono is a direct rmcp dependency with `now` and `serde` and is reached again through `schemars/chrono04`. Both edges activate in the candidate. Hold adoption for an approved dependency disposition or reviewed replacement. [OSV advisory](https://api.osv.dev/v1/vulns/RUSTSEC-2026-0543)

3.9.0 is the first version RustSec lists as patched for RUSTSEC-2026-0462. The affected Streamable HTTP client features are excluded from this server-only candidate. For adoption before 2026-11-19 16:07:10 UTC, document the D-033 security-fix age exception for selecting 3.9.0 over the affected 3.8.0 release. The exception covers release age and leaves the independent Chrono disposition open. [RustSec advisory](https://rustsec.org/advisories/RUSTSEC-2026-0462)

## Protocol and closure gates

Use the SDK stdio transport in the agent-side shim only. Set the returned server config protocol and the sole supported protocol version to `ProtocolVersion::V_2025_11_25`. A stand-alone probe built with the candidate features negotiated 2025-11-25 for an exact request and offered only 2025-11-25 for initialize requests proposing 2024-11-05 and 2026-07-28. Direct stateless `tools/list` and `server/discover` requests declaring 2026-07-28 returned unsupported version with the one pinned revision. Before any initialize exchange, the same stateless methods declaring 2025-11-25 succeeded. The SDK also documents that it may serve regular requests before receiving the initialized notification after a completed initialize response. **A version allow-list alone therefore does not pin the 2025-11-25 handshake.** Before an effect shim can be integrated, enforce the legacy lifecycle using the SDK’s typed transport or handlers and test that stateless opening messages, tool requests before initialization completes and unsupported versions cannot forward effects. Use the SDK for framing and negotiation. Treat transport identity and lifecycle flags as protocol data. Writ socket admission and broker approval checks govern effects. [Pinned protocol lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)

To reproduce in a scratch copy of the recovery commit, add a temporary workspace binary depending only on the declaration above and `tokio = { version = "=1.54.1", default-features = false, features = ["rt", "io-std", "time"] }`. Resolve from the existing lock, build with the pinned toolchain and `--locked`, inspect `cargo tree --locked -p <candidate> -e normal,build` with both the Linux target and `--target all`, and inspect the inverse feature tree with `cargo tree --locked -p <candidate> -e features -i chrono`. Compare each package's registry archive with its lock checksum and rerun OSV on the selected versions.

Before adoption, review the actual final graph and feature unification, add per-member allow-list constraints and notices, generate the SBOM, and run the existing dependency gate. Assert that kernel, broker and service closures contain neither MCP packages nor model SDKs. The scratch graph and negotiation results are candidate evidence.
