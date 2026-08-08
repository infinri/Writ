"""The guard for absence claims, tested against the misses it exists to prevent.

An absence claim passes by finding nothing, so the only interesting question about
tests/_scope.py is whether it fails when the search cannot reach the code. Every test here
is built the same way: plant the offender where it actually lived, then assert BOTH that the
guard rejects the scan AND that the naive scan reported a clean zero. Without the second
half "the guard catches it" is unfalsifiable -- a checker that refused every scan would pass
just as well.

The anti-vacuity here is deliberately about SCOPE, not about regexes. All three real misses
had a regex with teeth; what none of them had was proof the search opened the file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._scope import DEFAULT_IGNORE, ScopeError, Universe, scan, shell_file
from tests.test_session_identity_no_fallback import SCAN_ROOTS, SHELL_UNIVERSE, SYNTHETIC

REPO = Path(__file__).resolve().parent.parent

# The fallback that was live in bin/lib/common.sh while the hooks-only scan read clean.
HISTORICAL_SYNTHESIS = (
    'if [ -z "${HOOK_SESSION_ID:-}" ]; then\n'
    '    HOOK_SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d \' \')\n'
    "fi\n"
)


def _shell_universe(base: Path, dirs: tuple[str, ...]) -> Universe:
    return Universe(base=base, dirs=dirs, match=shell_file)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """hooks/scripts + bin/lib, the two-directory shape of the real repo's hook surface."""
    (tmp_path / "hooks" / "scripts").mkdir(parents=True)
    (tmp_path / "bin" / "lib").mkdir(parents=True)
    (tmp_path / "hooks" / "scripts" / "a-hook.sh").write_text(
        '#!/usr/bin/env bash\nsource "$(dirname "$0")/../../bin/lib/common.sh"\n'
    )
    (tmp_path / "bin" / "lib" / "common.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    return tmp_path


class TestIncompleteRootsFailEvenWithZeroHits:
    """The whole point. A scan that searches a subset must be rejected, not believed."""

    def test_incomplete_roots_are_rejected_when_the_offender_is_outside_them(
        self, tree: Path
    ) -> None:
        (tree / "bin" / "lib" / "common.sh").write_text(HISTORICAL_SYNTHESIS)
        universe = _shell_universe(tree, ("hooks/scripts", "bin/lib"))

        with pytest.raises(ScopeError) as excinfo:
            scan(SYNTHETIC, roots=[tree / "hooks" / "scripts"], universe=universe)

        assert "bin/lib" in str(excinfo.value), "the message must name the unopened ground"

    def test_the_naive_scan_of_those_same_roots_reports_a_clean_zero(self, tree: Path) -> None:
        """The other half: the guard is the only thing standing between that scan and a
        green test. Reading exactly the roots it declared finds nothing, because the
        offender is not in them."""
        (tree / "bin" / "lib" / "common.sh").write_text(HISTORICAL_SYNTHESIS)

        hits = {p.name: SYNTHETIC.findall(p.read_text())
                for p in (tree / "hooks" / "scripts").glob("*.sh")}
        assert {k: v for k, v in hits.items() if v} == {}

    def test_widening_the_roots_to_the_universe_finds_it(self, tree: Path) -> None:
        (tree / "bin" / "lib" / "common.sh").write_text(HISTORICAL_SYNTHESIS)
        universe = _shell_universe(tree, ("hooks/scripts", "bin/lib"))

        hits = scan(SYNTHETIC, roots=[tree / "hooks" / "scripts", tree / "bin" / "lib"],
                    universe=universe)

        assert list(hits) == ["bin/lib/common.sh"]

    def test_complete_roots_with_nothing_to_find_pass(self, tree: Path) -> None:
        """The control. A zero from roots that cover the universe is a zero worth having,
        and if this failed every rejection above would be worthless."""
        universe = _shell_universe(tree, ("hooks/scripts", "bin/lib"))

        assert scan(SYNTHETIC, roots=[tree / "hooks" / "scripts", tree / "bin" / "lib"],
                    universe=universe) == {}

    def test_a_parent_root_covers_the_directories_beneath_it(self, tree: Path) -> None:
        """Roots are walked recursively, so declaring `bin` covers `bin/lib`. Coverage is
        about what gets opened, not about spelling every path twice."""
        (tree / "bin" / "lib" / "common.sh").write_text(HISTORICAL_SYNTHESIS)
        universe = _shell_universe(tree, ("hooks/scripts", "bin/lib"))

        hits = scan(SYNTHETIC, roots=[tree / "hooks", tree / "bin"], universe=universe)

        assert list(hits) == ["bin/lib/common.sh"]


class TestTheUniverseIsCheckedToo:
    """A declared universe is still a declaration. If it were trusted, the cheapest way to
    pass any scan would be to declare a universe of one directory."""

    def test_a_file_of_that_kind_outside_the_universe_is_rejected(self, tree: Path) -> None:
        (tree / "scripts").mkdir()
        (tree / "scripts" / "stray.sh").write_text(HISTORICAL_SYNTHESIS)
        universe = _shell_universe(tree, ("hooks/scripts", "bin/lib"))

        with pytest.raises(ScopeError) as excinfo:
            scan(SYNTHETIC, roots=[tree / "hooks" / "scripts", tree / "bin" / "lib"],
                 universe=universe)

        assert "scripts/stray.sh" in str(excinfo.value)

    def test_an_ignored_directory_needs_no_universe_entry(self, tree: Path) -> None:
        """`ignore` is the escape hatch, and it is the only one: a directory leaves the
        universe by being named in a list a reviewer can read, not by being forgotten."""
        (tree / "archive").mkdir()
        (tree / "archive" / "old.sh").write_text(HISTORICAL_SYNTHESIS)
        universe = Universe(base=tree, dirs=("hooks/scripts", "bin/lib"), match=shell_file,
                            ignore=DEFAULT_IGNORE + ("archive",))

        assert scan(SYNTHETIC, roots=[tree / "hooks" / "scripts", tree / "bin" / "lib"],
                    universe=universe) == {}

    def test_a_root_that_does_not_exist_is_rejected(self, tree: Path) -> None:
        """A typo'd or renamed root contributes zero files, which is the same clean zero by
        a different route."""
        universe = _shell_universe(tree, ("hooks/scripts", "bin/lib"))

        with pytest.raises(ScopeError) as excinfo:
            scan(SYNTHETIC, roots=[tree / "hooks" / "scrips", tree / "bin" / "lib"],
                 universe=universe)

        assert "scrips" in str(excinfo.value)

    def test_an_extensionless_shebang_file_is_in_the_universe(self, tree: Path) -> None:
        """`hooks/git/post-commit` has no suffix, and a `*.sh` glob is exactly how it stayed
        out of every scan of this repo."""
        (tree / "hooks" / "git").mkdir()
        (tree / "hooks" / "git" / "post-commit").write_text(
            "#!/bin/sh\n" + HISTORICAL_SYNTHESIS
        )
        universe = _shell_universe(tree, ("hooks/scripts", "hooks/git", "bin/lib"))

        assert [p for p in (tree / "hooks" / "git").glob("*.sh")] == [], "the glob sees none"
        hits = scan(SYNTHETIC, roots=[tree / "hooks", tree / "bin" / "lib"], universe=universe)
        assert list(hits) == ["hooks/git/post-commit"]


class TestMultiLineConstructs:
    """Instance 2's shape: the offender spans lines, so a line-by-line scan cannot see it."""

    EMBEDDED = (
        "#!/usr/bin/env bash\n"
        "SID=$(python3 -c '\n"
        "import json, sys\n"
        'print(json.load(sys.stdin)["session_id"])\n'
        "')\n"
    )
    INLINE_JSON = re.compile(r"python3\s+-c\b.*?\bjson\b", re.S)

    def test_a_line_by_line_scan_reports_zero(self, tree: Path) -> None:
        (tree / "hooks" / "scripts" / "a-hook.sh").write_text(self.EMBEDDED)

        per_line = [ln for ln in self.EMBEDDED.splitlines() if self.INLINE_JSON.search(ln)]
        assert per_line == [], (
            "the construct fits on one line, so the multi-line read below is not what "
            "finds it -- re-derive the miss"
        )

    def test_the_whole_file_read_finds_it(self, tree: Path) -> None:
        (tree / "hooks" / "scripts" / "a-hook.sh").write_text(self.EMBEDDED)
        universe = _shell_universe(tree, ("hooks/scripts", "bin/lib"))

        hits = scan(self.INLINE_JSON, roots=[tree / "hooks" / "scripts", tree / "bin" / "lib"],
                    universe=universe)

        assert list(hits) == ["hooks/scripts/a-hook.sh"]


class TestAgainstTheRealRepoLayout:
    """Instance 1, reconstructed against the layout it actually happened in.

    The tests above prove the guard works on a tree built to make it work. These prove the
    declaration in test_session_identity_no_fallback.py is a real one: that the universe
    matches this repo today, and that the exact scope the fallbacks walked past is rejected.
    """

    def test_the_original_hooks_only_scope_is_rejected(self) -> None:
        with pytest.raises(ScopeError) as excinfo:
            scan(SYNTHETIC, roots=[REPO / "hooks" / "scripts"], universe=SHELL_UNIVERSE)

        message = str(excinfo.value)
        assert "bin/lib" in message, (
            "bin/lib is where the synthesis lived and where 21 hooks read it from; the "
            "rejection has to name it"
        )

    def test_the_widened_scope_that_replaced_it_is_also_rejected(self) -> None:
        """Even hooks/scripts + bin/lib -- the scope another agent widened to -- does not
        cover the universe. That widening was correct and still left four directories of
        shell unopened, which is precisely the difference between remembering and checking.
        """
        with pytest.raises(ScopeError) as excinfo:
            scan(SYNTHETIC, roots=[REPO / "hooks" / "scripts", REPO / "bin" / "lib"],
                 universe=SHELL_UNIVERSE)

        for missed in ("bin", "scripts", "scripts/lib", "hooks/git"):
            assert missed in str(excinfo.value)

    def test_the_declared_universe_still_holds_for_this_repo(self) -> None:
        """The one assertion that will age: it fails the day a shell file lands in a
        seventh directory, which is the day the declaration would otherwise go stale."""
        stragglers = sorted(str(p.relative_to(REPO)) for p in SHELL_UNIVERSE.stragglers())
        assert stragglers == [], (
            f"shell files live outside the declared universe {list(SHELL_UNIVERSE.dirs)}: "
            f"{stragglers}. Add the directory to SHELL_UNIVERSE.dirs and to SCAN_ROOTS."
        )

    def test_the_live_scope_covers_the_universe_and_opens_the_file_that_was_missed(
        self,
    ) -> None:
        universe = SHELL_UNIVERSE
        assert universe.uncovered(SCAN_ROOTS) == []
        opened = {str(p.relative_to(REPO)) for r in SCAN_ROOTS for p in universe.walk(r)}
        assert "bin/lib/common.sh" in opened, "instance 1's file"
        assert "hooks/git/post-commit" in opened, "the extensionless one"
        assert len([p for p in opened if p.startswith("hooks/scripts/")]) > 20


class TestMatchesAndFiltering:
    """The mechanics the assertions above rest on."""

    def test_exempt_takes_a_name_or_a_relative_path(self, tree: Path) -> None:
        (tree / "bin" / "lib" / "common.sh").write_text(HISTORICAL_SYNTHESIS)
        universe = _shell_universe(tree, ("hooks/scripts", "bin/lib"))
        roots = [tree / "hooks" / "scripts", tree / "bin" / "lib"]

        assert scan(SYNTHETIC, roots=roots, universe=universe, exempt={"common.sh"}) == {}
        assert scan(SYNTHETIC, roots=roots, universe=universe,
                    exempt={"bin/lib/common.sh"}) == {}
        assert scan(SYNTHETIC, roots=roots, universe=universe, exempt={"other.sh"}) != {}

    def test_transform_preprocesses_the_text(self, tree: Path) -> None:
        commented = "".join(f"# {ln}\n" for ln in HISTORICAL_SYNTHESIS.splitlines())
        (tree / "bin" / "lib" / "common.sh").write_text(commented)
        universe = _shell_universe(tree, ("hooks/scripts", "bin/lib"))
        roots = [tree / "hooks" / "scripts", tree / "bin" / "lib"]
        drop_comments = lambda src: re.sub(r"^[ \t]*#.*$", "", src, flags=re.M)  # noqa: E731

        assert scan(SYNTHETIC, roots=roots, universe=universe) != {}
        assert scan(SYNTHETIC, roots=roots, universe=universe, transform=drop_comments) == {}

    def test_a_predicate_may_stand_in_for_a_regex(self, tree: Path) -> None:
        """Not every absence claim is a regex. A callable over the whole file text works
        the same way and gets the same scope check."""
        (tree / "bin" / "lib" / "common.sh").write_text(HISTORICAL_SYNTHESIS)
        universe = _shell_universe(tree, ("hooks/scripts", "bin/lib"))
        has_ppid = lambda text: ["ppid"] if "ppid" in text else []  # noqa: E731

        hits = scan(has_ppid, roots=[tree / "hooks" / "scripts", tree / "bin" / "lib"],
                    universe=universe)

        assert hits == {"bin/lib/common.sh": ["ppid"]}

    def test_nested_roots_do_not_report_the_same_file_twice(self, tree: Path) -> None:
        (tree / "bin" / "lib" / "common.sh").write_text(HISTORICAL_SYNTHESIS)
        universe = _shell_universe(tree, ("hooks/scripts", "bin/lib"))

        hits = scan(SYNTHETIC, roots=[tree / "bin", tree / "bin" / "lib",
                                      tree / "hooks" / "scripts"], universe=universe)

        assert list(hits) == ["bin/lib/common.sh"]
