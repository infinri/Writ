"""Structural link check for the entry-point docs (workstream D).

Asserts only structure: the hero asset exists and is a PNG, README embeds it,
and every relative link, image path and #anchor in the entry-point documents
resolves under GitHub heading-slug rules. It never asserts what a sentence says
(the suite forbids tests on documentation prose; see tests/_inventory.py near
the phrase-scan spec and tests/test_doc_style_ratchet.py).

Needs no graph; imports nothing from writ.graph. Synthetic positive controls
under tmp_path prove the scanner can fail, so empty findings are not vacuous.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO = Path(__file__).resolve().parent.parent

ENTRY_DOCS = (
    "README.md",
    "SECURITY.md",
    "docs/install.md",
    "HANDBOOK.md",
    "SPONSORSHIP.md",
    "CONTRIBUTING.md",
)
HERO = "docs/assets/writ-hero.png"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_RE = re.compile(r"(`+)(.+?)\1", re.S)
_LINK_RE = re.compile(r"\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_ANCHOR_TAG_RE = re.compile(r"<a\s+[^>]*?(?:id|name)=[\"']([^\"']+)[\"']", re.I)
_EXTERNAL = ("http:", "https:", "mailto:", "ftp:", "tel:", "data:")


def _blank(s: str) -> str:
    return re.sub(r"[^\n]", " ", s)


def _blank_fences(text: str) -> str:
    """Blank closed fenced blocks in place; raise on an unclosed fence."""
    out: list[str] = []
    fence: tuple[str, int] | None = None
    for i, line in enumerate(text.split("\n"), 1):
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = (m.group(1)[0], len(m.group(1)))
                out.append(_blank(line))
            else:
                out.append(line)
        else:
            out.append(_blank(line))
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] \
                    and line.strip() == m.group(1):
                fence = None
    if fence is not None:
        raise ValueError("unclosed fenced code block")
    return "\n".join(out)


def _blank_code(text: str) -> str:
    """Blank closed fences and inline code spans in place (line numbers kept)."""
    text = _blank_fences(text)
    return _INLINE_RE.sub(lambda m: _blank(m.group(0)), text)


def _slugify(heading: str) -> str:
    h = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    h = re.sub(r"<[^>]+>", "", h)
    h = h.replace("`", "")
    h = unicodedata.normalize("NFC", h).lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def _slugs(text: str) -> set[str]:
    """GitHub anchors of every ATX heading outside fences, plus explicit
    <a id>/<a name> anchors. Repeated slugs get -1, -2, ..."""
    body = _blank_fences(text)
    seen: dict[str, int] = {}
    out: set[str] = set()
    for line in body.split("\n"):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        base = _slugify(m.group(2))
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.add(base if n == 0 else f"{base}-{n}")
    out.update(_ANCHOR_TAG_RE.findall(body))
    return out


def broken_links(doc: Path, *, repo: Path = REPO) -> list[tuple[int, str, str]]:
    """(line, target, reason) for every unresolved relative link or image."""
    text = doc.read_text(encoding="utf-8")
    scan = _blank_code(text)
    findings: list[tuple[int, str, str]] = []
    for m in _LINK_RE.finditer(scan):
        raw = m.group(1).strip("<>")
        if raw.lower().startswith(_EXTERNAL) or raw.startswith("//"):
            continue
        line = scan.count("\n", 0, m.start()) + 1
        path_part, _, frag = raw.partition("#")
        path_part = unquote(path_part.partition("?")[0])
        if not path_part:
            target = doc
        elif path_part.startswith("/"):
            target = repo / path_part.lstrip("/")
        else:
            target = doc.parent / path_part
        if not target.exists():
            findings.append((line, raw, "missing file"))
            continue
        if frag and target.is_file() and target.suffix.lower() == ".md":
            if unquote(frag).lower() not in _slugs(target.read_text(encoding="utf-8")):
                findings.append((line, raw, "missing anchor"))
    return findings


def _relative_link_count(doc: Path) -> int:
    scan = _blank_code(doc.read_text(encoding="utf-8"))
    return sum(
        1 for m in _LINK_RE.finditer(scan)
        if not m.group(1).strip("<>").lower().startswith(_EXTERNAL)
    )


def _tracked_or_present(rel: str) -> Path:
    p = REPO / rel
    if not p.is_file():
        pytest.fail(f"entry-point doc missing from working tree: {rel}")
    return p


class TestScannerControls:
    def test_reports_link_to_missing_file(self, tmp_path):
        d = tmp_path / "a.md"
        d.write_text("# T\n\nSee [x](nope.md).\n")
        found = broken_links(d, repo=tmp_path)
        assert [(f[1], f[2]) for f in found] == [("nope.md", "missing file")]

    def test_reports_link_to_missing_anchor(self, tmp_path):
        (tmp_path / "b.md").write_text("# B\n\n## Real\n")
        d = tmp_path / "a.md"
        d.write_text("# A\n\n[x](b.md#ghost) and [y](b.md#real) and [z](#nowhere)\n")
        found = broken_links(d, repo=tmp_path)
        assert sorted((f[1], f[2]) for f in found) == [
            ("#nowhere", "missing anchor"), ("b.md#ghost", "missing anchor")]

    def test_punctuated_heading_anchor_resolves(self, tmp_path):
        d = tmp_path / "a.md"
        d.write_text("# A\n\n## 8. Custom rules: what?\n\n[x](#8-custom-rules-what)\n")
        assert broken_links(d, repo=tmp_path) == []

    def test_code_marker_and_link_syntax_dropped_from_slug(self, tmp_path):
        assert _slugs("# Use `writ doc` [here](x.md)\n") == {"use-writ-doc-here"}

    def test_repeated_heading_gets_numeric_suffix(self, tmp_path):
        d = tmp_path / "a.md"
        d.write_text("# A\n\n## Setup\n\n## Setup\n\n[x](#setup) [y](#setup-1) [z](#setup-2)\n")
        found = broken_links(d, repo=tmp_path)
        assert [f[1] for f in found] == ["#setup-2"]

    def test_explicit_anchor_tag_resolves(self, tmp_path):
        d = tmp_path / "a.md"
        d.write_text('# A\n\n<a id="custom"></a>\n\n[x](#custom)\n')
        assert broken_links(d, repo=tmp_path) == []

    def test_links_in_fence_inline_code_and_external_urls_ignored(self, tmp_path):
        d = tmp_path / "a.md"
        d.write_text(
            "# A\n\n```\n[x](gone.md)\n```\n\n`[y](gone2.md)`\n\n"
            "[e](https://example.invalid/x#y) [m](mailto:a@b.c)\n")
        assert broken_links(d, repo=tmp_path) == []

    def test_heading_inside_fence_is_not_an_anchor(self, tmp_path):
        assert _slugs("# A\n\n```\n## Hidden\n```\n") == {"a"}

    def test_unclosed_fence_raises(self, tmp_path):
        d = tmp_path / "a.md"
        d.write_text("# A\n\n```\n[x](gone.md)\n")
        with pytest.raises(ValueError):
            broken_links(d, repo=tmp_path)

    def test_directory_target_resolves(self, tmp_path):
        (tmp_path / "sub").mkdir()
        d = tmp_path / "a.md"
        d.write_text("# A\n\n[x](sub/)\n")
        assert broken_links(d, repo=tmp_path) == []

    def test_line_number_is_one_based_and_survives_fences(self, tmp_path):
        d = tmp_path / "a.md"
        d.write_text("# A\n\n```\nx\n```\n\n[x](gone.md)\n")
        assert broken_links(d, repo=tmp_path)[0][0] == 7


class TestHeroAsset:
    def test_hero_exists(self):
        assert (REPO / HERO).is_file(), f"{HERO} is missing"

    def test_hero_is_png(self):
        p = REPO / HERO
        assert p.is_file(), f"{HERO} is missing"
        assert p.read_bytes()[:8] == PNG_SIGNATURE

    def test_readme_references_hero(self):
        readme = _tracked_or_present("README.md")
        scan = _blank_code(readme.read_text(encoding="utf-8"))
        hero = (REPO / HERO).resolve()
        targets = [
            (readme.parent / unquote(m.group(1).strip("<>").partition("#")[0])).resolve()
            for m in re.finditer(r"!\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)", scan)
            if not m.group(1).lower().startswith(_EXTERNAL)
        ]
        assert hero in targets, "README.md has no image reference resolving to the hero asset"


class TestEntryPointDocs:
    @pytest.mark.parametrize("rel", ENTRY_DOCS)
    def test_doc_has_relative_links(self, rel):
        assert _relative_link_count(_tracked_or_present(rel)) >= 1, \
            f"{rel} yields no relative link; empty findings would be vacuous"

    @pytest.mark.parametrize("rel", ENTRY_DOCS)
    def test_no_broken_relative_links_or_anchors(self, rel):
        assert broken_links(_tracked_or_present(rel)) == []

    def test_security_links_readme_evidence_and_limits(self):
        sec = _tracked_or_present("SECURITY.md").read_text(encoding="utf-8")
        assert "README.md#evidence-and-limits" in _blank_code(sec)
        assert "evidence-and-limits" in _slugs(
            _tracked_or_present("README.md").read_text(encoding="utf-8"))

    def test_readme_links_install_why_was_i_blocked(self):
        readme = _tracked_or_present("README.md").read_text(encoding="utf-8")
        assert "docs/install.md#why-was-i-blocked" in _blank_code(readme)
        assert "why-was-i-blocked" in _slugs(
            _tracked_or_present("docs/install.md").read_text(encoding="utf-8"))
