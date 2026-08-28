# Supported Agents (technical reference)

Per-agent local storage layout, canonical id scheme, and write constraints. The README just lists the agents; this is where the implementation detail lives.

| Agent | Local storage | canonical id | Write constraints |
|-------|----------|-------------------|----------|
| Hermes | scans all archives under `%LOCALAPPDATA%\hermes` (POSIX: `~/.hermes`): `state.db` (default) + `profiles/<name>/state.db` (named profiles) (SQLite) | bare id (hermes profile stored in the `profile_name` column, agent attribution in `agent_type`) | SQLite transactions |
| DeepSeek Harness | `~/.codex/sessions/rollout-*.jsonl` | bare id | append-only; titles must also be appended to `session_index.jsonl`; the harness discovers new sessions via backfill |
| OpenCode (CLI only; desktop not supported) | `$XDG_DATA_HOME/opencode/opencode.db` (SQLite; `session`/`message`/`part` tables, shared by CLI & desktop) | bare id | SQLite writes; foreign sessions get a `ses_` id via idmap and round-trip with stable dedupe; `model` column written as `{id, providerID}` JSON |
| Reasonix | `%APPDATA%\reasonix\sessions\*.jsonl` | bare id (file stem) | append-only; sessions that are currently running (have a lock file) are skipped |
| OpenClaw | `~/.openclaw/agents/<id>/sessions/sessions.json` + `<sessionId>.jsonl` (gateway session store; canonical id = transcript UUID, key kept in `meta.openclaw:session_key`) | bare id (UUID) | JSONL append + index update; gateway hot-reloads the index (mtime), a running gateway may overwrite `sessions.json` on its own writes, so re-pull after gateway session writes |
| Pi | `~/.pi/agent/sessions/`（`--<encoded-cwd>--/<timestamp>_<uuidv7>.jsonl`，JSONL v3 事件流；env `PI_CODING_AGENT_DIR` 可覆盖） | bare id (uuidv7) | append-only; 条目以 `parentId` 链式串接；`session_info` 条目写标题；同毫秒戳确定性 +1ms 消歧；分支消息按文件序线性化 |
| Oh My Pi | `~/.omp/agent/sessions/`（与 Pi 同构；env `OMP_CODING_AGENT_DIR` 可覆盖） | bare id (uuidv7) | append-only; `title_change`/`title` 条目写标题；其余与 Pi 一致（共享 `mcp/adapters/pi.py` 一个实现） |

Each Agent deploys its own MCP client instance (selected via `HERMES_SYNC_AGENT`); connect all of them to the same Workspace and they sync with each other. Adding a new Agent only requires implementing one adapter (see [ADDING_AGENT.md](ADDING_AGENT.md)) — zero changes to the server or the sync engine.