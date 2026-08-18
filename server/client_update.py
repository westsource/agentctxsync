"""Client distribution: archive build + manifest/download endpoints."""
import hashlib
import io
import json
import os
import re
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from agents import AGENTS
from config import _client_default_server
from auth import get_workspace_by_api_key
from render import get_lang

router = APIRouter()
# ============================================================


# Only Hermes has been strictly tested end-to-end. The other agent adapters
# stay in AGENTS (registry) for development, but their MCP client
# distribution and help-page onboarding are taken offline until validated.
# Re-enable an agent by adding its key here.
PUBLIC_AGENTS = ("hermes", "workbuddy")

# The client source is the repository `mcp/` package. Two layouts exist:
#   repo:    <repo>/server/server.py  + <repo>/mcp/            (one level up)
#   deploy:  /opt/hermes-sync-mcp/server.py + /opt/hermes-sync-mcp/mcp/
#            (same directory, deploy-server.sh copies mcp/ next to server.py)
_SRV_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_DIR = os.path.join(_SRV_DIR, "mcp") \
    if os.path.isdir(os.path.join(_SRV_DIR, "mcp")) \
    else os.path.join(os.path.dirname(_SRV_DIR), "mcp")

# Client distribution version. Bump this together with CLIENT_VERSION in
# mcp/server.py whenever the client package changes; clients compare it via
# /api/client/manifest and auto-update.
CLIENT_VERSION = "2026.08.18.1"

def _client_archive_files():
    """[(arcname, source_path)] for every file shipped in the client zip."""
    files = [("mcp/server.py", os.path.join(CLIENT_DIR, "server.py")),
             ("mcp/updater.py", os.path.join(CLIENT_DIR, "updater.py")),
             ("mcp/run.sh", os.path.join(CLIENT_DIR, "run.sh")),
             ("mcp/run.bat", os.path.join(CLIENT_DIR, "run.bat"))]
    ad = os.path.join(CLIENT_DIR, "adapters")
    if os.path.isdir(ad):
        for name in sorted(os.listdir(ad)):
            if name.endswith(".py"):
                files.append((f"mcp/adapters/{name}", os.path.join(ad, name)))
    return files

# The client zip ships mcp/server.py with two defaults rewritten at build
# time: SYNC_SERVER -> the serving server's address (configured PUBLIC_URL,
# or per-request base_url) and HERMES_SYNC_AGENT -> the agent the archive
# was downloaded for. The manifest hash must therefore be computed over the
# shipped bytes, not the raw repo file — otherwise client-side verification
# fails.
_SYNC_SERVER_RE = re.compile(
    r'(SYNC_SERVER = os\.environ\.get\("HERMES_SYNC_SERVER", )"[^"]*"')
_SYNC_AGENT_RE = re.compile(
    r'(AGENT = os\.environ\.get\("HERMES_SYNC_AGENT", )"[^"]*"')


def _ship_bytes(arcname: str, src: str, default_server: str,
                agent: str = "hermes") -> bytes:
    """Exact bytes shipped inside a client archive for one file."""
    if arcname == "mcp/server.py":
        text = open(src, encoding="utf-8").read()
        text = _SYNC_SERVER_RE.sub(
            lambda m: m.group(1) + json.dumps(default_server), text)
        text = _SYNC_AGENT_RE.sub(
            lambda m: m.group(1) + json.dumps(agent), text)
        return text.encode("utf-8")
    with open(src, "rb") as f:
        return f.read()


def _client_manifest_files(default_server: str, agent: str = "hermes"):
    """[{path, sha256, size}] for every shipped file, hashed over the
    bytes actually placed in the archive (server.py defaults rewritten)."""
    manifest = []
    for arcname, src in _client_archive_files():
        try:
            data = _ship_bytes(arcname, src, default_server, agent)
        except OSError:
            continue
        rel = arcname[len("mcp/"):] if arcname.startswith("mcp/") else arcname
        manifest.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(),
                         "size": len(data)})
    return manifest


