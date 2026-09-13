# 新增 Agent 接入指南

本文档说明如何把一个新的 agent（如 Claude Code、Cursor 等）接入 Agent Context Sync，
使其会话/消息能与 Hermes、DeepSeek Harness、opencode、reasonix、openclaw 互相同步。

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
2. 本地存储加密（如历史 codex 前身的 XOR；codex 引擎已移除，仅作参考）→ 仅适配器内部加解密，外部不变
3. 未来需要"按 agent 隔离的 workspace 语义"（默认是共享会话池）→ 服务端
   `/pull` 增加 `agent` 过滤参数即可，schema 不变
4. canonical 模型出现真正通用的新字段（如附件/图片）→ 在 `base.py` 演进字段
   列表，并回归全部适配器（每个适配器只需映射自己的对应字段）
5. 服务端 agent 注册表（`server/agents.py`）新增条目 → 帮助页与下载包自动生效

## 第 1 步：调研（必须，写入约束决定实现方式）

复制 `mcp/adapters/_template.py` 前，先按下列清单确认新 agent 的本地存储：

> 注：codex CLI 引擎（曾名 `deepseek_harness.py`/`codex.py`，存储 `~/.codex` rollout）已于 2026-09-06 移除并并入 dsh：
> 存量 `agent_type='deepseek-harness'` 数据迁移为 `dsh`。示例列以当前 agent 为准（dsh = 官方 DeepSeek Harness）。

