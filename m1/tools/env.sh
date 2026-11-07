# Source this file to use the pinned M1 toolchains in the current shell only.
_writ_m1_prefix="${WRIT_M1_TOOLCHAINS:-$HOME/.local/writ-m1-toolchains}"
export PATH="$_writ_m1_prefix/rust-1.99.1/bin:$_writ_m1_prefix/go1.27.3/bin:$PATH"
export CARGO_HOME="$_writ_m1_prefix/cargo-home"
export GOPATH="$_writ_m1_prefix/gopath"
export GOTOOLCHAIN=local
unset _writ_m1_prefix
