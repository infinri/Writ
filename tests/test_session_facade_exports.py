"""The 101 `F401`s: what `bin/lib/writ-session.py` re-exports on purpose, and what it does
not re-export at all.

THE FINDING. `bin/lib/writ-session.py` is a deliberate re-export facade for the POL-6 split
packages, and it says so in prose in nine comment blocks and nowhere a linter or a reader
can check. Ruff reports 100 unused imports in it (measured 2026-09-16,
`ruff check --select F401 --statistics`), plus one vestigial import in
`tests/firedrill/test_bash_refusals.py`, which is the 101st and the only reason that file
is in this module: it is the same finding, not a second subject.

WHAT IS NOT HERE, AND WHY. There is NO test that the facade exports what the facade
exports. That shape is an assertion whose right-hand side is computed from its left-hand
side, it cannot fail, and this repository has shipped it twice. The deletions are proven by
the consumers that already exist and already load this file by path: `writ/server/__init__.py`
loads it with `spec_from_file_location` and the route modules reach
`server.writ_session.<name>`, the POL-6 extraction tests assert named attributes on a
freshly loaded facade, and `tests/test_replan_reopen_planning.py::_require` fails loudly on
a missing name. Those are the independent side and the full suite is the gate. Anyone
looking here for a surface mirror is looking for a test that was deliberately not written.

THE TWO ASSERTIONS THAT DO SHIP EACH NAME AN INDEPENDENT PRODUCER.

  * DEMAND. Left side is the facade's import table, read from the AST of the real file.
    Right side is a scan of the OTHER `.py` and `.sh` files in the tree, matching attribute
    access (`.name`) and quoted strings (`"name"`), because
    `monkeypatch.setattr(mod, "_read_cache", ...)` and `_require(writ_session, "cmd_reopen_planning")`
    are real demand and both are in the tree. The scan OVER-approximates: a `.name` on an
    unrelated object counts. That direction is deliberate and sound for this use, because
    it can only KEEP a name and never delete a live one, so a verdict of zero demand is
    trustworthy while a verdict of demand is merely conservative. Nothing about the facade
    is on the right-hand side.

  * `__all__`. Left side is a hand-written literal in the facade, right side is the AST
    binding list of the same file. Two statements about one artifact is weaker than two
    artifacts, which is why it is scoped narrowly: its job is to catch the next edit that
    adds an import and forgets to declare it, a regression that is otherwise invisible
    because nothing in this repository runs ruff.

THE SCAN IS PROVEN CONDITIONAL. `_SENTINEL_NO_DEMAND` is a name nothing in the tree
demands, and it is assembled from two fragments at import time so that its own spelling
never appears as a quoted literal in any scanned file. If it appeared whole in this module,
this module would be a demand site for it and the sentinel would prove the opposite of what
it is for.

MUTATION AGAINST COPIES UNDER tmp_path, NEVER THE REAL TREE. Every detector takes a `path`
keyword for exactly that reason, following `tests/_inventory.py::rag_inject_field_slices`
and `prompt_parse_field_order`.

PLAIN IMPORTS ARE JUDGED DIFFERENTLY, AND THE REASON IS MEASURED. The over-approximating
demand scan is useless for `json`, `datetime` and friends: `.json` and `.datetime` appear
all over the tree with nothing to do with this facade, so the scan would report demand for
every one of them. They are judged by the facade's OWN BODY instead, which defines only
`main()` plus the `sys.path` bootstrap. A non-package import the body never references is a
leftover from before the POL-6 split, not a re-export.
"""
from __future__ import annotations

import ast
import functools
import importlib.util
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FACADE = REPO / "bin" / "lib" / "writ-session.py"
FIREDRILL_REFUSALS = REPO / "tests" / "firedrill" / "test_bash_refusals.py"
FIREDRILL_HARNESS = REPO / "tests" / "firedrill" / "_harness.py"

# The package the facade re-exports FROM. Everything else it binds is judged by the body.
PACKAGE_PREFIX = "writ.session"

