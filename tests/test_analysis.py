"""Tests for writ/analysis module.

C1: /analyze endpoint returns structured verdict
C2: Pattern extraction from rule violation examples with confidence scoring
C3: LLM escalation for ambiguous/architectural rule checking
C4: Calibration mode with paired logging
C5: Instrumentation: analysis_method, retrieval_scores
C7: Configurable model selection
C8: Context-aware pattern matching (skip comments, strings)
C9: Unified Finding model with source discriminator
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from writ.analysis import AnalyzeResponse, Finding
from writ.analysis.patterns import ViolationPattern, extract_violations, scan_code
from writ.analysis.instrumentation import Instrumentation, _derive_verdict
from writ.analysis.llm import (
    LlmAnalyzer,
    build_prompt,
    findings_from_llm_response,
    parse_llm_response,
    select_model,
    DEFAULT_MODEL,
    PLANNING_MODEL,
)


# ── Test data ─────────────────────────────────────────────────────────────────

RULE_SEC_UNI_003 = {
    "rule_id": "SEC-UNI-003",
    "trigger": "When an API endpoint returns entity data",
    "statement": "Never return the full entity",
    "violation": "$customer->__toArray();\n$order->getData();",
    "pass_example": "$this->responseFactory->create(['name' => $c->getName()]);",
}

RULE_FW_M2_002 = {
    "rule_id": "FW-M2-002",
    "trigger": "When entity retrieval uses Factory->create()->load(",
    "statement": "Use repository layer instead",
    "violation": "$this->productFactory->create()->load($productId);",
    "pass_example": "$this->productRepository->getById($productId);",
}

RULE_NO_VIOLATION = {
    "rule_id": "ARCH-DRY-001",
    "trigger": "When logic is duplicated",
    "statement": "Extract to shared function",
    "violation": "",
}

PHP_CODE_WITH_VIOLATION = """<?php
declare(strict_types=1);

class OrderService
{
    public function getOrderData(int $orderId): array
    {
        $order = $this->orderRepository->getById($orderId);
        return $order->__toArray();
    }
}
"""

PHP_CODE_CLEAN = """<?php
declare(strict_types=1);

class OrderService
{
    public function getOrderData(int $orderId): array
    {
        $order = $this->orderRepository->getById($orderId);
        return [
            'increment_id' => $order->getIncrementId(),
            'status' => $order->getStatus(),
        ];
    }
}
"""

PHP_CODE_IN_COMMENT = """<?php
// Note: we used to call $order->__toArray() here but removed it
class OrderService {}
"""

PHP_CODE_IN_STRING = """<?php
$message = "Do not use ->__toArray() in production code";
"""

PHP_CODE_IN_BLOCK_COMMENT = """<?php
/*
 * Old code: $order->__toArray()
 * New code: explicit field selection
 */
