# Supported Agents (technical reference)

Per-agent local storage layout, canonical id scheme, and write constraints. The README just lists the agents; this is where the implementation detail lives.

| Agent | Local storage | canonical id | Write constraints |
|-------|----------|-------------------|----------|
| Hermes | scans all archives under `%LOCALAPPDATA%\hermes` (POSIX: `~/.hermes`): `state.db` (default) + `profiles/<name>/state.db` (named profiles) (SQLite) | bare id (hermes profile stored in the `profile_name` column, agent attribution in `agent_type`) | SQLite transactions |
| DeepSeek Harness | `~/.codex/sessions/rollout-*.jsonl` | bare id | append-only; titles must also be appended to `session_index.jsonl`; the harness discovers new sessions via backfill |
| OpenCode (CLI & desktop 1.x) | `$XDG_DATA_HOME/opencode/opencode.db` (SQLite; `session`/`message`/`part` tables, shared by CLI & desktop) | bare id | SQLite writes in the desktop row shape (id prefixes `ses_`/`msg_`/`prt_`, ms timestamps, `project_id` resolved from the directory, unique slug); foreign sessions get a `ses_` id via idmap and round-trip with stable dedupe; `model` column written as `{id, providerID}` JSON |
| Reasonix | `%APPDATA%\reasonix\sessions\*.jsonl` | bare id (file stem) | append-only; sessions that are currently running (have a lock file) are skipped |
| OpenClaw | `~/.openclaw/agents/<id>/sessions/sessions.json` + `<sessionId>.jsonl` (gateway session store; canonical id = transcript UUID, key kept in `meta.openclaw:session_key`) | bare id (UUID) | JSONL append + index update; gateway hot-reloads the index (mtime), a running gateway may overwrite `sessions.json` on its own writes, so re-pull after gateway session writes |
| Oh My Pi | `~/.omp/agent/sessions/`（env `OMP_CODING_AGENT_DIR` 可覆盖） | bare id (uuidv7) | append-only; `title_change`/`title` 条目写标题；实现见 `mcp/adapters/omp.py` |

Each Agent deploys its own MCP client instance (selected via `HERMES_SYNC_AGENT`); connect all of them to the same Workspace and they sync with each other. Adding a new Agent only requires implementing one adapter (see [ADDING_AGENT.md](ADDING_AGENT.md)) — zero changes to the server or the sync engine.