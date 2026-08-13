# 新增 Agent 接入指南

本文档说明如何把一个新的 agent（如 Claude Code、Cursor 等）接入 Agent Contexts Sync，
使其会话/消息能与 Hermes、Codex、opencode、reasonix、openclaw 互相同步。

## 总览：接入 = 一个适配器文件 + 一行注册

新增 agent **不需要**修改服务端（数据库、`/push` `/pull` 协议、Web UI）或同步引擎
（锁、去重、分页、bootstrap）。全部工作都在 MCP 客户端侧：

```
mcp/adapters/<name>.py        # 适配器实现（从 _template.py 复制）
mcp/adapters/__init__.py      # _ADAPTER_MODULES 加一行
mcp/adapters/base.py          # AGENT_PREFIXES 分配一个前缀
mcp/tests/test_<name>.py      # fixture 往返单测
```

## 设计边界：永不需要改 vs 边界情况需评估

**永不需要改**（架构已泛化）：
- 数据库 schema：`agent_type` + `meta JSONB` 已承载任意 agent 字段
- Sync 协议：`/push` `/pull` `/status` `/sessions`（canonical id 前缀解析 + 去重三元组）
- 同步引擎：启动拉取、bootstrap、周期同步、单写者锁、分页
- Web UI 通用渲染：会话列表/查看器（role/content/title/model 均为通用列）

**边界情况需评估后才可能改动**：
1. 新 agent 的消息类型超出通用渲染（如新的富媒体 part）→ 仅需 Web UI 增加
   meta 驱动的渲染分支，协议不变
2. 本地存储加密（如历史 Codex 版本的 XOR）→ 仅适配器内部加解密，外部不变
3. 未来需要"按 agent 隔离的 workspace 语义"（默认是共享会话池）→ 服务端
   `/pull` 增加 `agent` 过滤参数即可，schema 不变
4. canonical 模型出现真正通用的新字段（如附件/图片）→ 在 `base.py` 演进字段
   列表，并回归全部适配器（每个适配器只需映射自己的对应字段）
5. 服务端 agent 注册表（`server/agents.py`）新增条目 → 帮助页与下载包自动生效

## 第 1 步：调研（必须，写入约束决定实现方式）

复制 `mcp/adapters/_template.py` 前，先按下列清单确认新 agent 的本地存储：

| 调研项 | 为什么要查 | 已有案例 |
|--------|-----------|----------|
| 数据目录 | 可能受环境变量/XDG/APPDATA 影响 | codex: `~/.codex/`（`CODEX_HOME` 可覆盖）；opencode: `$XDG_DATA_HOME/opencode/storage/`；reasonix: `%APPDATA%\reasonix\`；openclaw: `~/.openclaw/agents/<id>/` |
| 文件格式与 schema | 决定继承 `SQLiteAdapter` 还是 `JSONLAdapter` 或手写 | hermes/codex: SQLite/JSONL 各一例；opencode: JSON 文件（session/message/part 各一文件） |
| session id 生成与位置 | canonical id 前缀 + 本地 id 提取 | codex: 文件名中的 UUID；reasonix: 文件名主干；opencode: `ses_` 前缀 26 字符 |
| 写入约束 | **最高风险点** | codex: append-only + `.zst` 压缩 + `session_index.jsonl` 标题索引（backfill 后才进列表）；reasonix: append-only + 锁文件 + events 日志权威；opencode: `.tmp`+rename 原子替换 |
| 加密/完整性校验 | 决定能否直接读写 | codex 旧版曾有 XOR 加密；新版明文 |
| 索引/回填机制 | 写入后 UI 能否立即看到 | codex 靠 SQLite backfill；opencode 正在运行的实例有内存缓存（写前建议停实例） |
| 官方 API/MCP 桥 | 也许不用碰文件 | openclaw 提供 `mcp serve` 官方读写桥 |

> 参考实现：`mcp/adapters/codex.py`、`opencode.py`、`reasonix.py`、`openclaw.py`
> 是每个写入约束场景的具体示例。

## 第 2 步：实现适配器

1. `cp mcp/adapters/_template.py mcp/adapters/<name>.py`
2. 设置 `agent_type = "<name>"`（与 `AGENT_PREFIXES` 一致）
3. 实现 4 个方法：
   - `discover()` → 找到本地库路径，未安装返回 `None`
   - `read_sessions(limit)` → 本地格式转 canonical dict（`self.canonicalize()` 加前缀）
   - `write_sessions(sessions)` → canonical 转本地格式（`self.localize()` 去前缀），
     遵守第 1 步查明的写入约束；返回
     `{"imported", "updated", "new_messages", "duplicates"}`
   - `status()` → 本地会话/消息总数
4. 契约要点（详见 `base.py` docstring）：
   - canonical 会话必填 `id`（带前缀）+ `started_at`；消息必填
     `session_id`/`role`/`content`/`timestamp`
   - 消息去重键 `(session_id, role, timestamp)`；**永远不要复用远端消息 id**
   - 特有字段放 `meta`，键必须带 agent 前缀（如 `"<name>:foo"`）避免跨 agent 冲突
   - reasoning 内容统一映射到消息的 `reasoning` 字段

## 第 3 步：注册

1. `mcp/adapters/base.py` → `AGENT_PREFIXES` 添加前缀
   （检查 `split_agent_prefix` 的顺序——前缀冲突是唯一会产生数据串扰的风险点）
2. `mcp/adapters/__init__.py` → `_ADAPTER_MODULES` 添加
   `"<name>": "<module>"`

## 第 4 步：测试

1. 构造 fixture：按该 agent 真实格式造 2~3 个样例会话文件（不要依赖本机安装）
2. 往返单测（参考 `mcp/tests/` 现有用例）：
   - 读：fixture → `read_sessions()` → 断言 canonical 字段与 id 前缀
   - 写：`write_sessions()` 注入 → 再 `read_sessions()` → 断言往返一致
   - 幂等：同一批 sessions 写两次 → 第二次 `duplicates > 0` 且 `new_messages == 0`
   - 前缀：写入 `codex:` 前缀会话到本 adapter → 抛 `ValueError`（或按适配器语义拒绝）
3. 交叉同步（可选但推荐）：A adapter 推 → 服务端 → B adapter 拉取落地

## 第 5 步：部署

1. 按 `scripts/deploy-local-mcp.sh` 部署一份独立实例：
   `export HERMES_SYNC_AGENT=<name>` + 该 agent 的 API Key / 服务器地址
2. 服务端帮助页与 `/web/download/mcp-client` 由 agent 注册表驱动，自动生成该 agent
   的下载包（含预填 API Key 的 README）——无需改服务端代码
3. 在 README 的 agent 支持表格补一行

## 端到端验证清单

- [ ] `python -m py_compile mcp/adapters/<name>.py`
- [ ] `python -m mcp.tests.test_<name>` 全绿
- [ ] 对真实本地库跑 `python -m mcp.adapters.<name>` 自检（有该 agent 时）
- [ ] 双 agent 互推互拉验证交叉同步
- [ ] Hermes 存量链路回归（`python -m mcp.tests`）
