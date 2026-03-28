"""Origin context store -- SQLite backend for AI rule proposal context.

Write-once blob store. Written at AI rule proposal time (Phase 3),
read by `writ review <rule_id>`. If the DB or entry doesn't exist,
callers degrade gracefully.

Per ARCH-ORG-001: separate module, not coupled to graph layer.
Per ARCH-DBCLIENT-001: owns its own connection lifecycle.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".cache" / "writ" / "origin_context.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS origin_context (
    rule_id TEXT PRIMARY KEY,
    task_description TEXT NOT NULL,
    query_that_triggered TEXT,
    existing_rules_consulted TEXT,
    created_at TEXT NOT NULL
)
"""


class OriginContextStore:
    """SQLite store for AI rule proposal origin context."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    def write(
        self,
        rule_id: str,
        task_description: str,
        query_that_triggered: str | None,
        existing_rules_consulted: list[str],
    ) -> None:
        """Write origin context for a rule. Write-once -- ignores duplicates."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO origin_context
                (rule_id, task_description, query_that_triggered,
                 existing_rules_consulted, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                task_description,
                query_that_triggered,
                json.dumps(existing_rules_consulted),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def get(self, rule_id: str) -> dict | None:
        """Read origin context for a rule. Returns None if not found."""
        cursor = self._conn.execute(
            "SELECT rule_id, task_description, query_that_triggered, "
            "existing_rules_consulted, created_at "
            "FROM origin_context WHERE rule_id = ?",
            (rule_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "rule_id": row[0],
            "task_description": row[1],
            "query_that_triggered": row[2],
            "existing_rules_consulted": json.loads(row[3]) if row[3] else [],
            "created_at": row[4],
        }

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()