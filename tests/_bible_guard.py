"""Module-level guard for tests that need the untracked bible/ source tree.

bible/ is a derived, local, untracked export of the graph (the tracked, shipped
corpus is writ-corpus.cypher). Clean checkouts, and therefore CI, run without
it, so tests that read or re-ingest the source tree must skip there rather than
fail or, worse, wipe the shared graph and repopulate nothing. Apply per module:

    from tests._bible_guard import requires_bible
    pytestmark = requires_bible
"""
from pathlib import Path

import pytest

BIBLE_DIR = Path(__file__).resolve().parent.parent / "bible"

requires_bible = pytest.mark.skipif(
    not BIBLE_DIR.exists(),
    reason="requires the untracked bible/ source tree (regenerate with `writ export`; "
    "clean checkouts and CI run without it)",
)