# Directories the demand scan never reads: build artifacts and vendored code are not
# consumers, and including them would only ever manufacture demand.
EXCLUDED_DIRS = {".venv", ".git", "node_modules", "__pycache__", ".pytest_cache"}
SCANNED_SUFFIXES = (".py", ".sh")

# ASSEMBLED, NEVER WRITTEN WHOLE. See the module docstring: a sentinel spelled out as a
# literal here would be found by the very scan it exists to falsify.
_SENTINEL_NO_DEMAND = "_quokka" + "_shaped_reexport"

# The positive pole of the same scan: an attribute this tree reaches in 205 files
# (measured 2026-09-16) and that has nothing to do with the facade.
_LIVE_ATTRIBUTE = "environ"

# The facade's only function-scope import, named here because the derivation MUST exclude
# it: it is bound inside `main()`, it is not a module-level re-export, and a table that
# included it would demand a name in `__all__` that the module never binds at module level.
FUNCTION_SCOPE_BINDING = "dispatch"

# The helper the firedrill refusal module imports and never uses (the 101st F401). The
# property it would have served is already proven better at
# tests/firedrill/test_bash_refusals.py:148-155, which read WRIT_PORT out of build_env's
# own result and fail to connect to it, rather than trusting an address a test set.
VESTIGIAL_FIREDRILL_IMPORT = "closed_port"
HARNESS_ENV_BUILDER = "build_env"

_MISSING = "<tests/_inventory.py::session_facade_imports() is unavailable>"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _inventory():
    """`tests/_inventory.py` with this cycle's derivation, or a loud failure."""
    import tests._inventory as inventory

    if not hasattr(inventory, "session_facade_imports"):
        pytest.fail(
            "skeleton: tests/_inventory.py has no session_facade_imports yet (plan.md "
            "## Files assigns it there: the session facade's import table as a map of "
            "bound name to source module). It must read MODULE-LEVEL imports only, so "
            f"the function-scope `{FUNCTION_SCOPE_BINDING}` binding inside main() is not "
            "mistaken for a re-export.",
            pytrace=False,
        )
    return inventory


def _import_table(*, path: Path | None = None) -> dict[str, str]:
    return _inventory().session_facade_imports(path=path or FACADE)


def _from_bindings(table: dict[str, str]) -> dict[str, str]:
    return {
        name: source
        for name, source in table.items()
        if source.split(".")[:2] == PACKAGE_PREFIX.split(".")
    }


def _other_bindings(table: dict[str, str]) -> dict[str, str]:
    return {name: source for name, source in table.items() if name not in _from_bindings(table)}


@functools.lru_cache(maxsize=1)
def _scanned_files() -> tuple[tuple[str, str], ...]:
    """Every `.py` and `.sh` in the tree EXCEPT the facade, as (relative path, text).

    Read once because it is 11 MB across 784 files and every case below asks the same
    question of the same bytes. Read-only, so no test can observe another's mutation.
    """
    facade = FACADE.resolve()
    collected = []
    for path in sorted(REPO.rglob("*")):
        if path.suffix not in SCANNED_SUFFIXES or not path.is_file():
            continue
        if EXCLUDED_DIRS & set(path.parts):
            continue
        if path.resolve() == facade:
            continue
        collected.append(
            (path.relative_to(REPO).as_posix(), path.read_text(encoding="utf-8", errors="ignore"))
        )
    return tuple(collected)


def _demand_sites(name: str) -> list[str]:
    """The files outside the facade that reach this name, by attribute or by quoted string.

    Both forms count. `server.writ_session._read_cache` is attribute demand;
    `monkeypatch.setattr(mod, "_read_cache", ...)` and `_require(writ_session, "cmd_x")`
    are string demand and both shapes are in this tree.
    """
    needles = (f".{name}", f'"{name}"', f"'{name}'")
    return [rel for rel, text in _scanned_files() if any(n in text for n in needles)]


