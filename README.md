# Agent Context Sync

> **简体中文**: [README.zh-CN.md](README.zh-CN.md) · **English**: this document

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/westsource/agentctxsync/actions/workflows/ci.yml/badge.svg)](https://github.com/westsource/agentctxsync/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](mcp/)

> Official website: https://www.agentctxsync.com

A complete solution for syncing sessions across devices and agents. Supports multi-user, multi-Workspace isolation with a PostgreSQL backend, and syncs automatically when an Agent starts via MCP Server.

## Features

- **Cross-agent sync**: Hermes / OpenAI Codex / opencode / Reasonix / OpenClaw / WorkBuddy share the same session pool. Every client pulls the **full pool** (all agents) and pushes everything it holds, so A's sessions can be pulled by B and written to its local storage; a session continued on another device only pushes its newly-added messages
- **Sub-agent folding**: Hermes sub-agent (delegated-task) conversations are merged into the main session at sync time — the main agent and its sub-agents surface as one conversation, and sub-agent messages carry a badge in the session viewer
- **Multi-tenancy**: multi-user + multi-Workspace isolation, each Workspace has its own API Key; admins manage users, invite codes and workspace metadata but **cannot read any user's sessions or messages**
- **Automatic sync**: incremental pull on startup with bootstrap push on first pairing, then periodic auto-sync; batched to avoid timeouts, lock-safe (single-writer), idempotent message dedup across devices
- **Quota enforcement**: per-user session-storage limits and Agent allowlists via `free` / `unlimited` plans; enforced server-side on new session writes, DB-driven (no restart), with an audit trail
- **Web admin UI**: bilingual (Simplified Chinese / English); overview, Workspace management, a unified **all-sessions** page, Markdown session viewer, trash, export/import, admin console
- **Project sync**: Hermes projects (sidebar project list) sync across devices along with sessions — merge by name + union of paths
- **Data safety**: one-click export (Markdown / JSON.gz) and import; sessions/messages can be soft-deleted (recoverable from the trash), pinned, and searched by title/content
- **Onboarding**: built-in setup help page with one-click download of the MCP client per Agent (install instructions + registration commands; the API Key stays a placeholder, so forwarding the package leaks nothing), plus guided onboarding for WorkBuddy and Reasonix (desktop JSON plugin registration with the required `env` block)
- **Client auto-update**: the client periodically pulls new versions from the server, verifies every file, then replaces itself atomically; takes effect after the Agent is restarted
- **Open registration**: invite codes are optional — registration is open by default; invite codes (optional expiry, revocable, shareable via `?code=` links) are available when you need controlled rollouts

## Screenshots

![Login page](docs/screenshots/01-login.png)

![Dashboard — workspace overview, sync status, quota and recent sessions](docs/screenshots/02-dashboard.png)

![All sessions — unified cross-workspace list with search and filters](docs/screenshots/03-all-sessions.png)

![Workspace detail — session list, projects and sync devices](docs/screenshots/04-workspace.png)

![Session viewer — Markdown rendering with code blocks](docs/screenshots/05-session-viewer.png)

## Supported Agents

| Agent | Local storage | canonical id | Write constraints |
|-------|----------|-------------------|----------|
| Hermes | scans all archives under `%LOCALAPPDATA%\hermes` (POSIX: `~/.hermes`): `state.db` (default) + `profiles/<name>/state.db` (named profiles) (SQLite) | bare id (hermes profile stored in the `profile_name` column, agent attribution in `agent_type`) | SQLite transactions |
| OpenAI Codex | `~/.codex/sessions/rollout-*.jsonl` | bare id | append-only; titles must also be appended to `session_index.jsonl`; codex discovers new sessions via backfill |
| OpenCode | `$XDG_DATA_HOME/opencode/storage/` (JSON files) | bare id | `.tmp` + rename atomic write; foreign sessions get a `ses_` id via idmap |
| Reasonix | `%APPDATA%\reasonix\sessions\*.jsonl` | bare id (file stem) | append-only; sessions that are currently running (have a lock file) are skipped |
| OpenClaw | `~/.openclaw/agents/<id>/agent/openclaw-agent.sqlite` | bare id | schema auto-detection (experimental) |
| WorkBuddy | `~/.workbuddy/projects/<slug>/*.jsonl` + `workbuddy.db` | bare id (uuid) | JSONL append + SQLite upsert; cwd dir auto-created; written sessions appear after WorkBuddy restart (MIGRATE scan) |

Each Agent deploys its own MCP client instance (selected via `HERMES_SYNC_AGENT`); connect all of them to the same Workspace and they sync with each other. Adding a new Agent only requires implementing one adapter (see [docs/ADDING_AGENT.md](docs/ADDING_AGENT.md)) — zero changes to the server or the sync engine.

## Quick Start

> All `<SERVER_IP>` below are placeholders — replace them with the address of your actual deployment.

### 1. Server deployment

```bash
# Upload to the target server and run
scp -r server/ scripts/ root@<SERVER_IP>:/tmp/hermes-sync/
ssh root@<SERVER_IP>
cd /tmp/hermes-sync/server
bash ../scripts/deploy-server.sh
```

After deployment:
- API: `http://<SERVER_IP>:8765/health`
- Web UI: `http://<SERVER_IP>:8765/web/`
- On first start, a default admin `admin` is created automatically (random password printed in the server logs; **forced change on first login**) along with its default workspace (including the API Key — see the server logs)

> Server Python dependencies: `fastapi` `uvicorn` `psycopg2-binary` `jinja2` `markdown` `python-multipart` (already included in `deploy-server.sh`; Markdown/Jinja2 are used for Web UI rendering, python-multipart for web form parsing).
>
> More detailed server deployment, operations, and backup instructions can be found in [docs/server-deployment.md](docs/server-deployment.md).

### 2. Register a user and create a Workspace (Web UI)

1. Open `http://<SERVER_IP>:8765/web/` and click Register
2. Registration is open by default — the invite code is optional. To gate registration, an admin creates invite codes on the Invites page (format `HSYNC-XXXXXXXX`, optional expiry and notes, revocable at any time); you can also copy a share link with the `?code=` parameter and send it directly to users
3. A "Default Workspace" is created automatically after successful registration; you can also create more workspaces by clicking "+ Create" on the overview page
4. Copy the API Key from the Workspace details page (format `ws_xxx`)

### 3. Local MCP deployment

**Method A (recommended)**: log in to the Web UI → Setup Help (`/web/help`) → download the archive for your Agent. Unpack and register it following the install instructions (README.md) inside the archive — replace `<YOUR_API_KEY>` in the registration command with the API Key of the corresponding workspace on the help page (the download package no longer pre-fills the Key, so forwarding the package won't leak it). Restart the Agent when done.

> **Reasonix (desktop)**: the archive/help page ships a JSON plugin registration for `Settings → MCP & Tools → Add Server → JSON`. The `env` block is **required** — without `HERMES_SYNC_AGENT` the client falls back to the hermes adapter (wrong store) and without `HERMES_SYNC_API_KEY` every call fails auth; `HERMES_SYNC_AUTO_UPDATE=0` is included so repo-deployed clients skip update checks. After restart the plugin auto-starts (`auto_start: true`), pulls incrementally ~8s after startup and syncs both ways every 300s. CLI equivalent: a `[[plugins]]` block in `config.toml` with the same `name/type/command/args/env/auto_start` fields.

**Method B (manual)**:

```bash
# Choose an agent (hermes | codex | OpenCode | reasonix | openclaw | workbuddy), default hermes
export HERMES_SYNC_AGENT=codex

# Set the workspace API key (format ws_xxx)
export HERMES_SYNC_API_KEY=ws_yourkeyhere

# One-click deploy (note: the script's default server address is a placeholder — set HERMES_SYNC_SERVER to your actual deployment address)
bash scripts/deploy-local-mcp.sh
```

> Each Agent deploys its own instance (one `HERMES_SYNC_AGENT` value and independent lock files each); point them all at the same Workspace API Key and they sync with each other. The onboarding flow for a new Agent is in [docs/ADDING_AGENT.md](docs/ADDING_AGENT.md).
>
> MCP client behavior: one incremental pull on startup; if the remote is empty (first pairing), it pushes local data automatically as a bootstrap; afterwards it auto-syncs every 300 seconds (adjustable via `HERMES_SYNC_INTERVAL`).

### 4. Migrate existing data (optional)

Push historical sessions from the local Hermes `state.db` to the remote server:

```bash
python scripts/migrate-local-to-server.py ws_yourkeyhere http://<SERVER_IP>:8765
```

## Configuration

### Server-side environment variables

| Variable | Description |
|------|------|
| `HERMES_SYNC_PG_DSN` | PostgreSQL connection string |
| `HERMES_SYNC_MASTER_KEY` | Master API key (not for sync) |
| `HERMES_SYNC_JWT_SECRET` | Web UI JWT signing secret |
| `HERMES_SYNC_TOKEN_EXPIRE` | JWT expiration (hours, default 24) |
| `HERMES_SYNC_PUBLIC_URL` | Canonical public address (e.g. `https://www.example.com`) baked into shipped client packages and shown on the help page; when unset, each client package defaults to the address the download request arrived on |

### Local MCP environment variables

| Variable | Default | Description |
|------|--------|------|
| `HERMES_SYNC_AGENT` | `hermes` | Local storage adapter: `hermes`/`codex`/`opencode`/`reasonix`/`openclaw`/`workbuddy` |
| `HERMES_SYNC_SERVER` | `http://<SERVER_IP>:8765` | Remote server address (set it to match your deployment) |
| `HERMES_SYNC_API_KEY` | - | **Workspace API Key** (required, format `ws_xxx`) |
| `HERMES_SYNC_INTERVAL` | `300` | Auto-sync interval (seconds) |
| `HERMES_SYNC_AUTO_SYNC` | `1` | Background auto-sync switch (`0` disables; manual tool calls still work) |
| `HERMES_SYNC_AUTO_UPDATE` | `1` | Client auto-update switch (`0` disables) |
| `HERMES_SYNC_UPDATE_INTERVAL` | `86400` | Update check interval (seconds, default 24 hours) |

## Quota (Optional)

- Users carry a `plan` (`free` / `unlimited`); registrations and invite codes grant `unlimited` by default, and admins can create invite codes granting `free`. The default `free` plan caps a user at 200 active sessions.
- Enforcement: `POST /push` gates **new** session writes only — an Agent allowlist plus the user-wide active session count. Updates to existing sessions and pulls are never blocked, so lowering a quota never breaks an already-synced pool. Rejections return 403 (`agent_not_allowed` / `quota_exceeded_sessions`) and are recorded in the audit log.
- Policy lives in the DB (`users.plan` + `quota_config`): an operator changes it and the next push applies it — no API coupling, no restart. When a deployment has no limited-plan invite path, the quota UI stays hidden; enforcement still applies if an operator configures limits.
- Ops SQL (adjusting limits, minimal-privilege read-only role) is in [docs/server-deployment.md](docs/server-deployment.md).

## Client Auto-Update

MCP clients have built-in auto-update: a check runs about 15 seconds after startup and then every 24 hours. New versions are pulled from the server via `/api/client/manifest` (version comparison) and `/api/client/download` (a zip with a SHA256 manifest), verified file by file, then **atomically replaced in place**, with a backup of the previous version kept (`.bak-<version>/`). The update **takes effect after the Agent is restarted** (the MCP server cannot restart itself and does not interrupt ongoing sessions).

- Disable: `HERMES_SYNC_AUTO_UPDATE=0`; adjust the interval: `HERMES_SYNC_UPDATE_INTERVAL`
- **Server default in shipped packages**: every downloaded client package has its `HERMES_SYNC_SERVER` code default set to the server that served the download (per-request address), or to `HERMES_SYNC_PUBLIC_URL` when that server-side variable is configured. To migrate existing clients to a new address: set `HERMES_SYNC_PUBLIC_URL` to the new address on the server and bump `CLIENT_VERSION` — clients pull the new package from the old address (keep it reachable until they all update) and switch after the agent restarts.
- If verification fails or the network is unreachable, the old files are kept and only a log entry is recorded; sync is unaffected
- Rollback: copy the files from `.bak-<version>/` back into the `mcp/` directory and delete `.hermes-sync-version`
- **Release workflow**: after modifying the client, bump the `CLIENT_VERSION` constant in both `mcp/updater.py` and `server/client_update.py`; once the server is deployed, all clients upgrade automatically at their next check

## Multi-Profile Sync (Hermes profiles)

The Hermes desktop app supports multiple profiles: each named profile is a **completely independent storage directory** with its own `state.db` (default at the platform root, named profiles in `<root>/profiles/<name>/`). The client **scans every profile under the platform root** and syncs them one by one — a single MCP instance covers all profiles on the machine without cross-contamination.

### Profile Discovery Rules

```
Platform root <root> (default platform locations only; HERMES_HOME / active_profile are not read)
├─ Windows: %LOCALAPPDATA%\hermes
└─ POSIX:   ~/.hermes

Scan:
├─ <root>/state.db                  → default profile (bare id, backward compatible)
└─ <root>/profiles/<name>/state.db  → named profiles (e.g. magic), sorted by name
```

- **MCP subprocesses do not inherit the profile**: Hermes' profile switching is implemented with an in-process ContextVar and is not passed to MCP server subprocesses via environment variables; the desktop app neither writes an `active_profile` marker nor passes `HERMES_HOME`. The client therefore **scans the `profiles/` directory** to discover all profiles instead of resolving the "currently active profile".
- **Single watermark**: the incremental pull watermark lives at the platform root in `<root>/.hermes-sync-watermark`, shared by all profiles; every pull is incremental based on the server's `last_synced_at`, so profiles never get mixed.

### Session IDs and Isolation

| Profile | canonical id | Description |
|------|--------------|----------|
| default | bare id (`20260808_180012_0c275f`) | backward compatible, behavior unchanged |
| non-default (e.g. magic) | `20260808_180012_0c275f` | the profile travels in the `profile_name` column; bare ids never collide because profiles are a column, not part of the id |

- **Zero server changes**: sessions of different profiles are distinguished purely by the id prefix, and the `profile_name` column is filled in with the profile name as a bonus.
- **Push merges everything**: on push, the client reads `state.db` from every local profile and merges them into a single list to report; each session carries its `profile_name` (bare ids throughout).
- **Pull routes per profile**: on pull, sessions are routed back to each profile's `state.db` by id prefix; sessions of profiles that don't exist locally (profiles unique to other machines) are skipped. Sessions from other agents are stored in the default profile with their canonical id intact — every client pulls the **full workspace pool** and pushes everything it holds (the server merges by canonical id; `agent_type` is preserved per session and messages dedupe by `(session_id, role, timestamp)`, so a foreign session continued locally pushes only its newly-added messages).

### Cross-Computer Sync Example

```
Machine A: default + magic profile     Machine B: default + magic profile

t1: A only has default; B only has default
    → bare-id sessions merge on both sides, same behavior as single-profile sync
t2: A creates the magic profile; B hasn't created it yet
    → A pushes sessions with `profile_name=magic`; B skips them on pull (no local magic profile)
t3: A and B both have the magic profile
    → magic sessions merge on both sides (profile_name=magic); default syncs as usual
```

> Note: the client only syncs profiles that **already exist locally**. When a profile exists only on a remote device, the local pull skips that profile's sessions (the watermark still advances); once you create a profile with the same name locally, its historical sessions can be pulled back from the server.

### Project Sync (projects.db)

Hermes desktop projects (the sidebar project list) are stored in a **per-profile `projects.db`** (`<profile-dir>/projects.db`), alongside `state.db`. The client walks every `projects.db` by profile to sync projects across devices:

- **Push**: reads `projects.db` from every local profile and merges them into a canonical list to push (ids are bare, the `profile` field carries the hermes profile).
- **Same-name merge**: for projects in the same workspace with the same `(profile, slug)` but different ids, the server merges them into the **earliest-created** project: folders are unioned, and a remap (`old_id → new_id`) is recorded so clients can converge.
- **Pull**: pulls remote projects plus remap records and routes them back to each profile's `projects.db` by id prefix; folders merge incrementally (new paths inserted, existing paths updated, nothing deleted), and slug conflicts get a de-duplicated suffix automatically.
- **Web session association**: the project list on the Web workspace page prefix-matches session `cwd` against project folders to show the sessions under each project (consistent with Hermes' native `project_for_path` logic; paths are per-machine and the Web shows the union).
- **Tools**: `project_push` / `project_pull` can be triggered manually; periodic sync (default 300s) also syncs projects along the way.

## Data Retention & Retrieval (Delete / Trash / Pinned / Search)

- **Delete / Restore (soft-hide)**: the Web session list and message detail views support "delete / restore"; data is **kept, not deleted** (a soft `hidden` flag), and fully reversible. Deleted items move to the **trash**: `/web/workspace/{id}/trash` (session trash) and `/web/workspace/{id}/session/{sid}/trash` (message trash), where they can be restored. Once deleted:
  - the server's `/pull` no longer delivers deleted sessions/messages (data stays on the server and is delivered again after restore);
  - `/push` does not reset the `hidden` flag when updating existing rows;
  - the session list and message viewer hide deleted items by default; the trash pages show them for viewing and restoring.
- **Pinned ordering**: the session list sorts pinned sessions first (`pinned`, 📌 marker). Currently used for display ordering only; there is no pin-management entry yet.
- **Search**: the Workspace session list `?q=` fuzzy-filters by title / id; the session detail page `?q=` fuzzy-filters by message content (LIKE wildcards are escaped to prevent injection).

## Server Migration

Server address priority: the `HERMES_SYNC_SERVER` environment variable in `config.yaml` > the default value in the `server.py` code (currently the deployed server address). The client auto-update ships the new default address along with the update.

**Seamless migration (recommended — keep the old server online until all clients have updated)**:
1. Deploy the new server (with the new default address) and bump `CLIENT_VERSION`
2. Keep the old server online (clients still need to pull updates from the old address)
3. Wait for each client to finish auto-updating (checked 15 seconds after startup / every 24 hours)
4. Clients connect to the new server automatically after the Agent is restarted
5. Once no client is still connected to the old server, take the old server offline

**When the old server goes offline directly**: clients can no longer pull updates from the old address and need manual handling — add `HERMES_SYNC_SERVER: http://new-address:8765` to the `env` section of `config.yaml` on each machine (environment variables take priority), or manually copy the new `server.py` into the `mcp/` directory.

> **Id-scheme upgrade (2026.08.18)**: canonical session ids are bare for
every agent — `agent_type` (sessions + messages) records the owning agent and
`profile_name` records the hermes profile (projects: `profile` column). Legacy
prefixed ids (`codex:...`, `magic:...`, ...) pushed by old clients are
normalized by the server's inbound shim, so mixed-version deployments work.
To migrate an existing database run `python scripts/migrate-id-scheme.py
--apply` (dry-run by default; collisions are reported and skipped, never
merged). After migration, hermes-profile and workbuddy sessions pull onto
Windows machines too (their bare ids are legal file names).

**Watermark follows the server identity**: the incremental pull watermark records which server it belongs to. When a client points at a different server (via env var or the updated default address), the watermark mismatch triggers a full re-pull automatically — a leftover watermark from the old server can never suppress sessions on the new one.

## MCP Tools

| Tool | Description |
|------|------|
| `sync_status` (alias `hermes_sync_status`) | View sync status (remote session/message counts, per-device last sync time) |
| `sync_pull` (alias `hermes_sync_pull`) | Pull sessions from remote to local (params: `limit`, default 50; `full` — ignore the watermark and pull everything; background incremental pulls page through everything based on the watermark) |
| `sync_push` (alias `hermes_sync_push`) | Push local sessions to remote (auto-batching with session count + message count dual limits to avoid large-request timeouts) |
| `sync_full` (alias `hermes_sync_full`) | Full sync (push first, then pull) |
| `project_push` | Push projects from all local profiles' projects.db to remote (same-name merging handled by the server) |
| `project_pull` | Pull projects from remote into the local projects.db (applies remap, routes per profile) |

> `sync_*` are neutral tool names (common to all Agents); `hermes_sync_*` are compatibility aliases, so existing Hermes registrations are unaffected.

Background behavior:
- One automatic **incremental** pull at startup (delayed 8 seconds to avoid the host agent's startup/read peak; local `.hermes-sync-watermark` watermark + 5-minute clock tolerance); if the remote is empty, local data is pushed automatically (first-pairing bootstrap for new devices)
- Periodic auto-sync (default 300 seconds)
- **Watermark bound to the server identity**: pointing the client at a different server automatically triggers a full re-pull (see Server Migration)
- **Batching**: pulls page in small batches (15 sessions per request); pushes split by session-count + message-count dual limits — large syncs never time out
- **Pull write retry**: when the local store is locked by the host agent, the pull write retries a few times (each attempt fails fast with a 5 s busy_timeout)
- Single-writer lock: with two `serve` instances, only one process runs background sync, avoiding local storage races; auto-update uses a separate update lock
- Message dedup is based on the `(session_id, role, timestamp)` triple, idempotent across devices
- Background sync completion sends an MCP log notification (`notifications/message`, logger `hermes-sync`) to the host agent; whether the host surfaces it in the UI is host-dependent — the Web UI is the guaranteed visibility channel

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system architecture, multi-tenancy model, database schema, API reference
- [docs/server-deployment.md](docs/server-deployment.md) — server deployment, operations, backup, quota SQL
- [docs/ADDING_AGENT.md](docs/ADDING_AGENT.md) — adding a new Agent adapter

## Compatibility with Hermes 0.20+ (SQLite Lock Contention)

**Symptom**: the Hermes desktop app reports `request timed out after 30s: session.resume`.

**Root cause**: the SQLite 3.50.4 bundled with Hermes has a WAL-reset corruption bug (see the `hermes doctor` / errors.log warnings), so Hermes forcibly falls back to `journal_mode=DELETE` — in that mode **any write transaction blocks concurrent readers**. The MCP client's background sync (startup pull, periodic sync) writes directly to Hermes' `state.db`; while it holds the write lock, Hermes' own `session.resume` (reading `state.db`) keeps waiting and errors out after the desktop's 30-second RPC timeout (shown as `database is locked` in errors.log).

**Mitigations already in place in this client**:
- The SQLite write connection's `busy_timeout` is shortened to 5 seconds: if the lock can't be acquired it fails immediately and defers to the next sync cycle — it never holds or waits on a lock for long
- The startup auto-pull is delayed by 8 seconds to avoid the read peak of Hermes startup and session resume
- Background pulls are **incremental** (local `.hermes-sync-watermark` watermark + 5-minute tolerance) instead of a full rescan every time
- New `HERMES_SYNC_AUTO_SYNC=0` fully disables background auto-sync (manual tool calls still work), completely eliminating lock contention with Hermes

**Recommendation (root fix)**: run `hermes update` to upgrade Hermes' bundled SQLite to 3.51.3+ (or `hermes doctor` to repair the embedded runtime); once WAL concurrency is restored, reads and writes no longer block each other and the mitigations above are no longer needed.

## Known Issues & Troubleshooting

| Symptom | Cause | Resolution |
|------|------|------|
| Sync won't push (server `total_sessions` doesn't grow) | The old server returns 500 for columns added in Hermes 0.20 (e.g. `system_prompt_hash`) | Upgrade the server to a version with column filtering; the client recovers automatically next cycle |
| `request timed out after 30s: session.resume/create` | Hermes 0.20 SQLite lock contention (see previous section) | Upgrade SQLite or set `HERMES_SYNC_AUTO_SYNC=0` |
| Sync fails with `UNIQUE constraint failed: sessions.title` | Hermes 0.20+ enforces a partial unique index on `sessions.title` (`WHERE title IS NOT NULL`); sessions in the shared pool can carry the same auto-generated title | Client now disambiguates colliding titles with a ` (N)` suffix on pull (mirroring the desktop app); update the client |
| Authentication failure after registering the downloaded package | `<YOUR_API_KEY>` was not replaced with a real Key | Copy the Key for the corresponding workspace from the onboarding help page |
| Server session/message count keeps growing for reasonix sessions | The reasonix desktop normalizes local transcripts (strips timestamps, prepends its system prompt), so the `(role, timestamp)` dedupe triple no longer matches and every periodic push re-inserts the same messages | Server-side content-level dedupe now covers reasonix (same treatment as hermes' message-alternation repair); update the server |
| Reasonix local transcript grows forever | Same normalization breaks the local pull dedupe, re-appending identical messages | The reasonix adapter dedupes pulled writes by content as well; update the client |
| Foreign session titles revert to the bare id on the server | A pulled session re-pushed by reasonix carried the local-id title fallback | The adapter no longer sends the fallback title for foreign sessions (server keeps its own); update the client |
| After the id-scheme upgrade, `magic:`/`workbuddy:` sessions were not pullable on Windows | Prefixed ids contain `:` — invalid Windows file names (silently become NTFS alternate data streams) | Canonical ids are now bare for every agent (see the Id-scheme upgrade note) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — how to set up a dev environment, code style, i18n rules, and the PR process.

## License

[MIT](LICENSE) © 2026 道荣（黄超）、露（张渊） · [中文版](LICENSE.zh-CN.md)
