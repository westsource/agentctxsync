## [2026.09.21.2] - 2026-09-21

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.21.1），无 schema 变更。
> 已部署 236（2026-09-21 CST，提交 `bb04de2`）：1 个文件（`templates/register.html`），
> 对上次部署提交 `dc4de72` 预检通过、上传后回读一致、Jinja 模板编译通过，重启后 active、
> journal 无 error。线上复测（公网 `/web/register`）：两个密码输入各占整行、同 left、各 400px 宽
> （`passwordRows=2`），用户名/显示名仍并排（`userRow=1`），`grid grid-cols-2` 只剩用户名那一处。

### Fixed

- **注册页的密码字段改为纵向排列**：`/web/register` 的「密码 / 确认密码」原本在
  `grid grid-cols-2` 里左右并排（窄卡片里每个仅约 194px，密码管理器与移动端都别扭），
  现改为各占整行、纵向排列（与登录/重置/安全中心一致）。用户名 / 显示名仍保持并排（非密码字段）。
  核对：全站密码输入框分布 —— `/web/register`（2，本次修正）、`/web/login`（1）、`/web/reset`（2）、
  `/web/change-password?forced=1`（3，首次登录强制改密，纵向）、`/web/security` 修改密码弹窗
  （3，纵向）、更换邮箱弹窗与 `/web/email`（各 1）、管理员新建/编辑用户（各 1）；
  `grid`/`flex` 并排的密码字段仅注册页这一处，修正后全站密码字段均为纵向。
  验证：本地渲染 + 浏览器实测（`password`/`confirm_password` 两行、同 left、各 400px 宽），
  线上部署后复测同一指标。

## [2026.09.21.1] - 2026-09-21

> 服务端 + 客户端发布：`sessions` 新增 `sync_paused` / `sync_paused_at` 两列（`init_db()`
> 幂等 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，重启即生效）；`CLIENT_VERSION`
> 2026.09.13.4 → **2026.09.21.1**（`mcp/updater.py` 与 `server/client_update.py` 同步），
> 因为客户端 `push_sessions` 必须认识 `/push` 的新响应字段 `paused_ids`（否则恢复后
> 会把暂停期间的内容永远留在本地）。
> 已部署 236（2026-09-21 CST，提交 `9531474`）：11 个文件（6 个 .py + 4 个模板 + `mcp/`
> 客户端 2 个），对上次部署提交 `cab5a8a` 逐文件（LF 归一化）sha256 预检通过、上传后回读一致、
> `py_compile` 通过，重启后 active、journal 无 error；`check_drift_236.py` /
> `check_drift_mcp_236.py` 复核：server/ 50 个文件、mcp/ 15 个文件 drift=0，两端
> `CLIENT_VERSION` 均为 2026.09.21.1。
> 线上实测（一次性用户 + 工作空间，用完即删，残留 0 行；20 项检查全 PASS）：`sessions` 两列由
> `init_db()` 建出；匿名 `POST /web/sync/pause` → 307 `/web/login`（路由已注册）；Web 暂停后
> `/push` 返回 `paused_ids=['sess-alpha']` 且该会话标题/消息数原封不动、`/pull` 仍下发冻结版本
> （`sync_paused=1`）；列表页渲染「已暂停同步」徽标与批量栏；恢复后同一次推送补齐暂停期间的
> 2 条消息；`/api/client/manifest?agent=hermes&v=2026.09.13.4` 返回 version 2026.09.21.1 +
> `update_available=true`，下载包内 `manifest.json` 版本一致、`mcp/server.py` 的 sha256 与清单
> 相符且含 `paused_ids`。

### Added

- **按会话暂停 / 恢复同步（Web 端，可多选）**：会话列表（工作空间详情页、全部会话页）每行有
  勾选框与单行按钮，查看器页也有按钮，批量栏可一次暂停/恢复所选会话。语义是**服务端冻结**：
  `sessions.sync_paused=1` 的会话在 `/push` 与 Web 导入中**一律不写**（会话元数据、消息、
  `rev`/`field_rev`、`last_synced_at` 全部不动），闸门放在 `push_sync` 最前面，所以配额闸门、
  去重快照与 rev 记账都看不到它——整会话冻结，不是部分合并。`/pull` 照旧下发（冻结版本），
  暂停只断上行；否则其它设备会把它当"服务端已删"而触发补回逻辑。
- **`/push` 新增响应字段 `paused_ids`**：本次被冻结跳过的会话 id。客户端据此**不记录 push
  指纹**（指纹含义是"服务端已持有这份内容"）——记了它，暂停期间的内容在恢复后将永远补不上去
  （本地无变化 → 指纹命中 → 跳过）。不记指纹 = 每轮重发，恢复后下一轮即补齐；客户端日志与
  返回体给出 `paused` / `paused_ids` 计数。**混合版本窗口**：旧客户端不认该字段，服务端会停在
  暂停前的快照，直到该会话本地内容再次变化或 `sync_full`。
- 路由 `POST /web/sync/pause` / `POST /web/sync/resume`：表单字段 `sel` 可重复，值为
  `"<ws_id>:<session_id>"`（会话 id 自身可含冒号，只按**第一个**冒号切分）；只更新调用者自己的
  workspace（`workspaces.user_id` 校验），返回路径 `next` 限同源。行内已有各自的删除表单，
  所以每行勾选框用 `form="sync-bulk"` 关联到行外表单（HTML 不允许嵌套 form），两个提交按钮用
  `formaction` 区分暂停/恢复。
- Web 导入（`/web/workspace/{id}/import`）跳过暂停会话并在 flash 中报数（与 `/push` 同规则，
  不静默丢弃）；新增词条 `sync_pause_*` / `sync_bulk_*` / `ws_import_paused`（zh-CN + en）。
- 测试：`server/tests/test_sync.py`（`PushTest` 新增 "sync pause" 用例：被暂停会话零写入、
  同批次的未暂停会话照常落库、响应带 `paused_ids`、暂停的会话不再进入配额闸门）、
  `server/tests/test_sync_pause_ui.py`（`sel` 解析含冒号 id、端点、多选、属主校验、空选择提示、
  `next` 同源校验、导入跳过并计数、zh/en 词条齐备）、`mcp/tests/test_push_pause.py`
  （客户端不为暂停会话写指纹、下一轮重发、混合版本窗口、多 chunk 合并计数）。
- 文档：`docs/ARCHITECTURE.md` 新增「暂停/恢复同步」决策记录（含混合版本窗口与回归防线）、
  sessions 表新增两列、路由表、pull 过滤段说明；`docs/server-deployment.md` 的路由表、`/push`
  响应字段与 sessions 列表同步；`docs/OPERATIONS.md` 在数据保留章节补条目；
  README（中英）功能要点同步。

## [2026.09.19.2] - 2026-09-19

> 服务端专用发布：无客户端改动、无 schema 变更。
> 已部署 236（2026-09-19 CST，提交 `8e7bd3c`）：3 个文件（`auth.py` / `translations.py` /
> `templates/login.html`），对上次部署提交 `6bdcb16` 逐文件 sha256 预检通过、上传后回读一致、
> `py_compile` 通过，重启后 active、journal 无 error。
> 线上读回：中文登录页显示「用户名 / 邮箱」+ 提示「也可以用已验证的安全邮箱登录（未验证的邮箱不行）」，
> `Cookie: lang=en` 下为「Username or email」+ 英文提示；用**用户名**与用**邮箱**提交的错误页
> sha256 完全相同（`5249e761…`），即失败响应不泄露标识符形态。

### Added

- **登录页明确支持"已验证邮箱"**：`_find_login_user`（用户名优先，其次按 `email_normalized` +
  `email_verified_at IS NOT NULL`，大小写不敏感）此前只存在于代码里，UI 却只写"用户名"。
  现在标签在邮件功能开启时显示「用户名 / 邮箱」并加一行提示，关闭时仍只显示「用户名」
  （避免承诺不可用的能力）；新增 `render_login_page()` 统一渲染，三处登录页渲染点共用。
- 词条 `login_identifier` / `login_identifier_hint`（zh + en）；README（中英）与
  `docs/server-deployment.md`（路由表 + users 表说明）同步写明邮箱可登录及其前提。
- `server/tests/test_login.py`（8 例）：用户名优先且不触发邮箱查询、已验证邮箱命中且 SQL 带
  `email_verified_at IS NOT NULL`、大小写/空格归一化、SMTP 关闭时邮箱分支失效、无 `@` 不查邮箱、
  路由级"邮箱登录签发 cookie"、失败响应两种标识符同页、标签随邮件开关切换。

## [2026.09.19.1] - 2026-09-19

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变）；新增一张表
> `mail_stats`（`init_db()` 幂等创建，重启即生效）。
> 已部署 236（2026-09-19 CST，提交 `6bdcb16`）：11 个文件（8 个 .py + 3 个模板），部署前对上次部署
> 提交 `b03e695` 做 LF 归一化 sha256 校验通过，上传后逐文件回读一致，`py_compile` 通过，重启后
> active、journal 无 error；`init_db()` 建出 `mail_stats`（`to_regclass` 读回）。
> 线上校验：`/web/forgot` 与 `/web/register` 均渲染验证码组件且**标记里没有 `<text>`**（只有
> `<path>`、线条与点）；错答 → 200 + 「验证码错误，请重试」+ 表单保留已填标识符 + 新挑战；
> 用视觉读出真实挑战（`10 - 1`）作答、标识符填不存在的用户 → 统一「请求已收到」页（
> `mail_stats` 无记录，未发信）；`REQ src=<SERVER_IP> … xff=203.0.113.9, <SERVER_IP>`
> —— 伪造的 XFF 只落在 `xff=` 字段，`src` 是真实对端。
> 预算实测（部署后的 `mailer`，SMTP 指向关闭端口、`CAP=2`）：第 1/2 次 `mail_send_failed`
> （`failed=2`），第 3 次 `mail_budget_exceeded`（`rejected=1`），再调 `_budget_allow()` 为 False
> （`rejected=2`），计数从线上库读回为 `sent=3 / failed=2 / rejected=2`；探针行已当日清除。
> 未做线上实测的一项：按收件人冷却对**真实邮箱**的封顶（会真的发出邮件），由单元测试覆盖
> （`test_ratelimit.py::MailRecipientLimitTest`、`test_register.py::ForgotResetTest` 的
> 收件人桶与统一响应用例）。
> 注：2026-09-19 对**全部历史**做过一次重写，以清除部署细节（公网/内网 IP、SMTP 服务商与账号、
> 客户端设备名）并把提交作者/提交者邮箱统一为 GitHub `noreply`（`westsource@users.noreply.github.com`）；
> 因此本文件所有历史提交哈希都已变化，引用已同步为新值（tag 也已重指）。完整「旧哈希 → 新哈希」映射表
> 保存在本地运维手册（不入库）。

### Added

- **发信收件人侧限流**（`ratelimit.py` 新 scope，key 是收件地址/账号而非来源 IP —— 换 IP 不能放大）：
  `mail_recipient` 1 封/分钟 + 5 封/天（已解析的已验证邮箱，`/web/forgot` 与安全中心重置）、
  `mail_account` 10 封/小时（`bind` / `resend` / 安全中心，按 `user_id`）、
  `mail_address` 1 封/10 分钟（调用方自填地址：注册激活、绑定/重发目标）。
  此前唯一上限是每 IP 5 封/10 分钟 = 单邮箱 720 封/天/IP，且分布式来源线性放大。
- **全站每日发信预算**：`HERMES_SYNC_MAIL_DAILY_CAP`（默认 200），计数落 `mail_stats`
  （`sent` / `rejected` / `failed`），DB 计数所以重启不清零；超限拒绝发送并记 `rejected`。
  动机：发信账号是个人邮箱、有服务商日额度，被刷爆后**所有用户**收不到激活/重置邮件，
  而 `forgot` 是静默失败，用户只会觉得"没收到"。`rejected` 增长即可作为滥用告警信号。
- **`/web/forgot` 挂自托管验证码**（复用 `captcha` 模块）：闸门在任何账号查询之前，
  失败只回表单页（带 `forgot_captcha_failed`），不泄露标识符是否存在；表单保留已填标识符。
- `server/tests/test_mailer.py`（预算与计数）、`test_ratelimit.py` 新增多窗口/满表/收件人维度用例、
  `test_accessstats.py` 新增日志来源用例、`test_captcha.py` 改为断言"标记里没有可读文本"。

### Changed

- **验证码改为纯矢量线条渲染**（`captcha._render_svg`）：原先把算式写成 SVG `<text>` 节点，
  一个正则即可读出算式（实测 `"88 + 65 = ?"`，不需要 OCR 或打码平台）；现在只用
  `<path>`（7 段式折线 + 随机旋转/抖动/线宽）与装饰线点，标记里不含任何字符数据，
  答案只存服务端进程内。**这是成本门槛，不是人性证明**：愿意写 OCR 的人仍能过，
  真正的天花板是上面的收件人桶与日预算。
- **发信移出事件循环**：六个发信点全部改走 `asyncio.to_thread`（对齐 `render`/`sync` 的做法）。
  在此之前 `mailer.send_mail`（同步 smtplib，`timeout=20`、每封新建连接）直接在 async 处理器里
  调用，单进程 uvicorn 下每个发信请求都会卡住登录/同步。
- **限流计数表满时改为失败关闭**：原先超过 20000 个桶就整表 `clear()`，等于让刷不同 key 的调用者
  把所有人的预算（含 login 桶）一起清零；现在先淘汰过期条目，仍满则拒绝该请求并打印告警。

### Fixed

- **`REQ src=` 不再取 `X-Forwarded-For` 最左值**（那是调用方可控的）：改用 uvicorn 解析出的真实对端
  IP，原始链另存 `xff=` 字段。限流一直是按真实 IP 记的（伪造 XFF 绕不过，实测第 6 次即被限），
  但日志/取证此前可被伪造。

## [2026.09.18.4] - 2026-09-18

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），无 schema 变更。
> 已部署 236（2026-09-18 CST，提交 `b03e695`）：单文件改动（`main.py`），部署前对上次部署提交
> `109cce4` 做 LF 归一化 sha256 校验通过，上传后回读逐字节一致；`py_compile` 通过，重启后
> active、journal 无 error。线上读回：`ss` 仅见 `127.0.0.1:8765`（无 `0.0.0.0:8765`）、回环
> `/health` 与 `/web/login` 均 200、域名 `https://www.agentctxsync.com/health` 200、机内拨
> `<LAN_IP>:8765` 得 `000`（未绑定，符合预期）、公网直连 8765 仍 TIMEOUT。
> 端到端：重启后真实设备 `local-<device>` 的 `/pull`、`/api/projects/push`、
> `/api/projects/pull` 在 journal 中均 `status=200`、`proto=https`、
> `host=www.agentctxsync.com`。

