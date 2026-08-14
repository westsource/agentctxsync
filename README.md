# Agent Contexts Sync

> **简体中文**: [README.zh-CN.md](README.zh-CN.md) · **English**: this document

> Official website: http://www.agentctxsync.com

A complete solution for syncing sessions across devices and agents. Supports multi-user, multi-Workspace isolation with a PostgreSQL backend, and syncs automatically when an Agent starts via MCP Server.

Key features:
- **Multi-tenancy**: multi-user + multi-Workspace isolation, each Workspace has its own API Key
- **Cross-Agent sync**: Hermes / OpenAI Codex / opencode / Reasonix / OpenClaw share the same session pool; A's sessions can be pulled by B and written to its local storage
- **Web admin UI**: login/registration (invite code), overview, Workspace management, session viewer, admin console
- **Data safety**: one-click export (Markdown / JSON.gz) and import of sessions/workspaces
- **Project sync**: Hermes projects (sidebar project list) sync across devices along with sessions — merge by name + union of paths
- **Data retention & retrieval**: sessions/messages can be soft-hidden (reversible), pinned, and searched by title/content
- **Setup help**: built-in setup help page with one-click download of the MCP client per Agent (includes install instructions and registration commands)
- **Client auto-update**: the client periodically pulls new versions from the server, verifies every file, then replaces itself automatically; takes effect after restarting the Agent
- **i18n**: bilingual UI in Simplified Chinese / English

## Supported Agents

| Agent | Local storage | canonical id prefix | Write constraints |
|-------|----------|-------------------|----------|
| Hermes | scans all archives under `%LOCALAPPDATA%\hermes` (POSIX: `~/.hermes`): `state.db` (default) + `profiles/<name>/state.db` (named profiles) (SQLite) | none (bare id, compatible with existing data); non-default profiles get a `<profile>:` prefix | SQLite transactions |
| OpenAI Codex | `~/.codex/sessions/rollout-*.jsonl` | `codex:` | append-only; titles must also be appended to `session_index.jsonl`; codex discovers new sessions via backfill |
| opencode | `$XDG_DATA_HOME/opencode/storage/` (JSON files) | `opencode:` | `.tmp` + rename atomic write; foreign sessions get a `ses_` id via idmap |
| Reasonix | `%APPDATA%\reasonix\sessions\*.jsonl` | `reasonix:` | append-only; sessions that are currently running (have a lock file) are skipped |
| OpenClaw | `~/.openclaw/agents/<id>/agent/openclaw-agent.sqlite` | `openclaw:` | schema auto-detection (experimental) |

Each Agent deploys its own MCP client instance (selected via `HERMES_SYNC_AGENT`); connect all of them to the same Workspace and they sync with each other. Adding a new Agent only requires implementing one adapter (see [docs/ADDING_AGENT.md](docs/ADDING_AGENT.md)) — zero changes to the server or the sync engine.

## Architecture

