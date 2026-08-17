# 架构（Architecture）

> 本文档描述 Agent Context Sync 的系统架构、多租户模型、数据模型与 API 参考。
> 功能、特点与使用说明见 [README.zh-CN.md](../README.zh-CN.md) / [README.md](../README.md)。
> 服务端部署、运维与备份见 [server-deployment.md](server-deployment.md)。

## 总体架构

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
|  |  |  本地存储 (per agent)  |    |  full, project_push/pull         |    |  |
|  |  |  state.db / jsonl /    |<-->|  (+ hermes_sync_* 兼容别名)        |    |  |
|  |  |  SQLite / JSON files   | R/W|  Adapters: hermes/codex/opencode |    |  |
|  |  +------------------------+    |  /reasonix/openclaw/workbuddy  |    |  |
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
|  |  * 登录 / 注册(邀请可选)  * Auth (login/me)      * GET /health        |  |
|  |  * 信息概览 / 全部会话     * Workspace CRUD       * POST /pull          |  |
|  |  * 工作空间 / 会话查看器   * Admin (users/ws)     * POST /push          |  |
|  |  * 回收站 / 导出 / 导入    * Change Password      * GET /status/{dev}   |  |
|  |  * 接入帮助 + 客户端下载   * register (管理员)    * GET /sessions      |  |
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
|  |  * users       (id, username, password_hash, is_admin, plan)        |  |
|  |  * workspaces  (id, name, user_id FK, api_key)                       |  |
|  |  * invites     (邀请码(可选): code, used, revoked, expires_at)          |  |
|  |  * sessions    PK (workspace_id, id) + agent_type/meta               |  |
|  |  * messages    PK (workspace_id, session_id, id) + agent_type/meta   |  |
|  |  * sync_state  PK (device_id, workspace_id)                          |  |
|  |  * projects    PK (workspace_id, id) + folders/remap                 |  |
|  |  * quota_config / audit_log (配额策略/审计)                          |  |
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

### 组件说明

- **本地设备**：每个 Agent 独立部署一个 MCP Server 实例（`HERMES_SYNC_AGENT` 选择适配器），
  通过 stdio 与 Agent 通信，读写该 Agent 的本地存储（state.db / jsonl / SQLite / JSON）。
  水位线文件 `.hermes-sync-watermark` 绑定服务器身份（切换服务器自动全量重拉），
  `.hermes-sync-version` 记录客户端自动更新版本。
- **远程服务器**：单个 FastAPI 进程承载 Web UI、REST API、Sync API 与客户端更新 API；
  认证分三层——Web UI 用 JWT（Cookie）、REST 用 JWT（Header）、Sync/更新 API 用
  Workspace API Key（`ws_xxx`）；界面内置 zh-CN / en 双语。
- **部署形态**：systemd 服务（自动重启）+ Docker Compose（PostgreSQL pg18 + pgvector 扩展）+
  Cron 每日备份（pg_dump → gz，保留 7 天）。

## 多租户模型

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

- **Users**：新用户自助注册，默认开放、邀请码可选（填写则正常核销并授予对应套餐）；
  管理员也可直接创建账号
- **Workspaces**：每个用户可创建多个 workspace，每个 workspace 有独立 API key
- **隔离**：不同 workspace 之间的会话和消息完全隔离；同一 workspace 下的所有设备完全同步
- **管理员权限边界**：管理员可管理用户、邀请码与全局工作区（元数据与开关），但
  **无法查看任何用户的空间内容、会话或消息**——空间数据（会话列表、消息内容、项目）
  仅对所属用户可见，管理页面只暴露元数据与管理操作
- **配额**：每个用户带 `plan`（`free` / `unlimited`），配额策略存于数据库
  （`quota_config`），push 时按「用户全局活跃会话数 + Agent 白名单」对新建会话执法

## 数据模型（Database Schema）

### users
`id`, `username`, `password_hash`, `display_name`, `is_admin`, `is_active`,
`plan`（`free` / `unlimited`）, `must_change_password`, `lang`, `created_at`, `last_login_at`

### workspaces
`id`, `name`, `user_id` (FK), `api_key`, `description`, `created_at`, 唯一约束 `(user_id, name)`

### invites
`id`, `code` (格式 `HSYNC-XXXXXXXX`), `created_by` (FK), `used`, `used_by`, `revoked`,
`expires_at`, `note`, `grant_plan`（注册授予的套餐）, `created_at`

### sessions
复合主键: `(workspace_id, id)`; 外键: `workspace_id -> workspaces(id) ON DELETE CASCADE`
多 Agent 扩展列: `agent_type`（默认 `hermes`，存量数据自动归为 hermes）、`meta` (JSONB)
数据保留/排序扩展列: `hidden`/`hidden_at`（软删除，可逆）、`pinned`（置顶排序）、`profile_name`（来源档案）

### messages
复合主键: `(workspace_id, session_id, id)`; 外键: `workspace_id -> workspaces(id) ON DELETE CASCADE`
多 Agent 扩展列: `agent_type`、`meta` (JSONB)；软删除列: `hidden`/`hidden_at`
消息去重键: `(session_id, role, timestamp)` 三元组（跨设备幂等）

### sync_state
主键: `(device_id, workspace_id)`; 外键: `workspace_id -> workspaces(id) ON DELETE CASCADE`

