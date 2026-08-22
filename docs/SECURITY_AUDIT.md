# Agent Context Sync — 安全审计报告

- 审计日期：2026-08-18
- 审计范围：`server/`（Web 服务端）、`mcp/`（客户端 MCP 包 + 自动更新）、`scripts/`（部署与导入脚本）、模板与静态资源
- 审计方法：逐文件代码审查 + 关键漏洞点实测复现（XSS 向量已用本地 Python 验证）
- 结论：**存在 4 项高危、6 项中危、6 项低危问题**。核心风险集中在「客户端自动更新链路」与「会话消息渲染（XSS）」。

> 本文档仅记录问题与修复方案，**尚未实施任何修复**，等你决策后再优化。

---

## 一、严重度总览

| 编号 | 严重度 | 问题 | 位置 |
|------|--------|------|------|
| H1 | 高 | 客户端自动更新链路：明文 HTTP + manifest 无签名 + 路径穿越 → 任意文件写入 / RCE | `mcp/updater.py`、`mcp/server.py`、`server/server.py` |
| H2 | 高 | 存储型 XSS（Markdown `javascript:` 链接，已实证） | `server/server.py:1142` `md_to_html` |
| H3 | 高 | 全链路默认明文传输（无 TLS）+ cookie 无 `Secure` 标记 + API key 明文 | `server/server.py:663`、`scripts/deploy-server.sh`、`mcp/server.py` |
| H4 | 高 | 无 CSRF 防护，且存在 GET 型破坏性端点 | `server/server.py:1549`、`:1650` |
| M1 | 中 | 停用用户不吊销已签发的 JWT（`is_active` 仅登录时检查） | `server/server.py:544` `get_current_user` |
| M2 | 中 | 开放注册 + 登录/注册无速率限制 + 消息量无配额 → 暴力破解与存储滥用 | `server/server.py:673`、`:2216` |
| M3 | 中 | 上传/请求体/解压无大小上限（gzip 炸弹、`/push`、`/pull` limit） | `server/server.py:1472`、`:2178`、`:2216` |
| M4 | 中 | 开放重定向（信任 `Referer` 头） | `server/server.py:894` `web_set_language` |
| M5 | 中 | 密码策略偏弱（最短 6 位）、PBKDF2 迭代 100k 低于当前推荐 | `server/server.py` `hash_password` |
| M6 | 中 | 无安全响应头（CSP / X-Frame-Options / HSTS）→ 点击劫持等 | 全站（中间件） |
| L1 | 低 | WorkBuddy 适配器对远程 `cwd` 直接 `mkdir`（任意目录创建原语） | `mcp/adapters/workbuddy.py:316` |
| L2 | 低 | MASTER_API_KEY 比较非恒时 | `server/server.py:558` |
| L3 | 低 | JWT 无撤销机制；改密不使旧 token 失效；`JWT_SECRET` 未配置时每进程随机 | `server/server.py:33`、`create_jwt` |
| L4 | 低 | `/health` 失败时回显数据库异常细节 | `server/server.py:2168` |
| L5 | 低 | systemd 服务以 root 运行；初始管理员密码/API key 打印到日志 | `scripts/deploy-server.sh`、`server/server.py:367` |
| L6 | 低 | 用户名无字符/长度限制（注册枚举、显示污染） | `server/server.py:676` |

---

## 二、高危漏洞详情

### H1. 客户端自动更新链路：任意文件写入 / RCE（客户端机器）

**位置**：`mcp/updater.py:67-138`（`verify_archive` / `apply_update`）、`mcp/server.py:504`（`background_update_check`）、`server/server.py:2013`（`/api/client/manifest`）

**问题链**（三个环节叠加）：

1. **传输层无 TLS 强制**：客户端默认 `SYNC_SERVER = http://localhost:8765`（`mcp/server.py:35`），下载的客户端压缩包内嵌的默认地址也是 `http://`（`server/server.py:_SYNC_SERVER_RE` 重写处）。部署脚本给出的对外地址同样是 `http://IP:8765`。局域网/公网链路上的中间人可篡改 manifest 与 zip 两个响应。
2. **manifest 无签名、SHA256 只防损坏不防篡改**：`SECURITY.md` 声称"客户端自动更新对每个文件做 SHA256 校验（防篡改/截断）"，但 SHA256 来自**同一个不受信任的响应**。中间人同时伪造 manifest（含恶意哈希）和 zip 即可通过校验。校验只拦截"截断/损坏"，不拦"恶意替换"。
3. **`apply_update` 路径穿越**：`verify_archive` 对 manifest 的 `path` 字段**无任何校验**，`apply_update` 直接 `dst = mcp_dir / rel`（`updater.py:123`）。若 manifest 返回 `"path": "../../../evil"`，文件被写到 `mcp_dir` 之外任意用户可写目录。配合第 1、2 点，中间人 = 在客户端机器上写任意内容文件（如 `.bashrc`、启动目录文件、被 import 的 .py）。

