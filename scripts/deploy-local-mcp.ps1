# Agent Context Sync - Local MCP Server Deployment (multi-agent)
# Run in PowerShell on the local machine where the agent is installed.
# Prerequisites:
#   1. Create a workspace on the server Web UI
#   2. Copy the workspace API key
#   3. Set HERMES_SYNC_API_KEY (and HERMES_SYNC_AGENT to select the agent:
#      hermes | codex | opencode | reasonix | openclaw; default hermes)
#   4. Set HERMES_SYNC_SERVER to your deployment (defaults to a placeholder)
#
# Example (openclaw):
#   $env:HERMES_SYNC_AGENT = "openclaw"
#   $env:HERMES_SYNC_SERVER = "http://127.0.0.1:8765"
#   $env:HERMES_SYNC_API_KEY = "ws_xxx"
#   .\scripts\deploy-local-mcp.ps1

$ErrorActionPreference = "Stop"

$AGENT       = if ($env:HERMES_SYNC_AGENT)  { $env:HERMES_SYNC_AGENT }  else { "hermes" }
$SERVER      = if ($env:HERMES_SYNC_SERVER)  { $env:HERMES_SYNC_SERVER }  else { "http://<SERVER_IP>:8765" }
$HERMES_HOME = if ($env:HERMES_DIR)          { $env:HERMES_DIR }          else { "$env:LOCALAPPDATA\hermes" }
$MCP_DIR     = "$HERMES_HOME\mcp-servers\hermes-session-sync"
$HERMES_PY   = if ($env:HERMES_PYTHON)       { $env:HERMES_PYTHON }       else { "$HERMES_HOME\hermes-agent\venv\Scripts\python.exe" }
$TASK_NAME   = "agentctxsync-autosync"

# Source dir: this script lives in <repo>/scripts/, client in <repo>/mcp/
$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$MCP_SRC    = Join-Path (Split-Path -Parent $SCRIPT_DIR) "mcp"

Write-Host "=== Agent Context Sync MCP Deployment ==="
Write-Host "Agent: $AGENT"
Write-Host "Server: $SERVER"
Write-Host "Install dir: $MCP_DIR"

# 1. Create MCP server directory
New-Item -ItemType Directory -Force -Path "$MCP_DIR\adapters" | Out-Null

# 2. Copy files (server.py + adapter framework)
Copy-Item "$MCP_SRC\server.py" "$MCP_DIR\server.py" -Force
Copy-Item "$MCP_SRC\run.bat"   "$MCP_DIR\run.bat"   -Force
Copy-Item "$MCP_SRC\run.sh"    "$MCP_DIR\run.sh"    -Force
Copy-Item "$MCP_SRC\adapters\*.py" "$MCP_DIR\adapters\" -Force

# 3. Write run.bat with environment variables
$AGENT_LINE = if ($AGENT -ne "hermes") { "set HERMES_SYNC_AGENT=$AGENT" } else { "" }
$batContent = @"
@echo off
cd /d "%~dp0"
set HERMES_SYNC_SERVER=$SERVER
set HERMES_SYNC_API_KEY=$($env:HERMES_SYNC_API_KEY)
set HERMES_SYNC_INTERVAL=300
$AGENT_LINE
"$HERMES_PY" server.py
"@
Set-Content "$MCP_DIR\run.bat" $batContent -Encoding ASCII

# 4. Register in Hermes config.yaml (only for the hermes agent; other
#    agents register via their own MCP configuration, see docs/ADDING_AGENT.md)
if ($AGENT -eq "hermes" -and (Test-Path "$HERMES_HOME\config.yaml")) {
    if (Select-String -Path "$HERMES_HOME\config.yaml" -Pattern "hermes-session-sync" -Quiet) {
        Write-Host "Already registered in config.yaml"
    } else {
        Add-Content "$HERMES_HOME\config.yaml" "`n  hermes-session-sync:`n    command: $MCP_DIR\run.bat"
        Write-Host "Registered in config.yaml"
    }
} elseif ($AGENT -ne "hermes") {
    Write-Host "Agent '$AGENT' is not hermes: register server.py via its own MCP config"
    Write-Host "  (codex: ~/.codex/config.toml | opencode: opencode.jsonc |"
    Write-Host "   reasonix: [[plugins]] | openclaw: mcp.servers -- see docs/ADDING_AGENT.md)"
} else {
    Write-Host "WARNING: config.yaml not found at $HERMES_HOME\config.yaml"
}

# 5. OpenClaw: install the standalone auto-sync loop + Scheduled Task.
#    OpenClaw spawns bundled MCP servers lazily (only when the agent calls a
#    tool), so the MCP server's own background sync never runs on its own.
if ($AGENT -eq "openclaw") {
    Copy-Item "$MCP_SRC\auto-sync.py" "$MCP_DIR\auto-sync.py" -Force
    Write-Host "Installed auto-sync.py (standalone periodic sync for OpenClaw)"

    try {
        $action  = New-ScheduledTaskAction -Execute $HERMES_PY -Argument "-u `"$MCP_DIR\auto-sync.py`""
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName $TASK_NAME -Action $action -Trigger $trigger -Force | Out-Null
        Write-Host "Created Scheduled Task '$TASK_NAME' (runs at logon)"
    } catch {
        # Non-admin shells cannot create on-logon tasks; fall back to the
        # user Startup folder (same effect, no elevation needed).
        $startupDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
        $batContent = "@echo off`nstart `"`" /min `"$HERMES_PY`" -u `"$MCP_DIR\auto-sync.py`""
        Set-Content "$startupDir\agentctxsync-autosync.bat" $batContent -Encoding ASCII
        Write-Host "Scheduled Task creation denied (needs admin);"
        Write-Host "  installed Startup-folder launcher instead:"
        Write-Host "  $startupDir\agentctxsync-autosync.bat"
    }
}

Write-Host ""
Write-Host "=== MCP Deployment complete ==="
Write-Host ""
Write-Host "IMPORTANT: Set HERMES_SYNC_API_KEY to your workspace API key!"
Write-Host "  Find your workspace API key on the server Web UI (workspace detail page)"
Write-Host "  Or set via: `$env:HERMES_SYNC_API_KEY = 'ws_...'"
if ($AGENT -eq "openclaw") {
    Write-Host ""
    Write-Host "OpenClaw auto-sync: scheduled task '$TASK_NAME' starts"
    Write-Host "  auto-sync.py at logon (default interval 300s; override with"
    Write-Host "  HERMES_SYNC_INTERVAL). Stop with: schtasks /end /tn $TASK_NAME"
}
Write-Host ""
Write-Host "Restart your agent to activate"