### Changed

- **服务端只监听 `127.0.0.1:8765`**（原 `0.0.0.0:8765`）：uvicorn 绑定回环，反向代理（nginx 等
  TLS 终止层）成为唯一对外入口。此前公网可达性靠云安全组兜底，现在由进程自身保证——明文端口不
  出现在任何非回环接口上；`request.client.host` 只可能来自代理（`proxy_headers=True` 只信任
  127.0.0.1 的 `X-Forwarded-For`），按 IP 限流与「域名 vs IP 直连」来源审计的前提因此更硬。
- 自托管部署**必须**前置反向代理才能从其他机器访问：README（中英）、`SECURITY.md`、
  `docs/server-deployment.md`（新增「绑定地址与反向代理（必需）」小节）、`docs/OPERATIONS.md`
  的地址示例、`scripts/deploy-server.sh` 的输出、`scripts/deploy-local-mcp.{ps1,sh}` 与
  `scripts/migrate-local-to-server.py` 的默认地址占位同步更新；`server/_run_local.py`
  （本地开发启动器）一并改为回环。

## [2026.09.18.3] - 2026-09-18

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），无 schema 变更。
> 已部署 236（2026-09-18 CST，提交 `109cce4`）：单文件改动（`translations.py`），逐文件对上次部署
> 提交 `ff86c42` 校验通过，重启后 active、`/health` 200、journal 无 error。
> 线上实测侧边栏末三项：`接入帮助 → 官方首页 → GitHub Issues`，其中「官方首页」仍为
> `/?landing=1`、新标签页打开。

### Changed

- **侧边栏文案「官网」→「官方首页」**（纯文案，无逻辑/模板改动）。英文对应 Website → Homepage。
  `test_landing_link.py` 的标签断言同步更新。

## [2026.09.18.2] - 2026-09-18

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），无 schema 变更。
> 已部署 236（2026-09-18 CST，提交 `b72520b`）：文件集由 git 差集算出（3 改 1 增），逐文件对上次
> 部署提交 `47b4ef8` 校验通过，`py_compile` 与应用级 `import main` 通过，重启后 active、
> `/health` 200、journal 无 error。
> 线上实测：侧边栏末三项为「接入帮助 → 官网 ↗ → GitHub Issues ↗」（两个外链同图标成组）；
> 点击「官网」新开标签页落到 `https://www.agentctxsync.com/?landing=1`，渲染的是**静态 SEO 页**
> （无应用侧边栏、有 canonical）。应用侧分支也在线上应用上验证：带 flag 的 `/` 返回
> `200 / 31801 B`（应用自己的落地页，不重定向），不带 flag 仍 `307 → /web/`。

### Added

- **侧边栏「官网」入口**：登录后可直接跳到公开落地页，与「GitHub Issues」并列放在底部——
  两者都是"离开应用"的外链，故同一图标语义、都 `target="_blank"` + `noopener noreferrer`。
- **`?landing=1`**：`auth.root` 显式请求落地页时**跳过**"已登录 → 跳 /web/"的重定向。
  为什么需要它：在**没有 nginx 静态层**的自托管部署上，应用自己服务 `/`，链接点下去会被重定向
  回仪表盘、等于没反应；而线上由 nginx `location = /` 从文件服务时 query 被忽略，同一链接
  指向静态落地页。一个链接在两种部署下都成立，且不引入新配置。

### 测试

- 新增 `server/tests/test_landing_link.py`（7 例 / 4 子例）：`root` 三态（游客→落地页、
  已登录→重定向、已登录+flag→落地页且带 `agent_count`）；`landing=0` / `landing=` / `landing=1x` /
  其它参数**不得**绕过重定向；侧边栏链接必须新开标签且带 `noopener noreferrer`、必须与 GitHub 项相邻、
  词条中英齐备。
- server 全量 240 用例通过（2 skip，36 subtests）。

## [2026.09.18.1] - 2026-09-18

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），有 schema 变更
> （新增一张仅记录"谁读过哪条公告"的表，随重启的 `db.init_db()` 幂等创建）。
> **默认关闭**：未配置 `HERMES_SYNC_ANNOUNCEMENTS_URL` 的部署行为与之前完全一致。
> 已部署 236（2026-09-18 CST，提交 `248ca7b`）：文件集由 git 差集算出（1 增 6 改），逐文件对
> 上次部署提交 `ab3e144` 校验（新增文件按"远端不应存在"判定），`py_compile` 与应用级
> `import main` 通过，重启后 active、`/health` 200、journal 无 error。配置写入 systemd drop-in
> `40-announce.conf`（走 `daemon-reload`，未改 vhost 之外任何服务端配置）。
> 内容侧（`agentctxsync_seo`，提交 `90dd73a`）：`dist/` 以 tar + 原子换发布到
> `/var/www/agentctxsync-seo`；线上 vhost 新增 `location = /announcements.json`（先备份
> `*.bak-announce-<ts>`，`nginx -t` 通过后才 reload）。线上读回：`HTTP/2 200`、
> `content-type: application/json`、`access-control-allow-origin: *`、`cache-control: public, max-age=300`；
> `scripts/check-live.py`（新增 feed 断言）**ALL CHECKS PASSED（18 页 + 4 notes）**。
> 端到端实测（真实会话、真实 feed）：`/web/` 顶部横幅正确渲染标题/正文/外链（跨站自动
> `target=_blank`）与 `update` 配色；点关闭 → `announcement_dismissals` 落 1 行
> （`2026-09-18-oh-my-pi` / user 1）→ 刷新后横幅不再出现（服务端注入已读 id 生效）→ 删除该行后
> 横幅恢复；测试行已清理，表内 0 行。跨域路径也在真实浏览器验证（页面在 `127.0.0.1`、feed 在
> `www.agentctxsync.com`，CORS 生效可读到 body）——这是自托管部署的真实形态，缺 CORS 会静默不显示。

### Added

- **应用内公告横幅（可选，默认关）**：登录后在页面顶部显示来自公开 JSON feed 的公告，可逐条关闭。
  用途是"产品有新定位/新东西要告诉用户"，而 MCP 客户端在后台跑、没有 UI，无法承担这件事。
  内容**不写进产品**：运营者在自己已维护的站点上写一次（见 `agentctxsync_seo/announcements/`），
  应用只负责显示与已读状态 —— 因此不产生第二份文案真源，也不需要为公告新增任何翻译词条。
- **服务器不出网**：feed 由**浏览器**直接拉取（`config.ANNOUNCEMENTS_URL` 只是把 URL 交给页面），
  自托管实例不会向厂商发起任何请求；这也是该功能**默认关闭**、以及指向哪个站点由部署者决定的原因。
- **`announcement_dismissals` 表**：按 (公告 id, 用户) 记录关闭，逐用户生效（一个人关掉不影响别人）。
  公告 id 来自外部 feed，故**刻意不设外键**——内容归运营者，这张表只记"谁读过什么"。
  行数按关闭次数增长（几十字节/条），已退役公告的残留行无害，且能保证同一 id 重新发布时仍是已读。
- **`POST /web/announcement/dismiss`**：幂等写入；id 需匹配 `[a-z0-9-]`（与 feed 构建期同一约束），
  非法 id 返回 400、未登录返回 401，均不落库。
- 横幅实现：`textContent` 注入（feed 是数据不是标记）、feed `version != 1` 整体忽略、
  未知 `severity` 退回 `info`、语言缺失时按 `当前语言 → zh-CN → 任一` 回退、过期项不发请求也过滤。
  所有这些"消费方容错"都由 `agentctxsync_seo/build.py --check` 与 `scripts/check-live.py` 在**构建/线上**两侧断言。

### 测试

- 新增 `server/tests/test_announcements.py`（11 例 / 8 子例）：默认关闭时**不产生任何查询**（免成隐性开销）、
  登录用户取到已读 id、游客不查库、DB 故障不影响页面；横幅在未配置时不渲染、配置后带 URL 与已读 id、
  且**横幅块内不出现 `innerHTML`**（限定作用域，页面其它 JS 不受影响）；dismiss 幂等、
  游客 401、7 类畸形 id 全部 400 且不落库。
- server 全量 233 用例通过（2 skip，32 subtests）。

## [2026.09.17.3] - 2026-09-17

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），无 schema 变更
> （仅移除一个不再使用的建表语句，已存在的库不受影响）。
> 已部署 236（2026-09-17 CST，提交 `375ef22`），文件集由 git 依提交差集算出（4 改 2 删），
> 部署前逐文件校验远端与上次部署提交 `4434678` LF 归一化 sha256 一致（全部 OK），改前/删前文件
> 备份为同目录 `*.bak-2026.09.17.3`（含被删的 `feedback.py` 与 `templates/feedback.html`，可直接拷回
> 回滚）；`py_compile` 与应用级 `import main` 均通过，重启后 `systemctl is-active` 为 active、
> `/health` 200、journal 无 error/traceback。
> 线上核验：`feedback.py` / `templates/feedback.html` 已从 `/opt/agentctxsync` 移除（`__pycache__`
> 中无残留 .pyc，重启后日志无 Feedback 相关行）；部署后的 `templates/base.html` 中
> `github.com/westsource/agentctxsync/issues` 出现 1 次、`nav_github_issues` 存在、旧键
> `nav_feedback` 0 次；`/web/feedback` 返回 404（当前基线无自定义 404 页，故为 FastAPI 默认 JSON
> 体，符合预期）；渲染实测侧边栏末项为「GitHub Issues」、`target="_blank"`、href 指向 `/issues`，
> 图标为「方框 + 右上箭头」外链符号（视觉复核确认，非原铅笔图标）。

### Changed

- **侧边栏「提交反馈」改为「GitHub Issues」外链**：原条目文案「提交反馈 / Submit Feedback」、
  铅笔图标，却跳转到 GitHub **仓库首页**（既不是站内提交，也不是 issue 列表）。现改为直接指向
  `https://github.com/westsource/agentctxsync/issues`，图标换成「外链」符号（方块 + 右上箭头），
  让"点击会离开本站"这件事在点击前就可见；文案改为 `nav_github_issues`，直说目的地。
- **删除站内反馈整套（改用 GitHub 后它成了死代码）**：`server/feedback.py`（3 个端点）、
  `server/templates/feedback.html`、`server/tests/test_feedback.py`（6 例）、`main.py` 的 import
  与路由注册、`db.py` 的 `feedback` 建表、`translations.py` 的 25 个反馈词条（中英各 25）。
  删因：该功能完整（提交/分类/管理员处理）却在**任何模板里都没有入口**（全仓 grep 只剩它自己的
  重定向、模板内表单 action 与测试），而它面向的受众与 GitHub Issues 不同——站内反馈给**本服务器
  运营者**处理（多租户自托管下，第三方部署者的用户反馈不该被送去上游仓库），GitHub Issues 给
  **开源项目**。既然只保留 GitHub，就把它按"只用 GitHub"收干净，不留悬空引用。
- **文档同步**：`docs/ARCHITECTURE.md`（模块表的反馈域行 + 路由清单 3 条）、
  `docs/server-deployment.md`（文件树 2 处 + 路由表 3 条 + i18n 覆盖范围）移除反馈条目；
  顺带把 i18n 键数从已过期的 411 更正为实测 475（改动前实际已是 499，与本次无关的既有漂移）。

### 测试

- server 全量 222 用例通过（2 skip，24 subtests）；随功能删除一并移除 `test_feedback.py` 的 6 例。
- 中英词条实测 475 / 475 完全对齐，`feedback` 相关键 0 残留；全仓 `feedback` / `反馈` 引用 0 处
  （除 `base.html` 里同名的剪贴板 JS 函数与 `CONTRIBUTING.md` 的英文散文）。

## [2026.09.17.2] - 2026-09-17

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），**有 schema 变更**
> （`access_device` 增列并重建主键，随重启的 `db.init_db()` 幂等执行）。
> 已部署 236（2026-09-17 CST，提交 `695bd83`），文件集由 git 依提交差集算出（6 个服务端文件），
> 部署前逐文件校验远端与上次部署提交 `76cbedb` LF 归一化 sha256 一致（全部 OK），改前文件备份为
> 同目录 `*.bak-2026.09.17.2`；`py_compile` 与应用级 `import main` 均通过，重启后
> `systemctl is-active` 为 active、`/health` 200、journal 无 error/traceback。
> 重启后实测 `access_device` 主键已由 `(stat_date, device_id, agent, channel)` 变为
> `(..., user_id)`。
> 线上回填（`scripts/backfill-access-device-user.py`）：dry-run 报 155 行可归属、22 行保持
> 未归属 → `--apply` 实际回填 155 行 → 再跑一次 `nothing to do`（幂等）。回填后今日真实归属为
> 道荣 / 风林流墨 / 土豆 / 张渊 / xowm / 幸运星 等，与 `sync_state → workspaces → users` 映射一致。
> 线上端到端核验：用真实 workspace key 打 `/status/<probe>` 得 `user_id=1`，无效 key 与无 key 均得
> `user_id=0`（3 条探针行已删除，未留下测试设备）；另用部署后模块签发管理员会话 GET
> `/web/admin/access/devices`，HTTP 200（44131 字节），「用户」列与「未归属」标签均渲染，
> 真实归属名可见，`local-<device>` 徽章为 `2 个 Agent`（同 Agent 两账号不重复计数）。
> 双远端同步：gitee 推送 `76cbedb..695bd83`；github 由 236 侧中转推送，`refs/heads/main` 与
> GitHub API 均为 `695bd83`。

### Added

- **设备访问统计记录归属到用户**：`/web/admin/access/devices` 的每条 Agent 记录现在显示所属用户。
  身份只取自同步端使用的 workspace API key——`auth.get_workspace_by_api_key` 解析出 owner 写到
  `request.state.ws_user_id`，`requestlog` 中间件在请求结束后读取并随计数器落库；无效 key、无 key
  与 master key 一律记 `0`（页面显示「未归属」），不猜测。
- **`access_device.user_id`**：`BIGINT NOT NULL DEFAULT 0`，主键重建为
  `(stat_date, device_id, agent, channel, user_id)`。`device_id` 是客户端自报字符串（线上实测存在
  一个 device_id 对应两个用户），user_id 必须进键，否则两个账号的计数会互相覆盖。
