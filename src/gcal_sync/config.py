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
    # Who operates this specific instance (e.g. "Acme Corp") — shown on the /privacy page to
    # disclose that this deployment, unlike a self-run instance, stores account/token data
    # on the operator's infrastructure. Leave unset for a personal, self-hosted deployment.
    operator_name: str = ""
    # Contact address shown on the /privacy page for account/data questions specific to this
    # instance. Left unset, the page falls back to directing everything to GitHub Issues.
    contact_email: str = ""
    # Directed (source, dest) account-name pairs opted into mirroring full event data
    # (title, description, location) instead of just a "Busy" placeholder. Every pair not
    # listed here keeps the default busy-only behavior.
    full_copy_pairs: frozenset[tuple[str, str]] = frozenset()


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


def _parse_pairs(raw: str, calendars: dict[str, str]) -> frozenset[tuple[str, str]]:
    """Parse 'source:dest,source:dest,...' into a set of directed account-name pairs.

    Both names in each entry must be keys of `calendars` — this runs after
    `_parse_calendars` so unknown or self-paired account names are caught early
    rather than silently never matching in `sync_all_pairs`.
    """
    pairs: set[tuple[str, str]] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise SystemExit(
                f"Invalid FULL_COPY_PAIRS entry '{entry}': expected '<source>:<dest>' "
                "(see .env.example)."
            )
        source, dest = (part.strip() for part in entry.split(":", 1))
        if not source or not dest:
            raise SystemExit(f"Invalid FULL_COPY_PAIRS entry '{entry}': source and dest must be non-empty.")
        if source == dest:
            raise SystemExit(f"Invalid FULL_COPY_PAIRS entry '{entry}': source and dest must differ.")
        for name in (source, dest):
            if name not in calendars:
                raise SystemExit(
                    f"Invalid FULL_COPY_PAIRS entry '{entry}': '{name}' is not a configured "
                    "CALENDARS account name."
                )
        pairs.add((source, dest))
    return frozenset(pairs)


def load_config(require_calendars: bool = True) -> Config:
    load_dotenv()

    calendars = _parse_calendars(os.getenv("CALENDARS", ""))

    if require_calendars and len(calendars) < 2:
        raise SystemExit(
            "Missing required configuration: CALENDARS must list at least 2 "
            "'<account>:<calendar_id>' entries, comma-separated. Set it in .env (see "
            ".env.example) or run `gcal-sync calendars --account <name>` to discover IDs."
        )

    full_copy_pairs = _parse_pairs(os.getenv("FULL_COPY_PAIRS", ""), calendars)

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
        operator_name=os.getenv("OPERATOR_NAME", ""),
        contact_email=os.getenv("CONTACT_EMAIL", ""),
        full_copy_pairs=full_copy_pairs,
    )
