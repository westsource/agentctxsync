#!/bin/bash
# Agent Contexts Sync PostgreSQL Backup Script
# Runs daily at 3:00 AM via cron

BACKUP_DIR="/opt/hermes-sync-mcp/backups"
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/agentctxsync_${TIMESTAMP}.sql.gz"

# Dump agentctxsync database
docker exec agentctxsync-db pg_dump -U agentctxsync -d agentctxsync | gzip > "$BACKUP_FILE"

# Keep only last 7 days of backups
find "$BACKUP_DIR" -name "agentctxsync_*.sql.gz" -mtime +7 -delete

echo "[$(date)] Backup completed: $BACKUP_FILE"
