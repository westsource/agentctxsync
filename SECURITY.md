# 安全政策 / Security Policy

## 支持的版本 / Supported Versions

本项目采用日期版本号（`YYYY.MM.DD.N`）。只有**最新版本**会得到安全修复：

| 版本 | 支持状态 |
|------|---------|
| 最新版本（latest） | ✅ 积极支持 |
| 旧版本 | ❌ 不再维护，请升级 |

## 报告漏洞 / Reporting a Vulnerability

我们认真对待安全问题。**请勿**在公开渠道（GitHub Issues、讨论区、社交平台）披露漏洞细节。

### 上报渠道

| 方式 | 地址 |
|------|------|
| **首选：GitHub Security Advisory（私有上报）** | https://github.com/westsource/agentctxsync/security/advisories/new |
| 备选：邮件 | 见仓库维护者主页 |

### 上报内容建议

请尽量提供以下信息，帮助我们快速定位与复现：

- 影响组件（server / mcp client / 某个 adapter / web UI）
- 漏洞类型与严重程度评估
- 复现步骤（或最小 PoC）
- 受影响的版本号
- 你建议的修复方式（如有）

### 响应承诺

- **48 小时内**：确认收到上报，并给出初步评估
- **7 天内**：发布修复方案或缓解措施说明
- 修复完成后我们会公开致谢（如上报者同意署名）

## 安全注意事项 / Security Notes

- **服务端密钥**：`HERMES_SYNC_PG_DSN`、`HERMES_SYNC_MASTER_KEY`、`HERMES_SYNC_JWT_SECRET`
  必须通过环境变量提供（见 `server/.env.example`），**严禁**硬编码或提交到仓库。
- **Workspace API Key**（`ws_xxx`）是同步接口的唯一凭证，泄露后请在 Web UI
  中"重新生成 Key"立即轮换。
- 服务端默认暴露在 `:8765`，请务必置于防火墙 / 反向代理之后，并启用 HTTPS。
- 本项目为自托管工具，数据（会话/消息）包含你的私有 AI 对话，请妥善保护数据库备份。

## 已知安全设计

- 密码使用 PBKDF2-SHA256（100,000 轮迭代）存储
- JWT 签名使用 HMAC-SHA256，过期时间可配置
- 会话消息渲染对 HTML 转义（XSS 防护）
- 远程会话 id 经过路径穿越校验后才写入本地存储
- 客户端自动更新对每个文件做 SHA256 校验（防篡改/截断）
