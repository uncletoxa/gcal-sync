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
            contact_email=cfg.contact_email or None,
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

    @app.get("/calendars/<int:account_id>")
    def calendar_detail(account_id: int):
        tenant_id = session.get("tenant_id")
        if not tenant_id:
            return redirect(url_for("index"))
        account = db.get_connected_account(tenant_id, account_id)
        if account is None:
            abort(404)
        accounts = db.list_connected_accounts(tenant_id)
        full_copy_pairs = db.get_full_copy_pairs(tenant_id)
        disabled_pairs = db.get_disabled_pairs(tenant_id)
        pair_templates = db.get_pair_templates(tenant_id)
        other_accounts = [acc for acc in accounts if acc["id"] != account_id]
        incoming = [
            {
                "label": other["account_label"],
                "email": other["google_email"],
                "full_copy": (other["account_label"], account["account_label"]) in full_copy_pairs,
                "enabled": (other["account_label"], account["account_label"]) not in disabled_pairs,
                "title_template": pair_templates.get(
                    (other["account_label"], account["account_label"]), (None, None)
                )[0] or "",
                "description_template": pair_templates.get(
                    (other["account_label"], account["account_label"]), (None, None)
                )[1] or "",
            }
            for other in other_accounts
        ]
        return render_template(
            "calendar.html",
            account={
                "id": account["id"],
                "label": account["account_label"],
                "email": account["google_email"],
                "display_name": account["display_name"] or "",
                "last_synced": db.get_last_full_sync(
                    web_auth.tenant_account_key(tenant_id, account["account_label"])
                ),
                "sync_window_days": account["sync_window_days"],
            },
            default_sync_window_days=cfg.sync_window_days,
            incoming=incoming,
            sync_window_error=session.pop("sync_window_error", None),
            display_name_error=session.pop("display_name_error", None),
        )

    @app.post("/calendars/<int:account_id>/sync-window")
    def set_calendar_sync_window(account_id: int):
        tenant_id = session.get("tenant_id")
        if not tenant_id:
            return redirect(url_for("index"))
        if db.get_connected_account(tenant_id, account_id) is None:
            abort(404)
        raw = request.form.get("sync_window_days", "").strip()
        if not raw:
            db.set_account_sync_window(tenant_id, account_id, None)
        else:
            try:
                days = float(raw)
            except ValueError:
                days = None
            if days is None or days <= 0:
                session["sync_window_error"] = "Sync window must be a positive number of days."
                return redirect(url_for("calendar_detail", account_id=account_id))
            db.set_account_sync_window(tenant_id, account_id, days)
        log_event(logger, "web_calendar_sync_window_changed", tenant_id=tenant_id, account_id=account_id)
        return redirect(url_for("calendar_detail", account_id=account_id))

    @app.post("/calendars/<int:account_id>/display-name")
    def set_calendar_display_name(account_id: int):
        tenant_id = session.get("tenant_id")
        if not tenant_id:
            return redirect(url_for("index"))
        if db.get_connected_account(tenant_id, account_id) is None:
            abort(404)
        display_name = request.form.get("display_name", "").strip()[:100] or None
        db.set_account_display_name(tenant_id, account_id, display_name)
        log_event(logger, "web_calendar_display_name_changed", tenant_id=tenant_id, account_id=account_id)
        return redirect(url_for("calendar_detail", account_id=account_id))

    def _redirect_after_pair_change():
        raw_account_id = request.form.get("return_account_id", "")
        if raw_account_id.isdigit():
            return redirect(url_for("calendar_detail", account_id=int(raw_account_id)))
        return redirect(url_for("dashboard"))

    @app.post("/sync")
    def sync_now():
        tenant_id = session.get("tenant_id")
        if not tenant_id:
            return redirect(url_for("index"))
        accounts = db.list_connected_accounts(tenant_id)
        if len(accounts) < 2:
            return redirect(url_for("dashboard"))
        try:
            web_auth.sync_tenant(cfg, db, tenant_id, accounts, force_full=True)
        except AuthenticationError as exc:
            session["sync_error"] = str(exc)
        except Exception:
            logger.exception("web_sync_failed", extra={"tenant_id": tenant_id})
            session["sync_error"] = "Sync failed — please try again in a moment."
        else:
            session["sync_success"] = "Sync complete."
            log_event(logger, "web_sync_triggered", tenant_id=tenant_id)
        return redirect(url_for("dashboard"))

    @app.post("/pairs/copy-mode")
    def toggle_pair_copy_mode():
        tenant_id = session.get("tenant_id")
        if not tenant_id:
            return redirect(url_for("index"))
        source_label = request.form.get("source_account_label", "")
        dest_label = request.form.get("dest_account_label", "")
        mode = request.form.get("mode", "")
        valid_labels = {acc["account_label"] for acc in db.list_connected_accounts(tenant_id)}
        if source_label not in valid_labels or dest_label not in valid_labels or mode not in ("full", "busy_only"):
            abort(400)
        db.set_pair_copy_mode(tenant_id, source_label, dest_label, mode)
        log_event(
            logger, "web_pair_copy_mode_changed",
            tenant_id=tenant_id, source=source_label, dest=dest_label, mode=mode,
        )
        return _redirect_after_pair_change()

    @app.post("/pairs/template")
    def set_pair_template():
        tenant_id = session.get("tenant_id")
        if not tenant_id:
            return redirect(url_for("index"))
        source_label = request.form.get("source_account_label", "")
        dest_label = request.form.get("dest_account_label", "")
        valid_labels = {acc["account_label"] for acc in db.list_connected_accounts(tenant_id)}
        if source_label not in valid_labels or dest_label not in valid_labels:
            abort(400)
        title_template = request.form.get("title_template", "").strip()[:500] or None
        description_template = request.form.get("description_template", "").strip()[:500] or None
        db.set_pair_templates(tenant_id, source_label, dest_label, title_template, description_template)
        log_event(
            logger, "web_pair_template_changed",
            tenant_id=tenant_id, source=source_label, dest=dest_label,
        )
        return _redirect_after_pair_change()

    @app.post("/pairs/enabled")
    def toggle_pair_enabled():
        tenant_id = session.get("tenant_id")
        if not tenant_id:
            return redirect(url_for("index"))
        source_label = request.form.get("source_account_label", "")
        dest_label = request.form.get("dest_account_label", "")
        enabled = request.form.get("enabled", "")
        valid_labels = {acc["account_label"] for acc in db.list_connected_accounts(tenant_id)}
        if source_label not in valid_labels or dest_label not in valid_labels or enabled not in ("1", "0"):
            abort(400)
        db.set_pair_enabled(tenant_id, source_label, dest_label, enabled == "1")
        log_event(
            logger, "web_pair_enabled_changed",
            tenant_id=tenant_id, source=source_label, dest=dest_label, enabled=enabled,
        )
        return _redirect_after_pair_change()

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