**影响**：客户端机器任意文件写入 → 结合启动项/配置文件即可实现 RCE。所有运行 MCP 客户端的开发机均为攻击面。即使仅考虑"合法服务器"，一个被攻破/误配的服务器也能在所有客户端上写任意文件。

**证据**：`verify_archive`（`updater.py:76-88`）只做哈希比对，不校验 `rel` 是否含 `..`/绝对路径；`apply_update`（`updater.py:119-127`）对 `rel` 无白名单。zip 成员名 `f"mcp/{rel}"` 与写出路径 `mcp_dir / rel` 存在语义差。

**修复方案**（按优先级）：
1. `apply_update` 中对每个 `rel` 做严格校验：拒绝含 `..`、以 `/` 或 `\` 开头、含盘符/冒号、不以 `server.py`/`updater.py`/`run.sh`/`run.bat`/`adapters/` 开头的路径；只允许扁平白名单。
2. manifest 增加**服务端私钥签名**（如 Ed25519），客户端内置公钥验证 manifest 后再信任其中哈希；或退而求其次：客户端只接受 `https://` 服务器执行自动更新（`SYNC_SERVER` 非 https 时禁用更新并告警）。
3. `verify_archive` 增加解压大小上限（如单文件 ≤ 5MB、总量 ≤ 50MB），防 zip 炸弹。
4. 默认配置与部署文档强制 HTTPS（见 H3）。

### H2. 存储型 XSS：Markdown `javascript:` 链接（已实证）

**位置**：`server/server.py:1142-1155` `md_to_html`；渲染点 `server/templates/session_messages.html:129,167`、`trash_messages.html:38`（`{{ m.content_md|safe }}`）

**问题**：`md_to_html` 先 `html.escape` 再 `markdown.markdown`，能挡住 `<script>` 等标签注入，但 **URL scheme 未过滤**。实测：

```python
>>> markdown.markdown(html.escape('[click](javascript:alert(1))', quote=True))
'<p><a href="javascript:alert(1)">click</a></p>'
```

`javascript:` 链接完整保留，点击即执行。`data:` 等其他 scheme 同样不过滤（内容中的 `<` `>` 会被转义，`javascript:` 场景已足够）。内容来源：任何持有工作空间 API key 的客户端推送的会话消息（含 LLM 输出）、工作空间导入文件。查看该会话页面的用户（含管理员）点击恶意链接即触发。

**影响**：以受害者会话执行任意 JS → 读取/导出全部会话、改密、删除工作空间；若受害者为管理员，可进一步操作所有用户与工作空间。会话数据是私有 AI 对话，泄露后果严重。

**修复方案**：
- 方案 A（推荐）：渲染后对 `<a href>` 做 scheme 白名单——仅允许 `http:`/`https:`（及 `mailto:`），其余 scheme 的链接改为纯文本或 `href="#"` + 提示。
- 方案 B：引入 `bleach` 对 Markdown 输出做 HTML 净化（默认即会剔除 `javascript:` 等危险 scheme）。
- 无论哪种，保留现有"先转义后渲染"的顺序，并补一个回归测试（`[x](javascript:...)` 不出现在输出 href 中）。

### H3. 全链路默认明文传输 + cookie 无 `Secure` 标记

**位置**：`server/server.py:663`（登录 cookie）、`:915`（语言 cookie）、`scripts/deploy-server.sh:44-60`（systemd 无 TLS）、`mcp/server.py:35`（客户端默认 http）

**问题**：
- 服务端直接暴露 `0.0.0.0:8765`，部署脚本输出 `http://IP:8765`；`SECURITY.md` 仅提示"请置于反向代理之后启用 HTTPS"，默认部署即明文。
- `hsync_token` cookie：`httponly=True, samesite="lax"`，但**无 `secure=True`**——即使部署在 HTTPS 反向代理后，浏览器仍会把会话 cookie 发往明文 HTTP 请求；直接明文部署时，网络窃听者可整包抓取会话 cookie、Authorization 头中的 API key、以及全部会话/消息明文内容。

**影响**：局域网/公共网络窃听 → 会话劫持、API key 泄露、私有对话内容泄露。API key 泄露后可直接读写该工作空间全部数据（`/pull`、`/push` 只认 key）。

