# Agent Contexts Sync

> **English**: [README.md](README.md) · **简体中文**: 本文档

> 官网：http://www.agentctxsync.com

跨设备、跨 Agent 同步会话的完整解决方案。支持多用户、多 Workspace 隔离，基于 PostgreSQL 后端，通过 MCP Server 实现 Agent 启动时自动同步。

主要特性：
- **多租户**：多用户 + 多 Workspace 隔离，每个 Workspace 独立 API Key
- **跨 Agent 同步**：Hermes / OpenAI Codex / opencode / Reasonix / OpenClaw / WorkBuddy 共享同一会话池，A 的会话可被 B 拉取并写入其本地存储
- **Web 管理界面**：登录/注册（邀请码）、信息概览、Workspace 管理、会话查看器、管理后台
- **数据安全**：会话/工作区一键导出（Markdown / JSON.gz）与导入
- **项目同步**：Hermes 项目（侧边栏项目列表）随会话跨设备同步，同名合并 + 路径并集
- **数据保留与检索**：会话/消息可删除（软删除，回收站可恢复）、置顶排序、标题/内容搜索
- **接入帮助**：内置接入帮助页，按 Agent 一键下载 MCP 客户端（含安装说明与注册命令）
- **客户端自动更新**：客户端定时从服务端拉取新版本，逐文件校验后自动替换，重启 Agent 即生效
- **国际化**：简体中文 / English 双语界面

## 支持的 Agent

| Agent | 本地存储 | canonical id 前缀 | 写入约束 |
|-------|----------|-------------------|----------|
| Hermes | `%LOCALAPPDATA%\hermes`（POSIX：`~/.hermes`）下扫描全部档案：`state.db`（default）+ `profiles/<name>/state.db`（命名档案）(SQLite) | 无（裸 id，兼容存量）；非 default 档案叠加 `<profile>:` 前缀 | SQLite 事务 |
| OpenAI Codex | `~/.codex/sessions/rollout-*.jsonl` | `codex:` | append-only；标题需追加 `session_index.jsonl`；codex 靠 backfill 感知新会话 |
| opencode | `$XDG_DATA_HOME/opencode/storage/` (JSON 文件) | `opencode:` | `.tmp`+rename 原子写；外来会话经 idmap 分配 `ses_` id |
| Reasonix | `%APPDATA%\reasonix\sessions\*.jsonl` | `reasonix:` | append-only；运行中（有锁文件）的会话跳过 |
| OpenClaw | `~/.openclaw/agents/<id>/agent/openclaw-agent.sqlite` | `openclaw:` | schema 自动探测（实验性） |
| WorkBuddy | `~/.workbuddy/projects/<slug>/*.jsonl` + `workbuddy.db` | `workbuddy:` | JSONL 追加 + SQLite upsert；cwd 目录自动创建；写入的会话需重启 WorkBuddy 后出现（启动时 MIGRATE 扫描识别） |

每个 Agent 独立部署一个 MCP 客户端实例（`HERMES_SYNC_AGENT` 选择），全部接入同一 Workspace 即实现互相同步。新增 Agent 只需实现一个适配器（见 [docs/ADDING_AGENT.md](docs/ADDING_AGENT.md)），服务端与同步引擎零改动。

## 架构

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

### 多租户模型

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

- **Users**: 新用户通过管理员发放的邀请码自助注册（也可由管理员直接创建账号）
- **Workspaces**: 每个用户可创建多个 workspace，每个 workspace 有独立 API key
- **隔离**: 不同 workspace 之间的会话和消息完全隔离
- **同 workspace**: 同一 workspace 下的所有设备完全同步
- **管理员权限边界**: 管理员可管理用户、邀请码与全局工作区（元数据与开关），但**无法查看任何用户的空间内容、会话或消息**——空间数据（会话列表、消息内容、项目）仅对所属用户可见，管理页面只暴露元数据与管理操作。

## 快速开始

