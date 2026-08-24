"""
Server-side agent registry.

Drives the help page and the /web/download/mcp-client archive generation:
adding a new agent here (plus its client adapter in mcp/adapters/) is all
that is needed -- no endpoint or template changes required.

Each entry:
    label       display name
    desc        one-line description (zh/en)
    store       where the agent keeps its local session store
    register    how to register the MCP client (zh/en); placeholders
                <PYTHON>, <EXTRACT_DIR>, <KEY>, <SERVER> are substituted
                at README build time (KEY and SERVER always; PYTHON and
                EXTRACT_DIR are user-provided)
    verify      how to test the registration
    env_agent   whether the agent needs HERMES_SYNC_AGENT set (all new
                agents yes; hermes defaults to it so it is optional)
"""

AGENTS = {
    "hermes": {
        "label": "Hermes",
        "desc": {
            "zh": "Hermes 桌面版 AI Agent，通过 MCP stdio 接入（默认，无需 HERMES_SYNC_AGENT）。",
            "en": "Hermes desktop AI agent, connected via MCP stdio (default, no HERMES_SYNC_AGENT needed).",
        },
        "store": {
            "zh": "本地存储：%LOCALAPPDATA%\\hermes\\state.db",
            "en": "Local store: %LOCALAPPDATA%\\hermes\\state.db",
        },
        "register": {
            "zh": (
                'hermes mcp add hermes-sync --command "<PYTHON>" '
                '--env HERMES_SYNC_API_KEY=<KEY> --args "<EXTRACT_DIR>/mcp/server.py"'
            ),
            "en": (
                'hermes mcp add hermes-sync --command "<PYTHON>" '
                '--env HERMES_SYNC_API_KEY=<KEY> --args "<EXTRACT_DIR>/mcp/server.py"'
            ),
        },
        "verify": "hermes mcp test hermes-sync",
        "env_agent": False,
        "uninstall": {
            "zh": "hermes mcp remove hermes-sync",
            "en": "hermes mcp remove hermes-sync",
        },
        "install": {
            "zh": [
                {"text": "将压缩包解压到任意目录，例如 <code>C:\\hermes-sync-mcp</code>（解压后 <code>server.py</code> 位于 <code>&lt;EXTRACT_DIR&gt;/mcp/</code> 下）。"},
                {"text": "找到 Hermes 的 Python 解释器（Windows 下通常为）："},
                {"code": "C:\\Users\\<用户名>\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe"},
                {"text": "在终端中注册 MCP Server（将 <code>&lt;PYTHON&gt;</code> 替换为第 2 步路径，<code>&lt;EXTRACT_DIR&gt;</code> 替换为第 1 步目录）："},
                {"code": "<REGISTER>"},
                {"text": "提示 <code>Enable all 4 tools? [Y/n/select]:</code> 时输入 <code>Y</code> 并回车。"},
                {"text": "重启 Hermes（新会话生效），MCP 工具在启动时自动加载。"},
            ],
            "en": [
                {"text": "Unzip the archive to a folder, e.g. <code>C:\\hermes-sync-mcp</code> (after unzipping, <code>server.py</code> lives under <code>&lt;EXTRACT_DIR&gt;/mcp/</code>)."},
                {"text": "Find the Hermes Python interpreter (Windows usually):"},
                {"code": "C:\\Users\\<you>\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe"},
                {"text": "Register the MCP server in a terminal (replace <code>&lt;PYTHON&gt;</code> with the path from step 2 and <code>&lt;EXTRACT_DIR&gt;</code> with the folder from step 1):"},
                {"code": "<REGISTER>"},
                {"text": "When prompted <code>Enable all 4 tools? [Y/n/select]:</code>, type <code>Y</code> and press Enter."},
                {"text": "Restart Hermes (start a new session) - MCP tools load at startup."},
            ],
        },
    },
    "workbuddy": {
        "label": "WorkBuddy",
        "desc": {
            "zh": "腾讯 WorkBuddy 桌面智能体（5.3.x），会话为 ~/.workbuddy-ai（或旧版 ~/.workbuddy）/projects/<slug>/*.jsonl + workbuddy.db。",
            "en": "Tencent WorkBuddy desktop agent (5.3.x); sessions in ~/.workbuddy/projects/<slug>/*.jsonl + workbuddy.db.",
        },
        "store": {
            "zh": "本地存储：~/.workbuddy-ai（或旧版 ~/.workbuddy）/projects/<路径slug>/<conversationId>.jsonl（消息）+ ~/.workbuddy-ai/workbuddy.db（元数据）",
            "en": "Local store: ~/.workbuddy-ai (or legacy ~/.workbuddy)/projects/<slug>/<conversationId>.jsonl (messages) + ~/.workbuddy-ai/workbuddy.db (metadata)",
        },
        "register": {
            "zh": (
                '{\n'
                '  "mcpServers": {\n'
                '    "hermes-sync": {\n'
                '      "command": "<PYTHON>",\n'
                '      "args": ["<EXTRACT_DIR>/mcp/server.py"],\n'
                '      "env": {\n'
                '        "HERMES_SYNC_AGENT": "workbuddy",\n'
                '        "HERMES_SYNC_API_KEY": "<KEY>",\n'
                '        "HERMES_SYNC_SERVER": "<SERVER>"\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}'
            ),
            "en": (
                '{\n'
                '  "mcpServers": {\n'
                '    "hermes-sync": {\n'
                '      "command": "<PYTHON>",\n'
                '      "args": ["<EXTRACT_DIR>/mcp/server.py"],\n'
                '      "env": {\n'
                '        "HERMES_SYNC_AGENT": "workbuddy",\n'
                '        "HERMES_SYNC_API_KEY": "<KEY>",\n'
                '        "HERMES_SYNC_SERVER": "<SERVER>"\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}'
            ),
        },
        "verify": "WorkBuddy 会话中调用 hermes_sync_status（注意：写入的会话需重启 WorkBuddy 才会出现在列表）",
        "env_agent": True,
        "uninstall": {
            "zh": (
                "# 移除：删除 WorkBuddy MCP 设置中的 hermes-sync server 配置块"
            ),
            "en": (
                "# Remove: delete the hermes-sync server block in WorkBuddy's MCP settings"
            ),
        },
        "install": {
            "zh": [
                {"text": "将压缩包解压到任意目录，例如 <code>C:\\hermes-sync-mcp</code>（解压后 <code>server.py</code> 位于 <code>&lt;EXTRACT_DIR&gt;/mcp/</code> 下）。"},
                {"text": "打开 WorkBuddy，进入「专家 · 技能 · 连接器」，点击「添加自定义连接器」，新建一个自定义 MCP server（类型选 <b>stdio</b>）。"},
                {"text": "在连接器的 <code>mcpServers</code> 配置中加入 <code>hermes-sync</code>，完整配置示例如下（<code>&lt;PYTHON&gt;</code> 替换为 Python 3.10+ 解释器路径，<code>&lt;EXTRACT_DIR&gt;</code> 替换为第 1 步解压目录，<code>&lt;KEY&gt;</code> 替换为下方工作空间 API Key）："},
                {"code": "<REGISTER>"},
                {"text": "配置 MCP server 后，首次连接时需要信任（WorkBuddy 会提示确认，选择信任该 server 后即可正常调用工具）。"},
                {"text": "重启 WorkBuddy（新会话生效）。注意：适配器写入的会话需要重启 WorkBuddy 后才会出现在会话列表（启动时 MIGRATE 扫描识别）。"},
            ],
            "en": [
                {"text": "Unzip the archive to a folder, e.g. <code>C:\\hermes-sync-mcp</code> (after unzipping, <code>server.py</code> lives under <code>&lt;EXTRACT_DIR&gt;/mcp/</code>)."},
                {"text": "Open WorkBuddy → \"Experts · Skills · Connectors\", click \"Add Custom Connector\" to create a custom MCP server (type: <b>stdio</b>)."},
                {"text": "Add <code>hermes-sync</code> under <code>mcpServers</code> in the connector config. Full example below (replace <code>&lt;PYTHON&gt;</code> with a Python 3.10+ interpreter, <code>&lt;EXTRACT_DIR&gt;</code> with the folder from step 1, <code>&lt;KEY&gt;</code> with the workspace API key below):"},
                {"code": "<REGISTER>"},
                {"text": "After configuring the MCP server, the first connection requires trust (WorkBuddy prompts for confirmation; choose to trust the server so tools can be called normally)."},
                {"text": "Restart WorkBuddy (new sessions pick it up). Note: sessions written by the adapter only appear in the list after WorkBuddy restarts (MIGRATE scan on startup)."},
            ],
        },
    },
    "deepseek-harness": {
        "label": "DeepSeek Harness",
        "desc": {
            "zh": "DeepSeek Harness（codex rollout 格式），会话为 ~/.codex/sessions/*.jsonl。",
            "en": "DeepSeek Harness (codex rollout format); sessions live in ~/.codex/sessions/*.jsonl.",
        },
        "store": {
            "zh": "本地存储：~/.codex/sessions/（rollout-<时间戳>-<uuid>.jsonl）",
            "en": "Local store: ~/.codex/sessions/ (rollout-<timestamp>-<uuid>.jsonl)",
        },
        "register": {
            "zh": (
                "# 编辑 ~/.codex/config.toml（或运行 codex mcp add，若你的版本支持）\n"
                "[mcp_servers.hermes-sync]\n"
                'command = "<PYTHON>"\n'
                'args = ["<EXTRACT_DIR>/mcp/server.py"]\n'
                'env = { HERMES_SYNC_AGENT = "deepseek-harness", HERMES_SYNC_API_KEY = "<KEY>", HERMES_SYNC_SERVER = "<SERVER>" }'
            ),
            "en": (
                "# Edit ~/.codex/config.toml (or use codex mcp add if your version supports it)\n"
                "[mcp_servers.hermes-sync]\n"
                'command = "<PYTHON>"\n'
                'args = ["<EXTRACT_DIR>/mcp/server.py"]\n'
                'env = { HERMES_SYNC_AGENT = "deepseek-harness", HERMES_SYNC_API_KEY = "<KEY>", HERMES_SYNC_SERVER = "<SERVER>" }'
            ),
        },
        "verify": "codex --version && 在 harness 会话中调用 hermes_sync_status",
        "env_agent": True,
        "uninstall": {
            "zh": (
                "# 移除：删除 ~/.codex/config.toml 中的 [mcp_servers.hermes-sync] 配置块"
            # 若用 codex mcp remove hermes-sync 注册，可运行 codex mcp remove hermes-sync
            ),
            "en": (
                "# Remove: delete the [mcp_servers.hermes-sync] block in ~/.codex/config.toml"
            # or run codex mcp remove hermes-sync if you registered via it
            ),
        },
        "install": {
            "zh": [
                {"text": "将压缩包解压到任意目录，例如 <code>C:\\hermes-sync-mcp</code>（解压后 <code>server.py</code> 位于 <code>&lt;EXTRACT_DIR&gt;/mcp/</code> 下）。"},
                {"text": "确认已安装 DeepSeek Harness（<code>codex --version</code>，若基于 codex CLI）。"},
                {"text": "编辑 <code>~/.codex/config.toml</code>，添加 <code>[mcp_servers.hermes-sync]</code> 配置（<code>&lt;PYTHON&gt;</code> 替换为 Python 3.10+ 解释器路径，<code>&lt;EXTRACT_DIR&gt;</code> 替换为第 1 步目录）："},
                {"code": "<REGISTER>"},
                {"text": "重启 harness（新会话生效）。"},
            ],
            "en": [
                {"text": "Unzip the archive to a folder, e.g. <code>C:\\hermes-sync-mcp</code> (after unzipping, <code>server.py</code> lives under <code>&lt;EXTRACT_DIR&gt;/mcp/</code>)."},
                {"text": "Make sure DeepSeek Harness is installed (<code>codex --version</code> if it is built on the codex CLI)."},
                {"text": "Edit <code>~/.codex/config.toml</code> and add <code>[mcp_servers.hermes-sync]</code> (replace <code>&lt;PYTHON&gt;</code> with a Python 3.10+ interpreter and <code>&lt;EXTRACT_DIR&gt;</code> with the folder from step 1):"},
                {"code": "<REGISTER>"},
                {"text": "Restart the harness (new sessions pick it up)."},
            ],
        },
    },
    "opencode": {
        "label": "OpenCode",
        "desc": {
            "zh": "仅支持 **OpenCode CLI**（桌面版暂不支持）；会话存于共享 SQLite opencode.db。",
            "en": "OpenCode **CLI only** (desktop app not supported); sessions stored in the shared opencode.db SQLite.",
        },
        "store": {
            "zh": "本地存储：$XDG_DATA_HOME/opencode/opencode.db（session/message/part 表；仅支持 CLI，桌面版不支持）",
            "en": "Local store: $XDG_DATA_HOME/opencode/opencode.db (session/message/part tables; CLI-only, desktop not supported)",
        },
        "register": {
            "zh": (
                "# 编辑 ~/.config/opencode/opencode.json（或项目根 opencode.json）\n"
                '{\n'
                '  "mcp": {\n'
                '    "hermes-sync": {\n'
                '      "type": "local",\n'
                '      "command": ["<PYTHON>", "<EXTRACT_DIR>/mcp/server.py"],\n'
                '      "enabled": true,\n'
                '      "environment": {\n'
                '        "HERMES_SYNC_AGENT": "opencode",\n'
                '        "HERMES_SYNC_API_KEY": "<KEY>",\n'
                '        "HERMES_SYNC_SERVER": "<SERVER>"\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}'
            ),
            "en": (
                "# Edit ~/.config/opencode/opencode.json (or project opencode.json)\n"
                '{\n'
                '  "mcp": {\n'
                '    "hermes-sync": {\n'
                '      "type": "local",\n'
                '      "command": ["<PYTHON>", "<EXTRACT_DIR>/mcp/server.py"],\n'
                '      "enabled": true,\n'
                '      "environment": {\n'
                '        "HERMES_SYNC_AGENT": "opencode",\n'
                '        "HERMES_SYNC_API_KEY": "<KEY>",\n'
                '        "HERMES_SYNC_SERVER": "<SERVER>"\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}'
            ),
        },
        "verify": "opencode CLI 启动后 /mcp 命令查看 hermes-sync 状态（仅支持 CLI，不支持桌面版）",
        "env_agent": True,
        "uninstall": {
            "zh": (
                "# 移除：删除 opencode.json 中 mcp 下的 hermes-sync 配置块（~/.config/opencode/opencode.json 或项目根 opencode.json）"
            ),
            "en": (
                "# Remove: delete the hermes-sync block under mcp in opencode.json (~/.config/opencode/opencode.json or project-root opencode.json)"
            ),
        },
        "install": {
            "zh": [
                {"text": "注：仅支持 OpenCode CLI，OpenCode 桌面版暂不支持。将压缩包解压到任意目录，例如 <code>C:\\hermes-sync-mcp</code>（解压后 <code>server.py</code> 位于 <code>&lt;EXTRACT_DIR&gt;/mcp/</code> 下）。"},
                {"text": "编辑 <code>~/.config/opencode/opencode.json</code>（或项目根目录 <code>opencode.json</code>），在 <code>mcp</code> 字段添加 hermes-sync（<code>&lt;PYTHON&gt;</code> 替换为 Python 3.10+ 解释器路径，<code>&lt;EXTRACT_DIR&gt;</code> 替换为第 1 步目录）："},
                {"code": "<REGISTER>"},
                {"text": "重启 opencode（CLI）。"},
            ],
            "en": [
                {"text": "Note: OpenCode CLI only; the desktop app is not supported. Unzip the archive to a folder, e.g. <code>C:\\hermes-sync-mcp</code> (after unzipping, <code>server.py</code> lives under <code>&lt;EXTRACT_DIR&gt;/mcp/</code>)."},
                {"text": "Edit <code>~/.config/opencode/opencode.json</code> (or the project-root <code>opencode.json</code>) and add hermes-sync under <code>mcp</code> (replace <code>&lt;PYTHON&gt;</code> with a Python 3.10+ interpreter and <code>&lt;EXTRACT_DIR&gt;</code> with the folder from step 1):"},
                {"code": "<REGISTER>"},
                {"text": "Restart opencode (CLI)."},
            ],
        },
    },
    "reasonix": {
        "label": "Reasonix",
        "desc": {
            "zh": "Reasonix（DeepSeek-Reasonix）终端 Agent，会话为 %APPDATA%\\reasonix\\sessions\\*.jsonl。",
            "en": "Reasonix (DeepSeek-Reasonix) terminal agent; sessions in %APPDATA%\\reasonix\\sessions\\*.jsonl.",
        },
        "store": {
            "zh": "本地存储：%APPDATA%\\reasonix\\sessions\\（<id>.jsonl + 事件日志）",
            "en": "Local store: %APPDATA%\\reasonix\\sessions\\ (<id>.jsonl + event log)",
        },
        "register": {
            "zh": (
                "# Reasonix 桌面版：设置 → MCP与工具 → 添加服务器 → JSON 中粘贴（实测有效）\n"
                "# 注意：env 三要素缺一不可——缺 HERMES_SYNC_AGENT 会退回 hermes 适配器\n"
                "# 扫错目录，缺 HERMES_SYNC_API_KEY 会认证失败（401）；AUTO_UPDATE=0 避免\n"
                "# reasonix 包未公开发布导致的更新检查报错。CLI 等价写法：config.toml 中\n"
                "# [[plugins]] 块（name/type/command/args/env/auto_start 同字段）\n"
                "{\n"
                '  "hermes-sync": {\n'
                '    "type": "stdio",\n'
                '    "command": "<PYTHON>",\n'
                '    "args": ["<EXTRACT_DIR>/mcp/server.py"],\n'
                '    "env": {\n'
                '      "HERMES_SYNC_AGENT": "reasonix",\n'
                '      "HERMES_SYNC_SERVER": "<SERVER>",\n'
                '      "HERMES_SYNC_API_KEY": "<KEY>",\n'
                '      "HERMES_SYNC_AUTO_UPDATE": "0"\n'
                '    },\n'
                '    "auto_start": true\n'
                '  }\n'
                "}"
            ),
            "en": (
                "# Reasonix desktop: Settings → MCP & Tools → Add Server → JSON, paste (verified)\n"
                "# All three env vars are REQUIRED: without HERMES_SYNC_AGENT the client\n"
                "# falls back to the hermes adapter (wrong store), without\n"
                "# HERMES_SYNC_API_KEY every call fails auth (401); AUTO_UPDATE=0 avoids\n"
                "# update-check errors while the reasonix package is not publicly\n"
                "# released. CLI equivalent: a [[plugins]] block in config.toml\n"
                "# (same name/type/command/args/env/auto_start fields)\n"
                "{\n"
                '  "hermes-sync": {\n'
                '    "type": "stdio",\n'
                '    "command": "<PYTHON>",\n'
                '    "args": ["<EXTRACT_DIR>/mcp/server.py"],\n'
                '    "env": {\n'
                '      "HERMES_SYNC_AGENT": "reasonix",\n'
                '      "HERMES_SYNC_SERVER": "<SERVER>",\n'
                '      "HERMES_SYNC_API_KEY": "<KEY>",\n'
                '      "HERMES_SYNC_AUTO_UPDATE": "0"\n'
                '    },\n'
                '    "auto_start": true\n'
                '  }\n'
                "}"
            ),
        },
        "verify": "reasonix 会话中调用 hermes_sync_status",
        "env_agent": True,
        "uninstall": {
            "zh": (
                "# 移除：在 Reasonix 设置 → MCP与工具 中删除 hermes-sync 服务器条目；\n"
                "# 若 config.toml 中有 [[plugins]] 块一并删除"
            ),
            "en": (
                "# Remove: delete the hermes-sync server entry in Reasonix Settings → MCP & Tools;\n"
                "# also remove the [[plugins]] block from config.toml if present"
            ),
        },
        "install": {
            "zh": [
                {"text": "将压缩包解压到任意目录，例如 <code>C:\\hermes-sync-mcp</code>（解压后 <code>server.py</code> 位于 <code>&lt;EXTRACT_DIR&gt;/mcp/</code> 下）。"},
                {"text": "在 Reasonix 设置 → MCP与工具 → 添加服务器 → JSON 中粘贴以下配置（<code>&lt;PYTHON&gt;</code> 替换为 Python 3.10+ 解释器路径，<code>&lt;EXTRACT_DIR&gt;</code> 替换为解压目录）："},
                {"code": "<REGISTER>"},
                {"text": "重启 Reasonix——插件随桌面自动启动（auto_start），启动后约 8 秒执行增量拉取，之后每 300 秒自动双向同步。"},
                {"text": "若重启后不自动同步：检查插件配置的 env 三要素（HERMES_SYNC_AGENT / HERMES_SYNC_SERVER / HERMES_SYNC_API_KEY）是否完整——缺任一项都会静默失败。"},
            ],
            "en": [
                {"text": "Unzip the archive to a folder, e.g. <code>C:\\hermes-sync-mcp</code> (after unzipping, <code>server.py</code> lives under <code>&lt;EXTRACT_DIR&gt;/mcp/</code>)."},
                {"text": "In Reasonix Settings → MCP & Tools → Add Server → JSON, paste the config below (replace <code>&lt;PYTHON&gt;</code> with a Python 3.10+ interpreter and <code>&lt;EXTRACT_DIR&gt;</code> with the unzip folder):"},
                {"code": "<REGISTER>"},
                {"text": "Restart Reasonix — the plugin auto-starts with the desktop (auto_start), runs an incremental pull ~8s after startup, then syncs both ways every 300s."},
                {"text": "If it does not auto-sync after restart: make sure all three env vars (HERMES_SYNC_AGENT / HERMES_SYNC_SERVER / HERMES_SYNC_API_KEY) are present — any missing one fails silently."},
            ],
        },
    },
    "openclaw": {
        "label": "OpenClaw",
        "desc": {
            "zh": "OpenClaw 个人 AI 助手（openclaw.ai），会话存于 ~/.openclaw/agents/<id>/。",
            "en": "OpenClaw personal AI assistant (openclaw.ai); sessions under ~/.openclaw/agents/<id>/.",
        },
        "store": {
            "zh": "本地存储：~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite",
            "en": "Local store: ~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite",
        },
        "register": {
            "zh": (
                "# 在 openclaw 配置的 mcp.servers 中注册（stdio；字段以官方文档 docs.openclaw.ai/cli/mcp 为准）\n"
                '{\n'
                '  "mcp": {\n'
                '    "servers": {\n'
                '      "hermes-sync": {\n'
                '        "command": "<PYTHON>",\n'
                '        "args": ["<EXTRACT_DIR>/mcp/server.py"],\n'
                '        "env": {\n'
                '          "HERMES_SYNC_AGENT": "openclaw",\n'
                '          "HERMES_SYNC_API_KEY": "<KEY>",\n'
                '          "HERMES_SYNC_SERVER": "<SERVER>"\n'
                '        }\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}'
            ),
            "en": (
                "# Register under mcp.servers in the openclaw config (stdio; fields per docs.openclaw.ai/cli/mcp)\n"
                '{\n'
                '  "mcp": {\n'
                '    "servers": {\n'
                '      "hermes-sync": {\n'
                '        "command": "<PYTHON>",\n'
                '        "args": ["<EXTRACT_DIR>/mcp/server.py"],\n'
                '        "env": {\n'
                '          "HERMES_SYNC_AGENT": "openclaw",\n'
                '          "HERMES_SYNC_API_KEY": "<KEY>",\n'
                '          "HERMES_SYNC_SERVER": "<SERVER>"\n'
                '        }\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}'
            ),
        },
        "verify": "openclaw 会话中调用 hermes_sync_status",
        "env_agent": True,
        "uninstall": {
            "zh": (
                "# 移除：删除 openclaw 配置中 mcp.servers 下的 hermes-sync 块（字段以官方文档 docs.openclaw.ai/cli/mcp 为准）"
            ),
            "en": (
                "# Remove: delete the hermes-sync block under mcp.servers in the openclaw config (fields per docs.openclaw.ai/cli/mcp)"
            ),
        },
        "install": {
            "zh": [
                {"text": "将压缩包解压到任意目录，例如 <code>C:\\hermes-sync-mcp</code>（解压后 <code>server.py</code> 位于 <code>&lt;EXTRACT_DIR&gt;/mcp/</code> 下）。"},
                {"text": "在 openclaw 配置的 <code>mcp.servers</code> 中注册（stdio；字段以官方文档 docs.openclaw.ai/cli/mcp 为准）："},
                {"code": "<REGISTER>"},
                {"text": "重启 openclaw（新会话生效）。"},
            ],
            "en": [
                {"text": "Unzip the archive to a folder, e.g. <code>C:\\hermes-sync-mcp</code> (after unzipping, <code>server.py</code> lives under <code>&lt;EXTRACT_DIR&gt;/mcp/</code>)."},
                {"text": "Register under <code>mcp.servers</code> in the openclaw config (stdio; fields per docs.openclaw.ai/cli/mcp):"},
                {"code": "<REGISTER>"},
                {"text": "Restart openclaw (new sessions pick it up)."},
            ],
        },
    },
}