**修复方案**：
1. cookie 全部加 `secure=True`（`hsync_token`、`lang`、`_flash`）；`secure` 与部署是否 TLS 绑定——若坚持支持明文部署，则提供显式开关并默认开启。
2. 提供官方 TLS 终止配置（nginx/caddy 反代示例），部署脚本输出 https 地址；`HERMES_SYNC_PUBLIC_URL` 强制 `https://`。
3. 客户端侧：`SYNC_SERVER` 为 `http://` 且非 localhost 时，启动日志告警。

### H4. 无 CSRF 防护 + GET 型破坏性端点

**位置**：`server/server.py:1549` `web_delete_workspace`（GET 删工作空间）、`:1650` `web_toggle_user`（GET 停用/启用用户）、`:942` `web_logout`（GET）；其余全部 POST 变更端点（改密、导入、regen-key、邀请等）均无 CSRF token。

**问题**：全站无 CSRF token、无 Origin/Referer 校验。现代浏览器 `SameSite=Lax` 会拦截跨站 POST 携带 cookie，但 **Lax 允许顶层 GET 导航携带 cookie**——因此：

- 攻击者构造 `<a href="https://server/web/workspace/5/delete">` 或 `window.open(...)`，受害者点击即静默删除工作空间（含全部会话）。
- 管理员被诱导点击 `/web/admin/user/{uid}/toggle` 链接 → 目标用户被停用（含主 admin 自身被停用 → 全站管理员锁死）。
- `/web/logout` 为 GET，可被任意链接强制登出（骚扰级）。

**影响**：会话数据丢失（软删除不可逆？工作空间删除是硬删 `DELETE FROM workspaces`，级联删 sessions/messages——**不可恢复**）、管理员可用性被破坏。

**修复方案**：
1. **所有变更端点改 POST**（`web_delete_workspace`、`web_toggle_user`、`web_logout` 改 POST；删除/停用再加确认页）。
2. 引入 CSRF token（`secrets.token_hex` 存 cookie + 表单/`X-CSRF-Token` 头比对）；或对 POST 端点校验 `Origin`/`Referer` 与站点一致（快速兜底）。
3. 顶层导航无法携带自定义头，因此"改 POST + 校验 Origin"即可闭环。

---

## 三、中危漏洞详情

### M1. 停用用户不吊销已签发 JWT

**位置**：`server/server.py:544-551` `get_current_user`；`is_active` 仅在登录查询（`:644`）使用。

**问题**：`get_current_user` 只验签 + 查过期时间，**不查数据库 `is_active`**。管理员在 Web UI/API 停用某用户后，其已持有的 cookie/JWT 在过期前（默认 24h，可配置更长）仍可访问 `/web/*` 与 `/api/*` 全部功能。`enforce_password_change` 中间件查的是 `must_change_password`，与停用无关。

**影响**：账户吊销机制失效——离职/违规用户停用后仍能读写数据。若 `TOKEN_EXPIRE` 配得很大，吊销窗口很长。

**修复方案**：
1. 最小改法：`get_current_user` 中校验 JWT 后查一次 `SELECT is_active FROM users WHERE id = %s`，为 False 即拒绝（302 到登录页/401）。
2. 更好：JWT 加入 `jti` + 服务端 `token_version`（users 表加列），改密/停用/重置时递增版本号，验签时比对——实现真正的"立即吊销"与"改密失效旧 token"（同时解决 L3）。

### M2. 开放注册 + 无速率限制 + 消息量无配额

**位置**：`server/server.py:673` `web_register_submit`（邀请码可选，无码默认 `unlimited` 套餐）；`:637` `web_login_post`；`:2216` `/push`（消息无配额，仅新会话数量受 `quota_config` 限制）。

**问题**：
- 注册完全开放且默认 `unlimited` 套餐：任何人可无限注册账号、获得工作空间与 API key。
- 登录/注册端点无任何速率限制 → 在线暴力破解（PBKDF2 100k 迭代可缓解速度，但无锁定/退避）、批量注册。
- 配额 gate 只数"新会话数"（free 200 个），**消息行数无上限**：一个 key 持有者可推送海量消息 → 数据库无限膨胀（存储 DoS）。

**影响**：公网部署时账号滥用、暴力破解、存储耗尽。

**修复方案**：
1. 注册默认要求邀请码（`invite_code` 必填），或提供 `HERMES_SYNC_OPEN_REGISTRATION=0` 开关。
2. 登录/注册加速率限制（内存令牌桶或简单按 IP+用户名计数；可选用 `slowapi`/`limits`）。
3. 配额扩展：`quota_config` 增加 `max_messages`，`/push` 对消息新增量同样 gate。

