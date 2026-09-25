"""The unified investigation engine for the session helper (INV-1..9).

POL-6g-2 extracts the coverage / audit-fanout / research-triangulation / lens cluster out
of bin/lib/writ-session.py. One evidence-grounded process: a frozen coverage denominator,
audit fan-out partitioning, research corroboration, analysis capture, and the source_type
lens switch. Imports only lower layers (cache, citations, mode_engine, config) and the
stdlib -- never the facade -- so the graph stays acyclic. The facade re-exports this
surface; main()'s INV subcommands and its _AUDIT_BUDGET_* / _LENS_TABLE refs resolve unchanged.
"""

import json
import os
import sys

from writ.session.cache import _read_cache, mutate_cache
from writ.session.citations import _append_citation
from writ.session.config import EXT_TO_DOMAIN, PREFIX_TO_DOMAIN, UNIVERSAL_DOMAINS
from writ.session.mode_engine import _effective_source_type


# INV-4: cap the size of the unexamined / out-of-scope samples in the coverage map
# (the counts are exact; the lists are bounded for a flat per-call cost).
_COVERAGE_SAMPLE_MAX = 50

# INV-6a: default per-worker context budget for audit fan-out partitioning. A worker
# auditing <= this many LOC / files (plus injected rules) stays within a safe context
# window; the lead overrides via flags. The budget is the context-safety bound.
_AUDIT_BUDGET_LOC = 2000
_AUDIT_BUDGET_FILES = 30

# INV-6b: weight an error finding heavier than a plain finding when ranking where the
# lead should dig next. Coverage gap dominates (a barely-examined region ranks high
# even if it looks clean); errors and finding-density break ties within that.
_ATTENTION_ERROR_WEIGHT = 10

# INV-7a: a decision-driving web claim needs >= this many INDEPENDENT source domains.
# The hard, fail-closed corroboration floor (RESEARCH-CORROBORATE-001 made enforceable).
_TRIANGULATION_MIN_DOMAINS = 2

# INV-8: the source_type switch. Each lens of the one investigation engine maps to the
# gate that enforces it and that gate's strictness -- web (research) is the only hard one.
_LENS_TABLE = {
    "code": {"lens": "audit/explore", "enforcing_gate": "synthesis-gate", "gate_strictness": "advisory"},
    "web": {"lens": "research", "enforcing_gate": "triangulation-gate", "gate_strictness": "hard"},
    "runtime": {"lens": "debug", "enforcing_gate": "root-cause", "gate_strictness": "advisory"},
}


