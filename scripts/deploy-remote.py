"""Deploy the server build (and the client package it serves) to a host.

Steps: backup -> upload ``server/*.py`` + templates + static, the ``mcp/``
client package and the one-shot ``scripts/`` that ship inside the client zip ->
restart the service -> verify (health + ``agent_type`` column).

The upload lists are derived from the trees, not hardcoded: a hardcoded list
silently skipped every module added after it was written (it once shipped a
``feedback.py`` that no longer existed and dropped every new client file).

Environment:
    DEPLOY_SSH_HOST    target host (default: YOUR_SERVER_IP placeholder)
    DEPLOY_SSH_PORT    SSH port (default 22; set when the host uses another)
    DEPLOY_SSH_PASSWORD / DEPLOY_SSH_KEY (/ default ~/.ssh/id_ed25519)
    DEPLOY_REMOTE_DIR /opt/agentctxsync
    DEPLOY_SERVICE    agentctxsync
    DEPLOY_DB_*       optional psql probe overrides; the probe tries the local
                      PG first (63 runs PostgreSQL natively) and falls back to
                      the legacy docker container
    --list            print exactly what would be uploaded, connect nothing
"""
import os
import posixpath
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOST = os.environ.get("DEPLOY_SSH_HOST", "YOUR_SERVER_IP")
PORT = int(os.environ.get("DEPLOY_SSH_PORT", "22"))
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
# Schema probe: local PostgreSQL first (the 63 host runs PG natively), legacy
# docker container as the fallback.
DB_DIRECT_URL = os.environ.get("DEPLOY_DB_URL", "127.0.0.1")
DB_CONTAINER = os.environ.get("DEPLOY_DB_CONTAINER", "agentctxsync-db")
DB_USER = os.environ.get("DEPLOY_DB_USER", "agentctxsync")
DB_NAME = os.environ.get("DEPLOY_DB_NAME", "agentctxsync")
# SSH key used when DEPLOY_SSH_PASSWORD is unset (passwordless login).
KEY_FILE = os.environ.get("DEPLOY_SSH_KEY",
                          os.path.expanduser("~/.ssh/id_ed25519"))


def server_files():
    """[(local, remote)] for every server module/template/static file."""
    out = []
    srv = os.path.join(REPO, "server")
    for name in sorted(n for n in os.listdir(srv) if n.endswith(".py")):
        out.append((os.path.join(srv, name), f"{REMOTE}/{name}"))
    for sub in ("templates", "static"):
        directory = os.path.join(srv, sub)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                out.append((path, f"{REMOTE}/{sub}/{name}"))
    return out


def mcp_files():
    """Client package files the server serves (``mcp/`` -> ``{REMOTE}/mcp``)."""
    mcp = os.path.join(REPO, "mcp")
    out = []
    for name in sorted(os.listdir(mcp)):
        path = os.path.join(mcp, name)
        if os.path.isfile(path) and name.endswith(".py"):
            out.append((path, f"{REMOTE}/mcp/{name}"))
    for name in ("run.bat", "run.sh"):
        path = os.path.join(mcp, name)
        if os.path.exists(path):
            out.append((path, f"{REMOTE}/mcp/{name}"))
    adapters = os.path.join(mcp, "adapters")
    if os.path.isdir(adapters):
        for name in sorted(os.listdir(adapters)):
            if name.endswith(".py"):
                out.append((os.path.join(adapters, name),
                            f"{REMOTE}/mcp/adapters/{name}"))
    return out


def script_files():
    """One-shot scripts shipped inside the client zip (<remote>/../scripts).

    Keep this list in sync with ``server/client_update._client_archive_files``.
    """
    names = ("deploy-local-mcp.ps1", "deploy-local-mcp.sh",
             "install-hermes-sync-hook.py", "probe-hermes-write-path.py")
    out = []
    for name in names:
        path = os.path.join(REPO, "scripts", name)
        if os.path.exists(path):
            # Remote paths are POSIX; os.path would emit "\\opt\\scripts" here.
            out.append((path, posixpath.normpath(
                posixpath.join(REMOTE, "..", "scripts", name))))
    return out


def _list():
    for label, files in (("server", server_files()), ("mcp", mcp_files()),
                         ("scripts", script_files())):
        print(f"== {label} ({len(files)})")
        for local, remote in files:
            print(f"  {os.path.relpath(local, REPO)} -> {remote}")


def main(argv=None):
    if "--list" in (argv if argv is not None else sys.argv[1:]):
        _list()
        return 0
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER,
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
    run(f"tar -czf {REMOTE}/backups/pre-release-{stamp}.tar.gz "
        f"-C {REMOTE} . 2>/dev/null")
    print(f"backup: pre-release-{stamp}.tar.gz")

    # 2. server modules, templates, static (derived from the tree)
    for local, remote in server_files():
        sftp.put(local, remote)
    print(f"uploaded {len(server_files())} server files")

    # 3. client package + the scripts that ship inside its zip
    run(f"rm -rf {REMOTE}/mcp")
    run(f"mkdir -p {REMOTE}/mcp/adapters")
    for local, remote in mcp_files() + script_files():
        sftp.put(local, remote)
    print(f"uploaded {len(mcp_files())} mcp files + {len(script_files())} scripts")

    # 4. restart service (init_db runs the idempotent ALTER TABLE migration)
    out, code = run(f"systemctl restart {SERVICE} && sleep 4 && "
                    f"systemctl is-active {SERVICE}")
    print(f"service: {out} (exit {code})")

    # 5. verify /health
    out, _ = run("curl -s http://127.0.0.1:8765/health")
    print(f"health: {out}")

    # 6. schema probe (local PG on the 63 host; docker fallback for old boxes)
    probe = ('psql -h %s -U %s -d %s -tAc "SELECT column_name FROM '
             'information_schema.columns WHERE table_name=\'sessions\' '
             'AND column_name=\'agent_type\';"' % (DB_DIRECT_URL, DB_USER, DB_NAME))
    out, code = run(probe)
    if not out:
        out, code = run(f"docker exec {DB_CONTAINER} psql -U {DB_USER} -d {DB_NAME} "
                        f"-tAc \"SELECT column_name FROM information_schema.columns "
                        f"WHERE table_name='sessions' AND column_name='agent_type';\"")
    print(f"agent_type column: {out or '(NOT FOUND)'}")

    sftp.close()
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
