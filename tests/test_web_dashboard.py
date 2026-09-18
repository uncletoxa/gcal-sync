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
    assert b"Enable full copy" in resp.data
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
    account_id = _account_id(db, tenant_id, "a@example.com")

    resp = client.post(
        "/pairs/copy-mode",
        data={
            "source_account_label": "a@example.com", "dest_account_label": "b@example.com",
            "mode": "full", "return_account_id": str(account_id),
        },
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Full copy" in resp.data
    assert b"Disable full copy" in resp.data
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
    account_id = _account_id(db, tenant_id, "a@example.com")

    resp = client.post(
        "/pairs/enabled",
        data={
            "source_account_label": "a@example.com", "dest_account_label": "b@example.com",
            "enabled": "0", "return_account_id": str(account_id),
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
            "enabled": "1", "return_account_id": str(account_id),
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
