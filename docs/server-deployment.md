# Agent Context Sync — 服务端部署指南

## 1. 服务器信息

| 项目 | 值 |
|------|-----|
| IP | `YOUR_SERVER_IP` |
| SSH | `root`（密码自行设置，切勿写入文档） |
| OS | Ubuntu (Linux)（其他 Linux 发行版亦可） |
| Python | 3.12 (venv) |

## 2. 架构概览

```
YOUR_SERVER_IP
├── agentctxsync (:8765)         — 会话同步服务 (FastAPI，多租户)
└── postgres (:5432)             — PostgreSQL（自备，如 Docker 容器 agentctxsync-db）
     └── DB: agentctxsync
```

## 3. Docker 容器

| 容器名 | 镜像 | 端口 | 用途 |
|--------|------|------|------|
| agentctxsync-db | pgvector/pgvector:pg18 | 5432 | PostgreSQL 数据库 |

> PostgreSQL 由部署环境自行准备（示例为既有 PG 容器 agentctxsync-db）；
> `docker-compose.yaml` 不随本仓库分发，需自备。

## 4. 数据库

- **数据库名**: `agentctxsync`
- **用户**: `agentctxsync`
- **密码**: `<POSTGRES_PASSWORD>`（见 `server/.env.example`，不要提交真实密码）
- **DSN**: `postgresql://agentctxsync:<POSTGRES_PASSWORD>@localhost:5432/agentctxsync`
- 表结构由 `main.py` 启动时的 `init_db()` 幂等创建/迁移（见 `db.py`），无需手工建表

### 表结构

**users** — id, username, password_hash, display_name, is_admin, is_active, created_at, last_login_at, plan (free/unlimited), must_change_password, lang

**workspaces** — id, name, user_id, api_key, description, created_at

**invites** — id, code (HSYNC-XXXXXXXX), created_by, used, used_by, revoked, expires_at, note, created_at, grant_plan (free/unlimited)

**sessions** — id, workspace_id, title, model, message_count, started_at, hidden/hidden_at (软隐藏), pinned (置顶), profile_name (来源档案), agent_type, meta, ... (共 52 列)

**messages** — id, session_id, workspace_id, role, content, timestamp, hidden/hidden_at, agent_type, meta, ... (共 27 列)

**sync_state** — device_id, workspace_id, last_sync_at, sessions_synced, messages_synced

**projects** — id (canonical，default 裸 id / 命名档案 `<profile>:<id>`), workspace_id, slug, name, description, icon, color, board_slug, primary_path, created_at, archived, hidden/hidden_at, merged_into, agent_type

**project_folders** — workspace_id, project_id, path, label, is_primary, added_at (跨设备增量合并)

**project_remap** — workspace_id, old_id, new_id (同名合并路由记录)

**quota_config** — plan (PK), max_sessions (NULL=不限), allowed_agents (TEXT[]，NULL/空=全部放行)。种子：free=200、unlimited 无限制

**audit_log** — id, ts, event, user_id, workspace_id, device_id, code, detail（配额拒绝等事件审计）

## 5. 服务端文件结构

> 2026-08 起服务端由单文件 `server.py` 按业务域拆分为 14 个模块，入口为 `main.py`
> （旧 `server.py` 已废弃）。部署根目录由 `/opt/hermes-sync-mcp` 迁移至 `/opt/agentctxsync`。

