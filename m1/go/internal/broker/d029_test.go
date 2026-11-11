package broker

import (
	"database/sql"
	"fmt"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"writ.dev/m1/proto/internal/sjson"
)

// D-029 (LEDGER-SQLITE.md "Broker coordination gate and state conflicts"),
// Go unit tests: the coordination gate, STATE_CONFLICT diagnostics, and the
// decision B acceptance rule for the 64-way contention tests.

var busyBegin = map[string]any{"code": "busy", "at": "begin"}

// --- Decision B: verified category 3 aborts only -------------------------

// txErrors returns the transactions of a worker's evidence that carry a
// database error.
func txErrors(w map[string]any) map[string]any {
	errs := map[string]any{}
	for name, v := range w["tx"].(map[string]any) {
		if e := v.(map[string]any)["error"]; e != nil {
			errs[name] = e
		}
	}
	return errs
}

// auditFailed reports whether the worker's refusal audit failed busy at
// begin (its refusal is then not counted in final.refusals).
func auditFailed(w map[string]any) bool {
	a, ok := w["tx"].(map[string]any)["audit"].(map[string]any)
	return ok && reflect.DeepEqual(a["error"], busyBegin)
}

// verifiedAbort accepts one execute's LEDGER_UNAVAILABLE only as a verified
// category 3 abort (D-026, D-029 B): its first ledger transaction, reserve,
// failed busy at begin and rolled back; no other transaction ran except the
// refusal audit (itself without error or busy at begin); for an
// operation-unique key, no receipt, receipt event or grant-use row exists;
// and no consumed permit or effect exists that a dispatch does not account
// for (an aborted operation dispatches nothing).
func verifiedAbort(t *testing.T, w map[string]any, path, tenant, key string, uniqueKey bool) error {
	t.Helper()
	tx := w["tx"].(map[string]any)
	r, ok := tx["reserve"].(map[string]any)
	if !ok || !reflect.DeepEqual(r["error"], busyBegin) || r["outcome"] != "rollback" {
		return fmt.Errorf("first transaction is not reserve busy at begin: %v", tx)
	}
	for name, v := range tx {
		if name == "reserve" {
			continue
		}
		if name != "audit" {
			return fmt.Errorf("transaction %s after the abort", name)
		}
		if e := v.(map[string]any)["error"]; e != nil && !reflect.DeepEqual(e, busyBegin) {
			return fmt.Errorf("audit error %v", e)
		}
	}
	if uniqueKey {
		for _, q := range []string{
			`SELECT count(*) FROM receipts WHERE tenant_id = '%s' AND idempotency_key = '%s'`,
			`SELECT count(*) FROM receipt_events WHERE tenant_id = '%s' AND idempotency_key = '%s'`,
			`SELECT count(*) FROM grant_uses WHERE tenant_id = '%s' AND idempotency_key = '%s'`,
		} {
			if n := count(t, path, fmt.Sprintf(q, tenant, key)); n != 0 {
				return fmt.Errorf("aborted operation left rows: %s = %d", q, n)
			}
		}
	}
	dispatches := count(t, path, `SELECT count(*) FROM receipt_events WHERE cause = 'dispatch'`)
	consumed := count(t, path, `SELECT count(*) FROM adapter_consumed_permits`)
	effects := count(t, path, `SELECT count(*) FROM adapter_effects`)
	if consumed > dispatches || effects > consumed {
		return fmt.Errorf("unattributed effect: %d dispatches, %d consumed permits, %d effects", dispatches, consumed, effects)
	}
	return nil
}

// contentionGroup runs one timed concurrent group and returns its outcomes
// and its workers' evidence.
func contentionGroup(t *testing.T, b *Broker, ops []any, step int) (map[string]any, []map[string]any) {
	t.Helper()
	op := map[string]any{"op": "concurrent", "operations": ops}
	tm := newOpTiming(op)
	tm.setDispatch(time.Now())
	res := b.applyOn(b.led, tm, op, step)
	var ws []map[string]any
	for _, w := range tm.json()["operations"].([]any) {
		ws = append(ws, w.(map[string]any))
	}
	return res["outcomes"].(map[string]any), ws
}

// ledgerTx is the order of an execute's ledger transactions (LEDGER-SQLITE.md
// "Logical transactions").
var ledgerTx = []string{"reserve", "dispatch", "adapter", "observe"}

// committed reports whether the worker's transaction name committed.
func committed(w map[string]any, name string) bool {
	x, ok := w["tx"].(map[string]any)[name].(map[string]any)
	return ok && x["outcome"] == "commit"
}

