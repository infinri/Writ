#!/usr/bin/env bash
# M1-00: install the pinned Rust and Go toolchains for the M1 bake-off into user space.
# Official sources only; SHA-256 pinned here; GPG signatures verified against pinned
# fingerprints in a throwaway keyring. No sudo, no shell rc changes, no global config.
#
# Usage: m1/tools/install-toolchains.sh [download-cache-dir]
# Then:  source m1/tools/env.sh
set -euo pipefail

PREFIX="${WRIT_M1_TOOLCHAINS:-$HOME/.local/writ-m1-toolchains}"
CACHE="${1:-${XDG_CACHE_HOME:-$HOME/.cache}/writ-m1-downloads}"

RUST_VERSION=1.99.1
RUST_ARCHIVE="rust-${RUST_VERSION}-x86_64-unknown-linux-gnu.tar.xz"
RUST_URL="https://static.rust-lang.org/dist/${RUST_ARCHIVE}"
RUST_SHA256=231fcae47ee950621374725b5fc3b5441d2fbda2955ac45dd7dc816d8876cfe7
RUST_KEY_URL="https://static.rust-lang.org/rust-key.gpg.ascii"
RUST_KEY_FPR=108F66205EAEB0AAA8DD5E1C85AB96E6FA1BE5FE

GO_VERSION=1.27.3
GO_ARCHIVE="go${GO_VERSION}.linux-amd64.tar.gz"
GO_URL="https://dl.google.com/go/${GO_ARCHIVE}"
GO_SHA256=65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40
GO_KEY_URL="https://dl.google.com/linux/linux_signing_key.pub"
GO_KEY_FPR=EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796

[ "$(uname -s)-$(uname -m)" = "Linux-x86_64" ] || { echo "only Linux x86_64 is pinned" >&2; exit 2; }

mkdir -p "$CACHE"
fetch() { [ -s "$CACHE/$2" ] || curl -fsSL --proto '=https' --tlsv1.2 "$1" -o "$CACHE/$2"; }
fetch "$RUST_URL" "$RUST_ARCHIVE"; fetch "$RUST_URL.asc" "$RUST_ARCHIVE.asc"; fetch "$RUST_KEY_URL" rust-key.asc
fetch "$GO_URL" "$GO_ARCHIVE";     fetch "$GO_URL.asc" "$GO_ARCHIVE.asc";     fetch "$GO_KEY_URL" google-key.pub

echo "$RUST_SHA256  $CACHE/$RUST_ARCHIVE" | sha256sum -c -
echo "$GO_SHA256  $CACHE/$GO_ARCHIVE" | sha256sum -c -

GNUPGHOME="$(mktemp -d)"; export GNUPGHOME; trap 'rm -rf "$GNUPGHOME"' EXIT
gpg -q --import "$CACHE/rust-key.asc" "$CACHE/google-key.pub" 2>/dev/null
verify() {  # archive signature expected-primary-fingerprint
  local out; out="$(gpg --status-fd 1 --verify "$2" "$1" 2>/dev/null)"
  grep -q "^\[GNUPG:\] VALIDSIG .* $3\$" <<<"$out" || { echo "signature check failed for $1" >&2; exit 1; }
  echo "signature OK: $(basename "$1") primary key $3"
}
verify "$CACHE/$RUST_ARCHIVE" "$CACHE/$RUST_ARCHIVE.asc" "$RUST_KEY_FPR"
verify "$CACHE/$GO_ARCHIVE" "$CACHE/$GO_ARCHIVE.asc" "$GO_KEY_FPR"

RUST_DIR="$PREFIX/rust-$RUST_VERSION"; GO_DIR="$PREFIX/go$GO_VERSION"
for d in "$RUST_DIR" "$GO_DIR"; do [ ! -e "$d" ] || { echo "refusing to overwrite existing $d" >&2; exit 1; }; done
mkdir -p "$PREFIX"
WORK="$(mktemp -d)"; trap 'rm -rf "$GNUPGHOME" "$WORK"' EXIT
tar -xJf "$CACHE/$RUST_ARCHIVE" -C "$WORK"
"$WORK/rust-${RUST_VERSION}-x86_64-unknown-linux-gnu/install.sh" --prefix="$RUST_DIR" --disable-ldconfig \
  --components=rustc,cargo,rust-std-x86_64-unknown-linux-gnu,clippy-preview,rustfmt-preview >/dev/null
mkdir -p "$WORK/go" && tar -xzf "$CACHE/$GO_ARCHIVE" -C "$WORK/go" && mv "$WORK/go/go" "$GO_DIR"
echo "installed: $RUST_DIR and $GO_DIR"
