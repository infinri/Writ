"""Program item 5 (workstream D), phase 2: `writ docs ingest`, discovery and the CLI, against a fake db.

writ/documents/ingest.py is a WRITER (not a retrieval collector): it walks a repo, hashes each
eligible markdown file, skips the unchanged ones, replaces the changed or new ones through
the record store (replace_document, one atomic statement per document), deletes the documents
of vanished files under the kinds this run ingested, and returns an IngestReport. The CLI
(writ docs ingest) resolves the project, applies constraints, prints one summary line and asks
the daemon to reload only when something was written or deleted.

Everything here is mocked at the db boundary on purpose: the claims are about WHICH files are
read, split and handed to the store, in what order and with which arguments. Atomic replace,
orphan removal, concurrency and dump exclusion are claims about the graph and are proven in
tests/test_document_ingest_graph.py (ENF-SYS-005). tmp_path holds the fixture repos.

Capabilities: (D) 9, 10 and 11.
"""
from __future__ import annotations

import builtins
import hashlib
import importlib
import os
import pathlib
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

import writ.cli as cli
from writ.cli import app

DEFAULT_KINDS = frozenset({"docs", "adr", "readme"})
ALL_KINDS = frozenset({"docs", "adr", "readme", "claude_md", "memory"})
MAX_BYTES = 1024 * 1024
PROJECT = "proj"
runner = CliRunner()


def _ingest_mod():
    return importlib.import_module("writ.documents.ingest")


class FakeDb:
    """Records every store call in order; hashes seeds what the graph already holds.

    The registry holds one project whose repo_root is `root`; without one, the first cwd
    resolved is taken as the registered root. Resolution is by repo_root prefix, as in
    ProjectStoreMixin.resolve_project_for_cwd. get_projects is a registry read and is not
    recorded in `calls`."""

    def __init__(self, project: str = PROJECT, hashes: dict | None = None, root: str | None = None) -> None:
        self.project = project
        self.root = root
        self.hashes = dict(hashes or {})
        self.calls: list[tuple] = []
        self.replaced: list[tuple] = []
        self.deleted: list[tuple] = []

    async def resolve_project_for_cwd(self, cwd: str) -> str:
        self.calls.append(("resolve", cwd))
        if self.root is None:
            self.root = cwd
        return self.project if cwd == self.root or cwd.startswith(self.root.rstrip("/") + "/") else ""

    async def get_projects(self) -> list[dict]:
        return [{"name": self.project, "repo_root": self.root, "bible_root": None, "remote_url": None}]

    async def apply_constraints(self) -> None:
        self.calls.append(("constraints",))

    async def get_document_hashes(self, project: str) -> dict:
        self.calls.append(("hashes", project))
        return dict(self.hashes)

    async def replace_document(self, project: str, document, chunks) -> int:
        self.calls.append(("replace", document.doc_id))
        self.replaced.append((project, document, list(chunks)))
        self.hashes[document.doc_id] = {"source_hash": document.source_hash, "kind": document.kind}
        return len(chunks)

    async def delete_documents(self, project: str, doc_ids) -> int:
        self.calls.append(("delete", tuple(doc_ids)))
        self.deleted.append((project, list(doc_ids)))
        for doc_id in doc_ids:
            self.hashes.pop(doc_id, None)
        return len(doc_ids)

    def names(self, op: str) -> list[str]:
        return [c[1] for c in self.calls if c[0] == op]

    def kinds_written(self) -> dict[str, str]:
        return {doc.doc_id: doc.kind for _, doc, _ in self.replaced}


def _put(root: Path, rel: str, content: str | bytes = "# Title\n\nsome body text for the file\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for rel in ("README.md", "docs/guide.md", "docs/adr/ADR-001-first.md", "docs/adr/0002-second.md",
                "docs/ADR-004-flat.md", "adr/ADR-003-outside.md", "sub/README.md"):
        _put(root, rel, f"# {rel}\n\nbody of {rel}\n")
    return root


async def _run(db, repo_root, kinds=DEFAULT_KINDS, memory_files=()):
    return await _ingest_mod().ingest_documents(db, PROJECT, repo_root, kinds, list(memory_files))


