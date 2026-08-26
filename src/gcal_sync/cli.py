from __future__ import annotations

import logging
import signal
import time

import click

from .auth import authorize_account, load_credentials
from .config import load_config
from .db import Database
from .errors import AuthenticationError
from .google_client import GoogleCalendarClient
from .logging_config import log_event, setup_logging
from .sync_engine import run_sync_pass

logger = logging.getLogger(__name__)


def _fail(exc: Exception):
    raise click.ClickException(str(exc))


@click.group()
def main():
    """Self-hosted bidirectional Google Calendar availability sync."""


@main.command()
@click.option("--account", type=click.Choice(["workspace", "personal"]), required=True)
@click.option("--no-browser", is_flag=True, help="Print the authorization URL instead of opening a browser.")
def auth(account, no_browser):
    """Authorize a Google account via the loopback (Desktop app) OAuth flow."""
    cfg = load_config(require_calendars=False)
    setup_logging(cfg.log_level)
    try:
        authorize_account(account, cfg.client_secrets_file, cfg.token_dir, no_browser=no_browser)
    except FileNotFoundError as exc:
        _fail(exc)
    click.echo(f"Authorization complete for '{account}'. Token stored in {cfg.token_dir}/token_{account}.json")


@main.command()
@click.option("--account", type=click.Choice(["workspace", "personal"]), required=True)
def calendars(account):
    """List calendars visible to an authorized account, to help find calendar IDs."""
    cfg = load_config(require_calendars=False)
    setup_logging(cfg.log_level)
    try:
        creds = load_credentials(account, cfg.token_dir)
    except AuthenticationError as exc:
        _fail(exc)
    client = GoogleCalendarClient(creds, account)
    for cal in client.list_calendars():
        primary = "PRIMARY" if cal.get("primary") else ""
        click.echo(f"{cal['id']}\t{cal.get('summary', '')}\t{primary}")


def _build_clients(cfg):
    workspace_creds = load_credentials("workspace", cfg.token_dir)
    personal_creds = load_credentials("personal", cfg.token_dir)
    workspace_client = GoogleCalendarClient(workspace_creds, "workspace")
    personal_client = GoogleCalendarClient(personal_creds, "personal")
    return workspace_client, personal_client


@main.command()
@click.option("--dry-run", is_flag=True, help="Do not write any changes; log intended actions only.")
def sync(dry_run):
    """Run a single synchronization pass and exit."""
    cfg = load_config(require_calendars=True)
    setup_logging(cfg.log_level)
    db = Database(cfg.db_path)
    try:
        workspace_client, personal_client = _build_clients(cfg)
        run_sync_pass(cfg, db, workspace_client, personal_client, dry_run=dry_run)
    except AuthenticationError as exc:
        _fail(exc)
    finally:
        db.close()


@main.command()
@click.option("--dry-run", is_flag=True, help="Do not write any changes; log intended actions only.")
def start(dry_run):
    """Run the synchronization service continuously (polling loop)."""
    cfg = load_config(require_calendars=True)
    setup_logging(cfg.log_level)
    db = Database(cfg.db_path)

    shutdown = {"flag": False}

    def _handle_signal(signum, frame):
        shutdown["flag"] = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log_event(logger, "service_started", poll_interval_seconds=cfg.poll_interval_seconds, dry_run=dry_run)
    try:
        while not shutdown["flag"]:
            try:
                workspace_client, personal_client = _build_clients(cfg)
                run_sync_pass(cfg, db, workspace_client, personal_client, dry_run=dry_run)
            except Exception:
                logger.exception("sync_pass_failed")

            for _ in range(cfg.poll_interval_seconds):
                if shutdown["flag"]:
                    break
                time.sleep(1)
    finally:
        db.close()
        log_event(logger, "service_stopped")


if __name__ == "__main__":
    main()