// verifiedPartial accepts one execute's LEDGER_UNAVAILABLE as a D-026
// category 4 failure after durable progress: exactly one ledger transaction,
// dispatch, adapter or observe, failed busy at begin; every earlier one ran
// and committed; nothing else ran except the refusal audit (without error or
// busy at begin). For an operation-unique key, its rows must be the crash
// state the committed transactions imply: dispatch failed: receipt reserved,
// one reserve event, its grant use kept; adapter or observe failed: receipt
// dispatched (unknown_reconcile once a later reconcile ran), one reserve and
// one dispatch event, its grant use kept; never an observe event. Consumed
// permits and effects are checked group-wide by checkAttribution.
func verifiedPartial(t *testing.T, w map[string]any, path, tenant, key string, uniqueKey bool) (stage string, err error) {
	t.Helper()
	tx := w["tx"].(map[string]any)
	errs := txErrors(w)
	if e := errs["audit"]; e != nil && !reflect.DeepEqual(e, busyBegin) {
		return "", fmt.Errorf("audit error %v", e)
	}
	delete(errs, "audit")
	if len(errs) != 1 {
		return "", fmt.Errorf("failing transactions %v", errs)
	}
	for name, e := range errs {
		stage = name
		if !reflect.DeepEqual(e, busyBegin) {
			return "", fmt.Errorf("%s error %v", name, e)
		}
	}
	i := slices.Index(ledgerTx, stage)
	if i < 1 {
		return "", fmt.Errorf("%s is not a later ledger transaction", stage)
	}
	for _, name := range ledgerTx[:i] {
		if !committed(w, name) {
			return "", fmt.Errorf("%s did not commit before %s failed", name, stage)
		}
	}
	for name := range tx {
		if j := slices.Index(ledgerTx, name); name != "audit" && (j < 0 || j > i) {
			return "", fmt.Errorf("transaction %s after %s failed", name, stage)
		}
	}
	if r := tx[stage].(map[string]any); r["outcome"] != "rollback" {
		return "", fmt.Errorf("%s outcome %v", stage, r["outcome"])
	}
	if !uniqueKey {
		return stage, nil
	}
	where := fmt.Sprintf(` WHERE tenant_id = '%s' AND idempotency_key = '%s'`, tenant, key)
	status, grantLabel := receiptRow(t, path, where)
	wantStatus, dispatchEvents := map[string]bool{"reserved": true}, int64(0)
	if stage != "dispatch" {
		wantStatus, dispatchEvents = map[string]bool{"dispatched": true, "unknown_reconcile": true}, 1
	}
	if !wantStatus[status] {
		return "", fmt.Errorf("%s failed but the receipt is %q", stage, status)
	}
	for q, want := range map[string]int64{
		`SELECT count(*) FROM receipt_events` + where + ` AND cause = 'reserve'`:  1,
		`SELECT count(*) FROM receipt_events` + where + ` AND cause = 'dispatch'`: dispatchEvents,
		`SELECT count(*) FROM receipt_events` + where + ` AND cause = 'observe'`:  0,
		`SELECT count(*) FROM grant_uses` + where:                                 map[bool]int64{true: 1, false: 0}[grantLabel],
	} {
		if n := count(t, path, q); n != want {
			return "", fmt.Errorf("%s failed: %s = %d, want %d", stage, q, n, want)
		}
	}
	return stage, nil
}

// receiptRow is the status of the receipt matching where ("" if none) and
// whether it names a grant.
func receiptRow(t *testing.T, path, where string) (status string, grant bool) {
	t.Helper()
	db, err := sql.Open("sqlite3", "file:"+path+"?mode=ro")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var label sql.NullString
	err = db.QueryRow(`SELECT status, grant_label FROM receipts`+where).Scan(&status, &label)
	if err == sql.ErrNoRows {
		return "", false
	}
	if err != nil {
		t.Fatal(err)
	}
	return status, label.Valid
}

// checkAttribution is the group-wide accounting of the serial model
// (no effect_fails in these groups): every dispatch event, consumed permit
// and effect belongs to a committed transaction of some worker; no
// duplicate dispatch.
func checkAttribution(t *testing.T, ws []map[string]any, path string) error {
	t.Helper()
	var dispatches, adapters int64
	for _, w := range ws {
		if committed(w, "dispatch") {
			dispatches++
		}
		if committed(w, "adapter") {
			adapters++
		}
	}
	for q, want := range map[string]int64{
		`SELECT count(*) FROM receipt_events WHERE cause = 'dispatch'`: dispatches,
		`SELECT count(*) FROM adapter_consumed_permits`:                adapters,
		`SELECT count(*) FROM adapter_effects`:                         adapters,
	} {
		if n := count(t, path, q); n != want {
			return fmt.Errorf("%s = %d, but %d committed transactions account for it", q, n, want)
		}
	}
	return nil
}

// accepted classifies every worker with a database error: only a verified
// category 3 abort or a verified category 4 failure after durable progress
// is accepted; anything else fails the test.
type accepted struct {
	aborts, partials, audits int64
	stages                   map[string]int64
}

func acceptFailures(t *testing.T, ws []map[string]any, ops []any, path string, uniqueKeys bool) accepted {
	t.Helper()
	acc := accepted{stages: map[string]int64{}}
	for i, w := range ws {
		if auditFailed(w) {
			acc.audits++
		}
		errs := txErrors(w)
		if e := errs["audit"]; e != nil && !reflect.DeepEqual(e, busyBegin) {
			t.Fatalf("worker %d: audit error %v", i, e)
		}
		delete(errs, "audit")
		if len(errs) == 0 {
			continue
		}
		req := ops[i].(map[string]any)["request"].(map[string]any)
		tenant, key := req["tenant_id"].(string), req["idempotency_key"].(string)
		if errs["reserve"] != nil {
			if err := verifiedAbort(t, w, path, tenant, key, uniqueKeys); err != nil {
				t.Fatalf("worker %d: LEDGER_UNAVAILABLE is not a verified abort: %v", i, err)
			}
			acc.aborts++
			continue
		}
		stage, err := verifiedPartial(t, w, path, tenant, key, uniqueKeys)
		if err != nil {
			t.Fatalf("worker %d: LEDGER_UNAVAILABLE is not a verified failure after progress: %v", i, err)
		}
		acc.partials++
		acc.stages[stage]++
	}
	if err := checkAttribution(t, ws, path); err != nil {
		t.Fatal(err)
	}
	return acc
}

