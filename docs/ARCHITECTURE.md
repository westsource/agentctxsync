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
|  |                        Agent App (Hermes/DeepSeek Harness/...)                   |  |
|  |  +--------------+    stdio     +----------------------------------+    |  |
|  |  |  Agent Core  | <----------> |  MCP Server (server.py)          |    |  |
|  |  +--------------+              |  hermes-session-sync             |    |  |
|  |                                |                                  |    |  |
|  |  +------------------------+    |  Tools: sync_status/pull/push/   |    |  |
|  |  |  本地存储 (per agent)  |    |  full, project_push/pull         |    |  |
|  |  |  state.db / jsonl /    |<-->|  (+ hermes_sync_* 兼容别名)        |    |  |
|  |  |  SQLite / JSON files   | R/W|  Adapters: hermes/dsh/opencode |    |  |
|  |  +------------------------+    |  /reasonix/openclaw/workbuddy  |    |  |
|  |                                |                                  |    |  |
|  |  +------------------------+    |  Background Tasks:               |    |  |
|  |  |  config.yaml           |    |  * startup auto-pull (增量)      |    |  |
|  |  |  mcp_servers:          |    |  * bootstrap push (首次配对)      |    |  |
|  |  |    hermes-sync:        |    |  * periodic sync (5min)          |    |  |
|  |  |      env:              |    |  * auto-update (24h, 校验+替换)   |    |  |
|  |  |        HERMES_SYNC_... |    |  * 锁+主备角色 (多副本安全)       |    |  |
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
|                | FastAPI Server (server/ 多模块 :8765)                       |  |
|                |                                                          |  |
|  |  Web UI (/web/*)          REST API (/api/*)       Sync API             |  |
|  |  * 登录 / 注册(验证码+邀请) * Auth (login/me)     * GET /health        |  |
|  |  * 信息概览 / 全部会话     * Workspace CRUD       * POST /pull          |  |
|  |  * 工作空间 / 会话查看器   * Admin (users/ws)     * POST /push          |  |
|  |  * 回收站 / 导出 / 导入    * Change Password      * GET /status/{dev}   |  |
|  |  * 接入帮助 + 客户端下载   * register (管理员)    * GET /sessions      |  |
|  |  * Admin (用户/空间/邀请/统计)                   * GET /users          |  |
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
- **远程服务器**：单个 FastAPI 进程（`server/main.py` 装配）按业务域拆分模块承载
  Web UI、REST API、Sync API 与客户端更新 API；认证分三层——Web UI 用 JWT（Cookie）、
  REST 用 JWT（Header）、Sync/更新 API 用 Workspace API Key（`ws_xxx`）；
  界面内置 zh-CN / en 双语。模块结构见下节。
- **部署形态**：systemd 服务（自动重启）+ Docker Compose（PostgreSQL pg18 + pgvector 扩展）+
  Cron 每日备份（pg_dump → gz，保留 7 天）。

### 服务端代码结构（server/）

服务端从单文件 `server.py` 按业务域拆分（详见 [server-deployment.md](server-deployment.md) 第 5 节），
入口为 `main.py`；各业务域模块通过 FastAPI `APIRouter` 挂载，共享 `db.py` 连接池与
`render.py` 渲染基础设施：

| 模块 | 职责 | 路由归属 |
|------|------|----------|
| `main.py` | 应用装配：FastAPI 实例、静态文件、中间件、router 汇总、uvicorn 入口 | — |
| `config.py` | 环境变量与派生常量（PG_DSN / 密钥 / PUBLIC_URL） | — |
| `captcha.py` | 注册数学验证码（自托管 SVG、进程内一次性挑战、TTL） | `/web/captcha/new` |
| `db.py` | psycopg2 连接池、`init_db` 幂等建表迁移、配额策略查询、工作空间查询辅助 | — |
| `render.py` | Jinja2 渲染（executor 异步化）、flash 消息、请求作用域 ContextVar、中间件 | — |
| `requestlog.py` | 请求日志中间件：全站 REQ 行 + 每日访问统计（`access_stats`/`access_device` 表，domain/IP 渠道、设备/agent/版本） | 全站中间件；`/web/admin/access`、`/web/admin/access/devices` |
| `translations.py` | i18n 翻译表（zh-CN / en） | — |
| `agents.py` | Agent 注册表（静态数据，驱动帮助页与客户端包生成） | — |
| `auth.py` | 认证域：PBKDF2 密码、JWT 签发/校验、API key 依赖、登录/注册/改密/语言、账户状态中间件（强制改密 + 邮箱验证门）、邮箱验证（SMTP 可选开关） | `/`、`/web/login`、`/web/register`、`/web/change-password`、`/web/update-profile`、`/web/set-language/*`、`/web/logout`、`/web/verify-email`、`/web/email`、`/api/auth/*` |
| `workspace.py` | 工作空间域：仪表盘、全部会话、会话查看器、CRUD、导出/导入、软删除/回收站、REST | `/web/`、`/web/all-sessions`、`/web/workspace/*`、`/api/me`、`/api/workspaces` |
| `sync.py` | 同步域：pull/push/status/sessions/users，配额执法与审计日志 | `/health`、`/pull`、`/push`、`/status/{device_id}`、`/sessions`、`/users` |
| `projects.py` | 项目同步域：slug 同名合并、folders 增量合并、remap 路由 | `/api/projects/push`、`/api/projects/pull` |
| `invites.py` | 邀请码域：邀请管理、创建/撤销 | `/web/invites`、`/web/invite/create`、`/web/invite/{id}/revoke` |
| `admin.py` | 管理域：用户/全局空间管理/访问统计（仅管理员） | `/web/admin/*`、`/api/admin/*` |
| `search.py` | 全局搜索域：跨工作空间 + 租户隔离的会话/消息搜索（pg_trgm GIN + ILIKE），会话/消息双路命中、分页，结果定位到具体消息（二期） | `/web/search` |
| `client_update.py` | 客户端分发：zip 构建（运行时改写默认服务器/Agent + manifest 哈希）、下载端点 | `/api/client/manifest`、`/api/client/download` |
| `web_help.py` | 接入帮助域：帮助页、客户端包下载（由 `agents.py` 注册表 + `client_update.py` 驱动） | `/web/help`、`/web/help-hermes`（301）、`/web/download/mcp-client` |
| `feedback.py` | 问题反馈域：提交建议/缺陷，管理员列表与解决状态切换 | `/web/feedback`、`/web/feedback/submit`、`/web/feedback/{fid}/resolve` |

中间件注册顺序（`main.py`）：`flash_middleware`（render）→ `enforce_account_state`（auth）→ `request_log_middleware`（requestlog，最外层，全站 REQ 日志），
与单文件时代一致；`/web/*` 页面在强制改密期间仅放行
`/web/login`、`/web/change-password`、`/web/logout`、`/web/register`、`/web/set-language`；
邮箱验证开启时（SMTP 已配置，见 `config.smtp_configured`），待验证账户
（`PENDING_EMAIL_VERIFICATION`）仅放行上述页面 + `/web/verify-email`、`/web/email`。

### 客户端代码结构（mcp/）

每个 Agent 独立部署一份 MCP 客户端（`HERMES_SYNC_AGENT` 选择适配器，`server.py` 按需
在任意 cwd 运行）：

| 模块 | 职责 |
|------|------|
| `server.py` | MCP 入口：工具面（`sync_*` + `hermes_sync_*` 别名）、后台任务（启动拉取 / 周期同步 / 自动更新）、单写者锁（后台循环 + 写工具，主/备角色）、pull 分页与重试、API 调用与配额错误翻译；兼容 mcp SDK v1/v2 |
| `updater.py` | 自动更新：manifest 比对、zip 校验、备份后原子替换 |
| `adapters/base.py` | 适配器抽象：canonicalize/localize、`(session_id, role, timestamp)` 去重写入、水位线（含服务器身份绑定）、外来会话 owner 注册表、`validate_local_id` 路径穿越防护 |
| `adapters/hermes.py` | Hermes 多档案 state.db（含子代理折叠、项目同步） |
| `adapters/dsh.py` | 官方 DeepSeek Harness（deepseek-ai/dsh，世代化事件日志 `session.vN.jsonl[.zstd]`，当前 v3、逐行 zstd 帧、写入发布后继且冻结前代；workspace/投影缓存域归 dsh 原生，写入时折叠标题缓存） |
| `adapters/workbuddy.py` | WorkBuddy db+jsonl（`workbuddy:` 前缀、cwd slug 与 WorkBuddy 自身方案一致、ms↔s 时间戳换算；项目同步 = `workspaces` 表 + `.workbuddy-sync-projects.json` 身份侧车） |
| `adapters/reasonix.py` | Reasonix jsonl 转写（`reasonix:` 前缀；agent 运行中持有 `.jsonl.lock` 时跳过该会话；无可靠时间戳时用合成值保持去重键唯一） |
| `adapters/opencode.py` | opencode 1.x 共用 `opencode.db`（SQLite `session`/`message`/`part` 三表，CLI 与桌面版共享；`ses_/msg_/prt_` id、ms 时间戳、project_id 按目录解析、`model` 列写 `{id, providerID}` JSON）；外来会话按桌面版行格式写入同一库，`ses_` id 经 idmap 持久化保持去重稳定 |
| `adapters/openclaw.py` | OpenClaw 网关会话库（`sessions.json` 索引 + JSONL v3 transcript；`openclaw:server_id` 元数据保往返 id 稳定；运行中网关会覆写索引——建议关闭 OpenClaw 后同步） |

- **目录布局**：
  ```
  mcp/
  ├── server.py            # MCP 入口：工具面 + 后台任务 + 单写者锁（含主/备角色）
  ├── updater.py           # 自动更新：manifest 比对、zip 校验、原子替换
  ├── auto-sync.py          # OpenClaw 常驻同步循环（独立进程，见 openclaw 节）
  ├── run.bat / run.sh     # 本地运行入口（按 HERMES_SYNC_AGENT 选择适配器）
  ├── .hermes-sync-version # 自动更新版本记录（客户端侧车）
  ├── adapters/
  │   ├── base.py          # 适配器抽象 + canonical 模型 + AGENT_PREFIXES
  │   ├── __init__.py      # _ADAPTER_MODULES 注册表（惰性加载，缺模块不拖垮整体）
  │   ├── _template.py     # 新适配器骨架（从它复制）
  │   └── <agent>.py       # 每 agent 一个实现
  └── tests/               # fixture 往返单测（读/写/幂等/前缀）
  ```

- **工具面**（`server.py`，宿主 agent 以 MCP tool 调用）：`sync_status` / `sync_pull` /
  `sync_push` / `sync_full` / `project_push` / `project_pull`（及 `hermes_sync_*` 兼容别名）。
- **适配器接口**（`adapters/base.py::Adapter`，接入步骤见
  [ADDING_AGENT.md](ADDING_AGENT.md)）：四个方法 `discover()` / `read_sessions(limit)` /
  `write_sessions(sessions)` / `status()`。canonical 模型：会话必填 `id`（带前缀）+
  `started_at`，通用可选列见 `CANONICAL_SESSION_FIELDS`；消息必填
  `session_id`/`role`/`content`/`timestamp`，可选 `reasoning`/`tool_*`/`display_*`/
  `compacted`/`meta`。**特有字段一律进 `meta` 且键带 agent 前缀**（`<agent>:foo`）防跨
  agent 冲突；去重键统一为 `(session_id, role, timestamp)` 三元组（客户端与服务端同规则，
  详见「消息身份与幂等去重」）。**canonical 值必须是 JSON 可编码类型**（str/int/float/
  bool/None/dict/list）：二进制列（SQLite BLOB → `bytes`）没有对应槽位就**不要带进来**——
  push 的分块与请求编码都要 `json.dumps`，一个不可编码的值会让整轮同步失败。
- **后台任务**（`server.py`）：启动 8s 增量拉取 → bootstrap push（首次配对）→ 每 300s
  周期同步（push → pull → projects push/pull）→ 自动更新（启动 60s 后、每小时）；单写者锁
  （后台循环 + 写工具共用）+ 更新锁，多副本时仅启动赢家（主）跑后台同步、其余 standby 只应答
  工具，详见「增量同步与水位线」「客户端自动更新」及上文主/备角色段。
- **OpenClaw 常驻同步**（`mcp/auto-sync.py`）：OpenClaw 惰性拉起 MCP server（仅当 agent
  调用工具时），进程内 `HERMES_SYNC_AUTO_SYNC=1` 不会自行触发——独立循环进程按固定间隔
  （默认 300s、最小 60s）跑同一 `server.full_sync`（pull→push、字段级合并、水位线+去重），
  与 MCP server 共享单写者锁；`deploy-local-mcp.sh/.ps1` 对 openclaw 额外安装该循环
  （Windows 计划任务）。
- **部署模型**：每个 agent 独立部署一份实例（`HERMES_SYNC_AGENT=<name>` 选择适配器 +
  该 agent 的 API Key / 服务器地址），全部连到同一 workspace 即互相同步；服务端
  `client_update.py` 按 agent 打包分发（构建时重写默认服务器/agent 的 zip，manifest sha256
  对实际发货字节计算），可分发白名单 `PUBLIC_AGENTS`。
- **各 agent 本地存储一览**（数据目录 / canonical id / 写入约束）见
  [SUPPORTED_AGENTS.md](SUPPORTED_AGENTS.md)，新 agent 接入步骤见 [ADDING_AGENT.md](ADDING_AGENT.md)。

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
  管理员也可直接创建账号。邮箱验证为可选特性（配置 SMTP 即启用）：启用后新注册
  账户为待验证态（`PENDING_EMAIL_VERIFICATION`，验证激活时才建默认工作空间）；
  存量账户标记 `LEGACY_UNVERIFIED` 但一切照常——**唯一限制**：未验证邮箱的账户
  不能创建新工作空间（已验证邮箱全局唯一，部分唯一索引）。已有工作空间、API Key、
  会话同步与数据访问一概不限制，兼容存量用户
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
派生列: `last_activity_at`（会话真实最后活动时间 = 最新一条可见消息的 `timestamp`，
**服务端在每次 push 时从本次载荷的消息派生**，客户端只消费不回写——见「会话最后活动时间」
决策记录；存量行为 NULL，`scripts/backfill-last-activity.py` 一次性补齐）
多端字段合并扩展列: `rev`（会话级全局递增版本，默认 0）、`field_rev`（JSONB，
每字段最后被接受的 `rev`，默认 `{}`）——见「字段级乐观并发」决策记录

### messages
复合主键: `(workspace_id, session_id, id)`; 外键: `workspace_id -> workspaces(id) ON DELETE CASCADE`
多 Agent 扩展列: `agent_type`、`meta` (JSONB)；软删除列: `hidden`/`hidden_at`
消息去重键: `(session_id, role, timestamp)` 三元组（跨设备幂等）

### sync_state
主键: `(device_id, workspace_id)`; 外键: `workspace_id -> workspaces(id) ON DELETE CASCADE`

### projects
复合主键: `(workspace_id, id)`（canonical id 全部裸 id；agent 归属在 `agent_type` 列，hermes 档案在 `profile_name` 列；旧前缀 id 由 `server/sync.py` 的入站兼容层规范化）
列: `slug`（同 (workspace, profile) 唯一，同名合并依据，profile 存于 `profile` 列）、`name`、`description`、`icon`、`color`、
`board_slug`、`primary_path`、`created_at`、`archived`、`hidden`/`hidden_at`、`merged_into`、`agent_type`
多端字段合并扩展列: `rev`（项目级全局递增版本，默认 0）、`field_rev`（JSONB 每字段版本，
默认 `{}`）——见「字段级乐观并发」决策记录（扩 sessional merge + 项目标量字段）

### project_folders
复合主键: `(workspace_id, project_id, path)`；列: `label`、`is_primary`、`added_at`；
跨设备增量合并（新路径插入、已有路径更新，不删除）

### 路径分隔符约定（Path Separator Convention）
- **服务端统一存储与返回为 `/`（canonical）**。会话与项目的本地文件系统路径
  （`sessions.cwd`、`sessions.git_repo_root`、`projects.primary_path`、
  `project_folders.path`）在入库时统一把反斜线 `\` 归一化为 `/`；`/pull` 与
  `/api/projects/pull` 返回时同样返回 `/`。历史上已存的反斜线路径由独立脚本
  `scripts/migrate-path-sep.py` 一次性迁移（默认 dry-run，`--apply` 写库）。
- **客户端 pull 写本地按本机已有分隔符对齐合并**：MCP 客户端在 pull 将会话/项目
  写入本机存储前，先读取本地已有会话/项目（`cwd`、`primary_path`、`folders[].path`），
  对 pull 数据中「仅分隔符不同、其余相同」的路径改用本地已存的那份写法后再写入——即
  保持与本机已有数据一致并合并写入，避免因分隔符差异把同一路径/项目文件夹插成两条。
- 由此形成对称设计：**服务端规范存储 `/`，客户端落地按本机已有风格**。workbuddy
  的 cwd slug 生成已对分隔符归一化（天然不敏感）；会话按 id upsert（路径不参与主键），
  项目文件夹以 `path` 为主键的一部分（分隔符敏感），是本约定需要对齐的最关键环节。

### project_remap
复合主键: `(workspace_id, old_id)`；列: `new_id`（同名合并后 old_id → new_id 路由记录，供客户端收敛）

### quota_config
主键: `plan`；列: `max_sessions`（NULL = 不限；默认 `free` = 300，存量 200 由 `init_db` 幂等提升）、
`allowed_agents`（NULL / 空数组 = 全部允许）。策略由运营侧写入，server 每次 push 读取，改动即时生效。

### audit_log
运营审计表：`quota_rejected` 等事件由 server 写入；`plan_changed` 等运营操作由运营侧写入。

## 关键算法与决策（Key Algorithms & Decisions）

> 本节记录同步链路的关键算法与历史决策，改行为前先读本节。每项都标注边界（「勿改回」）与
> 回归测试位置；无测试覆盖的行为视为未锁定，改动时需补测试。

### 消息身份与幂等去重（Message Identity & Dedup）

- **身份 = `(session_id, role, timestamp)` 三元组，不是消息 id**。本地消息 id 是各存储的自增列，
  pull 重写后会被重新分配，不能跨设备作身份；服务端以部分唯一索引
  `uq_messages_dedup (workspace_id, session_id, role, timestamp) WHERE role/timestamp 非空` 兜底
  （`db.py::init_db` 建索引前先清一次存量重复行）。客户端与服务端共用同一去重规则，
  pull 写本地、push 写服务端天然幂等。
- **push 去重流水线**（`server/sync.py::push_sync`）：请求开头一次性快照本批会话的
  `(session_id, role, timestamp)` 键集合（一条查询，替代逐消息 SELECT）→ 内存判重 →
  每会话一个多行 `INSERT ... ON CONFLICT DO NOTHING RETURNING`（一次往返）→ 被 ON CONFLICT
  吞掉的行逐条复查：真重复丢弃；内存分配的自增 id 被并发 push 抢走、实为新消息的用新 id 重试一次。
  缺 role/timestamp 的损坏行退回按 `(session_id, id)` 查重。
- **内容级兜底去重**：hermes 在中断回合后会做「消息交替修复」——用 `time.time()` 重生成时间戳，
  同内容出现新时间戳，三元组键失效。兜底规则：同会话内 `(role, content)` 已存在即视为重复。
  作用域：hermes/reasonix 全角色（它们的重建会重写 tool 行）；其他 agent 仅 user/assistant
  （tool 输出合法重复，只能走三元组）。`meta` 为裸字符串（内容在外来存储往返中丢失、
  仅残留在 meta）时也参与内容比较。同一规则镜像在客户端 `mcp/adapters/base.py::write_sessions`
  （pull 写本地），保证拉推往返不产生重复行。
- **时间戳唯一性修补**：harness 同一毫秒戳会写入大量事件，两消息同三元组会在拉推往返中静默塌缩，
  适配器把碰撞时间戳确定性 +1ms 上调至空闲（该 +1ms 修补由历史 codex 引擎引入，2026-09-06 引擎移除；模式仍适用 JSONL 类适配器）；
  reasonix 转写文件无可靠时间戳，用单调递增合成值（`base + i/10`）保持去重键唯一。
- **message_count 修复**：服务端在 pull/push 时按实际消息数重算 `message_count`
  （sync 写入的会话本地该列常为 0，而桌面 UI 过滤 `message_count < 1` 的会话）。
- 回归防线：`server/tests/test_sync.py`（triple 去重、内容兜底、meta 兜底、id 竞争重试、
  并发 push 不重复等）。

### 会话身份与档案路由（Session Identity & Profile Routing）

- **canonical id 全部裸 id（无前缀）**；归属在 `agent_type` / `profile_name` 列。
  旧 `<agent>:` / `<profile>:` 前缀方案由 `scripts/migrate-id-scheme.py` 一次性迁移。
- **入站兼容层** `server/sync.py::_split_inbound_id`：push 时先把旧前缀 id 规范化成
  裸 id + agent_type/profile_name（在配额门与去重快照**之前**执行），旧客户端重推已迁移会话时
  命中同一行，不会变成新会话。
- **`agent_type` 只写一次**：INSERT 时设置，UPDATE 永不触碰——重推不得破坏创建者归属
  （否则拉取了外来会话的客户端会把归属洗成自己）；`hidden` 同理，重推不得复活软删除。
- **hermes 多档案**：适配器扫描 `profiles/` 下所有 state.db 全量同步；default 档案裸 id，
  命名档案的归属走 `profile_name` 字段；外来 agent 会话落在 default 档案、保留 canonical id
  原样往返（round-trip），push 时按 owner 过滤不回推。
- **`profile_name` 的规范拼写（勿破坏）**：默认档案 = `""` / NULL，命名档案 = 档案名。
  hermes 本地 `state.db` 那列的字面默认值是 `default`，它**不是**规范拼写：`canonicalize()`
  必须把它从 canonical 载荷里丢掉（否则 push 上行后服务端原样存储），`write_sessions()` 的路由表
  以 `''` 为默认档案键，因此 `default` 需按别名落回默认档案（仅当本地确实没有名为 `default`
  的档案）。破坏这条会让「服务端可见的会话在部分设备上永远拉不下来」——路由表查不到键时整行被
  静默丢弃，只留一行 `skipped N session(s) …` 汇总日志（决策记录 2026.09.13.1，
  回归防线 `mcp/tests/test_hermes.py::test_default_profile_does_not_carry_column_literal` /
  `::test_pull_alias_default_profile_writes_to_default`）。
- **外来会话 owner 注册表**：`.hermes-sync-foreign.json`（hermes）/ `*-foreign-ids.json`
  （其余 agent）记录 `{id: owner agent}`；pull 写入外来会话时登记，push 时按 owner 打
  `agent_type`，服务端保持归属。旧纯 id 列表格式读取时自动升级为 dict。
- **路径穿越防护**：自由 id 类存储（dsh / reasonix / opencode / openclaw / workbuddy；历史 codex 引擎已移除）写入前
  经 `validate_local_id` / `validate_file_id`（拒绝含 `/`、`\`、`.`、`..` 的 id），详见
  SECURITY_AUDIT.md。

### 子代理会话折叠（Sub-agent Folding）

- hermes 的每个子 agent 回合是独立 session 行（`parent_session_id` 链接，子行通常无标题）。
  客户端 `mcp/adapters/hermes.py::_fold_subagent_sessions` 在**读取（push 视图）时**折叠：
  沿 `parent_session_id` 链到最终根（支持子代理套子代理），子消息改挂根会话
  （`session_id` → 根 id）、打 `meta.subagent` 标记、合并后按时间戳排序、重算
  `message_count`，子行从 push 输出中剔除。批次内找不到父的会话保持原样（不丢数据）。
- 已同步到服务端的孤儿子行由 `scripts/migrate-fold-subagents.py` 软隐藏（可逆，unhide 恢复）。
- 服务端不折叠（按推送原样存储），Web 查看器按 `meta.subagent` 显示徽标。

### 增量同步与水位线（Incremental Sync & Watermark）

- 水位线是**边车文件**（hermes：`.hermes-sync-watermark`，各 agent 命名不同），内容
  `v2 <服务器身份> <时间戳>`，身份绑定 sync 服务器——切换服务器或读到旧格式文件 → 返回 0 →
  全量重拉（防止残留水位线让旧会话永远低于增量截断线、静默不再被拉取）。
- pull 增量语义：服务端 `(last_synced_at > X OR started_at > X)`；客户端发送
  `本地水位线 − 300s` 宽限（水位线是本机时钟、远端 `last_synced_at` 是**别的设备**时钟，
  严格截断会漏掉时钟偏移设备推的会话）；消息增量按 `timestamp > X` 过滤。
- 客户端后台任务（`mcp/server.py`）：启动 8s 后增量拉取（避开宿主启动读写峰）；每 300s
  周期同步（push → pull → projects push/pull，同周期顺带）；**bootstrap push**——水位线为 0
  （从未同步）且远端为空时，间隔重拉两次确认后把本地全量推上去（误判无害：服务端按三元组去重，
  多余全推是空操作）。
- **单写者锁**：O_EXCL 锁文件 + 持有者 PID，PID 已死则窃取（防崩溃进程永久锁死）；锁按 agent
  命名（互不阻塞）。守护对象 = 后台循环 **+ 显式写工具**：`sync_full/pull/push`、
  `project_push/pull` 经 `_locked_tool` 抢同一把锁（等待预算默认 20s、
  `HERMES_SYNC_TOOL_LOCK_WAIT_S` 可调，超时返回 busy 而非并发写库；同进程自己的后台周期
  持锁时也会等待）；更新锁独立。背景：Hermes 桌面会起两个 serve 实例、各 spawn 一个 MCP
  进程，无锁会并发写同一本地库。
- **主/备角色**：启动（+8s）抢锁成功的副本为主（`Primary`），负责此后全部后台周期同步；
  输家为备（`Standby`），整生命周期只应答工具、不再每周期空转抢锁（少日志少唤醒）。故障
  切换不靠备升级——宿主重拉副本时的新启动竞争窃取陈旧锁重新选出主。启动日志含角色与 pid
  （`Primary (pid N)` / `Standby (pid N)`），mcp-stderr 可辨识谁在跑后台同步；周期门控等待
  启动任务决出角色，避免启动早期误报 standby。
- pull 稳健性：每页 15 条（大页实测超时）；「页面与上次相同则停止」（防旧服务端忽略 offset
  死循环）；本地库被宿主锁定时按 0/2/5/10s 退避重试（`busy_timeout=5s` 快速失败，不阻塞宿主读）。
- **推送侧会话指纹（B5，客户端）**：每会话记录推送指纹 `(message_count, max_timestamp[, 文件 mtime])`
  于与 field-meta 同目录的 `-push-fingerprint.json` sidecar；push 循环跳过指纹未变化的会话，
  仅**成功推送后**更新指纹（失败不更新、下轮重试）。mtime 由 adapter 可选提供
  （`base.Adapter.session_mtime`，workbuddy 已实现——取该会话所有副本的最新 mtime，pull 触及的
  副本也会失效指纹）；无 field-meta 的 agent 回退全量推送（原行为）。
- **push 分块字节上限（B4，客户端）**：`_chunk_sessions` 在会话数/消息数之外增加 `max_bytes`
  （默认 8MB）上限，单会话超限单独成块；单块失败（413/配额/超时/编码失败）不再中止整个推送
  循环，记录错误继续，结束时汇总返回。
- **推送隔离与可编码性（客户端）**：指纹过滤在**分块之前**完成（未变化的会话连体积计算都不做）；
  随后 `_partition_encodable` 把不可 JSON 编码的会话单独摘出——按会话隔离、日志点名到字段
  （`_json_offender` → `messages[i].<col> (bytes)`）、随结果返回 `unsendable: [id...]`，其余会话
  照常推送。`api_call` 另把请求体编码放进 `try`：编码失败按普通请求失败返回 `{"error": …}`，
  于是"单块失败不影响其余"对编码错误同样成立（push/pull 一致）。
- 拉取范围见「全池拉取契约」：agent 参数不参与过滤。
- **本地删除不是删除信号**：增量截断之外，第一页另带本机 id 清单做完整性对账（缺的行当轮补回），
  见下节「本地删除不是删除信号（Pull 完整性修复）」。

### 本地删除不是删除信号（Pull 完整性修复，决策记录 2026.09.12.5）

> 背景：pull 是**增量**的（服务端 `last_synced_at > 水位线`），而「本地少一行」与「这行还没拉过」
> 在增量协议里无法区分。于是本地删掉的会话在服务端仍可见时，行为取决于它上次被推送的时刻：
> 落在 5 分钟宽限窗内 → 下一轮被重新投递（复活）；早于宽限窗 → 永远不再出现，直到**别的**设备
> 改动它（push 会刷新 `last_synced_at`）。同一根因还覆盖两类"静默漏拉"：水位线过新（切换服务器
> 后的残留水位线、或会话在隐藏期间被创建/解禁）让可见会话永远低于截断线。

**规则**：服务端**可见**（`hidden=0`）的会话集合 = 每台客户端本地存储应收敛到的集合。本地缺哪一行，
下一轮 pull 补哪一行；`hidden=1`（Web 软删除 / 回收站）是**唯一**的服务端退休信号——隐藏的会话
两个方向都不下发，也从不被报告为缺失。

**机制**（两个可选字段，双向兼容）：

- `known_ids`（客户端 → 服务端，仅第一页携带）：本机已有的会话 id 清单。
- `missing_ids`（服务端 → 客户端，仅第一页响应）：工作空间内可见但不在 `known_ids` 中的 id。
- `ids`（客户端 → 服务端）：按 id 点名取回，**忽略增量截断**（`last_synced_at/started_at` 不参与），
  仍走 `hidden=0` 过滤并按 `limit/offset` 分页。客户端据此按 `PULL_PAGE=15` 分块取回缺失会话，
  写入路径与普通拉取页**完全一致**（字段级合并 → 去重写入 → sidecar 锚定）；失败按尽力而为处理
  （记日志、下轮重试），绝不让修复失败连累整轮同步。

**成本**：稳态零额外请求、零额外下行（缺失集通常为空）；代价是每轮 pull 上行一份 id 清单（千级
会话约 45 B/id ≈ 44 KiB/轮，300s 间隔约 12 MiB/天）。服务端对账是**一次索引 id 扫描 + Python
集合差**（`SELECT id ... WHERE workspace_id=%s AND hidden=0`，O(可见会话数)）——**不要**写成
SQL 侧数组过滤：`id <> ALL($1)` 无法走 `(workspace_id, id)` 主键，退化成按行扫数组的
O(可见 × |清单|)。

**有意不触发的场景**：`last_synced_at == 0` 的全量拉取（本就下发全部可见会话，再报"缺失"会把库
拉两遍）；本地库为空；`limit` 显式限流时修复只花剩余额度（`limit` 始终权威）；只读上传型适配器
（`Adapter.stores_pulled_sessions = False`，chatgpt——本地没有承载共享池会话的目标）。

**不做的事**：本地删除**不会**反推服务端（push 无删除语义、无 tombstone）。要让会话彻底消失，
用 Web 软删除；想让本地删除"保持删掉"，先 Web 隐藏再删本地。

**兼容**：旧服务端忽略新字段、旧客户端不发新字段，双方都退化为原增量语义，无需版本协商。

**落地位置**：`server/sync.py::pull_sync`（`known_ids`/`ids`/`missing_ids` + `_clean_ids`）、
`mcp/server.py::pull_sessions`（清单上行 + `_restore_missing_sessions` 修复）、
`mcp/adapters/base.py::Adapter.stores_pulled_sessions`；回归防线
`server/tests/test_sync.py::PullCompletenessTest`、`mcp/tests/test_mcp_server.py::PullCompletenessRepairTest`。

### 字段级乐观并发 + 惰性 bootstrap（Field-Level Optimistic Concurrency，决策记录）

> 背景：三机（同一 workspace）并发编辑同一会话元数据时，旧的「全量推、拉全覆盖、服务器权威
> 一式」会让**任一端的过期快照抹掉另一端的有意改动**。典型：设备 A 把会话移到 B 项目
> （项目归属编码为 `sessions.cwd` 匹配 `project_folders.path`），随即重启 hermes——启动路径
> 先拉后推、增量拉退化为全量覆盖，服务端旧 `cwd` 把本地移动冲掉。本设计给出确定收敛的合并语义，
> **不需用户做任何一次强制全量 pull**。

- **概念**：`user-edit` 字段（纳入版本合并）= `cwd, git_branch, git_repo_root, title, pinned,
  archived, display_name`；`derived` 字段（保留现状 LWW + 既有守卫，不纳入）= `message_count,
  *tokens, *cost, model, source, started_at…`。字段冲突概率低/派生性质，维持原语义即可。
- **base_rev / field_rev**：服务器分配的**逻辑版本**（非墙钟，跨端时钟偏移不影响判定）。
  客户端只记录并回显，**从不生成**。约定规则：
  - 服务器持有 `sessions.rev`（全局递增）+ `field_rev[f]`（字段 f 最后被接受的 `rev`，基线 0）。
  - 客户端侧车（hermes：`.hermes-sync-meta.json`）逐字段存 `{base, val}`，其中
    `base` = 上次观察到的服务器 `field_rev[f]`，`val` = 上次同步到的值（last_known）。
  - **dirty 判定**：`本地当前值 != sidecar.val`。
- **push 合并规则**（对每个 user-edit 字段 f，载荷带 `value V, base B`）：
  - `B is None`（设备首次接触 / 字段从未同步）→ **不写**，返回 `(服务器值, cur)`；设备吸收 →
    **服务器权威一次**（无法区分该值「过期」还是「新改」，默认信任服务器）。
  - `B 已知`（客户端仅在 dirty 时才推该字段）→ `V == 服务器值` 则 no-op；否则**接受**并
    `rev++, field_rev[f]=rev`。多端同时改同一字段 = 到达先后 LWW（确定性）。
  - 旧客户端（载荷无 `field_meta`）→ 整会话回退既有全量覆盖语义；保护仅在新客户端之间生效。
- **pull 规则**：pull 返回每个会话的 `field_rev`（JSONB）+ 各字段现值。本地对 user-edit 字段：
  `本地脏`则**不写**（保留，下一轮推）；否则写服务器值并更新 `base/val`。derived 字段照旧写。
- **同步顺序 pull → push**（启动与周期一致）：先把服务器最新值/版本吸进来且不覆盖本地脏字段，
  再推脏字段；收敛、惰性、无强制全量。
- **惰性 bootstrap**（升级后无需强制全量 pull，无用户动作）：sidecar 初始为空 → 所有字段
  `base=None` → 每字段在**首次接触**时建立基准（pull 侧吸收服务器现值+rev；push 侧 `base=None`
  被服务器权威化并返回 rev）。每台设备靠正常 5 分钟周期自行灌满。
  一次性语义：升级瞬间、首接触前未推送的字段改动会被服务器权威覆盖一次——与现行行为一致，无回归。
- **消息层不动**：追加 + 三元组去重已跨端安全；pull 沿用 `hidden=0` 过滤、push 不复活隐藏消息。
- **Phase 2（已实现）· projects 元数据**：`projects` 表同样引入 `rev` + `field_rev`，对标量
  user-edit 字段（`name`/`primary_path`/`archived`/`description`）做同一字段级乐观并发 +
  惰性 bootstrap（sidecar `.hermes-sync-<agent>-projects-field-meta.json`）。
  **folders 不入字段版本**：路径按 `(project_id, path)` 并集（跨设备增量共存，已有路径
  label/is_primary 走路径级 LWW）——其 label/is_primary 实际近乎常量；且以路径为版本键会因
  分隔符拼写不同而分裂（客户端 pull 已按本机分隔符对齐，见「路径分隔符约定」）。slug 合并
  时存活项目保留其 `rev`/`field_rev`。回归防线：`server/tests/test_sync.py`
  `ProjectsPushMergeTest` + `mcp/tests/test_mcp_server.py` `ProjectFieldMergeTest`。
- **消息墓碑（不做）**：绝大多数 agent 不支持删除消息；soft-hide 已保证 pull 不下发、push 不
  复活，暂无跨端删除传播需求，故 Phase 2 不引入墓碑。
- **项目信息按 agent 的存储形态差异（分析记录）**：项目字段级合并只对**确有独立项目元数据
  存储**的 agent 有意义，各适配器差异如下——
  - **hermes**：`projects.db`（`projects` 表 + `project_folders` 表），项目为独立实体 →
    纳入 Phase 2 字段级合并。
  - **workbuddy**：会话文件存于 `~/.workbuddy-ai/projects/<slug>/<id>.jsonl`，
    `projects/` 下的目录 slug 由该会话 `cwd` 经 `slugify()` 派生（`workbuddy.py::_session_path`），
    `cwd` 存于 `workbuddy.db`——**会话层的"项目归属"就是 `cwd`**，已被 Phase 1 会话字段级并发
    （`cwd ∈ USER_EDIT_FIELDS`）覆盖。
    另有**项目清单**：`workbuddy.db` 的 `workspaces` 表（`path` + `last_opened_at`，
    WorkBuddy 自己维护的"打开过的目录"）。该表**没有** id/slug/name 列，所以 2026.09.13.2 起
    由适配器侧车 `.workbuddy-sync-projects.json` 承载身份（服务端下发的 id/slug/name/folders；
    服务端未见过的路径确定性地铸 `wb_<sha1(path_key)>`），即"本地项目库"由 `workspaces` 表 +
    侧车共同构成，字段级合并对 workbuddy 同样生效（详见 workbuddy 适配器章节的"项目"小节）。
    其中**根形状**条目（`D:\`、`C:\Users\<name>`）按决策记录 2026.09.13.2 **不入池**。
  - **reasonix**：**无项目概念**。会话为 `<state root>/sessions/<id>.jsonl` 扁平目录，读出的
    会话连 `cwd` 都没有，无项目目录/元数据，无冲突面。
  - 结论：项目字段级合并对 **hermes**（`projects.db`）与 **workbuddy**（`workspaces` + 身份侧车）
    生效；reasonix 无项目。其余适配器声明 `supports_projects = False`，周期同步直接跳过项目阶段
    （此前会在每轮日志里报一次 AttributeError）。
- 回归防线：`server/tests/test_sync.py`（base=None 拒绝 / 已知 base 接受 / no-op / 并发到达
  LWW / 旧客户端回退）、`mcp/tests`（脏检测、pull 不覆盖脏字段、sidecar 惰性填充）。

### 项目同名合并（Project Slug Merge）

- push：同 `(workspace, profile, slug)` 不同 id → 合入**最早**项目：folders 增量合并
  （新路径插入、已有路径更新，**不删除**——多设备编辑共存）、写 `project_remap`
  （old_id → new_id，幂等）、删除被合并行及其 folders。
- pull：客户端应用 remap（old → new）收敛；本地 slug 撞名时按 `unique_slug`（`-N` 后缀）改名。
- 分隔符敏感点：`project_folders.path` 是主键一部分，靠「路径分隔符约定」的对齐逻辑避免同一路径
  插成两条。

#### 重复项目改名不再污染 name（决策记录 2026.08.25.1）

> 背景：hermes 桌面端会为同一文件夹创建**重复项目**（实测「对话分析」×2、「投资研究」×2），
> 中文文件夹名的 slugify 落到 `project` 兜底，撞名后唯一 slug 退化为 63 字符数字链
> （`project-3-2-2-…-N`，尾部 `-N` 是 `_unique_slug` 截断改名的痕迹）。两台机器各持不同尾部
> 变体，客户端每 5 分钟周期互推。旧 push 先按 slug 查存量：重复项目每次改名后新 slug 查不到
> 行 → 落入 INSERT 的 `ON CONFLICT (workspace_id, id)` 分支 → `name = p.get("name") or slug`
> 把 63 字符 slug 链写进 name 列（字段级客户端对未脏 name 会省略上传，恰好触发兜底），且
> `rev`/`field_rev` 被重置为 1 → 两端互推把 name 在 `-2/-3/-4` 变体间来回覆盖，永不收敛；
> 另可见 `project_remap` 双向记录（同一对项目反复互相合并删除）。

- **项目身份由 id 决定**（`server/projects.py::api_projects_push`）：先按
  `(workspace_id, id)` 查存量，id 已存在**一律走字段级 UPDATE 路径**——slug/icon/color 等
  plain 字段照常 LWW 同步，name 只在 `field_meta` 断言（已知 base）时更新。改名后的重复项目
  无论推哪个 slug 变体都命中 UPDATE，不再触达 INSERT 冲突分支。
- **同名合并仅对全新 id 执行**：同 slug 撞存量行的合并（保留最早 + remap）只在 id 不存在时
  触发；已存在的行不因 slug 撞车被误删（旧代码会把改名撞上他人 slug 的存量行整个 merge 掉）。
- **INSERT 冲突分支保护 name**（并发兜底）：载荷未提供 name 时，`ON CONFLICT` 的 SET 列表
  动态剔除 `name` 列，即使并发竞争也不会用 slug 覆盖存量 name。
- 效果：服务端 name 收敛为用户设置的真实名；slug 继续随客户端同步（plain LWW），但不再反向
  污染 name。实测（生产）：存量 id + 新 slug 变体 + 无 name 的推送返回 `updated:1` 且
  `field_rev.name` 不变；客户端真实同步两轮后 name 稳定、重复行未回写。
- 回归防线：`server/tests/test_sync.py` `ProjectsPushMergeTest`
  （`test_existing_id_renamed_slug_keeps_server_name` /
  `test_existing_id_renamed_slug_new_client_preserves_name`）。

#### 会话最后活动时间（last_activity_at，决策记录 2026.09.13.3）

> **触发**：从远端拉取的会话在本地"最后更新/最后活动"上显示成**同步时刻**，不是最新消息时间。
> 例如 WorkBuddy 列表（`ORDER BY COALESCE(updated_at, created_at) DESC`）里，一次同步把被触碰的
> 会话全部塌到同一时刻后顶到最前；Hermes 桌面端的 `last_activity_at` 对同步创建的会话是 NULL；
> omp 的 title slot `updatedAt`、OpenClaw 索引的 `updatedAt`/`lastInteractionAt`/`lastActivityAt`
> 都恒为 `now`。
>
> **结论**：`last_activity_at` 提升为 canonical 字段 + 服务端**派生**列——值由**服务端**从
> 消息算（每次 push 取本次载荷里最新的消息时间戳），客户端只**消费**（写进各自本地的
> "最后更新"字段）并回读，客户端自算只作 fallback。

- **为什么不由各 agent 自己算**：`ended_at` 各 agent 语义不一致（2026-09-13 实测 40 条拉取会话：
  **5 条缺失**、只有 **3 条**等于最新消息时间；hermes 的 `ended_at` 常比最新消息早几千秒，omp 有几条
  为空），而"最新消息时间"只有一个权威来源——消息本身，服务端已持有全部消息（Web 早就在用
  `MAX(m.timestamp)` 做 `last_msg_at`/`synced_at` 排序）。放服务端=一处规则、跨 agent 一致、存量
  可回填；放客户端=每个 adapter 各写一套、且老客户端永远是错的。
- **服务端机制**（`server/sync.py::push`）：每个会话在装配行数据前，用它**本次载荷的消息**取
  `max(timestamp)` 写入 `last_activity_at`（客户端送来的同名字段被覆盖）；**没有消息的元数据型
  push**（例如只改标题）没有可派生来源，客户端值原样透传（不清空）。与 `message_count` 同规则：
  payload 是权威，陈旧客户端可能把值往回带（下一次新鲜 push 修正）。
- **客户端机制**（`base.session_last_activity()` 共享助手，规则一处定义）：
  `last_activity_at`（服务端派生值）→ 本次载荷最新消息时间 → `ended_at`；都没有才退回 `now`。
  - **workbuddy**：`sessions.updated_at` 与 `last_activity_at` 都写这个值（不再 `max(..., now)` 钳制，
    只保留 `>= created_at` 的下界）；读回 `last_activity_at`。
  - **omp**：title slot 的 `updatedAt`（列表时钟）用这个值；无可用时间戳时才是 `now`。
  - **openclaw**：索引 `updatedAt`/`lastInteractionAt`/`lastActivityAt` 用这个值；读回
    `lastActivityAt`。
  - **hermes**：无需改代码——`state.db` 本就有 `last_activity_at` 列（nullable），加入
    `CANONICAL_SESSION_FIELDS` 后 1:1 映射（`col_map` 空=同名）自动读写；此前该列不在 canonical
    里，同步创建的会话落成 NULL。
  - **opencode**：`session.time_updated` 已用 `ended_at`（缺失才回退 now），无该列，不改。
  - **dsh / reasonix**：JSONL 事件日志，无"最后更新"元数据，天然按最后事件排序，不改。
  - **chatgpt**：只读上传，不写本地，不改。
- **为什么不是 user-edit 字段**：它由消息派生，不参与字段级乐观并发（不进 `USER_EDIT_FIELDS`）；
  客户端"脏值"没有意义——消息才是事实。
- **存量数据**：列为 NULL 的行由 `scripts/backfill-last-activity.py` 一次性按
  `MAX(m.timestamp)`（只看可见消息）补齐；dry-run 默认、幂等。未补齐也不会错，只是那几行不会
  被任何客户端重新 push 时保持"无最后活动"。
- 回归防线：`mcp/tests/test_base.py::SessionLastActivityTest`（规则优先级）、
  `test_workbuddy.py`（落库时间 = 最新消息时间 / 服务端值优先 / 无时间戳才 now）、
  `test_omp.py`（title slot）、`test_openclaw.py`（索引 + 读回）、`test_hermes.py`（列往返）、
  `server/tests/test_sync.py`（push 派生覆盖客户端断言 / 元数据型 push 透传）。

#### 根目录（home / 盘符根）不入共享项目池（决策记录 2026.09.13.2）

> **触发**：workbuddy 项目清单接入后（决策记录同版本），WorkBuddy 的 `workspaces` 表里存在
> `D:\`、`C:\Users\rong` 这类"根"条目，被如实上行成项目卡。随后的问题：各 agent 拉到本机后，
> 是否应当**自动识别**这类根项目，并把**本机的根**（`Path.home()`）作为 folder 并集进去，
> 让"一张卡覆盖所有机器的 home"？
>
> **结论**：识别，但**排除**（`is_root_project_path`）——根形状路径永远不进共享项目池；
> 不做任何"跨机 home 并集"。理由是机制上的**单向门**与语义上的**兜底桶**：

- **为什么"并集本机 home"看着合理但不行**：
  1. **单向门**：`project_folders` 只并集、**无删除 API**（`server/projects.py` 全文只有合并
     时 `DELETE FROM project_folders`），且**项目没有 Web 隐藏/归档/删除入口**
     （`projects.hidden` 列存在但无任何代码写它）。别名 folder 一旦上行即永久，兜底卡无法在
     UI 关掉。
  2. **兜底桶**：Web 的项目↔会话关联是 `LOWER(cwd)` **前缀匹配该项目的任一 folder**
     （`server/workspace.py::_session_for_project_match`），任意深度子目录都算。根是万物祖先：
     实测本机 workbuddy 的 286 条会话中，`rong`(`C:/Users/rong`) 吞 **99** 条、`D:` 吞 **27**
     条，合计 **110/286（38%）**；其中 62 条是适配器为外来会话建的**合成兜底目录**
     `~/hermes-sync-foreign`（不是"主目录里的工作"）。再加本机 home 并集后该卡预计 **153/286**。
  3. **嵌套重复**：`C:\Users\X1\Documents` 已是项目「对话分析」(`p_e5fff637`) 的 folder；再把
     `C:\Users\X1` 并进根卡，同一会话会**同时**出现在两张卡下（关联不排他）——语义污染而非重复数据。
  4. **跨 agent 传染**：folder 是共享池数据，别名会上行并被所有设备/agent 拉走（hermes 会写进
     自己的 `projects.db` 并在桌面端项目里显示），而撤不回来。
- **另一条硬约束（即使要做也不许违反）**：跨机等价只能落在 **folders**（按路径并集、幂等），
  **绝不能改写 `primary_path`**——它是 per-field LWW 的 user-edit 字段，A 机推
  `C:/Users/rong`、B 机推 `C:/Users/X1` 会每周期互推覆盖（ping-pong 永不收敛）。
- **谓词**（`mcp/adapters/base.py::is_root_project_path`）：`_path_key` 归一后（`\`→`/`、
  Windows 折叠大小写）去掉尾斜杠，命中以下之一即"根形状"——空串（POSIX `/`）、`/root`、
  `^[a-z]:$`（盘符根）、`^(?:[a-z]:)?/(?:users|home)(?:/[^/]+)?$`（home 及其祖先
  `/home`、`/Users`、`C:/Users`、`/home/<n>`、`C:/Users/<n>`）。`C:/Users/<n>/Documents`
  这类**真目录不命中**。
- **三处使用**：
  - `mcp/server.py::push_projects`：逐项目过 `strip_root_project_paths`（丢根路径；**全部**
    路径都是根的"纯根项目"整个不上行；`primary_path` 只置 `None`、绝不改写成别的路径）。
  - `mcp/server.py::pull_projects`：同一过滤器作用于服务端返回的载荷——根路径不写进本地 store、
    不进身份侧车；纯根项目不落地（留在服务端、不改动）。
  - `mcp/adapters/workbuddy.py`：`read_projects` 跳过根形状 workspace（不铸 id）、
    `_save_project_map` 剔除根键、`write_projects` 不把根路径落成 workspace 行——共享谓词决定
    语义，适配器只保证自己的 id 注册表/本地列表干净。
  - 本地**全保留**：`D:\`、`C:\Users\rong` 仍是 WorkBuddy 的正常 workspace；hermes 的
    `projects.db` 完全不动（若它有 home 项目，只是不再同步）。
- **实测影响**（生产 workspace 4 + 本机 workbuddy store）：
  | 指标 | 排除前 | 排除后 |
  |---|---|---|
  | 服务端项目卡 | 13（含 `D:`、`rong`） | 11 |
  | 本机 workbuddy 参与同步的 workspace 键 | 15 | 13 |
  | 本机会话归组命中 | 207/286 | 97/286 |
  | 本地↔服务端差异 | 1（`F:/软考/2026下半年` 不可落地） | 3（2 条根 + 同上） |
  即"逐条一致"变成"**除根形状条目外一致**"；同时**诚实记录**：本次项目同步对本机会话分组的
  净收益≈0（97 vs 未同步前的 95），因为本机 workbuddy 会话大多住在 `C:\Users\X1`(54)、
  `~/hermes-sync-foreign`(71)、`c:/tmp`(7) 这类**非项目目录**；真实收益是"项目清单对齐 +
  其他机器/agent 的会话能归入这些项目"。
- **被否决的备选**：
  1. **客户端别名并集**（各 agent 拉到时把本机 home 并进根项目）：见上四条，单向门 + 兜底桶。
  2. **服务端可回退 path alias**（不动项目数据，只在 Web 关联时按设备解析 home 等价）：
     机制上更优雅且可撤销，但要新增 schema + UI；且解决不了"本机项目清单缺一张卡"。**保留为
     未来选项**：若将来确实需要"跨机 home 归组"，走这条路，而不是往 folders 里塞别名。
  3. **维持现状（各机各卡）**：语义重复的 home 卡并存（`rong` / `X1`），且仍带兜底桶效应。
- **退役已上行的根卡**：无 API → 用 `scripts/hide-root-projects.py`（`--dry-run` 默认 /
  `--apply` 隐藏 / `--undo` 恢复）。`UPDATE projects SET hidden=1, hidden_at=…` 让卡片同时从
  Web 项目列表与 `/api/projects/pull` 消失，且**不会被动复活**：push 的 UPDATE 分支只写
  plain 字段 + 被断言的 user-edit 字段，`hidden` 从不在写入列表内（与「`/push` 不复活隐藏会话」
  同一条规则）。脚本自带的谓词是 `base.py` 的镜像（服务端部署无法干净 import `mcp/`——它是与
  已安装 mcp SDK 撞名的 namespace 目录），由 `mcp/tests/test_project_roots.py::ScriptPredicateTest`
  守住一致性。
- **边界与例外**：有人确实把 home 当主工作区——排除后其 home 无法作为项目同步。要保这个能力
  需要**显式白名单**（如项目字段 `root_ok: true`），不做路径形状的自动放行。
- 回归防线：`mcp/tests/test_project_roots.py`（谓词正/反例表、`strip_root_project_paths` 的
  "只丢路径不改 primary_path"/纯根项目丢弃/无路径项目原样通过、脚本谓词一致性）、
  `mcp/tests/test_mcp_server.py::RootProjectFilterTest`（push 丢根与纯根不上行、pull 不落地）、
  `mcp/tests/test_workbuddy.py::WorkBuddyProjectsTest::test_read_projects_skips_root_workspaces`。

### 配额执法（Quota Enforcement）

- 只对**新建会话**执法，已存在会话继续同步（调低配额不破坏既有池）；Master API Key
  （user_id=None）不执法。
- 每次 push：取本 workspace 已存在 id 集 → 算出新会话的 agent 集；按用户 plan 读
  `quota_config`（缺行 fail-open：不限）；活跃数 = 该用户**全部 workspace** 中
  `archived=0` 的会话数；`quota_check` 顺序：agent 白名单（`allowed_agents`）→ 会话上限
  （`existing + new ≤ max_sessions`）。
- 拒绝时先写 `audit_log` 再抛 HTTP 403（get_conn 回滚会丢掉审计行）；客户端把机器码
  （`agent_not_allowed` / `quota_exceeded_sessions`）翻译成用户可读提示。

### 客户端自动更新（Auto-update）

- 分发（`server/client_update.py`）：zip 内含**重写默认值后**的 `mcp/` 包——构建时把
  `SYNC_SERVER` 默认值改成服务端地址、`HERMES_SYNC_AGENT` 改成目标 agent；manifest 的 sha256
  必须对**实际发货字节**计算（否则客户端校验失败）。可分发 agent 白名单 `PUBLIC_AGENTS`
  （hermes / workbuddy / reasonix / opencode / openclaw / dsh 已端到端验证并上线帮助页分发；
  历史 codex 引擎已移除，存量 deepseek-harness 数据并入 dsh）。
- 客户端（`mcp/updater.py`）：manifest 比对版本 → 下载 → 按 manifest 逐文件 sha256 校验 →
  备份后原子替换、删除不再分发的文件；版本写入 `.hermes-sync-version`；**重启后生效**
  （日志 + 宿主通知）。启动 60s 后首次检查、之后每小时（避开宿主启动峰值）；独立更新锁。

### 软删除与回收站（Hidden / Trash）

- `hidden=1` + `hidden_at`，可逆（unhide 恢复）；`/pull`、`/api/projects/pull` 停止下发 hidden
  行；Web 回收站页管理。子代理孤儿迁移（fold-subagents）复用同一机制。
- 重推不得重置 hidden（客户端仍持有该会话时不得让它复活）——push 的
  `sd.pop("hidden")` 保证。

### scripts/ 目录脚本（迁移 / 部署 / 测试）

**一次性迁移**：

| 脚本 | 用途 |
|---|---|
| `migrate-id-scheme.py` | 前缀 id → 裸 id + `agent_type`/`profile_name` 列 |
| `migrate-path-sep.py` | 存量反斜线路径 → `/` + 分隔符重复行去重 |
| `migrate-fold-subagents.py` | 软隐藏已同步的子代理孤儿会话 |
| `migrate-local-to-server.py` | 本地 hermes state.db → 远端服务器（首次上云） |
| `import_doubao.py` | 豆包云端会话导入（豆包无本地稳定存储） |

**部署 / 运维**：

| 脚本 | 用途 |
|---|---|
| `deploy-server.sh` | 服务端一键部署（目标机 `/opt/agentctxsync`，模块化 `server/` 全量拷贝） |
| `deploy-remote.py` | 远端发布辅助：备份 → SSH 上传 server 模块与 `mcp/` 客户端 → 重启服务 → 验证（health + agent_type 探测；`DEPLOY_SSH_HOST` 指定目标） |
| `deploy-local-mcp.sh` | 本地 MCP 客户端部署（bash；按 agent 写入 `config.yaml` 的 `mcp_servers`；openclaw 额外安装 `auto-sync.py` 常驻循环 + Windows 计划任务） |
| `deploy-local-mcp.ps1` | 同上 PowerShell 版（多 agent，注释含 openclaw 注册示例） |

**端到端测试**：

| 脚本 | 用途 |
|---|---|
| `e2e_multagent.py` | 多 agent 交叉同步 e2e：hermes → dsh → opencode 推/拉及反向，验证 id 稳定与重推幂等（历史 codex 腿随引擎移除改由 dsh 承接） |

## 已支持 Agent 接入方案（适配器实现细节）

> 每个 agent 一个适配器（`mcp/adapters/<name>.py`，`_ADAPTER_MODULES` 注册、惰性加载），
> 全部共用 `base.py` 的 canonical 模型、`(session_id, role, timestamp)` 三元组去重、水位线、
> 外来会话 owner 注册表与 `validate_local_id` 路径穿越防护。canonical id 全部裸 id，归属在
> `agent_type` / `profile_name` 列（前缀仅识别历史 id）。本节记录各家存储的**不可变事实与
> 写入红线**，改行为前先读对应适配器；quick 表见 [SUPPORTED_AGENTS.md](SUPPORTED_AGENTS.md)。

### hermes（Hermes 桌面端，多档案）

- **存储布局**：`%LOCALAPPDATA%\hermes\`（POSIX `~/.hermes`），每档案一个 `state.db`
  （SQLite）：default 档案 `state.db`，命名档案 `profiles/<name>/state.db`（magic/coder/
  …）；另有每档案 `projects.db`（项目 + project_folders）。列 1:1 映射，无字段改写。
- **多档案发现**：`discover()`/`read_sessions()` 扫描**全部**档案并合并（default 永远第一）。
  原因：Hermes 通过进程内 ContextVar 切档案、不写入子进程——单档案适配器永远只能读到
  default。命名档案的会话在 canonical 里带 `profile_name` 字段（default 为空）。
- **子代理折叠**：读取时沿 `parent_session_id` 链折叠（支持子代理套子代理），子消息改挂
  根会话 + `meta.subagent`，子行从 push 输出剔除；批次内找不到父的会话保持原样（不丢
  数据）。详见「子代理会话折叠」。
- **写入路由**：pull 按 `profile_name` 路由到对应档案的 state.db；本机不存在的档案跳过；
  外来 agent 会话落 default 档案、canonical id 原样往返（hermes 无 agent_type 列，靠
  `.hermes-sync-foreign.json` owner 注册表在 push 时补标签）。写入约束 = SQLite 事务。
- **项目同步**：projects.db 同样按档案聚合读、按 `profile` 路由写，应用服务端 remap；
  目标档案目录不存在时创建。

### 历史：codex 引擎（已移除，并入 dsh）

> codex CLI rollout 适配器（曾名 `deepseek_harness.py`，后更名 `codex.py`）面向 codex CLI
> 存储（`~/.codex` rollout jsonl），非官方 DeepSeek Harness。2026-09-06 起该引擎与注册键
> `deepseek-harness` 一并移除：存量 `agent_type='deepseek-harness'` 数据迁移为 `dsh`；
> 入站遗留 `codex:` 前缀 id 归一为 `agent_type=dsh`。官方 DeepSeek Harness 见下节 **dsh**。

### dsh（官方 DeepSeek Harness，deepseek-ai/dsh）

- **存储布局**：`<DSH_HOME 或 ~/.dsh>/sessions/--<cwd-slug>--/<session-<uuid>>/session.vN.jsonl[.zstd]`
  （每会话一目录；一代一文件——`session.jsonl` = v0，当前世代 v3；zstd 为**逐行独立帧**
  ——dsh 读器要求首帧解压后恰为一行 header）。`DSH_HOME` 可覆盖数据根。
- **读取**：`session/title` → 标题、`user/message`/`assistant/message`（assistant
  `source.kind=model`）→ canonical 消息；`seq` 连续；tool/chunk/compaction 事件非对话跳过。
- **写入**：**当前世代**（v3）头 + 种子头（`permission/preset`、`sandbox/mode`、
  `approval/policy`）+ 每轮 `turn/start`/`step/start` 帧 + 连续 seq 事件（原子替换）；既有
  旧世代文件**逐字节冻结**、后继写成新文件（dsh 自身写入的约定：读器永远取最高世代）。
  assistant/message 必须带 `usage`/`stream` 结算块，且**不能**带 `sourceEventSeqs`
  ——dsh 读器对旧世代产物跑 v0→v1→v2→v3 迁移链，链上**拒绝**只有消息事件的日志
  （"format v2 surface before first step cannot acquire a system head without changing
  chronology"），这正是"同步下来的会话在新版桌面打不开（历史加载失败：network
  error（gateway/internal））"的根因。外来 id 经 idmap 映射 `session-<uuid>`；
  cwd 漂移搬迁目录（Windows NTFS 大小写漂移就地改名）；zstd 需 `zstandard`（缺失时
  跳过压缩文件并计跳过数）。
- **域分工**：workspace 域由 dsh 首启按会话头 bootstrap（fs.realpath 规范路径），外部不写；
  投影缓存（`session_projcache` v5 文档，identity=header createdAt/cwd）在每次日志写入后
  按桌面折叠模板同步折叠 → 拉入会话在列表中即时显示真实标题；无 cwd 会话（`_no-cwd`
  兜底）不折叠且清除陈旧文档——v5 schema 要求 `identity.cwd` 为 string，null 文档会被
  桌面每次启动移入 `.json.bak.*`，折叠只会制造告警噪音、毫无列表收益。

### opencode（opencode CLI/桌面）

- **存储布局**：`opencode.db`（SQLite）位于 `$XDG_DATA_HOME/opencode/`（Windows
  `%LOCALAPPDATA%\opencode\`；候选路径按序探测，最具体的优先）；`session`/`message`/
  `part` 三表，id 为 `ses_`/`msg_`/`prt_` + 12 位 hex 毫秒时间戳 + 14 位 base62 随机。
- **读取**：`session` 行 → 会话（毫秒→秒）；`message.data` JSON 解析 role
  （`agent-switched`/`model-switched`/`compaction`/`step` 跳过，`shell` → `tool`）；
  `part` 行聚合 text/reasoning/tool 引用（tool 以 `[tool:name] input` 文本并入 content）；
  tokens 进 `meta["opencode:tokens"]`；`project_id` 进 `meta["opencode:projectID"]`。
- **写入红线**：`session.model` 列必须写 `{id, providerID}` JSON（opencode 的
  `Model.Ref` JSON-parses 该列，裸字符串或缺 providerID 会整个会话列表报错，providerID
  缺省 `unknown`）；`project_id` NOT NULL 且 FK——写前按 `cwd` 最长前缀匹配
  `project`/`project_directory` 解析归属项目（`_resolve_project_for_directory`），兜底
  `global`；`slug` 唯一性模拟桌面端（`-N` 后缀）；外来 id 经 idmap 分配新 `ses_` id。
  写入 = 直接 SQLite INSERT/UPDATE（autocommit），连接显式关闭（Windows 未关闭句柄会
  锁库）。
- **边界**：正在运行的 opencode 实例有内存缓存，写入后 UI 立即可见性不保证——建议宿主
  实例空闲/退出后同步。

### reasonix（DeepSeek-Reasonix）

- **存储布局**：`%APPDATA%\reasonix\sessions\`（`REASONIX_HOME` 覆盖；POSIX
  `~/.reasonix/sessions`）；每会话 `<id>.jsonl`（id = 文件主干，自由格式）；sidecar：
  `<id>.events.jsonl`（权威事件日志）、`<id>.jsonl.meta`、`<id>.goal-state.json`、
  `<id>.ckpt/`、`<id>.jsonl.lock` / `<id>.jsonl.lease.json`（锁）。
- **读取红线**：`_session_paths` 只收 `*.jsonl`（排除 `.events.jsonl`）；运行中会话
  （`.jsonl.lock` 存在）**跳过**——宿主关闭后再同步。消息行 `{"role","content",
  "tool_calls","tool_call_id","name"}` 直映射；transcript 无可靠时间戳 → 稳定单调合成值
  `started_at + i/10` 保持去重键唯一；无标题时以 local_id 兜底（外来会话剔除该兜底，
  避免 push 覆盖服务器真实标题）。
- **写入**：append-only；写入前检查 lock 文件（存在则跳过该会话）；**内容级兜底去重**——
  reasonix 桌面会剥离时间戳、前置 system prompt 重写 transcript，重读的时间戳与服务器
  真实值不再匹配，仅靠三元组会无限重 append：同 `(role, content)` 视为同一消息（空
  content 豁免——连续空 tool 结果合法不同）；`tool_calls` 归一化为 reasonix 期望的
  list 结构。

### openclaw（OpenClaw，网关会话库）

- **存储布局**（OpenClaw 2026.7.x）：`~/.openclaw/agents/<agentId>/sessions/`
  （`OPENCLAW_HOME` 覆盖；多 agent 取最新 mtime 的 `sessions.json`）：
  - `sessions.json` —— 会话索引 `{session_key: {sessionId, sessionFile,
    sessionStartedAt, updatedAt, ...}}`；
  - `<sessionId>.jsonl` —— 每会话 transcript（JSONL v3：session header +
    `model_change` / `thinking_level_change` / `message` 事件，message 以
    `parentId` 链式串接）。
  网关以 mtime 缓存持有该存储、变更即重载——**适配器写入的会话无需重启即可在 TUI 与
  `sessions.list` 出现**（此前 SQLite 探测版适配器已废弃，见 git 历史）。
- **id 方案**：本地 id = session key（如 `agent:main:main`）；canonical id 优先级：
  `meta.openclaw:server_id`（pull 时记录）→ key 派生池 id（`_POOL_ID_RE`：hermes
  时间戳 id / reasonix `rx-*` / 外来 UUID）→ foreign 注册表 → transcript UUID。
  `openclaw:server_id` 保证拉推往返命中同一服务器行（否则池内会话会按 transcript
  UUID 重新推送、在服务器上分叉成重复）。
- **读取**：索引按 `updatedAt` 排序；消息取 `type=="message"` 行（content 块归一为
  文本；时间戳 ms→s）；标题 = 首条 user 消息（截 80 字符）。
- **写入**：索引条目（`_save_index` tmp + `os.replace` 原子替换）+ transcript
  （新会话写 header + 链式消息；已有会话按 `(role, round(ts*1000))` 去重追加、
  `_tail_id` 续 parentId）；服务器内容可能携带二进制字节 → `_sanitize_text` 剔除
  孤立代理项（否则 UTF-8 写出失败、消息永不参与去重、每次 pull 重 append）。
- **写入红线**：**运行中的网关持久化内存状态时会覆写 `sessions.json`**，抹掉适配器在
  网关运行期间加入的索引条目——pull 可重复（去重安全），但优先在 OpenClaw 关闭时同步，
  或网关写会话后重拉一次。
- **常驻同步**：OpenClaw 惰性拉起 MCP server（仅当 agent 调用工具时），注册里的
  `HERMES_SYNC_AUTO_SYNC=1` 不会自行触发——`mcp/auto-sync.py` 独立进程按固定间隔
  （默认 300s、最小 60s）跑同一 `server.full_sync`（pull→push、字段级合并、水位线+
  去重），与 MCP server 共享单写者锁，保证会话自动上云；部署脚本自动安装该循环。
- **部署**：服务端帮助页 `server/agents.py` 的 openclaw 条目已发布（register/
  install/uninstall 片段，`mcp.servers` stdio 注册），下载包经 `client_update.py`
  分发。

### workbuddy（WorkBuddy 桌面端，双向）

- **存储布局**：`~/.workbuddy-ai/`（`WORKBUDDY_HOME` 覆盖；旧版 `~/.workbuddy`，同时
  存在时**优先 `.workbuddy-ai`**——写入 legacy 目录会让 WorkBuddy 启动 MIGRATE 看到零
  本地会话）：
  - 消息：`projects/<slug>/<conversationId>.jsonl`（slug = cwd 压平驱动器/分隔符，
    `F:\OpenCode\agentctxsync` → `f-OpenCode-agentctxsync`，盘符根 → 单字母）；
  - 元数据：`workbuddy.db`（SQLite `sessions` 表，毫秒时间戳）；
  - `edge-sync-mapping-v2.db`：WorkBuddy 自管映射，**绝不触碰**。
- **事件映射**（JSONL 每行一个事件，与 WorkBuddy 5.3.13 逐字段核对）：
  `message`→user/assistant；`reasoning`→assistant + reasoning；`function_call`→assistant
  + tool_name/call_id/参数；`function_call_result`→tool；`ai-title`→标题；
  `file-history-snapshot`→跳过。时间戳 ms↔s 换算保证三元组往返精确。
- **读取**：会话可能存在于多个 cwd slug 目录（项目移动/pull 周期写入的副本）——
  `_session_copies` 按 id 并集合并，`(role, timestamp)` 首文件优先、其余追加，防陈旧
  cwd 指针遗落新消息（2026-08-25 split-session 事故的修复）。
- **写入红线**：
  1. **重启后才可见**：WorkBuddy 运行时写入的会话，UI 要等重启（启动 MIGRATE 扫描注册
     `convmsg:<userId>` 映射）才显示；云端只同步元数据/标题，消息内容不上云。
  2. **cwd 目录必须存在**：否则 WorkBuddy 打不开（"工作目录可能已被重命名或删除"）；
     外来会话的远端 cwd 不存在时回退 `~/hermes-sync-foreign` 并建目录。
  3. **本地会话 preserve_cwd**：本机自建会话 UPDATE 时不改 cwd（peer 提供的 cwd 不得
     把读取路径指到陈旧副本）；外来会话以 pull 的 cwd 为准。
  4. `_ensure_schema` 镜像 WorkBuddy 5.3.13 的 `sessions` 表 + drizzle 迁移标记，确保
     下次启动被识别（drizzle 迁移在表已存在时跳过）。
  5. user_id 解析链：env `WORKBUDDY_USER_ID` → `settings.json claw.legacyOwnerUid` →
     首个现有会话的 user_id → 兜底 `hermes-sync`。
- **项目（共享项目池，2026.09.13.2 起）**：`workbuddy.db``workspaces` 表（`path` +
  `last_opened_at` ms，WorkBuddy 自己维护的"打开过的目录"）就是 WorkBuddy 的项目清单；它没有
  id/slug/name 列，身份由侧车 `.workbuddy-sync-projects.json` 承载：
  - **read（push 视图）**：每条 workspace 路径一个项目。路径已由上次 pull 记入侧车 → 直接用
    **服务端**的 id/slug/name/folders（若改用目录名等派生值，pull 后本地值与锚定 base 不等，
    会被判为"本地脏"并在每轮 push 把对端项目名覆盖掉）；服务端未见过的路径 → 铸
    `wb_<sha1(path_key)>`（`_path_key` = 分隔符归一 + Windows 大小写折叠）+ `slugify(path)`
    做 slug（整路径压平 ⇒ 不同目录绝不会撞 slug 而被服务端同名合并）+ 目录名做 name，
    并立即写入侧车（`workspaces` 表放不下 id，侧车即本地 id 注册表，保证重启后 id 稳定）。
  - **write（pull 视图）**：按本次 pull **全量重建**侧车，并为每个项目路径落一条 `workspaces`
    行（同时建目录——WorkBuddy 打不开不存在的工作目录）。已存在的行只保留、不再定日期：
    `last_opened_at` 是 app 自己的"用户打开过"时钟，不是同步数据；本地行**永不删除**
    （与"本地删除不是删除信号"一致）。remap 无需本地迁移：本地项目库不以 id 为键，合并后的
    项目随本次 pull 直接重建为存活 id。
  - **根形状条目不入池**（`D:\`、`C:\Users\<name>`，决策记录 2026.09.13.2）：`read_projects`
    跳过（不铸 id）、侧车剔除根键、`write_projects` 不落成 workspace 行；客户端另在
    push/pull 边界统一过滤（`base.strip_root_project_paths`），所以本地这些目录仍照常是
    WorkBuddy 的 workspace，只是永远不作为项目上行。
- **能力位**：`supports_projects = True`（base 默认 False）。没有本地项目库的适配器
  （opencode/dsh/omp/reasonix/openclaw/chatgpt）由 `server.py` 跳过项目阶段，工具面返回
  `Agent X has no local project store …` 而不是 AttributeError。


## oh-my-pi 接入方案（调研与决策记录 2026.08.28；pi 已随 2026-08-29 移除）

> 状态：**已实现（2026.08.28）**——适配器 `mcp/adapters/omp.py`（OmpAdapter，
> `_ADAPTER_MODULES` 注册 `omp`）、服务端注册（`server/agents.py` 帮助页条目 +
> `PUBLIC_AGENTS` 分发白名单）、页面（landing 胶囊、全部会话/工作空间 agent 过滤、徽标
> 颜色）与测试（`mcp/tests/test_omp.py`）。
> **2026-08-29：pi 支持已移除，仅保留 omp（Oh My Pi）。** 以下为原始调研与设计，仍具参考
> 价值。结论先行：omp 可以低成本接入；会话存储与 pi（其上游 fork）同源同构（同一 JSONL
> 事件格式、同一目录编码、同一文件命名），服务端**聊天级特性可完整承载（零改动）**；
> omp 特有的树/分支/压缩结构语义只能降级为线性视图 + `meta` 原样保存，要「完整」渲染需
> 另行扩展服务端（见文末）。

### 调研事实：pi / oh-my-pi 的本地存储

- **身份**：pi = `earendil-works/pi`（原 `badlogic/pi-mono`，Mario Zechner，TS monorepo）；
  omp = `can1357/oh-my-pi`（"Coding agent with the IDE wired in"，README 自述 fork of pi，
  Rust 核心 + TS 壳，npm `@oh-my-pi/pi-coding-agent`）。本机实测 omp 18.0.4。
- **数据根目录**：pi `~/.pi/agent`（Windows `%USERPROFILE%\.pi\agent`；env
  `PI_CODING_AGENT_DIR` / `PI_CODING_AGENT_SESSION_DIR` 可覆盖，见 pi `config.ts`）；
  omp `~/.omp/agent`（本机实测 `C:\Users\X1\.omp\agent`）。
- **会话文件布局**：`<根>/sessions/<encoded-cwd>/<timestamp>_<uuidv7>.jsonl`，每会话一个
  文件、JSONL 事件流、`version:3`。cwd 编码与 pi 源码 `getDefaultSessionDirPath` 逐字一致：
  `--` + cwd 去前导 `/` + `/`、`\`、`:` 全部替换为 `-` + `--`
  （`E:\OpenCode\agentctxsync` → `--E--OpenCode-agentctxsync--`）。
- **文件命名**：ISO 时间戳 `:`/`.`→`-` + `_` + uuidv7 会话 id + `.jsonl`（pi 源码
  `newSession`；本地 omp 文件 `2026-08-28T01-04-14-489Z_01a045e5-…jsonl` 吻合）。
- **事件条目**：header `{"type":"session","version":3,"id","timestamp","cwd"}`；message
  条目 `{"type":"message","id":8位hex,"parentId","timestamp","message":{role,content,
  attribution,timestamp}}`（thinking 块在 content 内）；`model_change` / `custom`（如
  tool 执行）/ `compaction` / `branch_summary` / `label` / `session_info` 等；条目以
  `parentId` 构成**树**，leaf 指针指向当前分支。
- **写入约束**：append-only；首个 assistant 消息到达时以 `O_EXCL`（wx）建文件并一次性写入
  全部条目，之后追加；迁移（v1→v2→v3）会整体重写文件。v1 无 `id/parentId`、v2 用
  `firstKeptEntryIndex`。

### 会话数据库一致性（pi vs omp）

| 维度 | pi | omp | 结论 |
|------|----|----|------|
| 会话目录 | `~/.pi/agent/sessions/` | `~/.omp/agent/sessions/` | 同构，仅根目录不同 |
| cwd 编码 / 文件命名 / uuidv7 / 8-hex 消息 id | — | 逐字一致 | **完全一致** |
| header 字段 | `session/version/id/timestamp/cwd` | 多 `title`/`titleSource` | omp 增量 |
| 首条记录 | `session` header | `title` 记录在前 | **方向不兼容**：pi 的 loader 要求首条即 header，直接读 omp 文件会被拒 |
| 标题事件 | `session_info`（name） | `title_change` + 前置 `title` | 字段不同 |
| `model_change` | `{provider, modelId}` | `{model, resolvedModelIsFallback}` | 字段不同 |
| 外围库 | 无 SQLite（settings/auth/models 均为 JSON） | `agent/history.db`（命令历史+标题）、`agent.db`（agent 注册表）、`models.db`、`blobs/`、`N.bash.log` 工具日志 | omp 独有 |

结论：核心会话流**同源同构、条目级兼容**（omp 是 pi 的 fork，message/custom 条目逐字段
一致）；omp 的扩展（title 记录、model_change 字段、外围 SQLite/日志）不影响适配器读取，
但 pi 程序自身读不了 omp 文件（首条非 header）。**对 agentctxsync 无影响**——适配器直接
解析文件，不依赖宿主程序互读。

### 服务端承载能力（canonical 模型逐项核对）

**一等公民列、零改动完整承载**：

| pi/omp 特性 | 落点 |
|---|---|
| header `cwd` | `sessions.cwd`（参与字段级乐观并发） |
| header `parentSession`（跨会话 fork） | `sessions.parent_session_id` |
| 标题（`session_info` / `title_change`） | `sessions.title` + 字段级并发 |
| 模型（`model_change` 现值） | `sessions.model`（切换历史 → meta） |
| 文本内容 | `messages.content` |
| thinking 块 | `messages.reasoning`（契约强制映射） |
| 工具调用 / 结果 | `tool` 角色 + `tool_call_id`/`tool_name`/`tool_calls` |
| 时间戳 | `messages.timestamp`（排序 + 三元组去重） |
| compaction 摘要 | `messages.compacted` + 摘要 assistant 消息（历史 codex 引擎同款降级，引擎已移除） |

**只能进 `meta`（原样保存、无语义/无 UI 渲染）**：条目树拓扑（`parentId`/leaf/分支）、
`thinking_level_change`、`label`、`custom` 条目（扩展状态、tool 执行记录）、omp 的
`title`/`title_change` 历史、消息级 8-hex 条目 id。

**服务端模型不承载、需适配器降级或扩展**：

1. **会话内分支树**：消息按 `timestamp` 线性存储 + `(session_id, role, timestamp)` 去重，
   分支（rewind 后另起路径）合并为线性时间线，旧分支消息仍可见；`parentId` 拓扑进 meta
   无渲染。语义失真。
2. **compaction 语义**：pi 的 compaction 是独立条目（`summary` + `firstKeptEntryId`，
   "当前上下文 = 摘要 + 保留路径"）。服务端只有消息级 `compacted` 标记，无路径重建逻辑。
3. **多模态 content**：`content` 为 TEXT 列，ImageContent 等结构化块需序列化/降级。
4. **去重冲突风险（适配器必做）**：分支重问场景下两条不同内容的消息可能共享
   `(role, 同一毫秒)` → 三元组去重折叠丢失。必须对碰撞时间戳做确定性 +1ms 修补
   （复用历史 codex 引擎 `_unique_ts` 模式；引擎已移除）。

### 适配器实现方案（mcp/adapters/omp.py）

- **注册**：`_ADAPTER_MODULES` 注册 `"omp"`（惰性加载已支持）；
  canonical id 用**裸 id**（uuidv7 直通），归属走 `agent_type` 列（omp）——与现行
  裸 id 方案、hermes 多档案同思路；`AGENT_PREFIXES` 无需新增（该表仅用于识别历史前缀 id，
  见「会话身份与档案路由」）。
- `discover()`：返回存在会话目录的根列表——pi 根与 omp 根各自独立适配器实例时各自返回；
  单实例实现则同时扫描两个根（POSIX `~/.pi`、`~/.omp`；Windows `%USERPROFILE%`；env 覆盖
  `PI_CODING_AGENT_DIR` 等）。
- `read_sessions(limit)`：递归扫 `sessions/*/`（`--…--` 目录即编码后的 cwd，可直接解码进
  `cwd` 字段）；header → 会话（`started_at` = header.timestamp）；message 条目 → canonical
  （thinking 块 → `reasoning`；tool 相关 custom/消息 → `tool` 角色 + `tool_name`/
  `tool_call_id`；文本合并进 `content`）；`session_info`/`title_change` → `title`；
  `compaction` → 摘要消息 + `compacted` 标记；`custom`/`label`/`thinking_level_change`/
  条目 id/parentId → `meta`（`pi:` 前缀键）。v1/v2 文件按 pi 的迁移规则就地升级后再解析。
- `write_sessions(sessions)`：对本地存在的会话文件**追加** message 条目（8-hex id、
  `parentId` = 当前 leaf、ISO 时间戳、`message` 结构按 pi 格式）；新会话按 pi 规则建文件
  （header + 首条消息）；标题写入按目标目录分支——pi 用 `session_info` 条目、omp 用
  `title_change` 条目；返回 `{"imported","updated","new_messages","duplicates"}`。
- `status()`：本地会话/消息总数（两个根合计）。
- **边界**：运行中的 pi/omp 实例正在写文件（条目不可变、append 安全，与 opencode 同模式）；
  omp 子代理会话与顶层会话同目录树（按需过滤或保留，`meta.subagent` 标记）；omp 的
  `agent.db` 注册表与本适配器无关（直接读文件）。
- **测试**：`mcp/tests/test_omp.py` fixture 造 pi 格式 + omp 扩展格式（title 首记录、
  model_change 差异、compaction、分支 parentId）各 2~3 个样例，覆盖往返、幂等、前缀、
  时间戳消歧（复用历史 codex 引擎测试模式；引擎已移除）。

### 服务端扩展路径（若需完整树/分支/压缩渲染）

按 [ADDING_AGENT.md](ADDING_AGENT.md) 边界清单，这属于第 1/4 类「需评估后才可能改动」：

- `messages` 增加 `parent_id` / `entry_type` 列（或把分支拓扑 meta 语义化）——协议
  `/push` `/pull` 不变，schema 增列；
- Web 查看器增加分支视图（按 leaf 路径渲染）与压缩标记展示；
- 去重键不受影响（仍按三元组），但分支内同 `(role, ts)` 仍需适配器消歧。

> 决策（已执行）：**按降级方案接入**（零服务端改动，风险集中在适配器时间戳消歧）；
> 实现时采用文件序线性化 + `_unique_ts` 确定性消歧；分支/压缩 UI 作为独立需求另行评估。

## API 参考

### Sync API（API Key 认证，格式 `ws_xxx`）
```
GET  /health                    # 健康检查
POST /pull                      # 拉取会话（limit/offset 分页、last_sync_at 增量；全量池——见下方「全池拉取契约」）
                                # 可选 completeness：known_ids（本机清单，第一页）→ missing_ids（缺的可见 id）
                                # 可选 ids=[...] 按 id 点名取回（忽略增量截断；见「本地删除不是删除信号」）
POST /push                      # 推送会话（upsert + 消息去重；按服务端真实列过滤；agent_type/meta；配额执法）
GET  /status/{device_id}        # 同步状态（设备最近同步时间、会话/消息总数）
GET  /sessions                  # 列出会话（最近 50 条，含 agent_type）
GET  /users                     # 列出同步设备
```

### Projects API（API Key 认证，格式 `ws_xxx`）
```
POST /api/projects/push   # 推送项目 + folders（同 (profile, slug) 合入最早项目并记录 remap）
POST /api/projects/pull   # 拉取项目 + folders + remap（全量池，不含已隐藏项目）
```

### 全池拉取契约（Full-Pool Pull，决策记录，勿改回）

**规则（2026.08.22.4 起）**：`POST /pull` 与 `POST /api/projects/pull` **始终返回工作空间全部可见会话/项目及其消息**，
无论请求体的 `agent` 字段传入什么。服务端接受该字段仅出于向后兼容（旧客户端一直在发送），但**故意忽略之**——
任何把 `agent` 用于过滤拉取结果的做法都是回归，不得恢复。

**决策依据**：跨 Agent 同步是核心能力——同一工作空间下不同设备可能运行不同 Agent
（hermes / dsh / workbuddy / opencode / reasonix / openclaw）。A 设备（hermes）必须能看到
B 设备（workbuddy）推上来的会话，否则跨 Agent 内容对桌面端不可见，与 README 声明的
「every client pulls the full pool (all agents)」相悖。此前服务端按 `agent` 过滤 `/pull`
（客户端又总是携带 `agent=hermes`），导致 hermes 客户端永远拉不到 workbuddy 会话，
是代码与文档背离的 bug，2026.08.22.4 修正。

**角色分工**：客户端自身 agent 只决定**推送**什么（push 侧按本地存储 + 外来会话
owner 注册表打 `agent_type`，见 `mcp/adapters/*.py` 的 foreign 路由）；**接收**侧一律全池，
客户端按会话的 `agent_type`/`profile_name` 字段把内容路由回本机各 Agent 存储。

**仍然生效的过滤（与 agent 无关，勿一并移除）**：`hidden=1` 的会话与消息（软删除/回收站、
子 agent 折叠后隐藏的孤儿行）不下发；`/pull` 增量分支按 `last_synced_at/started_at` 水位线、
消息按 `timestamp` 增量过滤；分页按 `limit/offset`。唯一例外是按 `ids` 点名的修复取回
（见「本地删除不是删除信号」）：它绕过水位线截断以便补回本地删掉的行，但 `hidden=0` 过滤照旧。

**落地位置**：`server/sync.py::pull_sync`（docstring）、`server/tests/test_sync.py`
（`test_agent_param_ignored_full_pool`、`ProjectsPullTest`）为回归防线；客户端
`mcp/server.py` 拉取请求仍携带 `agent` 字段（无副作用，服务端忽略）。

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
GET  /                             # 根路径：未登录 → 落地页；已登录 → 跳转 /web/
GET  /web/                      # 信息概览
GET  /web/all-sessions          # 全部会话（跨工作空间统一列表：搜索/工作空间/Agent 筛选/分页）
GET  /web/search?q=&page=       # 全局搜索（跨工作空间全文搜索，会话/消息双路命中；?focus=<mid> 定位到具体消息）
GET  /web/login                 # 登录页面
GET  /web/captcha/new           # 注册验证码（自托管数学题 SVG，进程内一次性挑战）
GET  /web/register              # 注册页面（自建数学验证码，邀请码可选，支持 ?code= 预填；SMTP 启用时必填邮箱）
GET  /web/verify-email          # 邮箱验证：等待/确认/无效/过期各态（?token= 仅展示不消费）
POST /web/verify-email          # 确认并消费令牌，激活账户（事务内建默认工作空间 + 自动登录）
GET  /web/email                 # 安全邮箱设置（存量绑定/更换邮箱状态与表单）
POST /web/email/bind            # 绑定/更换邮箱（写入 pending_email 并发验证邮件）
POST /web/email/resend          # 重发验证邮件（撤销旧令牌）
GET  /web/logout                # 登出
GET  /web/change-password       # 修改密码页（首次登录强制改密时跳转至此）
POST /web/change-password       # 修改密码
POST /web/update-profile        # 更新个人资料（显示名/密码/管理员标志）
GET  /web/set-language/{lang}   # 切换语言（zh-CN / en）
GET  /web/workspace/{id}        # Workspace 详情（会话列表：置顶/排序/分页/搜索/档案过滤/回收站入口/Agent 徽章/项目列表）
POST /web/workspace/create      # 创建工作空间
POST /web/workspace/{id}/update # 重命名/修改描述（属主与管理员）
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
POST /web/workspace/{id}/session/{sid}/messages/unhide-all    # 从回收站批量恢复该会话全部消息
GET  /web/help                                 # 接入帮助页（MCP 客户端接入帮助；/web/help-hermes 旧入口 301 跳转）
GET  /web/download/mcp-client?ws_id={id}&agent=X  # 下载 MCP 客户端 zip（Key 为占位符）
GET  /web/feedback                             # 问题反馈列表（管理员看全部，普通用户只看自己的）
POST /web/feedback/submit                      # 提交反馈（bug / feature / other）
POST /web/feedback/{fid}/resolve               # 切换反馈解决状态（管理员）
GET  /web/admin/users                             # 用户管理
POST /web/admin/user/create                       # 创建用户
GET  /web/admin/user/{uid}/edit                   # 编辑用户
POST /web/admin/user/{uid}/edit                   # 提交用户编辑（显示名/密码/管理员）
GET  /web/admin/user/{uid}/toggle                 # 启用/禁用用户
GET  /web/admin/workspaces                        # 所有空间管理（元数据与开关，不含会话内容）
GET  /web/admin/access                            # 访问统计（每日 domain/IP 渠道 × web/api 计数）
GET  /web/admin/access/devices                    # 设备访问明细（按设备/agent 聚合，含 client_version）
GET  /web/invites                                 # 邀请管理（所有登录用户；/web/admin/invites 旧入口 303 跳转至此）
POST /web/invite/create                     # 创建邀请码（有效期/备注/授予套餐）
POST /web/invite/{id}/revoke                # 撤销邀请码
```
