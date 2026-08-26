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