- **页面**：明细行新增「用户」列（`display_name` 为空回退 `username`；账号已删除显示 `#id`——
  统计表刻意不设外键，让统计比账号活得久）。摘要徽章改为按**去重后的 Agent 数**计数，同一 Agent
  被两个账号使用仍显示「1 个 Agent」，其版本徽章取该 Agent 最近一次上报。
- **`scripts/backfill-access-device-user.py`**：历史行按 `sync_state → workspaces → users` 的
  **唯一**映射回填；歧义（device 对应多个用户）与无映射（探针、已删工作区）保持未归属，默认
  dry-run、只填 `user_id = 0`，故幂等且不覆盖线上已写入的值。

### 测试

- 新增 `server/tests/test_admin_access_users.py`（8 例）：页面按 (device, agent, user) 聚合、
  未归属标签、账号已删的 `#id` 占位、摘要徽章去重计数、查询按 user_id 分组、活动量排序、中英键齐全。
- `server/tests/test_accessstats.py` 扩展：`user_id` 落库与冲突目标含 user_id、缺省/None 记 0；
  以及**中间件的 state 传播链路**专项——owner 由内层 FastAPI 依赖写在 `request.state`，计数由外层
  `BaseHTTPMiddleware` 写，用真实 `BaseHTTPMiddleware` + 内层 ASGI app 断言二者共享
  `scope["state"]`，并配负向用例（无 state → 0）；缺此测试则该机制若失效只会静默退化成全部「未归属」。
- 迁移在 236 的真实 PostgreSQL 上以**克隆表 + 事务回滚**彩排（线上表未被修改）：旧主键 4 列 →
  新主键 5 列、`user_id` NOT NULL、既有行保留为 0；并回放**从 `requestlog.py` 抽取的原始 upsert**
  验证冲突目标与 DO UPDATE 分支（同键两次合并为 1 行、换用户另起 1 行）。
- 迁移 DDL 连跑两遍验证幂等（`init_db()` 每次重启都会执行）：两遍后主键定义完全一致、无报错。
- server 全量 228 用例通过（2 skip，24 subtests）。

## [2026.09.17.1] - 2026-09-17

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），无 schema 变更。
> 已部署 236（2026-09-17 CST，提交 `a325cde`），部署前校验远端 `auth.py` / `translations.py` /
> `templates/landing.html` 与提交 `dc1632c`（线上当时版本）LF 归一化 sha256 逐字一致，改前文件
> 备份为同目录 `*.bak-agentcount-2026.09.17`；重启后 `systemctl is-active` 为 active、`/health`
> 200、journal 无 error/traceback。
> 线上校验：分别从服务进程（`127.0.0.1:8765/`）与公网（`https://www.agentctxsync.com/`）取渲染页，
> 两者 sha256 完全相同（`103e7dbb…`，31801 字节）——开源区 `<div class="num">7+</div>`、终端
> 「`· 7 个 Agent 共享`」、meta「`让 7 个 AI Agent（… Oh My Pi）`」，且页内既无 `6+` / `6 个 Agent 共享`
> 旧计数、也无未替换的 `{0}`。另在服务器上用 systemd drop-in 的真实环境导入**已部署**的
> `client_update` + `translations` 进程内断言：`PUBLIC_AGENTS` 为 7 项，三处文案与白名单成员逐一
> 一致（zh/en），终端文案渲染为「`· 7 个 Agent 共享`」「`· shared by 7 agents`」。
> 双远端同步：gitee 推送 `dc1632c..a325cde`；本机网络不通 `github.com:443`（`api.github.com` 可达），
> 改由 236 侧 `git clone --bare` gitee 后推送 github，`refs/heads/main` 已为 `a325cde`（令牌经
> SFTP 一次性脚本传入并即时删除，未落库、未入 argv 之外的文件）。

### Fixed

- **落地页「支持的 Agent」计数不再写死**：开源区该格此前是模板字面量 `6+`，第 7 个 Agent
  （Oh My Pi / `omp`）接入并进入 `PUBLIC_AGENTS` 后仍显示 6。现由 `auth.root` 传入
  `len(PUBLIC_AGENTS)`，落地页三处（开源区数字、终端演示「N 个 Agent 共享」、
  `<meta name="description">`）全部随分发白名单自动变化；`lp_meta_desc` / `lp_term_agents`
  改为 `{0}` 占位（两语同步）。
- **补齐枚举漏项**：`lp_meta_desc` 与 `lp_feat1_d`（zh/en 共 4 条）此前只列 6 个 Agent、
  漏 Oh My Pi，现列全；「主流 Agent 开箱即用」区标题句（zh「七大 Agent 全部支持」/
  en「All seven agents」）改为不带数字的表述，避免再出现硬编码计数。
- `docs/ARCHITECTURE.md` 的 `PUBLIC_AGENTS` 成员说明补上 `omp`（此前漏写）。

### 测试

- 新增 `server/tests/test_landing_agents.py`（4 例 / 12 子例）：经真实路由 `auth.root` 渲染落地页，
  断言开源区数字、终端演示与 meta 描述三处都等于 `len(PUBLIC_AGENTS)`、页面无未替换的 `{0}`，
  且两语文案必须列全白名单中的每个 Agent（只加白名单不改文案即失败）。
- 修复前跑该测试：9 项失败（`6+` vs `7+`、`· 6 个 Agent 共享`、meta 缺 Oh My Pi）；仅回退
  `auth.py` 也失败（页面出现 `+` 与未替换的 `{0}`），证明路由接线同样被覆盖。修复后全绿。
- server 全量 214 用例通过（2 skip）。

## [2026.09.14.5] - 2026-09-14

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），无 schema 变更。
> 已部署 236（2026-09-14 17:41 CST），部署前校验远端 `mailer.py` 与提交 `dc60198` 逐字一致
> （LF 归一化 sha256），改前文件备份为同目录 `mailer.py.bak-htmlmail-2026.09.14`；重启后
> `/health` 200、日志无异常。线上校验（用部署后的代码截获实际待发报文）：激活邮件与重置邮件均为
> `multipart/alternative`（`text/plain` + `text/html`），HTML 部分为 `<a href="URL">URL</a>`
> ——链接可点且显示完整 URL，纯文本部分仍含原始 URL；另向站点邮箱实发一封测试邮件，浏览器渲染
> HTML 部分确认 `href` 与可见文本同为完整链接、光标为 pointer。

### Changed

- **邮件正文改为可点击链接**：此前邮件是纯文本单段（`MIMEText(..., "plain")`），部分邮箱客户端
  （QQ / 126 / 企业邮箱等）不会把裸 URL 自动变成可点链接。现在带链接的邮件改为
  `multipart/alternative`：**纯文本部分原样保留**（URL 明文可见，纯文本客户端行为不变），新增 HTML
  部分把同一 URL 包成 `<a href="URL">URL</a>` —— 可见文字即完整链接，点击直接跳转。
- **`send_mail(..., html_body=...)`**：新增可选 HTML 备选部分；`_html_body(text, url)` 由纯文本正文
  派生（转义 + 换行转 `<br>` + 首个 URL 转锚点），正文只维护一份，两种格式不会各说各话。
- 适用范围：激活/绑定邮箱邮件与重置密码邮件；无链接的「邮箱变更通知」保持单段纯文本。

### 测试

- `server/tests/test_emailverify.py` 新增 `MailFormatTest`（3 例）：激活/重置邮件在 zh-CN 与 en 下
  均为 `multipart/alternative`，纯文本部分含原始 URL、HTML 部分同时具备 `<a href="URL">` 与
  `>URL</a>`（点击 + 显示完整链接）；无链接的通知邮件仍为单段纯文本。测试通过假的 SMTP 连接
  截获真实 `as_string()` 报文，不触网。
- server 全量 212 用例通过（2 skip）。

## [2026.09.14.4] - 2026-09-14

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），无 schema 变更。
> 已部署 236（2026-09-14 17:29 CST），部署前逐文件校验远端与提交 `87c8fa1` 逐字一致（LF 归一化
> sha256），改前文件备份为同目录 `*.bak-ttl3h-2026.09.14`；重启后 `/health` 200、日志无异常。
> 线上校验：`TOKEN_TTL = 10800`；并为最后注册的 3 个仍处 `PENDING_EMAIL_VERIFICATION` 的账号
> （`xiayimiao123` / `xp` / `zzc`）重发激活邮件——新令牌 `expires_at - created_at = 10800`、
> 旧令牌按 resend 语义被撤销、3 封邮件均被 SMTP 接受且正文写「链接 3 小时内有效」，随后外部
> （126 邮箱扫描器）在 17:31:00 拉取三条链接全部 200（确认页，GET 不消费令牌，符合设计）。

### Changed

- **链接有效期统一为 3 小时**：`emailverify.py` 回到单一 `TOKEN_TTL = 3 * 3600`，激活链与重置链
  同窗口。上一版的按用途 TTL 表（12 h / 30 min）随之删除：两个值再次相同，映射已是死结构。
- **文案统一由常量派生**：新增 `mailer._link_window()`，从 `TOKEN_TTL` 算出「3 小时 / 3 hours」
  （整小时给小时、否则给分钟），激活与重置两封邮件共用；等待页、忘记密码、安全信息页共 5 条中英
  词条同步改为 3 小时。

### 测试

- `server/tests/test_emailverify.py`：到期时间断言改为「激活与重置两个用途都等于 `TOKEN_TTL`」；
  新增文案漂移防线——两封邮件正文 + 5 条页面词条（zh/en）都必须含 `_link_window()` 给出的窗口，
  改了 TTL 不改文案即测试失败；另覆盖「整小时 / 非整小时」两种窗口格式。
- server 全量 209 用例通过（2 skip）。

## [2026.09.14.3] - 2026-09-14

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），无 schema 变更。
> 已部署 236（2026-09-14 17:25 CST），部署前逐文件校验远端与提交 `be2d1e2` 逐字一致（LF 归一化
> sha256），改前文件备份为同目录 `*.bak-ttl12h-2026.09.14`；重启后 `/health` 200、日志无异常。
> 线上校验：DB 中激活令牌 `expires_at - created_at = 43200`、重置令牌仍为 `1800`；中/英激活
> 邮件正文分别写明「12 小时 / 12 hours」，重置邮件仍写 30 分钟；等待页文案已更新；侧边栏
> 「安全信息」计算色值与「更改信息」一致（`rgb(75, 85, 99)`）。

### Changed

- **激活链接有效期 30 分钟 → 12 小时**：`emailverify.py` 改为**按用途**的 TTL 表 `PURPOSE_TTL`
  （`VERIFY_EMAIL_TTL = 12 * 3600`、`TOKEN_TTL = 30 * 60`），`issue_token` 按用途取用，未知用途
  回退 `TOKEN_TTL`。**重置密码链接保持 30 分钟**：那条链接本身即足以接管账户，不宜一并延长
  （本次需求只针对激活链）。
- **邮件正文由常量派生**：`mailer.send_verification_mail` 的小时数取自
  `emailverify.VERIFY_EMAIL_TTL`，窗口与文案不会再各说各话；重置邮件文案未动。
- **等待页文案**：`translations.verify_wait_desc`（zh-CN / en）改为「链接 12 小时内有效 /
  valid for 12 hours」。
- **侧边栏「安全信息」文字颜色**：`templates/base.html` 里该按钮由 `text-gray-400`（分区标题色，
  视觉上像禁用态）改为 `text-gray-600`、hover `text-gray-900`，与其下「更改信息」及各子菜单
  一致；字号 / 大小写 / 缩进等版式未动（仍可与上方「语言」标题区分）。

### 测试

- `server/tests/test_emailverify.py` +3：`issue_token` 写入的到期时间按用途分别为 12 h / 30 min
  （固定 `now` 断言实际写入值）；激活邮件正文含派生小时数且不再出现「30 分钟」；重置邮件仍为
  「30 分钟」（monkeypatch `send_mail` 捕获正文，不触网）。
- server 全量 208 用例通过（2 skip）。

## [2026.09.14.2] - 2026-09-14

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），无 schema 变更。
> 已部署 236（2026-09-14 17:19 / 17:22 CST 两批），部署前逐文件校验远端与提交 `e794b1c` 逐字
> 一致（LF 归一化 sha256；`jsonbody.py` 需不存在），改前文件备份为同目录
> `*.bak-json400-2026.09.14` 与 `workspace.py.bak-wscreate-2026.09.14`。重启后 `/health` 200、
> 日志无新异常；线上校验：7 个 JSON 端点畸形/非对象请求体 = 400、合法请求行为不变、
> `POST /api/workspaces` 返回真实 id、重名 409；另用临时非管理员账号复核侧边栏「安全信息」
> 三个子菜单（见下）。

### Fixed（三处：请求体解析 500、「绑定邮箱」深链、`POST /api/workspaces` 必 500）

1. **非 JSON 请求体应 400，此前是 500**：`/api/auth/login`、`/api/auth/register`、`/pull`、
   `/push`、`/api/projects/push`、`/api/me/change-password`、`/api/workspaces` 都直接
   `await request.json()`：体不是 JSON 时 `json.JSONDecodeError` 逃逸成 500；体是合法 JSON 但不是
   对象（如 `[1]`）时下一步 `.get()` 抛 `AttributeError`，同样是 500。新增
   `server/jsonbody.py::json_object()`（解析 + 要求对象，失败抛 400），7 处调用点统一改用——
   解析位于配额/DB 之前，畸形请求快速失败。
2. **安全信息页「绑定邮箱」按钮打不开邮箱对话框**：`security.html` 深链写成 `?d=email`，而对话框
   id 与白名单是 `dlgEmail`，参数被丢弃后停在枢纽页不动（未验证邮箱的账号才会看到该按钮）。改为
   `?d=dlgEmail`，与侧边栏子菜单、枢纽列表按钮一致。
3. **`POST /api/workspaces` 对任何合法请求都 500**：INSERT 缺 `RETURNING id`，返回处又写成
   `c.fetchone()[0] if c.fetchone() else None`（两次 `fetchone()`，且已跑出 `get_conn()` 块），
   实测 `psycopg2.ProgrammingError: no results to fetch`。更糟的是 INSERT 在异常前已提交：客户端
   收到 500，工作空间却已建好，重试即产生重复条目。改为 `RETURNING id` + 块内单次
   `fetchone()[0]`，返回真实 id。

