# Source this file to use the pinned M1 toolchains in the current shell only.
_writ_m1_prefix="${WRIT_M1_TOOLCHAINS:-$HOME/.local/writ-m1-toolchains}"
export PATH="$_writ_m1_prefix/rust-1.99.1/bin:$_writ_m1_prefix/go1.27.3/bin:$PATH"
export CARGO_HOME="$_writ_m1_prefix/cargo-home"
export GOPATH="$_writ_m1_prefix/gopath"
export GOTOOLCHAIN=local
# D-023: the shared pinned SQLite (m1/tools/install-sqlite.sh), linked statically by both prototypes.
export SQLITE3_INCLUDE_DIR="$_writ_m1_prefix/sqlite-3.54.1-r2/include"
export SQLITE3_LIB_DIR="$_writ_m1_prefix/sqlite-3.54.1-r2/lib"
export SQLITE3_STATIC=1
export PKG_CONFIG_LIBDIR="$SQLITE3_LIB_DIR/pkgconfig"
export CGO_ENABLED=1
export CGO_CFLAGS="-I$SQLITE3_INCLUDE_DIR"
export CGO_LDFLAGS="-L$SQLITE3_LIB_DIR -lsqlite3 -lm"
unset _writ_m1_prefix
