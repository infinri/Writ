"""C2 (Wave 1 Cycle 1, plan.md): the session cache read-modify-write path has no
lock. Two writers doing a read/mutate/write cycle on the same session cache
(routine: the daemon's PostToolUse /update + Stop escalation + statusLine
/context-percent routes, or the daemon racing the CLI subprocess fallback) can
interleave and drop each other's writes.

The fix (plan.md ## Analysis, C2) adds `mutate_cache(session_id)`, an
`fcntl.flock`-guarded read-modify-write context manager backed by a per-session
`.lock` file next to the cache, to writ/session/cache.py. Plain `_read_cache`
stays lock-free (the hot path is unaffected).

RED today: `mutate_cache` and `_lock_path` do not exist in writ.session.cache,
so every test below fails on ImportError until the implementation lands.
Hermetic per TEST-ISOLATE-001/TEST-ISOLATE-003: WRIT_CACHE_DIR is monkeypatched
to tmp_path so no test touches a real session cache directory.
"""

from __future__ import annotations

import glob
import os
import threading

import pytest

from writ.session.cache import _read_cache, _write_cache, _cache_path

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401


def _seed(session_id: str, data: dict) -> None:
    _write_cache(session_id, data)


class TestMutateCacheConcurrentIncrements:
    """Two concurrent writers, each doing N read-modify-write increments via
    mutate_cache, must lose no updates -- the flock serializes the RMW so the
    final value is the full sum, not a partial count from a lost update."""

    def test_concurrent_increments_lose_no_updates(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.session.cache import mutate_cache  # RED: does not exist yet

        session_id = "concurrency-increment-sid"
        _seed(session_id, {"n": 0})

        iterations = 200

        def _worker() -> None:
            for _ in range(iterations):
                with mutate_cache(session_id) as cache:
                    cache["n"] = cache.get("n", 0) + 1

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        result = _read_cache(session_id)
        assert result["n"] == 2 * iterations, (
            f"expected {2 * iterations} (no lost updates across 2 writers x "
            f"{iterations} increments each), got {result['n']} -- an unlocked "
            f"read-modify-write drops concurrent writes"
        )


class TestMutateCacheLockFile:
    """mutate_cache serializes writers via an fcntl.flock on a per-session .lock
    file that lives next to the cache but never collides with the
    writ-session-*.json cache glob used by enumeration elsewhere."""

    def test_lock_file_created_next_to_cache(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.session.cache import mutate_cache  # RED: does not exist yet

        session_id = "lock-file-sid"
        _seed(session_id, {"n": 0})

        with mutate_cache(session_id) as cache:
            cache["n"] += 1

        cache_path = _cache_path(session_id)
        lock_path = cache_path + ".lock"
        assert os.path.exists(lock_path), (
            f"expected a lock file at {lock_path!r} next to the cache after one "
            f"mutate_cache write; dir contents: {os.listdir(tmp_path)!r}"
        )

        cache_glob_matches = set(
            glob.glob(os.path.join(str(tmp_path), "writ-session-*.json"))
        )
        assert lock_path not in cache_glob_matches, (
            f"the lock file {lock_path!r} must NOT match the "
            f"writ-session-*.json cache glob used by enumeration elsewhere "
            f"(_collect_subagent_queried_rules, resolve_current_session_id)"
        )


class TestMutateCacheExceptionSafety:
    """An exception raised inside the mutate_cache body must propagate AND must
    not persist the partial in-progress mutation -- the write happens only on a
    clean exit."""

    def test_exception_in_body_does_not_persist_partial(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.session.cache import mutate_cache  # RED: does not exist yet

        session_id = "exception-safety-sid"
        _seed(session_id, {"n": 0})

        with pytest.raises(RuntimeError):
            with mutate_cache(session_id) as cache:
                cache["n"] = 99
                raise RuntimeError("boom mid-mutation")

        result = _read_cache(session_id)
        assert result["n"] == 0, (
            f"expected the partial mutation (n=99) to NOT be persisted after the "
            f"body raised, got n={result['n']!r}"
        )


# --- Atomic _write_cache (fix/session-mode-preserve) -------------------------
# Root cause of the observed mode=None session resets: _write_cache used a
# SHARED fixed temp name (path + ".tmp") with no lock, so two concurrent same-id
# writers truncated each other's temp -> torn JSON renamed onto the cache ->
# _read_cache returned the mode=None default -> a caller persisted that wipe.
# The fix writes via a per-call UNIQUE temp (tempfile.mkstemp) + fsync + atomic
# rename, and unlinks the temp on failure. These tests are RED on the old code.


def _concurrent_write_worker(barrier, cache_dir: str, session_id: str, tag: str, iterations: int) -> None:
    """Module-level (picklable) worker: hammer one session id with full-dict
    writes, all carrying mode='work'. The barrier makes every worker start its
    write burst at the same instant (max contention). A sizable pad widens the
    interleave window that torn writes need to corrupt a shared temp file.

    On the OLD shared-temp code this worker also crashes (nonzero exit) when a
    peer renames the shared temp away before this worker's own os.rename runs
    (FileNotFoundError). On the fixed code each worker owns a unique temp, so it
    never crashes."""
    os.environ["WRIT_CACHE_DIR"] = cache_dir
    from writ.session.cache import _write_cache as _wc

    pad = "x" * 8000
    barrier.wait()
    for i in range(iterations):
        _wc(session_id, {
            "mode": "work",
            "current_phase": "testing",
            "gates_approved": [],
            "tag": tag,
            "i": i,
            "pad": pad,
        })


class TestWriteCacheAtomicUniqueTemp:
    """Each _write_cache call must allocate its OWN temp file via tempfile.mkstemp
    so no two concurrent same-id writers can share (and clobber) a temp path."""

    def test_write_cache_uses_unique_temp_per_call(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        import tempfile

        seen: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def _spy(*args, **kwargs):
            fd, p = real_mkstemp(*args, **kwargs)
            seen.append(p)
            return fd, p

        monkeypatch.setattr("writ.session.cache.tempfile.mkstemp", _spy)

        session_id = "atomic-unique-tmp-sid"
        _write_cache(session_id, {"mode": "work", "n": 1})
        _write_cache(session_id, {"mode": "work", "n": 2})

        assert len(seen) == 2, (
            f"each _write_cache call must allocate its own temp via "
            f"tempfile.mkstemp; got {len(seen)} mkstemp call(s) -- a shared fixed "
            f"temp name lets concurrent same-id writers clobber one another"
        )
        assert seen[0] != seen[1], (
            f"temp file names must be unique per write; got duplicate {seen[0]!r}"
        )
        assert _cache_path(session_id) not in seen, (
            "must write via a temp file, never in place at the cache path"
        )
        assert _read_cache(session_id)["n"] == 2, "final cache must reflect the last write"


class TestWriteCacheAtomicFailureCleanup:
    """A write that raises mid-way must leave NO temp file behind and must leave
    the prior cache intact (all-or-nothing)."""

    def test_failed_write_leaves_no_temp_and_preserves_existing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        session_id = "atomic-fail-cleanup-sid"
        _seed(session_id, {"mode": "work", "current_phase": "testing"})

        def _boom(*args, **kwargs):
            raise ValueError("simulated disk-full mid-write")

        monkeypatch.setattr("writ.session.cache.json.dump", _boom)

        with pytest.raises(ValueError):
            _write_cache(session_id, {"mode": "SHOULD-NOT-PERSIST"})

        result = _read_cache(session_id)
        assert result.get("mode") == "work", (
            f"a failed write must leave the prior cache intact; "
            f"got mode={result.get('mode')!r}"
        )
        leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
        assert leftovers == [], (
            f"a failed write must not leave temp files behind; found {leftovers!r}"
        )


class TestWriteCacheConcurrentNoReset:
    """The real-world guard: many concurrent same-id writers (as a /compact or
    'continue'-retry burst spawns) while a reader watches. The reader must NEVER
    observe a reset mode -- torn writes previously wiped it to the None default."""

    def test_concurrent_writers_never_reset_mode(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        import multiprocessing as mp

        session_id = "atomic-concurrent-noreset-sid"
        _seed(session_id, {"mode": "work", "current_phase": "testing", "gates_approved": []})

        ctx = mp.get_context("fork")
        n_workers = 8
        barrier = ctx.Barrier(n_workers)
        procs = [
            ctx.Process(
                target=_concurrent_write_worker,
                args=(barrier, str(tmp_path), session_id, f"w{k}", 300),
            )
            for k in range(n_workers)
        ]
        for p in procs:
            p.start()

        bad_reads: list = []
        while any(p.is_alive() for p in procs):
            c = _read_cache(session_id)
            if c.get("mode") != "work":
                bad_reads.append(c.get("mode"))

        for p in procs:
            p.join(timeout=60)

        exit_codes = [p.exitcode for p in procs]
        assert all(code == 0 for code in exit_codes), (
            f"every writer must exit cleanly; got exit codes {exit_codes!r} -- a "
            f"nonzero code means a writer crashed (e.g. os.rename of a shared temp "
            f"already renamed away by a peer)"
        )
        assert not bad_reads, (
            f"a concurrent reader observed a reset/corrupted mode "
            f"{bad_reads[:5]!r} ({len(bad_reads)} times) -- torn cache writes "
            f"wiped the live session mode"
        )
        final = _read_cache(session_id)
        assert final.get("mode") == "work", (
            f"final cache mode must be 'work'; got {final.get('mode')!r}"
        )
        leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
        assert leftovers == [], (
            f"no temp files may linger after concurrent writes; found {leftovers!r}"
        )


# --- Reentrant mutate_cache + serialized writers (layer 2) -------------------
# The mode=None wipe also reproduces via a pure lost-update: an unlocked
# whole-dict writer (cmd_update, _mode_set, ...) persists a stale snapshot over a
# newer write. The fix routes every writer through mutate_cache; to migrate
# writers that call one another (e.g. _mode_init -> _mode_set) mutate_cache must
# be reentrant per-thread. These tests are RED on the current code.


class TestMutateCacheReentrant:
    """A nested mutate_cache for the same session id in ONE thread must not
    deadlock on the non-reentrant fcntl.flock; it shares the cache and the
    outermost block owns the single write."""

    def test_nested_same_session_does_not_deadlock(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.session.cache import mutate_cache

        session_id = "reentrant-sid"
        _seed(session_id, {"n": 0, "mode": "work"})

        result: dict = {}

        def _worker() -> None:
            with mutate_cache(session_id) as outer:
                outer["outer_touched"] = True
                with mutate_cache(session_id) as inner:
                    inner["inner_touched"] = True
                    result["same_object"] = inner is outer
            result["done"] = True

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=5)

        assert not t.is_alive(), (
            "nested mutate_cache(same session id) deadlocked -- the flock is not "
            "reentrant, so migrating a writer that calls another would hang"
        )
        assert result.get("done") is True
        assert result.get("same_object") is True, (
            "the nested block must yield the SAME cache dict as the outer block"
        )
        final = _read_cache(session_id)
        assert final.get("outer_touched") is True and final.get("inner_touched") is True, (
            "both mutations must persist via the single outer write"
        )
        assert final.get("mode") == "work", "reentrancy must not disturb existing fields"


class TestWriterSerialization:
    """Migrated writers acquire the per-session lock, so they cannot complete a
    read-modify-write while another holder has the cache locked -- which is what
    stops a stale writer from clobbering mode/gates (door C)."""

    def _assert_blocks_while_locked(self, tmp_path, call_writer) -> None:
        from writ.session.cache import mutate_cache

        session_id = "serialize-sid"
        _seed(session_id, {"mode": "work", "queries": 0})

        started = threading.Event()
        finished = threading.Event()

        def _writer() -> None:
            started.set()
            call_writer(session_id)
            finished.set()

        t = threading.Thread(target=_writer, daemon=True)
        with mutate_cache(session_id) as cache:
            cache["mode"] = "work"
            t.start()
            started.wait(2)
            # A different thread holding no reentrant token must block on the flock
            # for the whole time we hold the lock.
            blocked = not finished.wait(1.0)

        t.join(timeout=5)
        assert blocked, (
            "the writer completed while the cache lock was held -- it bypasses "
            "mutate_cache and can lost-update mode/gates"
        )
        assert finished.is_set(), "the writer never completed after the lock released"
        assert _read_cache(session_id)["mode"] == "work"

    def test_cmd_update_blocks_while_cache_locked(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.session.budget_tracking import cmd_update

        self._assert_blocks_while_locked(tmp_path, lambda sid: cmd_update(sid, ["--inc-queries"]))

    def test_mode_set_blocks_while_cache_locked(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.session.mode_engine import _mode_set

        self._assert_blocks_while_locked(tmp_path, lambda sid: _mode_set(sid, "work"))