### M3. 上传 / 请求体 / 解压无大小上限

**位置**：`server/server.py:1472`（`raw = await file.read()` 无大小限制）、`:1476`（`gzip.decompress(raw)` 无解压上限）、`:2178` `/pull`（`limit`/`offset` 不 clamp，`LIMIT` 可为任意大）、`:2216` `/push`（`request.json()` 无 body 上限）。

**问题**：认证用户（或泄露的 API key）可：
- 上传数 GB 导出文件或**gzip 炸弹**（极小压缩包解压出数 GB）→ 内存耗尽；
- `/pull` 带 `limit: 10^9` 拉全库 → 内存耗尽；
- `/push` 发超大 body → 内存耗尽。

**影响**：服务可用性 DoS（单请求即可 OOM，无并发要求）。

**修复方案**：
1. `file.read()` 前检查 `Content-Length` / 流式读取并限流（如 100MB）；`gzip.decompress` 用 `zlib.decompressobj` + 输出字节上限。
2. `/pull` 的 `limit` clamp 到合理上限（如 200）并强制 `int`；`offset` 同样 clamp。
3. 中间件级 body 大小限制（如 64MB），或 Nginx `client_max_body_size`。

### M4. 开放重定向（信任 Referer）

**位置**：`server/server.py:894-900` `web_set_language`：`referer = request.headers.get("referer", "/web/")` → `RedirectResponse(url=referer)`。

**问题**：`Referer` 完全由请求方控制。攻击者诱导受害者访问 `https://server/web/set-language/zh-CN`（例如发一个链接），若浏览器携带了攻击站点的 Referer，服务端 303 跳转到攻击站点 → 钓鱼/信誉滥用。该端点同时是 GET，链接式触发。

**修复方案**：只允许站内路径——校验 `referer` 以 `request.base_url` 或站点路径开头，否则回退 `/web/`；或改用服务端记住的上一页（如 session）。

### M5. 密码策略与 KDF 参数偏弱

**位置**：`server/server.py:680`（`len(password) < 6`）、`hash_password`（PBKDF2-SHA256 100,000 迭代）。

**问题**：最短 6 位无复杂度要求；PBKDF2-SHA256 100k 迭代低于 OWASP 当前推荐（600k+，且更推荐 Argon2id/bcrypt）。GPU 集群对弱密码的离线破解速度可观；初始 admin 密码是 12 位 `token_urlsafe`（此点没问题）。

**修复方案**：最短 10 位 + 复杂度建议；迭代数提升到 ≥ 600k（存量哈希自动随下次改密升级）；或迁移到 `argon2-cffi`（引入新依赖，需你决策）。

### M6. 无安全响应头

**位置**：全站（`server/server.py` 无任何安全头中间件）。

**问题**：无 `Content-Security-Policy`、`X-Frame-Options: DENY/SAMEORIGIN`、`X-Content-Type-Options: nosniff`、`Referrer-Policy`、HSTS。管理页面可被第三方 iframe 嵌入 → 点击劫持；M2 修复前 XSS 影响被 CSP 放大。

**修复方案**：加一个中间件统一输出：
`X-Frame-Options: SAMEORIGIN`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: same-origin`、`Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'`（模板内联 JS 较多，需先盘点再收紧）。HSTS 在 TLS 落地后启用。

---

## 四、低危 / 加固项

| 编号 | 位置 | 问题 | 修复建议 |
|------|------|------|----------|
| L1 | `mcp/adapters/workbuddy.py:316` | 拉取时对**远程会话的 `cwd` 字段**直接 `Path(cwd).mkdir(parents=True)`——同工作空间恶意 peer 可推送带任意路径的会话，在受害机任意位置创建目录链（仅目录创建，文件写入路径仍经 `slugify`，无内容控制） | 与 id 一样校验 `cwd`：只允许绝对路径且限定在用户主目录/已知项目根下；非法则回退 `~/hermes-sync-foreign` |
| L2 | `server/server.py:558` | `key == MASTER_API_KEY` 非恒时比较 | 改 `hmac.compare_digest(key, MASTER_API_KEY)`（API key 查询本身已是恒时无关紧要，但 master key 值得） |
| L3 | `server/server.py:33,506-540` | `JWT_SECRET` 未配置时每进程 `secrets.token_hex(32)`：重启/多 worker 全部登出（可用性）；无 `jti`/`token_version`，改密不使旧 token 失效；密码重置类操作无吊销能力 | 强制要求配置 `HERMES_SYNC_JWT_SECRET`（缺失即启动失败，与 PG DSN/master key 同策略）；JWT 加 `token_version`（见 M1） |
| L4 | `server/server.py:2174` | `/health` 异常时 `{"detail": str(e)}` 回显 psycopg2 异常，可能含主机/库名等连接细节 | 只返回 `{"status":"error"}`，细节写日志 |
| L5 | `scripts/deploy-server.sh:44-60`、`server/server.py:367-370` | systemd 无 `User=`（root 运行）；初始 admin 密码与工作空间 key `print` 到 stdout（进 systemd journal） | `User=agentctxsync` + 专用低权用户；初始凭证写一次性文件（`chmod 600`）并提示删除，或强制首次登录改密后轮换 |
| L6 | `server/server.py:676` | 用户名无字符/长度限制 | 限制长度（如 ≤ 64）、字符集（如 `[A-Za-z0-9_.-]`）；登录失败统一提示避免枚举 |

