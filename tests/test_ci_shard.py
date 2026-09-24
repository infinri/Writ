"""Pins the shard planner that splits the CI `test` job into four balanced,
file-level pytest shards with a coverage guard.

Mirrors, item for item, `.claude/plans/6bd39d6d-6668-46eb-9a23-b2eb8edede83/capabilities.md`.
Each class below is named after (or groups) one bullet from that file; the two
`(operational)` bullets (CI run after push, branch-protection check) are not
testable here and have no class.

Per ENF-PROC-TDD-001 this file is written, and observed failing, before
`scripts/ci_shard.py`, `scripts/ci_timings.json` and the workflow/Makefile
wiring exist. It defines the contract the implementer's script must satisfy:

    discover(root: Path) -> list[str]
        Every `test_*.py` / `*_test.py` under `root/tests`, at any depth,
        excluding pytest's default `norecursedirs`, as sorted repo-relative
        POSIX strings in pytest's joint alphabetical file-and-directory order.

    load_timings(path: Path) -> dict[str, float]
        Parses the committed timings JSON. Tolerates a missing file by
        returning {} (asserted only indirectly here; the committed file is
        expected to exist once the script ships).

    assign(files: list[str], timings: dict[str, float], shards: int)
        -> list[list[str]]
        Greedy longest-processing-time-first placement, described in
        plan.md's "Assignment" section: order files by (-weight, path),
        place each in the shard with the smallest (load, index), re-sort
        each shard's list into collection order before returning. A file
        missing from `timings` gets the median of the known weights for
        files present in `files`, or 1.0 when none are known. A `timings`
        entry for a file absent from `files` is ignored.

    rebuild_timings(xml_paths: list[Path], root: Path) -> dict[str, float]
        Sums xunit1 `testcase/@time` per `testcase/@file`; a testcase
        without a `file` attribute is mapped through `classname` to the
        longest discovered-file prefix. Merges multiple XML files.

    CLI (`python3 scripts/ci_shard.py ...`), also exposed as `main(argv)`
    for in-process testing with monkeypatching:
        --shard K --shards N [--root R] [--timings T]
            prints shard K's files, one per line, to stdout; a one-line
            summary to stderr; exits non-zero with no stdout on a bad
            shard index, shards < 1, an empty tree, or a whitespace path.
        --check --shards N [--root R] [--timings T]
            exits 0 with per-shard counts on a valid partition; exits 1
            naming missing/duplicated paths on a broken one.
        --rebuild-timings XML [XML ...] [--root R] [--timings T]
            writes sorted JSON, 0.1s precision, trailing newline, to
            `--timings` (default scripts/ci_timings.json).
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "ci_shard.py"
TIMINGS = REPO / "scripts" / "ci_timings.json"
PYPROJECT = REPO / "pyproject.toml"
MAKEFILE = REPO / "Makefile"
WORKFLOW = REPO / ".github" / "workflows" / "pr.yml"

_MODULE_COUNTER = itertools.count()


def _load_module():
    """Import scripts/ci_shard.py fresh, under a unique module name per call.

    The unique name (rather than a fixed one reused across tests) keeps tests
    independent per TEST-ISOLATE-001: no test can observe monkeypatching another
    test applied to a shared `sys.modules` entry.
    """
    spec = importlib.util.spec_from_file_location(
        f"_ci_shard_under_test_{next(_MODULE_COUNTER)}", SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_fake_tree(tmp_path: Path) -> Path:
    """A small tree exercising every discover() boundary in one shot:
    depth, both naming patterns, and every default norecursedirs glob."""
    t = tmp_path / "tests"
    (t / "sub" / "deep").mkdir(parents=True)
    (t / ".hidden").mkdir()
    (t / "build").mkdir()
    (t / "venv").mkdir()
    (t / "x.egg").mkdir()
    (t / "test_a.py").write_text("")
    (t / "sub" / "test_b.py").write_text("")
    (t / "sub" / "deep" / "test_c.py").write_text("")
    (t / "sub" / "widget_test.py").write_text("")
    (t / "conftest.py").write_text("")
    (t / "_helpers.py").write_text("")
    (t / "foo.py").write_text("")
    (t / ".hidden" / "test_hidden.py").write_text("")
    (t / "build" / "test_build.py").write_text("")
    (t / "venv" / "test_venv.py").write_text("")
    (t / "x.egg" / "test_egg.py").write_text("")
    return tmp_path


def _write_json(path: Path, data) -> Path:
    path.write_text(json.dumps(data) + "\n")
    return path


@pytest.fixture
def fake_tree(tmp_path):
    return _make_fake_tree(tmp_path)


# ---------------------------------------------------------------------------
# Capability: discover() on a fake tree returns every test_*.py and *_test.py
# under tests/ at any depth, sorted, in pytest's joint alphabetical order, and
# excludes non-matching files and default norecursedirs directories.
# ---------------------------------------------------------------------------
class TestDiscoverOnAFakeTree:
    def test_returns_exactly_the_matching_files_in_collection_order(self, fake_tree):
        mod = _load_module()
        result = mod.discover(fake_tree)
        assert result == [
            "tests/sub/deep/test_c.py",
            "tests/sub/test_b.py",
            "tests/sub/widget_test.py",
            "tests/test_a.py",
        ]

    def test_excludes_non_matching_filenames(self, fake_tree):
        mod = _load_module()
        result = mod.discover(fake_tree)
        assert not any(
            name in result
            for name in (
                "tests/conftest.py",
                "tests/_helpers.py",
                "tests/foo.py",
            )
        )

    def test_excludes_default_norecursedirs(self, fake_tree):
        mod = _load_module()
        result = mod.discover(fake_tree)
        assert not any(
            "hidden" in p or "build" in p or "venv" in p or "egg" in p
            for p in result
        )

    def test_paths_are_posix_strings_relative_to_root(self, fake_tree):
        mod = _load_module()
        result = mod.discover(fake_tree)
        assert all(isinstance(p, str) and "\\" not in p for p in result)
        assert all(p.startswith("tests/") for p in result)


# ---------------------------------------------------------------------------
# Capability: assign() places every file in exactly one shard -- union equals
# discover(), shards are pairwise disjoint, for shard counts 1 through 6, on
# the fake tree and on the real repo tree.
# ---------------------------------------------------------------------------
class TestAssignIsACompletePartition:
    @pytest.mark.parametrize("shards", [1, 2, 3, 4, 5, 6])
    def test_fake_tree_union_and_disjointness(self, fake_tree, shards):
        mod = _load_module()
        files = mod.discover(fake_tree)
        result = mod.assign(files, {}, shards)
        assert len(result) == shards
        seen: set[str] = set()
        for shard in result:
            for f in shard:
                assert f not in seen, f"{f} assigned to more than one shard"
                seen.add(f)
        assert seen == set(files)

    @pytest.mark.parametrize("shards", [1, 2, 3, 4, 5, 6])
    def test_real_tree_union_and_disjointness(self, shards):
        mod = _load_module()
        files = mod.discover(REPO)
        result = mod.assign(files, {}, shards)
        assert len(result) == shards
        seen: set[str] = set()
        for shard in result:
            for f in shard:
                assert f not in seen, f"{f} assigned to more than one shard"
                seen.add(f)
        assert seen == set(files)


# ---------------------------------------------------------------------------
# Capability: assign() is deterministic -- repeated calls, and calls with the
# timings dict built in a different insertion order, return identical lists.
# ---------------------------------------------------------------------------
class TestAssignIsDeterministic:
    def test_repeated_calls_are_identical(self):
        mod = _load_module()
        files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]
        timings = {"tests/test_a.py": 5.0, "tests/test_b.py": 3.0}
        first = mod.assign(list(files), dict(timings), 2)
        second = mod.assign(list(files), dict(timings), 2)
        assert first == second

    def test_timings_insertion_order_does_not_affect_the_result(self):
        mod = _load_module()
        files = [
            "tests/test_a.py",
            "tests/test_b.py",
            "tests/test_c.py",
            "tests/test_d.py",
        ]
        forward = {
            "tests/test_a.py": 5.0,
            "tests/test_b.py": 3.0,
            "tests/test_c.py": 1.0,
        }
        reversed_dict = dict(reversed(list(forward.items())))
        result_forward = mod.assign(list(files), forward, 2)
        result_reversed = mod.assign(list(files), reversed_dict, 2)
        assert result_forward == result_reversed


# ---------------------------------------------------------------------------
# Capability: greedy balancing on a known weight set produces the expected
# assignment, and on the real tree with the committed timings the heaviest
# and lightest shard estimates differ by no more than the largest single
# file weight.
# ---------------------------------------------------------------------------
class TestGreedyBalancing:
    def test_known_weight_set_produces_the_expected_split(self):
        mod = _load_module()
        files = [f"tests/test_{c}.py" for c in "abcdef"]
        timings = dict(zip(files, [10.0, 9.0, 8.0, 3.0, 2.0, 1.0]))
        result = mod.assign(files, timings, 2)
        assert result == [
            ["tests/test_a.py", "tests/test_d.py", "tests/test_e.py", "tests/test_f.py"],
            ["tests/test_b.py", "tests/test_c.py"],
        ]

    def test_real_tree_shards_are_balanced_within_the_heaviest_file(self):
        mod = _load_module()
        files = mod.discover(REPO)
        timings = mod.load_timings(TIMINGS)
        result = mod.assign(files, timings, 4)

        known = [timings[f] for f in files if f in timings]
        default = sorted(known)[len(known) // 2] if known else 1.0

        def weight(f: str) -> float:
            return timings.get(f, default)

        loads = [sum(weight(f) for f in shard) for shard in result]
        largest_single = max(weight(f) for f in files)
        assert max(loads) - min(loads) <= largest_single


# ---------------------------------------------------------------------------
# Capability: a file absent from the timings file gets the default weight
# (median of known weights for files that exist, 1.0 when none are known),
# and a timings entry for a file that no longer exists is ignored without
# error.
# ---------------------------------------------------------------------------
class TestDefaultWeightAndStaleTimings:
    def test_unknown_file_is_weighted_as_the_median_of_known_files(self):
        mod = _load_module()
        # median(10, 9, 1) == 9; "z" (unknown) should behave as weight 9, which
        # this greedy trace makes observable: a(10)->shard0, b(9)->shard1,
        # z(9, tie broken after b by path)->shard1 (load 9 < 10), c(1)->shard0.
        files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_z.py"]
        timings = {
            "tests/test_a.py": 10.0,
            "tests/test_b.py": 9.0,
            "tests/test_c.py": 1.0,
        }
        result = mod.assign(files, timings, 2)
        assert result == [
            ["tests/test_a.py", "tests/test_c.py"],
            ["tests/test_b.py", "tests/test_z.py"],
        ]

    def test_default_weight_is_one_when_no_timings_are_known(self):
        mod = _load_module()
        files = ["tests/test_a.py", "tests/test_b.py"]
        result = mod.assign(files, {}, 2)
        assert sorted(len(shard) for shard in result) == [1, 1]

    def test_stale_timings_entry_is_ignored_without_error(self):
        mod = _load_module()
        files = ["tests/test_a.py", "tests/test_b.py"]
        timings = {
            "tests/test_a.py": 5.0,
            "tests/test_b.py": 5.0,
            "tests/test_removed.py": 999.0,
        }
        result = mod.assign(files, timings, 2)
        seen = {f for shard in result for f in shard}
        assert seen == set(files)
        assert "tests/test_removed.py" not in seen


# ---------------------------------------------------------------------------
# Capability: each shard's list is in collection order, so relative file
# order within a shard matches the full-suite order.
# ---------------------------------------------------------------------------
class TestShardListsAreInCollectionOrder:
    def test_single_shard_is_fully_sorted(self):
        mod = _load_module()
        files = ["tests/test_c.py", "tests/test_a.py", "tests/test_b.py"]
        result = mod.assign(list(files), {}, 1)
        assert result == [sorted(files)]

    def test_every_shard_of_a_multi_shard_split_is_individually_sorted(self):
        mod = _load_module()
        files = [f"tests/test_{c}.py" for c in "fedcba"]
        timings = dict(zip(files, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]))
        result = mod.assign(files, timings, 2)
        for shard in result:
            assert shard == sorted(shard)


# ---------------------------------------------------------------------------
# Capability: `--shard K --shards N` prints only shard K's files, one per
# line, on stdout, and a summary line on stderr; bad inputs exit non-zero
# and print no file list.
# ---------------------------------------------------------------------------
class TestCLIShardPrinting:
    def _run(self, *args: str):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, cwd=str(REPO), timeout=30,
        )

    def test_prints_only_shard_files_on_stdout_and_a_summary_on_stderr(self, tmp_path):
        root = _make_fake_tree(tmp_path)
        timings = _write_json(tmp_path / "timings.json", {})
        proc = self._run(
            "--shard", "0", "--shards", "2", "--root", str(root), "--timings", str(timings),
        )
        assert proc.returncode == 0, proc.stderr
        lines = [ln for ln in proc.stdout.splitlines() if ln]
        assert lines, "expected at least one file path on stdout"
        assert all(ln.startswith("tests/") for ln in lines)
        assert "shard 0/2" in proc.stderr

    # Every rejection test below asserts, in addition to the non-zero exit
    # and empty stdout the plan requires, that the failure message is the
    # script's OWN validation message and not Python's "can't open file"
    # launch error. Without that second assertion these tests would pass
    # vacuously today (no script on disk also exits non-zero with empty
    # stdout), which would hide the difference between "not implemented yet"
    # and "implemented but the validation is missing".

    def test_shard_equal_to_shards_is_rejected(self, tmp_path):
        root = _make_fake_tree(tmp_path)
        timings = _write_json(tmp_path / "timings.json", {})
        proc = self._run(
            "--shard", "2", "--shards", "2", "--root", str(root), "--timings", str(timings),
        )
        assert proc.returncode != 0
        assert proc.stdout == ""
        assert "can't open file" not in proc.stderr
        assert "shard" in proc.stderr.lower()

    def test_shards_below_one_is_rejected(self, tmp_path):
        root = _make_fake_tree(tmp_path)
        timings = _write_json(tmp_path / "timings.json", {})
        proc = self._run(
            "--shard", "0", "--shards", "0", "--root", str(root), "--timings", str(timings),
        )
        assert proc.returncode != 0
        assert proc.stdout == ""
        assert "can't open file" not in proc.stderr
        assert "shard" in proc.stderr.lower()

    def test_empty_tree_is_rejected(self, tmp_path):
        (tmp_path / "tests").mkdir()
        timings = _write_json(tmp_path / "timings.json", {})
        proc = self._run(
            "--shard", "0", "--shards", "1", "--root", str(tmp_path), "--timings", str(timings),
        )
        assert proc.returncode != 0
        assert proc.stdout == ""
        assert "can't open file" not in proc.stderr
        assert proc.stderr.strip(), "an empty tree must still print a refusal message"

    def test_path_containing_whitespace_is_rejected(self, tmp_path):
        root = tmp_path
        weird = root / "tests" / "weird dir"
        weird.mkdir(parents=True)
        (weird / "test_a.py").write_text("")
        timings = _write_json(tmp_path / "timings.json", {})
        proc = self._run(
            "--shard", "0", "--shards", "1", "--root", str(root), "--timings", str(timings),
        )
        assert proc.returncode != 0
        assert proc.stdout == ""
        assert "can't open file" not in proc.stderr
        assert "whitespace" in proc.stderr.lower() or "space" in proc.stderr.lower()


# ---------------------------------------------------------------------------
# Capability: `--check --shards N` exits 0 with per-shard counts on a valid
# partition, and exits 1 naming the missing/duplicated paths when the
# partition is broken (exercised by monkeypatching the assignment).
# ---------------------------------------------------------------------------
class TestCLICheck:
    def _invoke(self, mod, argv, capsys):
        try:
            code = mod.main(argv)
        except SystemExit as exc:
            code = exc.code
        out = capsys.readouterr()
        return (0 if code is None else code), out.out, out.err

    def test_valid_partition_exits_zero_with_per_shard_counts(self, fake_tree, capsys):
        mod = _load_module()
        timings = _write_json(fake_tree / "timings.json", {})
        code, out, err = self._invoke(
            mod,
            ["--check", "--shards", "2", "--root", str(fake_tree), "--timings", str(timings)],
            capsys,
        )
        assert code == 0
        assert re.search(r"\d+ files?", out + err)

    def test_broken_partition_exits_one_and_names_the_gap(self, fake_tree, capsys, monkeypatch):
        mod = _load_module()
        timings = _write_json(fake_tree / "timings.json", {})
        files = mod.discover(fake_tree)
        dropped, duplicated = files[0], files[1]

        def _broken_assign(files, timings, shards):
            trimmed = [f for f in files if f != dropped]
            return [trimmed + [duplicated]]

        monkeypatch.setattr(mod, "assign", _broken_assign)
        code, out, err = self._invoke(
            mod,
            ["--check", "--shards", "1", "--root", str(fake_tree), "--timings", str(timings)],
            capsys,
        )
        assert code == 1
        assert dropped in (out + err)
        assert duplicated in (out + err)


# ---------------------------------------------------------------------------
# Capability: `--rebuild-timings` on synthetic xunit1 junit XML sums testcase
# times per `file`, maps a testcase without a `file` attribute through its
# `classname` to the discovered file, merges several XML files, and writes
# sorted JSON at 0.1s precision.
# ---------------------------------------------------------------------------
class TestRebuildTimings:
    def test_sums_time_per_file_and_merges_multiple_xml_files(self, fake_tree):
        mod = _load_module()
        xml1 = fake_tree / "a.xml"
        xml1.write_text(
            "<testsuite>"
            '<testcase classname="x" name="t1" time="1.05" file="tests/test_a.py"/>'
            '<testcase classname="x" name="t2" time="2.15" file="tests/test_a.py"/>'
            "</testsuite>"
        )
        xml2 = fake_tree / "b.xml"
        xml2.write_text(
            "<testsuite>"
            '<testcase classname="x" name="t3" time="0.80" file="tests/sub/test_b.py"/>'
            "</testsuite>"
        )
        result = mod.rebuild_timings([xml1, xml2], fake_tree)
        assert result["tests/test_a.py"] == pytest.approx(3.2, abs=0.05)
        assert result["tests/sub/test_b.py"] == pytest.approx(0.8, abs=0.05)

    def test_classname_without_file_attribute_maps_to_the_discovered_file(self, fake_tree):
        mod = _load_module()
        xml = fake_tree / "c.xml"
        xml.write_text(
            "<testsuite>"
            '<testcase classname="tests.sub.deep.test_c.TestSomething" '
            'name="test_it" time="4.4"/>'
            "</testsuite>"
        )
        result = mod.rebuild_timings([xml], fake_tree)
        assert result.get("tests/sub/deep/test_c.py") == pytest.approx(4.4, abs=0.05)

    def test_output_is_rounded_to_one_decimal_place(self, fake_tree):
        mod = _load_module()
        xml = fake_tree / "d.xml"
        xml.write_text(
            "<testsuite>"
            '<testcase classname="x" name="t" time="1.2345" file="tests/test_a.py"/>'
            "</testsuite>"
        )
        result = mod.rebuild_timings([xml], fake_tree)
        assert result["tests/test_a.py"] == 1.2

    def test_cli_writes_sorted_json_with_trailing_newline(self, fake_tree):
        xml = fake_tree / "run.xml"
        xml.write_text(
            "<testsuite>"
            '<testcase classname="x" name="t1" time="2.0" file="tests/test_a.py"/>'
            '<testcase classname="x" name="t2" time="1.0" file="tests/sub/test_b.py"/>'
            "</testsuite>"
        )
        out_path = fake_tree / "out_timings.json"
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--root", str(fake_tree),
                "--timings", str(out_path),
                "--rebuild-timings", str(xml),
            ],
            capture_output=True, text=True, cwd=str(REPO), timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        text = out_path.read_text()
        assert text.endswith("\n")
        data = json.loads(text)
        assert list(data.keys()) == sorted(data.keys())
        assert data["tests/test_a.py"] == 2.0
        assert data["tests/sub/test_b.py"] == 1.0


# ---------------------------------------------------------------------------
# Capability: a tripwire test fails if pyproject's [tool.pytest.ini_options]
# gains testpaths, python_files, norecursedirs or collect_ignore, naming
# scripts/ci_shard.py as the discovery that must follow it.
# ---------------------------------------------------------------------------
class TestPyprojectDiscoveryTripwire:
    def test_pyproject_has_not_gained_a_collection_option_discover_must_learn(self):
        with PYPROJECT.open("rb") as f:
            data = tomllib.load(f)
        ini = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        forbidden = {"testpaths", "python_files", "norecursedirs", "collect_ignore"}
        present = forbidden & ini.keys()
        assert not present, (
            f"pyproject [tool.pytest.ini_options] now sets {sorted(present)}; "
            "scripts/ci_shard.py's discover() reimplements pytest's default "
            "collection rules and must be taught to honour this option too"
        )


# ---------------------------------------------------------------------------
# Capability: scripts/ci_timings.json parses as a JSON object of path to
# non-negative number, and every key is a path discover() returns or is
# ignored as stale.
# ---------------------------------------------------------------------------
class TestTimingsFileShape:
    def test_timings_file_is_a_flat_object_of_path_to_non_negative_number(self):
        data = json.loads(TIMINGS.read_text())
        assert isinstance(data, dict)
        assert data, "scripts/ci_timings.json must not be empty"
        for key, value in data.items():
            assert isinstance(key, str)
            assert isinstance(value, (int, float)) and not isinstance(value, bool)
            assert value >= 0

    def test_timings_feed_assign_against_the_real_tree_without_error(self):
        mod = _load_module()
        files = mod.discover(REPO)
        timings = mod.load_timings(TIMINGS)
        result = mod.assign(files, timings, 4)
        assert sum(len(shard) for shard in result) == len(files)


# ---------------------------------------------------------------------------
# Capability: `make -n test` with no variables prints today's pytest line
# exactly, and `make -n test SHARD=1 SHARDS=4` prints a recipe that invokes
# scripts/ci_shard.py joined to pytest with &&.
# ---------------------------------------------------------------------------
class TestMakefileWiring:
    def _make_dash_n(self, *args: str):
        return subprocess.run(
            ["make", "-n", "test", *args],
            capture_output=True, text=True, cwd=str(REPO), timeout=30,
        )

    def test_bare_make_test_is_unchanged(self):
        proc = self._make_dash_n()
        assert proc.returncode == 0, proc.stderr
        assert ".venv/bin/python3 -m pytest tests/ --maxfail=10 -q" in proc.stdout
        assert "bash scripts/test-graph.sh up" in proc.stdout

    def test_sharded_make_test_invokes_ci_shard_then_pytest(self):
        proc = self._make_dash_n("SHARD=1", "SHARDS=4")
        assert proc.returncode == 0, proc.stderr
        assert "scripts/ci_shard.py --shard 1 --shards 4" in proc.stdout
        assert "&&" in proc.stdout
        assert "pytest" in proc.stdout


# ---------------------------------------------------------------------------
# Capability: .github/workflows/pr.yml has a test-shard job with
# fail-fast: false and a matrix whose length equals TEST_SHARDS, both witness
# steps, the junit upload, and a job named `test` that needs test-shard, runs
# ci_shard.py --check, and uses if: always().
# ---------------------------------------------------------------------------
class TestWorkflowWiring:
    def test_test_shard_job_has_a_four_way_fail_fast_false_matrix(self):
        text = WORKFLOW.read_text()
        assert re.search(r"^\s*test-shard:", text, re.MULTILINE), (
            "no test-shard job in .github/workflows/pr.yml"
        )
        assert "fail-fast: false" in text
        assert re.search(r"shard:\s*\[\s*0,\s*1,\s*2,\s*3\s*\]", text)
        assert 'TEST_SHARDS: "4"' in text

    def test_test_shard_job_carries_both_witness_steps_and_the_junit_upload(self):
        text = WORKFLOW.read_text()
        assert "Seed the production-address witness commit" in text
        assert "Verify the witness survived the suite" in text
        assert "actions/upload-artifact@v4" in text
        assert "junit-shard-" in text

    def test_test_job_is_the_aggregator_that_needs_test_shard(self):
        text = WORKFLOW.read_text()
        job_match = re.search(
            r"^  test:\n(.*?)(?=^  \S+:\n|\Z)", text, re.MULTILINE | re.DOTALL
        )
        assert job_match, "no aggregator `test:` job found"
        body = job_match.group(1)
        assert "test-shard" in body, "aggregator must declare needs: [test-shard]"
        assert "if: always()" in body
        assert "ci_shard.py --check" in body
