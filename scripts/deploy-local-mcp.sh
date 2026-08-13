#!/bin/bash
# Agent Contexts Sync - Local MCP Server Deployment (multi-agent)
# Run on the local machine where the agent is installed.
# Prerequisites:
#   1. Create a workspace on the server Web UI
#   2. Copy the workspace API key
#   3. Set HERMES_SYNC_API_KEY (and HERMES_SYNC_AGENT to select the agent:
#      hermes | codex | opencode | reasonix | openclaw; default hermes)
#   4. Set HERMES_SYNC_SERVER to your deployment (defaults to a placeholder)

set -e

AGENT="${HERMES_SYNC_AGENT:-hermes}"
SERVER="${HERMES_SYNC_SERVER:-http://<SERVER_IP>:8765}"
HERMES_HOME="${HERMES_DIR:-$HOME/AppData/Local/hermes}"
MCP_DIR="$HERMES_HOME/mcp-servers/hermes-session-sync"
HERMES_PYTHON="${HERMES_PYTHON:-$HERMES_HOME/hermes-agent/venv/Scripts/python.exe}"

# Source dir: this script lives in <repo>/scripts/, client in <repo>/mcp/
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MCP_SRC="$(cd "$SCRIPT_DIR/../mcp" && pwd)"

echo "=== Agent Contexts Sync MCP Deployment ==="
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
    echo "  (codex: ~/.codex/config.toml | opencode: opencode.json |"
    echo "   reasonix: [[plugins]] | openclaw: mcp.servers -- see docs/ADDING_AGENT.md)"
else
    echo "WARNING: config.yaml not found at $HERMES_HOME/config.yaml"
fi

echo ""
echo "=== MCP Deployment complete ==="
echo ""
echo "IMPORTANT: Set HERMES_SYNC_API_KEY to your workspace API key!"
echo "  Find your workspace API key on the server Web UI (workspace detail page)"
echo "  Or set via: export HERMES_SYNC_API_KEY=ws_..."
echo ""
echo "Restart your agent to activate"