```
/opt/agentctxsync/
├── main.py               # 应用组装 (FastAPI + 路由挂载 + 启动入口)
├── config.py             # 环境变量与常量（启动时强制校验）
├── db.py                 # 连接池 / get_conn / init_db（幂等建表迁移）/ 配额策略查询
├── render.py             # Jinja2 渲染 / flash / 请求上下文 (ContextVar)
├── auth.py               # 认证域：JWT / 密码 / 依赖 / 登录注册路由
├── invites.py            # 邀请码管理域
├── workspace.py          # 工作空间域 (Web + REST)
├── admin.py              # 管理域 (用户/空间管理)
├── sync.py               # 同步域 (/pull /push /status ...)
├── projects.py           # 项目同步域
├── client_update.py      # 客户端更新域 (打包/清单/下载，含 CLIENT_VERSION)
├── web_help.py           # 接入帮助域
├── agents.py             # Agent 注册表 (帮助页/下载包)
├── translations.py       # 国际化翻译 (zh-CN / en, 324 键)
├── venv/                 # Python 3.12 虚拟环境
├── templates/
│   ├── base.html         # 基础布局 + 侧边栏 + 语言切换
│   ├── login.html        # 登录页
│   ├── register.html     # 注册页 (邀请码可选)
│   ├── change_password.html  # 修改密码页
│   ├── landing.html      # 落地页
│   ├── dashboard.html    # 主仪表盘
│   ├── all_sessions.html # 全部会话（跨工作空间统一列表）
│   ├── workspace_detail.html  # 工作区详情 (会话列表/搜索/隐藏/项目)
│   ├── session_messages.html  # 会话消息查看器 (搜索/隐藏)
│   ├── trash_sessions.html    # 会话回收站
│   ├── trash_messages.html    # 消息回收站
│   ├── admin_users.html       # 用户管理
│   ├── admin_workspaces.html  # 全局工作区管理
│   ├── admin_invites.html     # 邀请管理
│   ├── help_hermes.html       # 接入帮助页
│   └── _macros.html           # 模板宏
├── static/
│   ├── favicon.svg        # 网站图标
│   ├── tailwind.js        # Tailwind CSS (本地)
│   ├── alpine.min.js      # Alpine.js (本地)
│   └── icon_preview.html  # 图标预览页
├── mcp/                   # MCP 客户端分发包（/api/client/download 按此打包）
│   ├── server.py / updater.py / run.bat / run.sh
│   └── adapters/          # 各 Agent 本地存储适配器 (base/codex/hermes/opencode/reasonix/openclaw/workbuddy)
├── backups/               # 数据库备份目录（每日 3:00 自动，保留 7 天）
├── backup.sh              # 备份脚本
├── data/                  # 数据目录
└── client/                # 旧版客户端包（遗留，可删）
```

## 6. 服务配置

### 环境变量 (config.py 读取)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| HERMES_SYNC_PG_DSN | postgresql://agentctxsync:...@localhost:5432/agentctxsync | 数据库连接 |
| HERMES_SYNC_MASTER_KEY | 无默认值（必须设置） | 主 API 密钥 |
| HERMES_SYNC_JWT_SECRET | secrets.token_hex(32) (每次重启随机) | JWT 签名密钥 |
| HERMES_SYNC_TOKEN_EXPIRE | 24 | JWT 有效期 (小时) |
| HERMES_SYNC_PUBLIC_URL | 空 | 对外公开地址；设置后所有下载的客户端包默认指向它（域名迁移用）；未设置时客户端包默认取下载请求的来源地址 |

### systemd 服务

> 推荐通过 drop-in 注入环境变量（升级服务文件时无需改动 unit 主体）：
> `/etc/systemd/system/agentctxsync.service.d/10-env.conf`（PG_DSN + MASTER_KEY）与
> `jwt-secret.conf`（JWT_SECRET，固定值避免重启失效）。

```ini
# /etc/systemd/system/agentctxsync.service
[Unit]
Description=Agent Context Sync Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/agentctxsync
ExecStart=/opt/agentctxsync/venv/bin/python /opt/agentctxsync/main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### 备件 Cron

```
0 3 * * * /opt/agentctxsync/backup.sh >> /opt/agentctxsync/backups/backup.log 2>&1
```

备件通过 `docker exec agentctxsync-db pg_dump -U agentctxsync` 导出，gzip 压缩，保留最近 7 天。

## 7. 常用运维命令

### 服务管理

```bash
systemctl status agentctxsync    # 查看状态
systemctl restart agentctxsync   # 重启服务
systemctl stop agentctxsync      # 停止服务
journalctl -u agentctxsync -f    # 实时日志
journalctl -u agentctxsync -n 50 # 最近 50 行日志
```

### 数据库操作

```bash
# 进入数据库
docker exec -it agentctxsync-db psql -U agentctxsync -d agentctxsync

# 常用查询
SELECT id, username, is_admin, is_active FROM users;
SELECT id, name, user_id FROM workspaces;
SELECT COUNT(*) FROM sessions;
SELECT COUNT(*) FROM messages;
```

### 手动备份恢复

```bash
# 备份
docker exec agentctxsync-db pg_dump -U agentctxsync -d agentctxsync | gzip > /opt/agentctxsync/backups/manual_\$(date +\%Y\%m\%d_\%H\%M\%S).sql.gz

