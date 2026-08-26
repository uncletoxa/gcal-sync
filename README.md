# gcal-sync

Self-hosted, bidirectional **availability** synchronization between a Google
Workspace calendar and a personal Google calendar.

It does **not** copy event titles, descriptions, attendees, locations, or
conferencing links. It only creates private "Busy" placeholder events on the
other calendar so both calendars accurately reflect your availability.

## What it does

* Polls both calendars on an interval (default: every 2 minutes) using
  Google Calendar's incremental sync (`syncToken`), so it doesn't re-download
  the whole calendar every pass.
* For every real event on the Workspace calendar, ensures a private,
  content-free "Busy" block exists at the same time on the personal calendar
  — and vice versa.
* Moves/resizes/deletes on the source event are propagated to its mirror.
* Recurring events and all-day events are handled correctly (Google expands
  recurring events into individual instances for us; each instance is
  mirrored/cancelled independently).
* Events marked "Free" (`transparency: transparent`) are treated as
  non-blocking and are not mirrored.
* Mirror events are tagged with Google Calendar `extendedProperties` so they
  are never re-mirrored back onto their source calendar (no sync loops), and
  so a manually-edited mirror gets restored on the next full resync.
* State (sync tokens + the source-event ↔ mirror-event mapping) is kept in a
  local SQLite database, so the service can be killed/restarted at any time
  without creating duplicates or losing track of anything.

It never modifies or deletes your real/original events — only the mirrors it
created.

## Project layout

```
src/gcal_sync/
  config.py        # env-based configuration
  auth.py           # OAuth loopback flow + credential storage/refresh
  google_client.py  # Google Calendar API client (retryable) + CalendarClient interface
  db.py             # SQLite persistence: sync tokens + event mappings
  sync_engine.py    # core bidirectional sync logic (pure, testable without the API)
  cli.py            # `gcal-sync` command group
tests/              # pytest suite; sync engine is tested against a fake in-memory client
deploy/             # Dockerfile / docker-compose (works with Podman) + a systemd unit example
```

## 1. Google Cloud project / API setup

1. Go to the Google Cloud Console and create a project (or reuse one you
   control) — you'll do this **twice** conceptually, but only need **one**
   OAuth client: the same Desktop OAuth client is used to authorize both
   your Workspace account and your personal account (they just produce two
   separate refresh tokens).
2. Enable the **Google Calendar API** for the project
   (APIs & Services → Library → "Google Calendar API" → Enable).
