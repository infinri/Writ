"""An absence claim must declare the ground it covers, and the declaration is checked.

An absence claim is a test that asserts something does not exist ANYWHERE: "no hook
synthesizes a session id", "zero inline `python3 -c` JSON reads remain". It differs from every
other assertion in one way that matters: it passes by finding nothing, so a search that never
reaches the offender is indistinguishable from a codebase that does not contain it. The test
is green either way, and the green is read as proof of removal.

Three of those shipped in this repo on 2026-08-08:

  1. test_session_identity_no_fallback.py globbed `hooks/scripts/*.sh` and reported 0
     offenders while the session-id fallback sat in `bin/lib/common.sh`, the one file 21
     hooks source. It scanned 37 copies of the fix and never opened the original.
  2. test_prompt_path_process_budget.py matched `python3 -c` and `json` on the SAME LINE, so
     an embedded block spanning lines was invisible. It reported 0 while 5 were live.
  3. test_pol5b1_load_hook_env.py asserted a fallback id MUST be produced -- a wrong
     assertion rather than a narrow scan, and not what this module addresses.

Each was backed by an anti-vacuity test that planted a matching string and proved the REGEX
had teeth. None proved the SEARCH ROOTS reached the code. That is the gap here.

test_session_identity_no_fallback.py is retrofitted. test_prompt_path_process_budget.py
(instance 2) is the next candidate and is deliberately untouched: its multi-line scan is
being repaired separately, and it should adopt this module once that lands -- a whole-file
read is half its fix, and a declared universe of "everywhere a hook can spawn a process"
is the other half.

Two declarations, both checked before a byte is searched:

  - the `Universe`: every directory a file of this kind may legitimately live in. If a file of
    that kind turns up outside it, the universe is stale and the scan refuses to run, so the
    list cannot quietly fall behind the repo.
  - the `roots`: the directories this scan actually opens. If they do not cover the universe,
    the scan refuses to run. A test that searches a subset must say so and be rejected,
    instead of reporting a zero it never earned.

Files are read WHOLE and matched whole, never line by line, because instance 2 was exactly a
multi-line construct. A pattern that must span lines still has to say so itself (`re.S`, or an
explicit `\\n`); this module only guarantees the newlines are there to match against.

    from tests._scope import Universe, scan, shell_file

    SHELL = Universe(base=REPO, dirs=("hooks/scripts", "bin", "bin/lib"), match=shell_file)
    offenders = scan(SYNTHETIC, roots=[REPO / d for d in SHELL.dirs], universe=SHELL)
    assert offenders == {}
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Iterable, Iterator, Sequence

# Never walked, in any universe: nothing here is source anyone reads or runs. Any *.egg-info
# build directory goes too (suffix match, since the name is package-specific) -- a stale copy
# of a fixed file is the one straggler that would fail a scan for no reason.
DEFAULT_IGNORE = (
    ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules",
)


class ScopeError(AssertionError):
    """The scan cannot observe everything the claim covers, so its zero means nothing.

    An AssertionError subclass so pytest reports it as a failed assertion rather than an
    error in the test: an under-scoped absence claim is a failing test, not a broken one.
    """


def shell_file(path: Path) -> bool:
    """A shell source file: `*.sh`, or extensionless with a sh/bash shebang.

    The second arm is not hypothetical: `hooks/git/post-commit` and `bin/writ` carry no
    suffix, and a `*.sh` glob is how they stay out of every scan.
    """
    if path.suffix == ".sh":
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as fh:
            head = fh.readline(200).decode("utf-8", "replace")
    except OSError:
        return False
    return head.startswith("#!") and ("bash" in head or re.search(r"\bsh\b", head) is not None)


def _is_under(rel: Path, dirs: Sequence[str]) -> bool:
    parents = set(rel.parents)
    return any(Path(d) == rel.parent or Path(d) in parents for d in dirs)


@dataclass(frozen=True)
class Universe:
    """Every directory a file of this kind may legitimately live in, in this repo.

    `dirs` are relative to `base`. `match` decides what "of this kind" means. `ignore`
    names directories that are never walked, and every entry is a claim that nothing
    executable lives there -- keep it short and keep a reason on each one.
    """

    base: Path
    dirs: tuple[str, ...]
    match: Callable[[Path], bool]
    ignore: tuple[str, ...] = DEFAULT_IGNORE

    def walk(self, root: Path) -> Iterator[Path]:
        """Every file of this kind under `root`, recursively, ignored dirs skipped."""
        for path in sorted(root.rglob("*")):
            if any(part in self.ignore or part.endswith(".egg-info") for part in path.parts):
                continue
            if path.is_file() and self.match(path):
                yield path

    def stragglers(self) -> list[Path]:
        """Files of this kind that live OUTSIDE the declared dirs.

        The universe checking itself. Without this, the cheapest way to make any scan pass
        is to declare a universe of one directory.
        """
        return [p for p in self.walk(self.base)
                if not _is_under(p.relative_to(self.base), self.dirs)]

    def uncovered(self, roots: Iterable[Path]) -> list[str]:
        """Declared dirs that no root reaches (a root reaches itself and everything under it)."""
        resolved = [r.resolve() for r in roots]
        out = []
        for d in self.dirs:
            full = (self.base / d).resolve()
            if not any(r == full or r in full.parents for r in resolved):
                out.append(d)
        return out


def scan(
    pattern: re.Pattern | Callable[[str], list[str]],
    *,
    roots: Iterable[Path],
    universe: Universe,
    transform: Callable[[str], str] | None = None,
    exempt: Collection[str] = (),
) -> dict[str, list]:
    """Search `roots` for `pattern`, after proving `roots` cover `universe`.

    Returns {path relative to universe.base: matches}, empty when the claim holds.

    Raises ScopeError -- before reading any file -- when a declared root does not exist,
    when a file of this kind lives outside the declared universe, or when the declared
    roots do not cover the declared universe. In every one of those cases the search would
    have returned a zero that proves nothing.

    `pattern` is a compiled regex (matched with findall) or any callable taking the file
    text and returning a list of matches. Each file is read and matched WHOLE, so multi-line
    constructs are visible. `transform` preprocesses the text (comment stripping, say);
    `exempt` skips files by name or by path relative to universe.base.
    """
    roots = [Path(r) for r in roots]

    missing = [str(r) for r in roots if not r.is_dir()]
    if missing:
        raise ScopeError(
            f"declared search roots do not exist: {missing}. A root that is not there "
            f"contributes zero files, so the scan would pass by searching nothing."
        )

    stragglers = universe.stragglers()
    if stragglers:
        rel = sorted(str(p.relative_to(universe.base)) for p in stragglers)
        raise ScopeError(
            f"the declared universe {list(universe.dirs)} is out of date: "
            f"{len(rel)} file(s) of this kind live outside it: {rel[:10]}. Add the "
            f"directory to the universe (and to the roots) or ignore it with a reason."
        )

    uncovered = universe.uncovered(roots)
    if uncovered:
        raise ScopeError(
            f"the declared roots {[str(r) for r in roots]} do not cover the universe: "
            f"{uncovered} would never be opened. An absence claim over a subset of the "
            f"places the code can live reports zero whether or not the code is there -- "
            f"widen the roots, or narrow the claim and the universe together."
        )

    find = pattern.findall if isinstance(pattern, re.Pattern) else pattern
    hits: dict[str, list] = {}
    for path in sorted({p.resolve() for r in roots for p in universe.walk(r)}):
        rel = str(path.relative_to(universe.base.resolve()))
        if path.name in exempt or rel in exempt:
            continue
        text = path.read_text(errors="replace")
        matches = find(transform(text) if transform else text)
        if matches:
            hits[rel] = list(matches)
    return hits