```
+-----------------------------------------------------------------------------+
|                             本地设备 (电脑 A/B/...)                          |
|                                                                             |
|  +-----------------------------------------------------------------------+  |
|  |                        Agent App (Hermes/Codex/...)                   |  |
|  |  +--------------+    stdio     +----------------------------------+    |  |
|  |  |  Agent Core  | <----------> |  MCP Server (server.py)          |    |  |
|  |  +--------------+              |  hermes-session-sync             |    |  |
|  |                                |                                  |    |  |
|  |  +------------------------+    |  Tools: sync_status/pull/push/   |    |  |
|  |  |  本地存储 (per agent)  |    |  full (+ hermes_sync_* 别名)      |    |  |
|  |  |  state.db / jsonl /    |<-->|                                  |    |  |
|  |  |  SQLite / JSON files   | R/W|  Adapters: hermes/codex/opencode |    |  |
|  |  +------------------------+    |  /reasonix/openclaw              |    |  |
|  |                                |                                  |    |  |
|  |  +------------------------+    |  Background Tasks:               |    |  |
|  |  |  config.yaml           |    |  * startup auto-pull (增量)      |    |  |
|  |  |  mcp_servers:          |    |  * bootstrap push (首次配对)      |    |  |
|  |  |    hermes-sync:        |    |  * periodic sync (5min)          |    |  |
|  |  |      env:              |    |  * auto-update (24h, 校验+替换)   |    |  |
|  |  |        HERMES_SYNC_... |    |  * 单写者锁/更新锁 (双实例安全)    |    |  |
|  |  +------------------------+    +--------------+-------------------+    |  |
|  |  +------------------------+                   |                        |  |
|  |  |  .hermes-sync-watermark| (增量拉取水位线)   | HTTP/8765              |  |
|  |  |  .hermes-sync-version  | (自动更新版本)     | (Workspace API Key)    |  |
|  |  +------------------------+                   |                        |  |
|  +-----------------------------------------------+------------------------+  |
+--------------------------------------+----------------------------------------+
                                       |
              push / pull / manifest / download (JSON over HTTP, Bearer: ws_xxx)
                                       |
+--------------------------------------+----------------------------------------+
|                     远程服务器 (自建部署)                                       |
|                                                                              |
|  +-----------------------------------------------------------------------+  |
|  |                FastAPI Server (server.py :8765)                        |  |
|  |                                                                        |  |
|  |  Web UI (/web/*)          REST API (/api/*)       Sync API             |  |
|  |  * 登录 / 注册(邀请码)      * Auth (login/me)      * GET /health        |  |
|  |  * 信息概览 / 工作空间     * Workspace CRUD       * POST /pull          |  |
|  |  * 会话查看器 (Markdown)   * Admin (users/ws)     * POST /push          |  |
|  |  * 导出 / 导入             * Change Password      * GET /status/{dev}   |  |
|  |  * 接入帮助 + 客户端下载    * register (管理员)    * GET /sessions      |  |
|  |  * Admin (用户/空间/邀请)                        * GET /users          |  |
|  |                                                                        |  |
|  |  Client Update API                                                      |  |
|  |  * GET /api/client/manifest (版本对比+sha256)                           |  |
|  |  * GET /api/client/download (带 manifest 的 zip)                       |  |
|  |                                                                        |  |
|  |  Auth: JWT (cookie)        Auth: JWT (header)     Auth: API Key (ws_)  |  |
|  |  i18n: zh-CN / en                                                        |  |
|  +----------------------------------+-------------------------------------+  |
|                                     |                                        |
|  +----------------------------------v-------------------------------------+  |
|  |  PostgreSQL (agentctxsync DB)                                        |  |
|  |  * users       (id, username, password_hash, is_admin, is_active)    |  |
|  |  * workspaces  (id, name, user_id FK, api_key)                       |  |
|  |  * invites     (邀请码注册: code, used, revoked, expires_at)          |  |
|  |  * sessions    PK (workspace_id, id) + agent_type/meta               |  |
|  |  * messages    PK (workspace_id, session_id, id) + agent_type/meta   |  |
|  |  * sync_state  PK (device_id, workspace_id)                          |  |
|  |  * projects    PK (workspace_id, id) + folders/remap                 |  |
|  +----------------------------------------------------------------------+  |
|                                                                              |
|  +---------------------+  +----------------------+  +---------------------+  |
|  |  systemd service     |  |  Docker Compose       |  |  Cron Backup        |  |
|  |  hermes-sync.service |  |  * postgres (pg18)    |  |  每天 3:00 AM       |  |
|  |  (auto-restart)      |  |    (pgvector 扩展)    |  |  pg_dump -> gz      |  |
|  +---------------------+  +----------------------+  |  保留 7 天          |  |
|                                                      +---------------------+  |
+------------------------------------------------------------------------------+
```

### Multi-tenancy model

```
User (admin / user)
 |
 +-- Workspace "Personal"  (api_key: ws_xxx)
 |    +-- Sessions / Messages / SyncState
 |    +-- Device A, Device B (same workspace = full sync)
 |
 +-- Workspace "Work"      (api_key: ws_yyy)
      +-- Sessions / Messages / SyncState
      +-- Device C (isolated from Personal workspace)
```

- **Users**: new users self-register with an invite code issued by an admin (admins can also create accounts directly)
- **Workspaces**: each user can create multiple workspaces, each with its own API key
- **Isolation**: sessions and messages are fully isolated between different workspaces
- **Same workspace**: all devices under the same workspace sync completely

## Quick Start

> All `<SERVER_IP>` below are placeholders — replace them with the address of your actual deployment.

### 1. Server deployment

```bash
# 上传到目标服务器并执行
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
2. Registration requires an **invite code**: an admin creates invite codes on the Admin → Invite Management page (format `HSYNC-XXXXXXXX`), with optional expiry date and notes, revocable at any time; you can also copy a share link with the `?code=` parameter and send it directly to users
3. A "Default Workspace" is created automatically after successful registration; you can also create more workspaces by clicking "+ Create" on the overview page
4. Copy the API Key from the Workspace details page (format `ws_xxx`)

### 3. Local MCP deployment

**Method A (recommended)**: log in to the Web UI → Setup Help (`/web/help-hermes`) → download the archive for your Agent. Unpack and register it following the install instructions (README.md) inside the archive — replace `<YOUR_API_KEY>` in the registration command with the API Key of the corresponding workspace on the help page (the download package no longer pre-fills the Key, so forwarding the package won't leak it). Restart the Agent when done.

**Method B (manual)**:

```bash
# 选择 agent（hermes | codex | opencode | reasonix | openclaw），默认 hermes
export HERMES_SYNC_AGENT=codex

