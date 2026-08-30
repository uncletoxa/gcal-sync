from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from . import crypto
from .auth import SCOPES
from .config import Config
from .db import Database
from .errors import AuthenticationError
from .google_client import GoogleCalendarClient
from .logging_config import log_event

logger = logging.getLogger(__name__)


def _redirect_uri(cfg: Config) -> str:
    if not cfg.web_base_url:
        raise RuntimeError("WEB_BASE_URL must be set to run the web app.")
    return f"{cfg.web_base_url.rstrip('/')}/oauth/callback"


def build_flow(cfg: Config, state: Optional[str] = None) -> Flow:
    """Build the Web-application OAuth flow (fixed redirect URI, not the CLI's loopback flow)."""
    if not Path(cfg.google_web_client_secrets_file).exists():
        raise FileNotFoundError(
            f"Web OAuth client secrets file not found at '{cfg.google_web_client_secrets_file}'. "
            "Create a 'Web application' OAuth client in Cloud Console (separate from the "
            "Desktop client used by `gcal-sync auth`) and point GOOGLE_WEB_CLIENT_SECRETS_FILE "
            "at the downloaded JSON."
        )
    return Flow.from_client_secrets_file(
        cfg.google_web_client_secrets_file,
        scopes=SCOPES,
        state=state,
        redirect_uri=_redirect_uri(cfg),
    )


def get_authorization_url(cfg: Config) -> tuple[str, str]:
    """Start a new OAuth grant. Returns (url_to_redirect_user_to, state_to_store_in_session)."""
    flow = build_flow(cfg)
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return url, state


def complete_authorization(cfg: Config, state: str, authorization_response_url: str) -> Credentials:
    """Exchange the callback's ?code= for credentials. Caller must verify `state` against the session first."""
    flow = build_flow(cfg, state=state)
    flow.fetch_token(authorization_response=authorization_response_url)
    return flow.credentials


def primary_calendar_email(credentials: Credentials) -> str:
    """The connecting account's own email, taken from its primary calendar ID."""
    client = GoogleCalendarClient(credentials, "web-signin")
    for cal in client.list_calendars():
        if cal.get("primary"):
            return cal["id"]
    raise AuthenticationError("Could not determine the primary calendar for this Google account.")


def tenant_account_key(tenant_id: int, account_label: str) -> str:
    """The namespaced account key used as event_mappings/sync_state's account column.

    This is the whole isolation mechanism for multi-tenant sync: as long as every
    tenant's accounts are namespaced by tenant id, sync_engine.py (which has no concept
    of tenants) can never mix one tenant's mirrors into another's.
    """
    return f"t{tenant_id}:{account_label}"


def save_connected_account(db: Database, cfg: Config, *, tenant_id: int, credentials: Credentials) -> str:
    """Persist a newly-authorized account under an existing tenant. Returns the account's Google email."""
    google_email = primary_calendar_email(credentials)
    encrypted = crypto.encrypt(credentials.to_json(), cfg.token_encryption_key)
    db.upsert_connected_account(
        tenant_id=tenant_id,
        account_label=google_email,
        google_email=google_email,
        calendar_id=google_email,  # primary calendar's ID is the account email
        credentials_json=encrypted,
    )
    return google_email


def load_account_credentials(db: Database, cfg: Config, account_row) -> Credentials:
    """Decrypt + refresh-if-needed credentials for one connected_accounts row, persisting refreshes back to the DB."""
    plaintext = crypto.decrypt(account_row["credentials_json"], cfg.token_encryption_key)
    creds = Credentials.from_authorized_user_info(json.loads(plaintext), SCOPES)

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            log_event(
                logger, "authentication_error", level="error",
                tenant_id=account_row["tenant_id"], account_label=account_row["account_label"],
            )
            raise AuthenticationError(
                f"Failed to refresh credentials for tenant {account_row['tenant_id']} "
                f"account '{account_row['account_label']}'. They need to reconnect via the web app."
            ) from exc
        db.update_connected_account_credentials(
            account_row["id"], crypto.encrypt(creds.to_json(), cfg.token_encryption_key)
        )
        log_event(
            logger, "token_refreshed",
            tenant_id=account_row["tenant_id"], account_label=account_row["account_label"],
        )

    if not creds.valid:
        raise AuthenticationError(
            f"Stored credentials for tenant {account_row['tenant_id']} "
            f"account '{account_row['account_label']}' are invalid."
        )

    return creds
