#!/bin/bash
# Agent Context Sync - Server Deployment Script (Multi-tenant)
# Run on the target server (set HERMES_SYNC_* env vars beforehand)
set -e

echo "=== Agent Context Sync Server Deployment (Multi-tenant) ==="

# 1. Create directories
mkdir -p /opt/hermes-sync-mcp/data
mkdir -p /opt/hermes-sync-mcp/backups
mkdir -p /opt/hindsight

# 2. Copy files
# Modular layout: main.py assembles config/db/render/auth/invites/workspace/
# admin/sync/projects/client_update/web_help (all .py in the server dir).
cp *.py /opt/hermes-sync-mcp/
cp backup.sh /opt/hermes-sync-mcp/backup.sh
chmod +x /opt/hermes-sync-mcp/backup.sh
# Note: docker-compose.yaml is NOT part of this project (hindsight stack);
# PostgreSQL is expected to be prepared by the deployment environment.
mkdir -p /opt/hermes-sync-mcp/templates /opt/hermes-sync-mcp/static
cp templates/*.html /opt/hermes-sync-mcp/templates/
cp static/* /opt/hermes-sync-mcp/static/
# MCP client package (shipped via /web/download/mcp-client; also the
# adapter framework used by Hermes and other agents)
cp -r ../mcp /opt/hermes-sync-mcp/mcp

# 3. Create Python venv (if not exists)
if [ ! -d /opt/hermes-sync-mcp/venv ]; then
    python3 -m venv /opt/hermes-sync-mcp/venv
fi
/opt/hermes-sync-mcp/venv/bin/pip install fastapi uvicorn psycopg2-binary jinja2 markdown python-multipart -q

# 4. Generate JWT secret if not set; require PG DSN + master key
if [ -z "$HERMES_SYNC_JWT_SECRET" ]; then
    export HERMES_SYNC_JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    echo "Generated JWT_SECRET: $HERMES_SYNC_JWT_SECRET"
fi
if [ -z "$HERMES_SYNC_PG_DSN" ]; then
    export HERMES_SYNC_PG_DSN="postgresql://${POSTGRES_USER:-agentctxsync}:${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}@localhost:5432/${POSTGRES_DB:-agentctxsync}"
    echo "Built HERMES_SYNC_PG_DSN from docker .env"
fi
if [ -z "$HERMES_SYNC_MASTER_KEY" ]; then
    export HERMES_SYNC_MASTER_KEY=$(python3 -c "import secrets; print('hsync_' + secrets.token_hex(16))")
    echo "Generated MASTER_KEY: $HERMES_SYNC_MASTER_KEY"
fi

# 5. Create systemd service
cat > /etc/systemd/system/hermes-sync.service << EOF
[Unit]
Description=Agent Context Sync MCP Server (Multi-tenant)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/hermes-sync-mcp
ExecStart=/opt/hermes-sync-mcp/venv/bin/python /opt/hermes-sync-mcp/main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=HERMES_SYNC_PG_DSN=$HERMES_SYNC_PG_DSN
Environment=HERMES_SYNC_JWT_SECRET=$HERMES_SYNC_JWT_SECRET
Environment=HERMES_SYNC_MASTER_KEY=$HERMES_SYNC_MASTER_KEY

[Install]
WantedBy=multi-user.target
EOF

# 6. Start PostgreSQL (via Docker Compose) — only if the hindsight stack
#    with its compose file is present; otherwise an existing PostgreSQL
#    instance is expected via HERMES_SYNC_PG_DSN.
if [ -f /opt/hindsight/docker-compose.yaml ]; then
cd /opt/hindsight
docker compose up -d db

# Wait for PG to be ready
echo "Waiting for PostgreSQL..."
for i in {1..30}; do
    docker exec agentctxsync-db pg_isready -U agentctxsync && break
    sleep 1
done

# 7. Create agentctxsync database (if not exists)
docker exec -i agentctxsync-db psql -U agentctxsync -d postgres -c "CREATE DATABASE agentctxsync;" 2>/dev/null || true

# 8. Create tables
docker exec -i agentctxsync-db psql -U agentctxsync -d agentctxsync << 'SQL'
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    is_admin BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at DOUBLE PRECISION,
    last_login_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS workspaces (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    api_key TEXT UNIQUE NOT NULL,
    description TEXT,
    created_at DOUBLE PRECISION,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT, workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT, source TEXT, model TEXT, model_config TEXT,
    system_prompt TEXT, parent_session_id TEXT, started_at DOUBLE PRECISION,
    ended_at DOUBLE PRECISION, end_reason TEXT, message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT, git_branch TEXT, git_repo_root TEXT, billing_provider TEXT,
    billing_base_url TEXT, billing_mode TEXT, estimated_cost_usd DOUBLE PRECISION,
    actual_cost_usd DOUBLE PRECISION, cost_status TEXT, cost_source TEXT,
    pricing_version TEXT, title TEXT, api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT, handoff_platform TEXT, handoff_error TEXT,
    rewind_count INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
    session_key TEXT, chat_id TEXT, chat_type TEXT, thread_id TEXT,
    compression_failure_cooldown_until DOUBLE PRECISION,
    compression_failure_error TEXT, display_name TEXT, origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0, compression_fallback_streak INTEGER DEFAULT 0,
    profile_name TEXT, compression_ineffective_count INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0, last_synced_at DOUBLE PRECISION,
    agent_type TEXT DEFAULT 'hermes', meta JSONB,
    PRIMARY KEY (workspace_id, id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER, session_id TEXT NOT NULL, workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT, role TEXT,
    content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
    timestamp DOUBLE PRECISION, token_count INTEGER, finish_reason TEXT,
    reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
    codex_reasoning_items TEXT, codex_message_items TEXT,
    platform_message_id TEXT, observed INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
    compacted INTEGER DEFAULT 0, effect_disposition TEXT, api_content TEXT,
    display_kind TEXT, display_metadata TEXT,
    agent_type TEXT DEFAULT 'hermes', meta JSONB,
    PRIMARY KEY (workspace_id, session_id, id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    device_id TEXT, workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
    last_sync_at DOUBLE PRECISION,
    sessions_synced INTEGER DEFAULT 0, messages_synced INTEGER DEFAULT 0,
    PRIMARY KEY (device_id, workspace_id)
);
SQL

# 9. Grant privileges
docker exec agentctxsync-db psql -U agentctxsync -d agentctxsync -c \
    "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO agentctxsync; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO agentctxsync;"
else
    echo "Skipping Docker PostgreSQL provisioning: /opt/hindsight/docker-compose.yaml not found."
    echo "Ensure HERMES_SYNC_PG_DSN points to a ready PostgreSQL (agentctxsync database must"
    echo "already exist; tables are auto-created by main.py on first start)."
fi

# 10. Setup backup cron
(crontab -l 2>/dev/null | grep -v "hermes-sync-mcp/backup"; echo "0 3 * * * /opt/hermes-sync-mcp/backup.sh >> /opt/hermes-sync-mcp/backups/backup.log 2>&1") | crontab -

# 11. Start service
systemctl daemon-reload
systemctl enable hermes-sync
systemctl restart hermes-sync

echo "=== Deployment complete ==="
echo "Service: $(systemctl is-active hermes-sync)"
echo "API: http://$(hostname -I | awk '{print $1}'):8765/health"
echo "Web UI: http://$(hostname -I | awk '{print $1}'):8765/web/"
echo ""
echo "Default admin: admin / (随机密码，首次启动时打印在服务端日志，登录后强制修改)"
echo "Login at http://$(hostname -I | awk '{print $1}'):8765/web/login"
echo "After login, create a workspace and use its API key in MCP client."