def cmd_coverage(session_id: str) -> None:
    """Report coverage: which file domains had rules vs which didn't.

    Legacy domain-ratio metric (kept for its existing consumers). For audit /
    investigation coverage use `coverage-map` (cmd_coverage_map), which reports an
    honest file-level ratio over a frozen denominator (INV-4).
    """
    cache = _read_cache(session_id)
    files = cache.get("files_written", [])
    rules = cache.get("loaded_rule_ids", [])

    if not files:
        json.dump({"status": "no_files", "message": "No files written this session"}, sys.stdout)
        sys.stdout.write("\n")
        return

    # Map files to domains
    file_domains = set()
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        domain = EXT_TO_DOMAIN.get(ext)
        if domain:
            file_domains.add(domain)

    # Extract domains from rule IDs (heuristic: first segment of rule ID)
    rule_domains = set()
    for rid in rules:
        prefix = rid.split("-")[0] if "-" in rid else rid
        mapped = PREFIX_TO_DOMAIN.get(prefix)
        if mapped:
            rule_domains.add(mapped)

    covered = file_domains & (rule_domains | UNIVERSAL_DOMAINS)
    uncovered = file_domains - covered

    report = {
        "status": "coverage_report",
        "files_written": len(files),
        "rules_loaded": len(rules),
        "file_domains": sorted(file_domains),
        "rule_domains": sorted(rule_domains),
        "covered_domains": sorted(covered),
        "uncovered_domains": sorted(uncovered),
        "coverage_pct": round(len(covered) / len(file_domains) * 100) if file_domains else 100,
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _examined_files(cache: dict) -> set[str]:
    """INV-4: the files the investigation has examined.

    Reuses the INV-2 citation_log (artifact_type=="file" rows) UNION the existing
    PreToolUse signal (pretool_queried_files). No new capture path -- presence of a
    file in either ledger means the agent cited or opened it.
    """
    examined = {
        r.get("ref") for r in cache.get("citation_log", [])
        if r.get("artifact_type") == "file" and r.get("ref")
    }
    examined.update(cache.get("pretool_queried_files", []))
    return examined


def cmd_coverage_map(session_id: str) -> None:
    """INV-4: honest file-level coverage over a FROZEN denominator.

    coverage = files examined in scope / files in the frozen scope. The denominator
    is frozen by `--freeze-scope`, so coverage cannot drift toward 100% by widening
    scope. Surfaces out-of-scope drift and the optional investigation-span budget.

    Honest ceiling: "examined" means cited or opened (presence), never that the
    examination was deep or correct. The map measures breadth of attention over a
    fixed denominator; it does not certify the audit found everything.
    """
    cache = _read_cache(session_id)
    scope = cache.get("coverage_scope")
    if not scope or not scope.get("frozen_at"):
        json.dump({
            "status": "no_scope",
            "message": "Freeze an investigation scope first via "
                       "`update <sid> --freeze-scope '{\"files\": [...]}'`.",
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    scope_files = set(scope.get("files", []))
    examined = _examined_files(cache)
    in_scope = examined & scope_files
    out_of_scope = examined - scope_files
    unexamined = scope_files - examined
    scope_total = len(scope_files)

    span_budget = scope.get("span_budget")
    examined_total = len(examined)
    over_budget = span_budget is not None and examined_total > span_budget
    span_remaining = max(0, span_budget - examined_total) if span_budget is not None else None

    report = {
        "status": "coverage_map",
        "frozen_at": scope.get("frozen_at"),
        "source": scope.get("source", ""),
        "scope_total": scope_total,
        "examined_in_scope": len(in_scope),
        "coverage_pct": round(len(in_scope) / scope_total * 100) if scope_total else 100,
        "unexamined_count": len(unexamined),
        "unexamined": sorted(unexamined)[:_COVERAGE_SAMPLE_MAX],
        "out_of_scope_count": len(out_of_scope),
        "out_of_scope_examined": sorted(out_of_scope)[:_COVERAGE_SAMPLE_MAX],
        "files_examined_total": examined_total,
        "span_budget": span_budget,
        "over_span_budget": over_budget,
        "span_remaining": span_remaining,
        "ceiling": "examined = cited or opened (presence); not a proof of depth or correctness",
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _parse_findings(raw: str) -> list[dict]:
    """Parse run-analysis.sh output: a JSON array, a single object, or NDJSON."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [f for f in data if isinstance(f, dict)]
        if isinstance(data, dict):
            return [data]
    except (ValueError, json.JSONDecodeError):
        pass
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except (ValueError, json.JSONDecodeError):
            continue
    return out


def cmd_record_analysis(session_id: str, file: str) -> None:
    """INV-5: record one analyzed file as an examined file-citation.

    Reads a run-analysis.sh JSON array (or NDJSON) from stdin, summarizes the
    findings, and (1) adds the file to the unbounded `pretool_queried_files`
    examined set -- the authoritative coverage signal that survives at scale --
    and (2) appends a bounded `citation_log` file row carrying the findings detail.
    A clean file (no findings) is still recorded: examined is presence of
    attention, not of defects.
    """
    findings = _parse_findings(sys.stdin.read())
    total = len(findings)
    errors = sum(1 for f in findings if str(f.get("severity", "")).lower() == "error")
    rules = sorted({str(f.get("rule")) for f in findings if f.get("rule")})
    excerpt = "0 findings (clean)" if total == 0 else (
        f"{total} findings ({errors} error): " + ", ".join(rules[:5])
    )

    with mutate_cache(session_id) as cache:
        examined = set(cache.get("pretool_queried_files", []))
        examined.add(file)
        cache["pretool_queried_files"] = sorted(examined)
        _append_citation(cache, {
            "artifact_type": "file",
            "ref": file,
            "excerpt": excerpt,
            "findings": total,
            "errors": errors,
        }, session_id)
    json.dump({"status": "recorded", "file": file, "findings": total, "errors": errors}, sys.stdout)
    sys.stdout.write("\n")


def cmd_synthesis_gate(session_id: str) -> None:
    """INV-5: presence synthesis gate for the audit/investigation lens.

    `ready` iff a coverage scope is frozen AND at least one in-scope file has been
    examined. The gate asserts the PRESENCE of coverage evidence to synthesize
    from -- never that the audit is complete or correct. `coverage_pct`,
    `unexamined_count`, and `full_coverage` are surfaced so a human/orchestrator
    judges SUFFICIENCY. Advisory (investigate mode's gate_strictness), not a hard block.
    """
    cache = _read_cache(session_id)
    scope = cache.get("coverage_scope")
    frozen = bool(scope and scope.get("frozen_at"))
    scope_files = set(scope.get("files", [])) if frozen else set()
    examined = _examined_files(cache)
    examined_in_scope = len(examined & scope_files)
    scope_total = len(scope_files)
    span_budget = scope.get("span_budget") if frozen else None
    over_budget = span_budget is not None and len(examined) > span_budget

    if not frozen:
        ready, reason = False, "no frozen scope: freeze an investigation scope before synthesizing"
    elif examined_in_scope == 0:
        ready, reason = False, "no in-scope files examined yet: gather evidence before synthesizing"
    else:
        ready, reason = True, "coverage evidence present (presence floor); judge sufficiency via coverage_pct"

    report = {
        "status": "synthesis_gate",
        "ready": ready,
        "reason": reason,
        "scope_total": scope_total,
        "examined_in_scope": examined_in_scope,
        "coverage_pct": round(examined_in_scope / scope_total * 100) if scope_total else 0,
        "unexamined_count": max(0, scope_total - examined_in_scope),
        "full_coverage": scope_total > 0 and examined_in_scope == scope_total,
        "over_span_budget": over_budget,
        "ceiling": "presence of coverage evidence, not proof the audit is complete or correct",
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _file_loc(path: str) -> int:
    """INV-6a: line count of a file (0 if unreadable). The partitioning unit."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _emit_no_scope_error() -> None:
    """Emit the standard 'no frozen scope' error JSON to stdout. Single source
    for the message shared by the scope-estimate and partition-scope commands."""
    json.dump({"status": "no_scope",
               "message": "Freeze a scope first via `update <sid> --freeze-scope ...`."},
              sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_scope_estimate(session_id: str, budget_loc: int = _AUDIT_BUDGET_LOC) -> None:
    """INV-6a: size the frozen scope and recommend a worker count.

    Tells the lead "this is N files / L LOC; at budget B you need ~K workers" so the
    fan-out width is computed, not eyeballed. recommended_workers = ceil(total_loc / budget).
    """
    cache = _read_cache(session_id)
    scope = cache.get("coverage_scope")
    if not scope or not scope.get("frozen_at"):
        _emit_no_scope_error()
        return
    files = list(scope.get("files", []))
    sizes = [(f, _file_loc(f)) for f in files]
    total_loc = sum(s for _, s in sizes)
    by_ext: dict = {}
    for f, s in sizes:
        ext = os.path.splitext(f)[1].lower() or "(none)"
        e = by_ext.setdefault(ext, {"files": 0, "loc": 0})
        e["files"] += 1
        e["loc"] += s
    largest = max(sizes, key=lambda x: x[1], default=(None, 0))
    if total_loc > 0 and budget_loc > 0:
        recommended = (total_loc + budget_loc - 1) // budget_loc
    else:
        recommended = 1 if files else 0
    report = {
        "status": "scope_estimate",
        "file_count": len(files),
        "total_loc": total_loc,
        "budget_loc": budget_loc,
        "recommended_workers": recommended,
        "largest_file": ({"file": largest[0], "loc": largest[1]} if largest[0] else None),
        "by_ext": by_ext,
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_partition_scope(session_id: str, max_loc: int = _AUDIT_BUDGET_LOC,
                        max_files: int = _AUDIT_BUDGET_FILES) -> None:
    """INV-6a: tile the frozen scope into per-worker partitions within a context budget.

    First-fit-decreasing over a deterministically sorted file list: every scope file lands
    in exactly one partition; each partition stays within max_loc AND max_files. A single
    file over max_loc gets its own partition flagged `oversized` (the recursion trigger:
    that worker re-estimates and re-partitions). The lead spawns one worker per partition.
    """
    cache = _read_cache(session_id)
    scope = cache.get("coverage_scope")
    if not scope or not scope.get("frozen_at"):
        _emit_no_scope_error()
        return
    files = list(scope.get("files", []))
    sizes = sorted(((f, _file_loc(f)) for f in files), key=lambda x: (-x[1], x[0]))
    partitions: list[dict] = []
    for f, loc in sizes:
        if loc > max_loc:
            partitions.append({"files": [f], "loc": loc, "count": 1, "oversized": True})
            continue
        placed = False
        for p in partitions:
            if p["oversized"]:
                continue
            if p["loc"] + loc <= max_loc and p["count"] < max_files:
                p["files"].append(f)
                p["loc"] += loc
                p["count"] += 1
                placed = True
                break
        if not placed:
            partitions.append({"files": [f], "loc": loc, "count": 1, "oversized": False})
    for i, p in enumerate(partitions):
        p["files"] = sorted(p["files"])
        p["index"] = i
    report = {
        "status": "partition_scope",
        "partition_count": len(partitions),
        "budget": {"max_loc": max_loc, "max_files": max_files},
        "total_files": len(files),
        "total_loc": sum(loc for _, loc in sizes),
        "partitions": partitions,
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_coverage_rollup(session_id: str) -> None:
    """INV-6a: aggregate worker coverage-maps (stdin JSON array) into global coverage.

    The stdin maps give the partition shape and each worker's CLAIM; examined is computed
    from the lead's own recorded evidence (`_examined_files` over its cache, which holds
    the worker reads SubagentStop rolled up), so rollup and synthesis-gate agree by
    construction. A claim is reconciled only when every examination it asserts is backed:
    the tiling held (partition scope_totals sum to the lead's frozen scope) and no claim,
    global or per partition, exceeds its evidence. Under-claiming asserts nothing false.
    A map carrying `files` is checked per partition; a file-less map cannot be.
    Counts PRESENCE signals -- breadth over a tiled denominator, never depth or correctness.
    """
    raw = sys.stdin.read().strip()
    try:
        maps = json.loads(raw) if raw else []
    except (ValueError, json.JSONDecodeError):
        maps = []
    if isinstance(maps, dict):
        maps = [maps]
    maps = [m for m in maps if isinstance(m, dict)]

    g_scope = sum(int(m.get("scope_total", 0) or 0) for m in maps)
    g_claimed = sum(int(m.get("examined_in_scope", 0) or 0) for m in maps)

    cache = _read_cache(session_id)
    scope = cache.get("coverage_scope")
    frozen = bool(scope and scope.get("frozen_at"))
    scope_files = set(scope.get("files", [])) if frozen else set()
    lead_total = len(scope.get("files", [])) if frozen else None
    evidenced_in_scope = scope_files & _examined_files(cache)
    g_evidenced = len(evidenced_in_scope)

    partitions = [_rollup_partition(i, m, scope_files, evidenced_in_scope)
                  for i, m in enumerate(maps)]
    unbacked_partitions = [
        p["index"] for p in partitions
        if p["evidenced_examined_in_scope"] is not None
        and p["claimed_examined_in_scope"] > p["evidenced_examined_in_scope"]
    ]
    checked_pcts = [p["evidenced_pct"] for p in partitions if p["evidenced_pct"] is not None]
    unbacked_claims = max(0, g_claimed - g_evidenced)
    tiling_held = lead_total is not None and g_scope == lead_total

    report = {
        "status": "coverage_rollup",
        "partitions_reported": len(maps),
        "global_scope_total": g_scope,
        "global_examined_in_scope": g_evidenced,
        "global_claimed_examined_in_scope": g_claimed,
        "global_coverage_pct": round(g_evidenced / lead_total * 100) if lead_total else 0,
        "lead_scope_total": lead_total,
        "unbacked_claims": unbacked_claims,
        "unbacked_partitions": unbacked_partitions,
        "tiling_held": tiling_held,
        "reconciled": tiling_held and unbacked_claims == 0 and not unbacked_partitions,
        "worst_partition_pct": min(checked_pcts) if checked_pcts else None,
        "partitions": partitions,
        "ready": frozen and g_evidenced > 0,
        "ceiling": "counts recorded presence (cited or opened) in the lead's evidence; "
                   "not proof of depth or correctness",
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _rollup_partition(index: int, cov_map: dict, scope_files: set[str],
                      evidenced_in_scope: set[str]) -> dict:
    """One coverage-rollup partition row: the worker's claim beside its evidence.

    Evidence per partition needs the map's `files`; without them it is None.
    """
    scope_total = int(cov_map.get("scope_total", 0) or 0)
    files = cov_map.get("files")
    evidenced = pct = None
    if isinstance(files, list):
        evidenced = len(set(files) & scope_files & evidenced_in_scope)
        denominator = scope_total or len(files)
        pct = round(evidenced / denominator * 100) if denominator else 0
    return {
        "index": index,
        "scope_total": scope_total,
        "claimed_examined_in_scope": int(cov_map.get("examined_in_scope", 0) or 0),
        "evidenced_examined_in_scope": evidenced,
        "evidenced_pct": pct,
    }


def cmd_aggregate_findings(session_id: str) -> None:
    """INV-6b: aggregate worker reports (stdin JSON array) for the lead's synthesis.

    Each report = {partition_index, coverage_map, findings:[{ref, rule, severity,
    message, subject?, stance?}]}. Produces: deduped findings by (ref, rule, message);
    by_severity / by_rule counts; CONTRADICTIONS (a subject with >=2 distinct stances
    across workers); and a coverage-aware ATTENTION ranking (low-coverage + high-error
    partitions first). Contradictions are surfaced, never adjudicated -- truth-deciding
    is triangulation (INV-7) / human work.
    """
    raw = sys.stdin.read().strip()
    reports = _parse_reports(raw)
    total, deduped, by_severity, by_rule, subjects, ranking = _aggregate_reports(reports)
    contradictions = _extract_contradictions(subjects)
    ranking.sort(key=lambda r: (-r["attention_score"], r["partition_index"]))

    out = {
        "status": "aggregate_findings",
        "workers": len(reports),
        "total_findings": total,
        "deduped_findings": len(deduped),
        "by_severity": by_severity,
        "by_rule": by_rule,
        "contradictions": contradictions,
        "attention_ranking": ranking,
        "ceiling": "merges and compares presence signals; contradictions are surfaced, not adjudicated",
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _parse_reports(raw: str) -> list[dict]:
    """Parse the stdin JSON into a list of report dicts (a bare dict is wrapped; non-dicts dropped)."""
    try:
        reports = json.loads(raw) if raw else []
    except (ValueError, json.JSONDecodeError):
        reports = []
    if isinstance(reports, dict):
        reports = [reports]
    return [r for r in reports if isinstance(r, dict)]


def _aggregate_reports(reports: list[dict]) -> tuple:
    """Walk all worker reports: dedup findings by (ref, rule, message), accumulate by_severity /
    by_rule counts, per-subject stance sets (for contradiction detection), and a per-partition
    attention ranking. Returns (total, deduped, by_severity, by_rule, subjects, ranking)."""
    total = 0
    seen: set = set()
    deduped: list = []
    by_severity: dict = {}
    by_rule: dict = {}
    subjects: dict = {}
    ranking: list = []

    for w, report in enumerate(reports):
        idx = report.get("partition_index", w)
        cov = report.get("coverage_map") or {}
        cov_pct = cov.get("coverage_pct", 0) or 0
        findings = report.get("findings") or []
        if not isinstance(findings, list):
            findings = []
        worker_errors = 0
        for f in findings:
            if not isinstance(f, dict):
                continue
            total += 1
            sev = str(f.get("severity", "")).lower()
            if sev == "error":
                worker_errors += 1
            key = (f.get("ref"), f.get("rule"), f.get("message"))
            if key not in seen:
                seen.add(key)
                deduped.append(f)
                by_severity[sev] = by_severity.get(sev, 0) + 1
                rule = f.get("rule")
                if rule:
                    by_rule[rule] = by_rule.get(rule, 0) + 1
            subj, stance = f.get("subject"), f.get("stance")
            if subj is not None and stance is not None:
                s = subjects.setdefault(subj, {"stances": {}, "refs": []})
                s["stances"].setdefault(stance, set()).add(idx)
                if f.get("ref"):
                    s["refs"].append(f.get("ref"))
        attention = (100 - cov_pct) + worker_errors * _ATTENTION_ERROR_WEIGHT + len(findings)
        ranking.append({
            "partition_index": idx,
            "coverage_pct": cov_pct,
            "findings": len(findings),
            "errors": worker_errors,
            "attention_score": attention,
        })
    return total, deduped, by_severity, by_rule, subjects, ranking


def _extract_contradictions(subjects: dict) -> list:
    """A subject carrying >=2 distinct stances across workers is a contradiction (surfaced,
    never adjudicated). Sorted by subject for deterministic output."""
    contradictions = []
    for subj in sorted(subjects):
        s = subjects[subj]
        if len(s["stances"]) >= 2:
            contradictions.append({
                "subject": subj,
                "stances": sorted(s["stances"].keys()),
                "sources": sorted({src for srcs in s["stances"].values() for src in srcs}),
                "refs": sorted(set(s["refs"])),
            })
    return contradictions


def _independent_domain(url: str) -> str:
    """INV-7a: registrable domain (eTLD+1 heuristic) for source-independence counting.

    Lowercases the host, strips userinfo/port and a leading `www.`, and keeps the last
    two dot-labels so `docs.python.org` and `www.python.org` collapse to `python.org`
    (one site -> one independent source). A coarse heuristic; multi-label public suffixes
    (e.g. co.uk) are not special-cased -- acceptable for a corroboration floor.
    """
    from urllib.parse import urlparse
    netloc = urlparse(url if "://" in url else "//" + url, scheme="").netloc.lower()
    host = netloc.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    labels = [seg for seg in host.split(".") if seg]
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def cmd_triangulation_gate(session_id: str) -> None:
    """INV-7a: the hard, fail-closed research corroboration gate.

    Counts INDEPENDENT source domains among the url citations. `triangulated` iff at
    least _TRIANGULATION_MIN_DOMAINS independent domains corroborate; otherwise `blocked`
    -- a stop signal, not a nudge. Fail-closed: zero captured url sources -> blocked.

    Ceiling: proves >=2 independent sources were captured and agree enough to cite, never
    that the claim is true (independent sources can share an upstream error).
    """
    cache = _read_cache(session_id)
    urls = [r for r in cache.get("citation_log", [])
            if r.get("artifact_type") == "url" and r.get("ref")]
    domains = sorted({d for d in (_independent_domain(r["ref"]) for r in urls) if d})
    triangulated = len(domains) >= _TRIANGULATION_MIN_DOMAINS
    if not urls:
        reason = "fail-closed, no url citations captured: a web synthesis needs captured sources"
    elif not triangulated:
        reason = (f"only {len(domains)} independent domain(s); "
                  f">= {_TRIANGULATION_MIN_DOMAINS} required to corroborate")
    else:
        reason = f"{len(domains)} independent domains corroborate"
    report = {
        "status": "triangulation_gate",
        "required": _TRIANGULATION_MIN_DOMAINS,
        "url_citations": len(urls),
        "independent_domains": domains,
        "domain_count": len(domains),
        "triangulated": triangulated,
        "blocked": not triangulated,
        "reason": reason,
        "ceiling": "proves >=2 independent sources were captured and agree enough to cite; not proof the claim is true",
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_staleness_check(session_id: str) -> None:
    """INV-7a: flag url sources whose captured content changed between fetches.

    Groups url citations by `ref`; a ref captured with >=2 distinct `excerpt_hash` values
    has drifted (the page changed since an earlier capture). Hash-based and deterministic
    -- no wall-clock comparison, so it cannot time-bomb. Detects that the text changed,
    never which version is correct.
    """
    cache = _read_cache(session_id)
    by_ref: dict = {}
    for r in cache.get("citation_log", []):
        if r.get("artifact_type") == "url" and r.get("ref"):
            by_ref.setdefault(r["ref"], []).append(r.get("excerpt_hash"))
    drifted = []
    for ref in sorted(by_ref):
        hashes = by_ref[ref]
        distinct = sorted({h for h in hashes if h})
        if len(distinct) >= 2:
            drifted.append({"ref": ref, "captures": len(hashes), "hashes": distinct})
    report = {
        "status": "staleness_check",
        "urls_tracked": len(by_ref),
        "drifted": drifted,
        "drift_count": len(drifted),
        "ceiling": "detects that captured text changed between fetches; not which version is correct",
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_lens(session_id: str) -> None:
    """INV-8: the one switch. Map the active source_type to its lens + enforcing gate.

    audit/explore (code) -> synthesis-gate (advisory); research (web) -> triangulation-gate
    (hard); debug (runtime) -> root-cause (advisory). The source_type (the lens), not the
    mode, owns strictness. Reports which gate enforces the lens -- not that the
    investigation is correct (per-lens ceilings still apply).
    """
    cache = _read_cache(session_id)
    st = _effective_source_type(cache)
    entry = _LENS_TABLE.get(st or "")
    if not entry:
        json.dump({
            "status": "no_lens",
            "source_type": st,
            "message": "No investigation lens active. Set one with "
                       "`update <sid> --set-source-type <code|web|runtime>`, or use debug mode.",
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    report = {
        "status": "lens",
        "source_type": st,
        "lens": entry["lens"],
        "enforcing_gate": entry["enforcing_gate"],
        "gate_strictness": entry["gate_strictness"],
        "note": "reports which gate enforces this lens; not a proof the investigation is correct",
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
