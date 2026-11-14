#!/usr/bin/env bash
# Reproduces the WV-1 dependency evidence. Run from the repository root.
set -u
P="${WRIT_M1_TOOLCHAINS:-$HOME/.local/writ-m1-toolchains}"
export PATH="$P/rust-1.99.1/bin:$PATH"
export CARGO_HOME="$P/cargo-home"
export SQLITE3_INCLUDE_DIR="$P/sqlite-3.54.1-r2/include"
export SQLITE3_LIB_DIR="$P/sqlite-3.54.1-r2/lib"
export SQLITE3_STATIC=1
export PKG_CONFIG_LIBDIR="$SQLITE3_LIB_DIR/pkgconfig"
E=docs/implementation/m4-deps
mkdir -p "$E/tree"
{
  cargo --version; rustc --version
  echo "== cargo build --locked"; cargo build --locked 2>&1 | tail -3; echo "exit ${PIPESTATUS[0]}"
  echo "== cargo build --release --locked"; cargo build --release --locked 2>&1 | tail -3; echo "exit ${PIPESTATUS[0]}"
  echo "== cargo test -p writ-approval --locked"; cargo test -p writ-approval --locked 2>&1 | grep -E "test result|Running|error|warning: unused" ; echo "exit ${PIPESTATUS[0]}"
  echo "== cargo test --workspace --locked --no-run"; cargo test --workspace --locked --no-run 2>&1 | tail -2; echo "exit ${PIPESTATUS[0]}"
} > "$E/build-test.txt" 2>&1
T=(cargo tree --locked --target all)
for c in writ-kernel writ-broker writ-ledger-sqlite writ-approval; do
  "${T[@]}" -p "$c" -e normal,build > "$E/tree/$c.normal-build.txt" 2>&1
  "${T[@]}" -p "$c" -e normal,build,dev > "$E/tree/$c.test.txt" 2>&1
done
ALL=(-p writ-kernel -p writ-broker -p writ-ledger-sqlite -p writ-approval)
"${T[@]}" "${ALL[@]}" -e normal,build > "$E/tree/combined.normal-build.txt" 2>&1
"${T[@]}" "${ALL[@]}" -e normal,build,dev > "$E/tree/combined.test.txt" 2>&1
"${T[@]}" "${ALL[@]}" -e normal,build -e features > "$E/tree/combined.release-features.txt" 2>&1
"${T[@]}" "${ALL[@]}" -e normal,build,dev -e features > "$E/tree/combined.test-features.txt" 2>&1
{
  for k in openssl openssl-sys native-tls openssl-src aws-lc-rs aws-lc-sys ring rustls tokio rsa; do
    echo "== cargo tree -e normal,build,dev -i $k (workspace)"
    "${T[@]}" --workspace -e normal,build,dev -i "$k" 2>&1 | head -40
  done
  echo "== every *-sys package in the combined test graph"
  "${T[@]}" --workspace -e normal,build,dev --prefix none --format '{p}' | sed 's/ (\*)//' | sort -u | grep -E -- '-sys ' || echo none
  echo "== packages with a links key (native libraries) in Cargo metadata"
  cargo metadata --locked --format-version 1 | python3 -I "$E/links.py"
} > "$E/native-and-tls.txt" 2>&1
bash "$E/rsa-attempt.sh" > "$E/rsa-attempt.txt" 2>&1
python3 kernel/tools/check_deps.py >"$E/check-deps.txt" 2>&1; echo "exit $?" >> "$E/check-deps.txt"
W=$(mktemp -d)
git show 4c07327:Cargo.lock > "$W/base.lock"
cargo metadata --locked --format-version 1 > "$W/meta.json"
python3 -I "$E/inventory.py" "$W/base.lock" "$W/meta.json" "$E"
