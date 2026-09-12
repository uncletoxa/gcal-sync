# gcal-sync

Self-hosted, bidirectional **availability** synchronization across two or
more Google calendars (e.g. a Workspace calendar and a personal calendar —
or any number of accounts/calendars beyond that).

It does **not** copy event titles, descriptions, attendees, locations, or
conferencing links. It only creates private "Busy" placeholder events on
every other calendar so all configured calendars accurately reflect your
combined availability.

## What it does

* Polls all configured calendars on an interval (default: every 2 minutes)
  using Google Calendar's incremental sync (`syncToken`), so it doesn't
  re-download the whole calendar every pass. Each calendar's events are
  fetched exactly once per pass no matter how many other calendars it's
  synced against.
* For every real event on any configured calendar, ensures a private,
  content-free "Busy" block exists at the same time on **every other**
  configured calendar. With N calendars, that's an N-way fan-out (each
  calendar mirrors busy blocks from all N-1 others), not just a single pair.
* Moves/resizes/deletes on the source event are propagated to all of its mirrors.
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
  auth.py           # OAuth loopback (Desktop app) flow + file-based credential storage
  web_auth.py       # OAuth Web-application flow + DB-backed credential storage for web sign-ups
  crypto.py         # Fernet encrypt/decrypt for credentials stored in the DB
  google_client.py  # Google Calendar API client (retryable) + CalendarClient interface
  db.py             # SQLite persistence: sync tokens, event mappings, tenants, connected accounts
  sync_engine.py    # core bidirectional sync logic (pure, testable without the API)
  cli.py            # `gcal-sync` command group (auth, calendars, sync, start, web)
  web/              # Flask app: self-serve sign-up/dashboard for multi-tenant use
tests/              # pytest suite; sync engine is tested against a fake in-memory client
deploy/             # Dockerfile / docker-compose (works with Podman), a systemd unit example,
                    # and a Caddyfile for the optional web app's reverse proxy
```

## 1. Google Cloud project / API setup

1. Go to the Google Cloud Console and create a project (or reuse one you
   control). You only need **one** OAuth client: the same Desktop OAuth
   client is used to authorize every account you want to sync (each account
   just produces its own separate refresh token).
2. Enable the **Google Calendar API** for the project
   (APIs & Services → Library → "Google Calendar API" → Enable).
3. Configure the **OAuth consent screen**:
   * User type: "Internal" if the project lives under your Workspace org and
     **every** account you're syncing belongs to that same org (Internal apps
     don't need Google's verification review, but Google will flat-out refuse
     to authorize any account outside that org — no test-user list can work
     around this). Otherwise choose "External" and add every Google account
     you plan to sync as a **test user** — apps in "Testing" mode work
     indefinitely for test users without verification.
   * Scopes: you don't need to add scopes on the consent screen itself for a
     Desktop app / testing setup; the app requests them directly (see below).
   * **Syncing accounts across multiple orgs/projects?** gcal-sync only takes
     one `GOOGLE_CLIENT_SECRETS_FILE` for every account (see step 2 below), so
     if you already have OAuth clients in several Cloud projects (e.g. two
     separate Workspace orgs plus a personal project), you can't just pick any
     of them — an Internal client can only authorize its own org's accounts.
     Use (or create) a client whose consent screen is **External**, and add
     every account you're syncing (across all orgs, plus personal) as test
     users on that one project. A project created under a personal Gmail
     account is always External by default (Internal isn't offered outside a
     Workspace org), which makes it a natural choice for this case.

## 2. OAuth client setup

1. APIs & Services → Credentials → Create Credentials → OAuth client ID.
2. Application type: **Desktop app** — this is required, not just a
   preference. `gcal-sync auth` (see `src/gcal_sync/auth.py`) runs Google's
   *loopback* OAuth flow: it starts a temporary local web server on a random
   free port and has Google redirect back to `http://localhost:<that port>`
   after you approve. Google only allows redirecting to arbitrary localhost
   ports like this for **Desktop app** (installed app) clients. A **Web
   application** client would require pre-registering one fixed redirect URI
   in Cloud Console, which doesn't work with a randomly-chosen port each run
   — the flow would fail at the redirect step.
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
CALENDARS=
POLL_INTERVAL_SECONDS=120
FULL_RESYNC_INTERVAL_HOURS=24
SYNC_WINDOW_DAYS=14
LOG_LEVEL=INFO
```

`CALENDARS` is a comma-separated list of `<account>:<calendar_id>` pairs —
one entry per calendar you want kept in sync. Pick any account names you
like (e.g. `workspace`, `personal`, `team`); they just need to match the
`--account` value you use in the `auth`/`calendars` commands below. At least
2 entries are required; any number (3, 4, ...) is supported. Leave it blank
until step 6 below (calendar discovery) — `auth` and `calendars` don't need
it.

## 4. Authorize each account

For every account you listed (or plan to list) in `CALENDARS`, run:

```bash
uv run gcal-sync auth --account <name>
```

e.g. for a 3-calendar setup:

```bash
uv run gcal-sync auth --account workspace
uv run gcal-sync auth --account personal
uv run gcal-sync auth --account team
```

Each run opens a browser (loopback redirect to `http://localhost:<random-port>`,
no public callback needed) — sign in with the corresponding Google account
and approve. The resulting refresh token is written to
`data/token_<name>.json` with `0600` permissions.

