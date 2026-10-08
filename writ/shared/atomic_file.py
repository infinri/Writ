"""Stage-then-commit file writes: a private temp file beside the target, fsynced, then
os.replace and a directory fsync.

Used for writ.toml by writ/neo4j_password.py and for the BM25 CURRENT pointer by
writ/retrieval/pipeline.py. Knows nothing about either file; each caller names its own
staged-file prefix.
"""

from __future__ import annotations

import os
import tempfile

PRIVATE_FILE_MODE = 0o600


def stage_file(path: str, text: str, prefix: str) -> str:
    """Write `text` to a new temp file beside `path`, fsynced, mode 0600; return its path.

    mkstemp creates it 0600 in the SAME directory, so the later rename is atomic and never
    exposes a partial or world-readable copy. On failure nothing is left behind.
    `prefix` names the staged file.
    """
    directory = os.path.dirname(path) or "."
    fd, staged = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, PRIVATE_FILE_MODE)
    except BaseException:
        discard_staged(staged)
        raise
    return staged


def commit_file(staged: str, path: str) -> None:
    """Rename the staged file over `path`, then fsync the directory so the rename is durable."""
    os.replace(staged, path)
    try:
        dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # The rename already happened. A filesystem that cannot fsync a directory must not
        # turn that into a reported failure.
        pass


def discard_staged(staged: str) -> None:
    try:
        os.unlink(staged)
    except FileNotFoundError:
        pass
