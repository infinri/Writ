#!/usr/bin/env python3
"""Carry session caches out of the install-relative directories earlier releases used.

Every release before the state root kept session state in `<install>/var/session`, and a plugin
install path carries the version, so the first session on a new version found an empty cache
and every live session lost its mode and approvals. This copies `writ-session-*.json` from those
directories into the state root's session/ (writ/shared/state_root.py) at SessionStart.

WHY A SESSIONSTART COPY and not a lazy copy inside _read_cache: the bash readers
(writ_session_mode_direct, the auto-route classifier's jq read) never call _read_cache, so a
lazy python-side copy would leave them blind on exactly the first turn that decides routing.

WHERE IT LOOKS. This install's own var/session; when the install sits in a plugin cache, every
sibling version's var/session (1.9.0's own directory is empty, the live state is in 1.8.0's);
and the two home locations earlier releases were installed at. Siblings of a checkout that is
NOT in a plugin cache are never scanned: a clone in ~/workspaces sits beside other projects,
and a Magento project's var/session holds PHP session files by the hundred thousand.

WHAT IT GUARANTEES. Only writ-session-*.json regular files (a grant expires in 30 minutes and
an event buffer is telemetry); never a symlink; the newest mtime wins among copies of one id;
a destination that already exists is never touched, and the final step is os.link, which fails
atomically if a hook of the same session created the file meanwhile. A set WRIT_CACHE_DIR
disables the whole thing: an explicit override is the operator's decision, and it is what keeps
the test suite from ever reading a real legacy directory. Never fails the caller.

RUN ONCE PER INSTALLED VERSION. session-start-bootstrap.sh passes `--stamp-file` and
`--stamp-key` ("<version> <skill_root>") and skips this script while the stamp's first line
equals the key. The stamp is written here, atomically, only after a carry that raised nothing
and failed no copy, so an unfinished migration is retried at the next SessionStart rather than
hidden behind a stamp.
"""

from __future__ import annotations

import glob
import os
import shutil
import stat
import sys
import tempfile

_PATTERN = "writ-session-*.json"


def legacy_session_dirs(skill_root: str, home: str) -> list[str]:
    """Existing legacy session directories, realpath'd and de-duplicated, in scan order."""
    candidates = [os.path.join(skill_root, "var", "session")]
    if (os.sep + os.path.join("plugins", "cache") + os.sep) in (skill_root + os.sep):
        candidates += sorted(glob.glob(os.path.join(os.path.dirname(skill_root), "*", "var", "session")))
    candidates += [
        os.path.join(home, ".claude", "plugins", "marketplaces", "writ", "var", "session"),
        os.path.join(home, ".claude", "skills", "writ", "var", "session"),
    ]
    seen, out = set(), []
    for candidate in candidates:
        real = os.path.realpath(candidate)
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            out.append(real)
    return out


def carry_legacy_sessions(skill_root: str, dest_dir: str, home: str) -> int:
    """Copy each legacy session cache absent from dest_dir into it. Returns how many landed."""
    return carry_legacy_sessions_report(skill_root, dest_dir, home)[0]


def carry_legacy_sessions_report(skill_root: str, dest_dir: str, home: str) -> tuple[int, int]:
    """carry_legacy_sessions, reporting (carried, failed). A destination that already exists
    is not a failure: that copy is already done."""
    dest_real = os.path.realpath(dest_dir)
    newest: dict[str, tuple[float, str]] = {}
    for directory in legacy_session_dirs(skill_root, home):
        if directory == dest_real:
            continue
        for path in glob.glob(os.path.join(directory, _PATTERN)):
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            name = os.path.basename(path)
            if name not in newest or st.st_mtime > newest[name][0]:
                newest[name] = (st.st_mtime, path)
    if not newest:
        return 0, 0
    os.makedirs(dest_dir, exist_ok=True)
    carried = failed = 0
    for name, (_mtime, src) in sorted(newest.items()):
        dest = os.path.join(dest_dir, name)
        if os.path.lexists(dest):
            continue
        fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=name + ".", suffix=".migrate.tmp")
        try:
            with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
                shutil.copyfileobj(inp, out)
            shutil.copystat(src, tmp)
            os.link(tmp, dest)
            carried += 1
        except OSError:
            failed += 1
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return carried, failed


def write_stamp(stamp_file: str, stamp_key: str) -> None:
    """Replace the stamp atomically: a temp file in the same directory, then os.replace."""
    directory = os.path.dirname(stamp_file) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-migrate.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(stamp_key + "\n")
        os.replace(tmp, stamp_file)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _stamp_args(argv: list[str]) -> tuple[str, str]:
    """(--stamp-file, --stamp-key), each "" when absent. Both are needed to stamp."""
    values = {"--stamp-file": "", "--stamp-key": ""}
    for i, arg in enumerate(argv):
        if arg in values and i + 1 < len(argv):
            values[arg] = argv[i + 1]
    return values["--stamp-file"], values["--stamp-key"]


def main(argv: list[str] | None = None) -> int:
    try:
        stamp_file, stamp_key = _stamp_args(sys.argv[1:] if argv is None else argv)
        if os.environ.get("WRIT_CACHE_DIR"):
            return 0
        skill_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if skill_root not in sys.path:
            sys.path.insert(0, skill_root)
        from writ.shared.state_root import state_root

        dest = os.path.join(state_root(), "session")
        carried, failed = carry_legacy_sessions_report(skill_root, dest, os.path.expanduser("~"))
        if carried:
            print(f"[Writ] carried {carried} session cache(s) into {dest}")
        if stamp_file and stamp_key and not failed:
            write_stamp(stamp_file, stamp_key)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
