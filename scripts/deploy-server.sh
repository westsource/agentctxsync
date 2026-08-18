#!/bin/bash
# Agent Context Sync - Server Deployment Script (Multi-tenant)
# Run on the target server (set HERMES_SYNC_* env vars beforehand)
set -e

echo "=== Agent Context Sync Server Deployment (Multi-tenant) ==="

# 1. Create directories
mkdir -p /opt/agentctxsync/data
mkdir -p /opt/agentctxsync/backups

# 2. Copy files
# Modular layout: main.py assembles config/db/render/auth/invites/workspace/
# admin/sync/projects/client_update/web_help (all .py in the server dir).
cp *.py /opt/agentctxsync/
cp backup.sh /opt/agentctxsync/backup.sh
chmod +x /opt/agentctxsync/backup.sh
# Note: docker-compose.yaml is NOT part of this project; PostgreSQL is
# expected to be prepared by the deployment environment (self-hosted or
# any container, e.g. agentctxsync-db).
mkdir -p /opt/agentctxsync/templates /opt/agentctxsync/static
cp templates/*.html /opt/agentctxsync/templates/
cp static/* /opt/agentctxsync/static/
# MCP client package (shipped via /web/download/mcp-client; also the
# adapter framework used by Hermes and other agents)
cp -r ../mcp /opt/agentctxsync/mcp

# 3. Create Python venv (if not exists)
if [ ! -d /opt/agentctxsync/venv ]; then
    python3 -m venv /opt/agentctxsync/venv
fi
/opt/agentctxsync/venv/bin/pip install fastapi uvicorn psycopg2-binary jinja2 markdown python-multipart -q

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
cat > /etc/systemd/system/agentctxsync.service << EOF
[Unit]
Description=Agent Context Sync MCP Server (Multi-tenant)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/agentctxsync
ExecStart=/opt/agentctxsync/venv/bin/python /opt/agentctxsync/main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=HERMES_SYNC_PG_DSN=$HERMES_SYNC_PG_DSN
Environment=HERMES_SYNC_JWT_SECRET=$HERMES_SYNC_JWT_SECRET
Environment=HERMES_SYNC_MASTER_KEY=$HERMES_SYNC_MASTER_KEY

[Install]
WantedBy=multi-user.target
EOF

# 6. PostgreSQL is provided by the deployment environment (self-hosted or
#    any Docker container, e.g. agentctxsync-db). HERMES_SYNC_PG_DSN must
#    point to a ready database named `agentctxsync` — tables are created
#    automatically by init_db() on first start (see db.py).
echo "PostgreSQL: expected via HERMES_SYNC_PG_DSN (database 'agentctxsync' must exist;"
echo "            tables are auto-created by main.py on first start)."

# 7. Setup backup cron
(crontab -l 2>/dev/null | grep -v "agentctxsync/backup"; echo "0 3 * * * /opt/agentctxsync/backup.sh >> /opt/agentctxsync/backups/backup.log 2>&1") | crontab -

# 8. Start service
systemctl daemon-reload
systemctl enable agentctxsync
systemctl restart agentctxsync

echo "=== Deployment complete ==="
echo "Service: $(systemctl is-active agentctxsync)"
echo "API: http://$(hostname -I | awk '{print $1}'):8765/health"
echo "Web UI: http://$(hostname -I | awk '{print $1}'):8765/web/"
echo ""
echo "Default admin: admin / (随机密码，首次启动时打印在服务端日志，登录后强制修改)"
echo "Login at http://$(hostname -I | awk '{print $1}'):8765/web/login"
echo "After login, create a workspace and use its API key in MCP client."