# 设置 workspace API key（格式 ws_xxx）
export HERMES_SYNC_API_KEY=ws_yourkeyhere

# 一键部署（注意：脚本内默认服务器地址为占位符，请设置 HERMES_SYNC_SERVER 为实际部署地址）
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

### Local MCP environment variables

| Variable | Default | Description |
|------|--------|------|
| `HERMES_SYNC_AGENT` | `hermes` | Local storage adapter: `hermes`/`codex`/`opencode`/`reasonix`/`openclaw` |
| `HERMES_SYNC_SERVER` | `http://<SERVER_IP>:8765` | Remote server address (set it to match your deployment) |
| `HERMES_SYNC_API_KEY` | - | **Workspace API Key** (required, format `ws_xxx`) |
| `HERMES_SYNC_INTERVAL` | `300` | Auto-sync interval (seconds) |
| `HERMES_SYNC_AUTO_SYNC` | `1` | Background auto-sync switch (`0` disables; manual tool calls still work) |
| `HERMES_SYNC_AUTO_UPDATE` | `1` | Client auto-update switch (`0` disables) |
| `HERMES_SYNC_UPDATE_INTERVAL` | `86400` | Update check interval (seconds, default 24 hours) |

## Client Auto-Update

MCP clients have built-in auto-update: a check runs about 15 seconds after startup and then every 24 hours. New versions are pulled from the server via `/api/client/manifest` (version comparison) and `/api/client/download` (a zip with a SHA256 manifest), verified file by file, then **atomically replaced in place**, with a backup of the previous version kept (`.bak-<version>/`). The update **takes effect after the Agent is restarted** (the MCP server cannot restart itself and does not interrupt ongoing sessions).

- Disable: `HERMES_SYNC_AUTO_UPDATE=0`; adjust the interval: `HERMES_SYNC_UPDATE_INTERVAL`
- If verification fails or the network is unreachable, the old files are kept and only a log entry is recorded; sync is unaffected
- Rollback: copy the files from `.bak-<version>/` back into the `mcp/` directory and delete `.hermes-sync-version`
- **Release workflow**: after modifying the client, bump the `CLIENT_VERSION` constant in both `mcp/server.py` and `server/server.py`; once the server is deployed, all clients upgrade automatically at their next check

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
| non-default (e.g. magic) | `magic:20260808_180012_0c275f` | prefix matches the `agent_type:` style; the server's composite primary key `(workspace_id, id)` distinguishes them naturally |

- **Zero server changes**: sessions of different profiles are distinguished purely by the id prefix, and the `profile_name` column is filled in with the profile name as a bonus.
- **Push merges everything**: on push, the client reads `state.db` from every local profile — default sessions keep their bare ids, named-profile sessions carry a `<profile>:` prefix — and merges them into a single list to report.
- **Pull routes per profile**: on pull, sessions are routed back to each profile's `state.db` by id prefix; sessions of profiles that don't exist locally (profiles unique to other machines) or of non-hermes agents are skipped and not written locally.

### Cross-Computer Sync Example

```
Machine A: default + magic profile     Machine B: default + magic profile

t1: A only has default; B only has default
    → bare-id sessions merge on both sides, same behavior as single-profile sync
t2: A creates the magic profile; B hasn't created it yet
    → A pushes magic:-prefixed sessions; B skips them on pull (no local magic profile)
t3: A and B both have the magic profile
    → magic sessions merge on both sides (ids all carry the magic: prefix); default syncs as usual
```

> Note: the client only syncs profiles that **already exist locally**. When a profile exists only on a remote device, the local pull skips that profile's sessions (the watermark still advances); once you create a profile with the same name locally, its historical sessions can be pulled back from the server.

### Project Sync (projects.db)

Hermes desktop projects (the sidebar project list) are stored in a **per-profile `projects.db`** (`<profile-dir>/projects.db`), alongside `state.db`. The client walks every `projects.db` by profile to sync projects across devices:

