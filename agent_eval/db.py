from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import EvaluationRecord, EvaluationStatus


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvaluationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evaluations (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    candidate_commit TEXT NOT NULL,
                    base_commit TEXT NOT NULL,
                    suite TEXT NOT NULL,
                    suite_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    token_budget INTEGER NOT NULL,
                    wall_time_seconds INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    attestation_path TEXT,
                    attestation_signature TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_run ON evaluations(run_id, created_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(
        self,
        evaluation_id: str,
        run_id: str,
        candidate_commit: str,
        base_commit: str,
        suite: str,
        suite_hash: str,
        model: str,
        token_budget: int,
        wall_time_seconds: int,
    ) -> EvaluationRecord:
        now = utcnow()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO evaluations(
                    id,run_id,status,candidate_commit,base_commit,suite,suite_hash,
                    model,token_budget,wall_time_seconds,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    evaluation_id, run_id, EvaluationStatus.QUEUED.value,
                    candidate_commit, base_commit, suite, suite_hash, model,
                    token_budget, wall_time_seconds, now, now,
                ),
            )
        return self.get(evaluation_id)

    def update(self, evaluation_id: str, **changes: Any) -> EvaluationRecord:
        if not changes:
            return self.get(evaluation_id)
        allowed = {
            "status", "result_json", "error", "attestation_path",
            "attestation_signature",
        }
        fields: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            if key == "status" and isinstance(value, EvaluationStatus):
                value = value.value
            if key == "result":
                key, value = "result_json", json.dumps(value)
            if key not in allowed:
                raise ValueError(f"unsupported evaluation field: {key}")
            fields.append(f"{key}=?")
            values.append(value)
        fields.append("updated_at=?")
        values.extend([utcnow(), evaluation_id])
        with self._connection() as conn:
            cur = conn.execute(
                f"UPDATE evaluations SET {', '.join(fields)} WHERE id=?",
                values,
            )
            if cur.rowcount != 1:
                raise KeyError(evaluation_id)
        return self.get(evaluation_id)

    def _row(self, row: sqlite3.Row) -> EvaluationRecord:
        return EvaluationRecord(
            id=row["id"],
            run_id=row["run_id"],
            status=EvaluationStatus(row["status"]),
            candidate_commit=row["candidate_commit"],
            base_commit=row["base_commit"],
            suite=row["suite"],
            suite_hash=row["suite_hash"],
            model=row["model"],
            token_budget=row["token_budget"],
            wall_time_seconds=row["wall_time_seconds"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            attestation_path=row["attestation_path"],
            attestation_signature=row["attestation_signature"],
        )

    def get(self, evaluation_id: str) -> EvaluationRecord:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(evaluation_id)
        return self._row(row)

    def by_run(self, run_id: str, limit: int = 20) -> list[EvaluationRecord]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM evaluations WHERE run_id=? ORDER BY created_at DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def list(self, limit: int = 100) -> list[EvaluationRecord]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM evaluations ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row(row) for row in rows]

    def recover_inflight(self) -> None:
        inflight = {
            EvaluationStatus.QUEUED.value,
            EvaluationStatus.PREPARING.value,
            EvaluationStatus.RUNNING.value,
            EvaluationStatus.CANCELLING.value,
        }
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id,status FROM evaluations WHERE status IN (?,?,?,?)",
                tuple(inflight),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE evaluations SET status=?, error=?, updated_at=? WHERE id=?",
                    (
                        EvaluationStatus.ERROR.value,
                        "evaluator restarted before job reached a terminal state",
                        utcnow(),
                        row["id"],
                    ),
                )