class TestDiscoveryAndKinds:
    @pytest.mark.asyncio
    async def test_default_kinds_find_docs_adrs_inside_and_outside_docs_and_readmes(self, repo):
        db = FakeDb()
        await _run(db, repo)
        assert db.kinds_written() == {
            "README.md": "readme", "sub/README.md": "readme",
            "docs/guide.md": "docs",
            "docs/adr/ADR-001-first.md": "adr", "docs/adr/0002-second.md": "adr",
            "docs/ADR-004-flat.md": "adr", "adr/ADR-003-outside.md": "adr",
        }

    @pytest.mark.asyncio
    async def test_a_markdown_file_outside_docs_adr_and_readme_is_not_ingested(self, repo):
        _put(repo, "notes/random.md")
        _put(repo, "docs/image.png", b"\x89PNG")
        db = FakeDb()
        await _run(db, repo)
        assert "notes/random.md" not in db.kinds_written()
        assert "docs/image.png" not in db.kinds_written()

    @pytest.mark.asyncio
    async def test_claude_md_is_ingested_only_when_its_kind_is_requested(self, repo):
        _put(repo, "CLAUDE.md"); _put(repo, "sub/CLAUDE.md")
        without = FakeDb()
        await _run(without, repo)
        assert not any(k == "claude_md" for k in without.kinds_written().values())
        with_it = FakeDb()
        await _run(with_it, repo, DEFAULT_KINDS | {"claude_md"})
        assert {d for d, k in with_it.kinds_written().items() if k == "claude_md"} == {"CLAUDE.md", "sub/CLAUDE.md"}

    @pytest.mark.asyncio
    async def test_memory_files_are_ingested_as_memory_documents_with_absolute_paths(self, repo, tmp_path):
        mem = tmp_path / "memdir"
        note = _put(mem, "feedback_note.md", "---\nname: n\n---\nremember this fact about the project\n")
        db = FakeDb()
        await _run(db, repo, ALL_KINDS, [note])
        doc = next(d for _, d, _ in db.replaced if d.kind == "memory")
        assert doc.doc_id.startswith("memory/") and doc.doc_id.endswith("/" + note.name)
        assert doc.path.endswith("/memdir/" + note.name)
        assert db.kinds_written()[doc.doc_id] == "memory"

    @pytest.mark.asyncio
    async def test_memory_files_are_ignored_when_the_memory_kind_is_not_requested(self, repo, tmp_path):
        note = _put(tmp_path / "memdir", "note.md")
        db = FakeDb()
        await _run(db, repo, DEFAULT_KINDS, [note])
        assert not any(d.startswith("memory/") for d in db.kinds_written())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pruned", [
        ".hidden/docs/h.md", "node_modules/pkg/README.md", ".venv/lib/docs/v.md", "venv/docs/v.md",
        "vendor/lib/README.md", "dist/docs/d.md", "build/docs/b.md", "__pycache__/docs/p.md",
        "lib/site-packages/pkg/README.md", ".git/docs/g.md",
    ])
    async def test_hidden_and_dependency_directories_are_pruned(self, repo, pruned):
        _put(repo, pruned)
        db = FakeDb()
        await _run(db, repo)
        assert pruned not in db.kinds_written()

    @pytest.mark.asyncio
    async def test_a_symlink_escaping_the_repo_is_skipped_and_counted_never_read(self, repo, tmp_path, monkeypatch):
        outside = _put(tmp_path / "elsewhere", "outside.md", "# Outside\n\nOUTSIDE-MARKER content\n")
        (repo / "docs" / "escape.md").symlink_to(outside)
        read: list[str] = []
        real = pathlib.Path.read_bytes
        monkeypatch.setattr(pathlib.Path, "read_bytes", lambda self: (read.append(str(self)), real(self))[1])
        db = FakeDb()
        report = await _run(db, repo)
        assert report.skipped_outside == 1
        assert "docs/escape.md" not in db.kinds_written()
        assert not any(p == str(outside) or p.endswith("/elsewhere/outside.md")
                       or p.endswith("/docs/escape.md") for p in read)

    @pytest.mark.asyncio
    async def test_a_symlink_that_stays_inside_the_repo_is_allowed(self, repo):
        (repo / "docs" / "alias.md").symlink_to(repo / "docs" / "guide.md")
        db = FakeDb()
        report = await _run(db, repo)
        assert report.skipped_outside == 0

    @pytest.mark.asyncio
    async def test_files_over_one_mebibyte_are_skipped_and_counted(self, repo):
        _put(repo, "docs/big.md", "# Big\n\n" + "x" * (MAX_BYTES + 1))
        _put(repo, "docs/exact.md", "# Exact\n\n" + "y" * (MAX_BYTES - len("# Exact\n\n")))
        db = FakeDb()
        report = await _run(db, repo)
        assert report.skipped_large == 1
        assert "docs/big.md" not in db.kinds_written()
        assert "docs/exact.md" in db.kinds_written(), "a file of exactly 1 MiB is within the limit"

    @pytest.mark.asyncio
    async def test_credential_named_and_secrets_directory_files_are_never_read(self, repo, monkeypatch):
        marker = "NEVER-READ-MARKER-7d41"
        _put(repo, "docs/" + "credentials" + ".md", f"# C\n\n{marker}\n")
        _put(repo, "docs/" + "credentials-prod" + ".md", f"# C\n\n{marker}\n")
        _put(repo, "docs/" + "secrets" + "/keys.md", f"# K\n\n{marker}\n")
        _put(repo, "secrets/README.md", f"# K\n\n{marker}\n")
        opened: list[str] = []
        real_open, real_read = builtins.open, pathlib.Path.read_bytes
        monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
        monkeypatch.setattr(pathlib.Path, "read_bytes", lambda self: (opened.append(str(self)), real_read(self))[1])
        db = FakeDb()
        await _run(db, repo)
        assert not [p for p in opened if "credentials" in p or "secrets" in p]
        assert all(marker not in chunk.text for _, _, chunks in db.replaced for chunk in chunks)
        written = db.kinds_written()
        assert not [d for d in written if "credentials" in d or "secrets" in d]

    @pytest.mark.asyncio
    async def test_invalid_utf8_is_decoded_with_replacement_not_skipped(self, repo):
        _put(repo, "docs/latin.md", b"# Latin\n\ncaf\xe9 au lait\n")
        db = FakeDb()
        await _run(db, repo)
        chunks = next(c for _, d, c in db.replaced if d.doc_id == "docs/latin.md")
        assert "�" in chunks[0].text


