"""P2 logging sweep: the scheduled backstop that bounds the P1 typed streams.

`rotate_logs()` is the daily backstop for everything the router's source-side
roll (writ.shared.logging._roll_if_oversize) cannot see -- bash-hook appends,
journald exports, and session-keyed `/tmp` scratch. It walks the central log
root once and, per project:

  1. rotates each live `<project>/<stream>.jsonl` into `<project>/archive/` when
     it is at/over `ROTATE_SIZE_BYTES` OR its mtime predates the current UTC day,
  2. gzips each uncompressed `archive/<stream>-<date>.jsonl` generation
     (including the ones just rotated) to `<stream>-<date>.jsonl.gz`,
  3. prunes archived `.jsonl.gz` generations older than the per-stream retention
     window, then
  4. deletes stale session-keyed scratch older than `SCRATCH_MAX_AGE_DAYS`.

The size threshold is imported DOWN from `writ.shared.logging` so the router and
the sweep share a single source (DRY-CONFIG-001); the retention/scratch windows
are sweep-only and live here.

`now` and `scratch_dir` are injected (keyword-only, default to
`datetime.now(timezone.utc)` and `/tmp`) so age-based behavior is deterministic
and hermetic (SOLID-DIP-001 / TEST-ISOLATE-002). Every file is processed inside
its own try/except: one unprocessable file never aborts the run (ERR-HANDLE-003),
and the function never raises. Idempotent, and a no-op (all-zero summary) when the
log root does not exist. stdlib only; no daemon, no Neo4j (ARCH-LAYER-001).
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from writ.shared.logging import (
    ROTATE_SIZE_BYTES,
    locked_archive_rename,
    log_root,
)

# Per-stream retention windows (days). audit/friction are 12-month compliance
# streams; metrics is high-volume operational telemetry; debug is short-lived.
RETENTION_DAYS: dict[str, int] = {
    "audit": 365,
    "friction": 365,
    "errors": 365,
    "metrics": 90,
    "debug": 14,
}

# The set of stream names that own a rotatable LIVE file `<stream>.jsonl`
# (derived from RETENTION_DAYS so the two can never drift). A live file is
# classified by this FILENAME invariant -- never by its parent directory name --
# so a project whose scope is literally `archive` (or ends in `/archive`, e.g.
# a `github.com/org/archive` remote) is never mistaken for an archive folder and
# never has its live streams gzipped/unlinked (the P2 data-loss regression).
_KNOWN_STREAMS: frozenset[str] = frozenset(RETENTION_DAYS)

# Session-keyed /tmp scratch is aged out after this window (a short one): these
# files are written by hooks and never otherwise cleaned.
SCRATCH_MAX_AGE_DAYS = 7

# Session-keyed scratch logs (one file per session id).
SCRATCH_GLOBS = (
    "writ-precompact-*.log",
    "writ-postcompact-*.log",
    "writ-feedback-*.log",
    "writ-coverage-*.log",
)

# Fixed-name payload cap files (line-capped by the hooks, never time-swept).
SCRATCH_CAP_FILES = (
    "writ-subagent-payloads.jsonl",
    "writ-subagent-stop-payloads.jsonl",
)

# Dated archive-generation filename patterns (stream and date tokens are
# internal, so a garbage-named archive entry simply fails to match and is
# skipped). Classification keys off THIS filename shape, not the parent dir
# name, so a project literally named `archive` is never confused with the
# `archive/` folder holding rolled generations.
#   `<stream>-<YYYY-MM-DD>[-<n>].jsonl`     (uncompressed, awaiting gzip)
#   `<stream>-<YYYY-MM-DD>[-<n>].jsonl.gz`  (compressed, subject to prune)
_ARCHIVE_JSONL_RE = re.compile(
    r"^(?P<stream>[A-Za-z]+)-(?P<date>\d{4}-\d{2}-\d{2})(?:-\d+)?\.jsonl$"
)
_ARCHIVE_GZ_RE = re.compile(
    r"^(?P<stream>[A-Za-z]+)-(?P<date>\d{4}-\d{2}-\d{2})(?:-\d+)?\.jsonl\.gz$"
)


def _is_live_stream_file(fn: str) -> bool:
    """True when `fn` is a rotatable LIVE stream file: its basename is exactly
    `<stream>.jsonl` for a known stream. A dated generation like
    `audit-2025-06-01.jsonl` never matches (its stem is not a bare stream name),
    so live files are told apart from archive generations by FILENAME shape
    alone -- the parent directory name is irrelevant."""
    if not fn.endswith(".jsonl"):
        return False
    return fn[: -len(".jsonl")] in _KNOWN_STREAMS


def _collect(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """One walk of the log root, partitioning entries into (live stream files,
    uncompressed archive generations, gzipped archive generations).

    Classification is by FILENAME invariant, never by the parent directory name
    (the P2 data-loss fix): a dated `<stream>-<date>[-<n>].jsonl[.gz]` is an
    archive generation wherever it sits, and a bare `<stream>.jsonl` for a known
    stream is a live file wherever it sits. This keeps a project whose resolved
    scope is literally `archive` -- or nested like `github.com/org/archive` --
    from having its live `audit.jsonl`/`friction.jsonl` treated as archive files
    and gzipped/unlinked. Root-level bookkeeping is never a live stream and is skipped;
    that set is exactly two files, both deliberately outside the taxonomy:

      `_fallback.jsonl`     the durable safety net. Rotating the thing that catches
                            failed writes would be self-defeating.
      `calibration.jsonl`   paired pattern-vs-LLM analyzer verdicts. Global rather than
                            per-project, so it is not a stream, and BOUNDED at the
                            writer: analysis/analyzer.py only calls log_calibration when
                            Instrumentation.get_mode() == "calibration", which flips to
                            "production" once the file reaches CALIBRATION_THRESHOLD
                            (100) lines. It cannot grow, so it needs no rotation.

    The coverage audit filed calibration.jsonl as "unrouted" (finding D3). It is
    unmanaged deliberately, not by omission -- which is only a defensible answer while
    that write bound holds, so tests/test_root_level_log_files.py pins both halves.
    """
    live: list[Path] = []
    arc_jsonl: list[Path] = []
    arc_gz: list[Path] = []
    root_str = str(root)
    for dirpath, _dirnames, filenames in os.walk(root):
        at_root = dirpath == root_str
        for fn in filenames:
            full = Path(dirpath) / fn
            if _ARCHIVE_GZ_RE.match(fn):
                arc_gz.append(full)
            elif _ARCHIVE_JSONL_RE.match(fn):
                arc_jsonl.append(full)
            elif not at_root and _is_live_stream_file(fn):
                live.append(full)
    return live, arc_jsonl, arc_gz


def _rotate_live(live: list[Path], now: datetime, summary: dict) -> list[Path]:
    """Rotate each over-size or over-age live stream file into its archive dir.
    Returns the freshly rotated destinations so they can be gzipped this run."""
    rotated: list[Path] = []
    today = now.date()
    for path in live:
        try:
            st = os.stat(path)
            mtime_date = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).date()
            over_size = st.st_size >= ROTATE_SIZE_BYTES
            over_age = mtime_date < today
            if not (over_size or over_age):
                continue
            dest = locked_archive_rename(
                path, path.parent / "archive", path.stem, mtime_date
            )
            summary["rotated"] += 1
            rotated.append(dest)
        except OSError:
            continue  # one bad file never aborts the sweep (ERR-HANDLE-003)
    return rotated


def _gzip_archives(candidates: list[Path], summary: dict) -> None:
    """Gzip each uncompressed archive generation to `<name>.gz` and drop the
    original. A pre-existing `.gz` sibling is left untouched (never clobbered)."""
    for jsonl in candidates:
        try:
            if not jsonl.is_file():
                continue
            gz = jsonl.with_suffix(jsonl.suffix + ".gz")
            if gz.exists():
                continue
            with open(jsonl, "rb") as fin, gzip.open(gz, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            jsonl.unlink()
            summary["gzipped"] += 1
        except OSError:
            continue


def _prune_archives(candidates: list[Path], now: datetime, summary: dict) -> None:
    """Delete gzipped generations older than their per-stream retention window.
    A generation whose name does not parse into <stream>-<date> is skipped."""
    today = now.date()
    for gz in candidates:
        try:
            match = _ARCHIVE_GZ_RE.match(gz.name)
            if not match:
                continue
            window = RETENTION_DAYS.get(match.group("stream"))
            if window is None:
                continue
            file_date = date.fromisoformat(match.group("date"))
            if (today - file_date).days > window:
                gz.unlink()
                summary["pruned"] += 1
        except (OSError, ValueError):
            continue


def _clean_scratch(scratch_dir: Path, now: datetime, summary: dict) -> None:
    """Delete session-keyed scratch + payload caps older than SCRATCH_MAX_AGE_DAYS."""
    if not scratch_dir.is_dir():
        return
    cutoff = now - timedelta(days=SCRATCH_MAX_AGE_DAYS)
    candidates: list[Path] = []
    for pattern in SCRATCH_GLOBS:
        candidates.extend(scratch_dir.glob(pattern))
    for name in SCRATCH_CAP_FILES:
        candidates.append(scratch_dir / name)
    for f in candidates:
        try:
            if not f.is_file():
                continue
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
                summary["scratch_cleaned"] += 1
        except OSError:
            continue


def rotate_logs(
    *,
    now: datetime | None = None,
    scratch_dir: Path | None = None,
) -> dict:
    """Rotate, compress, prune, and sweep the Writ log streams (see module docstring).

    Returns `{"rotated", "gzipped", "pruned", "scratch_cleaned"}` int counts and
    never raises. `now` / `scratch_dir` default to the real clock and `/tmp`.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if scratch_dir is None:
        scratch_dir = Path("/tmp")

    summary = {"rotated": 0, "gzipped": 0, "pruned": 0, "scratch_cleaned": 0}

    root = log_root()
    if root.is_dir():
        live, arc_jsonl, arc_gz = _collect(root)
        rotated = _rotate_live(live, now, summary)
        _gzip_archives(arc_jsonl + rotated, summary)
        _prune_archives(arc_gz, now, summary)

    _clean_scratch(scratch_dir, now, summary)
    return summary