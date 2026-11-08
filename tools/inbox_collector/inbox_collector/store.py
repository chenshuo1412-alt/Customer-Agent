"""Transactional SQLite state. Identity rows outlive redacted message bodies."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    identity_hash TEXT PRIMARY KEY,
    conversation_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    message_time TEXT NOT NULL,
    time_precision TEXT NOT NULL,
    list_time_text TEXT,
    body TEXT NOT NULL,
    reply_status TEXT,
    reply_status_source TEXT,
    reply_status_reliability TEXT,
    baseline INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbounds (
    id INTEGER PRIMARY KEY,
    identity_hash TEXT NOT NULL UNIQUE,
    conversation_key TEXT NOT NULL,
    status TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    batch_key TEXT NOT NULL UNIQUE,
    conversation_key TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT NOT NULL,
    synthetic INTEGER NOT NULL,
    draft_text TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_messages (
    draft_id INTEGER NOT NULL,
    identity_hash TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    last_error TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    code TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    count INTEGER NOT NULL,
    detail TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        self.db_path = self.data_dir / "collector.sqlite"
        self.conn = sqlite3.connect(
            self.db_path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        os.chmod(self.db_path, 0o600)
        self._txn = False
        if self.get_meta("state") is None:
            self.set_state("stopped", "never_started")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def immediate(self) -> Iterator[None]:
        if self._txn:
            raise RuntimeError("nested_transaction")
        self._txn = True
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            self._txn = False
            raise
        else:
            self.conn.commit()
            self._txn = False

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def state_reason(self) -> tuple[str, str]:
        return (
            self.get_meta("state") or "stopped",
            self.get_meta("state_reason") or "never_started",
        )

    def set_state(self, state: str, reason: str) -> None:
        if state not in {"running", "paused", "stopped"}:
            raise ValueError("bad_state")
        now = utc_now()
        owns = not self._txn
        if owns:
            self.conn.execute("BEGIN IMMEDIATE")
            self._txn = True
        try:
            self.set_meta("state", state)
            self.set_meta("state_reason", reason)
            self.conn.execute(
                "INSERT INTO run_log(state, reason, at) VALUES(?, ?, ?)",
                (state, reason, now),
            )
            if owns:
                self.conn.commit()
        except BaseException:
            if owns:
                self.conn.rollback()
            raise
        finally:
            if owns:
                self._txn = False

    def run_log(self) -> list[tuple[str, str]]:
        rows = self.conn.execute("SELECT state, reason FROM run_log ORDER BY id").fetchall()
        return [(str(row["state"]), str(row["reason"])) for row in rows]

    def baseline_done(self) -> bool:
        return self.get_meta("baseline_done") == "1"

    def mark_baseline(self) -> None:
        self.set_meta("baseline_done", "1")

    def touch_success(self, when: str | None = None) -> None:
        self.set_meta("last_success_collect_at", when or utc_now())
        self.set_meta("failure_streak", "0")

    def note_inbound(self, when: str | None = None) -> None:
        self.set_meta("last_inbound_at", when or utc_now())

    def failure_streak(self) -> int:
        raw = self.get_meta("failure_streak") or "0"
        return int(raw)

    def increment_failure(self) -> int:
        streak = self.failure_streak() + 1
        self.set_meta("failure_streak", str(streak))
        return streak

    def reset_failure(self) -> None:
        self.set_meta("failure_streak", "0")

    def insert_message(self, row: dict[str, object]) -> bool:
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO messages (
                identity_hash, conversation_key, message_id, direction, message_time,
                time_precision, list_time_text, body, reply_status, reply_status_source,
                reply_status_reliability, baseline, source_kind, first_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["identity_hash"],
                row["conversation_key"],
                row["message_id"],
                row["direction"],
                row["message_time"],
                row["time_precision"],
                row["list_time_text"],
                row["body"],
                row["reply_status"],
                row["reply_status_source"],
                row["reply_status_reliability"],
                row["baseline"],
                row["source_kind"],
                row["first_seen_at"],
            ),
        )
        del cursor
        changed = self.conn.execute("SELECT changes()").fetchone()
        return int(changed[0]) == 1

    def insert_inbound(self, identity_hash: str, conversation_key: str, status: str, detected_at: str) -> bool:
        self.conn.execute(
            "INSERT OR IGNORE INTO inbounds(identity_hash, conversation_key, status, detected_at) "
            "VALUES(?, ?, ?, ?)",
            (identity_hash, conversation_key, status, detected_at),
        )
        changed = self.conn.execute("SELECT changes()").fetchone()
        return int(changed[0]) == 1

    def undrafted_inbounds(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT i.identity_hash, i.conversation_key, i.status, m.message_time,
                       m.time_precision, m.body, m.message_id, m.direction,
                       m.reply_status, m.reply_status_reliability, m.list_time_text
                FROM inbounds i
                JOIN messages m ON m.identity_hash = i.identity_hash
                LEFT JOIN draft_messages d ON d.identity_hash = i.identity_hash
                WHERE d.identity_hash IS NULL
                ORDER BY m.message_time, m.message_id
                """
            )
        )

    def merchant_messages(self, conversation_key: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT message_time, time_precision, message_id
                FROM messages
                WHERE conversation_key=? AND direction='merchant'
                ORDER BY message_time, message_id
                """,
                (conversation_key,),
            )
        )

    def insert_draft(self, row: dict[str, object], identity_hashes: list[str]) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO drafts (
                batch_key, conversation_key, status, provider, synthetic,
                draft_text, reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["batch_key"],
                row["conversation_key"],
                row["status"],
                row["provider"],
                row["synthetic"],
                row["draft_text"],
                row["reason"],
                row["created_at"],
                row["updated_at"],
            ),
        )
        draft_id = int(cursor.lastrowid)
        for position, identity_hash in enumerate(identity_hashes):
            self.conn.execute(
                "INSERT INTO draft_messages(draft_id, identity_hash, position) VALUES(?, ?, ?)",
                (draft_id, identity_hash, position),
            )
        return draft_id

    def drafts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM drafts ORDER BY id"))

    def draft_messages(self, draft_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT m.* FROM draft_messages d
                JOIN messages m ON m.identity_hash = d.identity_hash
                WHERE d.draft_id=?
                ORDER BY d.position
                """,
                (draft_id,),
            )
        )

    def count_drafts(self, status: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM drafts WHERE status=?", (status,)).fetchone()
        return int(row["n"])

    def count_inbounds(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM inbounds").fetchone()
        return int(row["n"])

    def count_messages(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        return int(row["n"])

    def get_outbox_by_batch(self, batch_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM outbox WHERE batch_key=?", (batch_key,)).fetchone()

    def insert_outbox(self, row: dict[str, object]) -> None:
        self.conn.execute(
            """
            INSERT INTO outbox (
                event_id, batch_key, status, attempts, last_error, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["event_id"],
                row["batch_key"],
                row["status"],
                row["attempts"],
                row["last_error"],
                row["payload_json"],
                row["created_at"],
                row["updated_at"],
            ),
        )

    def update_outbox(self, event_id: str, status: str, attempts: int, last_error: str | None) -> None:
        self.conn.execute(
            "UPDATE outbox SET status=?, attempts=?, last_error=?, updated_at=? WHERE event_id=?",
            (status, attempts, last_error, utc_now(), event_id),
        )

    def update_draft_status(self, batch_key: str, status: str, reason: str | None) -> None:
        self.conn.execute(
            "UPDATE drafts SET status=?, reason=?, updated_at=? WHERE batch_key=?",
            (status, reason, utc_now(), batch_key),
        )

    def outbox_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM outbox ORDER BY created_at"))

    def open_alert(self, code: str) -> int:
        now = utc_now()
        existing = self.conn.execute("SELECT count FROM alerts WHERE code=?", (code,)).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO alerts(code, state, count, detail, updated_at) VALUES(?, 'open', 1, ?, ?)",
                (code, code, now),
            )
            return 1
        count = int(existing["count"]) + 1
        self.conn.execute(
            "UPDATE alerts SET state='open', count=?, detail=?, updated_at=? WHERE code=?",
            (count, code, now, code),
        )
        return count

    def open_alert_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE state='open'").fetchone()
        return int(row["n"])

    def alert_count(self, code: str) -> int:
        row = self.conn.execute("SELECT count FROM alerts WHERE code=?", (code,)).fetchone()
        return 0 if row is None else int(row["count"])

    def clear_alerts(self) -> None:
        self.conn.execute("UPDATE alerts SET state='cleared', updated_at=?", (utc_now(),))

    def redact_expired(self, retention_days: int, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        cutoff = (current - timedelta(days=retention_days)).isoformat()
        message_cursor = self.conn.execute(
            "UPDATE messages SET body='' WHERE body!='' AND first_seen_at < ?",
            (cutoff,),
        )
        self.conn.execute(
            "UPDATE drafts SET draft_text=NULL WHERE draft_text IS NOT NULL AND created_at < ?",
            (cutoff,),
        )
        return int(message_cursor.rowcount)

    def message_body(self, identity_hash: str) -> str | None:
        row = self.conn.execute(
            "SELECT body FROM messages WHERE identity_hash=?",
            (identity_hash,),
        ).fetchone()
        return None if row is None else str(row["body"])
