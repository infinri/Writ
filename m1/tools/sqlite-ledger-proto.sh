#!/bin/sh
# Run the prototype named by WRIT_PROTO with the SQLite ledger (D-023): every
# broker-scenario process gets a fresh database directory under
# ${WRIT_LEDGER_TMP:-${TMPDIR:-/tmp}}. Other commands pass through unchanged.
# Used as --proto for m1/driver/parity.py and --rust/--go for m1/props/run.py.
: "${WRIT_PROTO:?set WRIT_PROTO to the prototype binary}"
if [ "$1" = broker-scenario ]; then
  exec "$WRIT_PROTO" "$@" --ledger sqlite --ledger-dir "$(mktemp -d -p "${WRIT_LEDGER_TMP:-${TMPDIR:-/tmp}}" writ-ledger-XXXXXX)"
fi
exec "$WRIT_PROTO" "$@"
