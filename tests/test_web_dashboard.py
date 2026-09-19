from __future__ import annotations

from gcal_sync import web_auth
from gcal_sync.config import Config
from gcal_sync.db import Database
from gcal_sync.web import create_app


def _cfg(db_path) -> Config:
    return Config(
        client_secrets_file="unused",
        token_dir="unused",
        db_path=str(db_path),
        calendars={},
        poll_interval_seconds=1,
        full_resync_interval_hours=24,
        sync_window_days=14,
        log_level="INFO",
        web_secret_key="test-secret",
    )


def _client(tmp_path):
    db = Database(str(tmp_path / "s.sqlite3"))
    app = create_app(cfg=_cfg(tmp_path / "s.sqlite3"), db=db)
    app.testing = True
    return app.test_client(), db


def _sign_in(client, db, email="user@example.com"):
    tenant = db.get_or_create_tenant(email)
    with client.session_transaction() as sess:
        sess["tenant_id"] = tenant["id"]
    return tenant["id"]


def test_sync_button_hidden_with_fewer_than_two_accounts(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    db.upsert_connected_account(
        tenant_id=tenant_id, account_label="a@example.com",
        google_email="a@example.com", calendar_id="a@example.com", credentials_json="cipher",
    )

    resp = client.get("/dashboard")

    assert b"Sync calendars" not in resp.data
    db.close()


def test_sync_button_shown_with_two_accounts(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    for label in ("a@example.com", "b@example.com"):
        db.upsert_connected_account(
            tenant_id=tenant_id, account_label=label,
            google_email=label, calendar_id=label, credentials_json="cipher",
        )

    resp = client.get("/dashboard")

    assert b"Sync calendars" in resp.data
    db.close()


def test_sync_now_requires_two_accounts_and_triggers_sync_tenant(tmp_path, monkeypatch):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    db.upsert_connected_account(
        tenant_id=tenant_id, account_label="solo@example.com",
        google_email="solo@example.com", calendar_id="solo@example.com", credentials_json="cipher",
    )

    calls = []
    monkeypatch.setattr(
        web_auth, "sync_tenant",
        lambda cfg, db, tid, accounts, dry_run=False, force_full=False: calls.append(tid),
    )

    resp = client.post("/sync")
    assert resp.status_code == 302
    assert calls == []  # single account: not enough to sync

    db.upsert_connected_account(
        tenant_id=tenant_id, account_label="second@example.com",
        google_email="second@example.com", calendar_id="second@example.com", credentials_json="cipher",
    )

    resp = client.post("/sync", follow_redirects=True)
    assert calls == [tenant_id]
    assert b"Sync complete." in resp.data
    db.close()


def test_sync_now_requires_login(tmp_path):
    client, db = _client(tmp_path)
    resp = client.post("/sync")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    db.close()


def _account_id(db, tenant_id, label):
    return next(acc["id"] for acc in db.list_connected_accounts(tenant_id) if acc["account_label"] == label)


def test_calendar_detail_shows_pair_toggle_for_two_accounts(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    for label in ("a@example.com", "b@example.com"):
        db.upsert_connected_account(
            tenant_id=tenant_id, account_label=label,
            google_email=label, calendar_id=label, credentials_json="cipher",
        )
    account_id = _account_id(db, tenant_id, "a@example.com")

    resp = client.get(f"/calendars/{account_id}")

    assert b"Busy only" in resp.data
    assert b"Enable custom template" in resp.data
    db.close()


def test_calendar_detail_uses_accessible_labels_and_compact_help(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    for label in ("a@example.com", "b@example.com"):
        db.upsert_connected_account(
            tenant_id=tenant_id, account_label=label,
            google_email=label, calendar_id=label, credentials_json="cipher",
        )
    account_id = _account_id(db, tenant_id, "a@example.com")
    db.set_pair_copy_mode(tenant_id, "b@example.com", "a@example.com", "full")

    resp = client.get(f"/calendars/{account_id}")

    assert b'for="calendar-display-name"' in resp.data
    assert b'id="calendar-display-name"' in resp.data
    assert b'for="sync-window-days"' in resp.data
    assert b'id="sync-window-days"' in resp.data
    assert b'for="title-template-1"' in resp.data
    assert b'id="title-template-1"' in resp.data
    assert b'for="description-template-1"' in resp.data
    assert b'id="description-template-1"' in resp.data
    assert b"<summary>How mirroring works</summary>" in resp.data
    db.close()


def test_calendar_detail_requires_login(tmp_path):
    client, db = _client(tmp_path)
    resp = client.get("/calendars/1")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    db.close()


def test_calendar_detail_404_for_unknown_account(tmp_path):
    client, db = _client(tmp_path)
    _sign_in(client, db)
    resp = client.get("/calendars/999")
    assert resp.status_code == 404
    db.close()


def test_toggle_pair_copy_mode_persists_and_reflects_on_calendar_detail(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    for label in ("a@example.com", "b@example.com"):
        db.upsert_connected_account(
            tenant_id=tenant_id, account_label=label,
            google_email=label, calendar_id=label, credentials_json="cipher",
        )
    # a -> b is only configurable from b's page now — the incoming side.
    dest_account_id = _account_id(db, tenant_id, "b@example.com")

    resp = client.post(
        "/pairs/copy-mode",
        data={
            "source_account_label": "a@example.com", "dest_account_label": "b@example.com",
            "mode": "full", "return_account_id": str(dest_account_id),
        },
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Custom template" in resp.data
    assert b"Disable custom template" in resp.data
    assert db.get_full_copy_pairs(tenant_id) == {("a@example.com", "b@example.com")}
    db.close()


def test_toggle_pair_enabled_disables_and_reenables_pair(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    for label in ("a@example.com", "b@example.com"):
        db.upsert_connected_account(
            tenant_id=tenant_id, account_label=label,
            google_email=label, calendar_id=label, credentials_json="cipher",
        )
    # a -> b is only configurable from b's page now — the incoming side.
    dest_account_id = _account_id(db, tenant_id, "b@example.com")

    resp = client.post(
        "/pairs/enabled",
        data={
            "source_account_label": "a@example.com", "dest_account_label": "b@example.com",
            "enabled": "0", "return_account_id": str(dest_account_id),
        },
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Disabled" in resp.data
    assert db.get_disabled_pairs(tenant_id) == {("a@example.com", "b@example.com")}

    client.post(
        "/pairs/enabled",
        data={
            "source_account_label": "a@example.com", "dest_account_label": "b@example.com",
            "enabled": "1", "return_account_id": str(dest_account_id),
        },
    )
    assert db.get_disabled_pairs(tenant_id) == set()
    db.close()


def test_toggle_pair_enabled_rejects_unknown_account(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    db.upsert_connected_account(
        tenant_id=tenant_id, account_label="a@example.com",
        google_email="a@example.com", calendar_id="a@example.com", credentials_json="cipher",
    )

    resp = client.post(
        "/pairs/enabled",
        data={"source_account_label": "a@example.com", "dest_account_label": "ghost@example.com", "enabled": "0"},
    )

    assert resp.status_code == 400
    assert db.get_disabled_pairs(tenant_id) == set()
    db.close()


def test_toggle_pair_enabled_requires_login(tmp_path):
    client, db = _client(tmp_path)
    resp = client.post(
        "/pairs/enabled",
        data={"source_account_label": "a", "dest_account_label": "b", "enabled": "0"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    db.close()


def test_set_calendar_sync_window_persists_override(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    db.upsert_connected_account(
        tenant_id=tenant_id, account_label="a@example.com",
        google_email="a@example.com", calendar_id="a@example.com", credentials_json="cipher",
    )
    account_id = _account_id(db, tenant_id, "a@example.com")

    resp = client.post(
        f"/calendars/{account_id}/sync-window",
        data={"sync_window_days": "30"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert db.get_connected_account(tenant_id, account_id)["sync_window_days"] == 30.0

    client.post(f"/calendars/{account_id}/sync-window", data={"sync_window_days": ""})
    assert db.get_connected_account(tenant_id, account_id)["sync_window_days"] is None
    db.close()


def test_set_calendar_sync_window_rejects_non_positive(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    db.upsert_connected_account(
        tenant_id=tenant_id, account_label="a@example.com",
        google_email="a@example.com", calendar_id="a@example.com", credentials_json="cipher",
    )
    account_id = _account_id(db, tenant_id, "a@example.com")

    resp = client.post(
        f"/calendars/{account_id}/sync-window",
        data={"sync_window_days": "0"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"must be a positive number" in resp.data
    assert db.get_connected_account(tenant_id, account_id)["sync_window_days"] is None
    db.close()


def test_set_calendar_sync_window_rejects_over_max(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    db.upsert_connected_account(
        tenant_id=tenant_id, account_label="a@example.com",
        google_email="a@example.com", calendar_id="a@example.com", credentials_json="cipher",
    )
    account_id = _account_id(db, tenant_id, "a@example.com")

    resp = client.post(
        f"/calendars/{account_id}/sync-window",
        data={"sync_window_days": "365"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"can&#39;t exceed 56 days" in resp.data
    assert db.get_connected_account(tenant_id, account_id)["sync_window_days"] is None
    db.close()


def test_toggle_pair_copy_mode_rejects_unknown_account(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    db.upsert_connected_account(
        tenant_id=tenant_id, account_label="a@example.com",
        google_email="a@example.com", calendar_id="a@example.com", credentials_json="cipher",
    )

    resp = client.post(
        "/pairs/copy-mode",
        data={"source_account_label": "a@example.com", "dest_account_label": "ghost@example.com", "mode": "full"},
    )

    assert resp.status_code == 400
    assert db.get_full_copy_pairs(tenant_id) == set()
    db.close()


def test_toggle_pair_copy_mode_requires_login(tmp_path):
    client, db = _client(tmp_path)
    resp = client.post(
        "/pairs/copy-mode",
        data={"source_account_label": "a", "dest_account_label": "b", "mode": "full"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    db.close()


def test_calendar_detail_only_shows_incoming(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    for label in ("a@example.com", "b@example.com"):
        db.upsert_connected_account(
            tenant_id=tenant_id, account_label=label,
            google_email=label, calendar_id=label, credentials_json="cipher",
        )
    account_id = _account_id(db, tenant_id, "a@example.com")

    resp = client.get(f"/calendars/{account_id}")

    assert b"Outgoing" not in resp.data
    assert b"Incoming connections" in resp.data
    db.close()


def test_set_calendar_display_name_persists_and_reflects(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    db.upsert_connected_account(
        tenant_id=tenant_id, account_label="a@example.com",
        google_email="a@example.com", calendar_id="a@example.com", credentials_json="cipher",
    )
    account_id = _account_id(db, tenant_id, "a@example.com")

    resp = client.post(
        f"/calendars/{account_id}/display-name",
        data={"display_name": "Personal"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Personal" in resp.data
    assert db.get_connected_account(tenant_id, account_id)["display_name"] == "Personal"
    db.close()


def test_set_calendar_display_name_requires_login(tmp_path):
    client, db = _client(tmp_path)
    resp = client.post("/calendars/1/display-name", data={"display_name": "Personal"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    db.close()


def test_set_calendar_display_name_404_for_unknown_account(tmp_path):
    client, db = _client(tmp_path)
    _sign_in(client, db)
    resp = client.post("/calendars/999/display-name", data={"display_name": "Personal"})
    assert resp.status_code == 404
    db.close()


def test_set_pair_template_persists_and_reflects_on_incoming_side(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    for label in ("a@example.com", "b@example.com"):
        db.upsert_connected_account(
            tenant_id=tenant_id, account_label=label,
            google_email=label, calendar_id=label, credentials_json="cipher",
        )
    # a -> b's template is only editable/visible from b's page — the incoming side.
    dest_account_id = _account_id(db, tenant_id, "b@example.com")
    db.set_pair_copy_mode(tenant_id, "a@example.com", "b@example.com", "full")

    resp = client.post(
        "/pairs/template",
        data={
            "source_account_label": "a@example.com", "dest_account_label": "b@example.com",
            "title_template": "Away: {title}", "description_template": "From {calendar}",
            "return_account_id": str(dest_account_id),
        },
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Away: {title}" in resp.data
    assert db.get_pair_templates(tenant_id) == {
        ("a@example.com", "b@example.com"): ("Away: {title}", "From {calendar}")
    }
    db.close()


def test_set_pair_template_rejects_unknown_account(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    db.upsert_connected_account(
        tenant_id=tenant_id, account_label="a@example.com",
        google_email="a@example.com", calendar_id="a@example.com", credentials_json="cipher",
    )

    resp = client.post(
        "/pairs/template",
        data={
            "source_account_label": "a@example.com", "dest_account_label": "ghost@example.com",
            "title_template": "{title}",
        },
    )

    assert resp.status_code == 400
    assert db.get_pair_templates(tenant_id) == {}
    db.close()


def test_set_pair_template_requires_login(tmp_path):
    client, db = _client(tmp_path)
    resp = client.post(
        "/pairs/template",
        data={"source_account_label": "a", "dest_account_label": "b", "title_template": "{title}"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    db.close()


def test_set_pair_color_persists_and_reflects_on_incoming_side(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    for label in ("a@example.com", "b@example.com"):
        db.upsert_connected_account(
            tenant_id=tenant_id, account_label=label,
            google_email=label, calendar_id=label, credentials_json="cipher",
        )
    # a -> b's color is only editable/visible from b's page — the incoming side.
    dest_account_id = _account_id(db, tenant_id, "b@example.com")

    resp = client.post(
        "/pairs/color",
        data={
            "source_account_label": "a@example.com", "dest_account_label": "b@example.com",
            "color_id": "11", "return_account_id": str(dest_account_id),
        },
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert db.get_pair_colors(tenant_id) == {("a@example.com", "b@example.com"): "11"}

    # Clearing back to "Default" removes the entry entirely.
    client.post(
        "/pairs/color",
        data={
            "source_account_label": "a@example.com", "dest_account_label": "b@example.com",
            "color_id": "", "return_account_id": str(dest_account_id),
        },
    )
    assert db.get_pair_colors(tenant_id) == {}
    db.close()


def test_set_pair_color_rejects_unknown_color_id(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    for label in ("a@example.com", "b@example.com"):
        db.upsert_connected_account(
            tenant_id=tenant_id, account_label=label,
            google_email=label, calendar_id=label, credentials_json="cipher",
        )

    resp = client.post(
        "/pairs/color",
        data={
            "source_account_label": "a@example.com", "dest_account_label": "b@example.com",
            "color_id": "99",
        },
    )

    assert resp.status_code == 400
    assert db.get_pair_colors(tenant_id) == {}
    db.close()


def test_set_pair_color_rejects_unknown_account(tmp_path):
    client, db = _client(tmp_path)
    tenant_id = _sign_in(client, db)
    db.upsert_connected_account(
        tenant_id=tenant_id, account_label="a@example.com",
        google_email="a@example.com", calendar_id="a@example.com", credentials_json="cipher",
    )

    resp = client.post(
        "/pairs/color",
        data={
            "source_account_label": "a@example.com", "dest_account_label": "ghost@example.com",
            "color_id": "11",
        },
    )

    assert resp.status_code == 400
    assert db.get_pair_colors(tenant_id) == {}
    db.close()


def test_set_pair_color_requires_login(tmp_path):
    client, db = _client(tmp_path)
    resp = client.post(
        "/pairs/color",
        data={"source_account_label": "a", "dest_account_label": "b", "color_id": "11"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    db.close()
