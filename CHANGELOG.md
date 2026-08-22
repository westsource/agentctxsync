# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，
版本号采用日期格式 `YYYY.MM.DD.N`（与客户端自动更新版本号一致）。

## [2026.08.22.7] - 2026-08-22

### Changed
- **codex 适配器更名为 deepseek-harness**：实际运行的 Agent 是 DeepSeek Harness（codex CLI 配 DeepSeek 模型，rollout 存储格式）。适配器（`mcp/adapters/deepseek_harness.py`）、Agent 注册表、帮助页/模板颜色/README/架构文档中的标识全部由 `codex` 更名为 `deepseek-harness`（`HERMES_SYNC_AGENT=deepseek-harness`）；legacy `codex:` 前缀 id 入站时归一化为新 agent 类型；服务端存量 `agent_type='codex'` 数据迁移为 `deepseek-harness`。存储路径不变（`~/.codex` rollout 格式）。已知限制：harness 桌面版无法渲染外部写入的 rollout 会话（数据同步与 CLI 读取正常，桌面 UI 显示空白/偶发崩溃——harness 应用侧限制）。客户端版本 bump 至 `2026.08.22.7` 触发自动更新

## [2026.08.22.6] - 2026-08-22

### Fixed
- **`/pull` 增量拉取返回完整消息集（修复"幽灵会话"）**：增量分支此前按会话级 `last_synced_at/started_at` 返回会话、却按 `timestamp > 水位线` 过滤消息——某会话被对端设备周期重推（`last_synced_at` 刷新）而消息较旧时，客户端收到的会话不带任何消息，本地只写入 session 行（带服务端灌入的 message_count、零消息行），成为桌面可见但无内容的"幽灵会话"（实测：workbuddy 设备周期推送导致 hermes 客户端拉取 5 个 workbuddy 会话全部为空）。现在 `/pull` 对返回的每个会话**始终下发完整消息集**（与"每页返回 limit 个完整会话"的设计一致；客户端按 `(session_id, role, timestamp)` 幂等去重，重发旧行无副作用）。决策记录于 `server/sync.py::pull_sync` 注释与回归测试 `test_incremental_pull_serves_full_message_sets`。纯服务端改动，客户端无需更新（版本号保持 `2026.08.22.3`）

## [2026.08.22.5] - 2026-08-22

### Fixed
- **`/push` 空内容消息毫秒级去重（防重复复发）**：hermes 会话重建后重推的消息时间戳存在亚毫秒精度漂移（同一消息 `1780323802.979` 与 `1780323802.9798274` 各存一份），精确三元组与内容兜底都无法命中——尤其 content 为空（hermes 工具调用型消息）时内容兜底直接跳过，重复行持续累积。现在服务端 push 对**空 content 行**额外按毫秒截断时间戳（`trunc(x::numeric, 3)`）判重：与库内已有行同毫秒即视为重复，且同一批次内后到的重建副本也会被批内追踪去重。非空行不受影响（内容兜底已覆盖，且避免误伤 codex 同毫秒不同消息）。回归测试：`test_empty_content_ms_precision_duplicate_deduped`、`test_empty_content_ms_duplicate_within_same_push`、`test_nonempty_same_ms_distinct_messages_not_deduped`。纯服务端改动，客户端无需更新（版本号保持 `2026.08.22.3`）

## [2026.08.22.4] - 2026-08-22

### Fixed
- **`/pull` 全池拉取（full-pool）不再按 agent 过滤**：此前客户端在拉取请求体携带 `agent` 字段，服务端会据此只下发该 agent 的会话（如 hermes 客户端永远看不到 workbuddy/codex 设备推上来的会话，与文档声明的「全量池——不过滤 agent」背离）。现在服务端忽略请求体的 `agent` 字段，`/pull` 无论传入什么 agent 都返回工作空间全部可见会话及其消息；客户端只按自身 agent 决定**推送**什么，不再决定**接收**什么。`/api/projects/pull` 本就全量返回（无 agent 过滤），补回归测试锁定（`test_agent_param_ignored_full_pool`、`ProjectsPullTest`）。该决策已记录到 `docs/ARCHITECTURE.md`「全池拉取契约」及服务端 `pull_sync`/客户端 pull 方法注释，标注勿改回。纯服务端改动（客户端仅注释），客户端无需更新（版本号保持 `2026.08.22.3`）

