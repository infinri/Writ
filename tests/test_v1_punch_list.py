"""v1 punch-list verification (writ-evolution.md Phase 1 leftovers).

Two changes from the evolution plan that were authored but not finished:

1. Scope vocabulary generalization (Section 2.3): coding-specific values
   `file` and `module` rename to domain-agnostic `entity` and `component`.
   `slice`, `session`, `task` stay (already abstract).

2. ENF- mandatory convention drop (Section 2.2): the parser at
   `writ/graph/ingest.py:147-148` defaulted `mandatory=True` for any
   rule_id starting with `ENF-`. The doc says: explicit `Mandatory:` field
   in the markdown, defaulting to `False`. Drop the convention; require
   explicit declaration.

Both changes are minimal-risk: scope is regex-validated (no enum
narrowing), and all existing ENF-* rules in the corpus already carry
explicit `mandatory:` declarations so the convention fallback is dead
code for current data.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from writ.graph.ingest import _parse_rule_block

from tests._bible_guard import requires_bible

pytestmark = requires_bible


SKILL_DIR = (Path(__file__).resolve().parent.parent)
BIBLE = SKILL_DIR / "bible"


class TestScopeVocabularyMigrated:
    """No coding-specific scope values (`file`, `module`) remain in the
    bible/ corpus after the v1 rename."""

    def test_no_scope_file_in_coding_rules(self) -> None:
        offenders: list[str] = []
        for md in BIBLE.rglob("*.md"):
            if "methodology" in md.parts:
                continue  # methodology corpus uses task/session already
            body = md.read_text()
            if re.search(r"^\*\*Scope\*\*:\s*file\s*$", body, re.MULTILINE):
                offenders.append(str(md.relative_to(SKILL_DIR)))
        assert not offenders, (
            "**Scope**: file is renamed to **Scope**: entity per evolution plan. "
            f"Files still using 'file':\n  " + "\n  ".join(offenders)
        )

    def test_no_scope_module_in_coding_rules(self) -> None:
        offenders: list[str] = []
        for md in BIBLE.rglob("*.md"):
            if "methodology" in md.parts:
                continue
            body = md.read_text()
            if re.search(r"^\*\*Scope\*\*:\s*module\s*$", body, re.MULTILINE):
                offenders.append(str(md.relative_to(SKILL_DIR)))
        assert not offenders, (
            "**Scope**: module is renamed to **Scope**: component per evolution plan. "
            f"Files still using 'module':\n  " + "\n  ".join(offenders)
        )

    def test_canonical_vocabulary_present(self) -> None:
        """At least one rule should now use each renamed value, proving
        the migration ran (not just deletion)."""
        all_md = "\n".join(
            md.read_text() for md in BIBLE.rglob("*.md")
            if "methodology" not in md.parts
        )
        assert re.search(r"^\*\*Scope\*\*:\s*entity\s*$", all_md, re.MULTILINE | re.IGNORECASE), (
            "no rule uses the new 'entity' scope -- migration may not have run"
        )
        assert re.search(r"^\*\*Scope\*\*:\s*component\s*$", all_md, re.MULTILINE | re.IGNORECASE), (
            "no rule uses the new 'component' scope"
        )


class TestEnfMandatoryConvention:
    """ENF- prefix no longer auto-defaults `mandatory: true`. Rules must
    declare explicitly via the YAML / **Mandatory** field."""

    def test_enf_without_explicit_mandatory_defaults_to_false(self) -> None:
        """Per writ-evolution.md Section 2.2: 'Default to false. During
        ingestion, read from the Markdown field.' The convention fallback
        on rule_id.startswith('ENF-') is removed."""
        # Synthesize a rule block with ENF- id but NO **Mandatory** field.
        block = """
**Domain**: process
**Severity**: high
**Scope**: session

### Trigger
Some trigger.

### Statement
Some statement.

### Violation
Some violation.

### Pass Example
Some pass example.

### Enforcement
Some enforcement.

### Rationale
Some rationale.
"""
        result = _parse_rule_block("ENF-NEW-NOPRIORITY-001", block)
        assert result is not None
        assert result.get("mandatory") is False, (
            "ENF- rule without explicit **Mandatory** field must default to False; "
            "the rule_id.startswith('ENF-') -> True convention is removed per "
            "writ-evolution.md Section 2.2"
        )

    def test_explicit_mandatory_true_still_works(self) -> None:
        """Explicit **Mandatory**: true overrides the new false-default."""
        block = """
**Domain**: process
**Severity**: high
**Scope**: session
**Mandatory**: true

### Trigger
Some trigger.

### Statement
Some statement.

### Violation
Some violation.

### Pass Example
Some pass example.

### Enforcement
Some enforcement.

### Rationale
Some rationale.
"""
        result = _parse_rule_block("ENF-NEW-EXPLICIT-001", block)
        assert result is not None
        assert result.get("mandatory") is True

    def test_explicit_mandatory_false_works(self) -> None:
        """Even ENF- rules can be advisory if explicitly declared."""
        block = """
**Domain**: communication
**Severity**: high
**Scope**: session
**Mandatory**: false

### Trigger
Some trigger.

### Statement
Some statement.

### Violation
Some violation.

### Pass Example
Some pass example.

### Enforcement
Some enforcement.

### Rationale
Some rationale.
"""
        result = _parse_rule_block("ENF-NEW-ADVISORY-001", block)
        assert result is not None
        assert result.get("mandatory") is False

    def test_non_enf_rule_still_defaults_to_false(self) -> None:
        """Behavior unchanged for non-ENF rules."""
        block = """
**Domain**: testing
**Severity**: medium
**Scope**: slice

### Trigger
Some trigger.

### Statement
Some statement.

### Violation
Some violation.

### Pass Example
Some pass example.

### Enforcement
Some enforcement.

### Rationale
Some rationale.
"""
        result = _parse_rule_block("TEST-FOO-001", block)
        assert result is not None
        assert result.get("mandatory") is False
