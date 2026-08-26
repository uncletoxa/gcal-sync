from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import Database
from .errors import SyncTokenExpiredError
from .google_client import CalendarClient
from .logging_config import log_event

logger = logging.getLogger(__name__)

# Extended-property keys used to mark and identify mirror events.
# This is the loop-prevention mechanism: any event carrying this marker
# is never treated as a source event, no matter which calendar it appears in.
MIRROR_FLAG = "gcalSyncMirror"
SRC_ACCOUNT_KEY = "gcalSyncSourceAccount"
SRC_CALENDAR_KEY = "gcalSyncSourceCalendarId"
SRC_EVENT_KEY = "gcalSyncSourceEventId"
APP_KEY = "gcalSyncApp"
APP_VALUE = "gcal-sync"

# Event types that don't represent a real busy/free time block.
SKIPPED_EVENT_TYPES = {"workingLocation"}


@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    unchanged: int = 0


def is_mirror_event(event: dict) -> bool:
    props = event.get("extendedProperties", {}).get("private", {})
    return props.get(MIRROR_FLAG) == "true"


def should_skip_event(event: dict) -> bool:
    """Events that don't block time shouldn't produce a Busy mirror."""
    if event.get("transparency") == "transparent":
        return True
    if event.get("eventType") in SKIPPED_EVENT_TYPES:
        return True
    return False


def event_signature(event: dict) -> str:
    """A stable fingerprint of the parts of the event we mirror (start/end only)."""
    return json.dumps({"start": event.get("start"), "end": event.get("end")}, sort_keys=True)


def build_mirror_body(event: dict, source_account: str, source_calendar_id: str) -> dict:
    body = {
        "summary": "Busy",
        "visibility": "private",
        "transparency": "opaque",
        "reminders": {"useDefault": False},
        "extendedProperties": {
            "private": {
                MIRROR_FLAG: "true",
                SRC_ACCOUNT_KEY: source_account,
                SRC_CALENDAR_KEY: source_calendar_id,
                SRC_EVENT_KEY: event["id"],
                APP_KEY: APP_VALUE,
            }
        },
    }
    start, end = event.get("start", {}), event.get("end", {})
    if "date" in start:
        # All-day event: copy the date strings as-is, no timezone involved.
        body["start"] = {"date": start["date"]}
        body["end"] = {"date": end["date"]}
    else:
        # Preserve the instant + timezone Google gave us; never shift offsets manually.
        body["start"] = {"dateTime": start["dateTime"], "timeZone": start.get("timeZone")}
        body["end"] = {"dateTime": end["dateTime"], "timeZone": end.get("timeZone")}
    return body


