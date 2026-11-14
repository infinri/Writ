#!/usr/bin/env bash
# Release-build contents of writ-approval-server (Y3, D-043): no software passkey test
# authenticator in the normal/build dependency graph, and no test symbols in the release
# binary (positive control: the production PasskeyVerifier is present). Run from the
# repository root after `cargo build --release --locked -p writ-approval-server --bin writ-approval-server`.
set -euo pipefail
bin="${CARGO_TARGET_DIR:-target}/release/writ-approval-server"
[ -x "$bin" ] || { echo "check-release-binary: FAIL missing $bin" >&2; exit 1; }
tree=$(cargo tree --locked -p writ-approval-server -e normal,build --prefix none --format "{p}")
if grep -E '^webauthn-authenticator-rs ' <<<"$tree" >/dev/null; then
  echo "check-release-binary: FAIL test authenticator crate in the normal/build graph" >&2; exit 1
fi
syms=$(nm -C "$bin" 2>/dev/null || true)
[ -n "$syms" ] || { echo "check-release-binary: FAIL no symbols to inspect" >&2; exit 1; }
grep -q 'PasskeyVerifier' <<<"$syms" || { echo "check-release-binary: FAIL positive control PasskeyVerifier absent" >&2; exit 1; }
if grep -E 'SoftPasskey|softpasskey|webauthn_authenticator_rs|TestVerifier|m4_support|_for_tests|uncontained' <<<"$syms"; then
  echo "check-release-binary: FAIL test symbols in $bin" >&2; exit 1
fi
echo "check-release-binary: PASS (no test authenticator crate or test symbol; PasskeyVerifier present)"
