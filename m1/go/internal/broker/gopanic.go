package broker

import "sync"

// goPanic carries a panic out of a worker goroutine so that it reaches the
// per-line recover boundary in cmd/proto instead of killing the process.
type goPanic struct {
	mu  sync.Mutex
	val any
	set bool
}

// capture must be deferred directly by the worker goroutine.
func (g *goPanic) capture() {
	if r := recover(); r != nil {
		g.mu.Lock()
		if !g.set {
			g.val, g.set = r, true
		}
		g.mu.Unlock()
	}
}

// rethrow re-raises a captured panic on the calling goroutine.
func (g *goPanic) rethrow() {
	if g.set {
		panic(g.val)
	}
}
