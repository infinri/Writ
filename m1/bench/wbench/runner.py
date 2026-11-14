"""Prototype process execution: batch runs, concurrent sessions, cold start
and peak RSS. Inputs come from files so many concurrent processes do not hold
pipes open in the harness."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path


class Proto:
    def __init__(self, name: str, binary: str, cpu_list: str | None = None):
        self.name, self.binary = name, binary
        self.prefix = ["taskset", "-c", cpu_list] if cpu_list else []

    def argv(self, command: str) -> list[str]:
        return [*self.prefix, self.binary, *command.split(" ")]


def parse(stdout: bytes) -> tuple[dict, int, list[str]]:
    """({id: output}, null-id answers, problems). Lines split on LF only."""
    out, nulls, problems = {}, 0, []
    for raw in stdout.split(b"\n"):
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            problems.append("non-JSON output line")
            continue
        if not isinstance(msg, dict) or "output" not in msg:
            problems.append("line without output")
        elif msg.get("id") is None:
            nulls += 1
        elif msg["id"] in out:
            problems.append(f"duplicate id {msg['id']!r}")
        else:
            out[msg["id"]] = msg["output"]
    return out, nulls, problems


def run_batch(proto: Proto, command: str, input_path: Path, timeout: float = 3600) -> dict:
    with open(input_path, "rb") as f:
        started = time.perf_counter()
        p = subprocess.run(proto.argv(command), stdin=f, capture_output=True, timeout=timeout)
        wall = time.perf_counter() - started
    return {"wall_s": wall, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr[-500:]}


def run_concurrent(proto: Proto, command: str, input_paths: list[Path], out_dir: Path, timeout: float = 3600) -> dict:
    """Start one process per session at once; wall time until all exit."""
    procs = []
    started = time.perf_counter()
    for i, path in enumerate(input_paths):
        with open(path, "rb") as fin, open(out_dir / f"{proto.name}-{i}.out", "wb") as fout:
            procs.append(subprocess.Popen(proto.argv(command), stdin=fin, stdout=fout, stderr=subprocess.DEVNULL))
    codes = [p.wait(timeout=timeout) for p in procs]
    wall = time.perf_counter() - started
    stdout = b"".join((out_dir / f"{proto.name}-{i}.out").read_bytes() for i in range(len(input_paths)))
    return {"wall_s": wall, "returncode": max(codes) if codes else 0, "stdout": stdout}


def cold_start(proto: Proto, command: str, line: bytes, timeout: float = 60) -> dict:
    """Process launch to first answered line."""
    started = time.perf_counter()
    p = subprocess.Popen(proto.argv(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p.stdin.write(line + b"\n")
    p.stdin.close()
    first = p.stdout.readline()
    elapsed = time.perf_counter() - started
    rest = p.stdout.read()
    code = p.wait(timeout=timeout)
    return {"s": elapsed, "returncode": code, "stdout": first + rest}


RSS_RE = re.compile(rb"Maximum resident set size \(kbytes\): (\d+)")


def peak_rss(proto: Proto, command: str, input_path: Path, tmp: Path) -> dict:
    """Peak RSS of one full run under /usr/bin/time -v (separate from timed runs)."""
    report = tmp / f"time-{proto.name}-{os.getpid()}.txt"
    with open(input_path, "rb") as f:
        p = subprocess.run(["/usr/bin/time", "-v", "-o", str(report), *proto.argv(command)], stdin=f,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    m = RSS_RE.search(report.read_bytes()) if report.exists() else None
    return {"kb": int(m.group(1)) if m else None, "returncode": p.returncode}
