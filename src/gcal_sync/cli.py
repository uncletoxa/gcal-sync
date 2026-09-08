from __future__ import annotations

import logging
import signal
import time

import click

from . import web_auth
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
@click.option("--account", required=True, help="Account key, e.g. 'workspace', 'personal', 'team' (must match a key used in CALENDARS).")
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
@click.option("--account", required=True, help="Account key previously authorized with `gcal-sync auth`.")
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
    return {
        account: GoogleCalendarClient(load_credentials(account, cfg.token_dir), account)
        for account in cfg.calendars
    }


def _run_tenant_sync_passes(cfg, db, dry_run: bool) -> int:
    """Run one sync pass per web-signed-up tenant with >=2 connected accounts.

    Each tenant's accounts are namespaced (web_auth.tenant_account_key) so
    sync_engine.py's full-mesh logic only ever sees one tenant's own calendars at a
    time — this is the entire multi-tenant isolation mechanism, no changes to
    sync_engine.py itself. One tenant's failure (e.g. an expired grant) is isolated
    and never blocks other tenants' passes. Returns how many tenant passes ran.
    """
    ran = 0
    for tenant, accounts in db.list_tenants_with_accounts():
        if len(accounts) < 2:
            continue
        ran += 1
        try:
            web_auth.sync_tenant(cfg, db, tenant["id"], accounts, dry_run=dry_run)
        except Exception:
            logger.exception("tenant_sync_pass_failed", extra={"tenant_id": tenant["id"]})
    return ran


@main.command()
@click.option("--dry-run", is_flag=True, help="Do not write any changes; log intended actions only.")
def sync(dry_run):
    """Run a single synchronization pass (legacy CALENDARS + all web-connected tenants) and exit."""
    cfg = load_config(require_calendars=False)
    setup_logging(cfg.log_level)
    db = Database(cfg.db_path)
    try:
        legacy_ready = len(cfg.calendars) >= 2
        if legacy_ready:
            clients = _build_clients(cfg)
            run_sync_pass(cfg, db, clients, dry_run=dry_run)
        tenants_ran = _run_tenant_sync_passes(cfg, db, dry_run)
        if not legacy_ready and tenants_ran == 0:
            _fail(RuntimeError(
                "Nothing to sync: CALENDARS must list at least 2 '<account>:<calendar_id>' "
                "entries, or at least one web-signed-up user must have 2+ calendars connected "
                "via `gcal-sync web`."
            ))
    except AuthenticationError as exc:
        _fail(exc)
    finally:
        db.close()


@main.command()
@click.option("--dry-run", is_flag=True, help="Do not write any changes; log intended actions only.")
def start(dry_run):
    """Run the synchronization service continuously (polling loop)."""
    cfg = load_config(require_calendars=False)
    setup_logging(cfg.log_level)
    db = Database(cfg.db_path)
    legacy_ready = len(cfg.calendars) >= 2

    shutdown = {"flag": False}

    def _handle_signal(signum, frame):
        shutdown["flag"] = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log_event(logger, "service_started", poll_interval_seconds=cfg.poll_interval_seconds, dry_run=dry_run)
    try:
        while not shutdown["flag"]:
            if legacy_ready:
                try:
                    clients = _build_clients(cfg)
                    run_sync_pass(cfg, db, clients, dry_run=dry_run)
                except Exception:
                    logger.exception("sync_pass_failed")

            try:
                _run_tenant_sync_passes(cfg, db, dry_run)
            except Exception:
                logger.exception("tenant_sync_loop_failed")

            for _ in range(cfg.poll_interval_seconds):
                if shutdown["flag"]:
                    break
                time.sleep(1)
    finally:
        db.close()
        log_event(logger, "service_stopped")


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address for the dev server.")
@click.option("--port", default=8000, show_default=True, type=int, help="Bind port for the dev server.")
@click.option("--debug", is_flag=True, help="Enable Flask debug/auto-reload (development only).")
def web(host, port, debug):
    """Run the web sign-up app (development server — use gunicorn in production, see README)."""
    from .web import create_app

    app = create_app()
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