# 恢复
gunzip -c /opt/agentctxsync/backups/agentctxsync_XXXXXXXX_XXXXXX.sql.gz | docker exec -i agentctxsync-db psql -U agentctxsync -d agentctxsync
```

### 部署新版本（远程）

```bash
# 本地执行（脚本自动备份→上传→重启 agentctxsync 服务→健康检查）
# 注意：模块化布局下 systemd ExecStart 必须指向 main.py（脚本本身不修改 systemd 配置）
$env:DEPLOY_SSH_HOST = "YOUR_SERVER_IP"
$env:DEPLOY_SSH_PASSWORD = "..."      # 或 SSH key 免密（~/.ssh/id_ed25519）
$env:DEPLOY_REMOTE_DIR = "/opt/agentctxsync"
$env:DEPLOY_SERVICE = "agentctxsync"
python scripts/deploy-remote.py
```

## 8. Web UI 路由

### 认证页面 (需登录)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | / | 根路径：未登录 → 落地页；已登录 → 跳转 /web/ |
| GET | /web/ | 主仪表盘 |
| GET | /web/login | 登录页 |
| POST | /web/login | 登录提交 |
| GET | /web/logout | 登出 |
| GET | /web/register | 注册页 (邀请码可选，支持 ?code= 预填) |
| POST | /web/register | 注册提交 |
| POST | /web/change-password | 修改密码 |
| POST | /web/update-profile | 更新个人信息 |
| GET | /web/set-language/{lang} | 切换语言 (zh-CN/en) |
| POST | /web/workspace/create | 创建工作区 |
| POST | /web/workspace/{id}/update | 更新工作区 |
| GET | /web/workspace/{id} | 工作区详情 (置顶/搜索/隐藏/项目列表) |
| GET | /web/workspace/{id}/session/{sid} | 会话消息查看器 (搜索/隐藏) |
| GET | /web/workspace/{id}/trash | 会话回收站（已隐藏会话，可恢复） |
| GET | /web/workspace/{id}/session/{sid}/trash | 消息回收站（已隐藏消息，可恢复） |
| GET | /web/workspace/{id}/session/{sid}/export | 导出单个会话 (Markdown) |
| POST | /web/workspace/{id}/session/{sid}/hide | 隐藏会话 (可逆，/pull 停止下发) |
| POST | /web/workspace/{id}/session/{sid}/unhide | 恢复会话 |
| POST | /web/workspace/{id}/session/{sid}/message/{mid}/hide | 隐藏消息 |
| POST | /web/workspace/{id}/session/{sid}/message/{mid}/unhide | 恢复消息 |
| GET | /web/workspace/{id}/export | 导出整个工作区 (JSON.gz) |
| POST | /web/workspace/{id}/import | 导入备份 (JSON/JSON.gz) |
| GET | /web/workspace/{id}/delete | 删除工作区 |
| POST | /web/workspace/{id}/regen-key | 重新生成 API Key |
| GET | /web/help | 接入帮助页（旧入口 /web/help-hermes 301 跳转） |
| GET | /web/download/mcp-client?ws_id={id}&agent=X | 下载 MCP 客户端 zip |
| GET | /web/invites | 邀请管理 (所有登录用户) |
| POST | /web/invite/create | 创建邀请码 |
| POST | /web/invite/{id}/revoke | 撤销邀请码 |

### 管理员页面 (需 admin)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /web/admin/users | 用户管理 |
| POST | /web/admin/user/create | 创建用户 |
| GET | /web/admin/user/{id}/edit | 编辑用户页 |
| POST | /web/admin/user/{id}/edit | 提交用户编辑 |
| GET | /web/admin/user/{id}/toggle | 启用/禁用用户 |
| GET | /web/admin/workspaces | 全局工作区管理 |
| GET | /web/admin/invites | 邀请管理 (旧入口，跳转 /web/invites) |

### API 端点 (Bearer Token 认证)

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/auth/register | 注册 |
| POST | /api/auth/login | 登录获取 JWT |
| GET | /api/me | 当前用户信息 |
| POST | /api/me/change-password | 修改密码 |
| GET | /api/workspaces | 工作区列表 |
| POST | /api/workspaces | 创建工作区 |
| DELETE | /api/workspaces/{id} | 删除工作区 |
| POST | /api/workspaces/{id}/regen-key | 重新生成 Key |
| GET | /api/client/manifest?agent=X&v=版本 | 客户端版本对比 + sha256 清单 |
| GET | /api/client/download?agent=X | 下载客户端 zip (内嵌 manifest) |
| GET | /api/admin/users | 所有用户 |
| POST | /api/admin/users/{uid}/toggle | 启用/禁用用户 |
| GET | /api/admin/workspaces | 所有工作区 |

### 同步端点 (API Key 认证)

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /pull | 拉取远端会话 |
| POST | /push | 推送本地会话 |
| GET | /status/{device_id} | 设备同步状态 |
| GET | /sessions | 会话列表 |
| GET | /users | 设备列表 |
| POST | /api/projects/push | 推送项目 + folders (同名合入最早项目 + remap) |
| POST | /api/projects/pull | 拉取项目 + folders + remap (不含已隐藏) |
| GET | /health | 健康检查 |

## 9. 国际化 (i18n)

- **支持语言**: 简体中文 (zh-CN)、English (en)
- **默认语言**: zh-CN
- **切换方式**: 侧边栏语言切换按钮 / 登录页底部语言链接
- **持久化**: `lang` 用户级偏好（存 DB，随账号跨设备，经 JWT claim 携带）；登录前由 cookie 决定（cookie 兼容）
- **翻译键数**: 324 个 (zh-CN 和 en 完全对齐)
- **覆盖范围**: 侧边栏、仪表盘、全部会话、工作区详情、会话查看器、回收站、用户管理、工作区管理、邀请管理、接入帮助页、登录/注册页、错误/成功消息

## 10. 注意事项

1. **JWT Secret 建议固定** — 未设置时每次重启重新生成、已登录用户会失效；通过 drop-in
   （`/etc/systemd/system/agentctxsync.service.d/jwt-secret.conf`）注入固定值
2. **备份脚本依赖 Docker** — 通过 `docker exec agentctxsync-db pg_dump -U agentctxsync` 导出
3. **服务自动重启** — systemd 配置了 `Restart=always`，崩溃后 5 秒自动重启
4. **`init_db()` 幂等迁移** — 表结构变更（如新增列）由 `main.py` 启动时自动执行，
   无需手工建表/迁移
5. **磁盘空间** — 备份与日志增长需留意；默认保留 7 天备份

## 11. 配额机制（plan / quota_config）

server 内置一套**通用配额执法机制**：策略存于数据库、执法在 server，两者通过数据库解耦，改动即时生效（server 每次 push 读取配置，无缓存、无重启）。

- **users.plan** — `free` | `unlimited`（默认 `free`；存量用户自动补 `free`）
- **invites.grant_plan** — 使用邀请码注册时授予的套餐（默认 `unlimited`，创建邀请码时可选；不用邀请码注册的新用户同样为默认 `unlimited`）
- **quota_config** — 按 plan 配置：
  - `max_sessions`：该套餐用户的全局活跃会话数上限（NULL = 不限；默认 free = 200）
  - `allowed_agents`：允许同步的 Agent 类型白名单（NULL / 空数组 = 全部允许）
- **audit_log** — 审计事件表（`quota_rejected` 由 server 写入；`plan_changed` 等运营操作由运营侧写入）

### 执法行为

- `POST /push` 只对**新建会话**拦截：agent 白名单 + 用户全局活跃会话数（`archived=0`，跨该用户所有 Workspace 汇总）。
- 已有会话的更新与拉取不受影响，降低配额不会破坏已同步的数据池。
- 超限返回 HTTP `403`，`detail` 为 `agent_not_allowed` / `quota_exceeded_sessions`，并在 `audit_log` 记录 `quota_rejected`（含 user/workspace/device/code/detail）。
- 未知 plan（quota_config 无对应行）按**不限制**处理（宽松失败，不误伤合法同步）。
- Master API Key 不受配额限制。

### 自部署用户如何调整配额

配额管理界面属于运营侧（私有），自部署用户直接改数据库即可：

```sql
-- 某用户改为无限版
UPDATE users SET plan = 'unlimited' WHERE username = '...';
-- 免费版会话上限改为 500
UPDATE quota_config SET max_sessions = 500 WHERE plan = 'free';
-- 免费版只允许 Hermes 同步
UPDATE quota_config SET allowed_agents = ARRAY['hermes'] WHERE plan = 'free';
```

### 运营只读账号（最小权限）

运营侧只读消费统计数据，不读取会话内容。表级授权无法排除单列，若要彻底隔离会话内容（`content` / `reasoning` / `system_prompt`），请改用列级授权：

```sql
CREATE ROLE ops_reader LOGIN PASSWORD '<强密码>';
GRANT CONNECT ON DATABASE agentctxsync TO ops_reader;
GRANT USAGE ON SCHEMA public TO ops_reader;
-- 业务表只读
GRANT SELECT ON users, workspaces, invites, sessions, messages,
      sync_state, projects, project_folders, project_remap,
      quota_config, audit_log TO ops_reader;
-- 仅配置表可写（套餐调整；server 侧无权写配置）
GRANT UPDATE (plan) ON users TO ops_reader;
GRANT UPDATE (max_sessions, allowed_agents) ON quota_config TO ops_reader;
```

> 运营侧改动 plan / quota_config 后，下一次 push 即生效。