// expectStatus is the receipt status the workers' committed transactions
// imply for one key: observed after an observe, dispatched after a dispatch
// (a failed adapter or observe leaves it uncertain), otherwise reserved.
func expectStatus(ws []map[string]any) string {
	status := "reserved"
	for _, w := range ws {
		switch {
		case committed(w, "observe"):
			return "observed_success"
		case committed(w, "dispatch"):
			status = "dispatched"
		}
	}
	return status
}

func countCommitted(ws []map[string]any, name string) int64 {
	n := int64(0)
	for _, w := range ws {
		if committed(w, name) {
			n++
		}
	}
	return n
}

func outcomesOf(pairs ...any) map[string]any {
	m := map[string]any{}
	for i := 0; i < len(pairs); i += 2 {
		if n := pairs[i+1].(int64); n != 0 {
			m[pairs[i].(string)] = n
		}
	}
	return m
}

func outcome(m map[string]any, k string) int64 {
	n, _ := m[k].(int64)
	return n
}

// checkSameKey checks one group of executes on a single idempotency key
// against the serial model with accepted failures (D-026, D-029 B).
func checkSameKey(t *testing.T, got map[string]any, ws []map[string]any, ops []any, path, what string) accepted {
	t.Helper()
	acc := acceptFailures(t, ws, ops, path, false)
	done, unavailable := countCommitted(ws, "observe"), acc.aborts+acc.partials
	status := expectStatus(ws)
	replayed, rr := outcome(got, "replayed"), outcome(got, "RECONCILIATION_REQUIRED")
	if want := outcomesOf("effect_completed", done, "replayed", replayed, "RECONCILIATION_REQUIRED", rr, "LEDGER_UNAVAILABLE", unavailable); !reflect.DeepEqual(got, want) ||
		done > 1 || countCommitted(ws, "dispatch") > 1 || done+replayed+rr+unavailable != int64(len(ws)) ||
		status == "observed_success" && rr != 0 || status != "observed_success" && replayed != 0 {
		t.Fatalf("%s: outcomes %v (receipt %s, %d failures after progress %v)", what, got, status, acc.partials, acc.stages)
	}
	if s, _ := receiptRow(t, path, ""); s != status {
		t.Fatalf("%s: receipt %q, want %q", what, s, status)
	}
	for q, want := range map[string]int64{
		`SELECT count(*) FROM receipts`:                               1,
		`SELECT count(*) FROM receipt_events WHERE cause = 'reserve'`: 1,
		`SELECT value FROM counters WHERE name = 'next_permit'`:       countCommitted(ws, "dispatch"),
		`SELECT count(*) FROM audit_refusals`:                         unavailable + rr - acc.audits,
	} {
		if n := count(t, path, q); n != want {
			t.Fatalf("%s: %s = %d, want %d", what, q, n, want)
		}
	}
	return acc
}

// A real failure after durable progress, forced by an outside write lock
// held across one worker's dispatch, adapter or observe BEGIN (5 s busy
// timeout), is accepted and modelled: dispatch failed leaves the receipt
// reserved and the next operation resumes it; adapter or observe failed
// leaves it dispatched and the next operation answers
// RECONCILIATION_REQUIRED, with the effect only after a committed adapter.
func TestSQLiteContentionFailureAfterProgress(t *testing.T) {
	f := loadRace(t)
	for i, stage := range []string{"dispatch", "adapter", "observe"} {
		path := filepath.Join(t.TempDir(), "key.db")
		b := sqlBroker(t, f.initial, &SQLite{DB: path})
		ops := []any{withKey(t, f.initial, f.allow, "same-key", ""), withKey(t, f.initial, f.allow, "same-key", "")}
		var mu sync.Mutex
		victim, begins := -1, map[int]int{}
		var release func()
		b.trace = func(event string, worker int) {
			if event != "begin" {
				return
			}
			mu.Lock()
			defer mu.Unlock()
			if victim < 0 {
				victim = worker // the first to begin holds the key lock: the reserver
			}
			if worker != victim {
				return
			}
			begins[worker]++
			switch begins[worker] {
			case i + 2: // the stage's BEGIN: lock the file from outside
				release = holdLock(t, path)
			case i + 3: // the audit of the refusal: unlock
				release()
			}
		}
		got, ws := contentionGroup(t, b, ops, 0)
		acc := checkSameKey(t, got, ws, ops, path, stage)
		if acc.partials != 1 || acc.stages[stage] != 1 || outcome(got, "LEDGER_UNAVAILABLE") != 1 {
			t.Fatalf("%s: %v, accepted %+v", stage, got, acc)
		}
		want := map[string]map[string]any{
			"dispatch": {"effect_completed": int64(1), "LEDGER_UNAVAILABLE": int64(1)},
			"adapter":  {"RECONCILIATION_REQUIRED": int64(1), "LEDGER_UNAVAILABLE": int64(1)},
			"observe":  {"RECONCILIATION_REQUIRED": int64(1), "LEDGER_UNAVAILABLE": int64(1)},
		}[stage]
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("%s: outcomes %v, want %v", stage, got, want)
		}
	}
}

