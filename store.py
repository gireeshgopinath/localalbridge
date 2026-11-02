"""Durable SQLite store for orchestrator batches and events."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

_ACTIVE = ("queued", "running", "integrating")


class Store:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / "orchestrator.sqlite3"
        self.lock = threading.RLock()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.closing(self._conn()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created REAL NOT NULL
                );
                """
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path), timeout=30)

    @contextlib.contextmanager
    def closing(self, conn):
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, payload: dict) -> str:
        bid = uuid.uuid4().hex
        now = time.time()
        with self.lock, self.closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO batches (id, status, payload, created, updated)"
                " VALUES (?, 'queued', ?, ?, ?)",
                (bid, json.dumps(payload), now, now),
            )
        return bid

    def get(self, id: str) -> dict:
        with self.lock, self.closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT id, status, payload, created, updated FROM batches WHERE id=?", (id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"batch not found: {id}")
        data = json.loads(row[2])
        return {**data, "id": row[0], "status": row[1], "created": row[3], "updated": row[4]}

    def update(self, id: str, status: str | None = None, **payload_fields) -> dict:
        with self.lock, self.closing(self._conn()) as conn:
            cur = conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, payload FROM batches WHERE id=?", (id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"batch not found: {id}")
            data = json.loads(row[1])
            data.update(payload_fields)
            new_status = status if status is not None else row[0]
            now = time.time()
            conn.execute(
                "UPDATE batches SET status=?, payload=?, updated=? WHERE id=?",
                (new_status, json.dumps(data), now, id),
            )
            conn.commit()
        return self.get(id)

    def event(self, batch_id: str, event: str, **data) -> None:
        with self.lock, self.closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO events (batch_id, event, payload, created)"
                " VALUES (?, ?, ?, ?)",
                (batch_id, event, json.dumps(data), time.time()),
            )

    def events(self, batch_id: str) -> list[dict]:
        with self.lock, self.closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT id, batch_id, event, payload, created FROM events"
                " WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        return [
            {"id": r[0], "batch_id": r[1], "event": r[2], **json.loads(r[3]), "created": r[4]}
            for r in rows
        ]

    def recover(self) -> list[str]:
        """Mark active batches blocked with error Interrupted. Never resumes work.

        Runs automatically at startup with no argument; resuming a blocked batch
        later still requires an explicit orchestrator revision via decide_batch."""
        changed: list[str] = []
        now = time.time()
        with self.lock, self.closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT id, payload FROM batches WHERE status IN (%s)" % ",".join("?" * len(_ACTIVE)),
                _ACTIVE,
            ).fetchall()
            for bid, payload in rows:
                data = json.loads(payload)
                data["error"] = "Interrupted; explicit orchestrator revision required"
                conn.execute(
                    "UPDATE batches SET status='blocked', payload=?, updated=? WHERE id=?",
                    (json.dumps(data), now, bid),
                )
                changed.append(bid)
        for bid in changed:
            self.event(bid, "recovered", error="Interrupted; explicit orchestrator revision required")
        return changed
