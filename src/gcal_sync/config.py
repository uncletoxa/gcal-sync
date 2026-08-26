from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


@dataclass
class Config:
    client_secrets_file: str
    token_dir: str
    db_path: str
    workspace_calendar_id: Optional[str]
    personal_calendar_id: Optional[str]
    poll_interval_seconds: int
    full_resync_interval_hours: float
    log_level: str


def load_config(require_calendars: bool = True) -> Config:
    load_dotenv()

    workspace_calendar_id = os.getenv("WORKSPACE_CALENDAR_ID") or None
    personal_calendar_id = os.getenv("PERSONAL_CALENDAR_ID") or None

    if require_calendars:
        missing = [
            name
            for name, val in (
                ("WORKSPACE_CALENDAR_ID", workspace_calendar_id),
                ("PERSONAL_CALENDAR_ID", personal_calendar_id),
            )
            if not val
        ]
        if missing:
            raise SystemExit(
                f"Missing required configuration: {', '.join(missing)}. "
                "Set them in .env (see .env.example) or run `gcal-sync calendars "
                "--account <workspace|personal>` to discover the correct IDs."
            )

    return Config(
        client_secrets_file=os.getenv("GOOGLE_CLIENT_SECRETS_FILE", "data/client_secret.json"),
        token_dir=os.getenv("TOKEN_DIR", "data"),
        db_path=os.getenv("DB_PATH", "data/state.sqlite3"),
        workspace_calendar_id=workspace_calendar_id,
        personal_calendar_id=personal_calendar_id,
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "120")),
        full_resync_interval_hours=float(os.getenv("FULL_RESYNC_INTERVAL_HOURS", "24")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