## [2026.08.22.3] - 2026-08-22

### Changed
- **路径分隔符统一为 `/`**：服务端规范存储与返回均为正斜线 `/`（`sessions.cwd`、`sessions.git_repo_root`、`projects.primary_path`、`project_folders.path`），Windows 反斜线在 push 入库时归一化、pull 返回时归一化；历史数据由 `scripts/migrate-path-sep.py` 一次性迁移（dry-run 默认，`--apply` 写库）。客户端 pull 写本地时按本机已有会话/项目路径的分隔符对齐写入，与本地一致并合并，避免因分隔符差异把同一路径/项目文件夹插成两条。架构约定见 `docs/ARCHITECTURE.md`。客户端版本 bump 至 `2026.08.22.3` 触发自动更新

## [2026.08.22.2] - 2026-08-22

### Changed
- **MCP 更新检查改为惰性方式**：客户端首次更新检查延迟由「启动后 15 秒」改为「启动后 1 分钟」（充分避开 host agent 启动读取/更新峰值），之后仍每 `HERMES_SYNC_UPDATE_INTERVAL`（默认 1 小时）检查一次。空闲 agent 启动后不再过早主动打版本接口。客户端版本 bump 至 `2026.08.22.2` 触发自动更新

## [2026.08.22.1] - 2026-08-22

### Added
- **设备访问明细按 agent 区分客户端版本**：一台设备可安装多个 agent（`HERMES_SYNC_AGENT` 各自独立，每个 agent 是独立 MCP 实例、版本可能不同）。现在客户端（`mcp/server.py`）每次同步（push / pull / 项目同步）在请求体额外携带 `agent`，服务端 `access_device` 改为按 `(device_id, agent, channel)` 粒度聚合并记录该 agent 的客户端版本。「API 设备访问明细」页（`/web/admin/access/devices`）每设备一行，点击展开显示各 agent 的 Agent / 客户端版本 / 域名 / IP / 最后访问明细；多 agent 时版本列显示「agent → 版本」徽标。兼容旧客户端：未上报 agent 的请求归入 `unknown` 组，不影响既有统计。老库通过 `init_db()` 幂等 `ALTER TABLE` + 重建主键迁移。客户端版本 bump 至 `2026.08.22.1` 触发自动更新

## [2026.08.21.1] - 2026-08-21

### Changed
- **Web 字号统一为标准刻度**：工作空间详情页会话标题补 `text-sm`（此前无字号类继承 16px 基线）、项目卡标题由 `text-[15px]` 归一为 `text-sm font-semibold`；接入帮助页三大步骤区头由 `text-xl` 收敛为 `text-[17px]`（与全站卡片区头一致）、步骤数字圆点同归 17px、Agent 卡标题字重由 `font-bold` 对齐 `font-semibold`、FAQ 答案去除刻度外的 `text-[13px]` 归为 `text-sm`。两页仅保留标准刻度（24 / 17 / 14 / 12 / 11px），与全站其余页面一致
### Added
- **设备访问明细显示客户端版本**：客户端（`mcp/server.py`）每次同步（push / pull / 项目同步）在请求体携带安装版本（持久化于 `.hermes-sync-version`，缺省回退内置常量）；服务端 `requestlog` 中间件将其写入 `access_device.client_version`（无版本请求保留旧值，COALESCE 语义），「API 设备访问明细」页（`/web/admin/access/devices`）新增「客户端版本」列显示最后同步时版本。老库通过 `init_db()` 幂等 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 迁移。客户端版本 bump 至 `2026.08.21.1` 触发自动更新

## [2026.08.20.6] - 2026-08-20

### Fixed
- **Hermes 子 agent 会话折叠**：启用子 agent 时，主 agent 与子 agent 的对话此前被同步为多条独立会话。现在客户端（`mcp/adapters/hermes.py`）读取时按 `parent_session_id` 把子会话消息归并进主会话（按时间戳排序、重算消息数），子会话不再单独推送；子 agent 消息带 `meta.subagent` 标记，Web 会话查看器显示「子 agent」徽标。存量已同步的孤儿子会话用 `scripts/migrate-fold-subagents.py` 软隐藏（dry-run 默认，`--apply` 写入）。客户端版本 bump 至 `2026.08.20.6` 触发自动更新