> 以下 `<SERVER_IP>` 均为占位符，请替换为你实际部署的服务器地址。

### 1. 服务器端部署

```bash
# 上传到目标服务器并执行
scp -r server/ scripts/ root@<SERVER_IP>:/tmp/hermes-sync/
ssh root@<SERVER_IP>
cd /tmp/hermes-sync/server
bash ../scripts/deploy-server.sh
```

部署完成后：
- API: `http://<SERVER_IP>:8765/health`
- Web UI: `http://<SERVER_IP>:8765/web/`
- 首次启动自动创建默认管理员 `admin`（随机密码，打印在服务端日志，**首次登录强制修改**）及其默认工作区（含 API Key，见服务端日志）

> 服务器 Python 依赖：`fastapi` `uvicorn` `psycopg2-binary` `jinja2` `markdown` `python-multipart`（`deploy-server.sh` 已包含，Markdown/Jinja2 用于 Web UI 渲染，python-multipart 用于 Web 表单解析）。
>
> 更详细的服务器部署、运维与备份说明见 [docs/server-deployment.md](docs/server-deployment.md)。

### 2. 注册用户与创建 Workspace（Web UI）

1. 打开 `http://<SERVER_IP>:8765/web/`，点击注册
2. 注册需要**邀请码**：管理员在 管理 → 邀请管理 页面创建邀请码（格式 `HSYNC-XXXXXXXX`），可设置有效期、备注，可随时撤销；也可复制带 `?code=` 参数的分享链接直接发给用户
3. 注册成功后自动创建「默认工作区」；也可以在信息概览点击 "+ 创建" 创建更多 workspace
4. 在 Workspace 详情页复制 API Key（格式 `ws_xxx`）

### 3. 本地 MCP 部署

**方式 A（推荐）**：登录 Web UI → 接入帮助（`/web/help`）→ 按你的 Agent 下载对应压缩包。按包内安装说明（README.md）解压、注册——注册命令中的 `<YOUR_API_KEY>` 需替换为帮助页中对应工作区的 API Key（下载包不再预填 Key，避免包被转发导致泄露）。完成后重启 Agent 即可。

**方式 B（手动）**：

```bash
# 选择 agent（hermes | codex | opencode | reasonix | openclaw | workbuddy），默认 hermes
export HERMES_SYNC_AGENT=codex

# 设置 workspace API key（格式 ws_xxx）
export HERMES_SYNC_API_KEY=ws_yourkeyhere

# 一键部署（注意：脚本内默认服务器地址为占位符，请设置 HERMES_SYNC_SERVER 为实际部署地址）
bash scripts/deploy-local-mcp.sh
```

> 每个 Agent 独立部署一份实例（各占一个 `HERMES_SYNC_AGENT` 与独立的锁文件），全部指向同一 Workspace API Key 即互相同步。新增 Agent 的接入流程见 [docs/ADDING_AGENT.md](docs/ADDING_AGENT.md)。
>
> MCP 客户端行为：启动时自动增量拉取一次；若远程为空（首次配对）自动推送本地数据完成引导；之后每 300 秒自动同步一次（可用 `HERMES_SYNC_INTERVAL` 调整）。

### 4. 迁移现有数据（可选）

将本地 Hermes `state.db` 中的历史会话推送到远程服务器：

```bash
python scripts/migrate-local-to-server.py ws_yourkeyhere http://<SERVER_IP>:8765
```

## 配置

### 服务器端环境变量

| 变量 | 说明 |
|------|------|
| `HERMES_SYNC_PG_DSN` | PostgreSQL 连接字符串 |
| `HERMES_SYNC_MASTER_KEY` | Master API key（非同步用） |
| `HERMES_SYNC_JWT_SECRET` | Web UI JWT 签名密钥 |
| `HERMES_SYNC_TOKEN_EXPIRE` | JWT 过期时间（小时，默认 24） |
| `HERMES_SYNC_PUBLIC_URL` | 对外公开地址（如 `https://www.example.com`），打包进客户端并展示在帮助页；未设置时客户端包默认取下载请求的来源地址 |