If you're on a headless machine without a local browser, use
`--no-browser` and open the printed URL yourself (e.g. via an SSH tunnel:
`ssh -L <port>:localhost:<port> <remote-host>`).

You will not need to log in again after this; `gcal-sync` refreshes access
tokens automatically using the stored refresh tokens.

## 5. Find calendar IDs

```bash
uv run gcal-sync calendars --account <name>
```

Run once per account. Each line is `<calendar id>  <summary>  <PRIMARY if
applicable>`. Usually you want the `PRIMARY` calendar for each account — its
ID is the account's email address. Put the results into `.env` as
`CALENDARS`, e.g.:

```
CALENDARS=workspace:you@company.com,personal:you@gmail.com,team:abc123@group.calendar.google.com
```

## 6. First dry run

```bash
uv run gcal-sync sync --dry-run
```

This performs one real read-only pass — it reads every configured calendar
for real but does not create/update/delete anything or persist sync tokens —
and logs what it *would* do (`mirror_created` / `mirror_updated` /
`mirror_deleted` / `event_skipped`). Check the output looks sane, then run it
for real:

```bash
uv run gcal-sync sync
```

`sync` performs a single pass and exits (good for cron-style scheduling, or
just to sanity check things). `start` runs the same logic continuously:

```bash
uv run gcal-sync start
```

## 7. Run continuously

### Option A — Podman (recommended if you have Docker/Podman available)

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