## [2026.08.20.5] - 2026-08-20

### Added
- **问题反馈页面**：新增 `/web/feedback`，已登录用户可提交问题或建议（标题 / 分类 / 详细描述）；普通用户仅查看自己的反馈，管理员查看全域反馈并可标记「已处理 / 重新打开」。数据落库 `feedback` 表（`init_db()` 幂等建表），侧边栏新增「问题反馈」入口（zh-CN / en 双语）

## [2026.08.20.4] - 2026-08-20

### Added
- **仪表盘「设备数」卡片可点击**：点击弹出跨工作空间同步设备列表（设备 / 所有者 / 工作空间 / 最后同步 / 累计同步会话 / 累计同步消息），并显示设备所属用户显示名；管理员登录显示全域所有设备，普通用户仅显示自己的设备
- **回归测试**：`test_dashboard_devices.py`（仪表盘设备列表按角色分域）、`test_admin_access.py`（访问统计「今日」桶按日期对象命中）

### Fixed
- **访问统计顶部「今日」卡片恒为 0**：`web_admin_access` 用 `date.today().isoformat()`（字符串）查找以 `datetime.date` 为键的 `days` 字典，永不命中导致今日卡片恒 0；改为按 `date.today()` 命中

### Changed
- **侧边栏**：访问统计与邀请管理互换位置（访问统计仍仅管理员可见）

## [2026.08.20.3] - 2026-08-20

### Changed
- **访问统计分组显示**：今日统计卡片由四个独立卡片改为按类型分组——「WEB 页面（域名 / IP 直连）」与「API 访问（域名 / IP 直连）」；按日明细表列序同步改为 Web·域名 / Web·IP / API·域名 / API·IP

### Added
- **API 设备访问明细**：新增 `access_device` 表按设备（device_id）记录每日域名/IP 通道请求次数（`/push` `/pull` `/status` 等同步请求自动归因）；管理页点击「API 访问」卡片进入 `/web/admin/access/devices`，可查看哪些机器走域名、哪些走 IP 直连及最后访问时间

## [2026.08.20.2] - 2026-08-20

### Fixed
- **MCP 客户端兼容 mcp SDK v2**：`pip install mcp` 自 2026-07-28 规范重构（mcp 2.x）起不再提供低层 `Server.list_tools()`/`Server.call_tool()` 装饰器，Hermes 等客户端启动即报 `AttributeError: '_SyncServer' object has no attribute 'list_tools'`。客户端现按 SDK 时代自适应：v1 保持装饰器注册，v2 改用 `add_request_handler`（`(ctx, params) -> ListToolsResult/CallToolResult`），并通过中间件捕获会话维持后台同步日志通知；其余 API（`stdio_server`/`run()`/`create_initialization_options()`/`mcp.types`）两代共用。旧版客户端需重新下载客户端包（帮助页 zip）或手动替换 `mcp/server.py` 后重启 Agent

## [2026.08.20.1] - 2026-08-20

### Changed
- **会话查看器消息操作按钮统一为图标**：消息气泡内的「删除」按钮由文字改为垃圾桶图标（与「复制」图标按钮风格一致）；已删除消息显示恢复图标；复制成功反馈保持对勾图标
- **工作空间会话列表显示档案（profile）**：会话列表每条新增档案徽标（hermes 会话显示 `magic`/`default` 等所属档案；非 hermes 代理不显示），与已有档案过滤联动，便于跨档案定位会话

## [2026.08.19.2] - 2026-08-19

### Changed
- **访问统计按类型拆分**：`access_stats` 新增 `kind` 列（`web` = `/web/*` 页面与 `/` 落地页，`api` = 拉取/推送及全部 API），主键改为 `(stat_date, channel, kind)`；`init_db()` 启动时自动迁移存量表（历史行回填为 `api`）。管理页新增今日四象限卡片（域名 Web / 域名 API / IP Web / IP API）与按日六列明细表

## [2026.08.19.1] - 2026-08-19

### Added
- **管理员访问统计**：新增 `/web/admin/access` 页面，按日统计请求数并区分域名访问（Host 为域名，经 nginx HTTPS 代理）与 IP 直连（Host 为 IP:port）；数据落库 `access_stats` 表（`init_db()` 启动时幂等建表），统计排除静态资源与健康检查

## [2026.08.18.3] - 2026-08-18

