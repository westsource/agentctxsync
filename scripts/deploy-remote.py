"""Deploy the new multi-agent server build to YOUR_SERVER_IP.

Steps: backup -> upload server.py/agents.py/translations.py/templates/static
and the mcp/ client package -> restart hermes-sync -> verify (health +
agent_type column probe).
"""
import os
import sys
import time

import paramiko

HOST = os.environ.get("DEPLOY_SSH_HOST", "YOUR_SERVER_IP")
USER = "root"
# Password is read from the environment so it never lands in git:
#   $env:DEPLOY_SSH_PASSWORD = "..."  ; python scripts/deploy-remote.py
PASSWORD = os.environ.get("DEPLOY_SSH_PASSWORD", "")
# Remote deployment root; the server was renamed from hermes-sync-mcp to
# agentctxsync, so deployments target /opt/agentctxsync nowadays.
REMOTE = os.environ.get("DEPLOY_REMOTE_DIR", "/opt/agentctxsync")
# systemd service running the server (renamed from hermes-sync to
# agentctxsync alongside the directory migration).
SERVICE = os.environ.get("DEPLOY_SERVICE", "agentctxsync")
# PostgreSQL container/user used for the post-deploy schema probe. Defaults
# are the generic deployment names; override for a specific environment
# (e.g. DEPLOY_DB_CONTAINER=hindsight-db DEPLOY_DB_USER=hindsight_user).
DB_CONTAINER = os.environ.get("DEPLOY_DB_CONTAINER", "agentctxsync-db")
DB_USER = os.environ.get("DEPLOY_DB_USER", "agentctxsync")
# SSH key used when DEPLOY_SSH_PASSWORD is unset (passwordless login).
KEY_FILE = os.environ.get("DEPLOY_SSH_KEY",
                          os.path.expanduser("~/.ssh/id_ed25519"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER,
                   password=PASSWORD or None,
                   key_filename=KEY_FILE if not PASSWORD else None,
                   timeout=15)
    sftp = client.open_sftp()

    def run(cmd, timeout=120):
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        if err.strip() and code != 0:
            print(f"[ERR {code}] {cmd}\n{err[:800]}")
        return out.strip(), code

    # 1. backup existing deployment (keep current behavior recoverable)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run(f"mkdir -p {REMOTE}/backups")
    run(f"tar -czf {REMOTE}/backups/pre-multiagent-{stamp}.tar.gz "
        f"-C {REMOTE} *.py templates static client 2>/dev/null")
    print(f"backup: pre-multiagent-{stamp}.tar.gz")

    # 2. upload server files (modular layout: main.py + domain modules)
    srv = os.path.join(REPO, "server")
    for name in ("main.py", "config.py", "db.py", "render.py", "auth.py",
                 "invites.py", "workspace.py", "admin.py", "sync.py",
                 "projects.py", "client_update.py", "web_help.py",
                 "agents.py", "translations.py"):
        sftp.put(os.path.join(srv, name), f"{REMOTE}/{name}")
        print(f"uploaded {name}")
    for name in os.listdir(os.path.join(srv, "templates")):
        if name.endswith(".html"):
            sftp.put(os.path.join(srv, "templates", name),
                     f"{REMOTE}/templates/{name}")
    print("uploaded templates/*.html")
    for name in os.listdir(os.path.join(srv, "static")):
        sftp.put(os.path.join(srv, "static", name),
                 f"{REMOTE}/static/{name}")
    print("uploaded static/*")

    # 3. upload the mcp/ client package (download endpoint + adapters)
    run(f"rm -rf {REMOTE}/mcp")
    run(f"mkdir -p {REMOTE}/mcp/adapters")
    mcp = os.path.join(REPO, "mcp")
    for name in ("server.py", "updater.py", "run.bat", "run.sh"):
        sftp.put(os.path.join(mcp, name), f"{REMOTE}/mcp/{name}")
    for name in os.listdir(os.path.join(mcp, "adapters")):
        if name.endswith(".py"):
            sftp.put(os.path.join(mcp, "adapters", name),
                     f"{REMOTE}/mcp/adapters/{name}")
    print("uploaded mcp/ package")

    # 4. restart service (init_db runs the idempotent ALTER TABLE migration)
    out, code = run(f"systemctl restart {SERVICE} && sleep 4 && systemctl is-active {SERVICE}")
    print(f"service: {out} (exit {code})")

    # 5. verify /health
    out, code = run("curl -s http://localhost:8765/health")
    print(f"health: {out}")

    # 6. verify agent_type column exists
    out, code = run(f"docker exec {DB_CONTAINER} psql -U {DB_USER} -d agentctxsync "
                    f"-tAc \"SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name='sessions' AND column_name='agent_type';\"")
    print(f"agent_type column: {out or '(NOT FOUND)'}")

    sftp.close()
    client.close()


if __name__ == "__main__":
    main()
