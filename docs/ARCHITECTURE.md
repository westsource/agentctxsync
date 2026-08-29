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
|  |  |  SQLite / JSON files   | R/W|  Adapters: hermes/deepseek-harness/opencode |    |  |
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
| `auth.py` | 认证域：PBKDF2 密码、JWT 签发/校验、API key 依赖、登录/注册/改密/语言、强制改密中间件 | `/`、`/web/login`、`/web/register`、`/web/change-password`、`/web/update-profile`、`/web/set-language/*`、`/web/logout`、`/api/auth/*` |
| `workspace.py` | 工作空间域：仪表盘、全部会话、会话查看器、CRUD、导出/导入、软删除/回收站、REST | `/web/`、`/web/all-sessions`、`/web/workspace/*`、`/api/me`、`/api/workspaces` |
| `sync.py` | 同步域：pull/push/status/sessions/users，配额执法与审计日志 | `/health`、`/pull`、`/push`、`/status/{device_id}`、`/sessions`、`/users` |
| `projects.py` | 项目同步域：slug 同名合并、folders 增量合并、remap 路由 | `/api/projects/push`、`/api/projects/pull` |
| `invites.py` | 邀请码域：邀请管理、创建/撤销 | `/web/invites`、`/web/invite/create`、`/web/invite/{id}/revoke` |
| `admin.py` | 管理域：用户/全局空间管理/访问统计（仅管理员） | `/web/admin/*`、`/api/admin/*` |
| `search.py` | 全局搜索域：跨工作空间全文搜索（pg_trgm GIN + ILIKE），会话/消息双路命中、分页、消息深链定位 | `/web/search` |
| `client_update.py` | 客户端分发：zip 构建（运行时改写默认服务器/Agent + manifest 哈希）、下载端点 | `/api/client/manifest`、`/api/client/download` |
| `web_help.py` | 接入帮助域：帮助页、客户端包下载（由 `agents.py` 注册表 + `client_update.py` 驱动） | `/web/help`、`/web/help-hermes`（301）、`/web/download/mcp-client` |
| `feedback.py` | 问题反馈域：提交建议/缺陷，管理员列表与解决状态切换 | `/web/feedback`、`/web/feedback/submit`、`/web/feedback/{fid}/resolve` |

中间件注册顺序（`main.py`）：`flash_middleware`（render）→ `enforce_password_change`（auth）→ `request_log_middleware`（requestlog，最外层，全站 REQ 日志），
与单文件时代一致；`/web/*` 页面在强制改密期间仅放行
`/web/login`、`/web/change-password`、`/web/logout`、`/web/register`、`/web/set-language`。

### 客户端代码结构（mcp/）

每个 Agent 独立部署一份 MCP 客户端（`HERMES_SYNC_AGENT` 选择适配器，`server.py` 按需
在任意 cwd 运行）：

| 模块 | 职责 |
|------|------|
| `server.py` | MCP 入口：工具面（`sync_*` + `hermes_sync_*` 别名）、后台任务（启动拉取 / 周期同步 / 自动更新）、单写者锁、pull 分页与重试、API 调用与配额错误翻译；兼容 mcp SDK v1/v2 |
| `updater.py` | 自动更新：manifest 比对、zip 校验、备份后原子替换 |
| `adapters/base.py` | 适配器抽象：canonicalize/localize、`(session_id, role, timestamp)` 去重写入、水位线（含服务器身份绑定）、外来会话 owner 注册表、`validate_local_id` 路径穿越防护 |
| `adapters/hermes.py` | Hermes 多档案 state.db（含子代理折叠、项目同步） |
| `adapters/deepseek_harness.py` | DeepSeek Harness（codex rollout 格式；非 UUID 外来 id 映射本地 UUID、毫秒戳冲突 +1ms 修补、`session_index.jsonl` 标题回填） |
| `adapters/workbuddy.py` | WorkBuddy db+jsonl（`workbuddy:` 前缀、cwd slug 与 WorkBuddy 自身方案一致、ms↔s 时间戳换算） |
| `adapters/reasonix.py` | Reasonix jsonl 转写（`reasonix:` 前缀；agent 运行中持有 `.jsonl.lock` 时跳过该会话；无可靠时间戳时用合成值保持去重键唯一） |
| `adapters/opencode.py` | opencode 1.x 共用 `opencode.db`（SQLite `session`/`message`/`part` 三表，CLI 与桌面版共享；`ses_/msg_/prt_` id、ms 时间戳、project_id 按目录解析、`model` 列写 `{id, providerID}` JSON）；外来会话按桌面版行格式写入同一库，`ses_` id 经 idmap 持久化保持去重稳定 |
| `adapters/openclaw.py` | OpenClaw 网关存储（`~/.openclaw/agents/<id>/sessions/sessions.json` 索引 + `<sessionId>.jsonl` 转写，canonical id 为转写 UUID、key 存 `meta.openclaw:session_key`）；写入按网关持久化形态（索引条目 + 链式 parentId 消息图），网关热重载索引（mtime）；运行中的网关可能覆写 `sessions.json`，建议关闭 OpenClaw 时同步或同步后重拉 |

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
  适配器把碰撞时间戳确定性 +1ms 上调至空闲（`mcp/adapters/deepseek_harness.py::_unique_ts`，同文件恒等映射）；
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
- **外来会话 owner 注册表**：`.hermes-sync-foreign.json`（hermes）/ `*-foreign-ids.json`
  （其余 agent）记录 `{id: owner agent}`；pull 写入外来会话时登记，push 时按 owner 打
  `agent_type`，服务端保持归属。旧纯 id 列表格式读取时自动升级为 dict。
