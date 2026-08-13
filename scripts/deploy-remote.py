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
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=15)
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
    run("mkdir -p /opt/hermes-sync-mcp/backups")
    run(f"tar -czf /opt/hermes-sync-mcp/backups/pre-multiagent-{stamp}.tar.gz "
        "-C /opt/hermes-sync-mcp server.py templates static client 2>/dev/null")
    print(f"backup: pre-multiagent-{stamp}.tar.gz")

    # 2. upload server files
    srv = os.path.join(REPO, "server")
    for name in ("server.py", "agents.py", "translations.py"):
        sftp.put(os.path.join(srv, name), f"/opt/hermes-sync-mcp/{name}")
        print(f"uploaded {name}")
    for name in os.listdir(os.path.join(srv, "templates")):
        if name.endswith(".html"):
            sftp.put(os.path.join(srv, "templates", name),
                     f"/opt/hermes-sync-mcp/templates/{name}")
    print("uploaded templates/*.html")
    for name in os.listdir(os.path.join(srv, "static")):
        sftp.put(os.path.join(srv, "static", name),
                 f"/opt/hermes-sync-mcp/static/{name}")
    print("uploaded static/*")

    # 3. upload the mcp/ client package (download endpoint + adapters)
    run("rm -rf /opt/hermes-sync-mcp/mcp")
    run("mkdir -p /opt/hermes-sync-mcp/mcp/adapters")
    mcp = os.path.join(REPO, "mcp")
    for name in ("server.py", "updater.py", "run.bat", "run.sh"):
        sftp.put(os.path.join(mcp, name), f"/opt/hermes-sync-mcp/mcp/{name}")
    for name in os.listdir(os.path.join(mcp, "adapters")):
        if name.endswith(".py"):
            sftp.put(os.path.join(mcp, "adapters", name),
                     f"/opt/hermes-sync-mcp/mcp/adapters/{name}")
    print("uploaded mcp/ package")

    # 4. restart service (init_db runs the idempotent ALTER TABLE migration)
    out, code = run("systemctl restart hermes-sync && sleep 4 && systemctl is-active hermes-sync")
    print(f"service: {out} (exit {code})")

    # 5. verify /health
    out, code = run("curl -s http://localhost:8765/health")
    print(f"health: {out}")

    # 6. verify agent_type column exists
    out, code = run("docker exec agentctxsync-db psql -U agentctxsync -d agentctxsync "
                    "-tAc \"SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='sessions' AND column_name='agent_type';\"")
    print(f"agent_type column: {out or '(NOT FOUND)'}")

    sftp.close()
    client.close()


if __name__ == "__main__":
    main()
