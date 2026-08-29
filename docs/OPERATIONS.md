# Operations & Advanced Usage

This document covers the operational and advanced topics split out of the main README: quota enforcement, client auto-update, multi-profile sync, data retention, server migration, the MCP tools in detail, the Hermes SQLite-lock compatibility note, and a troubleshooting table.

- [Quota (Optional)](#quota-optional)
- [Client Auto-Update](#client-auto-update)
- [Multi-Profile Sync (Hermes profiles)](#multi-profile-sync-hermes-profiles)
- [Data Retention & Retrieval](#data-retention--retrieval-delete--trash--pinned--search)
- [Migrating existing local data](#migrating-existing-local-data)
- [Server Migration](#server-migration)
- [Sync Tools (background behavior)](#sync-tools-background-behavior)
- [Compatibility with Hermes 0.20+ (SQLite Lock Contention)](#compatibility-with-hermes-020-sqlite-lock-contention)
- [Known Issues & Troubleshooting](#known-issues--troubleshooting)

## Quota (Optional)

- Users carry a `plan` (`free` / `unlimited`); registration without an invite code grants `free`, registration with an invite grants the invite's plan (default `unlimited`), and admins can create invite codes granting `free`. The default `free` plan caps a user at 300 active sessions.
- Enforcement: `POST /push` gates **new** session writes only — an Agent allowlist plus the user-wide active session count. Updates to existing sessions and pulls are never blocked, so lowering a quota never breaks an already-synced pool. Rejections return 403 (`agent_not_allowed` / `quota_exceeded_sessions`) and are recorded in the audit log.
- Policy lives in the DB (`users.plan` + `quota_config`): an operator changes it and the next push applies it — no API coupling, no restart. The quota UI (sidebar usage, invite grant-plan controls) shows whenever a limited plan is reachable — a free-granting invite or existing `free` users (default registration grants `free`, so it appears once anyone registers); enforcement still applies when the UI is hidden.
- Ops SQL (adjusting limits, minimal-privilege read-only role) is in [server-deployment.md](server-deployment.md).

## Client Auto-Update

MCP clients have built-in auto-update: a check runs about 1 minute after startup and then every 1 hour. New versions are pulled from the server via `/api/client/manifest` (version comparison) and `/api/client/download` (a zip with a SHA256 manifest), verified file by file, then **atomically replaced in place**, with a backup of the previous version kept (`.bak-<version>/`). The update **takes effect after the Agent is restarted** (the MCP server cannot restart itself and does not interrupt ongoing sessions).

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

## Migrating existing local data

Already been using Hermes and want your history on the server too? Push historical sessions from the local `state.db` to the remote server:

```bash
python scripts/migrate-local-to-server.py ws_yourkeyhere http://<SERVER_IP>:8765
```

This is optional — new clients bootstrap locally-held data onto the server on first pairing; this script just uploads existing history proactively.

## Server Migration

Server address priority: the `HERMES_SYNC_SERVER` environment variable in `config.yaml` > the default value in the `server.py` code (currently the deployed server address). The client auto-update ships the new default address along with the update.

**Seamless migration (recommended — keep the old server online until all clients have updated)**:
1. Deploy the new server (with the new default address) and bump `CLIENT_VERSION`
2. Keep the old server online (clients still need to pull updates from the old address)
3. Wait for each client to finish auto-updating (checked 1 minute after startup / every 1 hour)
4. Clients connect to the new server automatically after the Agent is restarted
5. Once no client is still connected to the old server, take the old server offline

**When the old server goes offline directly**: clients can no longer pull updates from the old address and need manual handling — add `HERMES_SYNC_SERVER: http://new-address:8765` to the `env` section of `config.yaml` on each machine (environment variables take priority), or manually copy the new `server.py` into the `mcp/` directory.

> **Id-scheme upgrade (2026.08.18)**: canonical session ids are bare for
> every agent — `agent_type` (sessions + messages) records the owning agent and
> `profile_name` records the hermes profile (projects: `profile` column). Legacy
> prefixed ids (`codex:...`, `magic:...`, ...) pushed by old clients are
> normalized by the server's inbound shim, so mixed-version deployments work.
> To migrate an existing database run `python scripts/migrate-id-scheme.py
> --apply` (dry-run by default; collisions are reported and skipped, never
> merged). After migration, hermes-profile and workbuddy sessions pull onto
> Windows machines too (their bare ids are legal file names).

**Watermark follows the server identity**: the incremental pull watermark records which server it belongs to. When a client points at a different server (via env var or the updated default address), the watermark mismatch triggers a full re-pull automatically — a leftover watermark from the old server can never suppress sessions on the new one.

## Sync Tools (background behavior)

The full tool list lives in the README; details of the background engine:

- One automatic **incremental** pull at startup (delayed 8 seconds to avoid the host agent's startup/read peak; local `.hermes-sync-watermark` watermark + 5-minute clock tolerance); if the remote is empty, local data is pushed automatically (first-pairing bootstrap for new devices)
- Periodic auto-sync (default 300 seconds)
- **Watermark bound to the server identity**: pointing the client at a different server automatically triggers a full re-pull (see Server Migration)
- **Batching**: pulls page in small batches (15 sessions per request); pushes split by session-count + message-count + byte-size (default 8 MB) limits — large syncs never time out; a single oversized session rides alone in its own chunk, and a failed chunk (413 / quota / timeout) no longer aborts the rest of the push cycle
- **Push-side watermark (session fingerprint)**: each session records a push fingerprint `(message_count, max_timestamp[, file mtime])` in a sidecar next to the field-meta file; periodic push skips sessions whose fingerprint is unchanged (the fingerprint updates only after a successful push), so a large store no longer re-uploads everything every cycle. Agents without field-meta fall back to full push (previous behavior). mtime is provided by the adapter where available (workbuddy implements it, so a pull that touched a copy also invalidates its fingerprint)
- **Pull write retry**: when the local store is locked by the host agent, the pull write retries a few times (each attempt fails fast with a 5 s busy_timeout)
- Single-writer lock: with two `serve` instances, only one process runs background sync, avoiding local storage races; auto-update uses a separate update lock
- Message dedup is based on the `(session_id, role, timestamp)` triple, idempotent across devices
- Background sync completion sends an MCP log notification (`notifications/message`, logger `hermes-sync`) to the host agent; whether the host surfaces it in the UI is host-dependent — the Web UI is the guaranteed visibility channel

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