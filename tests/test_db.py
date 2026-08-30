from gcal_sync.db import Database


def test_upsert_mapping_insert_update_and_delete(tmp_path):
    db = Database(str(tmp_path / "s.sqlite3"))

    db.upsert_mapping(
        source_account="workspace",
        source_calendar_id="wc",
        source_event_id="e1",
        dest_account="personal",
        dest_calendar_id="pc",
        dest_event_id="m1",
        source_signature="sig1",
        status="active",
    )
    row = db.get_mapping("workspace", "wc", "e1", "personal", "pc")
    assert row["dest_event_id"] == "m1"
    assert row["status"] == "active"

    db.upsert_mapping(
        source_account="workspace",
        source_calendar_id="wc",
        source_event_id="e1",
        dest_account="personal",
        dest_calendar_id="pc",
        dest_event_id="m1",
        source_signature="sig2",
        status="active",
    )
    row2 = db.get_mapping("workspace", "wc", "e1", "personal", "pc")
    assert row2["source_signature"] == "sig2"
    assert row2["id"] == row["id"]  # same mapping row, upserted not duplicated

    db.mark_deleted(row2["id"])
    row3 = db.get_mapping("workspace", "wc", "e1", "personal", "pc")
    assert row3["status"] == "deleted"
    assert row3["dest_event_id"] is None

    db.close()


def test_sync_token_and_full_sync_timestamp_roundtrip(tmp_path):
    db = Database(str(tmp_path / "s.sqlite3"))

    assert db.get_sync_token("workspace") is None
    db.set_sync_token("workspace", "token-1")
    assert db.get_sync_token("workspace") == "token-1"

    assert db.get_last_full_sync("workspace") is None
    db.set_last_full_sync("workspace", "2026-01-01T00:00:00+00:00")
    assert db.get_last_full_sync("workspace") == "2026-01-01T00:00:00+00:00"

    # setting sync token again must not clobber last_full_sync_at
    db.set_sync_token("workspace", "token-2")
    assert db.get_last_full_sync("workspace") == "2026-01-01T00:00:00+00:00"

    db.close()


def test_list_active_mappings_for_pair(tmp_path):
    db = Database(str(tmp_path / "s.sqlite3"))
    db.upsert_mapping(
        source_account="workspace", source_calendar_id="wc", source_event_id="e1",
        dest_account="personal", dest_calendar_id="pc", dest_event_id="m1",
        source_signature="sig1", status="active",
    )
    db.upsert_mapping(
        source_account="workspace", source_calendar_id="wc", source_event_id="e2",
        dest_account="personal", dest_calendar_id="pc", dest_event_id="m2",
        source_signature="sig1", status="active",
    )

    rows = db.list_active_mappings_for_pair("workspace", "wc", "personal", "pc")
    assert {r["source_event_id"] for r in rows} == {"e1", "e2"}

    db.mark_deleted(rows[0]["id"])
    rows2 = db.list_active_mappings_for_pair("workspace", "wc", "personal", "pc")
    assert len(rows2) == 1

    db.close()


def test_get_or_create_tenant_is_idempotent(tmp_path):
    db = Database(str(tmp_path / "s.sqlite3"))

    t1 = db.get_or_create_tenant("alice@example.com")
    t2 = db.get_or_create_tenant("alice@example.com")
    assert t1["id"] == t2["id"]
    assert db.get_tenant(t1["id"])["email"] == "alice@example.com"
    assert db.get_tenant(9999) is None

    db.close()


def test_connected_account_upsert_and_list(tmp_path):
    db = Database(str(tmp_path / "s.sqlite3"))
    tenant = db.get_or_create_tenant("alice@example.com")

    db.upsert_connected_account(
        tenant_id=tenant["id"], account_label="alice@example.com",
        google_email="alice@example.com", calendar_id="alice@example.com",
        credentials_json="cipher-v1",
    )
    db.upsert_connected_account(
        tenant_id=tenant["id"], account_label="alice@work.com",
        google_email="alice@work.com", calendar_id="alice@work.com",
        credentials_json="cipher-v1",
    )
    accounts = db.list_connected_accounts(tenant["id"])
    assert {a["account_label"] for a in accounts} == {"alice@example.com", "alice@work.com"}

    # reconnecting the same label updates in place rather than duplicating
    db.upsert_connected_account(
        tenant_id=tenant["id"], account_label="alice@example.com",
        google_email="alice@example.com", calendar_id="alice@example.com",
        credentials_json="cipher-v2",
    )
    accounts = db.list_connected_accounts(tenant["id"])
    assert len(accounts) == 2
    updated = next(a for a in accounts if a["account_label"] == "alice@example.com")
    assert updated["credentials_json"] == "cipher-v2"

    db.delete_connected_account(tenant["id"], updated["id"])
    assert len(db.list_connected_accounts(tenant["id"])) == 1

    db.close()


def test_list_tenants_with_accounts_and_isolation(tmp_path):
    """Two tenants both using the account label 'personal' must never share sync state."""
    db = Database(str(tmp_path / "s.sqlite3"))
    t1 = db.get_or_create_tenant("t1@example.com")
    t2 = db.get_or_create_tenant("t2@example.com")
    for t in (t1, t2):
        db.upsert_connected_account(
            tenant_id=t["id"], account_label="personal",
            google_email="x@example.com", calendar_id="cal",
            credentials_json="cipher",
        )

    pairs = {t["id"]: accts for t, accts in db.list_tenants_with_accounts()}
    assert len(pairs[t1["id"]]) == 1
    assert len(pairs[t2["id"]]) == 1

    # The namespaced key format (see web_auth.tenant_account_key) is what actually
    # keeps these isolated in event_mappings/sync_state, not the raw label.
    key1, key2 = f"t{t1['id']}:personal", f"t{t2['id']}:personal"
    assert key1 != key2
    db.set_sync_token(key1, "token-for-tenant-1")
    assert db.get_sync_token(key2) is None
    assert db.get_sync_token(key1) == "token-for-tenant-1"

    db.close()
