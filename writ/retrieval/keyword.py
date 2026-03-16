"""Tantivy BM25 index build and query on trigger, statement, tag fields.

Mandatory rules (mandatory: true) are excluded at index build time.
Index is built at ingestion and pre-warmed into memory at service startup.
Read-only during query time.

Trigger field is boosted 2x at index time to prioritize matching on
the rule's activation condition over incidental keywords in statement text.
"""

from __future__ import annotations

import re
from pathlib import Path

import tantivy

# Per ARCH-CONST-001: named constants for field boost.
TRIGGER_BOOST = 2.0

# Characters that break Tantivy's query parser. Stripped before parse_query().
_TANTIVY_SPECIAL = re.compile(r"""['":\\\/\(\)\[\]\{\}\!\?\~\^\+\-\&\|]""")


class KeywordIndex:
    """Tantivy BM25 keyword search index.

    Per ARCH-DI-001: index_dir passed via constructor.
    Per ARCH-TYPE-001: all public methods fully typed.
    """

    def __init__(
        self,
        index_dir: str | Path | None = None,
        trigger_boost: float = TRIGGER_BOOST,
    ) -> None:
        self._trigger_boost = trigger_boost

        schema_builder = tantivy.SchemaBuilder()
        schema_builder.add_text_field("rule_id", stored=True)
        schema_builder.add_text_field("trigger", stored=True)
        schema_builder.add_text_field("statement", stored=True)
        schema_builder.add_text_field("tags", stored=True)
        self._schema = schema_builder.build()

        if index_dir is not None:
            path = Path(index_dir)
            path.mkdir(parents=True, exist_ok=True)
            self._index = tantivy.Index(self._schema, path=str(path))
        else:
            self._index = tantivy.Index(self._schema)

    def build(self, rules: list[dict]) -> int:
        """Build the index from a list of rule dicts.

        Mandatory rules (mandatory: true) are excluded.
        Returns the number of rules indexed.
        """
        writer = self._index.writer()
        count = 0
        for rule in rules:
            if rule.get("mandatory", False):
                continue
            # Boost trigger field by repeating text to increase term frequency.
            trigger_text = rule.get("trigger", "")
            boosted_trigger = " ".join([trigger_text] * int(self._trigger_boost))
            writer.add_document(tantivy.Document(
                rule_id=rule["rule_id"],
                trigger=boosted_trigger,
                statement=rule.get("statement", ""),
                tags=rule.get("tags", ""),
            ))
            count += 1
        writer.commit()
        self._index.reload()
        return count

    def search(self, query_text: str, limit: int = 50) -> list[dict]:
        """Search the index with BM25 scoring.

        Returns list of dicts with rule_id and score, ordered by relevance.
        """
        searcher = self._index.searcher()
        sanitized = _TANTIVY_SPECIAL.sub(" ", query_text).strip()
        if not sanitized:
            return []
        query = self._index.parse_query(sanitized, ["trigger", "statement", "tags"])
        results = searcher.search(query, limit).hits
        output: list[dict] = []
        for score, doc_address in results:
            doc = searcher.doc(doc_address)
            output.append({
                "rule_id": doc["rule_id"][0],
                "score": score,
            })
        return output