### projects
复合主键: `(workspace_id, id)`（canonical id：default 档案为裸 id，命名档案为 `<profile>:<id>`）
列: `slug`（同 (workspace, profile) 唯一，同名合并依据）、`name`、`description`、`icon`、`color`、
`board_slug`、`primary_path`、`created_at`、`archived`、`hidden`/`hidden_at`、`merged_into`、`agent_type`

### project_folders
复合主键: `(workspace_id, project_id, path)`；列: `label`、`is_primary`、`added_at`；
跨设备增量合并（新路径插入、已有路径更新，不删除）

### project_remap
复合主键: `(workspace_id, old_id)`；列: `new_id`（同名合并后 old_id → new_id 路由记录，供客户端收敛）

### quota_config
主键: `plan`；列: `max_sessions`（NULL = 不限；默认 `free` = 200）、
`allowed_agents`（NULL / 空数组 = 全部允许）。策略由运营侧写入，server 每次 push 读取，改动即时生效。

### audit_log
运营审计表：`quota_rejected` 等事件由 server 写入；`plan_changed` 等运营操作由运营侧写入。

## API 参考

### Sync API（API Key 认证，格式 `ws_xxx`）
```
GET  /health                    # 健康检查
POST /pull                      # 拉取会话（limit/offset 分页、last_sync_at 增量；全量池——不过滤 agent）
POST /push                      # 推送会话（upsert + 消息去重；按服务端真实列过滤；agent_type/meta；配额执法）
GET  /status/{device_id}        # 同步状态（设备最近同步时间、会话/消息总数）
GET  /sessions                  # 列出会话（最近 50 条，含 agent_type）
GET  /users                     # 列出同步设备
```

### Projects API（API Key 认证，格式 `ws_xxx`）
```
POST /api/projects/push   # 推送项目 + folders（同 (profile, slug) 合入最早项目并记录 remap）
POST /api/projects/pull   # 拉取项目 + folders + remap（不含已隐藏项目）
```

### Client Update API（API Key 认证）
```
GET  /api/client/manifest?agent=X&v=本地版本   # 版本对比 + 每文件 sha256/size
GET  /api/client/download?agent=X             # 客户端 zip（内嵌 manifest.json）
```

### REST API（JWT 认证）
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

### Admin API（管理员 JWT）
```
GET  /api/admin/users           # 所有用户
POST /api/admin/users/{uid}/toggle   # 启用/禁用用户
GET  /api/admin/workspaces      # 所有 workspace（元数据，不含会话/消息）
```

### Web UI（浏览器访问）
```
GET  /web/                      # 信息概览
GET  /web/all-sessions          # 全部会话（跨工作空间统一列表：搜索/工作空间/Agent 筛选/分页）
GET  /web/login                 # 登录页面
GET  /web/register              # 注册页面（邀请码可选，支持 ?code= 预填）
GET  /web/logout                # 登出
GET  /web/change-password       # 修改密码页（首次登录强制改密时跳转至此）
POST /web/change-password       # 修改密码
POST /web/update-profile        # 更新个人资料（显示名/密码/管理员标志）
GET  /web/set-language/{lang}   # 切换语言（zh-CN / en）
GET  /web/workspace/{id}        # Workspace 详情（会话列表：置顶/排序/分页/搜索/档案过滤/回收站入口/Agent 徽章/项目列表）
GET  /web/workspace/{id}/session/{sid}            # 会话消息查看器（Markdown 渲染、消息搜索、隐藏/恢复）
GET  /web/workspace/{id}/session/{sid}/export     # 导出单个会话为 Markdown
GET  /web/workspace/{id}/export                   # 导出整个 Workspace 为 JSON.gz
POST /web/workspace/{id}/import                   # 导入 Workspace 备份（JSON/JSON.gz）
POST /web/workspace/{id}/regen-key                # 重新生成 API key
GET  /web/workspace/{id}/delete                   # 删除 Workspace
POST /web/workspace/{id}/session/{sid}/hide           # 删除会话（软删除，移入回收站；/pull 停止下发，可恢复）
POST /web/workspace/{id}/session/{sid}/unhide         # 从回收站恢复会话
GET  /web/workspace/{id}/trash                        # 会话回收站（已删除会话，可恢复）
GET  /web/workspace/{id}/session/{sid}/trash          # 消息回收站（已删除消息，可恢复）
POST /web/workspace/{id}/session/{sid}/message/{mid}/hide     # 删除消息（软删除，移入回收站，可恢复）
POST /web/workspace/{id}/session/{sid}/message/{mid}/unhide   # 从回收站恢复消息
GET  /web/help                                 # 接入帮助页（MCP 客户端接入帮助；/web/help-hermes 旧入口 301 跳转）
GET  /web/download/mcp-client?ws_id={id}&agent=X  # 下载 MCP 客户端 zip（Key 为占位符）
GET  /web/admin/users                             # 用户管理
POST /web/admin/user/create                       # 创建用户
GET  /web/admin/user/{uid}/edit                   # 编辑用户
POST /web/admin/user/{uid}/edit                   # 提交用户编辑（显示名/密码/管理员）
GET  /web/admin/user/{uid}/toggle                 # 启用/禁用用户
GET  /web/admin/workspaces                        # 所有空间管理（元数据与开关，不含会话内容）
GET  /web/invites                                 # 邀请管理（所有登录用户；/web/admin/invites 旧入口 303 跳转至此）
POST /web/admin/invite/create                     # 创建邀请码（有效期/备注/授予套餐）
POST /web/admin/invite/{id}/revoke                # 撤销邀请码
```