### 本地 MCP 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HERMES_SYNC_AGENT` | `hermes` | 本地存储适配器：`hermes`/`codex`/`opencode`/`reasonix`/`openclaw`/`workbuddy` |
| `HERMES_SYNC_SERVER` | `http://<SERVER_IP>:8765` | 远程服务器地址（按实际部署配置） |
| `HERMES_SYNC_API_KEY` | - | **Workspace API Key**（必须，格式 `ws_xxx`） |
| `HERMES_SYNC_INTERVAL` | `300` | 自动同步间隔（秒） |
| `HERMES_SYNC_AUTO_SYNC` | `1` | 后台自动同步开关（`0` 关闭；手动工具调用仍可用） |
| `HERMES_SYNC_AUTO_UPDATE` | `1` | 客户端自动更新开关（`0` 关闭） |
| `HERMES_SYNC_UPDATE_INTERVAL` | `86400` | 更新检查间隔（秒，默认 24 小时） |

## 客户端自动更新

MCP 客户端内置自动更新：启动后约 15 秒检查一次、之后每 24 小时检查一次，通过
服务端 `/api/client/manifest`（版本对比）与 `/api/client/download`（带 SHA256
清单的 zip）拉取新版本，逐文件校验后**就地原子替换**并保留上一版本备份
（`.bak-<版本>/`），**重启 Agent 后生效**（MCP server 无法自重启，也不打断
正在进行的会话）。

- 关闭：`HERMES_SYNC_AUTO_UPDATE=0`；调整间隔：`HERMES_SYNC_UPDATE_INTERVAL`
- **下载包内的服务器默认地址**：每个下载的客户端包，其 `HERMES_SYNC_SERVER` 代码默认值都会被设为提供下载的服务端地址（按请求来源），若服务端配置了 `HERMES_SYNC_PUBLIC_URL` 则使用该值。迁移存量客户端到新地址：在服务端设置 `HERMES_SYNC_PUBLIC_URL` 为新地址并 bump `CLIENT_VERSION`——客户端从旧地址（保持可达直到全部升级完）拉取新包，重启 Agent 后自动切换。
- 校验失败/网络不可达时保留旧文件，仅记录日志，不影响同步
- 回滚：将 `.bak-<版本>/` 中的文件复制回 `mcp/` 目录并删除 `.hermes-sync-version`
- **版本发布流程**：修改客户端后，同时 bump `mcp/updater.py` 与 `server/server.py`
  中的 `CLIENT_VERSION` 常量，部署服务端后所有客户端在下次检查时自动升级

## 多档案同步（Hermes profiles）

Hermes 桌面应用支持多档案（profile）：每个命名档案是**完全独立的存储目录**，各自拥有
独立的 `state.db`（default 在平台根，命名档案在 `<根>/profiles/<name>/`）。客户端**扫描
平台根下全部档案**并逐一同步，一个 MCP 实例即可覆盖本机所有档案，互不串档。

### 档案发现规则

```
平台根 <root>（只按平台默认位置，不读取 HERMES_HOME / active_profile）
├─ Windows: %LOCALAPPDATA%\hermes
└─ POSIX:   ~/.hermes

扫描:
├─ <root>/state.db                  → default 档案（裸 id，兼容存量）
└─ <root>/profiles/<name>/state.db  → 命名档案（如 magic），按名称排序
```

- **MCP 子进程不继承档案**：hermes 的档案切换通过进程内 ContextVar 实现，不会传递给
  MCP server 子进程环境变量，桌面端也不写 `active_profile` 标记、不传入 `HERMES_HOME`，
  因此客户端直接**扫描 `profiles/` 目录**发现全部档案，而不是解析「当前激活档案」。
- **单一 watermark**：增量拉取水位线保存在平台根 `<root>/.hermes-sync-watermark`，
  所有档案共用；每次拉取按服务端 `last_synced_at` 增量进行，不会串档。

