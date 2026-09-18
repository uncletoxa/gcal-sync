from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


def event_signature(
    event: dict,
    full_copy: bool = False,
    title_template: str | None = None,
    description_template: str | None = None,
    calendar_name: str | None = None,
    color_id: str | None = None,
) -> str:
    """A stable fingerprint of the parts of the event we mirror.

    Start/end always matter; with `full_copy` the fields it also mirrors
    (title, description, location, meeting link) are fingerprinted too, so
    edits to those propagate even when the event's timing hasn't changed.
    The template strings and calendar name are fingerprinted too, so editing
    a pair's template (or renaming a calendar used via {calendar}) also
    forces an update on the next full-resync pass, without needing the
    source event itself to change. `color_id` is fingerprinted unconditionally
    since it applies regardless of full_copy.
    """
    fields = {"start": event.get("start"), "end": event.get("end"), "color_id": color_id}
    if full_copy:
        fields["summary"] = event.get("summary")
        fields["description"] = event.get("description")
        fields["location"] = event.get("location")
        fields["hangoutLink"] = event.get("hangoutLink")
        fields["title_template"] = title_template
        fields["description_template"] = description_template
        fields["calendar_name"] = calendar_name
    return json.dumps(fields, sort_keys=True)


def _render_template(template: str, *, title: str, description: str, calendar: str) -> str:
    """Fill {title}/{description}/{calendar} placeholders via literal substitution
    (not str.format, so stray braces in a real event's title/description can't raise
    or get misinterpreted as a format field)."""
    return (
        template
        .replace("{title}", title)
        .replace("{description}", description)
        .replace("{calendar}", calendar)
    )


