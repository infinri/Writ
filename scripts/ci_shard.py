#!/usr/bin/env python3
"""Split the pytest suite into N file-level shards for CI, and prove the split.

Stdlib only, so the aggregating CI job can run it with the runner's system
python3 and no venv.

    ci_shard.py --shard K --shards N       shard K's files, one per line
    ci_shard.py --check --shards N         every file in exactly one shard
    ci_shard.py --rebuild-timings XML...   rewrite the timings from junit XML

Discovery reimplements pytest's default collection for `pytest tests/`:
`test_*.py` and `*_test.py` at any depth, skipping the default
`norecursedirs`. pyproject sets none of the options that would change that;
tests/test_ci_shard.py fails the day it does. pytest itself is not asked
(`--collect-only`) because its session start wipes the disposable graph
under isolation.

Files are assigned whole, heaviest first, to the least-loaded shard, using
the committed per-file seconds in scripts/ci_timings.json. A stale timings
file only unbalances the shards; it can never drop a file.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TIMINGS_NAME = Path("scripts") / "ci_timings.json"

PYTHON_FILES = ("test_*.py", "*_test.py")
NORECURSEDIRS = ("*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}")


def _collection_key(path: str) -> tuple[str, ...]:
    # pytest 8 sorts files and directories of one directory jointly by name,
    # so comparing path parts reproduces the full-suite order.
    return tuple(path.split("/"))


def discover(root: Path) -> list[str]:
    root = Path(root)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root / "tests"):
        dirnames[:] = [
            d for d in dirnames if not any(fnmatch.fnmatch(d, pat) for pat in NORECURSEDIRS)
        ]
        for name in filenames:
            if any(fnmatch.fnmatch(name, pat) for pat in PYTHON_FILES):
                found.append((Path(dirpath) / name).relative_to(root).as_posix())
    return sorted(found, key=_collection_key)


def load_timings(path: Path) -> dict[str, float]:
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"timings file {path} must hold a JSON object, found {type(data).__name__}")
    return {str(k): float(v) for k, v in data.items()}


def _weights(files: list[str], timings: dict[str, float]) -> dict[str, float]:
    known = sorted(timings[f] for f in files if f in timings)
    default = known[len(known) // 2] if known else 1.0
    return {f: timings.get(f, default) for f in files}


def assign(files: list[str], timings: dict[str, float], shards: int) -> list[list[str]]:
    if shards < 1:
        raise ValueError(f"shard count must be at least 1, got {shards}")
    weights = _weights(files, timings)
    loads = [0.0] * shards
    result: list[list[str]] = [[] for _ in range(shards)]
    for f in sorted(files, key=lambda p: (-weights[p], p)):
        k = min(range(shards), key=lambda i: (loads[i], i))
        result[k].append(f)
        loads[k] += weights[f]
    return [sorted(shard, key=_collection_key) for shard in result]


def check_partition(files: list[str], shard_lists: list[list[str]]) -> tuple[list[str], list[str]]:
    """(missing, duplicated): files in no shard, and files in more than one."""
    counts: dict[str, int] = {}
    for shard in shard_lists:
        for f in shard:
            counts[f] = counts.get(f, 0) + 1
    missing = [f for f in files if f not in counts]
    duplicated = sorted((f for f, n in counts.items() if n > 1), key=_collection_key)
    return missing, duplicated


def _file_for_classname(classname: str, known: set[str]) -> str | None:
    parts = classname.split(".")
    for n in range(len(parts), 0, -1):
        candidate = "/".join(parts[:n]) + ".py"
        if candidate in known:
            return candidate
    return None


def rebuild_timings(xml_paths: list[Path], root: Path) -> dict[str, float]:
    known = set(discover(root))
    totals: dict[str, float] = {}
    for xml_path in xml_paths:
        for case in ET.parse(xml_path).getroot().iter("testcase"):
            file_attr = case.get("file")
            if file_attr:
                key = Path(file_attr).as_posix()
            else:
                key = _file_for_classname(case.get("classname", ""), known)
                if key is None:
                    continue
            totals[key] = totals.get(key, 0.0) + float(case.get("time") or 0.0)
    return {k: round(v, 1) for k, v in sorted(totals.items())}


def _write_timings(path: Path, timings: dict[str, float]) -> None:
    Path(path).write_text(json.dumps(timings, indent=2, sort_keys=True) + "\n")


def _fail(message: str) -> int:
    print(f"ci_shard: {message}", file=sys.stderr)
    return 1


def _describe(shard_lists: list[list[str]], weights: dict[str, float]) -> list[str]:
    return [
        f"shard {k}: {len(s)} files, estimated {sum(weights[f] for f in s):.1f} s"
        for k, s in enumerate(shard_lists)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shard", type=int)
    parser.add_argument("--shards", type=int)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rebuild-timings", nargs="+", type=Path, metavar="XML")
    parser.add_argument("--root", type=Path, default=REPO)
    parser.add_argument("--timings", type=Path)
    args = parser.parse_args(argv)
    timings_path = args.timings or args.root / TIMINGS_NAME

    if args.rebuild_timings:
        rebuilt = rebuild_timings(args.rebuild_timings, args.root)
        _write_timings(timings_path, rebuilt)
        print(f"wrote {len(rebuilt)} files, {sum(rebuilt.values()):.1f} s total, to {timings_path}")
        return 0

    if args.shards is None:
        return _fail("--shards N is required with --shard K or --check")
    if args.shards < 1:
        return _fail(f"--shards must be at least 1, got {args.shards}")
    if not args.check:
        if args.shard is None:
            return _fail("give --shard K (0-based) or --check")
        if not 0 <= args.shard < args.shards:
            return _fail(f"--shard {args.shard} is out of range for --shards {args.shards} (0 to {args.shards - 1})")

    files = discover(args.root)
    if not files:
        return _fail(f"no test files found under {args.root / 'tests'}; refusing to print an empty shard")
    spaced = [f for f in files if any(c.isspace() for c in f)]
    if spaced:
        return _fail("test paths containing whitespace cannot be word-split by the shell: " + ", ".join(spaced))

    timings = load_timings(timings_path)
    weights = _weights(files, timings)
    shard_lists = assign(files, timings, args.shards)

    if args.check:
        missing, duplicated = check_partition(files, shard_lists)
        if missing or duplicated:
            for f in missing:
                print(f"missing from every shard: {f}", file=sys.stderr)
            for f in duplicated:
                print(f"in more than one shard: {f}", file=sys.stderr)
            return _fail(f"partition broken: {len(missing)} missing, {len(duplicated)} duplicated")
        for line in _describe(shard_lists, weights):
            print(line)
        print(f"partition ok: {len(files)} files across {args.shards} shards, disjoint and complete")
        return 0

    estimates = ", ".join(f"{sum(weights[f] for f in s):.0f}" for s in shard_lists)
    mine = shard_lists[args.shard]
    if not mine:
        return _fail(
            f"shard {args.shard}/{args.shards} is empty ({len(files)} files); pytest given no "
            "paths would collect from the rootdir"
        )
    print(
        f"shard {args.shard}/{args.shards}: {len(mine)} files, estimated "
        f"{sum(weights[f] for f in mine):.1f} s; all shards: {estimates} s",
        file=sys.stderr,
    )
    sys.stdout.write("".join(f + "\n" for f in mine))
    return 0


if __name__ == "__main__":
    sys.exit(main())
