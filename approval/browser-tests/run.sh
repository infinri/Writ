#!/usr/bin/env bash
# Build and run the approval browser suite locally (approval-server/README.md). Requires the pinned
# toolchain (m1/tools/env.sh), Google Chrome and Node 22. Loopback only; user verification is
# simulated by a CDP virtual authenticator.
#
# CI mode (WRIT_BROWSER_CI=1, the required step of .github/workflows/v2-docs.yml, D-044 item 2):
# the job has already installed the pinned Rust and SQLite (its environment is used as is, not
# m1/tools/env.sh); run.mjs then refuses `--only`, checks Node and Chrome against
# ci-manifest.json and fails unless every flow ran and passed.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
node="${NODE:-node}"
chrome="${CHROME:-/usr/bin/google-chrome}"
command -v "$node" >/dev/null 2>&1 || { echo "browser suite: FAIL Node not found ($node)" >&2; exit 1; }
"$node" --version >/dev/null || { echo "browser suite: FAIL Node does not run ($node)" >&2; exit 1; }
if [ "${WRIT_BROWSER_CI:-}" = 1 ]; then
  rustc --version | grep -q '^rustc 1.99.1 ' || { echo "browser suite: FAIL rustc is not the pinned 1.99.1: $(rustc --version)" >&2; exit 1; }
  [ -f "${SQLITE3_LIB_DIR:-/nonexistent}/libsqlite3.a" ] || { echo "browser suite: FAIL pinned SQLite (SQLITE3_LIB_DIR) not installed" >&2; exit 1; }
else
  # shellcheck disable=SC1091
  source m1/tools/env.sh
fi
target="${CARGO_TARGET_DIR:-$root/target}"
export CARGO_TARGET_DIR="$target"
cargo build --release --locked -p writ-approval-server --bin writ-approval-server --example browser_fixture
approval/browser-tests/check-release-binary.sh
exec m3/tools/run-with-clean-fds.sh "$node" approval/browser-tests/run.mjs \
  --server "$target/release/writ-approval-server" \
  --fixture "$target/release/examples/browser_fixture" \
  --chrome "$chrome" "$@"