class TestWrittenRecords:
    @pytest.mark.asyncio
    async def test_the_document_and_chunks_carry_the_planned_fields(self, repo):
        content = "# Heading\n\nfirst paragraph\n\n## Sub\n\nsecond paragraph\n"
        raw = content.encode("utf-8")
        _put(repo, "docs/shape.md", content)
        db = FakeDb()
        await _run(db, repo)
        project, doc, chunks = next(r for r in db.replaced if r[1].doc_id == "docs/shape.md")
        assert project == PROJECT
        assert (doc.project, doc.path, doc.kind, doc.title) == (PROJECT, "docs/shape.md", "docs", "Heading")
        assert doc.source_hash == hashlib.sha256(raw).hexdigest()
        assert doc.chunk_count == len(chunks) == 2
        assert (doc.provenance, doc.source_origin) == ("record", "graph-authored")
        assert [c.ordinal for c in chunks] == [0, 1]
        assert [c.chunk_id for c in chunks] == [f"{PROJECT}:docs/shape.md#0000", f"{PROJECT}:docs/shape.md#0001"]
        assert all(c.doc_id == "docs/shape.md" and c.project == PROJECT and c.source_hash == doc.source_hash
                   for c in chunks)
        assert [c.breadcrumb for c in chunks] == ["Heading", "Heading > Sub"]
        assert chunks[0].text == "first paragraph"

    @pytest.mark.asyncio
    async def test_the_file_stem_is_the_title_when_the_file_has_no_heading(self, repo):
        _put(repo, "docs/plain-notes.md", "just some prose without any heading\n")
        db = FakeDb()
        await _run(db, repo)
        doc = next(d for _, d, _ in db.replaced if d.doc_id == "docs/plain-notes.md")
        assert doc.title == "plain-notes"

    @pytest.mark.asyncio
    async def test_a_document_with_no_body_still_writes_with_zero_chunks(self, repo):
        _put(repo, "docs/only-headings.md", "# A\n\n## B\n")
        db = FakeDb()
        report = await _run(db, repo)
        _, doc, chunks = next(r for r in db.replaced if r[1].doc_id == "docs/only-headings.md")
        assert chunks == [] and doc.chunk_count == 0
        assert report.written == len(db.replaced)

    @pytest.mark.asyncio
    async def test_the_report_counts_add_up(self, repo):
        _put(repo, "docs/big.md", "x" * (MAX_BYTES + 1))
        db = FakeDb()
        report = await _run(db, repo)
        assert report.scanned == report.unchanged + report.written
        assert report.written == len(db.replaced) and report.unchanged == 0
        assert report.chunks == sum(len(c) for _, _, c in db.replaced)
        assert report.deleted == 0 and report.skipped_large == 1 and report.skipped_outside == 0


