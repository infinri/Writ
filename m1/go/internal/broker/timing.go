package broker

import (
	"errors"
	"time"

	"github.com/mattn/go-sqlite3"

	"writ.dev/m1/proto/internal/sjson"
)

// Per-operation timing for `bench-broker-scenario` (M1-06d,
// LEDGER-SQLITE.md "Per-operation timing"). Timing is observation only:
// every recorder method accepts a nil receiver and does nothing, and no
// recorded value feeds back into a result. All durations come from time.Now
// and time.Since (monotonic).

// Timing is the `timing` member of one bench-broker-scenario answer.
type Timing struct {
	parse time.Duration
	start time.Time
	open  *time.Duration
	steps []*opTiming
	final *time.Duration
	run   time.Duration
}

// NewTiming starts the run clock (just after parsing) for a line whose
// envelope and input took parse to parse.
func NewTiming(parse time.Duration) *Timing {
	return &Timing{parse: parse, start: time.Now()}
}

// Done stops the run clock: the scenario output value is complete.
func (t *Timing) Done() {
	if t != nil {
		t.run = time.Since(t.start)
	}
}

func (t *Timing) step(op map[string]any) *opTiming {
	if t == nil {
		return nil
	}
	s := newOpTiming(op)
	t.steps = append(t.steps, s)
	return s
}

// JSON is T = {parse_ns, open_ns, steps, final_ns, run_ns}.
func (t *Timing) JSON() map[string]any {
	steps := make([]any, len(t.steps))
	for i, s := range t.steps {
		steps[i] = s.json()
	}
	return map[string]any{
		"parse_ns": t.parse.Nanoseconds(),
		"open_ns":  nsOrNull(t.open),
		"steps":    steps,
		"final_ns": nsOrNull(t.final),
		"run_ns":   t.run.Nanoseconds(),
	}
}

// opTiming is one step (or one operation of a concurrent step).
type opTiming struct {
	op                        string
	total                     time.Duration
	validate, decide, keyLock *time.Duration
	tx                        map[string]txTiming
	concurrent                bool
	operations                []*opTiming
}

// txTiming is one logical transaction: sql is false for the in-memory ledger
// (begin_ns and end_ns null).
type txTiming struct {
	sql              bool
	begin, body, end time.Duration
	commit           bool
	err              *txError // D-026: nil when no database error was raised
}

// txError is the D-026 `error` member of a transaction: code is busy (SQLite
// primary result code SQLITE_BUSY) or other; at is begin, body or end.
type txError struct {
	code, at string
}

// newTxError classifies a database or integrity error err raised at at.
func newTxError(at string, err error) *txError {
	code := "other"
	var se sqlite3.Error
	if errors.As(err, &se) && se.Code == sqlite3.ErrBusy {
		code = "busy"
	}
	return &txError{code: code, at: at}
}

func newOpTiming(op map[string]any) *opTiming {
	name, ok := sjson.Str(op, "op")
	if !ok {
		name = "unknown"
	}
	return &opTiming{op: name, tx: map[string]txTiming{}}
}

func since(start time.Time) *time.Duration {
	d := time.Since(start)
	return &d
}

func (o *opTiming) setTotal(d time.Duration) {
	if o != nil {
		o.total = d
	}
}

func (o *opTiming) setValidate(start time.Time) {
	if o != nil {
		o.validate = since(start)
	}
}

func (o *opTiming) setDecide(start time.Time) {
	if o != nil {
		o.decide = since(start)
	}
}

func (o *opTiming) setKeyLock(start time.Time) {
	if o != nil {
		o.keyLock = since(start)
	}
}

func (o *opTiming) addTx(name string, tx txTiming) {
	if o != nil {
		o.tx[name] = tx
	}
}

// worker returns the timing of operation i of a concurrent step.
func (o *opTiming) worker(op map[string]any) *opTiming {
	if o == nil {
		return nil
	}
	w := newOpTiming(op)
	o.operations = append(o.operations, w)
	return w
}

func (o *opTiming) json() map[string]any {
	txs := map[string]any{}
	for name, tx := range o.tx {
		begin, end := any(nil), any(nil)
		if tx.sql {
			begin, end = tx.begin.Nanoseconds(), tx.end.Nanoseconds()
		}
		outcome := "rollback"
		if tx.commit {
			outcome = "commit"
		}
		txErr := any(nil)
		if tx.err != nil {
			txErr = map[string]any{"code": tx.err.code, "at": tx.err.at}
		}
		txs[name] = map[string]any{"begin_ns": begin, "body_ns": tx.body.Nanoseconds(), "end_ns": end, "outcome": outcome, "error": txErr}
	}
	m := map[string]any{
		"op":               o.op,
		"total_ns":         o.total.Nanoseconds(),
		"validate_ns":      nsOrNull(o.validate),
		"decide_ns":        nsOrNull(o.decide),
		"key_lock_wait_ns": nsOrNull(o.keyLock),
		"tx":               txs,
	}
	if o.concurrent {
		ops := make([]any, len(o.operations))
		for i, w := range o.operations {
			ops[i] = w.json()
		}
		m["operations"] = ops
	}
	return m
}

func nsOrNull(d *time.Duration) any {
	if d == nil {
		return nil
	}
	return d.Nanoseconds()
}

// memTx times one in-memory ledger section as transaction name; f reports
// whether it committed (changed state) or rolled back (changed nothing).
func memTx(t *opTiming, name string, f func() bool) {
	start := time.Now()
	commit := f()
	t.addTx(name, txTiming{body: time.Since(start), commit: commit})
}
