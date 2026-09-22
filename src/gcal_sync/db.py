from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_state (
    calendar_key TEXT PRIMARY KEY,
    sync_token TEXT,
    last_full_sync_at TEXT,
    last_checked_at TEXT
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

CREATE INDEX IF NOT EXISTS idx_event_mappings_dest
    ON event_mappings(dest_account, dest_calendar_id, dest_event_id);

CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connected_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    account_label TEXT NOT NULL,
    google_email TEXT NOT NULL,
    calendar_id TEXT NOT NULL,
    credentials_json TEXT NOT NULL,
    sync_window_days REAL,
    display_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, account_label)
);

CREATE TABLE IF NOT EXISTS pair_settings (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    source_account_label TEXT NOT NULL,
    dest_account_label TEXT NOT NULL,
    copy_mode TEXT NOT NULL DEFAULT 'busy_only',
    enabled INTEGER NOT NULL DEFAULT 1,
    title_template TEXT,
    description_template TEXT,
    color_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, source_account_label, dest_account_label)
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
        # Multiple processes (poller + gunicorn web workers) share this database file;
        # without this, a writer that finds the file locked fails immediately instead
        # of retrying, surfacing as a spurious "database is locked" error.
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a table's initial CREATE TABLE IF NOT EXISTS,
        which only takes effect for brand-new databases."""
        account_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(connected_accounts)")}
        if "sync_window_days" not in account_columns:
            self._conn.execute("ALTER TABLE connected_accounts ADD COLUMN sync_window_days REAL")
        if "display_name" not in account_columns:
            self._conn.execute("ALTER TABLE connected_accounts ADD COLUMN display_name TEXT")

        sync_state_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(sync_state)")}
        if "last_checked_at" not in sync_state_columns:
            self._conn.execute("ALTER TABLE sync_state ADD COLUMN last_checked_at TEXT")

        pair_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(pair_settings)")}
        if "enabled" not in pair_columns:
            self._conn.execute("ALTER TABLE pair_settings ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        if "title_template" not in pair_columns:
            self._conn.execute("ALTER TABLE pair_settings ADD COLUMN title_template TEXT")
        if "description_template" not in pair_columns:
            self._conn.execute("ALTER TABLE pair_settings ADD COLUMN description_template TEXT")
        if "color_id" not in pair_columns:
            self._conn.execute("ALTER TABLE pair_settings ADD COLUMN color_id TEXT")

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

    def get_last_checked(self, calendar_key: str) -> Optional[str]:
        """When this calendar's source events were last fetched, whether that pass was
        full or incremental — what the dashboard shows as "Synced", since incremental
        passes (the common case) don't move last_full_sync_at."""
        row = self._conn.execute(
            "SELECT last_checked_at FROM sync_state WHERE calendar_key = ?", (calendar_key,)
        ).fetchone()
        return row["last_checked_at"] if row else None

    def set_last_checked(self, calendar_key: str, iso_timestamp: str) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_state (calendar_key, sync_token, last_checked_at)
            VALUES (?, NULL, ?)
            ON CONFLICT(calendar_key) DO UPDATE SET last_checked_at = excluded.last_checked_at
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

    def get_mapping_by_dest_event(
        self, dest_account, dest_calendar_id, dest_event_id
    ) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM event_mappings
            WHERE dest_account = ? AND dest_calendar_id = ? AND dest_event_id = ? AND status = 'active'
            """,
            (dest_account, dest_calendar_id, dest_event_id),
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

    # -- tenants (web sign-up) -------------------------------------------
    def get_or_create_tenant(self, email: str) -> sqlite3.Row:
        """Look up a tenant by their sign-in Google account email, creating one if needed."""
        row = self._conn.execute("SELECT * FROM tenants WHERE email = ?", (email,)).fetchone()
        if row is not None:
            return row
        self._conn.execute(
            "INSERT INTO tenants (email, created_at) VALUES (?, ?)", (email, _now())
        )
        self._conn.commit()
        return self._conn.execute("SELECT * FROM tenants WHERE email = ?", (email,)).fetchone()

    def get_tenant(self, tenant_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
        ).fetchone()

    # -- connected accounts (web sign-up) ---------------------------------
    def upsert_connected_account(
        self,
        *,
        tenant_id: int,
        account_label: str,
        google_email: str,
        calendar_id: str,
        credentials_json: str,
    ) -> None:
        now = _now()
        self._conn.execute(
            """
            INSERT INTO connected_accounts (
                tenant_id, account_label, google_email, calendar_id,
                credentials_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, account_label) DO UPDATE SET
                google_email = excluded.google_email,
                calendar_id = excluded.calendar_id,
                credentials_json = excluded.credentials_json,
                updated_at = excluded.updated_at
            """,
            (tenant_id, account_label, google_email, calendar_id, credentials_json, now, now),
        )
        self._conn.commit()

    def update_connected_account_credentials(self, account_id: int, credentials_json: str) -> None:
        self._conn.execute(
            "UPDATE connected_accounts SET credentials_json = ?, updated_at = ? WHERE id = ?",
            (credentials_json, _now(), account_id),
        )
        self._conn.commit()

    def list_connected_accounts(self, tenant_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM connected_accounts WHERE tenant_id = ? ORDER BY created_at",
            (tenant_id,),
        ).fetchall()


    def get_connected_account(self, tenant_id: int, account_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM connected_accounts WHERE id = ? AND tenant_id = ?",
            (account_id, tenant_id),
        ).fetchone()

    def set_account_sync_window(
        self, tenant_id: int, account_id: int, sync_window_days: Optional[float]
    ) -> None:
        """Per-calendar override for how many days ahead to sync when this account is the
        source; None reverts to the tenant/instance default (Config.sync_window_days)."""
        self._conn.execute(
            "UPDATE connected_accounts SET sync_window_days = ?, updated_at = ? WHERE id = ? AND tenant_id = ?",
            (sync_window_days, _now(), account_id, tenant_id),
        )
        self._conn.commit()


    def set_account_display_name(self, tenant_id: int, account_id: int, display_name: Optional[str]) -> None:
        """User-facing name for this calendar, used as the {calendar} template token when
        its events are mirrored elsewhere. None/empty falls back to showing the account's email."""
        self._conn.execute(
            "UPDATE connected_accounts SET display_name = ?, updated_at = ? WHERE id = ? AND tenant_id = ?",
            (display_name or None, _now(), account_id, tenant_id),
        )
        self._conn.commit()

    def delete_connected_account(self, tenant_id: int, account_id: int) -> None:
        self._conn.execute(
            "DELETE FROM connected_accounts WHERE id = ? AND tenant_id = ?",
            (account_id, tenant_id),
        )
        self._conn.commit()

    def list_tenants_with_accounts(self) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
        """All tenants paired with their connected accounts, for the sync poller."""
        tenants = self._conn.execute("SELECT * FROM tenants ORDER BY id").fetchall()
        return [(tenant, self.list_connected_accounts(tenant["id"])) for tenant in tenants]

    # -- pair settings (web sign-up) --------------------------------------
    def get_full_copy_pairs(self, tenant_id: int) -> set[tuple[str, str]]:
        """Directed (source_label, dest_label) pairs opted into full event copy."""
        rows = self._conn.execute(
            """
            SELECT source_account_label, dest_account_label FROM pair_settings
            WHERE tenant_id = ? AND copy_mode = 'full'
            """,
            (tenant_id,),
        ).fetchall()
        return {(row["source_account_label"], row["dest_account_label"]) for row in rows}

    def set_pair_copy_mode(
        self, tenant_id: int, source_account_label: str, dest_account_label: str, copy_mode: str
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO pair_settings (
                tenant_id, source_account_label, dest_account_label, copy_mode, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, source_account_label, dest_account_label) DO UPDATE SET
                copy_mode = excluded.copy_mode,
                updated_at = excluded.updated_at
            """,
            (tenant_id, source_account_label, dest_account_label, copy_mode, _now()),
        )
        self._conn.commit()


    def get_disabled_pairs(self, tenant_id: int) -> set[tuple[str, str]]:
        """Directed (source_label, dest_label) pairs excluded from syncing entirely."""
        rows = self._conn.execute(
            """
            SELECT source_account_label, dest_account_label FROM pair_settings
            WHERE tenant_id = ? AND enabled = 0
            """,
            (tenant_id,),
        ).fetchall()
        return {(row["source_account_label"], row["dest_account_label"]) for row in rows}

    def set_pair_enabled(
        self, tenant_id: int, source_account_label: str, dest_account_label: str, enabled: bool
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO pair_settings (
                tenant_id, source_account_label, dest_account_label, enabled, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, source_account_label, dest_account_label) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (tenant_id, source_account_label, dest_account_label, int(enabled), _now()),
        )
        self._conn.commit()


    def get_pair_templates(self, tenant_id: int) -> dict[tuple[str, str], tuple[Optional[str], Optional[str]]]:
        """Directed (source_label, dest_label) -> (title_template, description_template)
        for pairs with a custom template configured; pairs absent here use the default
        passthrough (mirror the source's own title/description verbatim)."""
        rows = self._conn.execute(
            """
            SELECT source_account_label, dest_account_label, title_template, description_template
            FROM pair_settings
            WHERE tenant_id = ? AND (title_template IS NOT NULL OR description_template IS NOT NULL)
            """,
            (tenant_id,),
        ).fetchall()
        return {
            (row["source_account_label"], row["dest_account_label"]): (row["title_template"], row["description_template"])
            for row in rows
        }

    def set_pair_templates(
        self,
        tenant_id: int,
        source_account_label: str,
        dest_account_label: str,
        title_template: Optional[str],
        description_template: Optional[str],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO pair_settings (
                tenant_id, source_account_label, dest_account_label,
                title_template, description_template, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, source_account_label, dest_account_label) DO UPDATE SET
                title_template = excluded.title_template,
                description_template = excluded.description_template,
                updated_at = excluded.updated_at
            """,
            (tenant_id, source_account_label, dest_account_label, title_template, description_template, _now()),
        )
        self._conn.commit()


    def get_pair_colors(self, tenant_id: int) -> dict[tuple[str, str], Optional[str]]:
        """Directed (source_label, dest_label) -> Google Calendar eventColor id applied
        to mirrored events for that pair; pairs absent here leave the destination
        calendar's default event color untouched."""
        rows = self._conn.execute(
            """
            SELECT source_account_label, dest_account_label, color_id FROM pair_settings
            WHERE tenant_id = ? AND color_id IS NOT NULL
            """,
            (tenant_id,),
        ).fetchall()
        return {(row["source_account_label"], row["dest_account_label"]): row["color_id"] for row in rows}

    def set_pair_color(
        self, tenant_id: int, source_account_label: str, dest_account_label: str, color_id: Optional[str]
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO pair_settings (
                tenant_id, source_account_label, dest_account_label, color_id, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, source_account_label, dest_account_label) DO UPDATE SET
                color_id = excluded.color_id,
                updated_at = excluded.updated_at
            """,
            (tenant_id, source_account_label, dest_account_label, color_id, _now()),
        )
        self._conn.commit()
