#!/bin/bash
# Agent Context Sync - Local MCP Server Deployment (multi-agent)
# Run on the local machine where the agent is installed.
# Prerequisites:
#   1. Create a workspace on the server Web UI
#   2. Copy the workspace API key
#   3. Set HERMES_SYNC_API_KEY (and HERMES_SYNC_AGENT to select the agent:
#      hermes | codex | opencode | reasonix | openclaw; default hermes)
#   4. Set HERMES_SYNC_SERVER to your deployment (defaults to a placeholder)
#
# Windows: run inside Git Bash (hermes bundles one) or WSL. The script
# deploys into HERMES_HOME and, for the openclaw agent, additionally installs
# the standalone auto-sync loop (mcp/auto-sync.py) with a Windows Scheduled
# Task so sessions sync automatically without relying on OpenClaw's lazy MCP
# spawning.

set -e

AGENT="${HERMES_SYNC_AGENT:-hermes}"
SERVER="${HERMES_SYNC_SERVER:-http://<SERVER_IP>:8765}"
HERMES_HOME="${HERMES_DIR:-$HOME/AppData/Local/hermes}"
MCP_DIR="$HERMES_HOME/mcp-servers/hermes-session-sync"
HERMES_PYTHON="${HERMES_PYTHON:-$HERMES_HOME/hermes-agent/venv/Scripts/python.exe}"
AUTOSYNC_TASK="${HERMES_SYNC_TASK:-agentctxsync-autosync}"

# Source dir: this script lives in <repo>/scripts/, client in <repo>/mcp/
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MCP_SRC="$(cd "$SCRIPT_DIR/../mcp" && pwd)"

echo "=== Agent Context Sync MCP Deployment ==="
echo "Agent: $AGENT"
echo "Server: $SERVER"
echo "Install dir: $MCP_DIR"

# 1. Create MCP server directory
mkdir -p "$MCP_DIR/adapters"

# 2. Copy files (server.py + adapter framework)
cp "$MCP_SRC/server.py" "$MCP_DIR/server.py"
cp "$MCP_SRC/run.bat" "$MCP_DIR/run.bat"
cp "$MCP_SRC/run.sh" "$MCP_DIR/run.sh"
cp "$MCP_SRC/adapters/"*.py "$MCP_DIR/adapters/"
chmod +x "$MCP_DIR/run.sh" 2>/dev/null || true

# 3. Write run.bat with environment variables
AGENT_LINE=""
if [ "$AGENT" != "hermes" ]; then
    AGENT_LINE="set HERMES_SYNC_AGENT=$AGENT"
fi
cat > "$MCP_DIR/run.bat" << EOF
@echo off
cd /d "%~dp0"
set HERMES_SYNC_SERVER=$SERVER
set HERMES_SYNC_API_KEY=$HERMES_SYNC_API_KEY
set HERMES_SYNC_INTERVAL=300
$AGENT_LINE
"$HERMES_PYTHON" server.py
EOF

# 4. Register in Hermes config.yaml (only for the hermes agent; other
#    agents register via their own MCP configuration, see docs/ADDING_AGENT.md)
if [ "$AGENT" = "hermes" ] && [ -f "$HERMES_HOME/config.yaml" ]; then
    if grep -q "hermes-session-sync" "$HERMES_HOME/config.yaml"; then
        echo "Already registered in config.yaml"
    else
        sed -i "/system-time:/,/command:.*run\.sh/{
            /command:.*run\.sh/a\  hermes-session-sync:\n    command: $MCP_DIR/run.bat
        }" "$HERMES_HOME/config.yaml"
        echo "Registered in config.yaml"
    fi
elif [ "$AGENT" != "hermes" ]; then
    echo "Agent '$AGENT' is not hermes: register server.py via its own MCP config"
    echo "  (codex: ~/.codex/config.toml | opencode: opencode.jsonc |"
    echo "   reasonix: [[plugins]] | openclaw: mcp.servers -- see docs/ADDING_AGENT.md)"
else
    echo "WARNING: config.yaml not found at $HERMES_HOME/config.yaml"
fi

# 5. OpenClaw: install the standalone auto-sync loop + Scheduled Task.
#    OpenClaw spawns bundled MCP servers lazily (only when the agent calls a
#    tool), so the MCP server's own background sync never runs on its own.
#    auto-sync.py is a persistent process that runs the same sync engine on
#    an interval; a Scheduled Task makes it survive reboots and logoffs.
if [ "$AGENT" = "openclaw" ]; then
    cp "$MCP_SRC/auto-sync.py" "$MCP_DIR/auto-sync.py"
    echo "Installed auto-sync.py (standalone periodic sync for OpenClaw)"

    # schtasks needs a Windows path; cygpath exists in Git Bash. On WSL or
    # POSIX there is no Scheduled Task -- print instructions instead.
    if command -v cygpath >/dev/null 2>&1; then
        WIN_PYTHON="$(cygpath -w "$HERMES_PYTHON")"
        WIN_AUTOSYNC="$(cygpath -w "$MCP_DIR/auto-sync.py")"
        if MSYS_NO_PATHCONV=1 schtasks /query /tn "$AUTOSYNC_TASK" \
                >/dev/null 2>&1; then
            echo "Scheduled Task '$AUTOSYNC_TASK' already exists (skipped)"
        elif MSYS_NO_PATHCONV=1 schtasks /create /tn "$AUTOSYNC_TASK" \
                /tr "\"$WIN_PYTHON\" -u \"$WIN_AUTOSYNC\"" \
                /sc onlogon /rl limited /f >/dev/null 2>&1; then
            echo "Created Scheduled Task '$AUTOSYNC_TASK' (runs at logon)"
        else
            # Non-admin shells cannot create on-logon tasks; fall back to the
            # user Startup folder (same effect, no elevation needed).
            STARTUP_DIR="$(cygpath -w "$APPDATA/Microsoft/Windows/Start Menu/Programs/Startup")"
            cat > "$STARTUP_DIR/agentctxsync-autosync.bat" << EOF2
@echo off
start "" /min "$WIN_PYTHON" -u "$WIN_AUTOSYNC"
EOF2
            echo "Scheduled Task creation denied (needs admin);"
            echo "  installed Startup-folder launcher instead:"
            echo "  $STARTUP_DIR\\agentctxsync-autosync.bat"
        fi
    else
        echo "No cygpath (non-Git-Bash shell): start auto-sync manually, e.g."
        echo "  \"$HERMES_PYTHON\" -u \"$MCP_DIR/auto-sync.py\""
    fi
fi

echo ""
echo "=== MCP Deployment complete ==="
echo ""
echo "IMPORTANT: Set HERMES_SYNC_API_KEY to your workspace API key!"
echo "  Find your workspace API key on the server Web UI (workspace detail page)"
echo "  Or set via: export HERMES_SYNC_API_KEY=ws_..."
if [ "$AGENT" = "openclaw" ]; then
    echo ""
    echo "OpenClaw auto-sync: scheduled task '$AUTOSYNC_TASK' starts"
    echo "  auto-sync.py at logon (default interval 300s; override with"
    echo "  HERMES_SYNC_INTERVAL). Stop with: schtasks /end /tn $AUTOSYNC_TASK"
fi
echo ""
echo "Restart your agent to activate"