| 调研项 | 为什么要查 | 已有案例 |
|--------|-----------|----------|
| 数据目录 | 可能受环境变量/XDG/APPDATA 影响 | dsh: `~/.dsh/`（`DSH_HOME` 可覆盖）；opencode: `$XDG_DATA_HOME/opencode/storage/`；reasonix: `%APPDATA%\reasonix\`；openclaw: `~/.openclaw/agents/<id>/` |
| 文件格式与 schema | 决定继承 `SQLiteAdapter` 还是 `JSONLAdapter` 或手写 | hermes: SQLite；dsh: 世代化 JSONL 事件日志（`session.vN.jsonl[.zstd]`，当前 v3；逐行 zstd 帧，需 `zstandard`）；opencode: JSON 文件（session/message/part 各一文件） |
| session id 生成与位置 | canonical id 前缀 + 本地 id 提取 | dsh: 目录名 `session-<uuid>`（外来 id 经 idmap）；reasonix: 文件名主干；opencode: `ses_` 前缀 26 字符 |
| 写入约束 | **最高风险点** | dsh: 世代化事件日志——写当前世代（v3）、冻结的前代逐字节不动、seq 连续、原子替换、cwd 漂移搬迁目录；reasonix: append-only + 锁文件 + events 日志权威；opencode: `.tmp`+rename 原子替换 |
| 加密/完整性校验 | 决定能否直接读写 | 历史 codex 前身旧版曾有 XOR 加密；现明文（引擎已移除） |
| 索引/回填机制 | 写入后 UI 能否立即看到 | dsh: 投影缓存文档写入时折叠（列表标题即时）；opencode 正在运行的实例有内存缓存（写前建议停实例） |
| 官方 API/MCP 桥 | 也许不用碰文件 | openclaw 提供 `mcp serve` 官方读写桥 |

> 参考实现：`mcp/adapters/dsh.py`、`omp.py`、`opencode.py`、
> `reasonix.py`、`openclaw.py` 是每个写入约束场景的具体示例。

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
   - **canonical 值必须可 JSON 编码**（str/int/float/bool/None/dict/list）。原生库里的二进制列
     （SQLite BLOB → Python `bytes`）不要拷进 canonical：push 的分块与请求编码都要
     `json.dumps`，一个不可编码的值曾让整台设备的推送全部失败。`SQLiteAdapter._map_cols`
     已统一丢弃 bytes/bytearray/memoryview，自写 SQL 读取的 adapter 需自行保证。
   - **只读上传型适配器**（`write_sessions` 是 no-op、不写回本地库，如 `chatgpt`）必须声明
     `stores_pulled_sessions = False`：pull 的完整性修复按 id 补回本地缺失的可见会话
     （见 ARCHITECTURE「本地删除不是删除信号」），只读适配器本地没有承载共享池会话的目标，
     不声明就会每轮把整个池子拉下来再丢弃。
5. **项目（可选，仅当该 agent 有本地项目清单）**：实现
   - `read_projects()` → canonical 项目列表（push 视图）
   - `write_projects(projects, remaps)` → 落库（pull 视图；返回 `{"imported": N}`）
   - `supports_projects = True`
   三条一起加：`False`（默认）时 `mcp/server.py` 会在周期同步里跳过项目阶段，工具面返回
   `Agent X has no local project store …`。契约要点（见 ARCHITECTURE「projects」「项目同名
   合并」「字段级乐观并发」）：
   - 项目标量 user-edit 字段 `name`/`primary_path`/`archived`/`description` 走字段级乐观
     并发：pull 落库的值必须与上次 pull 给的值**逐字一致**（路径字段按分隔符/大小写折叠比对），
     否则会被判为"本地脏"并在每轮 push 覆盖对端。路径身份/派生名要么原样保存服务端值，
     要么只在服务端从未见过的路径上生成（首次接触 `base=None`，服务端权威）。
   - folders 服务端按路径并集合并（不删除），客户端 pull 时已按本机分隔符对齐——写库时不要
     改写服务端路径拼写，否则同一目录会插成两条。
   - 项目池是工作空间级共享（`/api/projects/pull` 返回全部可见项目，与 `agent` 无关）；
     服务端的 slug 同名合并按 `(workspace, profile, slug)`，所以派生 slug 必须逐路径唯一。
   - 本地项目库若不以服务端 id 为键（如 WorkBuddy 只有路径表），需要自建身份侧车把服务端
     id 记住（参考 `mcp/adapters/workbuddy.py` 的 `.workbuddy-sync-projects.json`）。
   - **根形状路径不是项目**：`mcp/adapters/base.py::is_root_project_path`（盘符根/filesystem
     根、home 及其祖先）在 push/pull 边界由客户端统一过滤，适配器不必自己实现；但若你的本地
     项目清单是"打开过的目录"这类自动流水，请在 `read_projects` 里同样跳过它们（并清理自己的
     id 侧车），否则会给永不上的路径铸号。理由见 ARCHITECTURE「根目录（home / 盘符根）不入
     共享项目池」：根是万物的祖先，会让卡片变成兜底桶，且服务端 folders 只增不减（单向门）。

## 第 3 步：注册

1. `mcp/adapters/base.py` → `AGENT_PREFIXES` 添加前缀
   （检查 `split_agent_prefix` 的顺序——前缀冲突是唯一会产生数据串扰的风险点）
2. `mcp/adapters/__init__.py` → `_ADAPTER_MODULES` 添加
   `"<name>": "<module>"`

## 第 4 步：测试

1. 构造 fixture：按该 agent 真实格式造 2~3 个样例会话文件（不要依赖本机安装）
2. 往返单测（参考 `mcp/tests/` 现有用例）：
   - 读：fixture → `read_sessions()` → 断言 canonical 字段与 id 形状/归属
   - 写：`write_sessions()` 注入 → 再 `read_sessions()` → 断言往返一致
   - 幂等：同一批 sessions 写两次 → 第二次 `duplicates > 0` 且 `new_messages == 0`
   - 前缀：写入 `codex:`（历史前缀，对应遗留数据）会话到本 adapter → 抛 `ValueError`（或按适配器语义拒绝）
3. 交叉同步（可选但推荐）：A adapter 推 → 服务端 → B adapter 拉取落地

## 第 5 步：部署

1. 按 `scripts/deploy-local-mcp.sh` 部署一份独立实例：
   `export HERMES_SYNC_AGENT=<name>` + 该 agent 的 API Key / 服务器地址
2. 服务端帮助页与 `/web/download/mcp-client` 由 agent 注册表驱动，自动生成该 agent
   的下载包（README 内含 `<YOUR_API_KEY>` 占位符，需替换为帮助页对应工作区的 Key）——无需改服务端代码
3. 在 README 的 agent 支持表格补一行

## 端到端验证清单

- [ ] `python -m py_compile mcp/adapters/<name>.py`
- [ ] `python -m mcp.tests.test_<name>` 全绿
- [ ] 对真实本地库跑 `python -m mcp.adapters.<name>` 自检（有该 agent 时）
- [ ] 双 agent 互推互拉验证交叉同步
- [ ] Hermes 存量链路回归（`python -m mcp.tests`）
