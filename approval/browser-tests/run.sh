#!/usr/bin/env bash
# Build and run the approval browser suite locally (approval-server/README.md). Requires the pinned
# toolchain (m1/tools/env.sh), Google Chrome and Node 22. Loopback only; user verification is
# simulated by a CDP virtual authenticator.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
# shellcheck disable=SC1091
source m1/tools/env.sh
target="${CARGO_TARGET_DIR:-$root/target}"
export CARGO_TARGET_DIR="$target"
cargo build --release --locked -p writ-approval-server --bin writ-approval-server --example browser_fixture
approval/browser-tests/check-release-binary.sh
exec m3/tools/run-with-clean-fds.sh "${NODE:-node}" approval/browser-tests/run.mjs \
  --server "$target/release/writ-approval-server" \
  --fixture "$target/release/examples/browser_fixture" \
  --chrome "${CHROME:-/usr/bin/google-chrome}" "$@"