### 会话 id 与隔离

| 档案 | canonical id | 说明 |
|------|--------------|------|
| default | 裸 id（`20260808_180012_0c275f`） | 存量兼容，行为不变 |
| 非 default（如 magic） | `magic:20260808_180012_0c275f` | 前缀与 `agent_type:` 风格一致，服务端主键 `(workspace_id, id)` 天然区分 |

- 服务端**零改动**：靠 id 前缀区分不同档案的会话，`profile_name` 列顺带填充档案名。
- **Push 全量合并**：推送时读取本机全部档案的 `state.db`，default 会话保持裸 id，
  命名档案会话带 `<profile>:` 前缀，合并为一份列表上报。
- **Pull 按档案路由**：拉取时按 id 前缀路由回各档案的 `state.db`；本机不存在的档案
  （其他电脑独有的档案）或非 hermes agent 的会话直接跳过，不写入本地。

### 跨电脑同步示例

```
A 电脑: default + magic 档案     B 电脑: default + magic 档案

t1: A 只有 default；B 只有 default
    → 双方裸 id 会话互相合并，行为与单档案一致
t2: A 新建 magic 档案；B 尚未创建
    → A 推送 magic: 前缀会话；B 拉取时跳过（本机无 magic 档案）
t3: A、B 均有 magic 档案
    → 两侧 magic 会话互相合并（id 均带 magic: 前缀）；default 照常同步
```

> 注意：客户端只同步**本机已存在**的档案。某档案只存在于远端设备时，本机拉取会跳过
> 该档案的会话（watermark 正常推进）；本机创建同名档案后，该档案的历史会话即可从
> 服务端拉回。

### 项目同步（projects.db）

Hermes 桌面的项目（侧边栏项目列表）存储在**每档案独立的 `projects.db`**（
`<档案目录>/projects.db`），与 `state.db` 并存。客户端按档案遍历所有 `projects.db`，
实现跨设备项目同步：

- **Push**：读取本机所有档案的 `projects.db`，合并为 canonical 列表推送
  （default 档案 id 为裸 id，命名档案为 `<profile>:<id>`）。
- **同名合并**：同一 workspace 内 `(profile, slug)` 相同但 id 不同的项目，
  服务端合入**最早创建**的项目：folders 取并集，并记录 remap
  （`old_id → new_id`）供客户端收敛。
- **Pull**：拉取远程项目 + remap 记录，按 id 前缀路由回各档案的 `projects.db`；
  folders 增量合并（新路径插入、已有路径更新，不删除），slug 冲突时自动去重后缀。
- **Web 会话关联**：Web 工作空间页的项目列表会用会话的 `cwd` 对项目 folders
  做前缀匹配，展示每个项目下的会话（与 hermes 原生 `project_for_path` 逻辑一致；
  跨设备路径是各机器自己的，Web 展示为并集）。
- **工具**：`project_push` / `project_pull` 手动触发；周期同步（默认 300s）也会
  顺带同步项目。

## 数据保留与检索（删除 / 回收站 / 置顶 / 搜索）

- **删除 / 恢复（软删除 soft-hide）**：Web 会话列表与消息详情支持「删除 / 恢复」操作，数据**保留
  不删除**（软 `hidden` 标记）、完全可逆。被删除的项进入**回收站**：`/web/workspace/{id}/trash`
  （会话回收站）与 `/web/workspace/{id}/session/{sid}/trash`（消息回收站），可随时恢复。删除后：
  - 服务端 `/pull` 不再下发已删除的会话/消息（数据仍在服务端，恢复后重新下发）；
  - `/push` 更新已有行时不会重置其 `hidden` 标记；
  - 会话列表与消息查看器默认不显示已删除项；回收站页面可查看并恢复。
- **置顶排序**：会话列表按 `pinned` 置顶优先排序（📌 标记）。当前仅用于排序展示，
  暂无置顶管理入口。
