"""SQLite storage for registered servers and in-flight calls.

v1 runs as a single instance. Arguments and results are removed as soon as
they have been delivered, so the gateway keeps as little user data as possible.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    repository_id     TEXT PRIMARY KEY,
    full_name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    workflow_path     TEXT NOT NULL,
    ref               TEXT NOT NULL,
    token_hash        TEXT,
    manifest_json     TEXT,
    updated_at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id                TEXT PRIMARY KEY,
    repository_id     TEXT NOT NULL,
    tool              TEXT NOT NULL,
    arguments_json    TEXT,
    status            TEXT NOT NULL,   -- pending | running | done | failed | timeout
    approval_env      TEXT,            -- GitHub Environment the run must be in, if the tool needs approval
    run_id            TEXT,
    created_at        REAL NOT NULL,
    finished_at       REAL
);
CREATE INDEX IF NOT EXISTS calls_by_repo ON calls(repository_id, status);
"""


@dataclass
class ServerRecord:
    repository_id: str
    full_name: str
    workflow_path: str
    ref: str
    token_hash: str | None
    manifest: dict[str, Any] | None

    def approval_environment_for(self, tool: str) -> str | None:
        """The Environment a call to ``tool`` must run in, or None if no approval is needed."""
        manifest = self.manifest or {}
        if tool in (manifest.get("approval_required") or []):
            return manifest.get("approval_environment") or "mcp-approval"
        return None

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.full_name.split("/", 1)[1]


@dataclass
class CallRecord:
    id: str
    repository_id: str
    tool: str
    arguments: dict[str, Any] | None
    status: str
    run_id: str | None
    approval_env: str | None = None


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    # -- servers -----------------------------------------------------------

    def _server(self, row: sqlite3.Row | None) -> ServerRecord | None:
        if row is None:
            return None
        return ServerRecord(
            repository_id=row["repository_id"],
            full_name=row["full_name"],
            workflow_path=row["workflow_path"],
            ref=row["ref"],
            token_hash=row["token_hash"],
            manifest=json.loads(row["manifest_json"]) if row["manifest_json"] else None,
        )

    def get_server_by_name(self, full_name: str) -> ServerRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM servers WHERE full_name = ?", (full_name,)).fetchone()
        return self._server(row)

    def get_server(self, repository_id: str) -> ServerRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM servers WHERE repository_id = ?", (repository_id,)).fetchone()
        return self._server(row)

    def upsert_server(
        self,
        *,
        repository_id: str,
        full_name: str,
        workflow_path: str,
        ref: str,
        manifest: dict[str, Any],
        token_hash: str | None,
    ) -> None:
        with self._lock:
            # A renamed repo keeps its id; free the old name if another row held it.
            self._db.execute(
                "DELETE FROM servers WHERE full_name = ? AND repository_id != ?", (full_name, repository_id)
            )
            self._db.execute(
                """
                INSERT INTO servers (repository_id, full_name, workflow_path, ref, token_hash, manifest_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    workflow_path = excluded.workflow_path,
                    ref = excluded.ref,
                    token_hash = COALESCE(excluded.token_hash, servers.token_hash),
                    manifest_json = excluded.manifest_json,
                    updated_at = excluded.updated_at
                """,
                (repository_id, full_name, workflow_path, ref, token_hash, json.dumps(manifest), time.time()),
            )

    # -- calls -------------------------------------------------------------

    def create_call(
        self,
        call_id: str,
        repository_id: str,
        tool: str,
        arguments: dict[str, Any],
        approval_env: str | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO calls (id, repository_id, tool, arguments_json, status, approval_env, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (call_id, repository_id, tool, json.dumps(arguments), approval_env, time.time()),
            )

    def get_call(self, call_id: str) -> CallRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        if row is None:
            return None
        return CallRecord(
            id=row["id"],
            repository_id=row["repository_id"],
            tool=row["tool"],
            arguments=json.loads(row["arguments_json"]) if row["arguments_json"] else None,
            status=row["status"],
            run_id=row["run_id"],
            approval_env=row["approval_env"],
        )

    def claim_call(self, call_id: str, run_id: str) -> bool:
        """pending -> running, once. Arguments are dropped after the runner reads them."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE calls SET status = 'running', run_id = ? WHERE id = ? AND status = 'pending'",
                (run_id, call_id),
            )
            return cur.rowcount == 1

    def clear_arguments(self, call_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE calls SET arguments_json = NULL WHERE id = ?", (call_id,))

    def finish_call(self, call_id: str, status: str, *, from_status: tuple[str, ...]) -> bool:
        # Only "?" placeholders are interpolated; every value is bound as a parameter.
        marks = ",".join("?" * len(from_status))
        with self._lock:
            cur = self._db.execute(
                f"UPDATE calls SET status = ?, finished_at = ?, arguments_json = NULL "  # noqa: S608
                f"WHERE id = ? AND status IN ({marks})",
                (status, time.time(), call_id, *from_status),
            )
            return cur.rowcount == 1

    def count_calls_since(self, repository_id: str, since: float) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM calls WHERE repository_id = ? AND created_at >= ?",
                (repository_id, since),
            ).fetchone()
        return int(row[0])

    def count_active_calls(self, repository_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM calls WHERE repository_id = ? AND status IN ('pending', 'running')",
                (repository_id,),
            ).fetchone()
        return int(row[0])