class TestHashSkipAndReplace:
    @pytest.mark.asyncio
    async def test_an_unchanged_file_is_neither_split_nor_written(self, repo, monkeypatch):
        db = FakeDb()
        first = await _run(db, repo)
        assert first.written > 0
        db.calls.clear(); db.replaced.clear()

        def boom(*a, **k):
            raise AssertionError("an unchanged file must not be split")

        monkeypatch.setattr(importlib.import_module("writ.documents.splitter"), "split_markdown", boom)
        monkeypatch.setattr(_ingest_mod(), "split_markdown", boom, raising=False)
        second = await _run(db, repo)
        assert (second.written, second.chunks, second.deleted) == (0, 0, 0)
        assert second.unchanged == second.scanned == first.scanned
        assert db.replaced == [] and db.names("replace") == []

    @pytest.mark.asyncio
    async def test_only_the_changed_and_the_new_file_are_replaced(self, repo):
        db = FakeDb()
        await _run(db, repo)
        db.calls.clear(); db.replaced.clear()
        _put(repo, "docs/guide.md", "# docs/guide.md\n\nEDITED body\n")
        _put(repo, "docs/brand-new.md", "# New\n\nfresh content\n")
        report = await _run(db, repo)
        assert sorted(db.names("replace")) == ["docs/brand-new.md", "docs/guide.md"]
        assert report.written == 2 and report.unchanged == report.scanned - 2

    @pytest.mark.asyncio
    async def test_the_hash_read_is_one_call_for_the_project_before_any_write(self, repo):
        db = FakeDb()
        await _run(db, repo)
        ops = [c[0] for c in db.calls]
        assert ops.count("hashes") == 1 and db.calls[0] == ("hashes", PROJECT)
        assert ops.index("hashes") < ops.index("replace")


class TestDeletionScope:
    @pytest.mark.asyncio
    async def test_a_document_of_an_ingested_kind_whose_file_is_gone_is_deleted_in_one_call(self, repo):
        db = FakeDb()
        await _run(db, repo)
        (repo / "docs" / "guide.md").unlink()
        (repo / "sub" / "README.md").unlink()
        report = await _run(db, repo)
        assert len(db.deleted) == 1
        assert db.deleted[0][0] == PROJECT and sorted(db.deleted[0][1]) == ["docs/guide.md", "sub/README.md"]
        assert report.deleted == 2

    @pytest.mark.asyncio
    async def test_a_document_of_a_kind_not_ingested_this_run_is_kept(self, repo):
        db = FakeDb(hashes={"CLAUDE.md": {"source_hash": "h", "kind": "claude_md"},
                            "memory/old.md": {"source_hash": "h", "kind": "memory"}})
        report = await _run(db, repo, DEFAULT_KINDS)
        assert db.deleted == [] and report.deleted == 0
        assert {"CLAUDE.md", "memory/old.md"} <= set(db.hashes)

    @pytest.mark.asyncio
    async def test_claude_md_documents_are_deleted_once_the_kind_is_ingested_and_the_file_is_gone(self, repo):
        db = FakeDb(hashes={"CLAUDE.md": {"source_hash": "h", "kind": "claude_md"}})
        report = await _run(db, repo, DEFAULT_KINDS | {"claude_md"})
        assert db.deleted == [(PROJECT, ["CLAUDE.md"])] and report.deleted == 1

    @pytest.mark.asyncio
    async def test_a_different_memory_dir_deletes_only_the_memory_documents_it_did_not_see(self, repo, tmp_path):
        db = FakeDb(hashes={"memory/old.md": {"source_hash": "h", "kind": "memory"},
                            "docs/kept-doc.md": {"source_hash": "h", "kind": "docs"}})
        _put(repo, "docs/kept-doc.md")
        db.hashes["docs/kept-doc.md"]["source_hash"] = hashlib.sha256(
            (repo / "docs" / "kept-doc.md").read_bytes()).hexdigest()
        note = _put(tmp_path / "other-mem", "new_note.md", "a new memory body\n")
        await _run(db, repo, ALL_KINDS, [note])
        assert db.deleted == [(PROJECT, ["memory/old.md"])]

    @pytest.mark.asyncio
    async def test_nothing_gone_means_no_delete_with_ids(self, repo):
        db = FakeDb()
        await _run(db, repo)
        await _run(db, repo)
        assert all(ids for _, ids in db.deleted) and db.deleted == []


