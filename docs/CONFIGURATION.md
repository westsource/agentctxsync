# Configuration

Reference for the environment variables used by the server and the local MCP client. Most users only ever need two: `HERMES_SYNC_SERVER` and `HERMES_SYNC_API_KEY` on the client.

## Server-side environment variables

| Variable | Description |
|------|------|
| `HERMES_SYNC_PG_DSN` | PostgreSQL connection string |
| `HERMES_SYNC_MASTER_KEY` | Master API key (not for sync) |
| `HERMES_SYNC_JWT_SECRET` | Web UI JWT signing secret |
| `HERMES_SYNC_TOKEN_EXPIRE` | JWT expiration (hours, default 24) |
| `HERMES_SYNC_PUBLIC_URL` | Canonical public address (e.g. `https://www.example.com`) baked into shipped client packages and shown on the help page; when unset, each client package defaults to the address the download request arrived on |

## Local MCP environment variables

| Variable | Default | Description |
|------|--------|------|
| `HERMES_SYNC_AGENT` | `hermes` | Local storage adapter: `hermes`/`deepseek-harness`/`opencode`/`reasonix`/`openclaw`/`workbuddy` |
| `HERMES_SYNC_SERVER` | `https://www.agentctxsync.com` | Remote server address (the shipped client points at the public server; self-hosted deployments must set it to their own address) |
| `HERMES_SYNC_API_KEY` | - | **Workspace API Key** (required, format `ws_xxx`) |
| `HERMES_SYNC_INTERVAL` | `300` | Auto-sync interval (seconds) |
| `HERMES_SYNC_AUTO_SYNC` | `1` | Background auto-sync switch (`0` disables; manual tool calls still work) |
| `HERMES_SYNC_AUTO_UPDATE` | `1` | Client auto-update switch (`0` disables) |
| `HERMES_SYNC_UPDATE_INTERVAL` | `3600` | Update check interval (seconds, default 1 hour) |

## Server vs client address priority

Server address priority: the `HERMES_SYNC_SERVER` environment variable in `config.yaml` > the default value in `server.py` code. The client auto-update ships the new default address along with the update.