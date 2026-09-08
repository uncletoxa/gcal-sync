from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from gcal_sync.db import Database
from gcal_sync.sync_engine import run_sync_pass

WORKSPACE_CAL = "workspace-cal-id"
PERSONAL_CAL = "personal-cal-id"


def _cfg(full_resync_interval_hours: float = 24.0, sync_window_days: float = 14.0, calendars=None):
    return SimpleNamespace(
        calendars=calendars or {"workspace": WORKSPACE_CAL, "personal": PERSONAL_CAL},
        full_resync_interval_hours=full_resync_interval_hours,
        sync_window_days=sync_window_days,
    )


def _in_days(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _dt(day_offset: int, hour: int, minute: int = 0) -> str:
    """An event dateTime `day_offset` days from now, at a fixed wall-clock hour.

    Sync only mirrors events inside [now, now + sync_window_days) (see
    sync_engine._sync_window), so tests must anchor to the current date rather
    than a hardcoded one — a hardcoded date silently falls out of the window
    once enough real time has passed.
    """
    d = (datetime.now(timezone.utc) + timedelta(days=day_offset)).date()
    return f"{d.isoformat()}T{hour:02d}:{minute:02d}:00-04:00"


def _date(day_offset: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=day_offset)).date().isoformat()


def _clients(fake_client, *accounts):
    accounts = accounts or ("workspace", "personal")
    return {account: fake_client for account in accounts}


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
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))

    run_sync_pass(_cfg(), db, _clients(fake_client))

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
    fake_client.seed_event(PERSONAL_CAL, make_event("p1", _dt(1, 9), _dt(1, 9, 30)))

    run_sync_pass(_cfg(), db, _clients(fake_client))

    workspace_events = active(fake_client.events_in(WORKSPACE_CAL))
    assert len(workspace_events) == 1
    assert workspace_events[0]["summary"] == "Busy"


# 3. Source event moved -> mirror moved
def test_source_event_move_updates_mirror(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))
    run_sync_pass(_cfg(), db, _clients(fake_client))

    fake_client.patch_event(
        WORKSPACE_CAL,
        "w1",
        {
            "start": {"dateTime": _dt(1, 13), "timeZone": "America/New_York"},
            "end": {"dateTime": _dt(1, 14), "timeZone": "America/New_York"},
        },
    )
    run_sync_pass(_cfg(), db, _clients(fake_client))

    personal_events = active(fake_client.events_in(PERSONAL_CAL))
    assert len(personal_events) == 1
    assert personal_events[0]["start"]["dateTime"] == _dt(1, 13)
    assert personal_events[0]["end"]["dateTime"] == _dt(1, 14)


# 4. Source event duration changed -> mirror updated
def test_source_event_duration_change_updates_mirror(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))
    run_sync_pass(_cfg(), db, _clients(fake_client))

    fake_client.patch_event(
        WORKSPACE_CAL, "w1", {"end": {"dateTime": _dt(1, 12), "timeZone": "America/New_York"}}
    )
    run_sync_pass(_cfg(), db, _clients(fake_client))

    mirror = active(fake_client.events_in(PERSONAL_CAL))[0]
    assert mirror["end"]["dateTime"] == _dt(1, 12)


# 5. Source event deleted -> mirror deleted
def test_source_event_deleted_removes_mirror(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))
    run_sync_pass(_cfg(), db, _clients(fake_client))
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1

    fake_client.cancel_event(WORKSPACE_CAL, "w1")
    run_sync_pass(_cfg(), db, _clients(fake_client))

    assert active(fake_client.events_in(PERSONAL_CAL)) == []


# 6. Mirror is not mirrored again (loop prevention)
def test_mirror_is_not_remirrored(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))
    run_sync_pass(_cfg(), db, _clients(fake_client))
    run_sync_pass(_cfg(), db, _clients(fake_client))

    assert len(active(fake_client.events_in(WORKSPACE_CAL))) == 1  # original only
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1  # mirror only


# 7. Re-running sync doesn't create duplicates
def test_rerunning_sync_is_idempotent(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))

    for _ in range(5):
        run_sync_pass(_cfg(), db, _clients(fake_client))

    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1
    assert len(active(fake_client.events_in(WORKSPACE_CAL))) == 1


# 8. Two overlapping independent source events on both calendars
def test_overlapping_events_both_preserved(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 12)))
    fake_client.seed_event(PERSONAL_CAL, make_event("p1", _dt(1, 11), _dt(1, 13)))

    run_sync_pass(_cfg(), db, _clients(fake_client))

    assert len(active(fake_client.events_in(WORKSPACE_CAL))) == 2  # w1 + mirror of p1
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 2  # p1 + mirror of w1


# 9. Recurring events: each instance mirrored/cancelled independently
def test_recurring_event_instances_handled_independently(db, fake_client):
    fake_client.seed_event(
        WORKSPACE_CAL,
        make_event("w1_instance1", _dt(1, 10), _dt(1, 11), recurringEventId="w1"),
    )
    fake_client.seed_event(
        WORKSPACE_CAL,
        make_event("w1_instance2", _dt(8, 10), _dt(8, 11), recurringEventId="w1"),
    )

    run_sync_pass(_cfg(), db, _clients(fake_client))
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 2

    fake_client.cancel_event(WORKSPACE_CAL, "w1_instance2")
    run_sync_pass(_cfg(), db, _clients(fake_client))

    remaining = active(fake_client.events_in(PERSONAL_CAL))
    assert len(remaining) == 1