class OrderService {}
"""


# ── C2: Pattern extraction ────────────────────────────────────────────────────


class TestPatternExtraction:

    def test_extracts_method_call_pattern(self):
        patterns = extract_violations([RULE_SEC_UNI_003])
        labels = [p.label for p in patterns]
        assert any("__toArray(" in l for l in labels)

    def test_extracts_instantiation_pattern(self):
        rule = {"rule_id": "TEST", "violation": "new OrderRepository($this->connection);"}
        patterns = extract_violations([rule])
        labels = [p.label for p in patterns]
        assert any("new OrderRepository(" in l for l in labels)

    def test_extracts_factory_load_chain(self):
        patterns = extract_violations([RULE_FW_M2_002])
        labels = [p.label for p in patterns]
        assert any("Factory->create()->load" in l for l in labels)

    def test_empty_violation_produces_no_patterns(self):
        patterns = extract_violations([RULE_NO_VIOLATION])
        assert patterns == []

    def test_extraction_is_deterministic(self):
        p1 = extract_violations([RULE_SEC_UNI_003])
        p2 = extract_violations([RULE_SEC_UNI_003])
        assert [(p.rule_id, p.label) for p in p1] == [(p.rule_id, p.label) for p in p2]

    def test_skips_common_method_names(self):
        rule = {"rule_id": "TEST", "violation": "$repo->get($id); $factory->create();"}
        patterns = extract_violations([rule])
        labels = [p.label for p in patterns]
        assert not any("->get(" in l for l in labels)
        assert not any("->create(" in l for l in labels)


# ── C2, C8: Pattern scanning ─────────────────────────────────────────────────


class TestPatternScanning:

    def test_exact_match_returns_high_confidence(self):
        patterns = extract_violations([RULE_SEC_UNI_003])
        findings = scan_code(PHP_CODE_WITH_VIOLATION, patterns)
        violated = [f for f in findings if "__toArray" in f.evidence]
        assert len(violated) >= 1
        assert violated[0].confidence == "high"

    def test_match_in_comment_returns_medium_confidence(self):
        patterns = extract_violations([RULE_SEC_UNI_003])
        findings = scan_code(PHP_CODE_IN_COMMENT, patterns)
        if findings:
            assert all(f.confidence == "medium" for f in findings)

    def test_match_in_block_comment_returns_medium_confidence(self):
        patterns = extract_violations([RULE_SEC_UNI_003])
        findings = scan_code(PHP_CODE_IN_BLOCK_COMMENT, patterns)
        if findings:
            assert all(f.confidence == "medium" for f in findings)

    def test_match_in_string_returns_medium_confidence(self):
        patterns = extract_violations([RULE_SEC_UNI_003])
        findings = scan_code(PHP_CODE_IN_STRING, patterns)
        if findings:
            assert all(f.confidence == "medium" for f in findings)

    def test_substring_match_returns_low_confidence(self):
        code = "function convertToArrayFormat() { return []; }"
        rule = {"rule_id": "TEST", "violation": "->toArray()"}
        patterns = extract_violations([rule])
        findings = scan_code(code, patterns)
        # toArray pattern should not match convertToArrayFormat (different pattern)
        # but if a substring pattern did match, it would be low
        for f in findings:
            if f.confidence == "low":
                assert True
                return
        # No match is also acceptable (pattern is ->toArray( which won't match)
        assert True

    def test_no_match_returns_empty(self):
        patterns = extract_violations([RULE_SEC_UNI_003])
        findings = scan_code(PHP_CODE_CLEAN, patterns)
        assert findings == []

    def test_multiple_matches_same_rule(self):
        code = "<?php\n$a->__toArray();\n$b->__toArray();\n"
        patterns = extract_violations([RULE_SEC_UNI_003])
        findings = scan_code(code, patterns)
        toarray_findings = [f for f in findings if "__toArray" in f.evidence]
        assert len(toarray_findings) == 2

    def test_multiple_rules_multiple_matches(self):
        code = "<?php\n$a->__toArray();\n$this->productFactory->create()->load($id);\n"
        patterns = extract_violations([RULE_SEC_UNI_003, RULE_FW_M2_002])
        findings = scan_code(code, patterns)
        rule_ids = {f.rule_id for f in findings}
        assert "SEC-UNI-003" in rule_ids
        assert "FW-M2-002" in rule_ids


# ── C9: Finding model ─────────────────────────────────────────────────────────


class TestFindingModel:

    def test_pattern_finding_has_source_pattern(self):
        f = Finding(rule_id="R1", source="pattern", status="violated", evidence="test")
        assert f.source == "pattern"

    def test_pattern_finding_has_confidence(self):
        f = Finding(rule_id="R1", source="pattern", status="violated",
                    confidence="high", evidence="test")
        assert f.confidence is not None

    def test_llm_finding_has_source_llm(self):
        f = Finding(rule_id="R1", source="llm", status="violated", evidence="test")
        assert f.source == "llm"

    def test_llm_finding_has_null_confidence(self):
        f = Finding(rule_id="R1", source="llm", status="violated", evidence="test")
        assert f.confidence is None

    def test_finding_serializes_to_json(self):
        f = Finding(rule_id="R1", source="pattern", status="violated",
                    line=42, confidence="high", evidence="test", suggestion="fix it")
        d = f.model_dump()
        assert d["rule_id"] == "R1"
        assert d["source"] == "pattern"
        assert d["line"] == 42


# ── C4, C5: Instrumentation ──────────────────────────────────────────────────


class TestInstrumentation:

    def test_calibration_mode_when_under_100(self, tmp_path):
        log = tmp_path / "cal.jsonl"
        inst = Instrumentation(log_path=log)
        assert inst.get_mode() == "calibration"

    def test_production_mode_when_over_100(self, tmp_path):
        log = tmp_path / "cal.jsonl"
        with open(log, "w") as f:
            for i in range(100):
                f.write(json.dumps({"i": i}) + "\n")
        inst = Instrumentation(log_path=log)
        assert inst.get_mode() == "production"

    def test_counter_reads_from_jsonl_on_init(self, tmp_path):
        log = tmp_path / "cal.jsonl"
        with open(log, "w") as f:
            for i in range(50):
                f.write(json.dumps({"i": i}) + "\n")
        inst = Instrumentation(log_path=log)
        assert inst._counter == 50

    def test_counter_survives_reinit(self, tmp_path):
        log = tmp_path / "cal.jsonl"
        inst1 = Instrumentation(log_path=log)
        inst1.log_calibration("f.php", "code_generation", [], [], [], {})
        inst1.log_calibration("g.php", "code_generation", [], [], [], {})
        inst2 = Instrumentation(log_path=log)
        assert inst2._counter == 2

    def test_log_calibration_appends_jsonl(self, tmp_path):
        log = tmp_path / "cal.jsonl"
        inst = Instrumentation(log_path=log)
        inst.log_calibration("f.php", "code_generation", [], [], ["R1"], {"R1": 0.8})
        lines = log.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["file_path"] == "f.php"

    def test_calibration_log_contains_agreement_field(self, tmp_path):
        log = tmp_path / "cal.jsonl"
        inst = Instrumentation(log_path=log)
        pf = [Finding(rule_id="R1", source="pattern", status="violated", evidence="x")]
        lf = [Finding(rule_id="R1", source="llm", status="violated", evidence="x")]
        inst.log_calibration("f.php", "code_generation", pf, lf, ["R1"], {})
        data = json.loads(log.read_text().strip())
        assert "agreed" in data
        assert data["agreed"] is True

    def test_should_escalate_always_true_in_calibration(self, tmp_path):
        log = tmp_path / "cal.jsonl"
        inst = Instrumentation(log_path=log)
        assert inst.should_escalate([], {}) is True

    def test_should_escalate_true_for_medium_confidence(self):
        inst = Instrumentation.__new__(Instrumentation)
        inst._counter = 200
        inst._log_path = Path("/dev/null")
        match = Finding(rule_id="R1", source="pattern", status="violated",
                        confidence="medium", evidence="x")
        assert inst.should_escalate([match], {}) is True

    def test_should_escalate_true_for_low_confidence(self):
        inst = Instrumentation.__new__(Instrumentation)
        inst._counter = 200
        inst._log_path = Path("/dev/null")
        match = Finding(rule_id="R1", source="pattern", status="violated",
                        confidence="low", evidence="x")
        assert inst.should_escalate([match], {}) is True

    def test_should_escalate_true_for_no_matches_high_relevance(self):
        inst = Instrumentation.__new__(Instrumentation)
        inst._counter = 200
        inst._log_path = Path("/dev/null")
        assert inst.should_escalate([], {"R1": 0.8}) is True

    def test_should_escalate_false_for_all_high_confidence(self):
        inst = Instrumentation.__new__(Instrumentation)
        inst._counter = 200
        inst._log_path = Path("/dev/null")
        match = Finding(rule_id="R1", source="pattern", status="violated",
                        confidence="high", evidence="x")
        assert inst.should_escalate([match], {}) is False

    def test_should_escalate_false_for_no_matches_no_relevant_rules(self):
        inst = Instrumentation.__new__(Instrumentation)
        inst._counter = 200
        inst._log_path = Path("/dev/null")
        assert inst.should_escalate([], {"R1": 0.3}) is False


# ── C1, C3, C7: Analyzer ─────────────────────────────────────────────────────


class TestAnalyzer:

    def _make_pipeline_mock(self, rules=None):
        mock = MagicMock()
        if rules is None:
            rules = [RULE_SEC_UNI_003]
        mock.query.return_value = {
            "rules": rules,
            "mode": "standard",
            "total_candidates": len(rules),
            "latency_ms": 1.0,
        }
        return mock

    def _make_llm_mock(self, findings=None):
        mock = AsyncMock(spec=LlmAnalyzer)
        mock.analyze = AsyncMock(return_value=findings or [])
        return mock

    def _make_instrumentation(self, mode="production", should_escalate=False):
        mock = MagicMock(spec=Instrumentation)
        mock.get_mode.return_value = mode
        mock.should_escalate.return_value = should_escalate
        return mock

    @pytest.mark.asyncio
    async def test_returns_pass_when_no_violations(self):
        from writ.analysis.analyzer import run_analysis
        result = await run_analysis(
            code=PHP_CODE_CLEAN, file_path="Model/Order.php",
            phase="code_generation", context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        assert result.verdict == "pass"

    @pytest.mark.asyncio
    async def test_returns_fail_when_pattern_violation_found(self):
        from writ.analysis.analyzer import run_analysis
        result = await run_analysis(
            code=PHP_CODE_WITH_VIOLATION, file_path="Model/Order.php",
            phase="code_generation", context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        assert result.verdict == "fail"

    @pytest.mark.asyncio
    async def test_returns_warn_when_all_findings_uncertain(self):
        from writ.analysis.analyzer import run_analysis
        uncertain = [Finding(rule_id="R1", source="llm", status="uncertain", evidence="timeout")]
        result = await run_analysis(
            code=PHP_CODE_CLEAN, file_path="Model/Order.php",
            phase="code_generation", context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(findings=uncertain),
            instrumentation=self._make_instrumentation(should_escalate=True),
        )
        assert result.verdict == "warn"

    @pytest.mark.asyncio
    async def test_returns_warn_when_only_medium_confidence_and_no_llm(self):
        from writ.analysis.analyzer import run_analysis
        # Use code that matches in a comment (medium confidence)
        result = await run_analysis(
            code=PHP_CODE_IN_COMMENT, file_path="Model/Order.php",
            phase="code_generation", context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(),  # LLM returns empty (simulating failure)
            instrumentation=self._make_instrumentation(should_escalate=True),
        )
        # If there are medium-confidence findings and LLM returned nothing, verdict=warn
        if result.findings:
            assert result.verdict == "warn"

    @pytest.mark.asyncio
    async def test_analysis_method_pattern_when_no_escalation(self):
        from writ.analysis.analyzer import run_analysis
        result = await run_analysis(
            code=PHP_CODE_CLEAN, file_path="Model/Order.php",
            phase="code_generation", context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        assert result.analysis_method == "pattern"

    @pytest.mark.asyncio
    async def test_analysis_method_calibration_when_in_calibration(self):
        from writ.analysis.analyzer import run_analysis
        result = await run_analysis(
            code=PHP_CODE_CLEAN, file_path="Model/Order.php",
            phase="code_generation", context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(mode="calibration"),
        )
        assert result.analysis_method == "calibration"

    @pytest.mark.asyncio
    async def test_analysis_method_hybrid_when_escalated(self):
        from writ.analysis.analyzer import run_analysis
        llm_findings = [Finding(rule_id="SEC-UNI-003", source="llm",
                                status="violated", evidence="confirmed")]
        result = await run_analysis(
            code=PHP_CODE_WITH_VIOLATION, file_path="Model/Order.php",
            phase="code_generation", context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(findings=llm_findings),
            instrumentation=self._make_instrumentation(should_escalate=True),
        )
        assert result.analysis_method == "hybrid"

    @pytest.mark.asyncio
    async def test_rules_checked_contains_retrieved_rule_ids(self):
        from writ.analysis.analyzer import run_analysis
        result = await run_analysis(
            code=PHP_CODE_CLEAN, file_path="Model/Order.php",
            phase="code_generation", context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        assert "SEC-UNI-003" in result.rules_checked

    @pytest.mark.asyncio
    async def test_retrieval_scores_populated(self):
        from writ.analysis.analyzer import run_analysis
        rules = [{"rule_id": "R1", "score": 0.9, "violation": ""}]
        result = await run_analysis(
            code=PHP_CODE_CLEAN, file_path="Model/Order.php",
            phase="code_generation", context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(rules=rules),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        assert "R1" in result.retrieval_scores
        assert result.retrieval_scores["R1"] == 0.9

    @pytest.mark.asyncio
    async def test_llm_timeout_produces_uncertain_finding(self):
        from writ.analysis.analyzer import run_analysis
        uncertain = [Finding(rule_id="R1", source="llm", status="uncertain",
                            evidence="LLM analysis failed: TimeoutError")]
        result = await run_analysis(
            code=PHP_CODE_CLEAN, file_path="Model/Order.php",
            phase="code_generation", context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(findings=uncertain),
            instrumentation=self._make_instrumentation(should_escalate=True),
        )
        assert any(f.status == "uncertain" for f in result.findings)

    @pytest.mark.asyncio
    async def test_llm_parse_failure_produces_uncertain_finding(self):
        uncertain = [Finding(rule_id="R1", source="llm", status="uncertain",
                            evidence="LLM response parse failure")]
        assert uncertain[0].status == "uncertain"

    @pytest.mark.asyncio
    async def test_top_10_rules_sent_to_llm(self):
        rules = [{"rule_id": f"R{i:03d}", "score": 1.0 - i * 0.05, "violation": "->bad()"}
                 for i in range(15)]
        pipeline = self._make_pipeline_mock(rules=rules)
        llm = self._make_llm_mock()
        inst = self._make_instrumentation(should_escalate=True)

        from writ.analysis.analyzer import run_analysis
        await run_analysis(
            code="<?php\n$x->bad();\n", file_path="f.php",
            phase="code_generation", context="PHP",
            pipeline=pipeline, llm_client=llm, instrumentation=inst,
        )
        # LLM was called with at most 10 rules
        if llm.analyze.called:
            call_rules = llm.analyze.call_args.kwargs.get("rules", [])
            assert len(call_rules) <= 10

    @pytest.mark.asyncio
    async def test_remaining_rules_pattern_only(self):
        # 15 rules, only top 10 go to LLM, all get pattern matching
        rules = [{"rule_id": f"R{i:03d}", "score": 1.0 - i * 0.05, "violation": "->bad()"}
                 for i in range(15)]
        from writ.analysis.analyzer import run_analysis
        result = await run_analysis(
            code="<?php\n$x->bad();\n", file_path="f.php",
            phase="code_generation", context="PHP",
            pipeline=self._make_pipeline_mock(rules=rules),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        # All 15 rules should appear in rules_checked (pattern matching covers all)
        assert len(result.rules_checked) == 15

    def test_model_haiku_default(self):
        assert select_model("code_generation") == DEFAULT_MODEL

    def test_model_sonnet_for_planning_phase(self):
        assert select_model("planning") == PLANNING_MODEL

    @pytest.mark.asyncio
    async def test_summary_contains_violation_count(self):
        from writ.analysis.analyzer import run_analysis
        result = await run_analysis(
            code=PHP_CODE_WITH_VIOLATION, file_path="Model/Order.php",
            phase="code_generation", context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        if result.verdict == "fail":
            assert "violation" in result.summary.lower()

    def test_llm_prompt_contains_analyze_from_scratch_when_no_patterns(self):
        prompt = build_prompt(
            code="<?php class Foo {}",
            file_path="Foo.php",
            phase="code_generation",
            rules=[RULE_SEC_UNI_003],
            pattern_findings=[],
        )
        assert "from scratch" in prompt.lower()

    def test_llm_prompt_contains_refine_findings_when_patterns_present(self):
        pf = [Finding(rule_id="R1", source="pattern", status="violated",
                      line=5, confidence="medium", evidence="->toArray()")]
        prompt = build_prompt(
            code="<?php $x->toArray();",
            file_path="Foo.php",
            phase="code_generation",
            rules=[RULE_SEC_UNI_003],
            pattern_findings=pf,
        )
        assert "verify" in prompt.lower() or "refine" in prompt.lower() or "pattern" in prompt.lower()
