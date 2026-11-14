"""Parser for the V1 corpus dump (writ-corpus.cypher).

The dump uses exactly two statement shapes, one per line:

    CREATE (:Label {key: value, ...});
    MATCH (a {_dump_id: 'X'}), (b {_dump_id: 'Y'}) CREATE (a)-[:REL]->(b);

Anything else is rejected rather than skipped, so a format change in the
baseline surfaces as an audit failure instead of a silently smaller inventory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_NODE = re.compile(r"^CREATE \(:(?P<label>[A-Za-z_][A-Za-z0-9_]*) (?P<props>\{.*\})\);$")
_EDGE = re.compile(
    r"^MATCH \(a \{_dump_id: '(?P<src>(?:[^'\\]|\\.)*)'\}\), "
    r"\(b \{_dump_id: '(?P<dst>(?:[^'\\]|\\.)*)'\}\) "
    r"CREATE \(a\)-\[:(?P<rel>[A-Z_]+)(?P<props> \{.*\})?\]->\(b\);$"
)
# The dump's final statement strips the temporary import key; it carries no data.
_CLEANUP = "MATCH (n) WHERE n._dump_id IS NOT NULL REMOVE n._dump_id;"
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "\\": "\\"}


class CypherFormatError(ValueError):
    pass


@dataclass
class Node:
    label: str
    props: dict[str, Any]

    @property
    def dump_id(self) -> str:
        return self.props["_dump_id"]


@dataclass
class Edge:
    src: str
    rel: str
    dst: str
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class Corpus:
    nodes: list[Node]
    edges: list[Edge]

    def by_label(self, label: str) -> list[Node]:
        return [n for n in self.nodes if n.label == label]


class _MapParser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def error(self, message: str) -> CypherFormatError:
        return CypherFormatError(f"{message} at offset {self.pos}: {self.text[self.pos:self.pos + 40]!r}")

    def skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            self.pos += 1

    def expect(self, char: str) -> None:
        self.skip_ws()
        if not self.text.startswith(char, self.pos):
            raise self.error(f"expected {char!r}")
        self.pos += len(char)

    def parse_map(self) -> dict[str, Any]:
        self.expect("{")
        result: dict[str, Any] = {}
        self.skip_ws()
        if self.text.startswith("}", self.pos):
            self.pos += 1
            return result
        while True:
            key = self.parse_key()
            self.expect(":")
            if key in result:
                raise self.error(f"duplicate key {key!r}")
            result[key] = self.parse_value()
            self.skip_ws()
            if self.text.startswith(",", self.pos):
                self.pos += 1
                continue
            self.expect("}")
            return result

    def parse_key(self) -> str:
        self.skip_ws()
        if self.text.startswith("`", self.pos):
            end = self.text.index("`", self.pos + 1)
            key = self.text[self.pos + 1:end]
            self.pos = end + 1
            return key
        match = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(self.text, self.pos)
        if not match:
            raise self.error("expected key")
        self.pos = match.end()
        return match.group(0)

    def parse_value(self) -> Any:
        self.skip_ws()
        ch = self.text[self.pos:self.pos + 1]
        if ch == "'":
            return self.parse_string()
        if ch == "[":
            return self.parse_list()
        if ch == "{":
            return self.parse_map()
        for literal, value in (("true", True), ("false", False), ("null", None)):
            if self.text.startswith(literal, self.pos):
                self.pos += len(literal)
                return value
        match = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?").match(self.text, self.pos)
        if not match:
            raise self.error("expected value")
        self.pos = match.end()
        token = match.group(0)
        return float(token) if any(c in token for c in ".eE") else int(token)

    def parse_string(self) -> str:
        self.pos += 1
        out = []
        while True:
            if self.pos >= len(self.text):
                raise self.error("unterminated string")
            ch = self.text[self.pos]
            if ch == "\\":
                nxt = self.text[self.pos + 1:self.pos + 2]
                if nxt == "u":
                    out.append(chr(int(self.text[self.pos + 2:self.pos + 6], 16)))
                    self.pos += 6
                    continue
                if nxt not in _ESCAPES:
                    raise self.error(f"unknown escape \\{nxt}")
                out.append(_ESCAPES[nxt])
                self.pos += 2
            elif ch == "'":
                self.pos += 1
                return "".join(out)
            else:
                out.append(ch)
                self.pos += 1

    def parse_list(self) -> list[Any]:
        self.expect("[")
        items: list[Any] = []
        self.skip_ws()
        if self.text.startswith("]", self.pos):
            self.pos += 1
            return items
        while True:
            items.append(self.parse_value())
            self.skip_ws()
            if self.text.startswith(",", self.pos):
                self.pos += 1
                continue
            self.expect("]")
            return items


def parse_map(text: str) -> dict[str, Any]:
    parser = _MapParser(text)
    result = parser.parse_map()
    parser.skip_ws()
    if parser.pos != len(text):
        raise parser.error("trailing content")
    return result


def _unescape_id(raw: str) -> str:
    return _MapParser(f"'{raw}'").parse_string()


def parse_corpus(text: str) -> Corpus:
    nodes: list[Node] = []
    edges: list[Edge] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line == _CLEANUP:
            continue
        try:
            if m := _NODE.match(line):
                props = parse_map(m.group("props"))
                if "_dump_id" not in props:
                    raise CypherFormatError("node without _dump_id")
                nodes.append(Node(m.group("label"), props))
            elif m := _EDGE.match(line):
                props = parse_map(m.group("props").strip()) if m.group("props") else {}
                edges.append(Edge(_unescape_id(m.group("src")), m.group("rel"),
                                  _unescape_id(m.group("dst")), props))
            else:
                raise CypherFormatError("unrecognized statement")
        except CypherFormatError as exc:
            raise CypherFormatError(f"line {lineno}: {exc}") from None
    return Corpus(nodes, edges)
