from __future__ import annotations

import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .errors import AuthenticationError
from .logging_config import log_event

logger = logging.getLogger(__name__)

# Minimal scopes: manage events (create/read/update/delete) and list calendars.
# Deliberately not requesting the broad `calendar` scope.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]


def _token_path(token_dir: str, account_key: str) -> Path:
    return Path(token_dir) / f"token_{account_key}.json"


def _secure_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(path, 0o600)


def authorize_account(
    account_key: str, client_secrets_file: str, token_dir: str, no_browser: bool = False
) -> Credentials:
    """Run the loopback (Desktop app) OAuth flow and persist the resulting credentials."""
    if not Path(client_secrets_file).exists():
        raise FileNotFoundError(
            f"OAuth client secrets file not found at '{client_secrets_file}'. "
            "Download a Desktop app OAuth client JSON from Google Cloud Console "
            "and point GOOGLE_CLIENT_SECRETS_FILE at it."
        )
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=not no_browser, prompt="consent")
    _secure_write(_token_path(token_dir, account_key), creds.to_json())
    log_event(logger, "authorization_completed", account=account_key)
    return creds


def load_credentials(account_key: str, token_dir: str) -> Credentials:
    """Load stored credentials, refreshing the access token automatically if expired."""
    path = _token_path(token_dir, account_key)
    if not path.exists():
        raise AuthenticationError(
            f"No stored credentials for account '{account_key}'. "
            f"Run `gcal-sync auth --account {account_key}` first."
        )

    creds = Credentials.from_authorized_user_file(str(path), SCOPES)

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            log_event(logger, "authentication_error", level="error", account=account_key)
            raise AuthenticationError(
                f"Failed to refresh credentials for '{account_key}'. "
                f"Re-run `gcal-sync auth --account {account_key}`."
            ) from exc
        _secure_write(path, creds.to_json())
        log_event(logger, "token_refreshed", account=account_key)

    if not creds.valid:
        raise AuthenticationError(
            f"Stored credentials for '{account_key}' are invalid. "
            f"Re-run `gcal-sync auth --account {account_key}`."
        )

    return creds
