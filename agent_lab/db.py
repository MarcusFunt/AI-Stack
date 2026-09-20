from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import EventRecord, RunRecord, RunStatus, TaskSpec


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    workspace TEXT,
                    base_commit TEXT,
                    selected_harness TEXT,
                    result_json TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
                """
            )

    def create_run(self, run_id: str, task: TaskSpec) -> RunRecord:
        now = utcnow()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs(id,status,task_json,created_at,updated_at) VALUES(?,?,?,?,?)",
                (run_id, RunStatus.PREPARING.value, task.model_dump_json(), now, now),
            )
        self.add_event(run_id, "run_created", {"task_type": task.task_type})
        return self.get_run(run_id)

    def update_run(self, run_id: str, **changes: Any) -> RunRecord:
        if not changes:
            return self.get_run(run_id)
        fields: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            if key == "status" and isinstance(value, RunStatus):
                value = value.value
            if key == "result":
                key, value = "result_json", json.dumps(value)
            if key not in {
                "status", "workspace", "base_commit", "selected_harness",
                "result_json", "error",
            }:
                raise ValueError(f"unsupported run field: {key}")
            fields.append(f"{key}=?")
            values.append(value)
        fields.append("updated_at=?")
        values.extend([utcnow(), run_id])
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE runs SET {', '.join(fields)} WHERE id=?", values
            )
            if cur.rowcount != 1:
                raise KeyError(run_id)
        return self.get_run(run_id)

    def _row_to_run(self, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            status=RunStatus(row["status"]),
            task=TaskSpec.model_validate_json(row["task_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            workspace=row["workspace"],
            base_commit=row["base_commit"],
            selected_harness=row["selected_harness"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
        )

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._row_to_run(row)

    def list_runs(self, limit: int = 100) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def add_event(self, run_id: str, kind: str, payload: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events(run_id,ts,kind,payload_json) VALUES(?,?,?,?)",
                (run_id, utcnow(), kind, json.dumps(payload or {})),
            )

    def list_events(self, run_id: str) -> list[EventRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [
            EventRecord(
                id=row["id"],
                run_id=row["run_id"],
                ts=datetime.fromisoformat(row["ts"]),
                kind=row["kind"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]