def _fake_writ_db(db: FakeDb):
    @asynccontextmanager
    async def _ctx():
        yield db
    return _ctx


class TestCli:
    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch):
        self.notified: list[int] = []
        monkeypatch.setattr(cli, "_notify_daemon_reload", lambda: self.notified.append(1))

    def _invoke(self, monkeypatch, db: FakeDb, *args: str):
        monkeypatch.setattr(cli, "_writ_db", _fake_writ_db(db))
        return runner.invoke(app, ["docs", "ingest", *args])

    def test_the_project_is_resolved_from_the_repo_option(self, monkeypatch, repo):
        db = FakeDb()
        result = self._invoke(monkeypatch, db, "--repo", str(repo))
        assert result.exit_code == 0, result.output
        assert db.calls[0] == ("resolve", os.path.abspath(str(repo)))

    @pytest.mark.parametrize("unregistered", ["", None])
    def test_an_unregistered_repo_exits_one_with_a_plain_message_and_touches_nothing(
            self, monkeypatch, repo, unregistered):
        db = FakeDb(project=unregistered)
        result = self._invoke(monkeypatch, db, "--repo", str(repo))
        assert result.exit_code == 1
        assert "not registered" in result.output
        assert os.path.abspath(str(repo)) in result.output
        assert "Traceback" not in result.output
        assert [c[0] for c in db.calls] == ["resolve"]
        assert self.notified == []

    def test_constraints_are_applied_before_the_first_read_or_write(self, monkeypatch, repo):
        db = FakeDb()
        self._invoke(monkeypatch, db, "--repo", str(repo))
        ops = [c[0] for c in db.calls]
        assert ops[:3] == ["resolve", "constraints", "hashes"]

    def test_it_prints_exactly_one_summary_line_with_every_count(self, monkeypatch, repo):
        db = FakeDb()
        result = self._invoke(monkeypatch, db, "--repo", str(repo))
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 1
        match = re.fullmatch(
            r"Docs ingest: project=proj scanned=(\d+) unchanged=(\d+) written=(\d+) chunks=(\d+) "
            r"deleted=(\d+) skipped=(\d+)", lines[0])
        assert match, lines[0]
        scanned, unchanged, written, chunks, deleted, skipped = map(int, match.groups())
        assert (scanned, unchanged, written, deleted, skipped) == (7, 0, 7, 0, 0)
        assert chunks == sum(len(c) for _, _, c in db.replaced) > 0

    def test_the_skipped_count_adds_large_and_outside_files(self, monkeypatch, repo, tmp_path):
        _put(repo, "docs/big.md", "x" * (MAX_BYTES + 1))
        (repo / "docs" / "escape.md").symlink_to(_put(tmp_path / "away", "away.md"))
        result = self._invoke(monkeypatch, FakeDb(), "--repo", str(repo))
        assert "skipped=2" in result.output

    def test_the_daemon_reload_is_requested_once_after_a_write(self, monkeypatch, repo):
        self._invoke(monkeypatch, FakeDb(), "--repo", str(repo))
        assert self.notified == [1]

    def test_the_daemon_reload_is_requested_after_a_delete_only_run(self, monkeypatch, repo):
        db = FakeDb()
        self._invoke(monkeypatch, db, "--repo", str(repo))
        self.notified.clear()
        (repo / "docs" / "guide.md").unlink()
        result = self._invoke(monkeypatch, db, "--repo", str(repo))
        assert "written=0" in result.output and "deleted=1" in result.output
        assert self.notified == [1]

    def test_no_reload_is_requested_when_nothing_changed(self, monkeypatch, repo):
        db = FakeDb()
        self._invoke(monkeypatch, db, "--repo", str(repo))
        self.notified.clear()
        result = self._invoke(monkeypatch, db, "--repo", str(repo))
        assert "written=0" in result.output and "deleted=0" in result.output
        assert self.notified == []

    def test_writ_daemon_reload_zero_stops_the_real_notice_from_reaching_the_daemon(self, monkeypatch, repo):
        monkeypatch.undo()
        import writ.session.feedback as feedback
        reached: list[str] = []
        monkeypatch.setattr(feedback, "_daemon_client", lambda: reached.append("client"))
        monkeypatch.setenv("WRIT_DAEMON_RELOAD", "0")
        monkeypatch.setattr(cli, "_writ_db", _fake_writ_db(FakeDb()))
        result = runner.invoke(app, ["docs", "ingest", "--repo", str(repo)])
        assert result.exit_code == 0, result.output
        assert reached == []

    def test_claude_md_is_ingested_only_with_the_flag(self, monkeypatch, repo):
        _put(repo, "CLAUDE.md")
        plain = FakeDb()
        self._invoke(monkeypatch, plain, "--repo", str(repo))
        assert "CLAUDE.md" not in plain.kinds_written()
        flagged = FakeDb()
        self._invoke(monkeypatch, flagged, "--repo", str(repo), "--include-claude-md")
        assert flagged.kinds_written()["CLAUDE.md"] == "claude_md"

    def test_memory_dir_files_are_ingested_and_the_index_file_is_skipped(self, monkeypatch, repo, tmp_path):
        mem = tmp_path / "memory"
        _put(mem, "MEMORY.md", "- [Note](note.md) - an index line\n")
        _put(mem, "note.md", "---\nname: note\n---\nthe fact worth remembering\n")
        db = FakeDb()
        result = self._invoke(monkeypatch, db, "--repo", str(repo), "--memory-dir", str(mem))
        assert result.exit_code == 0, result.output
        written = db.kinds_written()
        assert [k for d, k in written.items() if d.endswith("/note.md")] == ["memory"]
        assert not [d for d in written if d.endswith("MEMORY.md")]

    def test_without_memory_dir_no_memory_document_is_written_or_deleted(self, monkeypatch, repo):
        db = FakeDb(hashes={"memory/old.md": {"source_hash": "h", "kind": "memory"}})
        self._invoke(monkeypatch, db, "--repo", str(repo))
        assert db.deleted == [] and "memory/old.md" in db.hashes

    def test_a_missing_repo_option_defaults_to_the_current_directory(self, monkeypatch, repo):
        monkeypatch.chdir(repo)
        db = FakeDb()
        result = self._invoke(monkeypatch, db)
        assert result.exit_code == 0, result.output
        assert db.calls[0] == ("resolve", os.path.abspath("."))