### 测试

- 新增 `server/tests/test_jsonbody.py`（7 例：对象透传、畸形体 400、非对象体 400，以及
  `api_login`/`pull`/`push` 三条路由的 400 契约——解析发生在 DB 之前，故无需数据库）。
- `server/tests/test_workspace.py` 新增 `ApiCreateWorkspaceTest`（返回 id = 新工作空间 id；
  空名 400 且不落库）。假游标在无结果集时按 psycopg2 抛 `ProgrammingError`，还原修复前形态。
- 三处均可复现：还原改动分别得 `JSONDecodeError`、`?d=` 失效、`ProgrammingError`，修复后通过。
  server 全量 205 用例通过（2 skip）。

## [2026.09.14.1] - 2026-09-14

> 服务端专用发布：无客户端改动（`CLIENT_VERSION` 保持 2026.09.13.4 不变），无 schema 变更。
> 已部署 236（2026-09-14 17:12 CST），部署前校验远端 `auth.py` 与提交 `e2d1514` 逐字一致
> （LF 归一化 sha256 `1ee30069…`），重启前把原文件备份为同目录的
> `auth.py.bak-verify-email-fix-2026.09.14`；重启后 `/health` 200、日志无异常，并用线上
> 真实令牌跑通一次激活（`POST /web/verify-email` → 303 `/web/`）。

### Fixed（邮箱注册的首次激活必 500：`RealDictCursor` 上按位置取 `RETURNING id`）

- **现象**：邮件里的激活链接本身正常（GET `/web/verify-email?token=…` 200，停在「确认并激活」
  页），但点按钮 `POST /web/verify-email` **必 500**（线上 journalctl：
  `auth.py:656 ws_id = c.fetchone()[0]` → `KeyError: 0`）。用户停在
  `PENDING_EMAIL_VERIFICATION`；异常发生在 `get_conn()` 事务内，回滚后令牌未消费
  （刷新页面可重试，TTL 30 分钟）。
- **根因**：`server/auth.py::web_verify_email_confirm` 用
  `psycopg2.extras.RealDictCursor` 建游标（用户行按 `u["id"]` 取值），但新建默认工作空间后
  `RETURNING id` 那行仍按位置取 `[0]` —— dict 行没有下标 0。触发条件是
  「`auth_source = EMAIL_REQUIRED` 且名下无工作空间」，正是邮箱注册的必经分支：**新注册账户
  100% 无法激活**；老账户换绑邮箱因已有工作空间不走该分支，未受影响。
- **修复**：改用 `c.fetchone()["id"]`（一行），并在原处注明游标类型防止同类回归。
- **测试**：`server/tests/test_register.py::EmailActivationTest` 的假游标此前用元组 `(1,)`
  喂 `RETURNING id`，与真实 `RealDictCursor` 形状不符，恰好掩盖了该缺陷；现改为 `{"id": 1}`
  （workspace-exists 检查行同步改为 `{"?column?": 1}`），并断言审计行记录新建工作空间
  （`audit[3] == 1`）。回归可复现：把 `[0]` 改回去即 `KeyError: 0`，修复后通过。
  server 全量 196 用例通过（2 skip）。

## [2026.09.13.4] - 2026-09-13

> 客户端发布：`CLIENT_VERSION` 2026.09.13.3 → **2026.09.13.4**（客户端包有改动，各端经
> `/api/client/manifest` 自动更新，Agent 重启后生效）；服务端仅 `client_update.py` 的版本常量，
> **无 schema 变更、无数据迁移**（13.3 已建的 `sessions.last_activity_at` 继续用）。

### Fixed（补齐 2026.09.13.3 漏掉的一个落点：opencode 的 `session.time_updated`）

- **漏判**：13.3 只改了 workbuddy / omp / openclaw 三个 adapter（外加共享的 `adapters/base.py`），
  理由栏里把 opencode 记为"已用 `ended_at`，不改"——但它和另外三个是同一类落点：`time_updated`
  是 opencode 桌面端的排序键，写入用的是 `ended_at`（缺失才回退 `now`），而 `ended_at` 各 agent
  语义不一致（13.3 实测 40 条拉取会话：5 条缺失、只有 3 条等于最新消息时间）。即那条会话在
  opencode 列表里仍可能显示成"刚刚"或陈旧时间。
- **修复**：`mcp/adapters/opencode.py` 的 INSERT/UPDATE 两条路径都改用共享助手
  `session_last_activity()`（服务端 `last_activity_at` → 本次载荷最新消息 → `ended_at`），
  并同样保留 `>= time_created` 下界；`now` 只在该会话没有任何可用时间戳时兜底。
- **复核结论（7 个注册 adapter 全量过一遍）**：改代码 **4 个**（13.3 的 workbuddy/omp/openclaw +
  本次 opencode）；**hermes** 零代码改动（`state.db` 本就有 `last_activity_at` 列，进 canonical 后
  1:1 映射自动读写）；**dsh/reasonix** 无"最后更新"元数据（dsh 投影缓存只有 `createdAt`，列表顺序
  由日志/消息决定）；**chatgpt** 只读上传不写本地。注册表共 7 个（`hermes/opencode/reasonix/
  openclaw/workbuddy/omp/dsh`；作者工作区里未提交的 `chatgpt` 为第 8 个，只读）。
- **测试**：`mcp/tests/test_opencode.py` +1（写入路径的 `time_updated` = 最新消息时间、不早于
  `time_created`；更新路径同样如此）。mcp 套件 196 项、server 套件 196 项通过（仅 3 项既有环境失败）。
- **文档**：ARCHITECTURE 决策记录 2026.09.13.3 的 adapter 映射表与回归防线同步修正（opencode 由
  "不改"改为"已补齐"）——代码与文档不允许各说各话。

## [2026.09.13.3] - 2026-09-13

> 客户端发布：`CLIENT_VERSION` 2026.09.13.2 → **2026.09.13.3**（`mcp/adapters/*` +
> `mcp/updater.py` 有改动，各端经 `/api/client/manifest` 自动更新，Agent 重启后生效）；
> 服务端 `server/sync.py`（push 派生 `last_activity_at`）+ `server/db.py`（新增列，幂等 ALTER）
> ——**有 schema 变更**（`sessions.last_activity_at DOUBLE PRECISION`），存量行由
> `scripts/backfill-last-activity.py` 一次性补齐。

### Fixed（拉取到本机的会话"最后更新时间"是同步时刻，不是最新消息时间）

- **现象**：从远端同步下来的会话在本地一律显示成"刚刚"——WorkBuddy 列表按
  `COALESCE(updated_at, created_at)` 排序，一次同步把被触碰的会话全部塌到**同一个时间戳**并顶到
  最前（实测本机 `workbuddy.db`：6 行最近被触碰的会话 `updated_at` 全等于同步时刻，而它们的最新
  消息分别在 08-21 / 08-29 / 09-03）；Hermes 桌面端的 `last_activity_at` 对同步创建的会话是
  **NULL**；omp 的 title slot `updatedAt`、OpenClaw 索引的 `updatedAt`/`lastInteractionAt`/
  `lastActivityAt` 都恒写 `now`。
- **根因**：`last_activity_at` **不在 canonical 模型里**，各 adapter 只能拿 `ended_at`，而
  `ended_at` 各 agent 语义不一致（2026-09-13 实测 40 条拉取会话：**5 条缺失**、只有 **3 条**等于
  最新消息时间），且 workbuddy 的写入路径还把值 `max(..., now)` 钳到同步时刻。
- **修复（服务端派生 + 客户端消费）**：
  - `sessions.last_activity_at`（新列）= 最新一条可见消息的 `timestamp`；`server/sync.py::push`
    在装配行数据前用它**本次载荷的消息**取 `max(timestamp)`（客户端同名字段被覆盖）；没有消息的
    元数据型 push（只改标题等）无派生来源，客户端值原样透传。
  - `mcp/adapters/base.py`：`last_activity_at` 进 `CANONICAL_SESSION_FIELDS`；新增共享助手
    `session_last_activity()`（服务端值 → 最新消息 → `ended_at` → `now`），规则一处定义。
  - **workbuddy**：`updated_at` 与 `last_activity_at` 都写该值（去掉 `now` 钳制，仅保留
    `>= created_at` 下界）；读回 `last_activity_at`。**omp**：title slot `updatedAt`。
    **openclaw**：索引三个时间字段 + 读回 `lastActivityAt`。**hermes**：零代码改动（`state.db`
    本就有该列，进 canonical 后 1:1 映射自动读写）。opencode（已用 `ended_at`）/dsh/reasonix
    （无该元数据）/chatgpt（只读）不改。
  - **存量数据**：`scripts/backfill-last-activity.py`（dry-run 默认 / `--apply`，幂等，只看可见
    消息），避免老行永久 NULL。
- **测试**：`mcp/tests/test_base.py::SessionLastActivityTest`（规则优先级 4 例）、
  `test_workbuddy.py` +2（落库 = 最新消息时间；服务端值优先；无时间戳才 now）、`test_omp.py` +2
  （title slot 用最新消息时间 / 无时间戳才 now）、`test_openclaw.py` +1（索引三字段 + 读回）、
  `test_hermes.py` +1（列往返）、`server/tests/test_sync.py` +2（push 派生覆盖客户端断言 /
  元数据型 push 透传）。mcp 套件 195 项、server 套件 196 项通过（仅 3 项既有环境失败）。

## [2026.09.13.2] - 2026-09-13

> 客户端发布：`CLIENT_VERSION` 2026.09.13.1 → **2026.09.13.2**（`mcp/adapters/workbuddy.py` +
> `mcp/adapters/base.py` + `mcp/server.py` 有改动，各端经 `/api/client/manifest` 自动更新，
> Agent 重启后生效）；服务端仅 `client_update.py` 的版本常量，无 schema 变更、无数据迁移。

### Added（workbuddy 项目清单接入共享项目池：本机项目列表与服务端项目卡片对齐）

- **背景**：项目是工作空间级共享池（`/api/projects/pull` 返回全部可见项目，与 `agent` 无关），
  但此前只有 hermes 在推。WorkBuddy 桌面端的项目列表（`workbuddy.db` 的 `workspaces` 表：
  `path` + `last_opened_at`）完全未上行——服务端 10 个项目全是 hermes 的，本机 WorkBuddy 的
  8 条 workspace 一条都不在服务端；网站的项目卡片按 `cwd` 前缀关联会话，因此 WorkBuddy 会话
  也大量落在项目之外（实测 286 条会话中 191 条所在目录不属于任何服务端项目文件夹）。
- **实现**（`mcp/adapters/workbuddy.py`，新增 `supports_projects = True`）：
  - **read（push 视图）**：`workspaces` 每条路径 = 一个项目。路径已由上次 pull 记入身份侧车
    `.workbuddy-sync-projects.json` → 原样使用**服务端**的 id/slug/name/folders（若改用目录名等
    派生值，pull 后本地值与锚定 base 不等，会被判为"本地脏"、每轮 push 覆盖对端项目名）；
    服务端未见过的路径 → 铸 `wb_<sha1(_path_key(path))>`（确定性：丢侧车或第二台设备同路径
    收敛到同一项目）+ `slugify(path)` 做 slug（整路径压平 ⇒ 逐路径唯一，不会触发服务端同名
    合并）+ 目录名做 name，并立即写入侧车（`workspaces` 表放不下 id，侧车即本地 id 注册表）。
  - **write（pull 视图）**：按本次 pull **全量重建**侧车，并为每个项目路径补一条 `workspaces`
    行（同时建目录）。已有行只保留、不再定日期——`last_opened_at` 是 app 自己的"用户打开过"
    时钟，不是同步数据；本地行**永不删除**。remap 无需本地迁移：本地项目库不以 id 为键，
    合并后的项目随本次 pull 直接重建为存活 id。
- **能力位**（`mcp/adapters/base.py` + `mcp/server.py`）：新增 `supports_projects`（默认 False，
  hermes/workbuddy 为 True）。没有本地项目库的适配器（opencode/dsh/omp/reasonix/openclaw/
  chatgpt）在周期同步里直接跳过项目阶段，工具面返回 `Agent X has no local project store …`——
  此前它们每轮同步都记一条 `Projects sync error: … has no attribute 'read_projects'` 日志。
- **文档**：ARCHITECTURE（workbuddy 适配器"项目"小节 + 修正此前"workbuddy 无独立项目实体"的
  分析记录）、SUPPORTED_AGENTS（项目池与能力位）、ADDING_AGENT（第 5 条：可选项目契约）。
- **测试**：`mcp/tests/test_workbuddy.py` +4（铸造身份稳定且逐路径唯一、pull 采纳服务端身份且
  不把目录名当本地改名、既有行不定日期 + 本地独有路径保留铸造 id + 重复 pull 幂等、空库）。
- **线上验证**（本机真实 WorkBuddy store ↔ 线上工作区 4，用 workbuddy 部署的 key）：
  pull 落地 8 条 `workspaces`（含为 `C:/Users/rong/Documents/对话分析` 建目录）、采纳 9 个服务端
  项目身份；push 服务端 10 → **13** 个（新增 `D:`、`rong`、`开发库`），既有 10 个项目**零字段
  改动**（`imported:3, updated:9, merged:0`，逐字段比对 name/slug/primary_path/archived/folders
  全部一致）；二次 push `imported:0, merged:0`（幂等）。收敛结果：本地 12 ↔ 服务端 13，唯一差异
  是 `2026下半年软考`（`F:/软考/2026下半年`，本机无 F: 盘 → 无法落地，属预期；该路径不会再被
  铸成第二个项目）。

### Added（根目录不入共享项目池：识别根形状路径并排除，退役已上行的根卡）

