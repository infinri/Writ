"""The collector registry: one entry per retrieval source, returning its units and a change
stamp, validated against the schema allowlists at import and failing closed
(docs/adr/ADR-collector-registry.md).

A doctrine collector may name only DOCTRINE_NODE_TYPES labels; a record collector only
RECORD_LABELS labels that are neither doctrine nor in NODE_ID_FIELDS. A label the schema does
not know is refused, so a new source cannot widen what reaches a caller without an edit to
those allowlists.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Literal, Protocol

from writ.graph.db import _common
from writ.graph.schema import NODE_ID_FIELDS
from writ.retrieval.node_scope import DOCTRINE_NODE_TYPES


def fingerprint_digest(value: object) -> str:
    """SHA-256 over the JSON of `value`: the _compute_bm25_hash idiom, with sort_keys so
    dict order is irrelevant and default=str for the graph's temporal values."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class CollectFn(Protocol):
    async def __call__(self, db) -> list[dict]: ...


@dataclass(frozen=True)
class Collected:
    units: list[dict]
    stamp_fn: Callable[[list[dict]], str]

    @cached_property
    def stamp(self) -> str:
        return self.stamp_fn(self.units)


@dataclass(frozen=True)
class Collector:
    name: str
    labels: tuple[str, ...]
    scope: Literal["doctrine", "record"]
    collect: CollectFn
    stamp_fn: Callable[[list[dict]], str] = fingerprint_digest

    async def run(self, db) -> Collected:
        return Collected(await self.collect(db), self.stamp_fn)


class CollectorRegistryError(ValueError):
    pass


def _label_error(collector: Collector, label: str) -> str | None:
    if label not in NODE_ID_FIELDS and label not in _common.RECORD_LABELS:
        return "is not a known node or record label"
    if collector.scope == "doctrine":
        return None if label in DOCTRINE_NODE_TYPES else "is not a doctrine label"
    if collector.scope != "record":
        return f"has an unknown scope {collector.scope!r}"
    if label not in _common.RECORD_LABELS or label in DOCTRINE_NODE_TYPES or label in NODE_ID_FIELDS:
        return "is not a record label"
    return None


def validate_collectors(collectors: Sequence[Collector]) -> None:
    names: set[str] = set()
    claimed: dict[str, str] = {}
    for collector in collectors:
        if not collector.name or collector.name in names:
            raise CollectorRegistryError(f"collector name {collector.name!r} is empty or repeated")
        names.add(collector.name)
        if not collector.labels:
            raise CollectorRegistryError(f"collector {collector.name!r} claims no label")
        for label in collector.labels:
            if label in claimed:
                raise CollectorRegistryError(
                    f"label {label!r} is claimed by collector {claimed[label]!r} and {collector.name!r}")
            claimed[label] = collector.name
            error = _label_error(collector, label)
            if error:
                raise CollectorRegistryError(
                    f"{collector.scope} collector {collector.name!r}: label {label!r} {error}")
