"""Core analysis orchestration for POST /analyze.

Coordinates the retrieval pipeline, pattern matching, LLM escalation,
and calibration instrumentation to produce a structured compliance verdict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from writ.analysis import AnalyzeResponse, Finding
from writ.analysis.instrumentation import Instrumentation, _derive_verdict
from writ.analysis.llm import LlmAnalyzer
from writ.analysis.patterns import extract_violations, scan_code

if TYPE_CHECKING:
    from writ.retrieval.pipeline import RetrievalPipeline


async def run_analysis(
    code: str,
    file_path: str,
    phase: str,
    context: str,
    pipeline: RetrievalPipeline,
    llm_client: LlmAnalyzer,
    instrumentation: Instrumentation,
) -> AnalyzeResponse:
    """Execute the full analysis pipeline.

    1. Retrieve relevant rules via the existing RAG pipeline
    2. Run pattern matching with confidence scoring
    3. Decide whether to escalate to LLM
    4. If calibration mode: always run both, log paired results
    5. Build verdict from combined findings
    """
    # Step 1: Retrieve rules
    query_text = f"{context} {file_path}"
    retrieval_result = pipeline.query(query_text=query_text)
    rules = retrieval_result.get("rules", [])
    retrieval_scores = {r["rule_id"]: r.get("score", 0.0) for r in rules}
    rules_checked = list(retrieval_scores.keys())

    if not rules:
        return AnalyzeResponse(
            verdict="pass",
            findings=[],
            rules_checked=[],
            analysis_method="pattern",
            retrieval_scores={},
            summary="No relevant rules found for this code.",
        )

    # Step 2: Pattern matching
    violation_patterns = extract_violations(rules)
    pattern_findings = scan_code(code, violation_patterns)

    # Step 3: Escalation decision
    mode = instrumentation.get_mode()
    should_escalate = instrumentation.should_escalate(pattern_findings, retrieval_scores)

    # Cap rules sent to LLM at top 10 by retrieval score
    from writ.analysis.llm import MAX_RULES_PER_CALL
    llm_rules = sorted(rules, key=lambda r: r.get("score", 0), reverse=True)[:MAX_RULES_PER_CALL]

    llm_findings: list[Finding] = []
    analysis_method = "pattern"

    if mode == "calibration":
        # Always run both, log paired comparison
        llm_findings = await llm_client.analyze(
            code=code,
            rules=llm_rules,
            phase=phase,
            file_path=file_path,
            pattern_findings=pattern_findings,
        )
        instrumentation.log_calibration(
            file_path=file_path,
            phase=phase,
            pattern_findings=pattern_findings,
            llm_findings=llm_findings,
            rules_checked=rules_checked,
            retrieval_scores=retrieval_scores,
        )
        analysis_method = "calibration"
    elif should_escalate:
        llm_findings = await llm_client.analyze(
            code=code,
            rules=llm_rules,
            phase=phase,
            file_path=file_path,
            pattern_findings=pattern_findings,
        )
        if pattern_findings:
            analysis_method = "hybrid"
        else:
            analysis_method = "llm"

    # Step 4: Build verdict
    # In calibration mode, LLM verdict is authoritative
    if mode == "calibration" and llm_findings:
        all_findings = llm_findings
    elif llm_findings:
        # Hybrid: LLM findings replace pattern findings for rules the LLM checked
        llm_rule_ids = {f.rule_id for f in llm_findings}
        all_findings = [f for f in pattern_findings if f.rule_id not in llm_rule_ids]
        all_findings.extend(llm_findings)
    else:
        all_findings = pattern_findings

    verdict = _compute_verdict(all_findings, should_escalate and not llm_findings)
    violation_count = sum(1 for f in all_findings if f.status == "violated")
    total_checked = len(rules_checked)

    if verdict == "pass":
        summary = f"No violations found across {total_checked} rules checked."
    elif verdict == "warn":
        summary = f"{violation_count} potential issues found but unconfirmed. Manual review recommended."
    else:
        summary = f"{violation_count} violation(s) found across {total_checked} rules checked."

    return AnalyzeResponse(
        verdict=verdict,
        findings=all_findings,
        rules_checked=rules_checked,
        analysis_method=analysis_method,
        retrieval_scores=retrieval_scores,
        summary=summary,
    )


def _compute_verdict(findings: list[Finding], escalation_failed: bool) -> str:
    """Derive verdict from findings.

    fail: at least one confirmed violation (high confidence or LLM-confirmed)
    warn: uncertain findings, or medium/low confidence without LLM resolution
    pass: no violations
    """
    if not findings:
        return "pass"

    statuses = {f.status for f in findings}

    if "violated" in statuses:
        # Check if all violations are uncertain confidence without LLM
        violated_findings = [f for f in findings if f.status == "violated"]
        all_ambiguous = all(
            f.source == "pattern" and f.confidence in ("medium", "low")
            for f in violated_findings
        )
        if all_ambiguous and escalation_failed:
            return "warn"
        return "fail"

    if "uncertain" in statuses:
        return "warn"

    return "pass"
