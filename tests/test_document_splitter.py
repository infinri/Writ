"""Program item 5 (workstream D), phase 2: the markdown splitter, writ/documents/splitter.py.

split_markdown(text, *, fallback_title) is a pure function (no graph, no filesystem) that turns
one markdown file into a title and an ordered list of chunk drafts:
ChunkDraft(ordinal, breadcrumb, text, est_tokens). The contract is pinned here from the
plan's section 2:

  * ATX H1 to H3 open sections outside fenced code; H4 and deeper stay in the body
  * every chunk carries the breadcrumb of its enclosing headings, led by the document title
    when the document has no H1; breadcrumbs are clipped to CHUNK_HEADER_CHARS
  * whole paragraphs are packed up to CHUNK_MAX_TOKENS without crossing a section boundary
  * a fenced code block is one paragraph; an oversize paragraph splits at line boundaries and
    an oversize line at a word boundary (hard cut when there is no space)
  * a heading with no body yields no chunk
  * front matter is stripped; the title is the first H1, else the front-matter title, else the
    fallback title (the file stem)
  * the output is a pure function of the input

The module is imported inside each test (through `_sp`) so a missing module fails each test
individually instead of collapsing the file into one collection error.
"""
from __future__ import annotations

import importlib

import pytest

CHARS_PER_TOKEN = 4


def _sp():
    return importlib.import_module("writ.documents.splitter")


def _split(text: str, fallback: str = "stem"):
    return _sp().split_markdown(text, fallback_title=fallback)


def _flat(result) -> tuple[str, list[tuple[int, str, str, int]]]:
    return result.title, [(c.ordinal, c.breadcrumb, c.text, c.est_tokens) for c in result.chunks]


def _crumbs(result) -> list[str]:
    return [c.breadcrumb for c in result.chunks]


def _max_chars() -> int:
    return _sp().CHUNK_MAX_TOKENS * CHARS_PER_TOKEN


def _para(tag: str, n: int) -> str:
    return (f"{tag} " + "lorem ipsum dolor " * 60)[:n].rstrip()


class TestConstants:
    def test_the_documented_limits(self):
        sp = _sp()
        assert sp.CHUNK_MAX_TOKENS == 300
        assert sp.CHUNK_HEADER_CHARS == 120
        assert _max_chars() == 1200