`deploy/gcal-sync.service` runs `uv run gcal-sync start` directly on the
host. It assumes the checkout lives at `~/gcal-sync` and that `uv` is
installed at `~/.local/bin/uv` (the default for `uv`'s own installer) —
edit `WorkingDirectory`/`ExecStart` if either differs on your machine.
It deliberately doesn't rely on `uv` being on `$PATH`, since systemd user
services start with a minimal environment and don't source your shell rc
files.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/gcal-sync.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gcal-sync
```

For a system-wide unit instead (`/etc/systemd/system/`), replace `%h` with
the absolute paths for whichever user should run it.

If this is a headless/always-on machine, make sure the service keeps
running after you log out or reboot: `loginctl enable-linger $USER`.

## CLI reference

| Command | Purpose |
|---|---|
| `gcal-sync auth --account <name> [--no-browser]` | One-time OAuth authorization for an account (`<name>` must match a key in `CALENDARS`) |
| `gcal-sync calendars --account <name>` | List calendars visible to an authorized account (to find IDs) |
| `gcal-sync sync [--dry-run]` | Run a single synchronization pass — the legacy `CALENDARS` group (if configured) plus every web-signed-up tenant's own calendars — and exit |
| `gcal-sync start [--dry-run]` | Run continuously, polling every `POLL_INTERVAL_SECONDS` |
| `gcal-sync web [--host] [--port] [--debug]` | Run the web sign-up app (dev server; use gunicorn in production — see "Web sign-up for a team" below) |

## Web sign-up for a team

For letting other people connect their own calendars self-serve (rather than you
running `gcal-sync auth` on their behalf), gcal-sync has a small Flask app where
someone signs in with Google and connects two or more of *their own* calendars.
Availability only ever syncs **within one person's own connected calendars** — there
is no cross-person mesh; person A's calendars are never compared against person B's.

This is entirely additive: your existing `CALENDARS`/`gcal-sync auth` setup (if any)
keeps working unchanged and continues to run alongside web-signed-up tenants in the
same `sync`/`start` process.

### Why this needs a second OAuth client

`gcal-sync auth` uses Google's *loopback* flow (a Desktop app OAuth client, redirecting
to a random `localhost` port) — that only works for a human running a CLI on the same
machine as their browser. A hosted sign-up page needs Google to redirect back to your
server instead, which requires a **Web application** OAuth client with one fixed,
pre-registered redirect URI. You need both client types configured in the same Cloud
project; they're independent and don't conflict.

1. Cloud Console → APIs & Services → Credentials → Create Credentials → OAuth client ID.
2. Application type: **Web application**.
3. Authorized redirect URI: `https://<your-domain>/oauth/callback` — must match
   `WEB_BASE_URL` exactly (scheme + host, no trailing slash on the base).
4. Download the JSON, save it as e.g. `data/web_client_secret.json`, and point
   `GOOGLE_WEB_CLIENT_SECRETS_FILE` at it.
5. On the OAuth consent screen (Branding page), fill in App name / support email /
   developer contact / home page / privacy policy — required once you have more than a
   handful of sign-ups, see "Publishing to production" below.

### Configuration

Set in `.env` (see `.env.example` for the full block):

```
GOOGLE_WEB_CLIENT_SECRETS_FILE=data/web_client_secret.json
WEB_BASE_URL=https://gcal.yourdomain.com
WEB_SECRET_KEY=<python -c "import secrets; print(secrets.token_hex(32))">
TOKEN_ENCRYPTION_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
```

Web-connected users' OAuth tokens are stored as encrypted rows in `data/state.sqlite3`
(unlike the CLI flow's plaintext-but-0600 `data/token_<name>.json` files) — a shared DB
holding several people's tokens is a bigger blast radius than one file per person, so
they're encrypted at rest with `TOKEN_ENCRYPTION_KEY`. Back that key up somewhere
separate from the DB itself; losing it makes those rows undecryptable (CLI-flow tokens
in `data/token_*.json` are unaffected either way).

### Restricting sign-up to a Workspace domain

Set `ALLOWED_DOMAIN` (e.g. `ALLOWED_DOMAIN=company.com`, no leading `@`) to only allow
Google accounts on that domain to create a new account. This only gates *brand-new*
sign-ups — an existing user can still connect a second, non-domain calendar afterwards
(e.g. their personal Gmail), which is the point of this app. Two things happen when it's
set: Google's account chooser is hinted to that domain via the `hd` parameter on the
*first* sign-in only (a UI convenience, not enforcement on its own), and the server
independently verifies the connecting account's email domain before creating the
tenant — so it's enforced even if someone bypasses the hint. Leave it blank to allow any
Google account to sign up (subject to whatever the OAuth consent screen's user
restrictions already impose).

### Running it

Development:

```bash
uv run gcal-sync web --debug
```

Production (behind a reverse proxy that terminates TLS): see `deploy/docker-compose.yml`,
which adds a `web` service (gunicorn) and a `caddy` service for automatic HTTPS — point
your domain's DNS at the host, edit `deploy/Caddyfile` with your actual domain, then:

```bash
podman compose -f deploy/docker-compose.yml up -d
```

The poller (`gcal-sync` service, `start` command) picks up newly-connected tenants
automatically on its next pass — no restart needed.

### Publishing to production (avoiding the 7-day token expiry)

While the OAuth consent screen is in **Testing** status, every grant — including your
team's — expires 7 days after consent, and only up to 100 manually-added test users can
sign in at all. For a real rollout, flip the consent screen to **In production** (Cloud
Console → OAuth consent screen / Audience). This does **not** require Google's full
verification review for a small internal rollout — it just requires the Branding page
to be complete (app name, support email, developer contact, home page, privacy policy).
Users will see a one-time "Google hasn't verified this app → Advanced → Go to
gcal-sync" click during sign-in unless you complete full verification, but grants no
longer expire on a 7-day cycle. This app's own `/` and `/privacy` routes
satisfy the home page / privacy policy links Google requires — point the
Branding page at your deployed instance's URLs.

## Deploying to a fresh VM (Podman)

End-to-end checklist for standing up gcal-sync — including the web sign-up
app for letting other people connect their own calendars — on a new VM you
don't already have configured. Assumes a systemd-based Linux VM (commands
below are Debian/Ubuntu; substitute your distro's package manager) and a
domain you can point at it.

### 1. Provision & install prerequisites

```bash
ssh <you>@<vm-host>
sudo apt update && sudo apt install -y git podman podman-compose
curl -LsSf https://astral.sh/uv/install.sh | sh   # only needed for the one-time auth/calendars steps below
```

### 2. Clone and install

```bash
git clone <repo-url> ~/gcal-sync && cd ~/gcal-sync
uv sync
cp .env.example .env
```

### 3. Google Cloud / OAuth setup

Do this once, from your laptop: follow "1. Google Cloud project / API
setup" and "2. OAuth client setup" above to create the **Desktop** OAuth
client (needed for `gcal-sync auth`, e.g. if you're also syncing your own
calendars via `CALENDARS`), then "Web sign-up for a team" → "Why this needs
a second OAuth client" to create the **Web application** OAuth client
(needed for the sign-up flow), using `https://<your-domain>/oauth/callback`
as its redirect URI.

### 4. Transfer secrets to the VM

```bash
scp client_secret.json <you>@<vm-host>:~/gcal-sync/data/
scp web_client_secret.json <you>@<vm-host>:~/gcal-sync/data/
```

### 5. Configure `.env`

Fill in both the base config ("3. Configure `.env`" above) and the web
sign-up block (see "Web sign-up for a team" → Configuration):
`GOOGLE_CLIENT_SECRETS_FILE`, `GOOGLE_WEB_CLIENT_SECRETS_FILE`,
`WEB_BASE_URL=https://<your-domain>`, `WEB_SECRET_KEY`,
`TOKEN_ENCRYPTION_KEY`. Leave `CALENDARS` blank until the next step.

### 6. Authorize your own account(s), if any

If you're also syncing your own calendars via the legacy `CALENDARS` group
(not just other people's via web sign-up), run "4. Authorize each account"
through "6. First dry run" above on the VM using `uv run` — the loopback
OAuth server binds to a random port, so this must run directly on the host
(via `uv run`), not inside a container. Skip this entirely if you're only
offering the web sign-up flow to others.

### 7. DNS and firewall

* Point the domain's A/AAAA record at the VM's public IP.
* Open inbound **80 and 443** in the VM's cloud firewall/security group —
  this is separate from any OS-level firewall (e.g. `ufw`) and is commonly
  missed. Caddy needs both ports to obtain and renew its Let's Encrypt
  certificate.
* Edit `deploy/Caddyfile`, replacing `gcal.yourdomain.com` with your real
  domain.

### 8. Build and run

```bash
podman build -t gcal-sync -f deploy/Dockerfile .
podman compose -f deploy/docker-compose.yml up -d   # or: podman-compose -f deploy/docker-compose.yml up -d
```

This starts all three services defined in `deploy/docker-compose.yml`:
`gcal-sync` (the poller), `web` (gunicorn), and `caddy` (TLS termination +
reverse proxy). Newly web-signed-up tenants are picked up by the poller
automatically on its next pass — no restart needed.

### 9. Verify

```bash
podman logs -f gcal-sync
podman logs -f gcal-sync-web
```

Visit `https://<your-domain>` and confirm the sign-up flow completes end to
end.

### 10. Back up

Back up `~/gcal-sync/data/` regularly (tokens + SQLite state), and also
back up `.env` separately — it holds `TOKEN_ENCRYPTION_KEY` and
`WEB_SECRET_KEY`, which aren't stored in `data/` but are required to
decrypt web-signed-up users' tokens and to keep existing sessions valid.

## Sync behavior notes

* **Sync window**: only events starting or ending within `[now, now +
  SYNC_WINDOW_DAYS]` (default 14 days) are mirrored. A full resync queries
  Google directly with that range. An incremental (sync-token) pass can't be
  time-bounded server-side — Google rejects combining a sync token with
  `timeMin`/`timeMax` — so out-of-window events are filtered out client-side
  instead; cancellations always pass through so a deleted source event still
  clears its mirror. Once a mirrored event's window has passed, its mirror is
  *not* proactively deleted — cleanup only happens if the source event itself
  is cancelled or becomes non-blocking.
* **N-way fan-out**: with more than 2 calendars configured, every calendar
  is synced against every other one (`accounts * (accounts - 1)` directional
  pairs per pass). A busy block on calendar A is mirrored onto B, C, D, ...
  independently. Each source calendar's events are still only fetched once
  per pass — the fan-out only affects how many destinations that one fetch
  is applied to. This fan-out is scoped to one sync group at a time: the
  legacy `CALENDARS` group, or (independently) one web-signed-up tenant's own
  connected calendars — never across groups/tenants.
* **Loop prevention**: every mirror event is created with
  `extendedProperties.private.gcalSyncMirror = "true"` plus source
  identifiers. Any event carrying that flag is skipped entirely when
  scanning for *source* events — so a mirror is never mirrored back, even
  across 3+ calendars (a mirror on calendar B is never re-mirrored onto C).
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
* **Missing configuration errors on `sync`/`start`** — `CALENDARS` must be
  set in `.env` with at least 2 `<account>:<calendar_id>` entries (see step
  3/5 above).
* **`Invalid CALENDARS entry`** — each entry must be `<account>:<calendar_id>`,
  comma-separated between entries; check for stray commas or missing colons.
* Structured JSON logs go to stdout; grep for `"event": "authentication_error"`
  or `"event": "api_error"` to find failures. Logs never contain tokens,
  secrets, or original event titles/descriptions/attendees.

## Backup / restore SQLite state

All persistent state lives under `data/` (gitignored):

* `data/client_secret.json` — your OAuth client (not a secret you generated,
  but treat it as sensitive)
* `data/token_<account>.json` (one per account in `CALENDARS`) — refresh tokens
* `data/state.sqlite3` (+ `-wal`/`-shm` while running) — sync tokens and
  event mappings

To back up: stop the service, copy the whole `data/` directory somewhere
safe. To restore: stop the service, replace `data/` with the backup, start
again — the service will resume incremental sync from the stored tokens (or
fall back to a full resync if the stored SQLite state is older/missing).

If you lose `data/state.sqlite3` but keep the tokens, that's safe too: the
next run has no sync tokens, so it performs a full resync of every configured
calendar and rebuilds the mapping table from scratch (it will not duplicate mirrors
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
* The core sync poller (`gcal-sync start`/`sync`) starts no HTTP server; it's
  polling-only and outbound-only. The optional `gcal-sync web` app (see "Web
  sign-up for a team") does listen on a port, since a hosted OAuth sign-up
  flow requires one — only run it if you're actually offering self-serve
  sign-up.
* Web-signed-up users' OAuth tokens are stored encrypted (Fernet,
  `TOKEN_ENCRYPTION_KEY`) as rows in `data/state.sqlite3`, since a shared DB
  holding multiple people's tokens is a bigger blast radius than the CLI
  flow's one-token-file-per-account model.
