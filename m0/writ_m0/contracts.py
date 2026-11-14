"""Schema registry and validation for the V2 draft contracts in docs/contracts.

Format assertions are enforced, not just annotated. jsonschema's default
FormatChecker silently accepts any "date-time" string unless an optional
dependency is installed, so this module registers its own strict checker.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "docs" / "contracts"
FIXTURES_DIR = CONTRACTS_DIR / "fixtures"

# RFC 3339 date-time with mandatory offset (Z or +hh:mm).
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)

FORMAT_CHECKER = FormatChecker(formats=())


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _is_rfc3339(value: object) -> bool:
    if not isinstance(value, str):
        return True  # type keyword handles non-strings
    if not _RFC3339.match(value):
        raise ValueError(f"not an RFC 3339 date-time: {value!r}")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return True


class ContractError(ValueError):
    """A document failed validation against its contract."""

    def __init__(self, contract: str, errors: list[str]):
        super().__init__(f"{contract}: " + "; ".join(errors))
        self.contract = contract
        self.errors = errors


@dataclass(frozen=True)
class Violation:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path or '<root>'}: {self.message}"


def schema_names() -> list[str]:
    return sorted(p.name.removesuffix(".schema.json") for p in CONTRACTS_DIR.glob("*.schema.json"))


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    path = CONTRACTS_DIR / f"{name}.schema.json"
    if not path.is_file():
        raise KeyError(f"unknown contract: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    schema = load_schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FORMAT_CHECKER)


def violations(name: str, document: Any) -> list[Violation]:
    errors = sorted(_validator(name).iter_errors(document), key=lambda e: list(e.absolute_path))
    return [Violation("/".join(str(p) for p in e.absolute_path), e.message) for e in errors]


def validate(name: str, document: Any) -> Any:
    """Return the document unchanged, or raise ContractError listing every violation."""
    found = violations(name, document)
    if found:
        raise ContractError(name, [str(v) for v in found])
    return document


def load_fixture(relative: str) -> Any:
    return json.loads((FIXTURES_DIR / relative).read_text(encoding="utf-8"))
