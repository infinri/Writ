# M6 offline synthetic policy compiler

Run from the repository root with the pinned Rust toolchain.

```sh
cargo run --locked --manifest-path m6/compiler/Cargo.toml -- m6/compiler/fixtures/deny.input.json
cargo test --locked --manifest-path m6/compiler/Cargo.toml
```

The executable takes one input file or `-` for stdin. It prints one D-006 canonical JSON report with a trailing newline. Status 0 means the candidate passed offline proof, 1 means the report refused it, and 2 means a usage or I/O error. A successful report includes the exact loadable bundle, its digest, overlaps and their witness resources, and all fixture Decisions. A refusal provides sorted diagnostics.

The input object contains only `manifests` and `companions`. Manifests follow the [closed PolicyManifest](../../docs/contracts/policy-manifest.schema.json). This slice accepts authorization rules in the `m6.synthetic.` namespace with `deny` posture, one exact tenant ID, and the sole action `synthetic.inspect`. Publisher fields must be absent or null. Each manifest has one companion with exactly these fields.

| Field | Accepted value |
| --- | --- |
| `entry_version` | `m6-authz-entry/1` |
| `policy_id`, `version` | Exact manifest identity pair |
| `effect` | `forbid`, `require_approval` or `permit` |
| `reason_code` | A kernel policy reason code |
| `resource_prefix` | Canonical `synthetic:<tenant>/<project>/<path>` within that manifest's tenant |

Statement IDs use the first 128 bits of the D-006 digest of the exact identity pair. Collisions refuse compilation. For the supported selectors, equal tenant and nested or equal byte prefixes establish intersection. The longer canonical prefix is an executable witness resource. The existing kernel compares raw bytes, so a prefix can match part of a path segment. Priority remains report metadata and contributes to exact manifest evidence binding.

The built-in enforcer registry resolves `m6-kernel-inspect/1` to the pure kernel decision function at a fixed fixture time and revision. The three checked-in evidence pins cover the permit-only, permit-plus-approval and permit-plus-approval-plus-deny candidates. Each pin binds canonical manifests, bundle digest, the fingerprint of the enforcer and kernel sources plus locked build inputs, and the complete fixture requests and expected Decisions. Changes to any of these inputs require fresh reviewed evidence. The standalone compiler lock uses the kernel's existing reviewed registry packages and leaves the root workspace resolution intact.

Every report sets `deployed_enforcement` to `UNVERIFIED` and `runtime_authority` to false. This is the [D-050](../../docs/implementation/DECISION-LOG.md#d-050-m6-slice-1-offline-synthetic-authorization-compiler-owner-2026-11-19) offline slice.

D-060 adds a fourth pin for `optional-inspect.input.json` and classifies only `m6.synthetic.optional-inspect` as removable in that pinned profile. Its exact removal yields the existing `deny.input.json` and its existing proof. The scope case moves from the optional permit to default deny, the hard-deny and approval cases retain their winners, and every Decision reflects the new bundle digest. Run `cargo test --locked --manifest-path m6/compiler/Cargo.toml exact_optional_removal` to verify complete before and after Decisions and stale evidence refusal.
