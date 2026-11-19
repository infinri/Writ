# MCP initialization gate candidate (M5b-02)

The dependency-free lifecycle gate permits protocol dispatch after the selected version is 2025-11-25 and the SDK delivers the initialized notification. Create one gate for each connection, owned by that connection's handler. Enforce initialization and tool admission in SDK receive order. In rmcp 3.9.0, request and notification callbacks run independently. A handler mutex protects shared state but cannot enforce arrival order. Close it on disconnect, shutdown or protocol failure. Duplicate initialization and initialized notification close it. Each effect handler must check readiness before forwarding to the authenticated Writ socket. Writ's broker still supplies effect authorization.

The rmcp 3.9.0 scratch experiment attached this type to a dedicated handler using a mutex, an overridden typed initialize handler, on_initialized notification handler and list_tools method. The SDK parsed the wire messages and negotiated the version. The guarded list_tools refused requests after initialize but before the notification, accepted the completed handshake, and refused a stateless request carrying the pinned version. This is candidate evidence for a future adapter. The [development MCP shim](../mcp-shim/README.md) is the subsequent SDK integration. This directory retains the earlier dependency-free contract fixture.

Apply the check to every future handler that can expose tools or forward effects. A tools/list probe alone cannot verify every dispatch route. Prevent service handler cloning from sharing lifecycle state across connections. Review cancellation, delayed callbacks, disconnect and concurrent request ordering in the concrete adapter, then repeat the wire tests with tools/call and broker dispatch counters. Keep pre-initialization ping and logging behavior under SDK control. [MCP lifecycle specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)

Run the local state tests with the pinned toolchain.

```bash
source m1/tools/env.sh
test_dir=$(mktemp -d)
rustc --edition 2021 -D warnings --test m5/mcp-contracts/lib.rs -o "$test_dir/mcp-contract-tests"
"$test_dir/mcp-contract-tests"
```

The integrated graph compiles Chrono directly and through schemars. D-049 permits the reviewed built graph for development only. D-048 covers the original unbuilt lock entry. The Apache-2.0 approval, security-fix age exception, checksum, notices, SBOM and advisory gates continue to apply. [Candidate dependency review](../../docs/implementation/m5-deps/d047-rmcp-review.md)