- **背景**：上一条上线后 workbuddy 的 `D:\`、`C:\Users\rong` 这类"根"条目被如实推成项目卡。
  Web 的项目↔会话关联是 `cwd` **前缀匹配**项目 folder（任意深度、不排他），根是万物祖先 ⇒
  卡片变**兜底桶**（实测：`rong` 吞 99 条、`D:` 吞 27 条，合计 **110/286**，其中 62 条是
  `~/hermes-sync-foreign` 合成目录）；且服务端 `project_folders` **只并集、无删除 API**、项目也
  **没有 Web 隐藏/删除入口** ⇒ 一旦上行即永久。因此"各 agent 拉到时把本机 home 并进根项目"的
  方案被否决（详见 ARCHITECTURE 决策记录 2026.09.13.2，含全部四条理由与被保留的服务端 alias 备选）。
- **实现**：新增 `mcp/adapters/base.py::is_root_project_path`（盘符/filesystem 根、
  `/home`·`/Users`·`C:/Users` 及其子项 `/home/<n>`）与 `strip_root_project_paths`；
  `push_projects`/`pull_projects` 在边界统一过滤（纯根项目整条不上行/不落地；根 `primary_path`
  只置 `None`，**绝不改写**——它是 per-field LWW 字段，改写会与对端逐周期互推覆盖）；
  workbuddy 适配器的 `read_projects`/`write_projects`/侧车另做清理，保证不铸永不上的 id。
  本地路径**全保留**（`D:\` 仍是 WorkBuddy 的 workspace；hermes `projects.db` 不动）。
- **退役已上行的两张卡**：新增 `scripts/hide-root-projects.py`（默认 dry-run；`--apply` 置
  `hidden=1`，`--undo` 还原）。`hidden` 同时被 `/api/projects/pull` 与 Web 项目列表过滤，
  且 push 的 UPDATE 分支从不写 `hidden` ⇒ **不会被客户端复活**（与"push 不复活隐藏会话"同规则）。
- **实测影响**（生产 workspace 4 + 本机 workbuddy store）：服务端 13 → **11** 张卡；本机参与
  同步的 workspace 键 15 → 13；本机会话归组 207/286 → **97/286**。**诚实结论**：本次项目同步
  对本机会话分组的净收益≈0（97 vs 加项目同步前的 95），因为本机 workbuddy 会话大多住在
  `C:\Users\X1`(54)、`~/hermes-sync-foreign`(71)、`c:/tmp`(7) 等非项目目录；真实收益是
  "项目清单对齐 + 其他机器/agent 的会话能归入这些项目"。差异由"1 条（不可落地的 `F:` 项目）"
  变为"3 条（+2 条根形状，属策略声明）"。
- **测试**：`mcp/tests/test_project_roots.py`（谓词正/反例表、`strip_root_project_paths` 语义、
  **运维脚本谓词与 base 谓词一致性**）、`mcp/tests/test_mcp_server.py::RootProjectFilterTest`
  （push 丢根/纯根不上行、pull 不落地）、`mcp/tests/test_workbuddy.py` +1（根 workspace 不铸号、
  根项目不落地、重启稳定）。

## [2026.09.13.1] - 2026-09-13

> 客户端发布：`CLIENT_VERSION` 2026.09.12.5 → **2026.09.13.1**（`mcp/adapters/hermes.py` +
> `mcp/server.py` 有改动，各端经 `/api/client/manifest` 自动更新，Agent 重启后生效）；服务端仅
> `client_update.py` 的版本常量，无 schema 变更、无数据迁移。

### Fixed（hermes 会话在服务端可见却永远拉不下来：`profile_name='default'` 被当成未知档案跳过）

- **现象**：服务端可见的 hermes 会话在某台设备的本地库里始终没有；客户端日志出现
  `skipped N session(s) from profiles/agents not present on this machine`；2026.09.12.5 的完整性
  修复会每轮把它们重新取回一次（白流量）并误报 `Restored N`（服务端送 N 个、落地 0 个）。
- **根因**：hermes 本地 `state.db` 自带 `profile_name` 列，字面默认值是 `default`；
  `canonicalize()` 以本地行为底，于是该字面值随 push 上行并被服务端原样存储。规范里「默认档案」
  的拼写是 `""`/NULL，而 pull 侧路由表（`{'': 默认库, '<name>': 档案库}`）没有 `default` 键 →
  `route.get('default')` 为 None → 整行被静默丢弃（只留一行汇总日志）。
- **修复**（客户端 `mcp/adapters/hermes.py`）：默认档案下 `canonicalize()` 不再把本地列字面值
  带进 canonical（新 push 不再产生 `default` 行）；`write_sessions()` 把 `default` 当作默认档案
  别名路由（仅当本地确实没有名为 `default` 的档案时），存量行因此可以落地。
- **顺带修掉计数说谎**（`mcp/server.py`）：完整性修复的 `restored` 改为按**实际落地数**
  （`imported + updated`）统计，并新增 `unfiled` 计数与日志——适配器无法归档（未知档案 / 只读库）
  时不再每轮谎报 `Restored N`。
- **测试**：`mcp/tests/test_hermes.py` +2（canonicalize 不带字面值、别名路由落地）、
  `mcp/tests/test_mcp_server.py` +1（无法归档时 `restored=0, unfiled=1`）。
- **线上验证**：本机 hermes store 缺 3 个可见会话（含 `系统架构设计师复习计划与刷题工具调研`），
  用修复后代码拉一次 → `imported: 3, new_messages: 420`，3 个会话全部落地（目标会话 169 条消息）。

## [2026.09.12.5] - 2026-09-12

> 客户端发布：`CLIENT_VERSION` 2026.09.12.4 → **2026.09.12.5**（客户端包有改动，各端经
> `/api/client/manifest` 自动更新，Agent 重启后生效）；服务端 `server/sync.py` 的 `POST /pull`
> 增加可选完整性字段（`known_ids`/`ids`/`missing_ids`），无 schema 变更、无数据迁移。

### Added（本地删掉的会话下一轮同步补回：pull 完整性修复）

- **行为**：服务端**可见**（`hidden=0`）的会话集合 = 客户端本地存储应收敛到的集合。本地删掉一行后，
  下一轮 pull 自动补回（含全部消息）；`hidden=1`（Web 软删除 / 回收站）仍是唯一的服务端退休信号。
- **背景**：pull 是增量的（`last_synced_at > 水位线`），协议里无法区分"本地少一行"与"这行还没拉过"。
  旧行为取决于该会话上次被推送的时刻——落在 5 分钟宽限窗内下一轮复活、早于宽限窗则永不出现，
  直到别的设备改动它。同一根因还覆盖"水位线过新导致的可见会话静默漏拉"（切换服务器后的残留水位线、
  会话在隐藏期间被创建/解禁）。
- **机制**：`POST /pull` 新增两个可选字段——`known_ids`（客户端第一页上报本机 id 清单）→ 服务端
  第一页回 `missing_ids`（可见但本机没有的 id）→ 客户端用 `ids=[...]` 按 id 点名取回（忽略增量截断，
  仍走 `hidden=0`）。修复写入复用普通拉取页的字段级合并/去重/锚定路径，失败尽力而为（日志 + 下轮重试）。
- **成本**：稳态零额外请求、零额外下行（缺失集通常为空）；每轮 pull 上行一份 id 清单，实测
  约 45 B/id（混合 id 形状：hermes 22 字符 ≈ 25 B、UUID ≈ 39 B）→ 1k 会话 44 KiB/轮、10k 会话
  427 KiB/轮，300s 间隔折合 0.15–1.4 KiB/s。服务端对账为**一次索引 id 扫描 + Python 集合差**
  （O(可见会话数)），**不走** SQL 数组过滤（`id <> ALL($1)` 用不上 `(workspace_id, id)` 主键，
  退化为 O(可见 × |清单|)）。
- **兼容**：旧服务端忽略新字段、旧客户端不发新字段，双方均退化为原增量语义，无需版本协商。
- **测试**：`server/tests/test_sync.py::PullCompletenessTest`（5 例：清单对账、仅第一页、旧客户端不变、
  `ids` 取回忽略截断且过滤 hidden、id 载荷清洗）、
  `mcp/tests/test_mcp_server.py::PullCompletenessRepairTest`（5 例：删后补回、当页已投递不重复取、
  只读适配器不问、全量拉取不上报清单、`limit` 额度权威）。
- **上线注意**：客户端与服务端两侧都需更新——只更新一侧时行为与原状一致（客户端侧 `pull_sessions`
  发清单、服务端侧 `pull_sync` 回缺失 id，缺一侧则不触发修复）。决策记录见
  `docs/ARCHITECTURE.md`「本地删除不是删除信号（Pull 完整性修复）」。

## [2026.09.12.4] - 2026-09-12

> 客户端发布：`CLIENT_VERSION` 2026.09.12.3 → **2026.09.12.4**（客户端包有改动，各端经
> `/api/client/manifest` 自动更新）。服务端仅帮助页内容更新（`server/agents.py` 的 dsh
> 注册片段），无 schema 变更。

### Fixed（dsh 同步下来的会话在新版桌面里打不开 —— v0 日志过不了 dsh 的迁移链）

- **根因**：dsh 读器对旧世代产物跑 **v0→v1→v2→v3 迁移链**，而 adapter 过去给新会话写的是
  「header + `session/title` + `user/message` + `assistant/message`」的 v0 日志：链上第一处
  拒绝是 `format v2 surface before first step cannot acquire a system head without
  changing chronology`（缺种子头/轮次骨架），补上种子头后下一处是
  `unexpected member "sourceEventSeqs"`。结果是**所有同步进来的会话**在 DSH Desktop
  （0.1.5-rc.2 / 桌面 2.0.x）打开时 `session/follow` 直接失败，UI 报
  「历史加载失败：network error（gateway/internal）」（`network error` 是 Chromium 对中断
  响应流的文案，`gateway/internal` 是网关错误码），而 dsh 自写的 v3 会话读取正常。
- **定位（实测）**：用一元 RPC `session/page` 拿到完整错误（同一会话在清空重拉前后报同一
  错误）；从备份恢复 dsh 原生 v3 会话后 `session/follow` 立刻正常 → 排除读路径与"会话被
  清空"这一变量；临时把 profile 的 MCP 注册置空重启，错误一字不变 → 与同步插件无关。
- **修复**：`_write_log` 改为**发布当前世代（v3）日志**——v3 header（`isSeeded:false`、
  `agentPreset:"standard"`）+ 种子头（`permission/preset`/`sandbox/mode`/`approval/policy`）
  + 每轮 `turn/start`/`step/start`（同一轮多个模型步各自成步、`step/end` 收束）
  + assistant/message 带 `usage`/`stream` 结算块、去掉 `sourceEventSeqs`；外来世代只保留身份
  字段（`version`/`isSeeded`/`agentPreset` 是世代专有）。既有旧世代文件**逐字节冻结**，后继
  写成新文件；`write_sessions` 对**旧世代日志即使没有新消息也升级**（否则存量 store 永远
  打不开）；标题事件改为始终随日志发布（后继才是 dsh 读的文件）。
- **顺带修掉同路径的写入 churn**：`_merge_messages` 不再把**无文本**回复（仅工具调用/推理的
  assistant 轮）写成空事件——读侧本就不认这类行，写侧却让它在每次拉取里"再新一次"，
  于是每轮同步都会重写全部会话（实测 25171 条消息事件里 8208 条为空文本）。修复后同一
  数据集单轮只改写真正有新消息的会话（实测 14/553 个日志）。
- **端到端验证（用 dsh 自己的读器，非自证）**：修复后对真实 store 跑一次全量拉取
  （276 个会话目录全部带 v3 文件、0 个 v0-only）；桌面实测三类会话（多轮拉取会话 /
  超长路径会话 / dsh 原生 v3 会话）`session/page` 全部 `ok`、`session/follow` 正常返回 v3
  快照，UI 正常渲染出历史（不再显示加载失败）；再跑一轮全量拉取确认幂等（无新消息的会话
  零改写）。
- **测试**：`mcp/tests/test_dsh.py` 新增 `CurrentGenerationWriteTest` 4 例（新会话的世代/
  种子头/轮次骨架/seq 连续、assistant 结算字段且无 `sourceEventSeqs`、存量 v0 会话升级且
  前代逐字节不变、无文本回复不落盘且不触发重写），并更新受契约影响的既有用例（标题-only
  升级、cwd 漂移、zstd 往返、`_no-cwd`）；mcp 套件 155 项、server 套件 189 项通过。

### Fixed（dsh 注册片段缺 `cwd` —— 新版桌面静默不起 MCP 客户端）

- **根因**：新版 `@deepseek-ai/dsh-mcp-client`（随 DSH Desktop 0.1.5-rc.2 分发）把 stdio 的
  `cwd` 定义为 `z.string().default("")`，并**无条件**传给 MCP SDK 的 `spawn()`；Node 对
  `cwd: ""` 直接抛 `ENOENT`（实测），插件启动失败又被 `failOnStartupError: false`
  （`registrationFailure: "contain"`）吞掉。表现极具迷惑性：**桌面照常启动、会话列表正常，
  但同步静默不跑**——没有 MCP 子进程、没有 `mcp__hermes-sync__*` 工具、水位线不动。
- **修复**：注册行补 `cwd: '<EXTRACT_DIR>'`（指引器解压目录，必须真实存在）；
  `server/agents.py` 的中英文注册片段与安装说明同步更新，并注明该陷阱与
  `failOnStartupError` 的取舍（`false` = 不阻塞桌面启动；需要定位启动错误时临时改 `true`，
  桌面启动页会直接显示合成错误）。
- **定位手法（可复用）**：把 profile 的 `cordis.patch.yml` 写成非法 YAML → 桌面启动页报
  `failed to parse overlay …` 证明该文件确实被读取；`failOnStartupError: true` 让被吞掉的
  插件错误显示在启动页；同环境手工复跑客户端可对照验证（输出
  `Agent: dsh (local store: ~/.dsh/sessions)` 并接管 `hermes-sync-dsh.lock`）。
- **验证（实测）**：改前桌面里没有 MCP 子进程；补 `cwd` 后重启，桌面拉起客户端
  （父进程 = desktop host 的 `node.exe`），客户端成为 primary、持有 `hermes-sync-dsh.lock`
  并完成启动同步。**注意**：旧 app 进程遗留的客户端会占着锁，新客户端会退化为 standby
  （整生命周期不同步）——升级/重启桌面时先确认没有遗留的
  `agentctxsync-mcp-client-*/mcp/server.py` 进程。

### Fixed（hermes 推送被 BLOB 列整轮打断 —— 会话永远停在旧快照）

- **根因**：Hermes 0.20 为每条消息写了 `messages.display_identity`（32 字节 BLOB 哈希）。
  `SQLiteAdapter._map_cols` 的规则是「非空列全部拷进 canonical 消息」，于是 BLOB 原样变成 Python
  `bytes`；而推送路径两处都要 `json.dumps` —— 分批前的 `_chunk_sessions._size`（算体积）与
  `api_call`（编码请求体）。`TypeError: Object of type bytes is not JSON serializable` 从
  `_chunk_sessions` 直接抛出，于是**整轮 push 中止**（不是跳过单会话）——该设备此后一条都推不上，
  服务端留着几小时前的旧快照（实测：本地 170 条 vs 服务端 `message_count = 1`）。
- **修复**：`_map_cols` 不再把二进制列放进 canonical 消息（BLOB 是 harness 内部状态，canonical 模型
  是 JSON，没有对应槽位就不带）。走 `SQLiteAdapter` 的只有 `HermesAdapter`，影响面即 hermes 一条链。
- **验证（实测）**：本地该会话 170 条（9 user / 66 assistant / 95 tool，约 1.09 MB）修复后可 JSON 编码、
  单块推送成功（`imported 2 / updated 11 / new_messages 417`），服务端 `message_count` **1 → 170**、
  标题同步为真实标题，并顺带补上此前被卡住的另外两个 hermes 会话（202 条 / 43 条）。
- **测试**：`mcp/tests/test_hermes.py::BinaryColumnTest` 2 例（BLOB 列不进 canonical 消息、整份读结果可
  `json.dumps`）；mcp 套件 155 项通过。

### Fixed（推送：单个不可编码的会话不再拖垮整轮）

- **背景**：上一条的 BLOB 能造成"整设备停摆"，是因为 `_chunk_sessions` 用 `json.dumps` 算每个会话的体积、
  `api_call` 又在 `try` 之外编码请求体——任意一个不可序列化的值都会在**发送之前**抛出，整轮 push 直接失败。
- **修复**：① `push_sessions` 先按指纹过滤、再做可编码性分区（`_partition_encodable`）：不可编码的会话
  **按会话隔离**，日志点名到字段（`_json_offender` → `… (messages[0].display_identity (bytes))`），其余会话
  照常分批推送，结果里带 `unsendable: [id...]`；② `api_call` 把请求体编码挪进 `try`，编码失败按普通请求
  失败返回 `{"error": …}`（单块失败不再中断整轮，push/pull 同等受益）；③ 指纹过滤提前到分块之前，未变化的
  大多数会话不再参与体积计算（hermes 那种全量上百 MB JSON 的开销省掉）。
- **验证**：在真实服务端上注入一个含 `bytes` 的探针会话 → 日志点名、无异常抛出、同一轮仍推送了真实变更
  （`updated 2 / new_messages 6`），结果返回 `unsendable: ["zz-isolation-probe"]`，探针未上云。
- **测试**：`mcp/tests/test_mcp_server.py::PushIsolationTest` 3 例（坏会话被跳过且其余照推 + 指纹只在成功后
  落盘、跳过原因点名到字段、`api_call` 对不可编码载荷返回错误而非抛出）；mcp 套件 155 项通过。

## [2026.09.12.3] - 2026-09-12

> 客户端发布：`CLIENT_VERSION` 2026.09.12.2 → **2026.09.12.3**（客户端包有改动，各端经
> `/api/client/manifest` 自动更新）。服务端无 schema 变更。

### Fixed（dsh 世代选择方向反了 —— 迁移过的会话会读到冻结的旧日志）

- **根因**：dsh 的会话日志有**世代**：`session.jsonl[.zstd]` = v0，`session.vN.jsonl[.zstd]` = vN
  （当前为 v3）。`dsh-session-persistence-jsonl` 明确规定：运行期读与写都选**数值最高的世代**；
  写入发布后继代并**保持源文件逐字节不变**（所以迁移过的 v0 从此冻结，只有 v3 继续增长），
  且保留的前代**不提供降级读取**。
- 旧 `_log_file` 先试 `session.jsonl`/`session.jsonl.zstd`、找不到才回退 vN —— 方向正好相反：
  对迁移过的会话会**永久读到冻结的 v0**（丢失后续轮次与标题），而写入落进 v0，
  那是一个 dsh 永不读取的文件（写入等于无效，还在存储里留了个误导性的副本）。
- **修复**：`_log_file` 改为镜像 dsh 的规则（按 `(世代, 压缩优先, mtime)` 取最高世代）；
  `_write_log` **保持会话既有的世代与编码**（`_log_filename(gen, zstd)`），不再在 vN 会话旁写 v0；
  并**原样保留既有 header**（`isSeeded` / `agentPreset` 等世代专有字段据此存活），
  物理 header 的 `version` 恒等于文件名世代；对合成的 vN header 兜底补 `isSeeded`
  （dsh 的 v2→v3 header 校验要求该字段）。读取侧无需改动：`_event_message` 本就同时支持
  v3 行形态（`data.{role,content}` 与 `data.message.{role,content}`）。
- **验证用 dsh 自己的编解码器（非自证）**：在真实 v3 会话上执行一次写入后，
  `@deepseek-ai/dsh-session-format` 的 `readHeader` 返回 `current`（stored 3 → target 3），
  `encodeCurrentEvent` **逐行接受全部 9 个事件**；且写入后目录中只有 v3 文件被改写
  （冻结的 v0 保持逐字节不变）。命名侧与 dsh 的 `parseSessionFormatLogFilename` 逐例一致
  （去掉 `.zstd` 后 0/1/3/12 全等，垃圾名同样拒绝）。
- **测试**：`mcp/tests/test_dsh.py::LogGenerationTest` 6 例（命名解析与往返、最高世代优先、
  缺失时回退到现有世代、读取取活动世代而非冻结 v0、写入保持世代与 header、不产生 v0 旁文件）；
  mcp 套件 146 项、server 套件 189 项通过。

## [2026.09.12.2] - 2026-09-12

> 客户端发布：`CLIENT_VERSION` 2026.09.08.1 → **2026.09.12.2**（客户端包有改动，
> 各端会经 `/api/client/manifest` 自动更新）。服务端无 schema 变更。

### Fixed（dsh 拉取在 Windows 长路径上整体中断 —— 实测影响首次同步）
- **根因**：dsh 的 cwd slug 把每个非 ASCII 字节转义成 `~XXXX`，中文 cwd 很容易把
  `<home>/sessions/--<slug>--/<id>/session.jsonl.zstd` 顶到 MAX_PATH 附近；adapter 的
  「临时文件 + 原子改名」因多出 `.tmp` 后缀正好越界（实测 237 字符目录 + 23 字符临时名
  = **260**，`LongPathsEnabled=0`），`open` 抛 `FileNotFoundError`，异常顺着
  `write_sessions` 冒出去 → **整个 pull 中止**（275 个会话只进来 9 个）。
- **修复**：`mcp/adapters/dsh.py` 新增 `_extended()`（纯函数）/`_lp()`，对 Windows 上
  ≥240 字符的绝对路径使用 `\\?\` 扩展长度形式（无需机器级 LongPathsEnabled 策略；
  UNC 保持原样，因其需 `\\?\UNC\` 形式），应用于写入/原子改名/读取/mkdir/目录搬迁/
  临时文件清理各点。实测：同一环境同一命令，修复后 `imported: 265 / new_messages: 24091`。
- **回归测试**：`mcp/tests/test_dsh.py::LongPathHelperTest`（短路径不变、≥260 加前缀、
  分隔符归一、UNC/已加前缀不动、非 Windows 为普通 abspath）。

### Fixed（dsh 注册片段语法失效，导致会话"同步了但看不见"）
- `server/agents.py` 的 dsh 接入片段原为裸 `- id:` 行；在固定版 dsh 的 loader patch
  语法里那是**覆盖已有条目**，命名不存在的 id 只打警告然后被忽略（实测重启后无任何拉取）。
  已改为 `insert:` 包裹形式（zh/en 两处），并加注说明。

### Known issue（未修，需产品决策）
- 同步下来的会话**默认不出现在 DSH Desktop 会话列表**：会话落盘在共享存储
  `$DSH_HOME/sessions/`（无 profile 层），但列表依赖 dsh 自己的 workspace 索引域
  `storages/workspace.json`，且该域只在**首次初始化**从会话 header 扫描（`initialized: true`
  后重启不重扫，实测 md5 不变）。本次通过手动重建索引验证：1 工作区/3 会话 → 33/256。
- 另：header `cwd` 在本机**不存在**的会话不会被分组显示（dsh 用 `fs.realpath` 归一化失败
  即跳过；实测 20 个 `c:/users/x1` 来源的会话）。

## [2026.09.12.1] - 2026-09-12

> 服务端专用发布：无客户端改动（mcp 版本保持 `2026.09.08.1`）。
> 已部署 236（2026-09-12 16:12 CST，重启前备份
> `backups/pre-multiagent-20260912-161159.tar.gz`），并通过线上校验：部署模块行为
> 15 项 + 两语言词条 6 项全通过；`www.agentctxsync.com/web/register` 已返回
> `pattern="^[A-Za-z0-9][A-Za-z0-9._\-]{0,31}$"` 与中文提示词条；`/health` ok、
> 重启后日志无异常。

### Changed（字段格式校验：注册 / 改密 / 管理端同一套规则，8 个写入口一次收口）
- **用户名 ASCII 白名单**：`USERNAME_RE = ^[A-Za-z0-9][A-Za-z0-9._\-]{0,31}$`
  （首字符须字母/数字；禁空格、`@`、`/`、控制字符与全部非 ASCII）。动机：登录按
  `WHERE username = %s` 逐字比对，放开 Unicode 即可用同形字（西里尔 `аlice`）
  冒充既有账号。
- **存量账户豁免**：用户名写一次即不可改（仅注册 / 管理员建号会写），因此规则上线前
  的账号（含**中文用户名**）登录、展示、同步一切照常，不强制改名、不迁移数据。
- **显示名**：新增控制字符（`\x00-\x1f\x7f`）拒绝——此前可写入换行 / NUL，污染日志、
  导出与响应头；长度 ≤ 64 不变。
- **密码**：新增「不得全为空白」（6 个空格此前可注册成功）；长度 6–128 不变，不引入
  复杂度要求（沿用既有产品决策）。
- **入口（8）**：`/web/register`、`/api/auth/register`、`/web/reset`、
  `/web/change-password`、`/api/me/change-password`、`/web/update-profile`、
  `/web/admin/user/create`、`/web/admin/user/{uid}/edit`。管理员建号此前除「非空 +
  密码 ≥ 6」外零校验；编辑页短密码被静默忽略（现在直接拒绝）。改密 / 重置 / REST
  与注册同口径，堵住「注册合规密码后改成空白」的绕过。
- **邀请码**：新增 ≤ 32 字符形状守卫，在 `FOR UPDATE` 查询前短路；**不加**字符集白名单
  以免误杀历史码。
- **前端镜像**：注册页与管理端用户名输入框的 `pattern` 由服务端 `USERNAME_RE.pattern`
  注入（单一事实源，杜绝漂移），管理端建号 / 编辑表单补 `maxlength`。注意正则里的
  `-` **必须转义**：HTML 的 `pattern` 以 `v` 标志编译，字符类中未转义的 `-` 是语法错误，
  Chromium 会静默忽略整个 pattern（浏览器实测 `checkValidity()` 全 `true` 才暴露）。
- 邮箱沿用 `emailverify.normalize_email`（>254 拒绝 + OWASP 宽松结构），未改动。
- i18n 新增 `username_invalid` / `display_invalid` / `pwd_blank`（zh-CN / en 对齐）。
- 测试：新增 `server/tests/test_field_validation.py`（谓词边界、各写入口拦截行为、
  词条齐全），`test_register.py` 补注册路由用例；server 全量 189 用例通过（本次新增
  19 个：`test_field_validation` 14 + `test_register` 5）。

## [2026.09.11.1] - 2026-09-11

> 服务端专用发布：客户端 mcp 版本保持 `2026.09.08.1` 不变（本次无客户端改动）。
> 已部署 236，并通过线上校验（模板编译、两语言词条、管理员会话拉取用户管理页）。

### Added（用户管理页展示邮箱激活状态）
- `/web/admin/users` 新增「邮箱」列：按 `email_verified_at` / `pending_email` 展示
  已验证 / 等待验证（邮箱修改过渡态时已验证地址与待验证地址并列）/ 未绑定；状态词条
  复用账户页的已验证 · 等待验证 · 未绑定（zh-CN / en）。

### Added（账户安全中心：找回密码 / 修改密码 / 更改邮箱 单入口）
- **找回密码**：登录页「忘记密码？」→ `/web/forgot` 统一响应防枚举；仅当标识命中
  「已验证邮箱」账户才发重置邮件（限速 5/10min/IP）。重置令牌复用
  `user_verification_tokens`（purpose=reset_password，SHA-256 摘要/30min/单次/
  重发作废旧令牌）；`/web/reset` GET 展示表单不消费令牌，POST 事务内校验账户仍持有
  该已验证邮箱后置新密码 + 消费 + 审计。
- **安全信息子菜单**：账户菜单「安全信息」为可折叠二级菜单（标题不可点击），三个子项
  修改密码 / 重置密码 / 更改邮箱 各自弹出对应弹窗（`/web/security?d=dlg*`）：
  - 修改密码：当前密码 + 新密码×2；
  - 重置密码：新端点 `/web/security/reset-request` 向已验证安全邮箱发重置邮件；
  - 更改邮箱：新邮箱 + 当前密码（待验证状态机），成功后向新旧邮箱发变更通知。
- **更改信息弹窗仅改显示名**：密码/管理员标志移出，改密/改邮收敛到安全信息。

### Added（邮箱验证可选特性，配置 SMTP 即启用）
- 新用户注册必填邮箱 → `PENDING_EMAIL_VERIFICATION`（不建工作空间；验证激活时才建
  默认工作空间 + 自动登录）；验证 GET 展示不消费、POST 事务内激活。
- 门禁仅一处：**新建工作空间需已验证邮箱**（ACTIVE/管理员代建豁免）；存量用户、既有
  工作空间、API Key、同步、读取一律不限制；存量账户默认 `LEGACY_UNVERIFIED`，无
  宽限期/时间锁定。
- users 增 email/email_normalized/email_verified_at/pending_email(+norm)/account_state/
  auth_source 列；新增 `user_verification_tokens` 表（哈希摘要、30min、单次）；
  已验证邮箱部分唯一索引（`uq_users_verified_email`）。迁移 idempotent，重启自动生效。
- 登录支持「用户名优先，回落已验证邮箱」；account_state 进入 JWT 与 api 登录响应；
  中间件 `enforce_account_state` 合并强制改密 + 待验证锁页。
- 存量绑定 / 更换 / 重发走 `/web/email`（pending 账户）或 `/web/security`（已验证）；
  绑定/重发按 IP 限速 5/10min；防枚举统一文案。
- 自研 `mailer.py`（纯 smtplib SSL/STARTTLS，零新依赖），发信失败不阻断注册。
- 236 已配置自建 SMTP 发信通道（服务商 / 账号 / 凭据仅存服务器 drop-in 与本机部署资料夹，不入库）。

### Changed（注册加固汇总）
- 进程内按 IP 限速（注册 10/10min、登录 30/10min、验证码 30/5min、邮箱/找回 5/10min）；
  uvicorn `proxy_headers=True`（反代后取真实客户端 IP）。
- PBKDF2 迭代 100k→600k（新哈希），存量登录惰性重哈希升级；密码上限 128、用户名≤32、
  显示名≤64。
- 注册单事务化（邀请 `FOR UPDATE`）、验证码失败表单保留、邀请码自动大写归一；
  api_register 对齐（自动建默认工作空间 + 审计 + 长度校验）。
- 注册页双列紧凑布局、必填字段红色星号。
- SECURITY_AUDIT M2/M5/L6 状态标注；server 全量 166 用例通过。

## [2026.09.08.1] - 2026-09-08

### Fixed（拉取把服务端标题当「本地脏字段」丢弃，DSH Desktop 列表回退显示工作区名）
- **无本地值的字段不再视为本地编辑**：`mcp/server.py::_field_dirty` 此前把
  「本地缺失」（None/空）与 sidecar 锚点值不同判为 dirty——同步写入的会话日志只要
  还没有 `session/title` 事件，拉取就会把载荷里的服务端标题 pop 掉，下一次拉取再次
  pop，会话永久无标题；DSH Desktop 列表对无标题会话回退显示工作区/目录名（实测
  250 个本地会话中 200 个无标题，而服务端 249 个都带真实标题；对副本做全量重拉、
  重写 7598 条消息后标题新增为 0）。现「缺失本地值 = 采纳服务端值」——各 agent 的
  本地存储都表达不了「用户删除了该字段」，None 只是从未写入；push 侧同步收紧为
  None 字段不参与断言（不会反向把服务端值清掉）。
- **拉取后按 sidecar 锚点回填缺失标题**：`mcp/server.py::_reconcile_sidecar_titles`
  在每次成功 pull 后把 field-meta 中服务端已接受的 title 值写回本地日志/存储
  （走 adapter 正常写入路径：dsh 补写 `session/title` 事件并折叠投影缓存文档、列表
  即时显示真实标题；消息集不变 → push 指纹不变 → 不触发重推）。对增量拉取不再
  下发（空闲）的会话也能一次性自愈，无需等待全量重拉。
- 回归：`mcp/tests/test_mcp_server.py` 新增 4 用例（脏语义 2：缺失值非脏/push 不
  断言 None；拉取保标题；sidecar 回填），`mcp/tests/test_dsh.py` 新增无标题日志
  标题-only 写入用例（seq 连续、无消息重复、缓存文档标题更新）。客户端版本 bump
  至 `2026.09.08.1`（`mcp/updater.py` + `server/client_update.py` +
  `mcp/.hermes-sync-version`）。
- **服务端无需改动**：核对 236 生产库（workspace 4），250 个本地会话全部存在于
  服务端、249 个带真实标题，`/pull` 始终返回 `title` + `field_rev`，push 不写 None
  值——标题缺口纯为客户端丢弃所致。

## [2026.09.07.1] - 2026-09-07

### Fixed（dsh 客户端，projcache 无 cwd 文档被 DSH Desktop 2.0.5 拒收）
- **无 cwd 会话不再写投影缓存文档，并清除历史遗留的 null-cwd 文档**：dsh 适配器
  （`mcp/adapters/dsh.py::_refresh_cache_docs`）此前对 `_no-cwd` 兜底会话折叠
  `identity.cwd: null` 的 v5 缓存文档，而 DSH Desktop 2.0.5 的 projcache schema 要求
  `identity.cwd` 为 string——每次桌面启动都把这类文档移入 `.json.bak.*`（视为缺失），
  下一次同步又把它们写回来，形成持续告警噪音（实测 8 个 `_no-cwd` 会话在 21:01/
  21:46/21:52 三次启动各产生一批 `.bak`）。修复：无 cwd 会话（`meta is None` 或
  `meta["cwd"]` 为空）不再写缓存文档，且清理同名的历史陈旧文档；有 cwd 会话的折叠
  行为不变。列表标题回退逻辑不受影响（缓存本就是 fail-soft，缺失时回退到目录名）。
- 回归：`mcp/tests/test_dsh.py` 新增 2 用例（无 cwd 写入不产生 projcache 文档 /
  更新触发的刷新移除陈旧 null-cwd 文档）。
- 客户端版本 bump 至 `2026.09.07.1`（`mcp/updater.py` +
  `server/client_update.py` + `mcp/.hermes-sync-version`）。

### Fixed（dsh 客户端，重写日志 seq 偏移导致 DSH Desktop observe 拒读）
- **整文件重写时 seq 一律从 0 重新编号**：`mcp/adapters/dsh.py::_write_log` 此前在
  追加更新触发重写时从 `existing seq + 1` 续号——整份文件被重新编号到非 0 区间，
  而 dsh 读器（`dsh-session-persistence-jsonl`）要求每个事件的 `seq` 等于其在文件中的
  0 基事件索引（`event.seq !== events.length` 即拒收，最终报
  `complete frame contains a torn JSONL record`）。实测清库全量重拉 256 个会话中
  93 个被二次同步重写过的会话全部落在非 0 区间，逐一 observe 失败；首轮导入
  （seq 0 起）的可正常读取。修复：重写即按行索引从 0 编号，turn/step 展示计数随文件
  重启；多次同步轮次结果稳定。
- 回归：`mcp/tests/test_dsh.py` 新增
  `test_rewrite_renumbers_seq_from_zero`（追加触发重写后 seqs == 0..N-1）。

## [2026.09.06.3] - 2026-09-06

### Changed（Agent 标识色统一 + 辨识度调整）
- **landing 页与系统内部配色对齐**：landing 的 agent pill 取色原为硬编码且两处过期
  （DeepSeek Harness 仍是 codex 时代橙、Oh My Pi 用青），现与 `_macros.html` 单一来源
  完全一致（宏为系统内所有 agent 徽标/胶囊/列表的唯一取色点）。
- **拉开相近色对**：hermes 改品牌紫 600 `#5B45B9`（与 dsh 的 DeepSeek 蓝 `#4D6BFE`
  拉开明度；默认/未知 agent 仍用品牌紫 500 `#6E56CF`）；reasonix 由蓝 `#4285F4` 改洋红
  `#D63384`（脱离蓝族，避免与 dsh 同色系）。调整后 7 色两两最小 ΔE≈22
  （hermes/dsh，双方均为品牌锚点色的上限），其余全部 ≥33。
