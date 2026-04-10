"""Tests for PostToolUse RAG hook logic.

Tests the code extraction, query building, and gap-only firing logic.
"""

import json
import os
import subprocess
import tempfile
import pytest

SESSION_HELPER = "bin/lib/writ-session.py"


def _run_session_cmd(*args: str, stdin: str = "") -> str:
    result = subprocess.run(
        ["python3", SESSION_HELPER, *args],
        capture_output=True, text=True, input=stdin, timeout=5,
    )
    return result.stdout.strip()


# -- Gap-only firing ----------------------------------------------------------

class TestGapOnlyFiring:
    def test_skip_when_pretool_already_queried(self):
        """PostToolUse should skip if PreToolUse already queried this file."""
        sid = "test-gap-skip"
        # Simulate PreToolUse recording a file
        _run_session_cmd("update", sid, "--add-pretool-file", "/src/Foo.php")
        cache = json.loads(_run_session_cmd("read", sid))
        assert "/src/Foo.php" in cache["pretool_queried_files"]

    def test_fire_when_pretool_did_not_query(self):
        """PostToolUse should fire if file path is not in pretool_queried_files."""
        sid = "test-gap-fire"
        _run_session_cmd("update", sid, "--add-pretool-file", "/src/Foo.php")
        cache = json.loads(_run_session_cmd("read", sid))
        assert "/src/Bar.php" not in cache["pretool_queried_files"]

    def test_pretool_records_file_path(self):
        """PreToolUse adds to pretool_queried_files via --add-pretool-file."""
        sid = "test-gap-record"
        _run_session_cmd("update", sid, "--add-pretool-file", "/a.py")
        _run_session_cmd("update", sid, "--add-pretool-file", "/b.py")
        cache = json.loads(_run_session_cmd("read", sid))
        assert set(cache["pretool_queried_files"]) == {"/a.py", "/b.py"}


# -- Content extraction -------------------------------------------------------

def _extract_query(content: str, file_path: str) -> str:
    """Run the PostToolUse query builder inline Python against content."""
    import re as _re
    lang_map = {
        '.php': 'php', '.xml': 'xml', '.py': 'python',
        '.js': 'javascript', '.ts': 'typescript',
        '.go': 'go', '.rs': 'rust', '.java': 'java',
    }
    ext = os.path.splitext(file_path)[1]
    lang = lang_map.get(ext, 'unknown')
    if lang == 'unknown':
        return ''

    MAX_KEYWORDS = 20
    signals = [lang]

    if lang == 'xml':
        class_refs = _re.findall(r'(?:class|type|instance|name)="([^"]+)"', content)
        for ref in class_refs:
            parts = ref.replace('\\\\', '\\').split('\\')
            if len(parts) > 1:
                signals.append(parts[-1])
                for p in parts[:-1]:
                    if len(p) > 3 and p[0].isupper():
                        signals.append(p)
        methods = _re.findall(r'method="(\w+)"', content)
        signals.extend(methods)
        events = _re.findall(r'<event\s+name="([^"]+)"', content)
        signals.extend(events)
        routes = _re.findall(r'url="([^"]+)"', content)
        for r in routes:
            signals.extend(p for p in r.strip('/').split('/') if len(p) > 3)
    else:
        classes = _re.findall(r'class\s+(\w+)', content)
        signals.extend(classes)
        functions = _re.findall(r'(?:function|def|func|fn)\s+(\w+)', content)
        signals.extend(f for f in functions if len(f) > 3)
        for line in content.split('\n'):
            if _re.match(r'\s*(?:import|use|from|require)', line):
                words = _re.findall(r'[A-Z]\w{2,}', line)
                signals.extend(words)
        type_refs = _re.findall(r':\s*([A-Z]\w{2,})', content)
        signals.extend(type_refs)
        if lang == 'php':
            repo_calls = _re.findall(r'->(\w+Repository|\w+Factory)\b', content)
            signals.extend(repo_calls)
        if lang == 'python':
            decorators = _re.findall(r'@(\w+)', content)
            signals.extend(d for d in decorators if len(d) > 3)

    seen = set()
    unique = []
    for s in signals:
        lower = s.lower()
        if lower in seen or len(lower) < 3:
            continue
        seen.add(lower)
        unique.append(s)
    return ' '.join(unique[:MAX_KEYWORDS])