def build_mirror_body(
    event: dict,
    source_account: str,
    source_calendar_id: str,
    full_copy: bool = False,
    title_template: str | None = None,
    description_template: str | None = None,
    calendar_name: str | None = None,
    color_id: str | None = None,
) -> dict:
    raw_title = event.get("summary") or ""
    raw_description = event.get("description", "")
    resolved_calendar_name = calendar_name or source_account

    title = "Busy"
    if full_copy:
        title = raw_title
        if title_template:
            title = _render_template(
                title_template, title=raw_title, description=raw_description, calendar=resolved_calendar_name
            )
        title = title or "Busy"

    body = {
        "summary": title,
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
    if color_id:
        body["colorId"] = color_id
    if full_copy:
        description = raw_description
        if description_template:
            description = _render_template(
                description_template, title=raw_title, description=raw_description, calendar=resolved_calendar_name
            )
        hangout_link = event.get("hangoutLink")
        if hangout_link:
            link_note = f"Meeting link: {hangout_link}"
            description = f"{description}\n\n{link_note}" if description else link_note
        if description:
            body["description"] = description
        location = event.get("location")
        if location:
            body["location"] = location
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


def _fetch_all_events(client: CalendarClient, calendar_id: str, sync_token, *, time_min=None, time_max=None):
    items = []
    page_token = None
    next_sync_token = None
    while True:
        page = client.list_events(
            calendar_id, sync_token=sync_token, page_token=page_token, time_min=time_min, time_max=time_max
        )
        items.extend(page.items)
        page_token = page.next_page_token
        if page.next_sync_token:
            next_sync_token = page.next_sync_token
        if not page_token:
            break
    return items, next_sync_token


def _sync_window(window_days: float) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now, now + timedelta(days=window_days)


def _parse_bound(value: "dict | None") -> "datetime | None":
    if not value:
        return None
    try:
        if "dateTime" in value:
            return datetime.fromisoformat(value["dateTime"])
        if "date" in value:
            return datetime.fromisoformat(value["date"]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return None


def _overlaps_window(start: "dict | None", end: "dict | None", window_start: datetime, window_end: datetime) -> bool:
    """True if [start, end) overlaps [window_start, window_end]. Unparseable bounds fail open (kept)."""
    start_dt, end_dt = _parse_bound(start), _parse_bound(end)
    if start_dt is None or end_dt is None:
        return True
    return start_dt < window_end and end_dt > window_start


def _event_in_window(event: dict, window_start: datetime, window_end: datetime) -> bool:
    return _overlaps_window(event.get("start"), event.get("end"), window_start, window_end)


def _signature_in_window(signature: str, window_start: datetime, window_end: datetime) -> bool:
    try:
        parsed = json.loads(signature)
    except (TypeError, ValueError):
        return False
    return _overlaps_window(parsed.get("start"), parsed.get("end"), window_start, window_end)


def _fetch_source_events(
    source_client: CalendarClient,
    db: Database,
    *,
    source_calendar_id: str,
    source_calendar_key: str,
    full_resync_interval_hours: float,
    sync_window_days: float,
    force_full: bool = False,
) -> tuple[list[dict], "str | None", bool, datetime, datetime]:
    """Fetch one source calendar's changed events since the last pass (once per pass,
    regardless of how many destination calendars it fans out to)."""
    stored_token = db.get_sync_token(source_calendar_key)
    force_full = (
        force_full or stored_token is None or _full_resync_due(db, source_calendar_key, full_resync_interval_hours)
    )
    sync_token = None if force_full else stored_token
    window_start, window_end = _sync_window(sync_window_days)

    if force_full:
        events, next_sync_token = _fetch_all_events(
            source_client,
            source_calendar_id,
            None,
            time_min=window_start.isoformat(),
            time_max=window_end.isoformat(),
        )
    else:
        try:
            events, next_sync_token = _fetch_all_events(source_client, source_calendar_id, sync_token)
        except SyncTokenExpiredError:
            log_event(logger, "sync_token_expired", level="warning", calendar=source_calendar_key)
            force_full = True
            events, next_sync_token = _fetch_all_events(
                source_client,
                source_calendar_id,
                None,
                time_min=window_start.isoformat(),
                time_max=window_end.isoformat(),
            )
        else:
            # Google rejects timeMin/timeMax combined with syncToken, so an incremental
            # fetch can't be time-bounded server-side — filter client-side instead.
            # Cancellations always pass through: a source deletion must still clear an
            # existing mirror even if the (now-gone) event would fall outside the window.
            events = [
                event
                for event in events
                if event.get("status") == "cancelled" or _event_in_window(event, window_start, window_end)
            ]

    # Never mirror a mirror: this is the core loop-prevention check. A cancelled
    # event always passes through regardless, even one that still happens to carry
    # our extendedProperties — heal_deleted_mirrors needs it to notice one of our
    # own mirrors was deleted, and a gone event can't be re-propagated as a source.
    events = [event for event in events if event.get("status") == "cancelled" or not is_mirror_event(event)]
    return events, next_sync_token, force_full, window_start, window_end


def propagate_to_destination(
    events: list[dict],
    force_full: bool,
    dest_client: CalendarClient,
    db: Database,
    *,
    source_account: str,
    source_calendar_id: str,
    source_calendar_key: str,
    dest_account: str,
    dest_calendar_id: str,
    window_start: datetime,
    window_end: datetime,
    dry_run: bool = False,
    full_copy: bool = False,
    title_template: str | None = None,
    description_template: str | None = None,
    calendar_name: str | None = None,
    color_id: str | None = None,
) -> SyncStats:
    """Mirror one source calendar's busy blocks into one destination calendar."""
    stats = SyncStats()
    processed_ids: set[str] = set()

    for event in events:
        event_id = event["id"]
        processed_ids.add(event_id)
        mapping = db.get_mapping(source_account, source_calendar_id, event_id, dest_account, dest_calendar_id)

        if event.get("status") == "cancelled":
            if mapping is not None and mapping["status"] == "active" and mapping["dest_event_id"]:
                if not dry_run:
                    dest_client.delete_event(dest_calendar_id, mapping["dest_event_id"])
                    db.mark_deleted(mapping["id"])
                stats.deleted += 1
                log_event(
                    logger, "mirror_deleted", source=source_calendar_key, dest=dest_account, reason="source_cancelled"
                )
            continue

        if should_skip_event(event):
            if mapping is not None and mapping["status"] == "active" and mapping["dest_event_id"]:
                if not dry_run:
                    dest_client.delete_event(dest_calendar_id, mapping["dest_event_id"])
                    db.mark_deleted(mapping["id"])
                stats.deleted += 1
                log_event(
                    logger,
                    "mirror_deleted",
                    source=source_calendar_key,
                    dest=dest_account,
                    reason="source_no_longer_blocking",
                )
            else:
                stats.skipped += 1
                log_event(logger, "event_skipped", source=source_calendar_key, dest=dest_account)
            continue

        signature = event_signature(event, full_copy, title_template, description_template, calendar_name, color_id)
        body = build_mirror_body(
            event, source_account, source_calendar_id, full_copy,
            title_template, description_template, calendar_name, color_id,
        )

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
                dest_event_id = mapping["dest_event_id"]
                if not color_id:
                    # build_mirror_body omits "colorId" entirely when unset, which is right
                    # for creation but would leave a previously-set color untouched on patch
                    # (Google's patch semantics: omitted = unchanged, explicit null = cleared).
                    body["colorId"] = None
                patched = dest_client.patch_event(dest_calendar_id, dest_event_id, body)
                if patched is None:
                    # The destination copy was deleted out from under us (e.g. manually,
                    # or by the user's own calendar client) — recreate it rather than
                    # leaving the mapping pointing at a dead event forever.
                    created = dest_client.insert_event(dest_calendar_id, body)
                    dest_event_id = created["id"]
                db.upsert_mapping(
                    source_account=source_account,
                    source_calendar_id=source_calendar_id,
                    source_event_id=event_id,
                    dest_account=dest_account,
                    dest_calendar_id=dest_calendar_id,
                    dest_event_id=dest_event_id,
                    source_signature=signature,
                    status="active",
                )
            stats.updated += 1
            log_event(logger, "mirror_updated", source=source_calendar_key, dest=dest_account)
        else:
            stats.unchanged += 1

    if force_full:
        # A full listing reflects every currently-active source event *within the sync
        # window*, so any mapping not seen here whose recorded event also falls in that
        # window is orphaned (its source was deleted while we had no valid token, or it
        # was manually tampered with on the destination) — restore by deleting; the next
        # pass (or this one, since it was just processed above) recreates it. Mappings
        # for events outside the window (already ended, or further out than we fetched)
        # are left untouched — this fetch tells us nothing about their current status.
        for mapping in db.list_active_mappings_for_pair(
            source_account, source_calendar_id, dest_account, dest_calendar_id
        ):
            if mapping["source_event_id"] in processed_ids:
                continue
            if not _signature_in_window(mapping["source_signature"], window_start, window_end):
                continue
            if not dry_run:
                dest_client.delete_event(dest_calendar_id, mapping["dest_event_id"])
                db.mark_deleted(mapping["id"])
            stats.deleted += 1
            log_event(logger, "mirror_deleted", source=source_calendar_key, dest=dest_account, reason="orphan_cleanup")

    return stats


def heal_deleted_mirrors(
    events: list[dict],
    account: str,
    calendar_id: str,
    clients: dict[str, CalendarClient],
    db: Database,
    *,
    dry_run: bool,
    disabled_pairs: frozenset[tuple[str, str]],
    full_copy_pairs: frozenset[tuple[str, str]],
    pair_templates: dict[tuple[str, str], tuple[str | None, str | None]],
    calendar_display_names: dict[str, str],
    pair_colors: dict[tuple[str, str], str | None],
) -> int:
    """Notice this account's own mirror events that were deleted directly on its
    calendar (by the user, or anything else outside this tool) and recreate them.

    `events` is one account's own already-fetched event list — the same one
    `_fetch_source_events` produces for it every pass regardless of whether this
    function is even called. A cancelled tombstone rarely still carries the
    extendedProperties that mark it as one of ours, so the mirror-loop-prevention
    filter can't recognize it as "our" event and it normally just falls through
    inertly. Cross-referencing its id against our mapping table as a *destination*
    event id (instead of the usual source-event lookup) recovers that signal for
    free: no extra API calls beyond the fetch this pass was already making, and no
    waiting for the next full resync — a plain incremental pass repairs it as soon
    as the deletion shows up in that account's own sync-token diff.
    """
    healed = 0
    for event in events:
        if event.get("status") != "cancelled":
            continue
        mapping = db.get_mapping_by_dest_event(account, calendar_id, event["id"])
        if mapping is None:
            continue
        pair = (mapping["source_account"], mapping["dest_account"])
        if pair in disabled_pairs:
            continue
        source_client = clients.get(mapping["source_account"])
        if source_client is None:
            continue

        source_event = source_client.get_event(mapping["source_calendar_id"], mapping["source_event_id"])
        if source_event is None or source_event.get("status") == "cancelled" or should_skip_event(source_event):
            # The original event is gone too (or no longer blocking) — nothing to
            # restore; just stop treating this mapping as active.
            if not dry_run:
                db.mark_deleted(mapping["id"])
            continue

        title_template, description_template = pair_templates.get(pair, (None, None))
        full_copy = pair in full_copy_pairs
        calendar_name = calendar_display_names.get(mapping["source_account"], mapping["source_account"])
        color_id = pair_colors.get(pair)
        signature = event_signature(
            source_event, full_copy, title_template, description_template, calendar_name, color_id
        )
        body = build_mirror_body(
            source_event, mapping["source_account"], mapping["source_calendar_id"], full_copy,
            title_template, description_template, calendar_name, color_id,
        )
        if not dry_run:
            created = clients[account].insert_event(calendar_id, body)
            db.upsert_mapping(
                source_account=mapping["source_account"],
                source_calendar_id=mapping["source_calendar_id"],
                source_event_id=mapping["source_event_id"],
                dest_account=account,
                dest_calendar_id=calendar_id,
                dest_event_id=created["id"],
                source_signature=signature,
                status="active",
            )
        healed += 1
        log_event(logger, "mirror_restored", source=mapping["source_account"], dest=account, reason="dest_deleted")
    return healed


def sync_all_pairs(
    clients: dict[str, CalendarClient],
    calendar_ids: dict[str, str],
    db: Database,
    *,
    full_resync_interval_hours: float = 24.0,
    sync_window_days: float = 14.0,
    dry_run: bool = False,
    full_copy_pairs: frozenset[tuple[str, str]] = frozenset(),
    disabled_pairs: frozenset[tuple[str, str]] = frozenset(),
    sync_window_overrides: dict[str, float] | None = None,
    pair_templates: dict[tuple[str, str], tuple[str | None, str | None]] | None = None,
    calendar_display_names: dict[str, str] | None = None,
    pair_colors: dict[tuple[str, str], str | None] | None = None,
    force_full: bool = False,
) -> dict[str, SyncStats]:
    """Mirror busy blocks between every ordered pair of configured calendars.

    Each calendar's events are fetched exactly once per pass and then fanned out
    to every *other* configured calendar, so this scales to any number (2+) of
    calendars without re-fetching the same source multiple times per pass.

    `full_copy_pairs` opts specific directed (source, dest) pairs into mirroring
    title/description/location instead of just a "Busy" placeholder; every pair
    not listed keeps the default busy-only behavior.

    `disabled_pairs` excludes specific directed (source, dest) pairs from syncing
    entirely — no propagation happens and no pair_stats entry is produced for them.

    `sync_window_overrides` maps an account name to a per-account sync_window_days
    override, used when that account is the source; accounts not present fall back
    to `sync_window_days`.

    `pair_templates` maps a (source, dest) pair in `full_copy_pairs` to a
    (title_template, description_template) override; a pair absent here, or with a
    None half, keeps the default raw passthrough for that half.

    `calendar_display_names` maps an account name to the user-facing name used to
    fill the {calendar} template placeholder when that account is the source;
    accounts not present fall back to their account name.

    `pair_colors` maps a (source, dest) pair to a Google Calendar eventColor id
    applied to mirrored events for that pair, regardless of busy-only/full-copy
    mode; a pair absent here leaves the destination's default event color
    untouched.

    `force_full` re-fetches every source calendar in full regardless of its
    sync-token/last-full-sync bookkeeping — needed e.g. right after a new
    destination calendar is connected, since that destination has never seen any
    of the other calendars' pre-existing, unchanged events.
    """
    accounts = list(calendar_ids)
    pair_stats: dict[str, SyncStats] = {}
    sync_window_overrides = sync_window_overrides or {}
    pair_templates = pair_templates or {}
    calendar_display_names = calendar_display_names or {}
    pair_colors = pair_colors or {}

    for source_account in accounts:
        source_calendar_id = calendar_ids[source_account]
        source_sync_window_days = sync_window_overrides.get(source_account, sync_window_days)
        events, next_sync_token, source_force_full, window_start, window_end = _fetch_source_events(
            clients[source_account],
            db,
            source_calendar_id=source_calendar_id,
            source_calendar_key=source_account,
            full_resync_interval_hours=full_resync_interval_hours,
            sync_window_days=source_sync_window_days,
            force_full=force_full,
        )

        # Uses this same fetch to notice and repair any of this account's own
        # mirror events that got deleted directly on its calendar — see
        # heal_deleted_mirrors for why this doesn't cost any extra API calls.
        heal_deleted_mirrors(
            events,
            source_account,
            source_calendar_id,
            clients,
            db,
            dry_run=dry_run,
            disabled_pairs=disabled_pairs,
            full_copy_pairs=full_copy_pairs,
            pair_templates=pair_templates,
            calendar_display_names=calendar_display_names,
            pair_colors=pair_colors,
        )

        for dest_account in accounts:
            if dest_account == source_account:
                continue
            if (source_account, dest_account) in disabled_pairs:
                continue
            title_template, description_template = pair_templates.get((source_account, dest_account), (None, None))
            color_id = pair_colors.get((source_account, dest_account))
            pair_stats[f"{source_account}->{dest_account}"] = propagate_to_destination(
                events,
                source_force_full,
                clients[dest_account],
                db,
                source_account=source_account,
                source_calendar_id=source_calendar_id,
                source_calendar_key=source_account,
                dest_account=dest_account,
                dest_calendar_id=calendar_ids[dest_account],
                window_start=window_start,
                window_end=window_end,
                dry_run=dry_run,
                full_copy=(source_account, dest_account) in full_copy_pairs,
                title_template=title_template,
                description_template=description_template,
                calendar_name=calendar_display_names.get(source_account, source_account),
                color_id=color_id,
            )

        if not dry_run:
            if source_force_full:
                db.set_last_full_sync(source_account, datetime.now(timezone.utc).isoformat())
            if next_sync_token:
                db.set_sync_token(source_account, next_sync_token)

    return pair_stats


def run_sync_pass(
    cfg,
    db: Database,
    clients: dict[str, CalendarClient],
    dry_run: bool = False,
    force_full: bool = False,
):
    log_event(logger, "sync_started", dry_run=dry_run, force_full=force_full, accounts=list(cfg.calendars))

    pair_stats = sync_all_pairs(
        clients,
        cfg.calendars,
        db,
        full_resync_interval_hours=cfg.full_resync_interval_hours,
        sync_window_days=cfg.sync_window_days,
        dry_run=dry_run,
        full_copy_pairs=cfg.full_copy_pairs,
        disabled_pairs=cfg.disabled_pairs,
        sync_window_overrides=cfg.sync_window_overrides,
        pair_templates=cfg.pair_templates,
        calendar_display_names=cfg.calendar_display_names,
        pair_colors=cfg.pair_colors,
        force_full=force_full,
    )

    log_event(logger, "sync_completed", **{key: vars(stats) for key, stats in pair_stats.items()})
    return pair_stats
