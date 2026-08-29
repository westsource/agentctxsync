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
| `db.py` | psycopg2 连接池、`init_db` 幂等建表迁移、配额策略查询、工作空间查询辅助 | — |
| `render.py` | Jinja2 渲染（executor 异步化）、flash 消息、请求作用域 ContextVar、中间件 | — |
| `translations.py` | i18n 翻译表（zh-CN / en） | — |
| `agents.py` | Agent 注册表（静态数据，驱动帮助页与客户端包生成） | — |
| `auth.py` | 认证域：PBKDF2 密码、JWT 签发/校验、API key 依赖、登录/注册/改密/语言、强制改密中间件 | `/`、`/web/login`、`/web/register`、`/web/change-password`、`/web/update-profile`、`/web/set-language/*`、`/web/logout`、`/api/auth/*` |
| `workspace.py` | 工作空间域：仪表盘、全部会话、会话查看器、CRUD、导出/导入、软删除/回收站、REST | `/web/`、`/web/all-sessions`、`/web/workspace/*`、`/api/me`、`/api/workspaces` |
| `sync.py` | 同步域：pull/push/status/sessions/users，配额执法与审计日志 | `/health`、`/pull`、`/push`、`/status/{device_id}`、`/sessions`、`/users` |
| `projects.py` | 项目同步域：slug 同名合并、folders 增量合并、remap 路由 | `/api/projects/push`、`/api/projects/pull` |
| `invites.py` | 邀请码域：邀请管理、创建/撤销 | `/web/invites`、`/web/invite/create`、`/web/invite/{id}/revoke` |
| `admin.py` | 管理域：用户/全局空间管理（仅管理员） | `/web/admin/*`、`/api/admin/*` |
| `client_update.py` | 客户端分发：zip 构建（运行时改写默认服务器/Agent + manifest 哈希）、下载端点 | `/api/client/manifest`、`/api/client/download` |
| `web_help.py` | 接入帮助域：帮助页、客户端包下载（由 `agents.py` 注册表 + `client_update.py` 驱动） | `/web/help`、`/web/help-hermes`（301）、`/web/download/mcp-client` |
| `search.py` | 全局搜索域：跨工作空间 + 租户隔离的会话/消息搜索，结果定位到具体消息（二期） | `/web/search` |

中间件注册顺序（`main.py`）：`flash_middleware`（render）→ `enforce_password_change`（auth），
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
| `adapters/opencode.py` | opencode storage/ 多文件（`ses_/msg_/prt_` id；外来会话首次写入分配新 `ses_` id 并持久化 canonical→local idmap，后续 pull 复用同一本地 id 保持去重稳定） |
| `adapters/openclaw.py` | OpenClaw 网关会话库（`sessions.json` 索引 + JSONL v3 transcript；`openclaw:server_id` 元数据保往返 id 稳定；运行中网关会覆写索引——建议关闭 OpenClaw 后同步） |

- **目录布局**：
  ```
  mcp/
  ├── server.py            # MCP 入口：工具面 + 后台任务 + 单写者锁
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
  详见「消息身份与幂等去重」）。
- **后台任务**（`server.py`）：启动 8s 增量拉取 → bootstrap push（首次配对）→ 每 300s
  周期同步（push → pull → projects push/pull）→ 自动更新（启动 60s 后、每小时）；单写者锁 +
  更新锁（双实例安全），详见「增量同步与水位线」与「客户端自动更新」。
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
  （hermes / workbuddy / reasonix 已端到端验证；其余在注册表中但分发与帮助页下线）。
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
| `e2e_multagent.py` | 多 agent 交叉同步 e2e：codex 推 → 服务端 → opencode 拉及反向，验证 id 稳定与重推幂等 |
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

### deepseek-harness（DeepSeek Harness / codex 格式）

- **存储布局**：`~/.codex/`（`CODEX_HOME` 可覆盖）下 `sessions/`，每会话一个
  `rollout-<ts>-<id>.jsonl`；0.142+ 按 `sessions/YYYY/MM/DD/` 分区，旧版扁平；`.zst`
  压缩文件跳过（无解压依赖）；标题在 `session_index.jsonl`（append-only 索引，last-wins）。
- **读取**：会话元数据取 `session_meta`（0.142+）/ 旧版 `{"meta":…}` 首行；conversation
  行取 `response_item`（或裸 payload）；`event_msg`/`turn_context` 生命周期事件跳过
  （turn_context 只贡献 model/cwd）；`compacted` 摘要保留为 assistant 消息；
  `reasoning` 跳过（内部思考不进池）；`function_call`/`custom_tool_call` 及 output 映射为
  `tool` 角色 + `tool_name`/`tool_call_id`（Web 折叠卡片渲染）；`developer` 角色归入
  `system`；标题从 `session_index.jsonl` 回填。
- **时间戳消歧 `_unique_ts`**：同毫秒大量条目 → 同三元组会在拉推往返静默塌缩；碰撞
  时间戳确定性 +1ms 上调至空闲（同文件恒等映射）。**新 JSONL 类适配器（omp）必读**。
- **外来会话 id 映射**：harness 桌面 backfill 只索引 UUID 形 rollout id——非 UUID 外来
  id（如 hermes 时间戳 id）写为映射 UUID（`.hermes-sync-idmap.json` 持久化，重拉复用
  同一本地 id 保持去重稳定）；UUID 形外来 id（workbuddy）直通。
- **写入**：新文件按当前时间落入 `YYYY/MM/DD` 分区（`session_meta` 头 + rollout 行）；
  已有文件按 id 定位（`_existing_path` 递归扫文件名中的 id）追加；标题变更追加到
  `session_index.jsonl`。写入约束 = append-only（服务端是去重权威，本地重复靠
  `(role, timestamp)` 集合拦截）。

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
| compaction 摘要 | `messages.compacted` + 摘要 assistant 消息（deepseek_harness 同款降级） |

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
   （复用 `deepseek_harness.py::_unique_ts` 模式）。

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
  时间戳消歧（复用 deepseek_harness 测试模式）。

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
GET  /web/login                 # 登录页面
GET  /web/register              # 注册页面（邀请码可选，支持 ?code= 预填）
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
GET  /web/help                                 # 接入帮助页（MCP 客户端接入帮助；/web/help-hermes 旧入口 301 跳转）
GET  /web/download/mcp-client?ws_id={id}&agent=X  # 下载 MCP 客户端 zip（Key 为占位符）
GET  /web/admin/users                             # 用户管理
POST /web/admin/user/create                       # 创建用户
GET  /web/admin/user/{uid}/edit                   # 编辑用户
POST /web/admin/user/{uid}/edit                   # 提交用户编辑（显示名/密码/管理员）
GET  /web/admin/user/{uid}/toggle                 # 启用/禁用用户
GET  /web/admin/workspaces                        # 所有空间管理（元数据与开关，不含会话内容）
GET  /web/invites                                 # 邀请管理（所有登录用户；/web/admin/invites 旧入口 303 跳转至此）
POST /web/invite/create                     # 创建邀请码（有效期/备注/授予套餐）
POST /web/invite/{id}/revoke                # 撤销邀请码
```