// 4. Contention (D-029 B): 64 goroutines, one connection each, busy timeout
// 5 s. A LEDGER_UNAVAILABLE is accepted only as a verified category 3 abort
// (removed as a no-op) or a verified category 4 failure after durable
// progress (modelled as the crash state its committed transactions imply);
// both are reported as availability. Every invariant of that serial model
// still holds: exactly one reservation and one grant use, no duplicate
// dispatch, at most one consumed permit and effect, the receipt state its
// history implies, and replay or RECONCILIATION_REQUIRED as that state
// dictates.
func TestSQLiteContention(t *testing.T) {
	const workers, reps = 64, 20
	f := loadRace(t)
	var aborts, partials int64
	for rep := 0; rep < reps; rep++ {
		// Distinct keys racing for one single-use grant.
		path := filepath.Join(t.TempDir(), "grant.db")
		b := sqlBroker(t, f.initial, &SQLite{DB: path})
		rtWant(t, b.apply(f.approve, 0), `{"result":"grant_issued"}`)
		ops := make([]any, workers)
		for i := range ops {
			ops[i] = withKey(t, f.initial, f.approval, fmt.Sprintf("race-%d", i), "")
		}
		got, ws := contentionGroup(t, b, ops, 1)
		acc := acceptFailures(t, ws, ops, path, true)
		aborts, partials = aborts+acc.aborts, partials+acc.partials
		done := countCommitted(ws, "observe")
		unavailable := acc.aborts + acc.partials
		if want := outcomesOf("effect_completed", done, "GRANT_EXHAUSTED", workers-done-unavailable, "LEDGER_UNAVAILABLE", unavailable); !reflect.DeepEqual(got, want) || done > 1 || acc.partials > 1 {
			t.Fatalf("rep %d grant race: outcomes %v, want %v (%d failures after progress)", rep, got, want, acc.partials)
		}
		if status, _ := receiptRow(t, path, ""); status != expectStatus(ws) {
			t.Fatalf("rep %d grant race: receipt %q, want %q", rep, status, expectStatus(ws))
		}
		for q, want := range map[string]int64{
			`SELECT count(*) FROM receipts`:                               1,
			`SELECT count(*) FROM receipt_events WHERE cause = 'reserve'`: 1,
			`SELECT count(*) FROM grant_uses`:                             1,
			`SELECT uses FROM grants WHERE label = 'g1'`:                  1,
			`SELECT count(*) FROM audit_refusals`:                         workers - done - acc.audits,
		} {
			if n := count(t, path, q); n != want {
				t.Fatalf("rep %d grant race: %s = %d, want %d", rep, q, n, want)
			}
		}

		// One idempotency key: the key lock orders the operations; no
		// duplicate dispatch. After an observed receipt later operations
		// replay; after a dispatch whose effect is uncertain they answer
		// RECONCILIATION_REQUIRED; a receipt left reserved is resumed.
		path = filepath.Join(t.TempDir(), "key.db")
		b = sqlBroker(t, f.initial, &SQLite{DB: path})
		for i := range ops {
			ops[i] = withKey(t, f.initial, f.allow, "same-key", "")
		}
		got, ws = contentionGroup(t, b, ops, 0)
		acc = checkSameKey(t, got, ws, ops, path, fmt.Sprintf("rep %d same key", rep))
		aborts, partials = aborts+acc.aborts, partials+acc.partials
	}
	t.Logf("availability: %d verified lock-contention aborts and %d failures after durable progress in %d operations",
		aborts, partials, 2*reps*workers)
}

// The acceptance rule cannot hide an unauthorized effect, a committed
// receipt, a grant use or a non-busy error.
func TestVerifiedAbortRejectsFabricatedEvidence(t *testing.T) {
	abort := func(reserveErr any, extra map[string]any) map[string]any {
		tx := map[string]any{
			"reserve": map[string]any{"begin_ns": int64(5), "body_ns": int64(0), "end_ns": int64(0), "outcome": "rollback", "error": reserveErr},
			"audit":   map[string]any{"begin_ns": int64(1), "body_ns": int64(1), "end_ns": int64(1), "outcome": "commit", "error": nil},
		}
		for k, v := range extra {
			tx[k] = v
		}
		return map[string]any{"op": "execute", "tx": tx}
	}
	fresh := func() string {
		path := filepath.Join(t.TempDir(), "l.db")
		sqlRT(t, path)
		return path
	}
	// Genuine evidence on a clean ledger is accepted.
	if err := verifiedAbort(t, abort(busyBegin, nil), fresh(), "ten", "k", true); err != nil {
		t.Fatalf("genuine abort rejected: %v", err)
	}
	committed := map[string]any{"begin_ns": int64(1), "body_ns": int64(1), "end_ns": int64(1), "outcome": "commit", "error": nil}
	for name, c := range map[string]struct {
		w     map[string]any
		setup string
	}{
		"unauthorized effect": {abort(busyBegin, nil),
			`INSERT INTO adapter_consumed_permits (permit_id) VALUES ('permit-9'); INSERT INTO adapter_effects (permit_id) VALUES ('permit-9')`},
		"effect without consumed permit": {abort(busyBegin, nil),
			`INSERT INTO adapter_consumed_permits (permit_id) VALUES ('permit-1');
			 INSERT INTO receipts VALUES ('ten', 'other', 'fp', NULL, 'dispatched');
			 INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES ('ten', 'other', 'dispatched', 'dispatch');
			 INSERT INTO adapter_effects (permit_id) VALUES ('permit-1'); INSERT INTO adapter_effects (permit_id) VALUES ('permit-1')`},
		"committed receipt": {abort(busyBegin, nil),
			`INSERT INTO receipts VALUES ('ten', 'k', 'fp', NULL, 'reserved')`},
		"receipt event": {abort(busyBegin, nil),
			`INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES ('ten', 'k', 'reserved', 'reserve')`},
		"grant use": {abort(busyBegin, nil),
			`INSERT INTO grants VALUES ('g1', '{}', 0, 0, 1, 1); INSERT INTO grant_uses (label, tenant_id, idempotency_key) VALUES ('g1', 'ten', 'k')`},
		"non-busy error":         {abort(map[string]any{"code": "other", "at": "begin"}, nil), ""},
		"busy in the body":       {abort(map[string]any{"code": "busy", "at": "body"}, nil), ""},
		"no error at all":        {abort(nil, nil), ""},
		"later transaction":      {abort(busyBegin, map[string]any{"dispatch": committed}), ""},
		"audit with other error": {abort(busyBegin, map[string]any{"audit": map[string]any{"outcome": "rollback", "error": map[string]any{"code": "other", "at": "body"}}}), ""},
	} {
		path := fresh()
		if c.setup != "" {
			execOn(t, path, c.setup)
		}
		if err := verifiedAbort(t, c.w, path, "ten", "k", true); err == nil {
			t.Errorf("%s: fabricated evidence accepted", name)
		}
	}
}

