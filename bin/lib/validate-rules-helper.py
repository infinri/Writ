#!/usr/bin/env python3
"""Single-spawn helper for validate-rules.sh.

Collapses the five pre/post Python blocks in validate-rules.sh into two
subprocess invocations: one before /analyze (pre-analyze) and one after
(post-analyze). Output is a single JSON blob the hook consumes with a
single read.

Pre-analyze (no stdin):
  validate-rules-helper.py pre-analyze --session-id <sid> --file <path>
    --project-root <root> [--cache-json <json>]
  Emits:
    {"should_proceed": bool, "context": "<lang fw role>", "phase": str,
     "boundary_mode": "boundary"|"warning", "plan_file": str}

Post-analyze (stdin = /analyze JSON response):
  validate-rules-helper.py post-analyze --session-id <sid> --file <path>
    --project-root <root> [--cache-json <json>] [--plan-file <path>]
  Emits routing decisions to stdout / stderr in the same shape the inline
  shell pipeline produces (one JSON blob with verdict + findings to route).

Stdlib only, with ONE exception: writ.session.locators supplies the session-scoped
gate-artifact path. That module is itself stdlib-only and imports nothing else from writ,
and the alternative was a THIRD hand-rolled copy of a path that already exists in two
languages -- the duplicated-seam class of bug this repo has been bitten by before.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from writ.session.locators import gate_artifact_path  # noqa: E402


EXT_TO_LANG = {
    ".php": "PHP", ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby",
    ".xml": "XML", ".graphqls": "GraphQL",
}


def _detect_framework(project_root: str) -> str:
    if not project_root:
        return ""
    markers = [
        ("composer.json", "Magento 2" if os.path.isdir(os.path.join(project_root, "app/code")) else "PHP"),
        ("package.json", "Node.js"),
        ("pyproject.toml", "Python"),
        ("Cargo.toml", "Rust"),
        ("go.mod", "Go"),
    ]
    for marker, fw in markers:
        if os.path.exists(os.path.join(project_root, marker)):
            return fw
    return ""


def _detect_role(file_path: str) -> str:
    p = file_path.lower()
    if "/test/" in p or "/tests/" in p:
        return "unit test"
    if "/api/data/" in p:
        return "DTO interface"
    if "/api/" in p:
        return "service contract"
    if "/model/data/" in p:
        return "DTO implementation"
    if "/model/" in p:
        return "service implementation"
    if "/etc/" in p:
        if "di.xml" in p:
            return "dependency injection configuration"
        if "webapi.xml" in p:
            return "REST API route configuration"
        if "module.xml" in p:
            return "module declaration"
        return "configuration"
    if "/controller/" in p or "/controllers/" in p:
        return "controller"
    if "/plugin/" in p:
        return "plugin interceptor"
    if "/observer/" in p:
        return "event observer"
    return "source file"


def _build_context(file_path: str, project_root: str) -> str:
    ext = os.path.splitext(file_path)[1]
    lang = EXT_TO_LANG.get(ext, "unknown")
    fw = _detect_framework(project_root)
    role = _detect_role(file_path)
    parts = [p for p in [lang, fw, role] if p]
    return " ".join(parts)


def _find_plan_md(project_root: str) -> str:
    if not project_root:
        return ""
    candidates: list[str] = []
    candidates += glob.glob(os.path.join(project_root, "app/code/*/*/plan.md"))
    candidates += glob.glob(os.path.join(project_root, "src/*/plan.md"))
    candidates += glob.glob(os.path.join(project_root, "bin/plan.md"))
    candidates += glob.glob(os.path.join(project_root, "*/plan.md"))
    found = [c for c in candidates if os.path.isfile(c)]
    if not found:
        return ""
    found.sort(key=os.path.getmtime, reverse=True)
    return found[0]


def _derive_phase(project_root: str, session_id: str) -> str:
    """Phase from THIS SESSION's own gate artifacts under .claude/gates/<session_id>/.

    The session id is required because this function read the flat, session-less path for
    eighteen days and reported the workflow phase of whichever session in the repo had
    approved last: a sibling's approval moved this session past its own plan gate. A session
    with no artifact of its own answers "planning", whatever a sibling directory holds.

    An unresolvable path (no root, or a session id that is not a valid path component)
    answers as if nothing were approved, which fails CLOSED to the earliest phase.
    """
    if not project_root:
        return "code_generation"
    phase_a = gate_artifact_path(project_root, session_id, "phase-a")
    test_skeletons = gate_artifact_path(project_root, session_id, "test-skeletons")
    if not phase_a or not os.path.isfile(phase_a):
        return "planning"
    if not test_skeletons or not os.path.isfile(test_skeletons):
        return "code_generation"
    return "testing"


def _planned_files(plan_path: str) -> set[str]:
    if not plan_path or not os.path.isfile(plan_path):
        return set()
    try:
        with open(plan_path) as f:
            content = f.read()
    except OSError:
        return set()
    files: set[str] = set()
    in_files = False
    for line in content.split("\n"):
        if re.match(r"^##\s+Files", line):
            in_files = True
            continue
        if in_files and line.startswith("## "):
            break
        if in_files:
            for m in re.finditer(r"`([^`]+\.\w+)`", line):
                files.add(m.group(1))
            for m in re.finditer(r"\|\s*([^\|]+\.\w+)\s*\|", line):
                path = m.group(1).strip().strip("`")
                if "/" in path or "." in path:
                    files.add(path)
    return files


def _boundary_mode(plan_path: str, cache: dict) -> str:
    planned = _planned_files(plan_path)
    if not planned:
        return "warning"
    written = set(cache.get("files_written", []))
    analysis = cache.get("analysis_results", {}) or {}
    written_suffixes: set[str] = set()
    for w in written:
        parts = w.split("/")
        for i in range(len(parts)):
            written_suffixes.add("/".join(parts[i:]))
    all_written = all(
        any(p.endswith(planned_one) for p in written) or planned_one in written_suffixes
        for planned_one in planned
    )
    for w in written:
        if analysis.get(w) == "fail":
            return "warning"
    return "boundary" if all_written else "warning"


def _load_cache_arg(args: argparse.Namespace) -> dict:
    if getattr(args, "cache_json", None):
        try:
            return json.loads(args.cache_json)
        except (ValueError, json.JSONDecodeError):
            return {}
    return {}


def cmd_pre_analyze(args: argparse.Namespace) -> int:
    file_path = args.file or ""
    project_root = args.project_root or ""
    cache = _load_cache_arg(args)

    if not file_path:
        json.dump(
            {
                "should_proceed": False,
                "context": "",
                "phase": "planning",
                "boundary_mode": "warning",
                "plan_file": "",
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0

    context = _build_context(file_path, project_root)
    # --session-id is already supplied by validate-rules.sh, so the derived phase is per
    # session rather than per project.
    phase = _derive_phase(project_root, args.session_id or "")
    plan_file = _find_plan_md(project_root)
    boundary = _boundary_mode(plan_file, cache)

    should_proceed = True
    analysis_status = (cache.get("analysis_results") or {}).get(file_path, "absent")
    if cache and analysis_status != "pass":
        should_proceed = False

    json.dump(
        {
            "should_proceed": should_proceed,
            "context": context,
            "phase": phase,
            "boundary_mode": boundary,
            "plan_file": plan_file,
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


def cmd_post_analyze(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        json.dump({"verdict": "pass", "findings_to_route": []}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    try:
        resp = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        json.dump({"verdict": "error", "findings_to_route": []}, sys.stdout)
        sys.stdout.write("\n")
        return 0

    verdict = resp.get("verdict", "pass")
    findings = resp.get("findings", []) or []
    cache = _load_cache_arg(args)
    loaded_rule_ids = {r.get("rule_id") for r in (cache.get("loaded_rules") or [])}

    plan_hash = ""
    if args.plan_file and os.path.isfile(args.plan_file):
        try:
            with open(args.plan_file) as f:
                plan_hash = hashlib.md5(f.read().encode()).hexdigest()[:12]
        except OSError:
            plan_hash = "unknown"

    routes: list[dict] = []
    for f in findings:
        if f.get("status") != "violated":
            continue
        rid = f.get("rule_id")
        if rid in loaded_rule_ids:
            routes.append({
                "rule_id": rid,
                "action": "invalidate-gate",
                "gate": "phase-a",
                "evidence": (f.get("evidence", "") or "")[:200],
                "plan_hash": plan_hash,
            })
        else:
            routes.append({
                "rule_id": rid,
                "action": "warn",
                "evidence": (f.get("evidence", "") or "")[:200],
            })

    json.dump(
        {
            "verdict": verdict,
            "summary": resp.get("summary", ""),
            "findings_to_route": routes,
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="validate-rules-helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pre = sub.add_parser("pre-analyze")
    pre.add_argument("--session-id", default="")
    pre.add_argument("--file", default="")
    pre.add_argument("--project-root", default="")
    pre.add_argument("--cache-json", default="")

    post = sub.add_parser("post-analyze")
    post.add_argument("--session-id", default="")
    post.add_argument("--file", default="")
    post.add_argument("--project-root", default="")
    post.add_argument("--cache-json", default="")
    post.add_argument("--plan-file", default="")

    args = parser.parse_args()
    if args.cmd == "pre-analyze":
        return cmd_pre_analyze(args)
    if args.cmd == "post-analyze":
        return cmd_post_analyze(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