- **Push**: reads `projects.db` from every local profile and merges them into a canonical list to push (default profile ids are bare ids, named profiles are `<profile>:<id>`).
- **Same-name merge**: for projects in the same workspace with the same `(profile, slug)` but different ids, the server merges them into the **earliest-created** project: folders are unioned, and a remap (`old_id → new_id`) is recorded so clients can converge.
- **Pull**: pulls remote projects plus remap records and routes them back to each profile's `projects.db` by id prefix; folders merge incrementally (new paths inserted, existing paths updated, nothing deleted), and slug conflicts get a de-duplicated suffix automatically.
- **Web session association**: the project list on the Web workspace page prefix-matches session `cwd` against project folders to show the sessions under each project (consistent with Hermes' native `project_for_path` logic; paths are per-machine and the Web shows the union).
- **Tools**: `project_push` / `project_pull` can be triggered manually; periodic sync (default 300s) also syncs projects along the way.

## Data Retention & Retrieval (Hidden / Pinned / Search)

- **Soft-hide**: the Web session list and message detail views support "hide / restore"; data is **kept, not deleted**, and fully reversible. Once hidden:
  - the server's `/pull` no longer delivers hidden sessions/messages (data stays on the server and is delivered again after restore);
  - `/push` does not reset the `hidden` flag when updating existing rows;
  - the Web hides hidden items by default; the "show hidden" toggle reveals them temporarily for viewing and restoring.
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

## MCP Tools

| Tool | Description |
|------|------|
| `sync_status` (alias `hermes_sync_status`) | View sync status (remote session/message counts, per-device last sync time) |
| `sync_pull` (alias `hermes_sync_pull`) | Pull sessions from remote to local (param: limit, default 50; background incremental pulls page through everything based on the watermark) |
| `sync_push` (alias `hermes_sync_push`) | Push local sessions to remote (auto-batching to avoid large-request timeouts) |
| `sync_full` (alias `hermes_sync_full`) | Full sync (push first, then pull) |
| `project_push` | Push projects from all local profiles' projects.db to remote (same-name merging handled by the server) |
| `project_pull` | Pull projects from remote into the local projects.db (applies remap, routes per profile) |

> `sync_*` are neutral tool names (common to all Agents); `hermes_sync_*` are compatibility aliases, so existing Hermes registrations are unaffected.

Background behavior:
- One automatic **incremental** pull at startup (local `.hermes-sync-watermark` watermark + 5-minute clock tolerance); if remote is empty, local data is pushed automatically (first-pairing bootstrap for new devices)
- Periodic auto-sync (default 300 seconds)
- Single-writer lock: with two `serve` instances, only one process runs background sync, avoiding local storage races; auto-update uses a separate update lock
- Message dedup is based on the `(session_id, role, timestamp)` triple, idempotent across devices

## API Endpoints

### Sync API (API Key auth, format `ws_xxx`)
```
GET  /health                    # 健康检查
POST /pull                      # 拉取会话（limit/offset 分页、last_sync_at 增量、agent 过滤）
POST /push                      # 推送会话（upsert + 消息去重；按服务端真实列过滤；agent_type/meta）
GET  /status/{device_id}        # 同步状态（设备最近同步时间、会话/消息总数）
GET  /sessions                  # 列出会话（最近 50 条，含 agent_type）
GET  /users                     # 列出同步设备
```

### Projects API (API Key auth, format `ws_xxx`)
```
POST /api/projects/push   # 推送项目 + folders（同 (profile, slug) 合入最早项目并记录 remap）
POST /api/projects/pull   # 拉取项目 + folders + remap（不含已隐藏项目）
```

### Client Update API (API Key auth)
```
GET  /api/client/manifest?agent=X&v=本地版本   # 版本对比 + 每文件 sha256/size
GET  /api/client/download?agent=X             # 客户端 zip（内嵌 manifest.json）
```

### REST API (JWT auth)
```
POST /api/auth/login            # 登录获取 JWT
POST /api/auth/register         # 创建用户（需管理员 JWT）
GET  /api/me                    # 当前用户信息
POST /api/me/change-password    # 修改密码
GET  /api/workspaces            # 列出我的 workspace
POST /api/workspaces            # 创建 workspace
DELETE /api/workspaces/{id}     # 删除 workspace
POST /api/workspaces/{id}/regen-key  # 重新生成 API key
```

### Admin API (admin JWT)
```
GET  /api/admin/users           # 所有用户
POST /api/admin/users/{uid}/toggle   # 启用/禁用用户
GET  /api/admin/workspaces      # 所有 workspace
```

### Web UI (browser access)
```
GET  /web/                      # 信息概览
GET  /web/login                 # 登录页面
GET  /web/register              # 注册页面（需邀请码，支持 ?code= 预填）
GET  /web/logout                # 登出
GET  /web/change-password       # 修改密码页（首次登录强制改密时跳转至此）
POST /web/change-password       # 修改密码
POST /web/update-profile        # 更新个人资料（显示名/密码/管理员标志）
GET  /web/set-language/{lang}   # 切换语言（zh-CN / en）
GET  /web/workspace/{id}        # Workspace 详情（会话列表：置顶/排序/分页/搜索/隐藏开关/Agent 徽章/项目列表）
GET  /web/workspace/{id}/session/{sid}            # 会话消息查看器（Markdown 渲染、消息搜索、隐藏/恢复）
GET  /web/workspace/{id}/session/{sid}/export     # 导出单个会话为 Markdown
GET  /web/workspace/{id}/export                   # 导出整个 Workspace 为 JSON.gz
POST /web/workspace/{id}/import                   # 导入 Workspace 备份（JSON/JSON.gz）
POST /web/workspace/{id}/regen-key                # 重新生成 API key
GET  /web/workspace/{id}/delete                   # 删除 Workspace
POST /web/workspace/{id}/session/{sid}/hide           # 隐藏会话（可逆，/pull 停止下发）
POST /web/workspace/{id}/session/{sid}/unhide         # 恢复会话
POST /web/workspace/{id}/session/{sid}/message/{mid}/hide     # 隐藏消息
POST /web/workspace/{id}/session/{sid}/message/{mid}/unhide   # 恢复消息
GET  /web/help-hermes                             # 接入帮助页（MCP 客户端接入帮助）
GET  /web/download/mcp-client?ws_id={id}&agent=X  # 下载 MCP 客户端 zip（Key 为占位符）
GET  /web/admin/users                             # 用户管理
POST /web/admin/user/create                       # 创建用户
GET  /web/admin/user/{uid}/edit                   # 编辑用户
POST /web/admin/user/{uid}/edit                   # 提交用户编辑（显示名/密码/管理员）
GET  /web/admin/user/{uid}/toggle                 # 启用/禁用用户
GET  /web/admin/workspaces                        # 所有空间管理
GET  /web/admin/invites                           # 邀请管理
POST /web/admin/invite/create                     # 创建邀请码（有效期/备注）
POST /web/admin/invite/{id}/revoke                # 撤销邀请码
```

## Database Schema

### users
`id`, `username`, `password_hash`, `display_name`, `is_admin`, `is_active`, `created_at`, `last_login_at`

### workspaces
`id`, `name`, `user_id` (FK), `api_key`, `description`, `created_at`, unique constraint `(user_id, name)`

### invites
`id`, `code` (format `HSYNC-XXXXXXXX`), `created_by` (FK), `used`, `used_by`, `revoked`, `expires_at`, `note`, `created_at`

### sessions
Composite primary key: `(workspace_id, id)`; foreign key: `workspace_id -> workspaces(id) ON DELETE CASCADE`
Multi-Agent extension columns: `agent_type` (default `hermes`; existing data is automatically classified as hermes), `meta` (JSONB, carries agent-specific fields)
Data retention/ordering extension columns: `hidden`/`hidden_at` (soft-hide, reversible), `pinned` (pin-to-top ordering), `profile_name` (source profile)

### messages
Composite primary key: `(workspace_id, session_id, id)`; foreign key: `workspace_id -> workspaces(id) ON DELETE CASCADE`
Multi-Agent extension columns: `agent_type`, `meta` (JSONB); soft-hide columns: `hidden`/`hidden_at`

### sync_state
Primary key: `(device_id, workspace_id)`; foreign key: `workspace_id -> workspaces(id) ON DELETE CASCADE`

### projects
Composite primary key: `(workspace_id, id)` (canonical id: bare id for the default profile, `<profile>:<id>` for named profiles)
Columns: `slug` (unique per (workspace, profile); the basis for same-name merging), `name`, `description`, `icon`, `color`,
`board_slug`, `primary_path`, `created_at`, `archived`, `hidden`/`hidden_at`, `merged_into`, `agent_type`

### project_folders
Composite primary key: `(workspace_id, project_id, path)`; columns: `label`, `is_primary`, `added_at`;
cross-device incremental merge (new paths inserted, existing paths updated)

### project_remap
Composite primary key: `(workspace_id, old_id)`; column: `new_id` (an old_id → new_id routing record after same-name merging, so clients can converge)

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
| Client never updates | `HERMES_SYNC_AUTO_UPDATE=0` or server unreachable | Check the `Update check` log lines in `mcp-stderr.log` |
| Authentication failure after registering the downloaded package | `<YOUR_API_KEY>` was not replaced with a real Key | Copy the Key for the corresponding workspace from the onboarding help page |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — how to set up a dev environment, code style, i18n rules, and the PR process.

## License

[MIT](LICENSE) © 2026 道荣（黄超）、露（张渊）
