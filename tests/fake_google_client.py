from __future__ import annotations

from uuid import uuid4

from gcal_sync.errors import SyncTokenExpiredError
from gcal_sync.google_client import EventsPage


class FakeCalendarClient:
    """In-memory stand-in for the Google Calendar API, used by sync_engine tests.

    A single instance can hold multiple calendars (keyed by calendar_id) and is used
    to play the role of both the "workspace" and "personal" accounts in tests, since
    the sync engine only ever addresses calendars by id.
    """

    def __init__(self):
        self._calendars: dict[str, dict] = {}

    def _cal(self, calendar_id: str) -> dict:
        return self._calendars.setdefault(
            calendar_id, {"events": {}, "revision": 0, "changes": [], "expired_tokens": set()}
        )

    # -- test helpers ----------------------------------------------------
    def seed_event(self, calendar_id: str, event: dict) -> None:
        cal = self._cal(calendar_id)
        cal["revision"] += 1
        cal["events"][event["id"]] = event
        cal["changes"].append((cal["revision"], event["id"]))

    def cancel_event(self, calendar_id: str, event_id: str) -> None:
        cal = self._cal(calendar_id)
        cal["revision"] += 1
        cal["events"][event_id]["status"] = "cancelled"
        cal["changes"].append((cal["revision"], event_id))

    def expire_token(self, calendar_id: str, token: str) -> None:
        self._cal(calendar_id)["expired_tokens"].add(token)

    def events_in(self, calendar_id: str) -> dict:
        return self._cal(calendar_id)["events"]

    # -- CalendarClient interface ----------------------------------------
    def list_calendars(self):
        return [{"id": cal_id, "summary": cal_id} for cal_id in self._calendars]

    def list_events(self, calendar_id, sync_token=None, page_token=None) -> EventsPage:
        cal = self._cal(calendar_id)
        if sync_token is not None:
            if sync_token in cal["expired_tokens"]:
                raise SyncTokenExpiredError("token expired")
            since_rev = int(sync_token)
            changed_ids = sorted({eid for rev, eid in cal["changes"] if rev > since_rev})
            items = [cal["events"][eid] for eid in changed_ids if eid in cal["events"]]
        else:
            items = [e for e in cal["events"].values() if e.get("status") != "cancelled"]
        return EventsPage(items=items, next_page_token=None, next_sync_token=str(cal["revision"]))

    def insert_event(self, calendar_id, body) -> dict:
        cal = self._cal(calendar_id)
        cal["revision"] += 1
        new_id = f"mirror-{uuid4().hex[:10]}"
        event = dict(body)
        event["id"] = new_id
        event["status"] = "confirmed"
        cal["events"][new_id] = event
        cal["changes"].append((cal["revision"], new_id))
        return event

    def patch_event(self, calendar_id, event_id, body) -> dict:
        cal = self._cal(calendar_id)
        cal["revision"] += 1
        event = cal["events"][event_id]
        event.update(body)
        cal["changes"].append((cal["revision"], event_id))
        return event

    def delete_event(self, calendar_id, event_id) -> None:
        cal = self._cal(calendar_id)
        if event_id not in cal["events"]:
            return
        cal["revision"] += 1
        cal["events"][event_id]["status"] = "cancelled"
        cal["changes"].append((cal["revision"], event_id))
