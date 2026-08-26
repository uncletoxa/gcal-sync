from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_state (
    calendar_key TEXT PRIMARY KEY,
    sync_token TEXT,
    last_full_sync_at TEXT
);

CREATE TABLE IF NOT EXISTS event_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_account TEXT NOT NULL,
    source_calendar_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    dest_account TEXT NOT NULL,
    dest_calendar_id TEXT NOT NULL,
    dest_event_id TEXT,
    source_signature TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_account, source_calendar_id, source_event_id, dest_account, dest_calendar_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """SQLite-backed persistence for sync tokens and source->mirror event mappings."""

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- sync tokens ---------------------------------------------------
    def get_sync_token(self, calendar_key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT sync_token FROM sync_state WHERE calendar_key = ?", (calendar_key,)
        ).fetchone()
        return row["sync_token"] if row else None

    def set_sync_token(self, calendar_key: str, token: Optional[str]) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_state (calendar_key, sync_token, last_full_sync_at)
            VALUES (?, ?, NULL)
            ON CONFLICT(calendar_key) DO UPDATE SET sync_token = excluded.sync_token
            """,
            (calendar_key, token),
        )
        self._conn.commit()

    def get_last_full_sync(self, calendar_key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT last_full_sync_at FROM sync_state WHERE calendar_key = ?", (calendar_key,)
        ).fetchone()
        return row["last_full_sync_at"] if row else None

    def set_last_full_sync(self, calendar_key: str, iso_timestamp: str) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_state (calendar_key, sync_token, last_full_sync_at)
            VALUES (?, NULL, ?)
            ON CONFLICT(calendar_key) DO UPDATE SET last_full_sync_at = excluded.last_full_sync_at
            """,
            (calendar_key, iso_timestamp),
        )
        self._conn.commit()

    # -- event mappings --------------------------------------------------
    def get_mapping(
        self, source_account, source_calendar_id, source_event_id, dest_account, dest_calendar_id
    ) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM event_mappings
            WHERE source_account = ? AND source_calendar_id = ? AND source_event_id = ?
              AND dest_account = ? AND dest_calendar_id = ?
            """,
            (source_account, source_calendar_id, source_event_id, dest_account, dest_calendar_id),
        ).fetchone()

    def upsert_mapping(
        self,
        *,
        source_account: str,
        source_calendar_id: str,
        source_event_id: str,
        dest_account: str,
        dest_calendar_id: str,
        dest_event_id: Optional[str],
        source_signature: str,
        status: str = "active",
    ) -> None:
        now = _now()
        self._conn.execute(
            """
            INSERT INTO event_mappings (
                source_account, source_calendar_id, source_event_id,
                dest_account, dest_calendar_id, dest_event_id,
                source_signature, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_account, source_calendar_id, source_event_id, dest_account, dest_calendar_id)
            DO UPDATE SET
                dest_event_id = excluded.dest_event_id,
                source_signature = excluded.source_signature,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                source_account,
                source_calendar_id,
                source_event_id,
                dest_account,
                dest_calendar_id,
                dest_event_id,
                source_signature,
                status,
                now,
                now,
            ),
        )
        self._conn.commit()

    def mark_deleted(self, mapping_id: int) -> None:
        self._conn.execute(
            "UPDATE event_mappings SET status = 'deleted', dest_event_id = NULL, updated_at = ? WHERE id = ?",
            (_now(), mapping_id),
        )
        self._conn.commit()

    def list_active_mappings_for_pair(
        self, source_account, source_calendar_id, dest_account, dest_calendar_id
    ) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM event_mappings
            WHERE source_account = ? AND source_calendar_id = ?
              AND dest_account = ? AND dest_calendar_id = ? AND status = 'active'
            """,
            (source_account, source_calendar_id, dest_account, dest_calendar_id),
        ).fetchall()
