"""
Client auto-update.

The MCP server cannot restart itself, so updates replace files in the
background and take effect on the next agent restart (a running Python
process keeps its loaded code; Windows allows overwriting loaded .py files).

Flow: fetch /api/client/manifest?agent=X&v=<local> -> if update_available,
download /api/client/download -> verify every file's sha256 against the
embedded manifest.json -> back up current files -> atomically replace ->
persist the new version in <mcp_dir>/.hermes-sync-version.

Failures (network, bad hash, write error) leave the previous files intact
and only log; sync functionality is never blocked by the updater.
"""

import hashlib
import io
import json
import shutil
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

# Keep in sync with CLIENT_VERSION in server/client_update.py when releasing.
CLIENT_VERSION = "2026.08.29.1"

VERSION_FILE_NAME = ".hermes-sync-version"


def local_version(version_file: Path, fallback: str = CLIENT_VERSION) -> str:
    """Version recorded locally (fallback to the built-in constant)."""
    try:
        v = version_file.read_text(encoding="utf-8").strip()
        return v if v else fallback
    except OSError:
        return fallback


def fetch_manifest(server: str, api_key: str, agent: str, local_v: str,
                   timeout: int = 30):
    """GET /api/client/manifest. Returns the parsed dict or None on error."""
    url = f"{server}/api/client/manifest?agent={agent}&v={local_v}"
    try:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None


def fetch_archive(server: str, api_key: str, agent: str,
                  timeout: int = 120) -> bytes | None:
    """GET /api/client/download. Returns raw zip bytes or None on error."""
    url = f"{server}/api/client/download?agent={agent}"
    try:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None


def verify_archive(data: bytes, manifest_files: list[dict]):
    """Verify the zip against the manifest (path -> sha256) and return
    {relative_path: file_bytes}. Returns None when any file is missing or
    its hash does not match (tampered/truncated archive)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    try:
        by_arc = {name: zf.read(name) for name in zf.namelist()}
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    out = {}
    for entry in manifest_files:
        rel = entry.get("path")
        arc = f"mcp/{rel}"
        payload = by_arc.get(arc)
        if payload is None:
            return None
        if hashlib.sha256(payload).hexdigest() != entry.get("sha256"):
            return None
        out[rel] = payload
    return out


def apply_update(files: dict, mcp_dir: Path, version_file: Path,
                 new_version: str, log) -> bool:
    """Back up current files, replace them atomically, drop files that are
    no longer shipped, and persist the new version. Returns True on success;
    on any write failure the old files are left intact."""
    mcp_dir = Path(mcp_dir)
    try:
        # 1. back up everything we are about to replace
        old_version = local_version(version_file)
        bak = mcp_dir / f".bak-{old_version or 'unknown'}"
        if bak.exists():
            shutil.rmtree(bak, ignore_errors=True)
        bak.mkdir(parents=True, exist_ok=True)
        for rel in files:
            src = mcp_dir / rel
            if src.exists():
                dst = bak / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        # 2. atomically replace (write .tmp then rename)
        for rel, payload in files.items():
            dst = mcp_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".tmp")
            tmp.write_bytes(payload)
            tmp.replace(dst)

        # 3. drop stale adapter files no longer shipped
        shipped = {rel for rel in files if rel.startswith("adapters/")}
        ad_dir = mcp_dir / "adapters"
        if ad_dir.is_dir():
            for p in ad_dir.glob("*.py"):
                if f"adapters/{p.name}" not in shipped:
                    try:
                        p.unlink()
                    except OSError:
                        pass

        # 4. persist version
        version_file.write_text(new_version, encoding="utf-8")
        log(f"Updated to v{new_version}; restart the agent to activate")
        return True
    except OSError as e:
        log(f"Update failed, keeping old files: {e}")
        return False


def check_and_update(server: str, api_key: str, agent: str, mcp_dir: Path,
                     version_file: Path, log) -> bool:
    """Full check-and-apply cycle. Returns True when an update was applied."""
    local_v = local_version(version_file)
    manifest = fetch_manifest(server, api_key, agent, local_v)
    if manifest is None:
        return False  # server unreachable / no manifest endpoint: skip
    if not manifest.get("update_available"):
        return False
    new_version = manifest.get("version") or ""
    if not new_version:
        return False
    archive = fetch_archive(server, api_key, agent)
    if archive is None:
        log("Update download failed, keeping current version")
        return False
    files = verify_archive(archive, manifest.get("files") or [])
    if files is None:
        log("Update verification failed (hash mismatch), keeping current version")
        return False
    return apply_update(files, Path(mcp_dir), Path(version_file),
                        new_version, log)
