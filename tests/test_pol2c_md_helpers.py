"""POL-2c: md-helpers dedup (Wave 4, B4).

Nine INC test files each inlined the identical frontmatter-body regex
`re.match(r"^---\\n.*?\\n---\\n(.*)$", text, re.S)` and a `read_text().lower()`. POL-2c moves the
parse to tests/fixtures/md_helpers.py; each file keeps its path/node-id resolution and delegates.

Structural dedup assertions + a behavioral check on the shared helpers (always run). The full
guard that bodies/text are read identically is the 9 INC files still passing under the suite.
"""
from __future__ import annotations

from pathlib import Path

from tests._bible_guard import requires_bible

pytestmark = requires_bible


WRIT_ROOT = Path(__file__).resolve().parent.parent
TESTS = WRIT_ROOT / "tests"
METH = WRIT_ROOT / "bible" / "methodology"

B4_FILES = [
    TESTS / "test_inc3_authoring_uplift.py",
    TESTS / "test_inc4_phase_model.py",
    TESTS / "test_inc5_investigate_engine.py",
    TESTS / "test_inc7_tdd_design.py",
    TESTS / "test_inc8_planning.py",
    TESTS / "test_inc9_receiving_review.py",
    TESTS / "test_inc10_worktree.py",
    TESTS / "test_inc11_methodology_check.py",
    TESTS / "test_inc12_verify_parallel.py",
]

# The inlined regex literal that must no longer appear in the INC files.
INLINED_REGEX = 'r"^---\\n.*?\\n---\\n(.*)$"'


class TestHelperModule:
    def test_exposes_api(self) -> None:
        from tests.fixtures import md_helpers as mh

        for name in ("frontmatter_body", "text_lower", "word_count"):
            assert hasattr(mh, name), f"md_helpers missing {name}"

    def test_frontmatter_body_strips_yaml(self) -> None:
        from tests.fixtures.md_helpers import frontmatter_body

        # A real methodology node: body must start AFTER the front matter, not with '---'.
        node = METH / "SKL-PROC-MODE-001.md"
        body = frontmatter_body(node)
        assert body and not body.lstrip().startswith("---"), "front matter not stripped"
        assert "skill_id:" not in body, "front-matter field leaked into body"

    def test_text_lower_is_lowercased(self) -> None:
        from tests.fixtures.md_helpers import text_lower

        t = text_lower(METH / "SKL-PROC-MODE-001.md")
        assert t == t.lower() and "skl-proc-mode-001" in t

    def test_word_count(self) -> None:
        from tests.fixtures.md_helpers import word_count

        assert word_count("one two three") == 3
        assert word_count("   ") == 0


class TestNoInlinedRegex:
    @staticmethod
    def _src(p: Path) -> str:
        return p.read_text(encoding="utf-8")

    def test_inc_files_do_not_inline_frontmatter_regex(self) -> None:
        offenders = [f.name for f in B4_FILES if INLINED_REGEX in self._src(f)]
        assert not offenders, (
            "INC files still inline the frontmatter-body regex (should delegate to "
            f"md_helpers): {offenders}"
        )

    def test_inc_files_import_md_helpers(self) -> None:
        missing = [f.name for f in B4_FILES if "md_helpers" not in self._src(f)]
        assert not missing, (
            "INC files do not import the shared md_helpers: " + ", ".join(missing)
        )
