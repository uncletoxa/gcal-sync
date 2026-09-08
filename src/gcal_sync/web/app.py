from __future__ import annotations

import logging

from flask import Flask, abort, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import web_auth
from ..config import Config, load_config
from ..db import Database
from ..errors import AuthenticationError
from ..logging_config import log_event, setup_logging

logger = logging.getLogger(__name__)


def create_app(cfg: Config | None = None, db: Database | None = None) -> Flask:
    cfg = cfg or load_config(require_calendars=False)
    setup_logging(cfg.log_level)

    if not cfg.web_secret_key:
        raise RuntimeError(
            "WEB_SECRET_KEY is not set. Generate one with "
            "`python -c \"import secrets; print(secrets.token_hex(32))\"` and set it in .env."
        )

    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    app.secret_key = cfg.web_secret_key

    db = db or Database(cfg.db_path)

    @app.get("/")
    def index():
        if session.get("tenant_id"):
            return redirect(url_for("dashboard"))
        return render_template("index.html", allowed_domain=cfg.allowed_domain or None)

    @app.get("/login")
    def login():
        restrict_domain = not session.get("tenant_id")
        try:
            url, state, code_verifier = web_auth.get_authorization_url(cfg, restrict_domain=restrict_domain)
        except FileNotFoundError as exc:
            abort(500, str(exc))
        session["oauth_state"] = state
        session["code_verifier"] = code_verifier
        return redirect(url)

    @app.get("/privacy")
    def privacy():
        return render_template(
            "privacy.html",
            operator_name=cfg.operator_name or None,
            instance_url=cfg.web_base_url or None,
            allowed_domain=cfg.allowed_domain or None,
        )

    @app.get("/oauth/callback")
    def oauth_callback():
        expected_state = session.pop("oauth_state", None)
        code_verifier = session.pop("code_verifier", None)
        got_state = request.args.get("state")
        if not expected_state or expected_state != got_state:
            abort(400, "OAuth state mismatch — please try signing in again.")
        if request.args.get("error"):
            return render_template("index.html", error="Google sign-in was cancelled or denied.")

        try:
            credentials = web_auth.complete_authorization(cfg, expected_state, request.url, code_verifier)
        except Exception:
            logger.exception("oauth_callback_failed")
            abort(400, "Could not complete Google sign-in — please try again.")

        existing_tenant_id = session.get("tenant_id")
        try:
            if existing_tenant_id:
                tenant_id = existing_tenant_id
                google_email = web_auth.save_connected_account(
                    db, cfg, tenant_id=tenant_id, credentials=credentials
                )
            else:
                google_email = web_auth.primary_calendar_email(credentials)
                web_auth.check_domain_allowed(google_email, cfg)
                tenant = db.get_or_create_tenant(google_email)
                tenant_id = tenant["id"]
                web_auth.save_connected_account(db, cfg, tenant_id=tenant_id, credentials=credentials)
        except AuthenticationError as exc:
            abort(400, str(exc))

        session["tenant_id"] = tenant_id
        log_event(logger, "web_account_connected", tenant_id=tenant_id, account=google_email)
        return redirect(url_for("dashboard"))

    @app.get("/dashboard")
    def dashboard():
        tenant_id = session.get("tenant_id")
        if not tenant_id:
            return redirect(url_for("index"))
        tenant = db.get_tenant(tenant_id)
        if tenant is None:
            session.clear()
            return redirect(url_for("index"))
        accounts = db.list_connected_accounts(tenant_id)
        rows = [
            {
                "id": acc["id"],
                "email": acc["google_email"],
                "last_synced": db.get_last_full_sync(
                    web_auth.tenant_account_key(tenant_id, acc["account_label"])
                ),
            }
            for acc in accounts
        ]
        return render_template(
            "dashboard.html",
            tenant_email=tenant["email"],
            accounts=rows,
            needs_second_account=len(rows) < 2,
            sync_error=session.pop("sync_error", None),
            sync_success=session.pop("sync_success", None),
        )

    @app.post("/sync")
    def sync_now():
        tenant_id = session.get("tenant_id")
        if not tenant_id:
            return redirect(url_for("index"))
        accounts = db.list_connected_accounts(tenant_id)
        if len(accounts) < 2:
            return redirect(url_for("dashboard"))
        try:
            web_auth.sync_tenant(cfg, db, tenant_id, accounts)
        except AuthenticationError as exc:
            session["sync_error"] = str(exc)
        except Exception:
            logger.exception("web_sync_failed", extra={"tenant_id": tenant_id})
            session["sync_error"] = "Sync failed — please try again in a moment."
        else:
            session["sync_success"] = "Sync complete."
            log_event(logger, "web_sync_triggered", tenant_id=tenant_id)
        return redirect(url_for("dashboard"))

    @app.post("/accounts/<int:account_id>/disconnect")
    def disconnect(account_id: int):
        tenant_id = session.get("tenant_id")
        if not tenant_id:
            return redirect(url_for("index"))
        db.delete_connected_account(tenant_id, account_id)
        log_event(logger, "web_account_disconnected", tenant_id=tenant_id, account_id=account_id)
        return redirect(url_for("dashboard"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    return app