def _build_client_zip(agent: str, default_server: str,
                      readme: str | None = None) -> bytes:
    """Client archive bytes (mcp/ package + manifest.json at the top level).
    ``readme``, when given, is written as mcp/README.md inside the archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, src in _client_archive_files():
            try:
                zf.writestr(arcname, _ship_bytes(arcname, src, default_server,
                                                 agent))
            except OSError:
                continue
        if readme:
            zf.writestr("mcp/README.md", readme)
        zf.writestr("manifest.json", json.dumps(
            {"version": CLIENT_VERSION, "files": _client_manifest_files(default_server, agent)},
            ensure_ascii=False))
    return buf.getvalue()

def _build_readme(agent_key, api_key, ws_name, server_url):
    """Build the in-archive README. ``api_key`` is a placeholder string
    (never a real key -- the archive may be shared)."""
    agent = AGENTS[agent_key]
    lang = get_lang()
    is_en = lang == "en"
    t_ = (lambda zh, en: en if is_en else zh)
    register = agent["register"]["en" if is_en else "zh"] \
        .replace("<KEY>", api_key).replace("<SERVER>", server_url)
    if is_en:
        return (
            f"# Agent Context Sync MCP Client ({agent['label']})\n\n"
            f"Workspace: **{ws_name}**  \u00b7  Sync server: `{server_url}`\n\n"
            "This client connects your agent to the sync server above so sessions "
            "sync across devices and across agents (Hermes / Codex / opencode / "
            "Reasonix / OpenClaw / WorkBuddy share the same workspace pool).\n\n"
            "## Files\n"
            "- `mcp/server.py` - the MCP server (stdio)\n"
            "- `mcp/run.sh` / `mcp/run.bat` - optional launchers\n"
            "- `mcp/adapters/` - per-agent local store adapters (loaded by server.py)\n\n"
            f"Agent: **{agent['label']}**  \u00b7  {agent['desc']['en']}\n\n"
            f"Local store: {agent['store']['en']}\n\n"
            "## Install\n"
            "1. Unzip this archive to a folder, e.g. `C:\\hermes-sync-mcp`.\n"
            "2. Register the MCP server for your agent (replace `<PYTHON>` with a "
            "Python 3.10+ interpreter, `<EXTRACT_DIR>` with the folder from step 1, "
            "and `<YOUR_API_KEY>` with your workspace API key from the help page):\n\n"
            "```bash\n" + register + "\n```\n\n"
            "## Remove (old version / cleanup)\n"
            f"```bash\n{agent.get('uninstall', {}).get('en', '')}\n```\n\n"
            "## Verify\n"
            f"```bash\n{agent['verify']}\n```\n"
            "Tools: `hermes_sync_status`, `hermes_sync_pull`, `hermes_sync_push`, `hermes_sync_full`\n\n"
            "The client pulls once on startup (bootstrapping local data when the "
            "remote workspace is empty), then auto-syncs every 300 seconds.\n\n"
            "## Auto-update\n"
            "The client checks for updates shortly after startup and then every "
            "24 hours; new files replace the old ones in place and take effect "
            "on the next agent restart (set HERMES_SYNC_AUTO_UPDATE=0 to "
            "disable, or HERMES_SYNC_UPDATE_INTERVAL to change the interval).\n"
        )
    return (
        f"# Hermes 会话同步 MCP 客户端（{agent['label']}）\n\n"
        f"工作空间：**{ws_name}**  \u00b7  同步服务器：`{server_url}`\n\n"
        "将该 Agent 接入同步服务器，实现跨设备、跨 Agent（Hermes / Codex / "
        "opencode / Reasonix / OpenClaw / WorkBuddy 共享同一工作空间会话池）的会话同步。\n\n"
        "## 文件说明\n"
        "- `mcp/server.py` — MCP 服务端程序（stdio 模式）\n"
        "- `mcp/run.sh` / `mcp/run.bat` — 可选启动脚本\n"
        "- `mcp/adapters/` — 各 Agent 本地存储适配器（由 server.py 加载）\n\n"
        f"Agent：**{agent['label']}**  \u00b7  {agent['desc']['zh']}\n\n"
        f"本地存储：{agent['store']['zh']}\n\n"
        "## 安装步骤\n"
        "1. 将本压缩包解压到任意目录，例如 `C:\\hermes-sync-mcp`。\n"
        "2. 按你的 Agent 注册 MCP Server（将 `<PYTHON>` 替换为 Python 3.10+ 解释器路径，"
        "`<EXTRACT_DIR>` 替换为第 1 步的目录，`<YOUR_API_KEY>` 替换为帮助页中的工作空间 "
        "API Key）：\n\n"
        "```bash\n" + register + "\n```\n\n"
        "## 移除旧版（重新安装或清理时）\n"
        f"```bash\n{agent.get('uninstall', {}).get('zh', '')}\n```\n\n"
        "## 验证\n"
        f"```bash\n{agent['verify']}\n```\n"
        "可用工具：`hermes_sync_status`、`hermes_sync_pull`、`hermes_sync_push`、`hermes_sync_full`\n\n"
        "客户端启动时自动拉取一次（远程为空时自动推送本地数据完成首次配对），"
        "之后每 300 秒自动同步一次。\n\n"
        "## 自动更新\n"
        "客户端启动后稍候即检查一次更新，之后每 24 小时检查一次；新文件就地替换，"
        "**重启 Agent 后生效**（设 `HERMES_SYNC_AUTO_UPDATE=0` 可关闭，"
        "或用 `HERMES_SYNC_UPDATE_INTERVAL` 调整检查间隔）。\n"
    )

# ============================================================

@router.get("/api/client/manifest")
async def api_client_manifest(request: Request, agent: str = "hermes", v: str = "",
                              ws: dict = Depends(get_workspace_by_api_key)):
    """Lightweight version check: the client sends its local version via ``v``
    and gets update_available + the file manifest (sha256/size) without
    downloading the whole archive."""
    if agent not in PUBLIC_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent {agent!r} is not publicly released yet")
    default_server = _client_default_server(str(request.base_url).rstrip("/"))
    return {"version": CLIENT_VERSION,
            "update_available": v != CLIENT_VERSION,
            "files": _client_manifest_files(default_server, agent)}

@router.get("/api/client/download")
async def api_client_download(request: Request, agent: str = "hermes",
                              ws: dict = Depends(get_workspace_by_api_key)):
    """Client archive zip with an embedded manifest.json for verification."""
    if agent not in PUBLIC_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent {agent!r} is not publicly released yet")
    default_server = _client_default_server(str(request.base_url).rstrip("/"))
    data = _build_client_zip(agent, default_server)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="agentctxsync-mcp-client-{agent}.zip"'},
    )


# ============================================================
# API Routes (unchanged)
# ============================================================

# ============================================================