# --------------------------------------------------------------------------- #
# Code review fixes (program item 5): repo root, skipped files kept, memory ids
# --------------------------------------------------------------------------- #
class TestRepoMustBeTheRegisteredRoot:
    """Doc ids are relative to --repo and deletion is scoped by (project, kind), so a --repo
    below the registered root would ingest the subtree under ids that collide with the root's
    and delete the project's documents outside it. The CLI refuses such a --repo."""

    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch):
        self.notified: list[int] = []
        monkeypatch.setattr(cli, "_notify_daemon_reload", lambda: self.notified.append(1))

    def _invoke(self, monkeypatch, db: FakeDb, *args: str):
        monkeypatch.setattr(cli, "_writ_db", _fake_writ_db(db))
        return runner.invoke(app, ["docs", "ingest", *args])

    def test_a_subdirectory_of_the_registered_root_is_refused_and_nothing_is_written_or_deleted(
            self, monkeypatch, repo):
        db = FakeDb(root=str(repo), hashes={
            "README.md": {"source_hash": "h", "kind": "readme"},
            "docs/guide.md": {"source_hash": "h", "kind": "docs"}})
        result = self._invoke(monkeypatch, db, "--repo", str(repo / "docs"))
        assert result.exit_code != 0
        assert str(repo) in result.output and "Traceback" not in result.output
        assert db.replaced == [] and db.deleted == []
        assert not [c for c in db.calls if c[0] in ("hashes", "replace", "delete")]
        assert {"README.md", "docs/guide.md"} <= set(db.hashes)
        assert self.notified == []

    def test_the_registered_root_itself_is_accepted(self, monkeypatch, repo):
        db = FakeDb(root=str(repo))
        result = self._invoke(monkeypatch, db, "--repo", str(repo))
        assert result.exit_code == 0, result.output
        assert db.replaced