// The category 4 rule rejects evidence inconsistent with the database rows,
// inconsistent evidence, and effects no committed transaction accounts for.
func TestVerifiedPartialRejectsInconsistentEvidence(t *testing.T) {
	ok := func() map[string]any {
		return map[string]any{"begin_ns": int64(1), "body_ns": int64(1), "end_ns": int64(1), "outcome": "commit", "error": nil}
	}
	failed := func(e any) map[string]any {
		return map[string]any{"begin_ns": int64(5), "body_ns": int64(0), "end_ns": int64(0), "outcome": "rollback", "error": e}
	}
	partial := func(stage string, e any) map[string]any {
		tx := map[string]any{"audit": ok()}
		for _, name := range ledgerTx[:slices.Index(ledgerTx, stage)] {
			tx[name] = ok()
		}
		tx[stage] = failed(e)
		return map[string]any{"op": "execute", "tx": tx}
	}
	edit := func(w map[string]any, f func(tx map[string]any)) map[string]any {
		f(w["tx"].(map[string]any))
		return w
	}
	const (
		reserved   = `INSERT INTO receipts VALUES ('ten', 'k', 'fp', NULL, 'reserved'); INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES ('ten', 'k', 'reserved', 'reserve');`
		dispatched = `INSERT INTO receipts VALUES ('ten', 'k', 'fp', NULL, 'dispatched'); INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES ('ten', 'k', 'reserved', 'reserve'), ('ten', 'k', 'dispatched', 'dispatch');`
		granted    = `INSERT INTO grants VALUES ('g1', '{}', 0, 0, 1, 1); INSERT INTO receipts VALUES ('ten', 'k', 'fp', 'g1', 'reserved'); INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES ('ten', 'k', 'reserved', 'reserve');`
	)
	fresh := func(setup string) string {
		path := filepath.Join(t.TempDir(), "l.db")
		sqlRT(t, path)
		if setup != "" {
			execOn(t, path, setup)
		}
		return path
	}
	// Consistent evidence and rows are accepted for each stage.
	for stage, setup := range map[string]string{"dispatch": reserved, "adapter": dispatched, "observe": dispatched,
		"dispatch ": granted + `INSERT INTO grant_uses (label, tenant_id, idempotency_key) VALUES ('g1', 'ten', 'k')`} {
		if _, err := verifiedPartial(t, partial(strings.TrimSpace(stage), busyBegin), fresh(setup), "ten", "k", true); err != nil {
			t.Fatalf("%s: consistent evidence rejected: %v", stage, err)
		}
	}
	for name, c := range map[string]struct {
		w     map[string]any
		setup string
	}{
		"dispatch failed, receipt dispatched":   {partial("dispatch", busyBegin), dispatched},
		"adapter failed, receipt reserved":      {partial("adapter", busyBegin), reserved},
		"observe failed, receipt observed":      {partial("observe", busyBegin), dispatched + `UPDATE receipts SET status = 'observed_success'`},
		"observe failed, observe event":         {partial("observe", busyBegin), dispatched + `INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES ('ten', 'k', 'observed_success', 'observe')`},
		"dispatch failed, no receipt":           {partial("dispatch", busyBegin), ""},
		"dispatch failed, grant use missing":    {partial("dispatch", busyBegin), granted},
		"dispatch failed, extra grant use":      {partial("dispatch", busyBegin), reserved + `INSERT INTO grants VALUES ('g1', '{}', 0, 0, 1, 1); INSERT INTO grant_uses (label, tenant_id, idempotency_key) VALUES ('g1', 'ten', 'k')`},
		"dispatch failed, dispatch event":       {partial("dispatch", busyBegin), reserved + `INSERT INTO receipt_events (tenant_id, idempotency_key, status, cause) VALUES ('ten', 'k', 'dispatched', 'dispatch')`},
		"busy in the body":                      {partial("adapter", map[string]any{"code": "busy", "at": "body"}), dispatched},
		"non-busy error":                        {partial("adapter", map[string]any{"code": "other", "at": "begin"}), dispatched},
		"reserve rolled back before":            {edit(partial("dispatch", busyBegin), func(tx map[string]any) { tx["reserve"].(map[string]any)["outcome"] = "rollback" }), reserved},
		"earlier transaction missing":           {edit(partial("adapter", busyBegin), func(tx map[string]any) { delete(tx, "dispatch") }), dispatched},
		"transaction after the failure":         {edit(partial("adapter", busyBegin), func(tx map[string]any) { tx["observe"] = ok() }), dispatched},
		"two failing transactions":              {edit(partial("observe", busyBegin), func(tx map[string]any) { tx["dispatch"] = failed(busyBegin) }), dispatched},
		"audit with other error":                {edit(partial("dispatch", busyBegin), func(tx map[string]any) { tx["audit"] = failed(map[string]any{"code": "other", "at": "end"}) }), reserved},
		"reserve failure is not after progress": {partial("reserve", busyBegin), ""},
	} {
		if _, err := verifiedPartial(t, c.w, fresh(c.setup), "ten", "k", true); err == nil {
			t.Errorf("%s: inconsistent evidence accepted", name)
		}
	}
	// Group-wide: an effect or consumed permit no committed adapter accounts
	// for, or a dispatch no committed dispatch accounts for, is rejected.
	adapterFailed := []map[string]any{partial("adapter", busyBegin)}
	if err := checkAttribution(t, adapterFailed, fresh(dispatched)); err != nil {
		t.Fatalf("consistent group rejected: %v", err)
	}
	for name, setup := range map[string]string{
		"hidden effect":                        dispatched + `INSERT INTO adapter_consumed_permits VALUES ('permit-1'); INSERT INTO adapter_effects (permit_id) VALUES ('permit-1')`,
		"committed dispatch without its event": reserved,
	} {
		if checkAttribution(t, adapterFailed, fresh(setup)) == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

// --- Coordination gate ---------------------------------------------------

// gateFree reports whether nobody holds the coordination gate and no key
// lock is held or awaited.
func gateFree(b *Broker) bool {
	if !b.gate.TryLock() {
		return false
	}
	b.gate.Unlock()
	b.keys.mu.Lock()
	defer b.keys.mu.Unlock()
	return len(b.keys.m) == 0
}

// The gate (and every key lock) is released on every path: success,
// refusal, storage failure, each crash fault, panic; for execute,
// present_permit and reconcile.
func TestGateReleasedOnEveryPath(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	b, l := sqlRT(t, path)
	check := func(what string, got map[string]any, want string) {
		t.Helper()
		rtWant(t, got, want)
		if !gateFree(b) {
			t.Fatalf("%s: gate or key lock still held", what)
		}
	}
	first := rtExecute(t, "k1", "")
	check("success", b.apply(first, 0), `{"result":"effect_completed"}`)
	check("replay", b.apply(first, 1), `{"replayed":true,"result":"effect_completed"}`)
	conflict := rtExecute(t, "k1", "")
	conflict["request"].(map[string]any)["action"].(map[string]any)["resource"].(map[string]any)["uri"] = "x://z"
	k, _ := sjson.Obj(conflict["seal"])
	k["mac"] = mac(hexKey(rtKeyHex), sjson.MustCanonical(conflict["request"]))
	check("refusal", b.apply(conflict, 2), `{"code":"IDEMPOTENCY_CONFLICT","result":"refused"}`)
	for i, fault := range []string{"crash_before_reserve", "crash_after_reserve", "crash_after_dispatch"} {
		check(fault, b.apply(rtExecute(t, fmt.Sprintf("c%d", i), fault), 3+i), `{"result":"crashed"}`)
	}
	check("effect_fails", b.apply(rtExecute(t, "f1", "effect_fails"), 6), `{"result":"effect_failed"}`)
	check("present_permit replay", b.apply(map[string]any{"op": "present_permit", "of_step": int64(0), "request": first["request"]}, 7),
		`{"code":"PERMIT_REPLAYED","result":"refused"}`)
	check("present_permit tampered", b.apply(map[string]any{"op": "present_permit", "of_step": int64(0), "request": first["request"], "tamper": "mac"}, 8),
		`{"code":"PERMIT_INVALID","result":"refused"}`)
	check("present_permit missing", b.apply(map[string]any{"op": "present_permit", "of_step": int64(99)}, 9),
		`{"code":"PERMIT_MISSING","result":"refused"}`)
	check("reconcile", b.apply(map[string]any{"op": "reconcile"}, 10), `{"count":1,"result":"reconciled"}`)
	check("restart", b.apply(map[string]any{"op": "restart_broker"}, 11), `{"result":"restarted"}`)
	check("after restart", b.apply(rtExecute(t, "k2", ""), 12), `{"result":"effect_completed"}`)

	// Storage failure: an injected statement failure inside T-reserve, and
	// a busy reconcile.
	execOn(t, path, `CREATE TRIGGER inj BEFORE INSERT ON receipts WHEN NEW.idempotency_key = 's1' BEGIN SELECT RAISE(ABORT, 'injected'); END`)
	stderrOf(t, func() { check("storage failure", b.apply(rtExecute(t, "s1", ""), 13), unavailable) })
	release := holdLock(t, path)
	if _, err := l.conn.ExecContext(ctx, `PRAGMA busy_timeout = 50`); err != nil {
		t.Fatal(err)
	}
	stderrOf(t, func() {
		check("busy reconcile", b.apply(map[string]any{"op": "reconcile"}, 14), unavailable)
		check("busy execute", b.apply(rtExecute(t, "s2", ""), 15), unavailable)
	})
	release()
	if _, err := l.conn.ExecContext(ctx, `PRAGMA busy_timeout = 5000`); err != nil {
		t.Fatal(err)
	}

	// Panic inside an operation holding the gate (shared and exclusive).
	for _, op := range []map[string]any{rtExecute(t, "p1", ""), {"op": "reconcile"}} {
		l.onBegin = func() { panic("injected") }
		func() {
			defer func() {
				if r := recover(); r != "injected" {
					t.Fatalf("panic %v", r)
				}
			}()
			b.apply(op, 16)
		}()
		l.onBegin = nil
		if !gateFree(b) {
			t.Fatalf("%v: gate or key lock held after a panic", op["op"])
		}
	}
	check("after panics", b.apply(rtExecute(t, "k3", ""), 17), `{"result":"effect_completed"}`)
}

// Lock order: an execute waits for the gate before it takes its key lock.
func TestGateBeforeKeyLock(t *testing.T) {
	b, _ := sqlRT(t, filepath.Join(t.TempDir(), "l.db"))
	b.gate.Lock()
	done := make(chan map[string]any, 1)
	op := rtExecute(t, "k1", "")
	go func() { done <- b.apply(op, 0) }()
	time.Sleep(100 * time.Millisecond)
	b.keys.mu.Lock()
	held := len(b.keys.m)
	b.keys.mu.Unlock()
	if held != 0 {
		t.Fatal("key lock taken while waiting for the gate")
	}
	select {
	case <-done:
		t.Fatal("execute ran while the gate was held exclusive")
	default:
	}
	b.gate.Unlock()
	select {
	case r := <-done:
		rtWant(t, r, `{"result":"effect_completed"}`)
	case <-time.After(groupDeadline):
		t.Fatal("execute never acquired the gate")
	}
}

// blockedExecute starts an execute on its own connection that stops just
// before its first BEGIN (holding the gate shared and its key lock) until
// release is closed.
func blockedExecute(t *testing.T, b *Broker, key string) (entered, release chan struct{}, done chan map[string]any) {
	t.Helper()
	w, err := b.led.fork()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(w.close)
	entered, release, done = make(chan struct{}), make(chan struct{}), make(chan map[string]any, 1)
	var once sync.Once
	w.(*sqlLedger).onBegin = func() {
		once.Do(func() { close(entered); <-release })
	}
	op := rtExecute(t, key, "")
	go func() { done <- b.applyOn(w, nil, op, -1) }()
	return
}

func wait[T any](t *testing.T, ch <-chan T, what string) T {
	t.Helper()
	select {
	case v := <-ch:
		return v
	case <-time.After(groupDeadline):
		t.Fatalf("%s: no progress within %v", what, groupDeadline)
		var zero T
		return zero
	}
}

func blocked[T any](t *testing.T, ch <-chan T, what string) {
	t.Helper()
	select {
	case <-ch:
		t.Fatalf("%s finished while it should wait", what)
	case <-time.After(150 * time.Millisecond):
	}
}

// Executes hold the gate shared and run concurrently; a reconcile waits
// for the in-flight execute, blocks new executes only while it waits for or
// holds the gate, and never sees the in-flight receipt.
func TestGateSharedAndExclusive(t *testing.T) {
	b, _ := sqlRT(t, filepath.Join(t.TempDir(), "l.db"))
	enteredA, releaseA, doneA := blockedExecute(t, b, "a")
	wait(t, enteredA, "execute a")
	// Another execute runs to completion while a holds the gate shared.
	doneB := make(chan map[string]any, 1)
	wb := mustFork(t, b)
	opB := rtExecute(t, "b", "")
	go func() { doneB <- b.applyOn(wb, nil, opB, -1) }()
	rtWant(t, wait(t, doneB, "concurrent execute"), `{"result":"effect_completed"}`)
	// A reconcile waits for a.
	doneR := make(chan map[string]any, 1)
	wr := mustFork(t, b)
	go func() { doneR <- b.applyOn(wr, nil, map[string]any{"op": "reconcile"}, -1) }()
	blocked(t, doneR, "reconcile")
	// A new execute waits behind the waiting reconcile (the gate is never
	// re-entered, so this cannot deadlock a).
	doneC := make(chan map[string]any, 1)
	wc, opC := mustFork(t, b), rtExecute(t, "c", "")
	go func() { doneC <- b.applyOn(wc, nil, opC, -1) }()
	blocked(t, doneC, "execute c")
	close(releaseA)
	rtWant(t, wait(t, doneA, "execute a"), `{"result":"effect_completed"}`)
	rtWant(t, wait(t, doneR, "reconcile"), `{"count":0,"result":"reconciled"}`)
	rtWant(t, wait(t, doneC, "execute c"), `{"result":"effect_completed"}`)
	rtWant(t, b.final(), `{"consumed_permits":3,"effects_applied":3,"grant_uses":{},"receipts":{"observed_success":3},"refusals":0}`)
}

func mustFork(t *testing.T, b *Broker) ledger {
	t.Helper()
	w, err := b.led.fork()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(w.close)
	return w
}

// Executes (some crashing after dispatch) and reconciles in one group, both
// profiles, repeated: reconcile never touches an in-flight execute, and
// nothing deadlocks; also 64 operations in one group.
func TestReconcileAndExecutesInOneGroup(t *testing.T) {
	mixed := func(nExec, nCrash, nRec int, salt string) []any {
		var ops []any
		for i := 0; i < nExec; i++ {
			ops = append(ops, rtExecute(t, fmt.Sprintf("%se%d", salt, i), ""))
		}
		for i := 0; i < nCrash; i++ {
			ops = append(ops, rtExecute(t, fmt.Sprintf("%sc%d", salt, i), "crash_after_dispatch"))
		}
		for i := 0; i < nRec; i++ {
			ops = append(ops, map[string]any{"op": "reconcile"})
		}
		return ops
	}
	for _, p := range []Profile{Primary, Cold} {
		for rep := 0; rep < 10; rep++ {
			path := filepath.Join(t.TempDir(), "l.db")
			b, _ := sqlRT(t, path)
			b.profile = p
			salt := fmt.Sprintf("r%d", rep)
			var res map[string]any
			errs := stderrOf(t, func() {
				res, _ = mustRun(t, b, map[string]any{"op": "concurrent", "operations": mixed(8, 3, 8, salt)})
			})
			if !reflect.DeepEqual(res["outcomes"], map[string]any{"effect_completed": int64(8), "crashed": int64(3), "reconciled": int64(8)}) || errs != "" {
				t.Fatalf("profile %d rep %d: %v, stderr %q", p, rep, res, errs)
			}
			unknown := count(t, path, `SELECT count(*) FROM receipts WHERE status = 'unknown_reconcile'`)
			for q, want := range map[string]int64{
				`SELECT count(*) FROM receipts WHERE status = 'observed_success'`:                                                8,
				`SELECT count(*) FROM receipts WHERE status IN ('dispatched', 'unknown_reconcile')`:                              3,
				`SELECT count(*) FROM receipts WHERE status = 'unknown_reconcile' AND idempotency_key NOT LIKE '` + salt + `c%'`: 0,
				`SELECT count(*) FROM receipt_events WHERE cause = 'reconcile'`:                                                  unknown,
				`SELECT count(*) FROM adapter_effects`:                                                                           11,
				`SELECT count(*) FROM adapter_consumed_permits`:                                                                  11,
			} {
				if n := count(t, path, q); n != want {
					t.Fatalf("profile %d rep %d: %s = %d, want %d", p, rep, q, n, want)
				}
			}
		}
		// 64 operations: no deadlock, no state conflict, intact file; busy
		// timeouts are possible (D-026) but only as verified aborts.
		path := filepath.Join(t.TempDir(), "l.db")
		b, _ := sqlRT(t, path)
		b.profile = p
		ops := mixed(48, 8, 8, "x")
		var res, g map[string]any
		errs := stderrOf(t, func() { res, g = mustRun(t, b, map[string]any{"op": "concurrent", "operations": ops}) })
		total := int64(0)
		for _, n := range res["outcomes"].(map[string]any) {
			total += n.(int64)
		}
		if total != 64 || strings.Contains(errs, "STATE_CONFLICT") {
			t.Fatalf("profile %d 64-way: %v, stderr %q", p, res, errs)
		}
		var execs []map[string]any
		var execOps []any
		uncertain := map[string]bool{}
		for i, o := range g["operations"].([]any) {
			w := o.(map[string]any)
			if w["op"] == "reconcile" {
				if errs := txErrors(w); len(errs) != 0 && !reflect.DeepEqual(errs["reconcile"], busyBegin) {
					t.Fatalf("reconcile %d: %v", i, errs)
				}
				continue
			}
			execs, execOps = append(execs, w), append(execOps, ops[i])
			if errs := txErrors(w); errs["adapter"] != nil || errs["observe"] != nil {
				uncertain[ops[i].(map[string]any)["request"].(map[string]any)["idempotency_key"].(string)] = true
			}
		}
		acceptFailures(t, execs, execOps, path, true)
		// Only receipts whose operation returned (crashed, or failed after
		// dispatch) may have been reconciled.
		rows, err := sql.Open("sqlite3", "file:"+path+"?mode=ro")
		if err != nil {
			t.Fatal(err)
		}
		r, err := rows.Query(`SELECT idempotency_key FROM receipts WHERE status = 'unknown_reconcile'`)
		if err != nil {
			t.Fatal(err)
		}
		for r.Next() {
			var k string
			r.Scan(&k)
			if !strings.HasPrefix(k, "xc") && !uncertain[k] {
				t.Fatalf("profile %d 64-way: reconcile touched in-flight execute %s", p, k)
			}
		}
		r.Close()
		rows.Close()
		if ok := integrityCheck(t, path); ok != "ok" {
			t.Fatalf("profile %d 64-way: integrity_check %q", p, ok)
		}
	}
}

// --- State conflict ------------------------------------------------------

// A receipt transition that matches no row is STATE_CONFLICT on stderr,
// LEDGER_UNAVAILABLE on stdout, D-026 evidence other at body.
func TestStateConflictObserve(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	b, _ := sqlRT(t, path)
	execOn(t, path, `CREATE TRIGGER foreign_reconcile AFTER UPDATE OF status ON receipts WHEN NEW.status = 'dispatched'
		BEGIN UPDATE receipts SET status = 'unknown_reconcile' WHERE tenant_id = NEW.tenant_id AND idempotency_key = NEW.idempotency_key; END`)
	tm := newOpTiming(map[string]any{"op": "execute"})
	errs := stderrOf(t, func() { rtWant(t, b.applyOn(b.led, tm, rtExecute(t, "k1", ""), 0), unavailable) })
	if errs != "ledger error: transaction observe STATE_CONFLICT\n" {
		t.Fatalf("stderr %q", errs)
	}
	wantTxError(t, txJSON(t, tm, "observe"), map[string]any{"code": "other", "at": "body"})
}

// A stored receipt status outside the schema is STATE_CONFLICT in reserve.
func TestStateConflictReserve(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.db")
	b, _ := sqlRT(t, path)
	op := rtExecute(t, "k1", "")
	execOn(t, path, `PRAGMA ignore_check_constraints = ON;
		INSERT INTO receipts VALUES ('ten', 'k1', '`+requestFingerprint(op["request"])+`', NULL, 'bogus')`)
	tm := newOpTiming(map[string]any{"op": "execute"})
	errs := stderrOf(t, func() { rtWant(t, b.applyOn(b.led, tm, op, 0), unavailable) })
	if errs != "ledger error: transaction reserve STATE_CONFLICT\n" {
		t.Fatalf("stderr %q", errs)
	}
	wantTxError(t, txJSON(t, tm, "reserve"), map[string]any{"code": "other", "at": "body"})
}

func integrityCheck(t *testing.T, path string) string {
	t.Helper()
	db, err := sql.Open("sqlite3", "file:"+path+"?mode=ro")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var ok string
	if err := db.QueryRow(`PRAGMA integrity_check`).Scan(&ok); err != nil {
		t.Fatal(err)
	}
	return ok
}
