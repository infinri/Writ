"""Collection-time marking for the fire drill package.

Applies `firedrill` and `no_friction_isolation` to every test item collected under
`tests/firedrill/`, from ONE place, rather than each drill module declaring its own
`pytestmark`. That is the same argument the sub-agent cache seeding makes in
`bin/lib/common.sh` (plan.md Decision 1): coverage that comes from WHERE a file
lives cannot go stale, and a list of modules that each remembered to mark
themselves goes stale the moment a fifth one does not. A drill module added later
with no `pytestmark` of its own still gets both markers here, so it cannot silently
escape `pytest -m firedrill` and cannot silently inherit the autouse single-file
log collapse (`tests/conftest.py::_isolate_friction_log`), which would make every
stream-routing assertion in this package vacuous (plan.md Decision 4).

`firedrill` is registered in `pyproject.toml` by the implementer (plan.md ## Files);
using `pytest.mark.firedrill` before that registration lands produces an
unregistered-marker warning, never a collection error, so this file collects clean
either way.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    for item in items:
        try:
            item_path = Path(str(item.fspath)).resolve()
        except OSError:
            continue
        if item_path == _THIS_DIR or _THIS_DIR in item_path.parents:
            item.add_marker(pytest.mark.firedrill)
            item.add_marker(pytest.mark.no_friction_isolation)
