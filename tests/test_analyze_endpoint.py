"""Integration tests for POST /analyze endpoint.

C1: /analyze endpoint accepts code + phase + context, returns verdict
C6: validate-rules.sh calls /analyze instead of inline pattern matching
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from writ.analysis import AnalyzeRequest, AnalyzeResponse, Finding
from writ.analysis.llm import LlmAnalyzer
from writ.analysis.instrumentation import Instrumentation


# ── C1, C6: Endpoint integration ─────────────────────────────────────────────


class TestAnalyzeEndpoint:
    """Tests using run_analysis directly (no HTTP server needed)."""

    def _make_pipeline_mock(self, rules=None):
        mock = MagicMock()
        if rules is None:
            rules = [{"rule_id": "SEC-UNI-003", "score": 0.9,
                       "violation": "$customer->__toArray();",
                       "statement": "Never return full entity"}]
        mock.query.return_value = {
            "rules": rules,
            "mode": "standard",
            "total_candidates": len(rules),
            "latency_ms": 1.0,
        }
        return mock

    def _make_llm_mock(self):
        mock = AsyncMock(spec=LlmAnalyzer)
        mock.analyze = AsyncMock(return_value=[])
        return mock

    def _make_instrumentation(self):
        mock = MagicMock(spec=Instrumentation)
        mock.get_mode.return_value = "production"
        mock.should_escalate.return_value = False
        return mock

    @pytest.mark.asyncio
    async def test_endpoint_returns_valid_response(self):
        from writ.analysis.analyzer import run_analysis
        result = await run_analysis(
            code="<?php class Foo {}",
            file_path="Foo.php",
            phase="code_generation",
            context="PHP Magento 2",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        assert isinstance(result, AnalyzeResponse)
        assert result.verdict in ("pass", "fail", "warn")

    @pytest.mark.asyncio
    async def test_endpoint_returns_analyze_response_schema(self):
        from writ.analysis.analyzer import run_analysis
        result = await run_analysis(
            code="<?php class Foo {}",
            file_path="Foo.php",
            phase="code_generation",
            context="PHP",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        d = result.model_dump()
        assert "verdict" in d
        assert "findings" in d
        assert "rules_checked" in d
        assert "analysis_method" in d
        assert "retrieval_scores" in d
        assert "summary" in d

    @pytest.mark.asyncio
    async def test_endpoint_returns_pass_for_no_rules(self):
        from writ.analysis.analyzer import run_analysis
        result = await run_analysis(
            code="<?php class Foo {}",
            file_path="Foo.php",
            phase="code_generation",
            context="PHP",
            pipeline=self._make_pipeline_mock(rules=[]),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        assert result.verdict == "pass"
        assert result.rules_checked == []

    @pytest.mark.asyncio
    async def test_endpoint_handles_empty_code(self):
        from writ.analysis.analyzer import run_analysis
        result = await run_analysis(
            code="",
            file_path="empty.php",
            phase="code_generation",
            context="PHP",
            pipeline=self._make_pipeline_mock(),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        assert result.verdict == "pass"

    @pytest.mark.asyncio
    async def test_endpoint_populates_retrieval_scores(self):
        from writ.analysis.analyzer import run_analysis
        rules = [{"rule_id": "R1", "score": 0.85, "violation": ""}]
        result = await run_analysis(
            code="<?php class Foo {}",
            file_path="Foo.php",
            phase="code_generation",
            context="PHP",
            pipeline=self._make_pipeline_mock(rules=rules),
            llm_client=self._make_llm_mock(),
            instrumentation=self._make_instrumentation(),
        )
        assert "R1" in result.retrieval_scores
        assert isinstance(result.retrieval_scores["R1"], float)

    @pytest.mark.asyncio
    async def test_request_validation(self):
        req = AnalyzeRequest(
            code="<?php class Foo {}",
            file_path="Foo.php",
            phase="code_generation",
            context="PHP",
        )
        assert req.code == "<?php class Foo {}"
        assert req.phase == "code_generation"


# ── Context construction ──────────────────────────────────────────────────────


class TestContextConstruction:
    """Verify context assembly logic (tested via the Python inline in the hook)."""

    def _build_context(self, file_path: str, project_markers: dict | None = None) -> str:
        """Simulate the context construction from the hook."""
        import os

        ext_map = {
            '.php': 'PHP', '.py': 'Python', '.js': 'JavaScript', '.ts': 'TypeScript',
            '.xml': 'XML', '.graphqls': 'GraphQL',
        }
        ext = os.path.splitext(file_path)[1]
        lang = ext_map.get(ext, 'unknown')

        framework = project_markers.get("framework", "") if project_markers else ""

        role = 'source file'
        path_lower = file_path.lower()
        if '/test/' in path_lower or '/tests/' in path_lower:
            role = 'unit test'
        elif '/api/data/' in path_lower:
            role = 'DTO interface'
        elif '/api/' in path_lower:
            role = 'service contract'
        elif '/model/data/' in path_lower:
            role = 'DTO implementation'
        elif '/model/' in path_lower:
            role = 'service implementation'
        elif '/etc/' in path_lower:
            if 'di.xml' in path_lower:
                role = 'dependency injection configuration'
            else:
                role = 'configuration'

        parts = [p for p in [lang, framework, role] if p]
        return ' '.join(parts)

    def test_php_magento_service_context(self):
        ctx = self._build_context(
            "app/code/Custom/Module/Model/RecentOrders.php",
            {"framework": "Magento 2"},
        )
        assert "PHP" in ctx
        assert "Magento 2" in ctx
        assert "service implementation" in ctx

    def test_xml_config_context(self):
        ctx = self._build_context(
            "app/code/Custom/Module/etc/di.xml",
            {"framework": "Magento 2"},
        )
        assert "XML" in ctx
        assert "dependency injection configuration" in ctx

    def test_python_module_context(self):
        ctx = self._build_context("src/analysis/patterns.py")
        assert "Python" in ctx

    def test_test_file_context(self):
        ctx = self._build_context(
            "app/code/Custom/Module/Test/Unit/Model/RecentOrdersTest.php",
            {"framework": "Magento 2"},
        )
        assert "PHP" in ctx
        assert "unit test" in ctx
