# Supported Agents (technical reference)

Per-agent local storage layout, canonical id scheme, and write constraints. The README just lists the agents; this is where the implementation detail lives.

| Agent | Local storage | canonical id | Write constraints |
|-------|----------|-------------------|----------|
| Hermes | scans all archives under `%LOCALAPPDATA%\hermes` (POSIX: `~/.hermes`): `state.db` (default) + `profiles/<name>/state.db` (named profiles) (SQLite) | bare id (hermes profile stored in the `profile_name` column, agent attribution in `agent_type`) | SQLite transactions |
| DeepSeek Harness | `~/.codex/sessions/rollout-*.jsonl` | bare id | append-only; titles must also be appended to `session_index.jsonl`; the harness discovers new sessions via backfill |
| OpenCode (CLI only; desktop not supported) | `$XDG_DATA_HOME/opencode/opencode.db` (SQLite; `session`/`message`/`part` tables, shared by CLI & desktop) | bare id | SQLite writes; foreign sessions get a `ses_` id via idmap and round-trip with stable dedupe; `model` column written as `{id, providerID}` JSON |
| Reasonix | `%APPDATA%\reasonix\sessions\*.jsonl` | bare id (file stem) | append-only; sessions that are currently running (have a lock file) are skipped |
| OpenClaw | `~/.openclaw/agents/<id>/agent/openclaw-agent.sqlite` | bare id | schema auto-detection (experimental) |
| WorkBuddy | `~/.workbuddy/projects/<slug>/*.jsonl` + `workbuddy.db` | bare id (uuid) | JSONL append + SQLite upsert; cwd dir auto-created; written sessions appear after WorkBuddy restart (MIGRATE scan) |

Each Agent deploys its own MCP client instance (selected via `HERMES_SYNC_AGENT`); connect all of them to the same Workspace and they sync with each other. Adding a new Agent only requires implementing one adapter (see [ADDING_AGENT.md](ADDING_AGENT.md)) — zero changes to the server or the sync engine.