### Changed
- **id 方案升级：canonical id 全部裸 id**（不再有 `codex:`/`reasonix:`/`workbuddy:`/`<profile>:` 前缀）。归属存于 `agent_type` 列（sessions/messages），hermes 档案存于 `profile_name` 列（projects 新增 `profile` 列）。服务端保留入站兼容层：旧客户端推送的带前缀 id 自动规范化为裸 id + 列归属，混合版本可用。客户端 `canonicalize/localize` 不再加前缀；外来会话注册表升级为 `{id: agent}`，推送按注册的归属打标。Windows 上 magic:/workbuddy: 会话现在可以正常拉取（裸 id 是合法文件名）
- **生产迁移**：`scripts/migrate-id-scheme.py`（dry-run 默认，`--apply` 写入；同裸 id 碰撞报告并跳过，绝不静默合并），README/ARCHITECTURE 已同步

### Fixed
- **服务端内容兜底去重覆盖 reasonix**：reasonix 桌面规范化重写转录（剥时间戳 + 前置系统提示词）使 `(role, timestamp)` 三元组去重失效，每轮周期同步把相同内容重复入库；现对 reasonix 会话启用同内容兜底（codex 等保留三元组去重，避免误折叠合法重复的工具输出）
- **reasonix 本地拉取内容级去重**：规范化文件（无时间戳）重拉时不再按回退时间戳重复追加，本地转录不再无限增长
- **opencode 消息读取按时间戳排序**：消息文件以随机 id 命名，按文件名排序导致转录顺序随机；改为按消息时间戳排序

## [2026.08.18.2] - 2026-08-18

### Fixed
- **Codex 适配器兼容 Codex Desktop 0.142+**：会话文件改为 `sessions/YYYY/MM/DD/` 分区存放（新写入也落入对应分区），旧版扁平目录仍兼容；识别新行格式 `session_meta`/`event_msg`/`turn_context`/`compacted`（此前全部被当成空消息同步，产生大量垃圾消息）；工具调用/输出映射为 `tool` 角色（Web 查看器折叠卡片渲染）；`developer` 角色归入 `system`；`reasoning` 内部思考跳过；响应行同时读取顶层 `timestamp` 字段
- **Codex 消息时间戳消歧**：Codex 会把同一毫秒的多个不同条目打上相同时间戳，而同步池按 `(session_id, role, timestamp)` 三元组去重，会把不同内容的消息误判为重复而折叠丢失；适配器对冲突时间戳做确定性微调（+1ms 步进），保证每条消息唯一
- **推送不再饥饿**：`sync_push` 原先每次只读最新 50 个会话，超过 50 个会话的本地存储中较旧会话永远无法推送到服务器；改为全量读取、按 20 会话/3000 消息双上限分批推送（"pushes everything it holds" 契约）
- **服务端并发推送去重竞态**：两个客户端同时推送同一会话时，消息去重快照（请求开始时读取）会失效，同一三元组在不同 id 下重复入库；新增 `uq_messages_dedup` 部分唯一索引兜底（启动时自动清理存量重复），插入改为 `ON CONFLICT DO NOTHING` + 逐行三元组复查，id 冲突才重试
- **内容兜底去重限定 hermes/reasonix**：服务端「同内容视为重复」的兜底本为 hermes 的 message-alternation repair 设计，会误折叠 codex 等代理的重复工具输出（如多次相同命令输出）；现仅对 hermes 与 reasonix 生效（reasonix 桌面会规范化重写转录：剥时间戳 + 前置系统提示词，导致三元组去重失效、每轮周期同步重复入库）
- **Windows 文件名冒号安全**：`workbuddy:`/`magic:` 等带冒号的远端会话 id 在 Windows 上写入文件名时会静默变成 NTFS 备用数据流（可见文件为 0 字节空壳、内容藏入隐藏流、适配器永远读不到）；新增 `validate_file_id`（文件名型适配器 codex/reasonix 使用），Windows 上直接跳过此类会话，不再半写

## [2026.08.18.1] - 2026-08-18

### Fixed
- **Hermes 0.20+ 会话标题唯一索引适配**：拉取会话的标题与本地已有会话冲突（`UNIQUE constraint failed: sessions.title`）时自动加 ` (N)` 后缀，避免整批同步失败（此前同步反复失败重试，加剧与桌面端的 SQLite 锁竞争，导致 `session storage was busy`）