- 仅服务端模板改动（`_macros.html` / `landing.html`），客户端版本不变。
- **「最新接入」徽标移至 DeepSeek Harness**：landing「主流 Agent 开箱即用」区的
  NEW/最新接入标记由 WorkBuddy 改挂最新接入的 dsh；翻译键改为通用名
  `lp_agent_newest_tag`（`translations.py` + `landing.html`）。

## [2026.09.06.2] - 2026-09-06

### Changed（codex 引擎移除并并入 dsh）
- **移除历史 codex 引擎**：`mcp/adapters/deepseek_harness.py`（更名 `codex.py` 后）与注册键
  `deepseek-harness` 一并删除——codex CLI rollout 存储非本系统支持对象，引擎仅用于旧数据
  兼容的阶段结束。
- **存量数据迁移**：`agent_type='deepseek-harness'` 的历史会话迁移为 `agent_type='dsh'`
  （官方 DeepSeek Harness）；入站遗留 `codex:` 前缀 id 归一目标同步改为 `dsh`。
- 客户端 manifest 随之移除该适配器文件；版本 bump 至 `2026.09.06.2`。
- 文档同步：ARCHITECTURE/ADDING_AGENT/SECURITY_AUDIT/server-deployment 的历史叙述改为
  「引擎已移除、数据并入 dsh」。