---

## 五、已验证的安全设计（正面清单）

以下方面经代码审查确认**当前实现是安全的**，修复时勿破坏：

1. **SQL 注入**：全部查询使用参数化绑定；`web_workspace_detail` 的排序列/方向、agent 过滤为白名单；profile 过滤经 `re.fullmatch(r"[A-Za-z0-9_.-]+")` 校验后才拼入 LIKE 字面量。动态列名来自 `information_schema` 白名单。客户端 SQLite 适配器同（列名经 `PRAGMA` 枚举）。
2. **模板 XSS 基线**：Jinja2 全局 `autoescape`；`md_to_html` 先转义后渲染挡住了 HTML 标签注入（H2 是 scheme 层面的漏网，修复后此防线完整）。
3. **客户端路径穿越**：`validate_local_id` 在 deepseek-harness/reasonix/opencode/openclaw/workbuddy 的 `write_sessions` 中均被调用（拒绝含 `/`、`\`、`.`、`..` 的 id）；hermes 走 SQLite 参数化。SECURITY.md 此条声明属实。
4. **权限边界**：工作空间读写均以属主/API key 为界（`WHERE workspace_id = %s AND user_id = %s`）；管理员只对工作空间元数据有写权，**读不到**其他用户的会话/消息内容；admin 页面不暴露他人 api_key。
5. **凭证卫生**：`MASTER_API_KEY` 与 workspace key 分离；`.gitignore` 排除 `.env`、`.workbuddy/`（含 cookies.txt）、`.reasonix/`；仓库内无硬编码密钥。
6. **密码存储**：PBKDF2-SHA256 + 随机盐 + `hmac.compare_digest` 比对（参数强度见 M5）。
7. **邀请码**：单次使用经原子 UPDATE + rowcount 校验，并发注册安全；过期/撤销校验完整。
8. **配额 gate**：DB 读策略、master key 豁免、拒绝写 audit_log（事务内提交），free 默认 200 会话。
9. **消息去重**：服务端与客户端同用 `(session_id, role, timestamp)` 三元组 + 内容兜底，防止重复膨胀。
10. **下载包**：`_build_client_zip` 的 arcname 全部来自固定清单，无 zip-slip。

---

## 六、非安全问题（顺带发现，供参考）

- `server/server.py:96-101` `_current_request` 为模块级全局：并发请求会互相覆盖，`get_lang()`/`render()` 可能读到另一个请求的上下文（表现为语言/闪存串台，非安全）。
- `server/server.py:2069-2072` `api_create_workspace`：INSERT 无 `RETURNING`，返回的 `id` 恒为 `None`（功能 bug）。
- `/pull` 的 `limit`/`offset` 未强制 `int`（字符串会被 PG 隐式转换），配合 M3 一并修。
- 反代部署时 `request.base_url` 为内网地址，未设 `HERMES_SYNC_PUBLIC_URL` 会让客户端拿到内网地址（运维项，非安全）。

---

## 七、修复优先级建议

| 批次 | 内容 | 理由 |
|------|------|------|
| P0（立即） | H2 XSS、H4 CSRF/GET 端点、H1 updater 路径校验 | 可被低成本利用、影响数据完整性与客户端机器 |
| P1（尽快） | H3 TLS/Secure cookie、M1 停用吊销、M2 速率限制与注册开关、M3 大小限制 | 公网部署的合规与可用性底线 |
| P2（计划） | M4 重定向、M5 密码/KDF、M6 安全头 | 加固项 |
| P3（随迭代） | L1–L6 | 顺手修复 |

各修复方案已在上文对应条目给出；实施时建议按批处理并配套回归测试（H2 需 XSS 回归用例，H4 需端点方法变更用例）。
