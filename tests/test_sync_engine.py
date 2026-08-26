from types import SimpleNamespace

from gcal_sync.db import Database
from gcal_sync.sync_engine import run_sync_pass

WORKSPACE_CAL = "workspace-cal-id"
PERSONAL_CAL = "personal-cal-id"


def _cfg(full_resync_interval_hours: float = 24.0):
    return SimpleNamespace(
        workspace_calendar_id=WORKSPACE_CAL,
        personal_calendar_id=PERSONAL_CAL,
        full_resync_interval_hours=full_resync_interval_hours,
    )


def make_event(event_id, start, end, tz="America/New_York", status="confirmed", **extra):
    event = {
        "id": event_id,
        "status": status,
        "start": {"dateTime": start, "timeZone": tz},
        "end": {"dateTime": end, "timeZone": tz},
        "summary": "Original title - must never be copied",
        "description": "Sensitive details",
    }
    event.update(extra)
    return event


def make_all_day(event_id, start_date, end_date, status="confirmed"):
    return {"id": event_id, "status": status, "start": {"date": start_date}, "end": {"date": end_date}}


def active(events: dict) -> list:
    return [e for e in events.values() if e.get("status") != "cancelled"]


# 1. New Workspace event -> personal mirror created
def test_workspace_event_creates_personal_mirror(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", "2026-09-01T10:00:00-04:00", "2026-09-01T11:00:00-04:00"))

    run_sync_pass(_cfg(), db, fake_client, fake_client)

    personal_events = active(fake_client.events_in(PERSONAL_CAL))
    assert len(personal_events) == 1
    mirror = personal_events[0]
    assert mirror["summary"] == "Busy"
    assert mirror["visibility"] == "private"
    assert mirror["transparency"] == "opaque"
    assert "description" not in mirror
    assert "Original title" not in str(mirror)
    assert mirror["extendedProperties"]["private"]["gcalSyncMirror"] == "true"
    assert "attendees" not in mirror


# 2. New personal event -> workspace mirror created
def test_personal_event_creates_workspace_mirror(db, fake_client):
    fake_client.seed_event(PERSONAL_CAL, make_event("p1", "2026-09-02T09:00:00-04:00", "2026-09-02T09:30:00-04:00"))

    run_sync_pass(_cfg(), db, fake_client, fake_client)

    workspace_events = active(fake_client.events_in(WORKSPACE_CAL))
    assert len(workspace_events) == 1
    assert workspace_events[0]["summary"] == "Busy"


# 3. Source event moved -> mirror moved
def test_source_event_move_updates_mirror(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", "2026-09-01T10:00:00-04:00", "2026-09-01T11:00:00-04:00"))
    run_sync_pass(_cfg(), db, fake_client, fake_client)

    fake_client.patch_event(
        WORKSPACE_CAL,
        "w1",
        {
            "start": {"dateTime": "2026-09-01T13:00:00-04:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-09-01T14:00:00-04:00", "timeZone": "America/New_York"},
        },
    )
    run_sync_pass(_cfg(), db, fake_client, fake_client)

    personal_events = active(fake_client.events_in(PERSONAL_CAL))
    assert len(personal_events) == 1
    assert personal_events[0]["start"]["dateTime"] == "2026-09-01T13:00:00-04:00"
    assert personal_events[0]["end"]["dateTime"] == "2026-09-01T14:00:00-04:00"


# 4. Source event duration changed -> mirror updated
def test_source_event_duration_change_updates_mirror(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", "2026-09-01T10:00:00-04:00", "2026-09-01T11:00:00-04:00"))
    run_sync_pass(_cfg(), db, fake_client, fake_client)

    fake_client.patch_event(
        WORKSPACE_CAL, "w1", {"end": {"dateTime": "2026-09-01T12:00:00-04:00", "timeZone": "America/New_York"}}
    )
    run_sync_pass(_cfg(), db, fake_client, fake_client)

    mirror = active(fake_client.events_in(PERSONAL_CAL))[0]
    assert mirror["end"]["dateTime"] == "2026-09-01T12:00:00-04:00"


# 5. Source event deleted -> mirror deleted
def test_source_event_deleted_removes_mirror(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", "2026-09-01T10:00:00-04:00", "2026-09-01T11:00:00-04:00"))
    run_sync_pass(_cfg(), db, fake_client, fake_client)
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1

    fake_client.cancel_event(WORKSPACE_CAL, "w1")
    run_sync_pass(_cfg(), db, fake_client, fake_client)

    assert active(fake_client.events_in(PERSONAL_CAL)) == []


# 6. Mirror is not mirrored again (loop prevention)
def test_mirror_is_not_remirrored(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", "2026-09-01T10:00:00-04:00", "2026-09-01T11:00:00-04:00"))
    run_sync_pass(_cfg(), db, fake_client, fake_client)
    run_sync_pass(_cfg(), db, fake_client, fake_client)

    assert len(active(fake_client.events_in(WORKSPACE_CAL))) == 1  # original only
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1  # mirror only


# 7. Re-running sync doesn't create duplicates
def test_rerunning_sync_is_idempotent(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", "2026-09-01T10:00:00-04:00", "2026-09-01T11:00:00-04:00"))

    for _ in range(5):
        run_sync_pass(_cfg(), db, fake_client, fake_client)

    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1
    assert len(active(fake_client.events_in(WORKSPACE_CAL))) == 1


# 8. Two overlapping independent source events on both calendars
def test_overlapping_events_both_preserved(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", "2026-09-01T10:00:00-04:00", "2026-09-01T12:00:00-04:00"))
    fake_client.seed_event(PERSONAL_CAL, make_event("p1", "2026-09-01T11:00:00-04:00", "2026-09-01T13:00:00-04:00"))

    run_sync_pass(_cfg(), db, fake_client, fake_client)

    assert len(active(fake_client.events_in(WORKSPACE_CAL))) == 2  # w1 + mirror of p1
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 2  # p1 + mirror of w1


# 9. Recurring events: each instance mirrored/cancelled independently
def test_recurring_event_instances_handled_independently(db, fake_client):
    fake_client.seed_event(
        WORKSPACE_CAL,
        make_event("w1_20260901T140000Z", "2026-09-01T10:00:00-04:00", "2026-09-01T11:00:00-04:00", recurringEventId="w1"),
    )
    fake_client.seed_event(
        WORKSPACE_CAL,
        make_event("w1_20260908T140000Z", "2026-09-08T10:00:00-04:00", "2026-09-08T11:00:00-04:00", recurringEventId="w1"),
    )

    run_sync_pass(_cfg(), db, fake_client, fake_client)
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 2

    fake_client.cancel_event(WORKSPACE_CAL, "w1_20260908T140000Z")
    run_sync_pass(_cfg(), db, fake_client, fake_client)

    remaining = active(fake_client.events_in(PERSONAL_CAL))
    assert len(remaining) == 1


# 10. All-day events handled without timezone conversion
def test_all_day_event_mirror(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_all_day("w1", "2026-09-05", "2026-09-06"))

    run_sync_pass(_cfg(), db, fake_client, fake_client)

    mirror = active(fake_client.events_in(PERSONAL_CAL))[0]
    assert mirror["start"] == {"date": "2026-09-05"}
    assert mirror["end"] == {"date": "2026-09-06"}


# 11. Restarting the service preserves mappings (DB is reopened from the same file)
def test_restart_preserves_mappings(tmp_path, fake_client):
    db_path = str(tmp_path / "state.sqlite3")

    db1 = Database(db_path)
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", "2026-09-01T10:00:00-04:00", "2026-09-01T11:00:00-04:00"))
    run_sync_pass(_cfg(), db1, fake_client, fake_client)
    db1.close()

    db2 = Database(db_path)
    run_sync_pass(_cfg(), db2, fake_client, fake_client)
    db2.close()

    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1


# 12. Invalid/expired sync token triggers a safe full resync, no duplicates
def test_expired_sync_token_triggers_full_resync(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", "2026-09-01T10:00:00-04:00", "2026-09-01T11:00:00-04:00"))
    run_sync_pass(_cfg(), db, fake_client, fake_client)

    token = db.get_sync_token("workspace")
    fake_client.expire_token(WORKSPACE_CAL, token)
    fake_client.seed_event(WORKSPACE_CAL, make_event("w2", "2026-09-02T10:00:00-04:00", "2026-09-02T11:00:00-04:00"))

    run_sync_pass(_cfg(), db, fake_client, fake_client)

    personal_events = active(fake_client.events_in(PERSONAL_CAL))
    assert len(personal_events) == 2  # w1's mirror preserved (not duplicated) + w2's mirror created


# Transparent ("free") source events should not create a Busy mirror.
def test_transparent_event_is_skipped_and_removed_if_it_becomes_transparent(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", "2026-09-01T10:00:00-04:00", "2026-09-01T11:00:00-04:00"))
    run_sync_pass(_cfg(), db, fake_client, fake_client)
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1

    fake_client.patch_event(WORKSPACE_CAL, "w1", {"transparency": "transparent"})
    run_sync_pass(_cfg(), db, fake_client, fake_client)

    assert active(fake_client.events_in(PERSONAL_CAL)) == []


# Dry-run must not write anything
def test_dry_run_makes_no_changes(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", "2026-09-01T10:00:00-04:00", "2026-09-01T11:00:00-04:00"))

    run_sync_pass(_cfg(), db, fake_client, fake_client, dry_run=True)

    assert active(fake_client.events_in(PERSONAL_CAL)) == []
    assert db.get_sync_token("workspace") is None
