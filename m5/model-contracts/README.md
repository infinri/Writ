# Model adapter contract fixtures (M5b-05)

The dependency-free Rust contract defines the agent-side runner, a local text stub and a hosted structured-tool stub. The external-host path takes no model binding. The managed path uses a locally configured tenant and project policy. Before any adapter receives a prompt, the runner checks the configured provider ID, residency, capability and exact egress endpoint. It repeats the same checks at each fallback and refuses missing or ambiguous policy bindings and duplicate provider IDs. A hosted binding requires an approved endpoint. The model request carries only task, prompt and capability.

Synthetic coding, research and review tasks produce text or tool proposals. Proposals carry no authorization decisions, grants, permits or receipts. The tests cover incompatible output and providers that lack eligibility, including forbidden endpoint and region. The boundary test calls the actual kernel with existing golden fixtures while the model is unavailable and after it offers a tool proposal. Allow, deny, approval-required and unavailable decisions remain identical. The test uses no model SDK or network. [M5 start plan](../../docs/implementation/M5-START-PLAN.md)

Run the fixtures from the Writ root with the pinned Rust toolchain.

```bash
source m1/tools/env.sh
test_dir=$(mktemp -d)
rustc --edition=2021 -D warnings --test m5/model-contracts/lib.rs -o "$test_dir/model-tests"
"$test_dir/model-tests"
cargo build --locked -p writ-kernel --target-dir "$test_dir/target"
rustc --edition=2021 -D warnings --crate-name writ_model_contracts --crate-type rlib \
  m5/model-contracts/lib.rs -o "$test_dir/libwrit_model_contracts.rlib"
rustc --edition=2021 -D warnings --test m5/model-contracts/kernel_boundary.rs \
  -L dependency="$test_dir/target/debug/deps" \
  --extern writ_kernel="$test_dir/target/debug/libwrit_kernel.rlib" \
  --extern writ_model_contracts="$test_dir/libwrit_model_contracts.rlib" \
  -o "$test_dir/kernel-boundary-tests"
"$test_dir/kernel-boundary-tests"
```

Production adoption still requires separate agent-side crates, trusted provider configuration, transport enforcement of the actual destination including redirects, and a closure scan for kernel, broker and service. This fixture does not wire a managed model or broker effect path.