class TestSectionsAndBreadcrumbs:
    def test_h1_h2_h3_open_sections_and_each_chunk_carries_its_breadcrumb(self):
        text = ("# Guide\n\nintro text\n\n## Setup\n\nsetup text\n\n### Install\n\ninstall text\n\n"
                "## Usage\n\nusage text\n")
        result = _split(text)
        assert _crumbs(result) == ["Guide", "Guide > Setup", "Guide > Setup > Install", "Guide > Usage"]
        assert [c.text for c in result.chunks] == ["intro text", "setup text", "install text", "usage text"]

    def test_a_new_h2_resets_the_h3_below_it(self):
        text = "# A\n\n## B\n\n### C\n\nc text\n\n## D\n\nd text\n"
        assert _crumbs(_split(text)) == ["A > B > C", "A > D"]

    def test_a_second_h1_restarts_the_breadcrumb(self):
        text = "# First\n\none\n\n## Sub\n\ntwo\n\n# Second\n\nthree\n"
        assert _crumbs(_split(text)) == ["First", "First > Sub", "Second"]

    def test_a_document_without_an_h1_is_led_by_its_title(self):
        text = "---\ntitle: Front Title\n---\nopening words\n\n## Part\n\npart words\n"
        result = _split(text)
        assert _crumbs(result) == ["Front Title", "Front Title > Part"]

    def test_h4_and_deeper_stay_in_the_body_and_never_open_a_section(self):
        text = "# Top\n\nlead\n\n#### Deep heading\n\ndeep body\n\n##### Deeper\n\nmore\n"
        result = _split(text)
        assert _crumbs(result) == ["Top"]
        body = result.chunks[0].text
        assert "#### Deep heading" in body and "##### Deeper" in body and "deep body" in body

    def test_ordinals_are_consecutive_from_zero(self):
        text = "# T\n\n" + "\n\n".join(f"## S{i}\n\nbody {i}" for i in range(5))
        assert [c.ordinal for c in _split(text).chunks] == [0, 1, 2, 3, 4]

    def test_est_tokens_is_the_text_length_over_chars_per_token(self):
        text = "# T\n\n" + _para("alpha", 700)
        for c in _split(text).chunks:
            assert c.est_tokens == len(c.text) // CHARS_PER_TOKEN

    def test_a_heading_with_no_body_produces_no_chunk_but_still_names_its_children(self):
        text = "# T\n\n## Empty\n\n### Child\n\nchild body\n\n## Also Empty\n"
        result = _split(text)
        assert _crumbs(result) == ["T > Empty > Child"]

    def test_a_document_of_only_headings_has_no_chunks(self):
        assert _split("# A\n\n## B\n\n### C\n").chunks == []

    def test_empty_and_whitespace_only_input_have_no_chunks_and_the_fallback_title(self):
        for text in ("", "\n\n   \n"):
            result = _split(text, fallback="notes")
            assert result.chunks == []
            assert result.title == "notes"

    def test_a_long_breadcrumb_is_clipped_to_the_header_limit(self):
        sp = _sp()
        text = f"# {'A' * 200}\n\n## {'B' * 200}\n\nbody\n"
        crumbs = _crumbs(_split(text))
        assert crumbs and all(len(c) <= sp.CHUNK_HEADER_CHARS for c in crumbs)
        assert crumbs[0].startswith("A" * 50)

    def test_every_chunk_of_a_split_section_repeats_the_same_short_breadcrumb(self):
        text = "# Doc\n\n## Long\n\n" + "\n\n".join(_para(f"p{i}", 500) for i in range(6))
        result = _split(text)
        assert len(result.chunks) >= 3
        assert set(_crumbs(result)) == {"Doc > Long"}


class TestFencedCode:
    def test_a_heading_inside_a_fence_is_not_a_heading(self):
        text = "# Real\n\nbefore\n\n```bash\n# not a heading\n## also not\n```\n\nafter\n"
        result = _split(text)
        assert _crumbs(result) == ["Real"]
        assert "# not a heading" in result.chunks[0].text

    def test_tilde_fences_hide_headings_too(self):
        text = "# Real\n\n~~~\n# hidden\n~~~\n\ntail\n"
        assert _crumbs(_split(text)) == ["Real"]

    def test_a_longer_outer_fence_is_not_closed_by_a_shorter_inner_fence(self):
        text = "# Real\n\n````md\n```\n# still inside\n```\n````\n\ntail\n"
        result = _split(text)
        assert _crumbs(result) == ["Real"]
        assert "# still inside" in result.chunks[0].text

    def test_a_fenced_block_with_blank_lines_stays_whole_in_one_chunk(self):
        code = "```python\nfirst = 1\n\n\nsecond = 2\n```"
        text = f"# T\n\nlead in\n\n{code}\n\ntrailing prose\n"
        holders = [c for c in _split(text).chunks if "first = 1" in c.text]
        assert len(holders) == 1
        assert code in holders[0].text