# 10. All-day events handled without timezone conversion
def test_all_day_event_mirror(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_all_day("w1", _date(1), _date(2)))

    run_sync_pass(_cfg(), db, _clients(fake_client))

    mirror = active(fake_client.events_in(PERSONAL_CAL))[0]
    assert mirror["start"] == {"date": _date(1)}
    assert mirror["end"] == {"date": _date(2)}


# 11. Restarting the service preserves mappings (DB is reopened from the same file)
def test_restart_preserves_mappings(tmp_path, fake_client):
    db_path = str(tmp_path / "state.sqlite3")

    db1 = Database(db_path)
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))
    run_sync_pass(_cfg(), db1, _clients(fake_client))
    db1.close()

    db2 = Database(db_path)
    run_sync_pass(_cfg(), db2, _clients(fake_client))
    db2.close()

    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1


# 12. Invalid/expired sync token triggers a safe full resync, no duplicates
def test_expired_sync_token_triggers_full_resync(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))
    run_sync_pass(_cfg(), db, _clients(fake_client))

    token = db.get_sync_token("workspace")
    fake_client.expire_token(WORKSPACE_CAL, token)
    fake_client.seed_event(WORKSPACE_CAL, make_event("w2", _dt(2, 10), _dt(2, 11)))

    run_sync_pass(_cfg(), db, _clients(fake_client))

    personal_events = active(fake_client.events_in(PERSONAL_CAL))
    assert len(personal_events) == 2  # w1's mirror preserved (not duplicated) + w2's mirror created


# Transparent ("free") source events should not create a Busy mirror.
def test_transparent_event_is_skipped_and_removed_if_it_becomes_transparent(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))
    run_sync_pass(_cfg(), db, _clients(fake_client))
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1

    fake_client.patch_event(WORKSPACE_CAL, "w1", {"transparency": "transparent"})
    run_sync_pass(_cfg(), db, _clients(fake_client))

    assert active(fake_client.events_in(PERSONAL_CAL)) == []


# Dry-run must not write anything
def test_dry_run_makes_no_changes(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))

    run_sync_pass(_cfg(), db, _clients(fake_client), dry_run=True)

    assert active(fake_client.events_in(PERSONAL_CAL)) == []
    assert db.get_sync_token("workspace") is None


# 3+ calendars: an event on any one calendar is mirrored to every other calendar,
# and mirrors are never re-mirrored back onto a third calendar (no fan-out loops).
TEAM_CAL = "team-cal-id"


def test_three_calendars_fan_out_and_no_loop(db, fake_client):
    cfg = _cfg(calendars={"workspace": WORKSPACE_CAL, "personal": PERSONAL_CAL, "team": TEAM_CAL})
    clients = _clients(fake_client, "workspace", "personal", "team")

    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))

    run_sync_pass(cfg, db, clients)

    # w1 is mirrored onto both other calendars...
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1
    assert len(active(fake_client.events_in(TEAM_CAL))) == 1
    # ...and running again doesn't fan the mirrors back out or duplicate anything.
    run_sync_pass(cfg, db, clients)

    assert len(active(fake_client.events_in(WORKSPACE_CAL))) == 1  # original only
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1
    assert len(active(fake_client.events_in(TEAM_CAL))) == 1


# Sync window: events far outside [now, now + SYNC_WINDOW_DAYS] are left alone.
def test_event_outside_window_is_not_mirrored(db, fake_client):
    fake_client.seed_event(
        WORKSPACE_CAL,
        make_event("w1", _in_days(30), _in_days(30.04)),  # ~1 hour long, 30 days out
    )

    run_sync_pass(_cfg(sync_window_days=7), db, _clients(fake_client))

    assert active(fake_client.events_in(PERSONAL_CAL)) == []


# Once a mirrored event's window has passed, a later full resync must not delete
# it just because the (now narrower) window no longer covers it.
def test_mirror_outside_window_not_deleted_by_orphan_cleanup(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _in_days(2), _in_days(2.04)))
    run_sync_pass(_cfg(sync_window_days=7), db, _clients(fake_client))
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1

    # Force another full resync with a window that no longer reaches w1's dates.
    run_sync_pass(_cfg(full_resync_interval_hours=0, sync_window_days=1), db, _clients(fake_client))

    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1


# A cancellation must still clear an existing mirror via the incremental (sync-token)
# path even if a subsequently-narrowed window would no longer cover the event's dates.
def test_cancellation_outside_window_still_deletes_mirror_via_incremental_sync(db, fake_client):
    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _in_days(2), _in_days(2.04)))
    run_sync_pass(_cfg(sync_window_days=7), db, _clients(fake_client))
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 1

    fake_client.cancel_event(WORKSPACE_CAL, "w1")
    run_sync_pass(_cfg(sync_window_days=1), db, _clients(fake_client))

    assert active(fake_client.events_in(PERSONAL_CAL)) == []


def test_three_calendars_each_source_reaches_both_others(db, fake_client):
    cfg = _cfg(calendars={"workspace": WORKSPACE_CAL, "personal": PERSONAL_CAL, "team": TEAM_CAL})
    clients = _clients(fake_client, "workspace", "personal", "team")

    fake_client.seed_event(WORKSPACE_CAL, make_event("w1", _dt(1, 10), _dt(1, 11)))
    fake_client.seed_event(PERSONAL_CAL, make_event("p1", _dt(2, 10), _dt(2, 11)))
    fake_client.seed_event(TEAM_CAL, make_event("t1", _dt(3, 10), _dt(3, 11)))

    run_sync_pass(cfg, db, clients)

    # Each calendar ends up with its own original plus a mirror of the other two.
    assert len(active(fake_client.events_in(WORKSPACE_CAL))) == 3
    assert len(active(fake_client.events_in(PERSONAL_CAL))) == 3
    assert len(active(fake_client.events_in(TEAM_CAL))) == 3