- **dsh 进入公开分发**：DSH Desktop 2.0.5 实机验证通过后加入 `PUBLIC_AGENTS`
  （`server/client_update.py`），客户端 zip 分发与帮助页完整接入流程启用（见
  `2026.09.06.1` 的 Fixed 实机验证项）。

## [2026.09.06.1] - 2026-09-06

### Added（dsh 支持）
- **新 Agent：dsh（官方 DeepSeek Harness）**：`mcp/adapters/dsh.py`，读/写
  deepseek-ai/deepseek-harness 的 v0 会话事件日志（`<DSH_HOME 或 ~/.dsh>/sessions/
  --<encoded-cwd>--/<session-id>/session.jsonl[.zstd]`，每会话一目录；`DSH_HOME` 可
  覆盖）。适配器按 `HERMES_SYNC_AGENT=dsh` 选择。
- **读取**：zstd/明文 JSONL 均可（zstd 需要 `zstandard` 包，缺失时跳过并计入跳过数）；
  `session` 头 → `session/title` → canonical；`user/message`/`assistant/message`
  → 会话消息（tool/chunk/compaction 事件非对话文本，跳过）；写入 `seq` 自 0 连续；
  外来会话经 idmap 映射 `session-<uuid>` 本地 id。
- **写入**：新会话目录 + v0 头 + 连续 seq 事件日志，原子替换（temp+rename）；同 id
  会话跨 cwd 漂移时复用既有目录（防重复分裂）；`storages/workspace.json` 尽力而为
  索引（workspace→sessionIds），投影缓存留给 dsh 自身（外来会话列表需重启
  DSH Desktop 后出现）。
- **服务端**：`server/agents.py` 注册 dsh 条目（label/desc/store/register/install，
  面向 `~/.dsh/profiles/<profile>/cordis.patch.yml` 的 dsh-mcp-client 行）；
  workspace 会话胶囊白名单加入 dsh。
- **测试**：`mcp/tests/test_dsh.py` 9 用例（v0 读、外来会话往返、追加/幂等、同 id
  就地更新与跨 cwd 复用、_no-cwd 兜底、slug 编码、status、zstd 条件跳过）；
  mcp 套件 134 项全绿（1 跳过=缺 zstandard）。
- 客户端版本 bump 至 `2026.09.06.1`（`mcp/updater.py` + `server/client_update.py` +
  `mcp/.hermes-sync-version`）。分发与帮助页仍按未验证处理（不加入 `PUBLIC_AGENTS`）。

### Fixed（dsh v0 格式兼容性，经 DSH Desktop 2.0.5 实机启动验证）
- **zstd 逐行帧**：dsh 读器要求**首个 frame 解压后恰为一行 header**；写入改为
  每行一个独立 frame（拼接），与 dsh 自身 append 帧结构兼容（原先整文件单帧会被
  判 corrupt 导致 host-boot 失败）。
- **assistant 消息 `source`**：`kind` 必须为 `model`（校验器拒绝 `user`），按
  `{kind: model, provider, model}` 写入。
- **slug 编码**：`~/.dsh/sessions` 目录名保留尾部连字符（`E:/` → `--E---`），
  与上游 projectKey 一致（原先无条件 `rstrip('-')` 造成目录与 header cwd 身份不匹配）。
- **会话目录随 cwd 迁移**：header cwd 与目录 slug 不一致时（服务端 cwd 编辑/大小写
  漂移）搬迁目录并重写 header；NTFS 大小写不敏感场景改就地改名。
- **workspace/投影缓存域分工**：workspace 域由 dsh 原生 bootstrap 归组
  （fs.realpath 规范路径，外部合成会破坏其不变式——filter/双记账/空列表）；
  投影缓存（session_projcache）文档按桌面自身折叠输出的 v5 全模板由适配器
  在每次日志写入后同步折叠（identity=header createdAt/cwd、title/titleInput/
  sessionListMetadata 等 21 行），使「重拉后单列表即时显示真实标题」而无需
  逐个打开。workspace.json 不写、交由 dsh 首次启动按会话头引导。
- 测试扩至 135 项（含逐行帧/模型 source/slug/搬迁/单归属/缓存标题回归）。

## [2026.09.05] - 2026-09-05

### Fixed（mcp 客户端多实例加固）
- **写工具与后台周期同步共用跨进程锁**：`sync_full`/`sync_pull`/`sync_push`/`project_push`/
  `project_pull`（含 `hermes_sync_*` 别名）执行时统一抢占与后台循环相同的锁文件；等待预算默认
  20s（`HERMES_SYNC_TOOL_LOCK_WAIT_S` 可调），超时返回 busy 提示而非与另一实例并发写库。
- **standby 角色**：启动（+8s）抢锁失败的副本整生命周期只应答工具、不再每周期空转抢锁；
  故障切换依赖宿主重新拉起副本时的新启动竞争（陈旧锁由 `_pid_alive` 自动偷取）。
- **角色日志**：启动打印 `PID`/锁文件/等待预算，8s 后打印 `Primary (pid N)` 或
  `Standby (pid N)`，mcp-stderr 可区分谁在跑后台同步；修复启动早期误报 standby 的时序问题。
- **测试**：新增 12 个锁/角色单测（互斥/陈旧锁偷取/外部持锁 busy/同进程等待/门控），mcp 套件 125 项全绿。
- 客户端版本 bump 至 `2026.09.05.1`（`mcp/updater.py` + `server/client_update.py`）。

## [2026.08.29] - 2026-08-29

### Removed（pi 支持）
- **移除 Agent Pi**（earendil-works/pi）：删除 `mcp/adapters/pi.py` 与其注册，`AGENTS` 注册表、
  agent 过滤白名单、落地页/徽标颜色、README/翻译/文档中的 Pi 与 `pi` 一并移除。
- **保留 Oh My Pi（omp）**：适配器为 `mcp/adapters/omp.py`，测试 `mcp/tests/test_pi.py` 改为
  `mcp/tests/test_omp.py`（针对真实 omp 适配器的读写/幂等/外来 id/状态用例）。

## [2026.08.28.1] - 2026-08-28

### Added（pi / oh-my-pi 支持）
- **新 Agent：Pi 与 Oh My Pi**（`mcp/adapters/pi.py`，一个模块两个注册键）。
  pi（earendil-works/pi）与 omp（can1357/oh-my-pi，fork）共用同一 JSONL v3 会话格式
  （`<agent_dir>/sessions/<encoded-cwd>/<时间戳>_<uuidv7>.jsonl`，cwd 编码与 pi 源码
  `getDefaultSessionDirPath` 逐字一致），适配器按 `HERMES_SYNC_AGENT` 选择
  PiAdapter / OmpAdapter（数据根 `~/.pi/agent` / `~/.omp/agent`，env
  `PI_CODING_AGENT_DIR` / `OMP_CODING_AGENT_DIR` 可覆盖）。
