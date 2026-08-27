# Agent Context Sync

> **简体中文**: 本文档 · **English**: [README.md](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/westsource/agentctxsync/actions/workflows/ci.yml/badge.svg)](https://github.com/westsource/agentctxsync/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](mcp/)

> 官网：https://www.agentctxsync.com

**在任意设备、任意 AI Agent 之间，无缝接着上次继续。**

Agent Context Sync 让 Hermes、DeepSeek Harness、opencode、Reasonix、OpenClaw、WorkBuddy 等 AI Agent，以及你手头所有设备，共享同一个会话池并自动同步——不用再复制粘贴历史。在 A 设备开始的对话，到 B 设备直接接着聊，也能从一个 Agent 无缝交接给另一个。每条会话都会备份到你自己掌控的服务器上——设备丢了、坏了，历史也随时找得回。

## 解决的问题

- **每次切换，上下文就断了。** 换个设备、换个 Agent，就得复制粘贴历史、重新交代背景。Agent Context Sync 让完整会话池随处可用、自动同步——坐下就能直接接着上次聊。
- **本地数据，怕丢怕坏。** 一次崩溃、一次误删、一块硬盘损坏，多年的对话可能付之东流。每条会话都同步到你掌控的服务器上，无论出了什么意外，历史都随时找得回。
- **主线和子任务，混成一团。** 委托出去的子代理对话容易淹没主线。Agent Context Sync 把它们折叠进父会话、附上徽章，让主线保持清爽、每段都有迹可循。
- **多人共享，不该牺牲隐私。** 多档案、多项目、多台设备，会话与项目各归各位、跨机同步；团队共享同一会话池，管理员却只管理用户、邀请码与工作区，始终读不到任何人的会话内容。

## 价值

这个工具给你带来的两大价值——并在下面的汇总表里延伸到第三层。

### 1. 无缝续接——任意设备 × 任意 Agent

你的会话都活在同一个共享会话池里：在一台设备开始的对话，坐到另一台设备上时已经在等着你——无论它属于哪个 Agent。

- **任意设备**：新设备在首次配对时自动拉取你的完整历史——无需复制粘贴、无需重新交代背景
- **任意 Agent**：Hermes、DeepSeek Harness、opencode、Reasonix、OpenClaw 与 WorkBuddy 读写同一个会话池，你可以把一个话题在不同 Agent 之间无缝交接
- **在哪儿都能续**：子代理折叠进父会话（带徽章），多档案与项目跨机器同步且互不串扰

### 2. 服务端备份——会话数据不丢失

每条会话都会推送到你自建的服务器上，所以单台机器崩溃、档案被清空、或本地存储损坏，都不会让历史数据随之而去。

- **自建自管**：数据存在你掌控的服务器上，而非第三方云端
- **扛得住本地任何意外**：即使某台设备或某个档案被清空，完整历史也能从服务端找回
- **导出 / 导入 / 恢复**：一键导出（Markdown / JSON.gz）与导入，外加软删除回收站，连误操作都能复原

### 它如何改变你的日常

| 没有 Agent Context Sync | 有 Agent Context Sync |
|------|------|
| 每个 Agent、每台设备都是一个孤岛 | 所有 Agent + 设备共享一个自动同步的会话池 |
| 换设备要靠复制粘贴历史 | 新设备在首次配对时自动拉取你的历史 |
| 设备丢失或损坏 = 会话丢失 | 历史已备份在服务端，随时可找回 |
| 子代理线程淹没主线对话 | 子代理折叠进父会话并带徽章标识 |
| 多档案 / 项目在多台机器上碎片化 | 一个客户端覆盖全部 profile，会话与项目同步且零服务端改动 |
| 团队共享 = 所有人都能看到一切 | 租户隔离：管理员只管基础设施，读不到你的数据 |

## 支持的 Agent

覆盖你真正在用的 AI Agent——**Hermes、DeepSeek Harness、opencode、Reasonix、OpenClaw、WorkBuddy**。

每个 Agent 部署各自的客户端、连到同一个 Workspace 即可相互同步。各 Agent 的本地存储位置、id 规范与写入约束等技术细节见 [docs/SUPPORTED_AGENTS.md](docs/SUPPORTED_AGENTS.md)；新增一个 Agent 只需实现一个适配器（[docs/ADDING_AGENT.md](docs/ADDING_AGENT.md)），服务端与同步引擎零改动。

## 界面截图

![仪表盘 — 工作区概览、同步状态、配额与最近会话](docs/screenshots/02-dashboard.png)

![全部会话 — 跨工作区统一列表，支持搜索与筛选](docs/screenshots/03-all-sessions.png)

> **全局搜索** — 跨工作空间全文搜索会话标题与消息内容,仅限自己的工作空间(admin 同),支持深链跳转到具体消息([docs/SEARCH.md](docs/SEARCH.md))。

![工作区详情 — 会话列表、项目与同步设备](docs/screenshots/04-workspace.png)

![会话查看器 — Markdown 渲染与代码块](docs/screenshots/05-session-viewer.png)

## 快速开始

> 下面所有 `<SERVER_IP>` 都是占位符——请替换为你实际部署的地址。

### 1. 部署服务器

```bash
# 上传到目标服务器并执行
scp -r server/ scripts/ root@<SERVER_IP>:/tmp/hermes-sync/
ssh root@<SERVER_IP>
cd /tmp/hermes-sync/server
bash ../scripts/deploy-server.sh
```

部署完成后：
- API：`http://<SERVER_IP>:8765/health`
- Web UI：`http://<SERVER_IP>:8765/web/`
- 首次启动会自动创建默认管理员 `admin`（随机密码打印在服务器日志里；**首次登录强制修改**），连同默认工作区（含 API Key——见服务器日志）

> 更详细的服务器部署、运维与备份说明：[docs/server-deployment.md](docs/server-deployment.md)。

### 2. 注册用户并创建 Workspace（Web UI）

1. 打开 `http://<SERVER_IP>:8765/web/` 点击 Register 注册——默认开放注册（自建数学验证码，邀请码可选）
2. 注册成功后自动创建「Default Workspace」；可在概览页点「+ Create」创建更多工作区
3. 从工作区详情页复制 API Key（格式 `ws_xxx`）

### 3. 安装 MCP 客户端（每个 Agent）

**方式 A（推荐）**：登录 Web UI → 部署帮助（`/web/help`）→ 下载对应 Agent 的安装包。按包内说明解压并注册——将 `<YOUR_API_KEY>` 替换为帮助页上对应工作区的 API Key（安装包不再预填 Key，转发不会泄露）。完成后重启 Agent。

**方式 B（手动）**：

```bash
# 选择 agent（hermes | deepseek-harness | opencode | reasonix | openclaw | workbuddy），默认 hermes
export HERMES_SYNC_AGENT=deepseek-harness

# 设置 workspace api key（格式 ws_xxx）
export HERMES_SYNC_API_KEY=ws_yourkeyhere

# 一键部署（注意：脚本内默认服务器地址为占位符，请设置 HERMES_SYNC_SERVER 为实际部署地址）
bash scripts/deploy-local-mcp.sh
```

每个 Agent 各自部署一个实例（一个 `HERMES_SYNC_AGENT` 值 + 独立锁文件）；将它们都指向同一 Workspace API Key 即可相互同步。客户端行为：启动约 8 秒后做一次增量拉取（首次配对时自动上传本地数据作为 bootstrap），之后每 300 秒自动同步（`HERMES_SYNC_INTERVAL`）。

## 同步工具

| 工具 | 说明 |
|------|------|
| `sync_status`（别名 `hermes_sync_status`） | 查看同步状态（远端会话/消息数、各设备最近同步时间） |
| `sync_pull`（别名 `hermes_sync_pull`） | 从远端拉取会话到本地（`limit`，默认 50；`full` 忽略水位线） |
| `sync_push`（别名 `hermes_sync_push`） | 推送本地会话到远端（自动分批避免大请求超时） |
| `sync_full`（别名 `hermes_sync_full`） | 全量同步（先推后拉） |
| `project_push` | 将所有本地 profile 的 projects.db 推送到远端（同名合并由服务端处理） |
| `project_pull` | 从远端拉取项目到本地 projects.db（应用 remap，按 profile 路由） |

`sync_*` 是所有 Agent 通用的中性命名；`hermes_sync_*` 是兼容别名。后台引擎细节：[docs/OPERATIONS.md](docs/OPERATIONS.md#sync-tools-background-behavior)。

## 了解更多

- [docs/OPERATIONS.md](docs/OPERATIONS.md) — 配额、客户端自动更新、多档案与项目同步、数据保留、服务器迁移、SQLite 锁兼容、排障
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — 服务器与本地环境变量参考
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 系统架构、多租户模型、数据库结构、API 参考
- [docs/server-deployment.md](docs/server-deployment.md) — 部署、运维、备份、配额 SQL
- [docs/SUPPORTED_AGENTS.md](docs/SUPPORTED_AGENTS.md) — 各 Agent 的存储、id 与写入约束
- [docs/ADDING_AGENT.md](docs/ADDING_AGENT.md) — 新增 Agent 适配器
- [docs/SEARCH.md](docs/SEARCH.md) — 全局搜索（跨工作空间、租户隔离、消息定位）

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md) ——开发环境搭建、代码风格、i18n 规范与 PR 流程。

## License

[MIT](LICENSE) © 2026 道荣（黄超）、露（张渊） · [English](LICENSE)