- **路径穿越防护**：自由 id 类存储（deepseek-harness / reasonix / opencode / openclaw / workbuddy）写入前
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
  命名（互不阻塞）；只守护后台循环（显式工具调用不加锁）；更新锁独立。背景：Hermes 桌面会起两个
  serve 实例、各 spawn 一个 MCP 进程，无锁会并发写同一本地库。
- pull 稳健性：每页 15 条（大页实测超时）；「页面与上次相同则停止」（防旧服务端忽略 offset
  死循环）；本地库被宿主锁定时按 0/2/5/10s 退避重试（`busy_timeout=5s` 快速失败，不阻塞宿主读）。
- **推送侧会话指纹（B5，客户端）**：每会话记录推送指纹 `(message_count, max_timestamp[, 文件 mtime])`
  于与 field-meta 同目录的 `-push-fingerprint.json` sidecar；push 循环跳过指纹未变化的会话，
  仅**成功推送后**更新指纹（失败不更新、下轮重试）。mtime 由 adapter 可选提供
  （`base.Adapter.session_mtime`，workbuddy 已实现——取该会话所有副本的最新 mtime，pull 触及的
  副本也会失效指纹）；无 field-meta 的 agent 回退全量推送（原行为）。
- **push 分块字节上限（B4，客户端）**：`_chunk_sessions` 在会话数/消息数之外增加 `max_bytes`
  （默认 8MB）上限，单会话超限单独成块；单块失败（413/配额/超时）不再中止整个推送循环，
  记录错误继续，结束时汇总返回。
- 拉取范围见「全池拉取契约」：agent 参数不参与过滤。

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
  - **workbuddy**：**无独立项目实体**。会话文件存于 `~/.workbuddy-ai/projects/<slug>/<id>.jsonl`，
    `projects/` 下的目录 slug 由该会话 `cwd` 经 `slugify()` 派生（`workbuddy.py::_session_path`），
    `cwd` 存于 `workbuddy.db`。因此 workbuddy 的"项目归属"本质就是 `cwd`——已被 Phase 1 会话
    字段级并发（`cwd ∈ USER_EDIT_FIELDS`）覆盖，项目层无元数据可冲突。
  - **reasonix**：**无项目概念**。会话为 `<state root>/sessions/<id>.jsonl` 扁平目录，读出的
    会话连 `cwd` 都没有，无项目目录/元数据，无冲突面。
  - 结论：项目字段级合并仅 hermes 生效；workbuddy 由 cwd 承载（会话层已保护）；reasonix 无项目。
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
  （hermes / workbuddy / reasonix / opencode / openclaw 已端到端验证并上线帮助页分发；
  deepseek-harness 在注册表中但分发与帮助页未启用）。
- 客户端（`mcp/updater.py`）：manifest 比对版本 → 下载 → 按 manifest 逐文件 sha256 校验 →
  备份后原子替换、删除不再分发的文件；版本写入 `.hermes-sync-version`；**重启后生效**
  （日志 + 宿主通知）。启动 60s 后首次检查、之后每小时（避开宿主启动峰值）；独立更新锁。

### 软删除与回收站（Hidden / Trash）

- `hidden=1` + `hidden_at`，可逆（unhide 恢复）；`/pull`、`/api/projects/pull` 停止下发 hidden
  行；Web 回收站页管理。子代理孤儿迁移（fold-subagents）复用同一机制。
- 重推不得重置 hidden（客户端仍持有该会话时不得让它复活）——push 的
  `sd.pop("hidden")` 保证。

### 一次性迁移脚本（scripts/）

| 脚本 | 用途 |
|---|---|
| `migrate-id-scheme.py` | 前缀 id → 裸 id + `agent_type`/`profile_name` 列 |
| `migrate-path-sep.py` | 存量反斜线路径 → `/` + 分隔符重复行去重 |
| `migrate-fold-subagents.py` | 软隐藏已同步的子代理孤儿会话 |
| `migrate-local-to-server.py` | 本地 hermes state.db → 远端服务器（首次上云） |
| `import_doubao.py` | 豆包云端会话导入（豆包无本地稳定存储） |

## API 参考

### Sync API（API Key 认证，格式 `ws_xxx`）
```
GET  /health                    # 健康检查
POST /pull                      # 拉取会话（limit/offset 分页、last_sync_at 增量；全量池——见下方「全池拉取契约」）
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
（hermes / deepseek-harness / workbuddy / opencode / reasonix / openclaw）。A 设备（hermes）必须能看到
B 设备（workbuddy）推上来的会话，否则跨 Agent 内容对桌面端不可见，与 README 声明的
「every client pulls the full pool (all agents)」相悖。此前服务端按 `agent` 过滤 `/pull`
（客户端又总是携带 `agent=hermes`），导致 hermes 客户端永远拉不到 workbuddy 会话，
是代码与文档背离的 bug，2026.08.22.4 修正。

**角色分工**：客户端自身 agent 只决定**推送**什么（push 侧按本地存储 + 外来会话
owner 注册表打 `agent_type`，见 `mcp/adapters/*.py` 的 foreign 路由）；**接收**侧一律全池，
客户端按会话的 `agent_type`/`profile_name` 字段把内容路由回本机各 Agent 存储。

**仍然生效的过滤（与 agent 无关，勿一并移除）**：`hidden=1` 的会话与消息（软删除/回收站、
子 agent 折叠后隐藏的孤儿行）不下发；`/pull` 增量分支按 `last_synced_at/started_at` 水位线、
消息按 `timestamp` 增量过滤；分页按 `limit/offset`。

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
GET  /web/register              # 注册页面（自建数学验证码，邀请码可选，支持 ?code= 预填）
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