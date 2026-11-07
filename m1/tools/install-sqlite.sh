#!/usr/bin/env bash
# D-023: build ONE pinned SQLite static library that both M1 prototypes link,
# so the Rust and Go SQLite ledgers run the identical engine with identical
# compile options (neither driver's bundled copy is used).
# Official source only (sqlite.org); SHA3-256 of the archive and of sqlite3.c
# pinned from sqlite.org/download.html and releaselog/3_54_1.html. User space.
#
# Usage: m1/tools/install-sqlite.sh [download-cache-dir]
# Then:  source m1/tools/env.sh   (exports the include and library paths)
set -euo pipefail

PREFIX="${WRIT_M1_TOOLCHAINS:-$HOME/.local/writ-m1-toolchains}"
CACHE="${1:-${XDG_CACHE_HOME:-$HOME/.cache}/writ-m1-downloads}"

SQLITE_VERSION=3.54.1           # released 2026-10-16
SQLITE_NUM=3540100
SQLITE_ZIP="sqlite-amalgamation-${SQLITE_NUM}.zip"
SQLITE_URL="https://sqlite.org/2026/${SQLITE_ZIP}"
SQLITE_ZIP_SHA3=92281c86e2957f9b9305890f9ac04204caa7b677cc868dc326d942689df28e30
SQLITE_C_SHA3=e2b869d3003b33c942b7dd7063604fbc53553e1b66609570df39b9b7b558ee81
SQLITE_SOURCE_ID="2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"
# Compile options shared by both prototypes (recorded by `proto sqlite-info`).
SQLITE_CFLAGS="-O2 -fPIC -DHAVE_USLEEP=1 -DSQLITE_THREADSAFE=1 -DSQLITE_DQS=0 -DSQLITE_OMIT_LOAD_EXTENSION -DSQLITE_ENABLE_COLUMN_METADATA"

BUILD_REV=2                     # bump when SQLITE_CFLAGS change (rev 2: HAVE_USLEEP, millisecond busy waits)
DEST="$PREFIX/sqlite-$SQLITE_VERSION-r$BUILD_REV"
[ ! -e "$DEST" ] || { echo "already installed: $DEST"; exit 0; }
command -v cc >/dev/null || { echo "a C compiler (cc) is required" >&2; exit 2; }

sha3() { python3 -c 'import hashlib,sys; print(hashlib.sha3_256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }
mkdir -p "$CACHE"
[ -s "$CACHE/$SQLITE_ZIP" ] || curl -fsSL --proto '=https' --tlsv1.2 "$SQLITE_URL" -o "$CACHE/$SQLITE_ZIP"
[ "$(sha3 "$CACHE/$SQLITE_ZIP")" = "$SQLITE_ZIP_SHA3" ] || { echo "SHA3-256 mismatch: $SQLITE_ZIP" >&2; exit 1; }
echo "SHA3-256 OK: $SQLITE_ZIP"

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
python3 -c 'import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$CACHE/$SQLITE_ZIP" "$WORK"
SRC="$WORK/sqlite-amalgamation-$SQLITE_NUM"
[ "$(sha3 "$SRC/sqlite3.c")" = "$SQLITE_C_SHA3" ] || { echo "SHA3-256 mismatch: sqlite3.c" >&2; exit 1; }
grep -qF "#define SQLITE_SOURCE_ID      \"$SQLITE_SOURCE_ID\"" "$SRC/sqlite3.h" || { echo "unexpected SQLITE_SOURCE_ID" >&2; exit 1; }
echo "SHA3-256 OK: sqlite3.c ($SQLITE_SOURCE_ID)"

# shellcheck disable=SC2086
cc $SQLITE_CFLAGS -c "$SRC/sqlite3.c" -o "$WORK/sqlite3.o"
mkdir -p "$DEST/include" "$DEST/lib"
ar rcs "$DEST/lib/libsqlite3.a" "$WORK/sqlite3.o"
cp "$SRC/sqlite3.h" "$SRC/sqlite3ext.h" "$DEST/include/"
printf '%s\n' "version=$SQLITE_VERSION" "source_id=$SQLITE_SOURCE_ID" "cflags=$SQLITE_CFLAGS" "cc=$(cc --version | head -1)" > "$DEST/BUILD-INFO"
echo "installed: $DEST (static libsqlite3.a)"
