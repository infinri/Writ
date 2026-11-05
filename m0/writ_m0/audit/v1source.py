"""Read-only access to the pinned V1 baseline through Git objects.

The audit reads V1 evidence from the pinned main commit rather than the working
tree, so generated inventories do not change when someone edits the legacy files
on this branch.
"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# main at the time the V2 audit began (docs/audit/V1-ARCHITECTURE-BASELINE.md).
V1_BASELINE_SHA = "ebdafe5450b201339f81e717ed104e6bcfd3fe3e"


class BaselineUnavailable(RuntimeError):
    """Raised when the pinned commit is not present in the local clone."""


@dataclass(frozen=True)
class TreeEntry:
    path: str
    blob_sha: str
    size: int


def _git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise BaselineUnavailable(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def read_blob(path: str, sha: str = V1_BASELINE_SHA) -> bytes:
    return _git("show", f"{sha}:{path}")


def read_text(path: str, sha: str = V1_BASELINE_SHA) -> str:
    return read_blob(path, sha).decode("utf-8")


def list_tree(sha: str = V1_BASELINE_SHA) -> list[TreeEntry]:
    entries = []
    for line in _git("ls-tree", "-r", "-l", "-z", sha).split(b"\0"):
        if not line:
            continue
        meta, path = line.split(b"\t", 1)
        _mode, kind, blob_sha, size = meta.split()
        if kind != b"blob":
            continue
        entries.append(TreeEntry(path.decode("utf-8"), blob_sha.decode(), int(size)))
    return sorted(entries, key=lambda e: e.path)


def commit_metadata(sha: str = V1_BASELINE_SHA) -> dict[str, str]:
    raw = _git("show", "-s", "--format=%H%n%cI%n%T", sha).decode().splitlines()
    return {"commit": raw[0], "committed_at": raw[1], "tree": raw[2]}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
