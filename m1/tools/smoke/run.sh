#!/usr/bin/env bash
# Compile and run the M1-00 smoke tests with the pinned toolchains. Outputs go to a temp dir.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/../env.sh"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
rustc -O --edition 2021 "$here/smoke.rs" -o "$out/smoke-rs" && "$out/smoke-rs"
cargo new -q --vcs none "$out/cargo-smoke" && (cd "$out/cargo-smoke" && cargo build -q --release --offline && ./target/release/cargo-smoke)
(cd "$out" && cp "$here/smoke.go" . && go mod init smoke >/dev/null 2>&1 && go vet ./... && go build -o smoke-go . && ./smoke-go)
echo "smoke: all passed"