- **搜索**：Workspace 会话列表 `?q=` 按标题 / id 模糊过滤；会话详情页 `?q=` 按消息内容
  模糊过滤（LIKE 通配符已转义，避免注入）。

## 服务器迁移流程

服务端地址的优先级：`config.yaml` 的 `HERMES_SYNC_SERVER` 环境变量 > `server.py`
代码默认值（当前为部署服务器地址）。客户端自动更新会连同新的默认地址一起下发。

**无缝迁移（推荐，旧服务器保持在线直到全部客户端更新完）**：
1. 在新服务器部署新版服务端（含新地址默认值），bump `CLIENT_VERSION`
2. 保持旧服务器在线（客户端更新仍需从旧地址拉取）
3. 等待各客户端完成自动更新（启动后 15 秒 / 每 24 小时检查）
4. 各客户端重启 Agent 后自动连上新服务器
5. 确认无客户端仍连旧服务器后，再下线旧服务器

**旧服务器直接下线的场景**：客户端无法再从旧地址拉取更新，需要手动处理——
在各机器 `config.yaml` 的 `env` 段添加 `HERMES_SYNC_SERVER: http://新地址:8765`
（环境变量优先），或手动复制新版 `server.py` 到 `mcp/` 目录。

## MCP 工具

| 工具 | 说明 |
|------|------|
| `sync_status`（别名 `hermes_sync_status`） | 查看同步状态（远程会话/消息数、设备最近同步时间） |
| `sync_pull`（别名 `hermes_sync_pull`） | 从远程拉取会话到本地（参数: limit，默认 50；后台增量拉取时按水位线分页获取全部） |
| `sync_push`（别名 `hermes_sync_push`） | 推送本地会话到远程（自动分批，避免大请求超时） |
| `sync_full`（别名 `hermes_sync_full`） | 完整同步（先 push 再 pull） |
| `project_push` | 推送本地全部档案的 projects.db 项目到远程（同名合并由服务端处理） |
| `project_pull` | 从远程拉取项目到本地 projects.db（应用 remap、按档案路由） |

> `sync_*` 为中性工具名（所有 Agent 通用）；`hermes_sync_*` 为兼容别名，存量 Hermes 注册不受影响。

后台行为：
- 启动时自动**增量**拉取一次（本地 `.hermes-sync-watermark` 水位线 + 5 分钟时钟容差）；远程为空时自动推送本地数据（新设备首次配对引导）
- 周期性自动同步（默认 300 秒）
- 单写者锁：双 `serve` 实例只允许一个进程运行后台同步，避免本地存储竞争；自动更新使用独立更新锁
- 消息去重基于 `(session_id, role, timestamp)` 三元组，跨设备幂等

## API 端点

### Sync API（API Key 认证，格式 `ws_xxx`）
```
GET  /health                    # 健康检查
POST /pull                      # 拉取会话（limit/offset 分页、last_sync_at 增量、agent 过滤）
POST /push                      # 推送会话（upsert + 消息去重；按服务端真实列过滤；agent_type/meta）
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
GET  /web/register              # 注册页面（需邀请码，支持 ?code= 预填）
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
POST /web/admin/invite/create                     # 创建邀请码（有效期/备注）
POST /web/admin/invite/{id}/revoke                # 撤销邀请码
```

## 数据库表结构

### users
`id`, `username`, `password_hash`, `display_name`, `is_admin`, `is_active`, `created_at`, `last_login_at`

### workspaces
`id`, `name`, `user_id` (FK), `api_key`, `description`, `created_at`, 唯一约束 `(user_id, name)`

### invites
`id`, `code` (格式 `HSYNC-XXXXXXXX`), `created_by` (FK), `used`, `used_by`, `revoked`, `expires_at`, `note`, `created_at`

### sessions
复合主键: `(workspace_id, id)`, 外键: `workspace_id -> workspaces(id) ON DELETE CASCADE`
多 Agent 扩展列: `agent_type`（默认 `hermes`，存量数据自动归为 hermes）、`meta` (JSONB，承载各 Agent 特有字段)
数据保留/排序扩展列: `hidden`/`hidden_at`（软删除，可逆）、`pinned`（置顶排序）、`profile_name`（来源档案）

