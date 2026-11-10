package broker

import (
	"sync"
	"time"
)

// ledger is the durable broker state behind the logical transactions of
// LEDGER-SQLITE.md: one method is one transaction.
type ledger interface {
	// serialize is held for a whole operation: the in-memory ledger runs
	// every operation as one transaction; SQLite relies on its own locks.
	serialize() func()
	// Each transaction records its timing in t (nil: not timed).
	reserve(t *opTiming, r reservation) (string, error)
	dispatch(t *opTiming, tenant, key string) (int64, error)
	// adapter is T-adapter: check (the permit's fingerprint, MAC and expiry
	// checks) runs inside the transaction, then the replay check, then the
	// consumed id and effect are recorded. It returns a refusal code or "".
	adapter(t *opTiming, check func() string, permitID string, effect bool) (code string, err error)
	observe(t *opTiming, tenant, key, status string) error
	addGrant(t *opTiming, label string, g grant) (inUse bool, err error)
	reconcile(t *opTiming) (int64, error)
	audit(t *opTiming, code string) error
	final() (map[string]any, error)
	fork() (ledger, error) // the ledger for one concurrent worker
	restart() error
	close()
}

type grant struct {
	binding string // grantBinding, including the verdict
	expires time.Time
	maxUses int64
	uses    int64
}

// reservation is the input of T-reserve. Its outcome is "" (reserved now, or
// an existing reservation resumes), outcomeCrashed, a terminal receipt status
// to replay, or a refusal code.
type reservation struct {
	tenant, key, fingerprint string
	grantLabel               string // effective grant, "" for allow
	approval                 bool   // require_approval: consume one use of grantLabel
	crashBefore              bool
	check                    func(g *grant) string // refusal for a grant (nil if unknown), or ""
}

const outcomeCrashed = "crashed"

// existingOutcome is T-reserve's answer for a receipt that already exists.
func (r reservation) existingOutcome(fingerprint, grantLabel, status string) string {
	if fingerprint != r.fingerprint || grantLabel != r.grantLabel {
		return "IDEMPOTENCY_CONFLICT"
	}
	switch status {
	case "observed_success", "observed_failure":
		return status
	case "dispatched", "unknown_reconcile":
		return "RECONCILIATION_REQUIRED"
	}
	return ""
}

type receipt struct {
	fingerprint string
	grantLabel  string // "" for allow
	status      string // reserved | dispatched | observed_success | observed_failure | unknown_reconcile
}

// memLedger is the in-memory ledger: every operation holds mu throughout.
type memLedger struct {
	mu             sync.Mutex
	grants         map[string]*grant
	receipts       map[string]*receipt // tenant + "\x00" + idempotency key
	consumed       map[string]bool
	permitSeq      int64
	effectsApplied int64
	refusals       int64
}

func newMemLedger() *memLedger {
	return &memLedger{grants: map[string]*grant{}, receipts: map[string]*receipt{}, consumed: map[string]bool{}}
}

func (m *memLedger) serialize() func() {
	m.mu.Lock()
	return m.mu.Unlock
}

// Each in-memory method is the section of its logical transaction: it
// "commits" when it changes state as the SQLite transaction would commit,
// and "rolls back" otherwise.

func (m *memLedger) reserve(t *opTiming, r reservation) (outcome string, err error) {
	memTx(t, "reserve", func() bool {
		k := r.tenant + "\x00" + r.key
		if x, ok := m.receipts[k]; ok {
			outcome = r.existingOutcome(x.fingerprint, x.grantLabel, x.status)
			return outcome == "" // a reservation resumes: commit nothing
		}
		if r.crashBefore {
			outcome = outcomeCrashed
			return false
		}
		if r.approval {
			g := m.grants[r.grantLabel]
			if outcome = r.check(g); outcome != "" {
				return false
			}
			g.uses++
		}
		m.receipts[k] = &receipt{fingerprint: r.fingerprint, grantLabel: r.grantLabel, status: "reserved"}
		return true
	})
	return outcome, nil
}

func (m *memLedger) dispatch(t *opTiming, tenant, key string) (seq int64, err error) {
	memTx(t, "dispatch", func() bool {
		m.permitSeq++
		m.receipts[tenant+"\x00"+key].status = "dispatched"
		seq = m.permitSeq
		return true
	})
	return seq, nil
}

func (m *memLedger) adapter(t *opTiming, check func() string, permitID string, effect bool) (code string, err error) {
	memTx(t, "adapter", func() bool {
		if code = check(); code != "" {
			return false
		}
		if m.consumed[permitID] {
			code = "PERMIT_REPLAYED"
			return false
		}
		m.consumed[permitID] = true
		if effect {
			m.effectsApplied++
		}
		return true
	})
	return code, nil
}

func (m *memLedger) observe(t *opTiming, tenant, key, status string) error {
	memTx(t, "observe", func() bool {
		m.receipts[tenant+"\x00"+key].status = status
		return true
	})
	return nil
}

func (m *memLedger) addGrant(t *opTiming, label string, g grant) (inUse bool, err error) {
	memTx(t, "approve", func() bool {
		if _, inUse = m.grants[label]; inUse {
			return false
		}
		m.grants[label] = &g
		return true
	})
	return inUse, nil
}

func (m *memLedger) reconcile(t *opTiming) (n int64, err error) {
	memTx(t, "reconcile", func() bool {
		for _, r := range m.receipts {
			if r.status == "dispatched" {
				r.status = "unknown_reconcile"
				n++
			}
		}
		return true
	})
	return n, nil
}

func (m *memLedger) audit(t *opTiming, _ string) error {
	memTx(t, "audit", func() bool {
		m.refusals++
		return true
	})
	return nil
}

func (m *memLedger) final() (map[string]any, error) {
	uses := map[string]any{}
	for l, g := range m.grants {
		uses[l] = g.uses
	}
	receipts := map[string]any{}
	for _, r := range m.receipts {
		n, _ := receipts[r.status].(int64)
		receipts[r.status] = n + 1
	}
	return map[string]any{
		"effects_applied":  m.effectsApplied,
		"grant_uses":       uses,
		"receipts":         receipts,
		"consumed_permits": int64(len(m.consumed)),
		"refusals":         m.refusals,
	}, nil
}

func (m *memLedger) fork() (ledger, error) { return m, nil }
func (m *memLedger) restart() error        { return nil }
func (m *memLedger) close()                {}

// keyLocks is the in-process lock per (tenant, idempotency key): operations
// on one key never interleave; an entry exists only while it is held or
// awaited.
type keyLocks struct {
	mu sync.Mutex
	m  map[string]*keyLock
}

type keyLock struct {
	sync.Mutex
	refs int
}

func (k *keyLocks) lock(key string) func() {
	k.mu.Lock()
	if k.m == nil {
		k.m = map[string]*keyLock{}
	}
	l := k.m[key]
	if l == nil {
		l = &keyLock{}
		k.m[key] = l
	}
	l.refs++
	k.mu.Unlock()
	l.Lock()
	return func() {
		l.Unlock()
		k.mu.Lock()
		if l.refs--; l.refs == 0 {
			delete(k.m, key)
		}
		k.mu.Unlock()
	}
}