- **读取**：message 条目 → canonical（thinking 块 → `reasoning`、tool 块 → `tool` 角色）；
  omp 扩展（`title` 首记录、`title_change`、header `title`、`model_change.model`）全部兼容；
  `compaction`/`branch_summary` → 摘要 assistant 消息（`meta.pi:entry_type`）；
  同毫秒戳条目确定性 +1ms 消歧（复用 deepseek-harness `_unique_ts` 模式），分支消息按
  文件序线性化。
- **写入**：append-only；header 首行（pi 兼容）+ `parentId` 链式 8-hex 消息 id；
  标题按目标库写入——pi 根 `session_info`、omp 根 `title_change`；
  `(role, 毫秒戳)` 本地去重，服务端保持去重权威；外来会话 owner 注册 +
  `validate_file_id` 路径穿越防护。
- **服务端**：`server/agents.py` 注册 pi/omp 帮助页条目（register/install/uninstall），
  `PUBLIC_AGENTS` 加入分发白名单（帮助页完整流程 + 客户端 zip 下载），客户端版本
  bump 至 `2026.08.28.1`（`server/client_update.py` + `mcp/updater.py`）。
- **帮助页（hermes 接入）**：新增「配置文件方式」说明——可在 `%LOCALAPPDATA%\hermes\config.yaml`
  的 `mcp_servers:` 下直接写 `hermes-sync` 配置块（`<YOUR_API_KEY>` / `HERMES_SYNC_SERVER` 自动注入，
  `HERMES_SYNC_AGENT` 显式声明为 `hermes`）；安装步骤中的代码块改为独立展示、不再占用步骤序号。
- **测试**：`mcp/tests/test_pi.py` 11 用例（pi/omp 双格式读、往返、幂等、追加、外来
  会话、时间戳消歧、compaction、cwd 编码）；本机真实 omp 库冒烟（29 会话 / 6762 消息）。

### Changed（客户端版本）
- 客户端自动更新版本 `2026.08.27.3` → `2026.08.28.1`（含新适配器分发）。
### Fixed（服务端推送计数，本地实测发现）
- **push 新消息统计对大会话少计**：`server/sync.py` 消息批量插入用
  `execute_values(..., page_size=500)`，而 psycopg2 内部始终按 `page_size` 分页、
  游标只暴露**最后一批**的 RETURNING 行——`inserted = set(c.fetchall())` 对超过
  100 条新消息的会话（page_size 缺省即 100）只计入最后一批：`new_messages` 与
  `sync_state.messages_synced` 少计，且重试循环把已入库的早批行误判为重复
  （`duplicates` 虚高）。数据本身无丢失（重试 SELECT 兜底）。修复：手动分页 +
  显式传入一致 `page_size`，逐页 `fetchall` 并集。本地实测：清空一个 1654 消息会话
  后重推，报告精确 1654（修复前 ≤100）；服务端测试 117 例全绿。

## [2026.08.25.2] - 2026-08-25

## [2026.08.25.2] - 2026-08-25

### Changed（客户端，推送水位线）
- **B5 会话指纹跳过（推送侧水位线）**：此前 push 每轮把本地 store **全部会话**重推
  （全池契约 + 服务端幂等去重，无水位线），本机实测每轮约 1.6GB、单会话最大 156MB，
  正是 413/超时的放大器。现在每个会话记录推送指纹
  `(message_count, max_timestamp[, 文件 mtime])`（`mcp/server.py` 新增
  `_session_fingerprint` + `PUSH_FINGERPRINT_PATH` sidecar，与 field-meta 同目录），
  push 循环跳过指纹未变化的会话，仅成功推送后更新指纹（失败不更新、下轮重试）。
  mtime 由 adapter 可选提供（`base.Adapter.session_mtime`，workbuddy 已实现——
  取该会话所有副本的最新 mtime，与 B2 合并读路径一致，pull 触及的副本也会失效指纹）。
  无 field-meta 的 agent 回退为全量推送（原行为）。
  回归：`mcp/tests/test_mcp_server.py::PushFingerprintTest`
  （跳过未变化 / 失败不锚定 / mtime 参与指纹）。
- **客户端版本 bump 至 `2026.08.25.2`**（`mcp/.hermes-sync-version` +
  `server/client_update.py`）。

## [2026.08.25.1] - 2026-08-25

### Fixed（客户端，workbuddy 适配器 + 通用同步）
- **会话 cwd 漂移导致新增消息不再同步（workbuddy 会话分裂事故）**：同一会话的
  jsonl 出现在多个 `projects/<slug>/` 目录时（cwd 被反复改写为其他值：主页目录、另一份
  项目克隆目录），`read_sessions` 只读 `workbuddy.db.sessions.cwd` 指向的那一份，其余副本里
  的新消息永久搁浅。实测某会话第一轮已同步、后续 40 条消息（含最终结论）滞留
  `projects/e-OpenCode-agentctxsync/` 副本 1 小时以上。
  - **B2（读路径）**：`read_sessions` 现在收集同 id 会话在所有项目目录的副本，
    按 `(role, timestamp)` 去重合并、按时间排序；行 cwd 指向的文件优先（冲突时内容以它为准）。
  - **B1（写路径）**：`_upsert_session` 对**本机自有**会话（`agent_type` 为 workbuddy/空）
    的已存在行不再用拉取值覆写 `cwd`（外来会话保持原行为：拉取路径即其存储位置）。
    此前一次 peer 值（或应用侧改写）经「push 上报 → 服务端接受 → pull 回写」回路固化，
    把读路径指向陈旧副本。回归：`mcp/tests/test_workbuddy.py`
    （`test_upsert_preserves_local_cwd` / `test_upsert_moves_foreign_session_cwd` /
    `test_read_merges_split_session_copies`）。
- **巨型 push 分块 413 中止整个推送循环**：`_chunk_sessions` 只按会话数/消息数分块，
  不按字节大小；本机 store 中单会话可达 100MB+（实测 156.7MB / 8.5 万消息），一个 chunk
  超过 nginx `client_max_body_size 100m` 被拒（413），`push_sessions` 随即中止，
  排在巨型会话之后的会话整轮推不出去（每 5 分钟周期重复失败）。
  - **B4**：`_chunk_sessions` 增加 `max_bytes`（默认 8MB）字节上限；单会话超限时单独成块。
  - **循环容错**：单个 chunk 失败（413/配额/超时）不再中止剩余会话，记录错误并继续，
    结束时汇总返回。回归：`mcp/tests/test_mcp_server.py`
    （`test_bytes_bound_splits_big_session_out` / `test_single_huge_session_rides_alone`）。
- **客户端版本 bump 至 `2026.08.25.1`**（`mcp/.hermes-sync-version` +
  `server/client_update.py`）；nginx 部署侧 `client_max_body_size` 100m → 300m。

## [2026.08.24.1] - 2026-08-24

### Fixed
- **服务端项目「关联会话」大小写不敏感匹配（S1 修复）**：Windows 盘符/路径大小写不敏感
  （`D:` == `d:`），但 `server/workspace.py` 的会话↔项目关联用大小写敏感的 SQL `=`/`LIKE`，
  导致 `cwd` 盘符大小写与会话项目 folder 不一致的会话在 Web 项目卡片下漏显示「暂无关联会话」
  （实测某项目下 7 个会话被隐藏）。改为 `LOWER(cwd)` 大小写不敏感 + `LEFT(...)=prefix`
  无 LIKE 通配符的前缀匹配，与客户端 `_path_key` 的 Windows 大小写折叠一致。
- **字段级合并 bootstrap 死锁修复**：`base=None`（客户端未锚定基准）此前被服务端一律按
  「服务器权威」拒绝，导致 `field_rev[f]` 永远不被种成非 0，客户端永远无法锚定 base，
  每次推送都 base=None 且被拒——**任何字段在新协议下都无法写入**（实测 266 个会话
  `field_rev={}` 卡住，会话换项目/改标题/改 pinned 等元数据改动全部推不上去）。修复：
  当服务端该字段从未有过新协议版本（`field_rev[f]==0`，迁移基线）时，`base=None` 作为
  首次种子接受并分配版本**；仅当已有版本（`field_rev[f]>0`）才拒绝过期写入。纯服务端
  修复（`server/sync.py` + `server/projects.py`），客户端无需改（推后拉自然锚定）。
- **项目卡片「查看全部会话」按项目过滤而非标题搜索**：此前跳
  `/web/workspace/{id}?q={项目名}`，但 `q` 过滤的是会话 title/id，项目名搜不到任何会话。
  改为新增 `project=<项目id>` 参数：服务端按该项目 `project_folders` 的所有路径做
  `cwd` 大小写不敏感前缀过滤，页面显示「仅项目：<名称>」胶片可清除，title 搜索独立可用。
- S1/死锁/项目过滤为纯服务端改动；本版同时含 opencode 客户端改动，**客户端版本 bump 至
  `2026.08.24.1`**。回归：`server/tests/test_workspace.py::ProjectSessionMatchTest` +
  `server/tests/test_sync.py`（field_merge_none_base_seeds_unversioned /
  project_field_merge_none_base_seeds_unversioned）

### Changed（客户端，opencode 适配器）
- **opencode 适配器改写为读 SQLite `opencode.db`**：原适配器读旧开源版 JSON 布局
  （`storage/session/info/*.json`），而 opencode CLI/桌面版 1.x 共用 `opencode.db`
  （`session`/`message`/`part` 表），导致适配器对真实会话（实测 52 会话/1838 消息）完全不可见。
  新适配器 `discover()` 定位 `opencode.db`、读三表→canonical（text/reasoning/tool part 映射、
  ms→s、tool 引用折叠），写入按桌面版行格式 INSERT（id 前缀 `ses_/msg_/prt_`、`project_id`
  按目录解析到对应项目或回退 `'global'`、唯一 slug、`version` 头、ms 时间戳、`model` 列写
  `{id, providerID}` 合法 JSON）；外来会话经 idmap 生成 `ses_` id 往返、dedupe 稳定。
- **支持范围：仅 OpenCode CLI，桌面版暂不支持**（桌面 UI 无法可靠渲染外部写入的会话 &
  按项目分桶显示）。服务端 `agents.py` 帮助页 opencode 部分已注明「仅支持 CLI」。
  客户端改动，随下次客户端版本 bump 分发。回归：
  `mcp/tests/test_opencode.py` + `mcp/tests/test_cross_agent.py`（已按新存储改写）

## [2026.08.23.1] - 2026-08-23

### Added
- **字段级乐观并发（多端冲突安全同步）**：会话 user-edit 字段（`cwd`/`git_branch`/`git_repo_root`/`title`/`pinned`/`archived`/`display_name`）纳入字段级合并，确定收敛、跨设备不丢数据。详见 `docs/ARCHITECTURE.md`「字段级乐观并发 + 惰性 bootstrap」决策记录。

### Changed
- **服务端**：`sessions` 新增 `rev`（会话级逻辑版本，默认 0）+ `field_rev`（JSONB 每字段版本，默认 `{}`，基线 0，无需重建历史）。`/push` 按字段合并：`base=None`（未知基准）→ 服务器权威一次不写；已知 base 的脏字段 → 接受并递增；新会话播种 `rev=1`。`/pull` 返回每字段 `field_rev`（JSONB 规范化为 dict）。push 响应新增 `session_revs` 供客户端即时锚定基准。旧客户端（无 `field_meta`）整会话回退既有全量覆盖语义，混合版本窗口短暂。
- **服务端（Phase 2）**：`projects` 同样新增 `rev` + `field_rev`；`/api/projects/push` 对项目标量字段（`name`/`primary_path`/`archived`/`description`）做同一字段级合并（`base=None` 服务器权威、已知 base 接受递增、新项目播种 `rev=1`），响应带 `project_revs`；`/api/projects/pull` 返回每项目 `field_rev`。folders 保持路径并集（跨设备增量共存），不入字段版本。
- **客户端**：新增字段级 sidecar（`.hermes-sync-<agent>-field-meta.json` + `.hermes-sync-<agent>-projects-field-meta.json`，惰性填充、无需强制全量 pull）。push 仅推「脏或首次接触」的 user-edit 字段并带 base，其余剔除（绝不覆盖对端）；pull 跳过本地脏字段、锚定被采纳字段的 base/val。`full_sync` 与启动路径统一改为 **pull → push**（先锚定基准再推脏字段，本地改动不被回滚、对端改动不被冲掉）。

### Fixed
- **会话换项目后重启被回退**：把会话从 A 项目移到 B 项目（项目归属编码为 `cwd`）后立刻重启 hermes，先拉后推会拿服务器旧 `cwd` 把本地移动抹掉。字段级合并下：本地移动是脏字段 → pull 跳过、push 带走，跨设备各自保留有意改动。（此前「启动 push→pull」方案已被本设计取代。）
- **server / mcp 消息层不变**：追加 + 三元组去重已跨端安全，pull 沿用 hidden 过滤、push 不复活隐藏消息。
- 客户端版本 bump 至 `2026.08.23.1` 触发自动更新（需配套部署服务端 schema 迁移）

## [2026.08.22.8] - 2026-08-22

### Changed
- **帮助页未公开发布 Agent 只显示「适配中」**：选中 deepseek-harness / opencode / openclaw 时，安装卡片只显示「适配中」徽标，② 工作空间 API Key、③ 启动验证与常见问题两卡隐藏（hermes / workbuddy / reasonix 完整流程不变）
- **渲染页 `Cache-Control: no-store`**：动态 HTML 不再被浏览器/代理启发式缓存，页面文案变更即时可见
- **显示名统一**：会话列表/筛选胶囊、landing 与翻译文案中的 `Codex` → `DeepSeek Harness`、`opencode` → `OpenCode`
- **文档修订**：README / CONTRIBUTING / ADDING_AGENT / ARCHITECTURE / SECURITY_AUDIT / server-deployment 中 agent 指代统一为 DeepSeek Harness（存储路径 `~/.codex`、legacy `codex:` 前缀、CHANGELOG 历史记录保留）
- 纯服务端/文档改动，客户端无需更新（版本号保持 `2026.08.22.7`）

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
