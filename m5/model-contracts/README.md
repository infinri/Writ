# Model adapter contract fixtures (M5b-05)

This dependency-free Rust draft exercises model selection and inert proposals with local text and hosted structured-tool fixtures. The fixture runner has separate external-host and managed entry points. Its output cannot carry a decision, grant, permit or receipt. The managed path checks the caller's residency and capability requirements before passing the prompt to each adapter, including fallbacks. Synthetic code, research and review tasks require no network, tools, credentials or provider SDK.

Run the fixture tests from the Writ root with the pinned Rust toolchain.

```bash
source m1/tools/env.sh
test_dir=$(mktemp -d)
rustc --edition=2021 -D warnings --test m5/model-contracts/lib.rs -o "$test_dir/writ-model-contract-tests"
"$test_dir/writ-model-contract-tests"
```

The fixtures cover separate provider capabilities, unavailable providers, forbidden fallback and external-host operation without a provider (INV-22, INV-26). Keep the production contracts and providers in separate agent-side crates after lead review. Production provider eligibility must come from trusted configuration, tenant policy and approved egress before a prompt is shared. These fixtures use only locally constructed adapters and reject proposal shapes outside the requested capability. The production INV-21 policy-evaluation outage test and production dependency closure check remain open. [M5 start plan](../../docs/implementation/M5-START-PLAN.md)