class TestPacking:
    def test_whole_paragraphs_are_packed_up_to_the_token_cap(self):
        p1, p2, p3 = _para("one", 500), _para("two", 500), _para("three", 500)
        result = _split(f"# T\n\n{p1}\n\n{p2}\n\n{p3}\n")
        assert len(result.chunks) == 2
        assert p1 in result.chunks[0].text and p2 in result.chunks[0].text
        assert p3 not in result.chunks[0].text and p3 in result.chunks[1].text
        assert all(len(c.text) <= _max_chars() for c in result.chunks)

    def test_packing_never_crosses_a_section_boundary(self):
        text = "# T\n\n## A\n\nshort a\n\n## B\n\nshort b\n"
        result = _split(text)
        assert [c.text for c in result.chunks] == ["short a", "short b"]

    def test_a_paragraph_is_never_split_across_chunks_when_it_fits(self):
        paras = [_para(f"p{i}", 450) for i in range(7)]
        result = _split("# T\n\n" + "\n\n".join(paras))
        for para in paras:
            assert sum(1 for c in result.chunks if para in c.text) == 1

    def test_an_oversize_paragraph_splits_at_line_boundaries(self):
        lines = [f"line {i:03d} " + "w" * 50 for i in range(40)]
        result = _split("# T\n\n" + "\n".join(lines))
        assert len(result.chunks) >= 2
        assert all(len(c.text) <= _max_chars() for c in result.chunks)
        rebuilt = [ln for c in result.chunks for ln in c.text.split("\n")]
        assert rebuilt == lines

    def test_an_oversize_line_splits_at_a_word_boundary(self):
        result = _split("# T\n\n" + "word " * 500)
        assert len(result.chunks) >= 2
        for c in result.chunks:
            assert len(c.text) <= _max_chars()
            assert set(c.text.split()) == {"word"}, "a word was cut in half"

    def test_an_oversize_line_with_no_space_is_hard_cut_without_loss(self):
        result = _split("# T\n\n" + "x" * 3000)
        assert all(len(c.text) <= _max_chars() for c in result.chunks)
        assert "".join(c.text for c in result.chunks) == "x" * 3000

    def test_every_chunk_fits_the_token_cap_for_a_mixed_document(self):
        parts = ["# Mixed"]
        for i in range(4):
            parts += [f"## S{i}", _para("body", 900), "```\n" + "c = 1\n" * 100 + "```", "x" * 1500]
        result = _split("\n\n".join(parts))
        assert result.chunks and all(len(c.text) <= _max_chars() for c in result.chunks)


class TestTitleAndFrontMatter:
    def test_the_first_h1_is_the_title(self):
        assert _split("# First\n\na\n\n# Second\n\nb\n", fallback="stem").title == "First"

    def test_front_matter_is_stripped_from_every_chunk(self):
        text = "---\ntitle: FM\ntags: [secret-marker]\n---\n# Heading\n\nbody text\n"
        result = _split(text)
        assert all("secret-marker" not in c.text and "tags:" not in c.text for c in result.chunks)
        assert [c.text for c in result.chunks] == ["body text"]

    def test_the_h1_beats_the_front_matter_title(self):
        assert _split("---\ntitle: FM Title\n---\n# H1 Title\n\nbody\n").title == "H1 Title"

    def test_the_front_matter_title_beats_the_file_stem(self):
        assert _split("---\ntitle: FM Title\n---\nbody only\n", fallback="stem").title == "FM Title"

    def test_the_file_stem_is_the_last_resort(self):
        assert _split("just prose, no headings\n", fallback="my-notes").title == "my-notes"

    def test_a_document_with_no_headings_is_one_chunk_led_by_the_title(self):
        result = _split("just prose, no headings\n", fallback="my-notes")
        assert _crumbs(result) == ["my-notes"]
        assert result.chunks[0].text == "just prose, no headings"


class TestDeterminism:
    def test_identical_input_gives_identical_output(self):
        text = ("---\ntitle: T\n---\n# A\n\n" + _para("x", 800) + "\n\n## B\n\n```\ncode\n```\n\n"
                + "word " * 400)
        assert _flat(_split(text)) == _flat(_split(text))

    def test_output_does_not_depend_on_call_history(self):
        a = "# A\n\nalpha\n"
        b = "# B\n\nbeta\n"
        first = _flat(_split(a))
        _split(b)
        assert _flat(_split(a)) == first

    def test_the_splitter_is_pure_and_touches_neither_graph_nor_filesystem(self):
        import inspect
        src = inspect.getsource(_sp())
        for token in ("open(", "Path(", "neo4j", "Neo4jConnection", "import os"):
            assert token not in src

    def test_the_splitter_reuses_the_shared_clip_and_chars_per_token(self):
        import inspect
        src = inspect.getsource(_sp())
        assert "clip" in src and "CHARS_PER_TOKEN" in src