def _declared_all(path: Path) -> list[str] | None:
    """The facade's `__all__` literal, or None if it declares none.

    Read from the AST rather than by importing, so a syntactically declared surface is
    judged without executing 95 re-export imports as a side effect of asking.
    """
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "__all__" not in targets:
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            raise AssertionError(
                f"{path.name} declares __all__ as {type(node.value).__name__}, not a list "
                "or tuple literal; a computed surface cannot be compared to the bindings"
            )
        names = []
        for element in node.value.elts:
            if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
                raise AssertionError(
                    f"{path.name} puts a non-string in __all__: {ast.dump(element)}"
                )
            names.append(element.value)
        return names
    return None


def _body_referenced_names(path: Path) -> set[str]:
    """Every bare name the module's own code loads, imports aside."""
    return {
        node.id
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _imported_names(path: Path) -> set[str]:
    """Every name any import statement in the module binds, at any scope."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {alias.asname or alias.name for alias in node.names}
    return names


def _copy(source: Path, tmp_path: Path) -> Path:
    destination = tmp_path / source.name
    shutil.copy2(source, destination)
    return destination


# --------------------------------------------------------------------------- #
# reusable detectors, so the mutation cases can point them at a copy
# --------------------------------------------------------------------------- #
def _assert_every_from_binding_has_demand(path: Path) -> None:
    """Detector for the demand capability, callable against a mutated copy."""
    bindings = _from_bindings(_import_table(path=path))
    assert bindings, (
        f"no `from {PACKAGE_PREFIX}.* import` bindings derived from {path}; this detector "
        "would then pass on any file at all"
    )
    undemanded = sorted(name for name in bindings if not _demand_sites(name))
    assert undemanded == [], (
        f"{path.name} re-exports {len(undemanded)} name(s) nothing outside it reaches: "
        f"{undemanded}. A re-export with no consumer is a claim the code makes and the "
        "tree does not support; delete it rather than declaring it"
    )


def _assert_all_equals_the_import_table(path: Path) -> None:
    """Detector for the declared-surface capability, callable against a mutated copy."""
    declared = _declared_all(path)
    assert declared is not None, (
        f"{path.name} declares no __all__, so nothing in the file says which of its "
        "imports are re-exports on purpose. That is the whole finding: 100 F401s and nine "
        "comment blocks of prose, none of which a reader or a linter can check"
    )
    bindings = set(_from_bindings(_import_table(path=path)))
    undeclared = sorted(bindings - set(declared))
    unbound = sorted(set(declared) - bindings)
    assert undeclared == [], (
        f"{path.name} imports {undeclared} from {PACKAGE_PREFIX}.* without declaring them "
        "in __all__"
    )
    assert unbound == [], (
        f"{path.name} declares {unbound} in __all__ without importing them"
    )
    assert len(declared) == len(set(declared)), (
        f"{path.name} repeats a name in __all__: "
        f"{sorted(n for n in set(declared) if declared.count(n) > 1)}"
    )


def _assert_no_unreferenced_plain_binding(path: Path) -> None:
    """Detector for the leftover-import capability, callable against a mutated copy."""
    table = _import_table(path=path)
    referenced = _body_referenced_names(path)
    stranded = sorted(name for name in _other_bindings(table) if name not in referenced)
    assert stranded == [], (
        f"{path.name} binds {stranded}, which its own body never references. These are "
        "not re-exports of the session package, they are leftovers from before the POL-6 "
        "split, and the over-approximating demand scan cannot judge them because `.json` "
        "and `.datetime` appear all over a tree that has nothing to do with this file"
    )


# --------------------------------------------------------------------------- #
# collection-time populations, never silently empty
# --------------------------------------------------------------------------- #
def _from_binding_names() -> list:
    try:
        import tests._inventory as inventory

        names = sorted(_from_bindings(inventory.session_facade_imports(path=FACADE)))
    except BaseException:  # noqa: BLE001
        return [pytest.param(_MISSING, id="import-table-unavailable")]
    return names or [pytest.param(_MISSING, id="import-table-empty")]


def _other_binding_names() -> list:
    try:
        import tests._inventory as inventory

        names = sorted(_other_bindings(inventory.session_facade_imports(path=FACADE)))
    except BaseException:  # noqa: BLE001
        return [pytest.param(_MISSING, id="import-table-unavailable")]
    return names or [pytest.param(_MISSING, id="no-non-package-bindings")]


# --------------------------------------------------------------------------- #
# capability: every re-export is demanded outside the facade
# --------------------------------------------------------------------------- #
class TestEveryReExportIsDemandedOutsideTheFacade:
    """RED TODAY for the eight names measured to have no consumer at all:
    `_migrate_command_log`, `_BUDGET_JSON`, `_budget_data`, `_CITATION_EXCERPT_MAX`,
    `_glob_match`, `_matches_any`, `_has_real_content` and `cmd_can_read_code`. Two of
    those look alive and are not: `cmd_can_read_code` is dispatched by
    `writ/session/cli_dispatch.py`, which imports it straight from `writ.session.gates`, so
    the facade's copy is reached by nobody and the `can-read-code` subcommand keeps working;
    `_has_real_content` and `_glob_match` are called inside `writ/session/gates.py` as bare
    names, which is demand on the package module and not on the facade.

    Those eight names are NOT written down as a population here. The point of the scan is
    that it finds them, and a hardcoded list would be blind to the ninth.
    """

    @pytest.mark.parametrize("name", _from_binding_names())
    def test_the_re_export_is_reached_by_at_least_one_other_file(self, name) -> None:
        assert name is not _MISSING, name
        sites = _demand_sites(name)
        assert sites, (
            f"nothing outside bin/lib/writ-session.py reaches `{name}`, as attribute "
            "access or as a quoted string, in any .py or .sh file in the tree. The scan "
            "over-approximates on purpose, so a verdict of zero demand is not a near miss"
        )

    def test_the_whole_surface_passes_the_detector(self) -> None:
        _assert_every_from_binding_has_demand(FACADE)


class TestTheDemandScanIsConditional:
    """A scan that matched everything would make the class above pass on any facade at all,
    including one that re-exported names nothing has ever called."""

    def test_a_name_nothing_in_the_tree_demands_has_no_demand_sites(self) -> None:
        assert _demand_sites(_SENTINEL_NO_DEMAND) == [], (
            f"the scan reports demand for {_SENTINEL_NO_DEMAND!r}, a name this repository "
            "does not contain. It is assembled from two fragments at import time exactly "
            "so this module is not itself a demand site for it; if that assembly was "
            "flattened into a literal, this is where it shows up"
        )

    def test_a_name_the_tree_really_uses_does_have_demand_sites(self) -> None:
        """The positive pole. Without it, an always-empty scan would satisfy the test
        above, report every re-export as undemanded, and send the next reader to delete a
        live surface.

        `os.environ` is chosen because it is attribute demand this tree makes in over two
        hundred files and has nothing to do with the facade, and because the floor is far
        above the single site this module's own literal contributes. It deliberately does
        not go through the import-table derivation, so this pole still stands while that
        derivation is being written.
        """
        sites = _demand_sites(_LIVE_ATTRIBUTE)
        assert len(sites) >= 10, (
            f"the scan found {len(sites)} site(s) for `{_LIVE_ATTRIBUTE}`, an attribute "
            "this tree reaches in hundreds of files. A scan this blind would report every "
            "re-export as undemanded"
        )

    def test_a_facade_carrying_the_sentinel_is_rejected_and_one_without_it_is_not(
        self, tmp_path
    ) -> None:
        """Polarity on a synthetic pair the test authors itself, so the real tree is never
        mutated and the control does not depend on today's facade being clean."""
        table = _from_bindings(_import_table())
        demanded = [name for name in sorted(table) if _demand_sites(name)]
        assert demanded, "no demanded re-export to build the positive control from"

        good = tmp_path / "good-facade.py"
        good.write_text(
            "\n".join(f"from {table[name]} import {name}" for name in demanded[:3]) + "\n"
        )
        _assert_every_from_binding_has_demand(good)

        bad = tmp_path / "bad-facade.py"
        bad.write_text(
            good.read_text()
            + f"from {PACKAGE_PREFIX}.cache import {_SENTINEL_NO_DEMAND}\n"
        )
        with pytest.raises(AssertionError, match=_SENTINEL_NO_DEMAND):
            _assert_every_from_binding_has_demand(bad)


# --------------------------------------------------------------------------- #
# capability: the declared surface equals the import table
# --------------------------------------------------------------------------- #
class TestTheDeclaredSurfaceEqualsTheImportTable:
    """RED TODAY: the facade declares no `__all__` at all, so there is nothing in the file
    that says which of its imports are re-exports on purpose.

    `__all__` lists the re-export surface, so it holds the `from writ.session.* import`
    bindings and NOT `main`, which is defined here rather than re-exported. Nothing in the
    repository star-imports anything (verified: zero `import *` in the tree) and the facade
    is loaded by path through importlib, so this has no runtime effect beyond documenting
    intent and satisfying pyflakes. The idiom is already used in `writ/server/models.py`
    and `writ/session/pr_comments.py`.
    """

    def test_the_facade_declares_a_re_export_surface(self) -> None:
        assert _declared_all(FACADE) is not None, (
            "bin/lib/writ-session.py declares no __all__. The alternatives were "
            "considered and rejected: `# noqa: F401` on 100 lines or a per-file ignore in "
            "pyproject.toml silences the reader as well as the linter, and the redundant "
            "alias form (`import x as x`) is honored by ruff as an explicit re-export only "
            "in __init__.py and stub files, so it would clear nothing in bin/lib"
        )

    def test_the_declared_surface_matches_the_bindings_exactly(self) -> None:
        _assert_all_equals_the_import_table(FACADE)

    def test_the_surface_does_not_declare_names_the_facade_defines(self) -> None:
        """`main` is defined here, not re-exported, so it does not belong to the surface."""
        declared = _declared_all(FACADE)
        if declared is None:
            pytest.fail(
                "skeleton: no __all__ to check yet (plan.md ## Files: the facade adds "
                "__all__ declaring the remaining re-export surface)",
                pytrace=False,
            )
        defined = {
            node.name
            for node in ast.parse(FACADE.read_text()).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert defined, "the facade defines no function; this check would be vacuous"
        assert not (defined & set(declared)), (
            f"__all__ declares {sorted(defined & set(declared))}, which this file DEFINES; "
            "the surface is the re-export list"
        )


class TestTheSurfaceDetectorIsConditional:

    def test_a_matching_pair_passes_and_each_direction_of_drift_fails(self, tmp_path) -> None:
        """Both failure directions on a synthetic pair: imported-but-undeclared, and
        declared-but-unbound. Each must fail BY NAME, so the next reader is told which
        edit forgot which half."""
        table = _from_bindings(_import_table())
        names = [name for name in sorted(table) if _demand_sites(name)][:3]
        assert len(names) >= 2, "need at least two demanded re-exports for this pair"

        matching = tmp_path / "matching-facade.py"
        matching.write_text(
            "\n".join(f"from {table[name]} import {name}" for name in names)
            + "\n__all__ = "
            + repr(names)
            + "\n"
        )
        _assert_all_equals_the_import_table(matching)

        undeclared = tmp_path / "undeclared-facade.py"
        undeclared.write_text(
            "\n".join(f"from {table[name]} import {name}" for name in names)
            + "\n__all__ = "
            + repr(names[:-1])
            + "\n"
        )
        with pytest.raises(AssertionError, match=names[-1]):
            _assert_all_equals_the_import_table(undeclared)

        unbound = tmp_path / "unbound-facade.py"
        unbound.write_text(
            "\n".join(f"from {table[name]} import {name}" for name in names[:-1])
            + "\n__all__ = "
            + repr(names)
            + "\n"
        )
        with pytest.raises(AssertionError, match=names[-1]):
            _assert_all_equals_the_import_table(unbound)

    def test_dropping_a_name_from_the_real_files_surface_is_caught(self, tmp_path) -> None:
        """The same mutation against a COPY of the real facade, never the real file.

        RED TODAY, and the failure is the missing production behaviour: there is no
        __all__ in the real file yet, so there is nothing to drop.
        """
        declared = _declared_all(FACADE)
        if declared is None:
            pytest.fail(
                "skeleton: bin/lib/writ-session.py has no __all__ yet, so this detector "
                "cannot be proven conditional against the real file. plan.md ## Files: "
                "the facade adds __all__ declaring the remaining re-export surface.",
                pytrace=False,
            )
        copy = _copy(FACADE, tmp_path)
        source = copy.read_text()
        victim = declared[0]
        mutant = source.replace(repr(victim) + ",", "", 1)
        assert mutant != source, f"could not remove {victim!r} from the copied __all__"
        copy.write_text(mutant)
        with pytest.raises(AssertionError, match=victim):
            _assert_all_equals_the_import_table(copy)


# --------------------------------------------------------------------------- #
# capability: no binding the facade's own body ignores
# --------------------------------------------------------------------------- #
class TestTheFacadeBindsNoNameItsOwnBodyIgnores:
    """RED TODAY for `hashlib`, `json`, `tempfile`, `datetime` and `timezone`. The file's
    own code is `main()` plus the `sys.path` bootstrap, which reference `os` and `sys` and
    nothing else."""

    @pytest.mark.parametrize("name", _other_binding_names())
    def test_the_binding_is_referenced_by_the_facades_own_code(self, name) -> None:
        assert name is not _MISSING, name
        assert name in _body_referenced_names(FACADE), (
            f"bin/lib/writ-session.py binds `{name}` and never uses it. It is not a "
            f"re-export either: `{name}` does not come from {PACKAGE_PREFIX}.*, so no "
            "consumer reaches it through this facade"
        )

    def test_the_whole_file_passes_the_detector(self) -> None:
        _assert_no_unreferenced_plain_binding(FACADE)

    def test_an_unused_import_added_to_a_copy_is_caught(self, tmp_path) -> None:
        """Polarity on a synthetic pair: a referenced import passes, an ignored one fails
        by name."""
        used = tmp_path / "used-import.py"
        used.write_text("import os\n\n\ndef main():\n    return os.getcwd()\n")
        _assert_no_unreferenced_plain_binding(used)

        ignored = tmp_path / "ignored-import.py"
        ignored.write_text(
            "import os\nimport uuid\n\n\ndef main():\n    return os.getcwd()\n"
        )
        with pytest.raises(AssertionError, match="uuid"):
            _assert_no_unreferenced_plain_binding(ignored)


# --------------------------------------------------------------------------- #
# capability: the vestigial firedrill import (the 101st F401)
# --------------------------------------------------------------------------- #
class TestTheVestigialFiredrillImportIsGone:
    """The helper itself stays. Only the import into the refusal module goes: the property
    it would have served is already proven better at test_bash_refusals.py:148-155, which
    call `build_env`, read `WRIT_PORT` out of the RESULT, and assert a connection to that
    port raises. That reads the address the builder actually chose rather than the one a
    test set, which is this repository's keystone about a unix socket winning over a
    base_url."""

    def test_the_refusal_module_no_longer_imports_the_port_helper(self) -> None:
        assert VESTIGIAL_FIREDRILL_IMPORT not in _imported_names(FIREDRILL_REFUSALS), (
            f"tests/firedrill/test_bash_refusals.py still imports "
            f"`{VESTIGIAL_FIREDRILL_IMPORT}` and references it nowhere"
        )

    def test_the_refusal_module_still_imports_the_env_builder_and_uses_it(self) -> None:
        """Anti-vacuity: an import block deleted wholesale, or a module emptied out, would
        satisfy the test above. GREEN TODAY and after, because the surviving proof at
        lines 148 to 155 is exactly the `build_env` call this pins."""
        imported = _imported_names(FIREDRILL_REFUSALS)
        referenced = _body_referenced_names(FIREDRILL_REFUSALS)
        assert HARNESS_ENV_BUILDER in imported, (
            f"tests/firedrill/test_bash_refusals.py no longer imports "
            f"{HARNESS_ENV_BUILDER}; the port proof reads WRIT_PORT out of its result"
        )
        assert HARNESS_ENV_BUILDER in referenced, (
            f"{HARNESS_ENV_BUILDER} is imported but never called, which is the same defect "
            "this class deletes one line above"
        )

    def test_the_harness_still_defines_the_port_helper_and_calls_it(self) -> None:
        """The other half, and the reason only the import is deleted: `_harness.build_env`
        fills WRIT_PORT from this helper, so removing the function would silently point
        every drill hook at whatever is listening on the default port."""
        tree = ast.parse(FIREDRILL_HARNESS.read_text(encoding="utf-8"))
        definitions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        assert VESTIGIAL_FIREDRILL_IMPORT in definitions, (
            f"tests/firedrill/_harness.py no longer defines {VESTIGIAL_FIREDRILL_IMPORT}"
        )
        builder = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == HARNESS_ENV_BUILDER
        ]
        assert builder, f"tests/firedrill/_harness.py defines no {HARNESS_ENV_BUILDER}"
        called = {
            node.func.id
            for node in ast.walk(builder[0])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert VESTIGIAL_FIREDRILL_IMPORT in called, (
            f"{HARNESS_ENV_BUILDER} no longer calls {VESTIGIAL_FIREDRILL_IMPORT}, so the "
            "drill's WRIT_PORT is not guaranteed to be a closed port"
        )


# --------------------------------------------------------------------------- #
# the derivation itself
# --------------------------------------------------------------------------- #
class TestTheImportTableDerivationIsSound:
    """The risk this whole module carries: a derivation that silently returned nothing
    would make every assertion above pass on any tree, which is worse than the duplicated
    literals it replaced."""

    def test_the_table_is_populated(self) -> None:
        table = _import_table()
        assert len(_from_bindings(table)) >= 10, (
            f"the facade's re-export table derived implausibly few entries: {table!r}"
        )
        assert _other_bindings(table), (
            "the table holds no non-package bindings at all, yet the file opens with a "
            "block of them; the derivation is dropping plain imports"
        )

    def test_the_table_excludes_the_function_scope_import(self) -> None:
        """MEASURED, and it is the difference between 95 re-exports and 96: the facade's
        `main()` does `from writ.session.cli_dispatch import dispatch` at call time. It is
        used, so ruff does not flag it, and it is not a module-level binding, so declaring
        it in `__all__` would name something the module does not export."""
        assert FUNCTION_SCOPE_BINDING not in _import_table(), (
            f"the import table includes `{FUNCTION_SCOPE_BINDING}`, which is imported "
            "inside main() rather than at module level. Scan the module body, not the "
            "whole AST"
        )

    def test_every_source_module_in_the_table_resolves(self) -> None:
        """The map's VALUE half has to mean something, or `from {source} import {name}`
        in the mutation cases above builds nonsense."""
        sources = sorted(set(_import_table().values()))
        assert sources, "no source modules derived"
        unresolvable = [
            source for source in sources if importlib.util.find_spec(source) is None
        ]
        assert unresolvable == [], (
            f"the table names source module(s) that do not resolve: {unresolvable}"
        )

    def test_the_derivation_reads_the_file_it_is_given(self, tmp_path) -> None:
        """A hardcoded population is blind, not just stale: if this returned a remembered
        list, the mutant and the original would agree and every assertion here would be
        decoration."""
        copy = _copy(FACADE, tmp_path)
        source = copy.read_text()
        table = _import_table(path=copy)
        victim = sorted(_from_bindings(table))[0]
        mutant = source.replace(f"    {victim},\n", "", 1)
        assert mutant != source, f"could not remove the {victim!r} import from the copy"
        copy.write_text(mutant)
        assert victim not in _import_table(path=copy)
