# Configuration

Reference for the environment variables used by the server and the local MCP client. Most users only ever need two: `HERMES_SYNC_SERVER` and `HERMES_SYNC_API_KEY` on the client.

Everything below is read once at process start (`server/config.py`, module-level `os.environ.get` in the client). Secrets come from the environment only — the server refuses to start when a required one is missing, and `server/.env.example` is the template.

## Server-side environment variables

| Variable | Required | Description |
|------|------|------|
| `HERMES_SYNC_PG_DSN` | ✅ | PostgreSQL connection string (the schema is created/migrated on startup) |
| `HERMES_SYNC_MASTER_KEY` | ✅ | Master API key: a sync-API credential that bypasses the workspace/quota checks (never hand it to a client) |
| `HERMES_SYNC_JWT_SECRET` | recommended | Web UI JWT signing secret (`openssl rand -hex 32`); when unset a random one is generated per process, which logs everyone out on restart |
| `HERMES_SYNC_TOKEN_EXPIRE` | | JWT expiration in hours (default `24`) |
| `HERMES_SYNC_PUBLIC_URL` | | Canonical public address (e.g. `https://www.example.com`) baked into shipped client packages and shown on the help page; when unset, each client package defaults to the address the download request arrived on |
| `HERMES_SYNC_ANNOUNCEMENTS_URL` | | Public JSON feed for the in-app announcement banner (absolute `http(s)` URL). Unset = banner off. The **browser** fetches it; the server never calls out |
| `HERMES_SYNC_SMTP_HOST` | | SMTP host — setting host + user + password + from turns the email-verification feature on |
| `HERMES_SYNC_SMTP_PORT` | | SMTP port (default `465`, implicit TLS) |
| `HERMES_SYNC_SMTP_USER` | | SMTP account |
| `HERMES_SYNC_SMTP_PASSWORD` | | SMTP password / authorization code |
| `HERMES_SYNC_SMTP_FROM` | | Sender address shown in the mail |
| `HERMES_SYNC_MAIL_DAILY_CAP` | | Outbound-mail ceiling per calendar day, counted in the DB (`mail_stats`) so a restart cannot refill it (default `200`; `0` disables the cap, not recommended) |

Deployment scripts additionally read `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` (see `server/.env.example`) to build the DSN.

When SMTP is not configured the whole email feature is dormant: registration and login behave exactly as before, no activation mail is required, and `/web/security` (the security hub) redirects to the dashboard.

## Local MCP environment variables

| Variable | Default | Description |
|------|--------|------|
| `HERMES_SYNC_AGENT` | `hermes` | Local storage adapter: `hermes`/`dsh`/`opencode`/`reasonix`/`openclaw`/`workbuddy`/`omp` |
| `HERMES_SYNC_SERVER` | `https://www.agentctxsync.com` | Remote server address (the shipped client points at the public server; self-hosted deployments must set it to their own address) |
| `HERMES_SYNC_API_KEY` | - | **Workspace API Key** (required, format `ws_xxx`) |
| `HERMES_SYNC_INTERVAL` | `300` | Auto-sync interval (seconds) |
| `HERMES_SYNC_AUTO_SYNC` | `1` | Background auto-sync switch (`0` disables the startup pull + periodic sync; manual tool calls still work) |
| `HERMES_SYNC_AUTO_UPDATE` | `1` | Client auto-update switch (`0` disables) |
| `HERMES_SYNC_UPDATE_INTERVAL` | `3600` | Update check interval (seconds, default 1 hour) |
| `HERMES_SYNC_TOOL_LOCK_WAIT_S` | `20` | Seconds a mutating sync tool waits for the cross-process sync lock before returning a "busy" hint (lock shared with the background cycle and across client copies) |
| `HERMES_SYNC_LOCK_FILE` | derived per agent | Override the sync lock file path (useful when several agents share one machine) |
| `HERMES_SYNC_UPDATE_LOCK_FILE` | derived per agent | Override the auto-update lock file path |

## Server vs client address priority

Server address priority: the `HERMES_SYNC_SERVER` environment variable in `config.yaml` > the default value in the client code (the shipped client is built with the deployment's public address). The client auto-update ships the new default address along with the update, which is how a deployment moves its clients to a new domain.

## Where the client keeps its local state

Sidecars live next to the agent's store (each agent-scoped file is prefixed with its `agent_type`, so several agents on one machine never collide). All of them are safe to delete — the cost is a full re-pull / re-push:

| File | Purpose |
|------|---------|
| `.{agent}-sync-watermark` | Pull watermark; recorded together with the server identity it belongs to, so switching servers triggers a full pull |
| `.{agent}-sync-field-meta.json` | Field-level optimistic-concurrency sidecar (`base`/`val` per user-edit field) |
| `.{agent}-sync-field-meta-push-fingerprint.json` | Push fingerprint sidecar (unchanged sessions are skipped; delete it to force a full re-push) |
| `.{agent}-sync-foreign.json` | Owner registry for sessions pulled from other agents (keeps their `agent_type` on re-push) |
| `.{agent}-projects-field-meta.json` | Same as the field sidecar, for the project store |
| `.hermes-sync-version` (in `mcp/`) | Installed client version, written by the updater |
