"""FIX-4: enforcement-tag + comment consistency (audit findings #3, #7).

Three violation tags the hooks emit -- ENF-GATE-006, ENF-POST-006 (validate-handoff.sh),
ENF-TEST-001 (writ-run-pending-tests.sh) -- had no corpus rule. FIX-4 authors them in
bible/enforcement/reasoning-discipline.md (where their siblings live) and re-cites two
comment refs that named non-existent IDs (ARCH-DRY-001, ARCH-ORG-001).
"""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path

import pytest

from writ.graph.ingest import parse_nodes_from_file, validate_parsed_node

from tests._bible_guard import requires_bible

pytestmark = requires_bible


SKILL = Path(__file__).resolve().parent.parent
REASONING = SKILL / "bible" / "enforcement" / "reasoning-discipline.md"
WRIT_SESSION = SKILL / "bin" / "lib" / "writ-session.py"
HANDOFF_HOOK = SKILL / "hooks" / "scripts" / "validate-handoff.sh"
PENDING_HOOK = SKILL / "hooks" / "scripts" / "writ-run-pending-tests.sh"

NEW_RULES = {
    "ENF-GATE-006": "validate-handoff.sh",
    "ENF-POST-006": "validate-handoff.sh",
    "ENF-TEST-001": "writ-run-pending-tests.sh",
}
ID_RE = re.compile(r"\b([A-Z]{2,}(?:-[A-Z0-9]+){1,}-\d{3})\b")


def _defined() -> set[str]:
    defined: set[str] = set()
    for f in glob.glob(str(SKILL / "bible" / "**" / "*.md"), recursive=True):
        txt = Path(f).read_text()
        defined |= set(re.findall(r"RULE START:\s*(\S+)", txt))
        defined |= set(re.findall(r"^##\s+Rule\s+(\S+)", txt, re.M))
        defined |= set(re.findall(r"^(?:rule|skill|playbook|technique|antipattern|forbidden|phase|rationalization|scenario|example|role)_id:\s*(\S+)", txt, re.M))
        if os.path.dirname(f).endswith("methodology"):
            defined.add(os.path.basename(f)[:-3])
    return defined


def _rule_block(text: str, rule_id: str) -> str:
    m = re.search(rf"RULE START:\s*{re.escape(rule_id)}\s*-->(.*?)<!--\s*RULE END:\s*{re.escape(rule_id)}", text, re.S)
    return m.group(1) if m else ""


class TestEnforcementRulesDefined:
    def test_three_rules_present_in_corpus(self) -> None:
        defined = _defined()
        missing = [r for r in NEW_RULES if r not in defined]
        assert not missing, f"these hook-emitted tags still have no corpus rule: {missing}"

    def test_parse_as_valid_rule_nodes(self) -> None:
        nodes = {n.get("rule_id"): n for n in parse_nodes_from_file(REASONING)}
        for rid in NEW_RULES:
            assert rid in nodes, f"{rid} not parsed from reasoning-discipline.md"
            node = nodes[rid]
            assert node.get("node_type") == "Rule"
            assert node.get("statement"), f"{rid} must have a Statement"
            assert node.get("trigger"), f"{rid} must have a Trigger"
            validate_parsed_node(node)

    def test_each_rule_names_its_enforcing_hook(self) -> None:
        text = REASONING.read_text()
        for rid, hook in NEW_RULES.items():
            block = _rule_block(text, rid)
            assert block, f"{rid} block not found"
            assert hook in block, f"{rid} must name its enforcing hook ({hook})"


class TestCommentRefsResolve:
    def test_writ_session_comment_uses_real_id(self) -> None:
        # POL-6a moved the budget block (and its DRY-CONFIG-001 citation) from
        # writ-session.py into writ/session/config.py; the citation followed the code.
        src = WRIT_SESSION.read_text()
        config_src = (SKILL / "writ" / "session" / "config.py").read_text()
        assert "ARCH-DRY-001" not in src, "writ-session.py must not cite the non-existent ARCH-DRY-001"
        assert "ARCH-DRY-001" not in config_src, "config.py must not cite the non-existent ARCH-DRY-001"
        assert "DRY-CONFIG-001" in config_src, "config.py comment should cite the real DRY-CONFIG-001"


class TestNoDanglingHookTags:
    def test_affected_files_have_no_unresolved_rule_ids(self) -> None:
        defined = _defined()
        unresolved: dict[str, list[str]] = {}
        for f in (HANDOFF_HOOK, PENDING_HOOK, WRIT_SESSION):
            for rid in set(ID_RE.findall(f.read_text())):
                if rid not in defined:
                    unresolved.setdefault(rid, []).append(f.name)
        assert not unresolved, f"unresolved rule-ID references remain: {unresolved}"