class TestSkippedFilesKeepTheirDocument:
    """A file that grows past the size limit or becomes an escaping symlink is still there:
    its stored document must be kept, not deleted as if the file were gone."""

    @pytest.mark.asyncio
    async def test_a_file_grown_past_the_limit_keeps_its_stored_document(self, repo):
        db = FakeDb()
        await _run(db, repo)
        _put(repo, "docs/guide.md", "# Guide\n\n" + "x" * (MAX_BYTES + 1))
        report = await _run(db, repo)
        assert report.skipped_large == 1
        assert db.deleted == [] and report.deleted == 0
        assert "docs/guide.md" in db.hashes

    @pytest.mark.asyncio
    async def test_a_file_turned_into_an_escaping_symlink_keeps_its_stored_document(self, repo, tmp_path):
        db = FakeDb()
        await _run(db, repo)
        outside = _put(tmp_path / "elsewhere", "away.md", "# Away\n\nbody\n")
        (repo / "docs" / "guide.md").unlink()
        (repo / "docs" / "guide.md").symlink_to(outside)
        report = await _run(db, repo)
        assert report.skipped_outside == 1
        assert db.deleted == [] and report.deleted == 0
        assert "docs/guide.md" in db.hashes


class TestMemoryDocumentIds:
    @pytest.mark.asyncio
    async def test_the_same_file_name_in_two_memory_dirs_gets_two_ids(self, repo, tmp_path):
        first = _put(tmp_path / "proj-one" / "memory", "note.md", "first memory body\n")
        second = _put(tmp_path / "proj-two" / "memory", "note.md", "second memory body\n")
        db = FakeDb()
        await _run(db, repo, ALL_KINDS, [first, second])
        ids = [d.doc_id for _, d, _ in db.replaced if d.kind == "memory"]
        assert len(ids) == 2 and len(set(ids)) == 2
        assert all(i.startswith("memory/") and i.endswith("/note.md") for i in ids)

    @pytest.mark.asyncio
    async def test_a_memory_id_is_stable_so_an_unchanged_memory_is_not_rewritten(self, repo, tmp_path):
        note = _put(tmp_path / "memdir", "note.md", "a stable memory body\n")
        db = FakeDb()
        await _run(db, repo, ALL_KINDS, [note])
        db.replaced.clear()
        report = await _run(db, repo, ALL_KINDS, [note])
        assert not [d for _, d, _ in db.replaced if d.kind == "memory"]
        assert report.deleted == 0

    @pytest.mark.asyncio
    async def test_a_memory_path_under_home_is_stored_home_relative(self, repo, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        note = _put(home / ".claude" / "projects" / "p" / "memory", "note.md", "home memory body\n")
        db = FakeDb()
        await _run(db, repo, ALL_KINDS, [note])
        doc = next(d for _, d, _ in db.replaced if d.kind == "memory")
        assert doc.path == "~/.claude/projects/p/memory/note.md"
        assert str(home) not in doc.path
