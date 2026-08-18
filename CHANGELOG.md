# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，
版本号采用日期格式 `YYYY.MM.DD.N`（与客户端自动更新版本号一致）。

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
