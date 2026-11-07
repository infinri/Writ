//go:build !libsqlite3 || !sqlite_omit_load_extension

package broker

// The SQLite ledger must link the pinned shared SQLite (D-023), never the
// driver's bundled copy: build with -tags "libsqlite3 sqlite_omit_load_extension".
var _ = buildRequiresTags_libsqlite3_sqlite_omit_load_extension