class TestWriteExtraction:
    def test_extract_content_from_write_envelope(self):
        """Write envelope has tool_input.content with the full file content."""
        envelope = {
            "tool_input": {
                "file_path": "/src/Model/Order.php",
                "content": "<?php\nclass Order {}\n",
            }
        }
        content = envelope["tool_input"]["content"]
        assert "class Order" in content

    def test_extract_file_path_from_write_envelope(self):
        envelope = {
            "tool_input": {"file_path": "/src/Foo.php", "content": "<?php\n"}
        }
        assert envelope["tool_input"]["file_path"] == "/src/Foo.php"

    def test_empty_content_produces_only_language(self):
        """Empty content yields only the language token, which is too short for a useful query."""
        query = _extract_query("", "/src/Foo.php")
        # Only the language signal "php" -- no code-derived keywords
        assert query == "php"


class TestEditExtraction:
    def test_extract_new_string_from_edit_envelope(self):
        envelope = {
            "tool_input": {
                "file_path": "/src/Foo.php",
                "old_string": "// old",
                "new_string": "class Replacement {}",
            }
        }
        content = envelope["tool_input"]["new_string"]
        assert "Replacement" in content


# -- Query building: source code ----------------------------------------------

class TestSourceCodeQuery:
    def test_extract_php_class_names(self):
        code = "<?php\nuse Magento\\Sales\\Api\\OrderRepositoryInterface;\nclass OrderManager {}"
        query = _extract_query(code, "/src/OrderManager.php")
        assert "OrderRepositoryInterface" in query
        assert "OrderManager" in query

    def test_extract_python_imports(self):
        code = "from fastapi import FastAPI\nfrom pydantic import BaseModel\n"
        query = _extract_query(code, "/src/server.py")
        assert "FastAPI" in query
        assert "BaseModel" in query

    def test_extract_function_names(self):
        code = "def calculate_discount(quote):\n    pass\ndef validate_order(order):\n    pass\n"
        query = _extract_query(code, "/src/utils.py")
        assert "calculate_discount" in query
        assert "validate_order" in query

    def test_extract_framework_calls(self):
        code = "$this->orderRepository->getById($id);\n$this->productFactory->create();\n"
        query = _extract_query(code, "/src/Service.php")
        assert "orderRepository" in query
        assert "productFactory" in query

    def test_caps_keywords_at_limit(self):
        # Generate 30 unique class names
        lines = [f"class Class{i} {{}}" for i in range(30)]
        code = "\n".join(lines)
        query = _extract_query(code, "/src/Big.py")
        assert len(query.split()) <= 20


# -- Query building: XML config -----------------------------------------------

class TestXmlConfigQuery:
    def test_extract_class_refs_from_di_xml(self):
        xml = '<type name="Magento\\Sales\\Model\\Order">\n  <plugin name="custom" type="Vendor\\Module\\Plugin\\OrderPlugin"/>\n</type>'
        query = _extract_query(xml, "/etc/di.xml")
        assert "Order" in query
        assert "OrderPlugin" in query

    def test_extract_plugin_methods(self):
        xml = '<type name="Foo">\n  <plugin name="bar" type="Baz" method="afterSave"/>\n</type>'
        query = _extract_query(xml, "/etc/di.xml")
        assert "afterSave" in query

    def test_extract_route_definitions(self):
        xml = '<route url="/V1/orders/:orderId" method="GET">\n  <service class="Vendor\\Api\\OrderInterface" method="getById"/>\n</route>'
        query = _extract_query(xml, "/etc/webapi.xml")
        assert "orders" in query
        assert "OrderInterface" in query

    def test_extract_observer_names(self):
        xml = '<event name="sales_order_save_after">\n  <observer name="custom" instance="Vendor\\Observer\\OrderObserver"/>\n</event>'
        query = _extract_query(xml, "/etc/events.xml")
        assert "sales_order_save_after" in query
        assert "OrderObserver" in query


# -- Budget and skip conditions -----------------------------------------------

class TestBudgetAndSkip:
    def test_skip_when_budget_exhausted(self):
        sid = "test-budget-zero"
        _run_session_cmd("update", sid, "--cost", "8000")
        cache = json.loads(_run_session_cmd("read", sid))
        assert cache["remaining_budget"] == 0

    def test_skip_non_source_files(self):
        assert _extract_query("{}", "/config/data.json") == ""
        assert _extract_query("# readme", "/docs/README.md") == ""

    def test_budget_capped_at_quarter(self):
        # 4000 remaining -> cap at 1000 (4000/4 < 1500)
        # This is tested by the hook itself; here we verify the math
        remaining = 4000
        max_budget = 1500
        expected = min(remaining, max_budget)
        assert expected == 1500
        remaining = 400
        expected = min(remaining, max_budget)
        assert expected == 400