## [2026.08.17.1] - 2026-08-17

### Added
- **开放注册**：注册不再强制要求邀请码，邀请码改为可选（填写则正常核销并授予对应套餐）；邀请码注册原有流程保留
- **帮助页改版**：下载客户端步骤改为胶囊切换；三步纵向平铺并统一标题/说明样式；工作空间选择移入步骤 2
- **Agent 配色统一**：共享 `_macros.html`，6 个 Agent（Hermes/WorkBuddy/Codex/opencode/Reasonix/OpenClaw）各有独立标识色
- **同步水位绑定服务器身份**：切换服务器自动全量重拉，避免旧水位导致会话永远无法同步
- **同步分批**：拉取每批 15 会话、推送按会话数+消息数双上限分批，防止大批量同步超时
- **sync_pull 支持 full 参数**：可手动触发全量拉取
- **部署脚本支持 SSH 密钥认证**，服务名修正为 agentctxsync

### Fixed
- 接入帮助页验证标签双冒号（"验证：: Hermes"）
- 帮助页顶部工作空间选择块导致的容器嵌套错误

## [2026.08.16.4] - 2026-08-16

### Added
- **全池同步（full-pool sync）**：客户端不再局限于本机已有档案，服务端会话池全量下发、按 id 前缀路由回各 Agent 本地存储
- **推送续传（push continuations）**：会话跨设备续写时，追加消息正确合并到远端已有会话
- **WorkBuddy 引导（onboarding）**：新用户接入 WorkBuddy 的一站式引导流程
- **拉取重试**：本地存储锁冲突时自动重试（`fix(mcp): retry pull write on local-store lock`）

### Fixed
- WorkBuddy 驱动器根路径 cwd 的 slug 化（末尾不再出现多余连字符）
- 本地存储写锁竞争导致拉取偶发失败

### Changed
- UI/UX 打磨：会话列表、状态展示、交互细节优化

## [2026.08.16.3] - 2026-08-16

### Added
- **三步接入帮助向导（help wizard）**：下载客户端 → 注册 → 验证，逐步引导
- **下载时服务端地址预填**：客户端 zip 内 README 自动填入当前服务器地址（Key 仍为占位符防泄露）
- i18n 清理与补充

### Changed
- 帮助页结构与文案重构

## [2026.08.16.2] - 2026-08-16

### Added
- **WorkBuddy 适配器**：第 6 个受支持的 Agent（canonical id 前缀 `workbuddy:`）
- **全部会话页（all-sessions）**：跨 Workspace 聚合浏览所有会话
- **配额机制（quota）**：按用户/Workspace 的会话存储配额控制
- UI / i18n 大版本重构

## [2026.08.15.1] - 2026-08-15

### Added
- **隐藏 → 删除重命名**：会话/消息的 soft-hide 语义升级为回收站（trash）
- 会话/消息回收站（可恢复，数据不物理删除）

### Fixed
- 补充遗漏的 trash/delete 翻译键（i18n）

## [2026.08.14.1] - 2026-08-14

### Added
- **MIT License**（中英双语 LICENSE 文件）
- **英文 README + 简体中文镜像**，双语文档结构
- **CONTRIBUTING 贡献指南**

### Changed
- 项目 slug 列截断为 5 字符（带悬浮提示）
- 账号级语言偏好（记忆用户选择，不再每次会话重置）
- 管理端权限收紧（admin 操作校验强化）
- 内联静态资源：Tailwind / Alpine.js 本地化（离线可用）

## [2026.08.13.1] - 2026-08-13

### Added
- **开源发布**（clean history 重写）：Agent Contexts Sync v1 初始版本
- 跨设备、跨 Agent 会话同步（Hermes / Codex / opencode / Reasonix / OpenClaw / WorkBuddy）
- 多租户架构：多用户 + 多 Workspace 隔离 + 独立 API Key
- Web 管理界面：登录/注册（邀请码）、会话查看器、管理后台、中英双语
- 客户端自动更新（SHA256 校验 + 原子替换 + 备份回滚）
- 项目同步（projects.db）、数据导出/导入（Markdown / JSON.gz）

---

> 本项目的完整开发历史在开源前已重写为干净历史；`2026.08.13.1` 为开源发布基线版本。