def _full_resync_due(db: Database, calendar_key: str, interval_hours: float) -> bool:
    last = db.get_last_full_sync(calendar_key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
    return age_hours >= interval_hours


def _fetch_all_events(client: CalendarClient, calendar_id: str, sync_token):
    items = []
    page_token = None
    next_sync_token = None
    while True:
        page = client.list_events(calendar_id, sync_token=sync_token, page_token=page_token)
        items.extend(page.items)
        page_token = page.next_page_token
        if page.next_sync_token:
            next_sync_token = page.next_sync_token
        if not page_token:
            break
    return items, next_sync_token


def sync_direction(
    source_client: CalendarClient,
    dest_client: CalendarClient,
    db: Database,
    *,
    source_account: str,
    source_calendar_id: str,
    source_calendar_key: str,
    dest_account: str,
    dest_calendar_id: str,
    full_resync_interval_hours: float = 24.0,
    dry_run: bool = False,
) -> SyncStats:
    """Mirror busy blocks from one calendar into another. Call twice for bidirectional sync."""
    stats = SyncStats()
    stored_token = db.get_sync_token(source_calendar_key)
    force_full = stored_token is None or _full_resync_due(db, source_calendar_key, full_resync_interval_hours)
    sync_token = None if force_full else stored_token

    try:
        events, next_sync_token = _fetch_all_events(source_client, source_calendar_id, sync_token)
    except SyncTokenExpiredError:
        log_event(logger, "sync_token_expired", level="warning", calendar=source_calendar_key)
        if not dry_run:
            db.set_sync_token(source_calendar_key, None)
        force_full = True
        events, next_sync_token = _fetch_all_events(source_client, source_calendar_id, None)

    processed_ids: set[str] = set()

    for event in events:
        if is_mirror_event(event):
            # Never mirror a mirror: this is the core loop-prevention check.
            continue

        event_id = event["id"]
        processed_ids.add(event_id)
        mapping = db.get_mapping(source_account, source_calendar_id, event_id, dest_account, dest_calendar_id)

        if event.get("status") == "cancelled":
            if mapping is not None and mapping["status"] == "active" and mapping["dest_event_id"]:
                if not dry_run:
                    dest_client.delete_event(dest_calendar_id, mapping["dest_event_id"])
                    db.mark_deleted(mapping["id"])
                stats.deleted += 1
                log_event(logger, "mirror_deleted", source=source_calendar_key, reason="source_cancelled")
            continue

        if should_skip_event(event):
            if mapping is not None and mapping["status"] == "active" and mapping["dest_event_id"]:
                if not dry_run:
                    dest_client.delete_event(dest_calendar_id, mapping["dest_event_id"])
                    db.mark_deleted(mapping["id"])
                stats.deleted += 1
                log_event(logger, "mirror_deleted", source=source_calendar_key, reason="source_no_longer_blocking")
            else:
                stats.skipped += 1
                log_event(logger, "event_skipped", source=source_calendar_key)
            continue

        signature = event_signature(event)
        body = build_mirror_body(event, source_account, source_calendar_id)

        if mapping is None or mapping["status"] != "active" or not mapping["dest_event_id"]:
            if not dry_run:
                created = dest_client.insert_event(dest_calendar_id, body)
                db.upsert_mapping(
                    source_account=source_account,
                    source_calendar_id=source_calendar_id,
                    source_event_id=event_id,
                    dest_account=dest_account,
                    dest_calendar_id=dest_calendar_id,
                    dest_event_id=created["id"],
                    source_signature=signature,
                    status="active",
                )
            stats.created += 1
            log_event(logger, "mirror_created", source=source_calendar_key, dest=dest_account)
        elif mapping["source_signature"] != signature:
            if not dry_run:
                dest_client.patch_event(dest_calendar_id, mapping["dest_event_id"], body)
                db.upsert_mapping(
                    source_account=source_account,
                    source_calendar_id=source_calendar_id,
                    source_event_id=event_id,
                    dest_account=dest_account,
                    dest_calendar_id=dest_calendar_id,
                    dest_event_id=mapping["dest_event_id"],
                    source_signature=signature,
                    status="active",
                )
            stats.updated += 1
            log_event(logger, "mirror_updated", source=source_calendar_key, dest=dest_account)
        else:
            stats.unchanged += 1

    if force_full:
        # A full listing reflects every currently-active source event, so any mapping
        # not seen here is orphaned (its source was deleted while we had no valid token)
        # or was manually tampered with on the destination — restore by deleting;
        # the next pass (or this one, since it was just processed above) recreates it.
        for mapping in db.list_active_mappings_for_pair(
            source_account, source_calendar_id, dest_account, dest_calendar_id
        ):
            if mapping["source_event_id"] not in processed_ids:
                if not dry_run:
                    dest_client.delete_event(dest_calendar_id, mapping["dest_event_id"])
                    db.mark_deleted(mapping["id"])
                stats.deleted += 1
                log_event(logger, "mirror_deleted", source=source_calendar_key, reason="orphan_cleanup")
        if not dry_run:
            db.set_last_full_sync(source_calendar_key, datetime.now(timezone.utc).isoformat())

    if not dry_run and next_sync_token:
        db.set_sync_token(source_calendar_key, next_sync_token)

    return stats


def run_sync_pass(
    cfg,
    db: Database,
    workspace_client: CalendarClient,
    personal_client: CalendarClient,
    dry_run: bool = False,
):
    log_event(logger, "sync_started", dry_run=dry_run)

    stats_w2p = sync_direction(
        workspace_client,
        personal_client,
        db,
        source_account="workspace",
        source_calendar_id=cfg.workspace_calendar_id,
        source_calendar_key="workspace",
        dest_account="personal",
        dest_calendar_id=cfg.personal_calendar_id,
        full_resync_interval_hours=cfg.full_resync_interval_hours,
        dry_run=dry_run,
    )
    stats_p2w = sync_direction(
        personal_client,
        workspace_client,
        db,
        source_account="personal",
        source_calendar_id=cfg.personal_calendar_id,
        source_calendar_key="personal",
        dest_account="workspace",
        dest_calendar_id=cfg.workspace_calendar_id,
        full_resync_interval_hours=cfg.full_resync_interval_hours,
        dry_run=dry_run,
    )

    log_event(
        logger,
        "sync_completed",
        workspace_to_personal=vars(stats_w2p),
        personal_to_workspace=vars(stats_p2w),
    )
    return stats_w2p, stats_p2w