3. Configure the **OAuth consent screen**:
   * User type: "Internal" if the project lives under your Workspace org and
     you only need the Workspace account to authorize (Internal apps don't
     need Google's verification review). Otherwise choose "External" and add
     both Google accounts (Workspace + personal) as **test users** — apps in
     "Testing" mode work indefinitely for test users without verification.
   * Scopes: you don't need to add scopes on the consent screen itself for a
     Desktop app / testing setup; the app requests them directly (see below).

## 2. OAuth client setup

1. APIs & Services → Credentials → Create Credentials → OAuth client ID.
2. Application type: **Desktop app**.
3. Download the resulting JSON file.
4. Save it in this repo's `data/` directory (already gitignored), e.g. as
   `data/client_secret.json`, or point `GOOGLE_CLIENT_SECRETS_FILE` in `.env`
   at wherever you saved it.

This app requests only two scopes (see `src/gcal_sync/auth.py`):

* `https://www.googleapis.com/auth/calendar.events` — create/read/update/delete events
* `https://www.googleapis.com/auth/calendar.calendarlist.readonly` — list calendars (for the `calendars` command)

## Install

This project uses [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
```

## 3. Configure `.env`

Edit `.env` (see `.env.example` for the full list):

```
GOOGLE_CLIENT_SECRETS_FILE=data/client_secret.json
TOKEN_DIR=data
DB_PATH=data/state.sqlite3
WORKSPACE_CALENDAR_ID=
PERSONAL_CALENDAR_ID=
POLL_INTERVAL_SECONDS=120
FULL_RESYNC_INTERVAL_HOURS=24
LOG_LEVEL=INFO
```

`WORKSPACE_CALENDAR_ID` / `PERSONAL_CALENDAR_ID` are filled in after step 6
below (calendar discovery). Leave them blank until then — `auth` and
`calendars` don't need them.

## 4. Authorize the Workspace account

```bash
uv run gcal-sync auth --account workspace
```

This opens a browser (loopback redirect to `http://localhost:<random-port>`,
no public callback needed) — sign in with your **Workspace** account and
approve. The resulting refresh token is written to `data/token_workspace.json`
with `0600` permissions.

If you're on a headless machine without a local browser, use
`--no-browser` and open the printed URL yourself (e.g. via an SSH tunnel:
`ssh -L <port>:localhost:<port> this-machine`).

## 5. Authorize the personal account

```bash
uv run gcal-sync auth --account personal
```

Same flow — sign in with your **personal** Google account this time. Stored
as `data/token_personal.json`.

You will not need to log in again after this; `gcal-sync` refreshes access
tokens automatically using the stored refresh tokens.

## 6. Find calendar IDs

```bash
uv run gcal-sync calendars --account workspace
uv run gcal-sync calendars --account personal
```

Each line is `<calendar id>  <summary>  <PRIMARY if applicable>`. Usually
you want the `PRIMARY` calendar for each account — its ID is the account's
email address. Put the IDs into `.env` as `WORKSPACE_CALENDAR_ID` /
`PERSONAL_CALENDAR_ID`.

## 7. First dry run

```bash
uv run gcal-sync sync --dry-run
```

This performs one real read-only pass — it reads both calendars for real but
does not create/update/delete anything or persist sync tokens — and logs
what it *would* do (`mirror_created` / `mirror_updated` / `mirror_deleted` /
`event_skipped`). Check the output looks sane, then run it for real:

```bash
uv run gcal-sync sync
```

`sync` performs a single pass and exits (good for cron-style scheduling, or
just to sanity check things). `start` runs the same logic continuously:

```bash
uv run gcal-sync start
```

## 8. Run continuously

### Option A — Podman (recommended, since it's available on this machine)

```bash
podman build -t gcal-sync -f deploy/Dockerfile .
podman compose -f deploy/docker-compose.yml up -d   # or: podman-compose -f deploy/docker-compose.yml up -d
```

The container mounts `./data` (containing your tokens + SQLite DB) and reads
`.env`. No ports are published — this service only makes outbound HTTPS
calls to Google, so nothing needs to be exposed.

To view logs: `podman logs -f gcal-sync`
To stop: `podman compose -f deploy/docker-compose.yml down` (data persists in `./data`)

### Option B — systemd (no containers)

See `deploy/gcal-sync.service` for a unit that runs `uv run gcal-sync start`
directly on the host. Copy/adapt it into `~/.config/systemd/user/` (or
`/etc/systemd/system/` for a system unit), then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now gcal-sync
```

## CLI reference

| Command | Purpose |
|---|---|
| `gcal-sync auth --account workspace\|personal [--no-browser]` | One-time OAuth authorization for an account |
| `gcal-sync calendars --account workspace\|personal` | List calendars visible to an authorized account (to find IDs) |
| `gcal-sync sync [--dry-run]` | Run a single synchronization pass and exit |
| `gcal-sync start [--dry-run]` | Run continuously, polling every `POLL_INTERVAL_SECONDS` |

## Sync behavior notes

* **Loop prevention**: every mirror event is created with
  `extendedProperties.private.gcalSyncMirror = "true"` plus source
  identifiers. Any event carrying that flag is skipped entirely when
  scanning for *source* events — so a mirror is never mirrored back.
* **Idempotency**: mirrors are keyed by `(source account, source calendar,
  source event id, dest account, dest calendar)` in SQLite. Re-running sync
  never creates duplicates; it only creates/patches/deletes when the stored
  state disagrees with the source.
* **Manual edits to a mirror**: mirrors are restored to the correct state on
  the next full resync (see `FULL_RESYNC_INTERVAL_HOURS`, default 24h) since
  a full listing re-asserts every mirror's expected state and cleans up
  anything no longer backed by a live source event.
* **Expired/invalid sync tokens** (HTTP 410) are caught automatically; the
  affected calendar falls back to a full listing and reconciles safely
  (deletes orphaned mirrors whose source event vanished while the token was
  invalid, without duplicating anything still present).
* **Time zones / DST**: mirror events copy Google's own `dateTime` +
  `timeZone` fields (or `date` for all-day events) verbatim — there is no
  manual offset math anywhere, so DST transitions are handled the same way
  Google handles them for the original event.
* **Retries**: transient errors (HTTP 429/500/502/503/504, rate-limit 403s,
  and network errors) are retried with exponential backoff + jitter
  (`src/gcal_sync/retry.py`). Non-retryable errors (expired sync token, auth
  failures, 4xx client errors) are not retried and are logged distinctly.

## Testing

```bash
uv run pytest
```

The sync engine (`sync_engine.py`) is tested entirely against an in-memory
fake Calendar client (`tests/fake_google_client.py`) — no real Google API
calls are made in the test suite. Covered scenarios include: new events in
either direction, moves/resizes, deletions, loop prevention, idempotent
re-runs, overlapping independent events, recurring event instances, all-day
events, restart/persistence across a fresh `Database` instance pointing at
the same SQLite file, expired sync tokens, retry/backoff behavior, and
automatic OAuth token refresh.

## Troubleshooting

* **`AuthenticationError: No stored credentials for account 'X'`** — run
  `gcal-sync auth --account X`.
* **`Failed to refresh credentials`** — the refresh token was revoked
  (e.g. you removed the app's access in your Google Account settings, or it
  expired because the OAuth consent screen is still in "Testing" mode and
  test-user refresh tokens can expire after 7 days of the app being
  unverified — re-run `auth`, or move the consent screen to "In production"
  if this is a long-lived internal deployment).
* **`OAuth client secrets file not found`** — check `GOOGLE_CLIENT_SECRETS_FILE`
  in `.env` points at the JSON you downloaded from Cloud Console.
* **HTTP 403 `accessNotConfigured`** — the Calendar API isn't enabled on the
  Cloud project backing your OAuth client.
* **Missing configuration errors on `sync`/`start`** — `WORKSPACE_CALENDAR_ID`
  / `PERSONAL_CALENDAR_ID` must be set in `.env` (see step 6 above).
* Structured JSON logs go to stdout; grep for `"event": "authentication_error"`
  or `"event": "api_error"` to find failures. Logs never contain tokens,
  secrets, or original event titles/descriptions/attendees.

## Backup / restore SQLite state

All persistent state lives under `data/` (gitignored):

* `data/client_secret.json` — your OAuth client (not a secret you generated,
  but treat it as sensitive)
* `data/token_workspace.json`, `data/token_personal.json` — refresh tokens
* `data/state.sqlite3` (+ `-wal`/`-shm` while running) — sync tokens and
  event mappings

To back up: stop the service, copy the whole `data/` directory somewhere
safe. To restore: stop the service, replace `data/` with the backup, start
again — the service will resume incremental sync from the stored tokens (or
fall back to a full resync if the stored SQLite state is older/missing).

If you lose `data/state.sqlite3` but keep the tokens, that's safe too: the
next run has no sync tokens, so it performs a full resync of both calendars
and rebuilds the mapping table from scratch (it will not duplicate mirrors
across a full rebuild since it's still matching by source event ID — it
simply has no memory of previously-created mirrors, so in that specific
scenario it may create a second round of mirrors for events whose original
mirror it can no longer recognize; treat losing the DB as roughly equivalent
to a first run and expect to briefly see doubled mirrors until you manually
clean up stale ones, which is why regular backups of `state.sqlite3` are the
one thing worth doing here).

## Upgrading / redeploying safely

* Pull/build the new version, then restart the container or systemd unit.
  `data/` is a bind mount / lives on the host filesystem, so it survives
  redeploys.
* No database migrations are currently required between versions (schema is
  created with `CREATE TABLE IF NOT EXISTS`); if that changes in the future,
  back up `data/state.sqlite3` first regardless.
* Safe to restart at any time — the service is designed to resume cleanly
  (see "Restarting the service preserves mappings" in the test suite).

## Security considerations

* `.env`, `data/` (tokens + SQLite DB), and any `client_secret*.json` /
  `token_*.json` are all covered by `.gitignore` — verify `git status` never
  shows them before committing.
* Token files are written with `0600` permissions.
* Only two narrow Calendar scopes are requested — not the broad `calendar`
  scope.
* Mirror events never include attendees, descriptions, locations, or
  conferencing info, and are inserted/patched/deleted with
  `sendUpdates="none"` so no one is ever notified about them.
* No HTTP server is started; this is a polling-only, outbound-only service.
