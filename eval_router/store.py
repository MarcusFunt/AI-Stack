from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .schemas import (
    DeepEvaluationResult,
    EscalationRecord,
    EvaluationScreenResult,
)


class EvaluationStore:
    """Small durable SQLite store for screening results and deep-eval work."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS screenings (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    invocation_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS escalations (
                    id TEXT PRIMARY KEY,
                    screening_id TEXT NOT NULL UNIQUE REFERENCES screenings(id),
                    invocation_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_by TEXT,
                    result_json TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_escalations_queue ON escalations(status, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_screenings_invocation ON screenings(invocation_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_escalations_claims ON escalations(status, updated_at)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _screening(row: sqlite3.Row) -> EvaluationScreenResult:
        return EvaluationScreenResult.model_validate_json(row["result_json"])

    @staticmethod
    def _escalation(row: sqlite3.Row) -> EscalationRecord:
        return EscalationRecord(
            id=row["id"],
            screening_id=row["screening_id"],
            invocation_id=row["invocation_id"],
            trace_id=row["trace_id"],
            reasons=json.loads(row["reasons_json"]),
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            claimed_by=row["claimed_by"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
        )

    def _screening_by_event(self, connection: sqlite3.Connection, event_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM screenings WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return row

    def save_screening(
        self,
        event_id: str,
        result: EvaluationScreenResult,
        escalation_reasons: tuple[str, ...] | list[str],
    ) -> tuple[EvaluationScreenResult, EscalationRecord | None]:
        created_at = result.created_at.astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM screenings WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO screenings(id,event_id,invocation_id,trace_id,status,result_json,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        result.id,
                        event_id,
                        result.invocation_id,
                        result.trace_id,
                        result.status,
                        result.model_dump_json(),
                        created_at,
                    ),
                )
                screening_id = result.id
                if escalation_reasons:
                    now = datetime.now(timezone.utc).isoformat()
                    connection.execute(
                        """INSERT INTO escalations(id,screening_id,invocation_id,trace_id,reasons_json,status,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            str(uuid4()),
                            screening_id,
                            result.invocation_id,
                            result.trace_id,
                            json.dumps(list(escalation_reasons), separators=(",", ":")),
                            "queued",
                            now,
                            now,
                        ),
                    )
            else:
                screening_id = existing["id"]
            screening_row = connection.execute(
                "SELECT * FROM screenings WHERE id=?", (screening_id,)
            ).fetchone()
            escalation_row = connection.execute(
                "SELECT * FROM escalations WHERE screening_id=?", (screening_id,)
            ).fetchone()
        return (
            self._screening(screening_row),
            self._escalation(escalation_row) if escalation_row is not None else None,
        )

    def get_screening(self, screening_id: str) -> EvaluationScreenResult:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM screenings WHERE id=?", (screening_id,)).fetchone()
        if row is None:
            raise KeyError(screening_id)
        return self._screening(row)

    def list_screenings(self, limit: int = 100) -> list[EvaluationScreenResult]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM screenings ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._screening(row) for row in rows]

    def get_escalation(self, escalation_id: str) -> EscalationRecord:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM escalations WHERE id=?", (escalation_id,)).fetchone()
        if row is None:
            raise KeyError(escalation_id)
        return self._escalation(row)

    def list_escalations(self, limit: int = 100) -> list[EscalationRecord]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM escalations ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._escalation(row) for row in rows]

    def claim_next_escalation(
        self, worker_id: str, *, claim_ttl_seconds: float = 120.0
    ) -> EscalationRecord | None:
        if not 1 <= claim_ttl_seconds <= 86_400:
            raise ValueError("claim_ttl_seconds must be between 1 and 86400")
        current = datetime.now(timezone.utc)
        now = current.isoformat()
        expired_before = (current - timedelta(seconds=claim_ttl_seconds)).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE escalations SET status='queued',claimed_by=NULL,updated_at=?
                   WHERE status='claimed' AND updated_at < ?""",
                (now, expired_before),
            )
            row = connection.execute(
                "SELECT * FROM escalations WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE escalations SET status='claimed',claimed_by=?,updated_at=? WHERE id=? AND status='queued'",
                (worker_id, now, row["id"]),
            )
            claimed = connection.execute("SELECT * FROM escalations WHERE id=?", (row["id"],)).fetchone()
        return self._escalation(claimed)

    def release_escalation(self, escalation_id: str, worker_id: str) -> EscalationRecord:
        """Return work to the queue when a worker is cancelled before completion."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE escalations SET status='queued',claimed_by=NULL,updated_at=?
                   WHERE id=? AND status='claimed' AND claimed_by=?""",
                (now, escalation_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("escalation is not claimed by this worker")
        return self.get_escalation(escalation_id)

    def complete_escalation(
        self,
        escalation_id: str,
        worker_id: str,
        result: DeepEvaluationResult | dict[str, Any],
    ) -> EscalationRecord:
        normalized = DeepEvaluationResult.model_validate(result)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE escalations SET status='completed',result_json=?,updated_at=?
                   WHERE id=? AND status='claimed' AND claimed_by=?""",
                (normalized.model_dump_json(), now, escalation_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("escalation is not claimed by this worker")
        return self.get_escalation(escalation_id)

    def fail_escalation(self, escalation_id: str, worker_id: str, error_code: str) -> EscalationRecord:
        now = datetime.now(timezone.utc).isoformat()
        error_code = error_code[:80]
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE escalations SET status='error',result_json=?,updated_at=?
                   WHERE id=? AND status='claimed' AND claimed_by=?""",
                (json.dumps({"error_code": error_code}), now, escalation_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("escalation is not claimed by this worker")
        return self.get_escalation(escalation_id)