### messages
复合主键: `(workspace_id, session_id, id)`, 外键: `workspace_id -> workspaces(id) ON DELETE CASCADE`
多 Agent 扩展列: `agent_type`、`meta` (JSONB)；软删除列: `hidden`/`hidden_at`

### sync_state
主键: `(device_id, workspace_id)`, 外键: `workspace_id -> workspaces(id) ON DELETE CASCADE`

### projects
复合主键: `(workspace_id, id)`（canonical id：default 档案为裸 id，命名档案为 `<profile>:<id>`）
列: `slug`（同 (workspace, profile) 唯一，同名合并依据）、`name`、`description`、`icon`、`color`、
`board_slug`、`primary_path`、`created_at`、`archived`、`hidden`/`hidden_at`、`merged_into`、`agent_type`

### project_folders
复合主键: `(workspace_id, project_id, path)`；列: `label`、`is_primary`、`added_at`；
跨设备增量合并（新路径插入、已有路径更新）

### project_remap
复合主键: `(workspace_id, old_id)`；列: `new_id`（同名合并后 old_id → new_id 路由记录，供客户端收敛）

## 与 Hermes 0.20+ 的兼容性（SQLite 锁竞争）

**现象**：Hermes 桌面端报 `request timed out after 30s: session.resume`。

**根因**：Hermes 内置的 SQLite 3.50.4 存在 WAL-reset 损坏 bug（见 `hermes doctor` / errors.log 警告），Hermes 因此强制回退 `journal_mode=DELETE`——该模式下**任何写事务都会阻塞并发读者**。本 MCP 客户端的后台同步（启动拉取、周期同步）直接写 Hermes 的 `state.db`，当它持有写锁时，Hermes 自己的 `session.resume`（读 `state.db`）会一直等待，超过桌面端 30 秒 RPC 超时后报错（errors.log 中表现为 `database is locked`）。

**本客户端已采取的缓解措施**：
- SQLite 写连接 `busy_timeout` 缩短为 5 秒：拿不到锁立即失败并推迟到下一同步周期，绝不长时间占用/等待锁
- 启动自动拉取延迟 8 秒，避开 Hermes 启动与会话恢复的读高峰
- 后台拉取改为**增量**（本地 `.hermes-sync-watermark` 水位线 + 5 分钟容差），不再每次全量重扫
- 新增 `HERMES_SYNC_AUTO_SYNC=0` 可完全关闭后台自动同步（手动调用工具仍可用），彻底消除与 Hermes 的锁竞争

**建议（治本）**：运行 `hermes update` 将 Hermes 内置 SQLite 升级到 3.51.3+（或 `hermes doctor` 修复 embedded runtime），恢复 WAL 并发模式后读写互不阻塞，以上缓解措施可不再需要。

## 已知问题与排障

| 现象 | 原因 | 处理 |
|------|------|------|
| 同步不上去（服务端 `total_sessions` 不增长） | 旧服务端对 Hermes 0.20 新增列（如 `system_prompt_hash`）报 500 | 升级服务端到含列过滤的版本；客户端下个周期自动恢复 |
| `request timed out after 30s: session.resume/create` | Hermes 0.20 SQLite 锁竞争（见上节） | 升级 SQLite 或设 `HERMES_SYNC_AUTO_SYNC=0` |
| 客户端一直不更新 | `HERMES_SYNC_AUTO_UPDATE=0` 或服务端不可达 | 检查 `mcp-stderr.log` 的 `Update check` 日志 |
| 下载包注册后报认证失败 | `<YOUR_API_KEY>` 未替换为真实 Key | 在接入帮助页复制对应工作区 Key |

## License

[MIT](LICENSE) © 2026 道荣（黄超）、露（张渊） · [中文版](LICENSE.zh-CN.md)
