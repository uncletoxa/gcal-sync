from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Config:
    client_secrets_file: str
    token_dir: str
    db_path: str
    calendars: dict[str, str]
    poll_interval_seconds: int
    full_resync_interval_hours: float
    sync_window_days: float
    log_level: str
    # Web sign-up (multi-tenant) settings — optional, only needed to run `gcal-sync web`
    # or to have the poller pick up web-connected tenants.
    google_web_client_secrets_file: str = "data/web_client_secret.json"
    web_base_url: str = ""
    web_secret_key: str = ""
    token_encryption_key: str = ""
    # Restricts *new* sign-ups to Google accounts on this domain (e.g. "company.com").
    # Only enforced when creating a brand-new tenant; a tenant's later, additional
    # calendar connections (e.g. a personal Gmail account) are unaffected.
    allowed_domain: str = ""


def _parse_calendars(raw: str) -> dict[str, str]:
    """Parse 'account:calendar_id,account:calendar_id,...' into an ordered dict."""
    calendars: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise SystemExit(
                f"Invalid CALENDARS entry '{entry}': expected '<account>:<calendar_id>' "
                "(see .env.example)."
            )
        account, calendar_id = entry.split(":", 1)
        account, calendar_id = account.strip(), calendar_id.strip()
        if not account or not calendar_id:
            raise SystemExit(f"Invalid CALENDARS entry '{entry}': account and calendar id must be non-empty.")
        calendars[account] = calendar_id
    return calendars


def load_config(require_calendars: bool = True) -> Config:
    load_dotenv()

    calendars = _parse_calendars(os.getenv("CALENDARS", ""))

    if require_calendars and len(calendars) < 2:
        raise SystemExit(
            "Missing required configuration: CALENDARS must list at least 2 "
            "'<account>:<calendar_id>' entries, comma-separated. Set it in .env (see "
            ".env.example) or run `gcal-sync calendars --account <name>` to discover IDs."
        )

    return Config(
        client_secrets_file=os.getenv("GOOGLE_CLIENT_SECRETS_FILE", "data/client_secret.json"),
        token_dir=os.getenv("TOKEN_DIR", "data"),
        db_path=os.getenv("DB_PATH", "data/state.sqlite3"),
        calendars=calendars,
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "120")),
        full_resync_interval_hours=float(os.getenv("FULL_RESYNC_INTERVAL_HOURS", "24")),
        sync_window_days=float(os.getenv("SYNC_WINDOW_DAYS", "14")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        google_web_client_secrets_file=os.getenv(
            "GOOGLE_WEB_CLIENT_SECRETS_FILE", "data/web_client_secret.json"
        ),
        web_base_url=os.getenv("WEB_BASE_URL", ""),
        web_secret_key=os.getenv("WEB_SECRET_KEY", ""),
        token_encryption_key=os.getenv("TOKEN_ENCRYPTION_KEY", ""),
        allowed_domain=os.getenv("ALLOWED_DOMAIN", ""),
    )
