"""Phase 2: Origin context SQLite store tests.

Per TEST-ISO-001: each test uses its own temp directory.
"""

from __future__ import annotations

from pathlib import Path

from writ.origin_context import OriginContextStore


class TestOriginContextStore:
    """SQLite origin context store -- write-once, read-only."""

    def test_write_and_read(self, tmp_path: Path) -> None:
        store = OriginContextStore(tmp_path / "origin.db")
        store.write(
            "TEST-001",
            "fixing a bug",
            "how to handle errors",
            ["ARCH-ERR-001"],
        )
        result = store.get("TEST-001")
        store.close()

        assert result is not None
        assert result["rule_id"] == "TEST-001"
        assert result["task_description"] == "fixing a bug"
        assert result["query_that_triggered"] == "how to handle errors"
        assert "ARCH-ERR-001" in result["existing_rules_consulted"]
        assert result["created_at"] is not None

    def test_read_nonexistent_returns_none(self, tmp_path: Path) -> None:
        store = OriginContextStore(tmp_path / "origin.db")
        result = store.get("NONEXISTENT-001")
        store.close()
        assert result is None

    def test_db_created_on_init(self, tmp_path: Path) -> None:
        db_path = tmp_path / "subdir" / "origin.db"
        assert not db_path.exists()
        store = OriginContextStore(db_path)
        store.close()
        assert db_path.exists()

    def test_write_once_no_overwrite(self, tmp_path: Path) -> None:
        store = OriginContextStore(tmp_path / "origin.db")
        store.write("TEST-001", "first task", None, [])
        store.write("TEST-001", "second task", None, [])
        result = store.get("TEST-001")
        store.close()
        assert result["task_description"] == "first task"

    def test_null_query_stored(self, tmp_path: Path) -> None:
        store = OriginContextStore(tmp_path / "origin.db")
        store.write("TEST-001", "task", None, [])
        result = store.get("TEST-001")
        store.close()
        assert result["query_that_triggered"] is None

    def test_empty_consulted_list(self, tmp_path: Path) -> None:
        store = OriginContextStore(tmp_path / "origin.db")
        store.write("TEST-001", "task", "query", [])
        result = store.get("TEST-001")
        store.close()
        assert result["existing_rules_consulted"] == []

    def test_multiple_rules_stored(self, tmp_path: Path) -> None:
        store = OriginContextStore(tmp_path / "origin.db")
        store.write("TEST-001", "task 1", None, [])
        store.write("TEST-002", "task 2", None, ["ARCH-ORG-001"])
        r1 = store.get("TEST-001")
        r2 = store.get("TEST-002")
        store.close()
        assert r1["task_description"] == "task 1"
        assert r2["task_description"] == "task 2"
