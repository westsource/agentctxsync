"""
Agent Context Sync MCP Server (multi-agent)
- Adapter framework: HERMES_SYNC_AGENT selects the local store adapter
  (hermes | deepseek-harness | opencode | reasonix | openclaw); every
  agent gets its own deployment (each instance manages one local store)
- Auto-pull on startup (background); bootstrap push when remote is empty
- Periodic sync every N minutes (HERMES_SYNC_INTERVAL)
- Tools: sync_status / sync_pull / sync_push / sync_full (+ hermes_sync_*
  aliases for backwards compatibility)
- Authenticated via workspace API key
"""

import asyncio
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# Allow running as ``python <abs path>/server.py`` from ANY working
# directory: host agents (Hermes, reasonix plugins, ...) do not guarantee
# the cwd is the mcp/ folder, and ``from adapters import ...`` below needs
# this package on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# mcp SDK v2 (the 2026-07-28 spec rework, mcp>=2.0.0) removed the low-level
# decorator API: ``Server.list_tools()`` / ``Server.call_tool()`` no longer
# exist, handlers register via ``add_request_handler`` with ``(ctx, params)
# -> result`` signatures. Detect the era once; the v1 path keeps the
# historical decorators, the v2 path registers the same handlers with v2
# signatures. Everything else (Server, stdio_server, mcp.types, run(),
# create_initialization_options()) is shared by both.
SDK_V2 = not hasattr(Server, "list_tools")

from adapters import get_adapter, available_agents
from adapters.base import (AGENT_PREFIXES, PROJECT_USER_EDIT_FIELDS,
                           USER_EDIT_FIELDS, align_path_to_local,
                           build_path_map)
import updater


SYNC_SERVER = os.environ.get("HERMES_SYNC_SERVER", "https://www.agentctxsync.com")
SYNC_API_KEY = os.environ.get("HERMES_SYNC_API_KEY", "hsk_placeholder")
SYNC_INTERVAL = int(os.environ.get("HERMES_SYNC_INTERVAL", "300"))
# Background auto-sync (startup pull + periodic sync) can be disabled so the
# client never competes for the local store locks; manual tool calls still
# work. Hermes on SQLite < 3.51.3 uses journal_mode=DELETE where a write
# blocks concurrent readers -- set to 0 to keep Hermes' own reads (e.g.
# session.resume) entirely lock-free.
AUTO_SYNC = os.environ.get("HERMES_SYNC_AUTO_SYNC", "1") != "0"
# Client auto-update: check once shortly after startup, then every
# HERMES_SYNC_UPDATE_INTERVAL seconds (default 1h). Files are replaced in
# the background and take effect on the next agent restart. Set
# HERMES_SYNC_AUTO_UPDATE=0 to disable.
AUTO_UPDATE = os.environ.get("HERMES_SYNC_AUTO_UPDATE", "1") != "0"
UPDATE_INTERVAL = int(os.environ.get("HERMES_SYNC_UPDATE_INTERVAL", "3600"))
MCP_DIR = Path(__file__).resolve().parent
VERSION_FILE = MCP_DIR / updater.VERSION_FILE_NAME
AGENT = os.environ.get("HERMES_SYNC_AGENT", "opencode")
if AGENT not in available_agents():
    sys.stderr.write(
        f"[hermes-sync] Unknown agent {AGENT!r}; "
        f"falling back to 'hermes'. Known agents: {available_agents()}\n")
    AGENT = "hermes"
adapter = get_adapter(AGENT)
DEVICE_ID = f"local-{os.environ.get('COMPUTERNAME', 'unknown')}"
# Version of this client installation (persisted by the updater, falling
# back to the built-in constant). Sent with every sync request so the
# server can show each device's MCP client version at last sync.
CLIENT_VERSION = updater.local_version(VERSION_FILE)

# Single-writer guard for the BACKGROUND sync loops (startup pull + periodic
# sync). The Hermes desktop app runs two `serve` instances (Hermes.exe →
# serve(venv) → serve(.hermes-runtime)), each spawning its own copy of this
# MCP server — so two processes can otherwise run the sync concurrently and
# race on the same local store. Only the process holding the lockfile runs
# the background loops; the other skips. Explicit tool calls are NOT guarded.
# Lock files are per-agent so independent agent deployments never block each
# other (hermes keeps its legacy lock name).
_lock_name = "hermes-sync" if AGENT == "hermes" else f"hermes-sync-{AGENT}"
LOCK_FILE = Path(os.environ.get(
    "HERMES_SYNC_LOCK_FILE",
    str(Path.home() / "AppData/Local/hermes" / (_lock_name + ".lock"))))
UPDATE_LOCK_FILE = Path(os.environ.get(
    "HERMES_SYNC_UPDATE_LOCK_FILE",
    str(Path.home() / "AppData/Local/hermes" / (_lock_name + "-update.lock"))))

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def _try_acquire_lock(lock_path: Path | None = None) -> bool:
    """Create the lockfile atomically (O_EXCL). Steal it if the previous
    holder's PID is dead (crashed process would otherwise block sync forever)."""
    lock_path = lock_path or LOCK_FILE
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            holder = int(lock_path.read_text().strip())
        except Exception:
            return False
        if not _pid_alive(holder):
            try:
                lock_path.unlink()
            except OSError:
                return False
            return _try_acquire_lock(lock_path)
        return False

def _release_lock(lock_path: Path | None = None):
    lock_path = lock_path or LOCK_FILE
    try:
        lock_path.unlink()
    except OSError:
        pass

class _SyncServer(Server):
    """MCP server that keeps a handle on the active session so background
    sync tasks can push log notifications (notifications/message) to the
    host agent. Hosts may or may not surface them in their UI — they are
    best-effort; failures are swallowed."""

    def __init__(self, name: str):
        super().__init__(name)
        self.active_session = None
        self._session_ready = asyncio.Event()
        if SDK_V2:
            # v2 hands us no run() hook that exposes the connection session,
            # so capture the per-request ServerSession on the first inbound
            # message (request or notification). Background sync
            # notifications then use its standalone outbound channel -- the
            # same shape v1's active_session had. The middleware list only
            # exists on v2.
            async def _capture_session(ctx, call_next):
                self.active_session = ctx.session
                self._session_ready.set()
                return await call_next(ctx)
            self.middleware.append(_capture_session)

    async def run(self, read_stream, write_stream, initialization_options,
                  raise_exceptions: bool = False, stateless: bool = False):
        if SDK_V2:
            # v2 SDK owns the connection loop (handshake + modern eras); the
            # capture middleware above already stashed the session. Its
            # run() takes no `stateless` kwarg.
            await super().run(read_stream, write_stream, initialization_options,
                              raise_exceptions=raise_exceptions)
            return
        # v1: mirrors mcp.server.Server.run (mcp SDK 1.28.1) so we can
        # capture the ServerSession for background notifications.
        from contextlib import AsyncExitStack
        import anyio
        from mcp.server.session import ServerSession
        async with AsyncExitStack() as stack:
            lifespan_context = await stack.enter_async_context(self.lifespan(self))
            self.active_session = await stack.enter_async_context(
                ServerSession(read_stream, write_stream, initialization_options,
                              stateless=stateless))
            self._session_ready.set()
            task_support = (self._experimental_handlers.task_support
                            if self._experimental_handlers else None)
            if task_support is not None:
                task_support.configure_session(self.active_session,
                                               stateless=stateless)
                await stack.enter_async_context(task_support.run())
            async with anyio.create_task_group() as tg:
                try:
                    async for message in self.active_session.incoming_messages:
                        tg.start_soon(self._handle_message, message,
                                      self.active_session, lifespan_context,
                                      raise_exceptions)
                finally:
                    tg.cancel_scope.cancel()
            self.active_session = None


server = _SyncServer("hermes-session-sync")


async def _notify_host(message: str, level: str = "info"):
    """Best-effort MCP log notification after background sync. Waits up to
    30s for the session (startup sync may finish before the stdio handshake)
    and never raises: hosts that don't surface log notifications just ignore
    them."""
    try:
        await asyncio.wait_for(server._session_ready.wait(), timeout=30)
        if server.active_session is None:
            return
        from mcp.types import (LoggingMessageNotification,
                               LoggingMessageNotificationParams)
        await server.active_session.send_notification(LoggingMessageNotification(
            params=LoggingMessageNotificationParams(
                level=level, logger="hermes-sync", data=message)))
    except Exception:
        pass

def log(msg):
    sys.stderr.write(f"[hermes-sync] {msg}\n")
    sys.stderr.flush()

def api_call(method, path, data=None):
    url = f"{SYNC_SERVER}{path}"
    headers = {"Authorization": f"Bearer {SYNC_API_KEY}", "Content-Type": "application/json",
               "User-Agent": "hermes-sync-client/1.0"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()}
    except Exception as e:
        return {"error": str(e)}

# Quota rejections come back as HTTP 403 with our machine-readable code in
# {"detail": "<code>"}. Translate them into a human-readable hint so the
# Agent user understands WHY the push was refused, without losing the code.
QUOTA_ERROR_HINTS = {
    "agent_not_allowed": "同步被拒：当前套餐不支持该 Agent 类型的会话同步（如需启用请联系管理员）",
    "quota_exceeded_sessions": "同步被拒：会话数量已达当前套餐上限，请清理旧会话或升级套餐",
}

def explain_quota_error(result):
    """Return ``result`` with a friendly ``error`` hint when it is a quota
    rejection (403 + our code), keeping the machine-readable ``code``."""
    if not isinstance(result, dict) or result.get("error") != 403:
        return result
    try:
        detail = json.loads(result.get("detail", "{}")).get("detail", "")
    except Exception:
        return result
    hint = QUOTA_ERROR_HINTS.get(detail)
    if hint:
        return {**result, "error": hint, "code": detail}
    return result

# ---- Field-level optimistic concurrency sidecar (see ARCHITECTURE.md) ----
# Per-agent persistent store of {session_id: {field: {"base": server_rev,
# "val": last_known_value}}}. base is the server's per-field logical clock;
# a field is "dirty" when the local store value differs from "val". Lazy
# populate: entries appear only after the first pull/push contact. Absent
# entry == base unknown -> the server stays authoritative for that field.
PROJECT_FIELD_META_PATH = getattr(adapter, "project_field_meta_path",
                                  lambda: None)()
FIELD_META_PATH = getattr(adapter, "field_meta_path", lambda: None)()
# Push fingerprint sidecar (sibling of the field meta): {session_id:
# [message_count, max_timestamp, mtime]} recorded after a successful push.
# The push loop skips sessions whose fingerprint is unchanged, so a full
# store is not re-uploaded every cycle. `None` (no field meta) disables
# the optimization and falls back to push-everything.
PUSH_FINGERPRINT_PATH = None
if FIELD_META_PATH is not None:
    PUSH_FINGERPRINT_PATH = FIELD_META_PATH.with_name(
        FIELD_META_PATH.stem + "-push-fingerprint.json")


def _load_push_fingerprint() -> dict:
    if PUSH_FINGERPRINT_PATH is None:
        return {}
    try:
        data = json.loads(PUSH_FINGERPRINT_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_push_fingerprint(fp: dict):
    if PUSH_FINGERPRINT_PATH is None:
        return
    try:
        PUSH_FINGERPRINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        PUSH_FINGERPRINT_PATH.write_text(json.dumps(fp, ensure_ascii=False),
                                         encoding="utf-8")
    except OSError:
        pass


def _session_fingerprint(s: dict) -> tuple:
    """Fingerprint of one session for the push skip-check: message count +
    max message timestamp, plus the backing file mtime when the adapter
    provides it (covers in-place edits that change neither)."""
    msgs = s.get("messages") or []
    count = len(msgs)
    max_ts = max((m.get("timestamp") or 0 for m in msgs), default=0)
    fp: tuple = (count, max_ts)
    mtime_fn = getattr(adapter, "session_mtime", None)
    if mtime_fn is not None:
        try:
            m = mtime_fn(str(s["id"]))
            if m is not None:
                fp = (count, max_ts, round(m, 3))
        except Exception:
            pass
    return fp


def _load_field_meta():
    if FIELD_META_PATH is None:
        return {}
    try:
        data = json.loads(FIELD_META_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_field_meta(meta: dict):
    if FIELD_META_PATH is None:
        return
    try:
        FIELD_META_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIELD_META_PATH.write_text(json.dumps(meta, ensure_ascii=False),
                                   encoding="utf-8")
    except OSError:
        pass


def _annotate_push_session(s, meta: dict):
    """Return a push copy of session ``s`` containing only the user-edit
    fields this device is asserting (dirty / first-contact), tagged with
    their base in ``field_meta``. Non-dirty user-edit fields are omitted so
    the server never overwrites them from this (possibly stale) device."""
    sid = str(s["id"])
    sm = meta.get(sid, {})
    out, field_meta = {}, {}
    for k, v in s.items():
        if k in USER_EDIT_FIELDS:
            entry = sm.get(k)
            if entry is None:
                field_meta[k] = None            # base unknown -> server authority
                out[k] = v
            elif v == entry.get("val"):
                continue                         # not dirty: omit entirely
            else:
                field_meta[k] = entry.get("base")  # dirty: push with known base
                out[k] = v
        else:
            out[k] = v
    out["field_meta"] = field_meta
    return out


def _anchor_push_meta(meta: dict, chunk, session_revs):
    """After a successful push, record the accepted base/val per field so the
    device stops treating them as dirty. base=None fields (server-authoritative
    once) are NOT anchored -- the next pull adopts the server value instead."""
    for so in chunk:
        sid = str(so["id"])
        revmap = (session_revs.get(sid) or {}).get("field_rev") or {}
        for f, B in (so.get("field_meta") or {}).items():
            if B is not None and f in revmap and f in so:
                meta.setdefault(sid, {})[f] = {"base": revmap[f], "val": so[f]}


def _load_project_field_meta():
    if PROJECT_FIELD_META_PATH is None:
        return {}
    try:
        data = json.loads(PROJECT_FIELD_META_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_project_field_meta(meta: dict):
    if PROJECT_FIELD_META_PATH is None:
        return
    try:
        PROJECT_FIELD_META_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROJECT_FIELD_META_PATH.write_text(json.dumps(meta, ensure_ascii=False),
                                           encoding="utf-8")
    except OSError:
        pass


def _annotate_push_project(p, meta: dict) -> dict:
    """Push copy of project ``p`` asserting only dirty / first-contact scalar
    user-edit fields (tagged with their base in ``field_meta``). Non-dirty
    fields are omitted so this device never overwrites a peer's value."""
    pid = str(p["id"])
    pm = meta.get(pid, {})
    out, field_meta = {}, {}
    for k, v in p.items():
        if k == "folders":
            out["folders"] = v
            continue
        if k in PROJECT_USER_EDIT_FIELDS:
            entry = pm.get(k)
            if entry is None:
                field_meta[k] = None            # base unknown -> server authority
            elif v == entry.get("val"):
                continue                         # not dirty: omit
            else:
                field_meta[k] = entry.get("base")  # dirty: known base
            out[k] = v
        else:
            out[k] = v
    out["field_meta"] = field_meta
    return out


def _anchor_push_project_meta(meta: dict, projects, project_revs):
    """Record accepted base/val after a successful project push so those
    fields stop reading dirty. base=None fields are NOT anchored -- the next
    pull adopts the server value instead."""
    for po in projects:
        pid = str(po["id"])
        revmap = (project_revs.get(pid) or {}).get("field_rev") or {}
        for f, B in (po.get("field_meta") or {}).items():
            if B is not None and f in revmap and f in po:
                meta.setdefault(pid, {})[f] = {"base": revmap[f], "val": po[f]}


def pull_sessions(last_sync_at=None, limit=None):
    """Pull remote sessions into the local store via the agent adapter.

    ``last_sync_at=None`` (default) pulls incrementally from the adapter's
    own ``last_synced_at()`` watermark; pass 0 for a full pull. Paginates
    over the remote /pull endpoint so ALL sessions are fetched; ``limit=N``
    caps the total (the MCP tool uses this). Each page is written with
    stable (session_id, role, timestamp) dedupe in the adapter; remote
    message ids are dropped so the local store assigns fresh ids.
    """
    if adapter.discover() is None:
        return {"error": f"Local store not found for agent {AGENT}"}
    if last_sync_at is None:
        # Incremental from the local watermark, with a 5-min grace window:
        # the watermark is a LOCAL timestamp while remote last_synced_at
        # values are written by OTHER devices' clocks, so a strict cutoff
        # could silently skip sessions pushed by a clock-skewed peer.
        last_sync_at = max(0.0, adapter.last_synced_at() - 300)

    imported, new_messages = 0, 0
    total_remote = 0

    # One pull request returns `limit` FULL sessions (all messages); a big
    # batch is slow and risks the HTTP timeout (observed: ~60s for a 50-
    # session page). Keep pages small so a full resync never times out.
    PAGE = 15
    fetched = 0
    prev_page_ids = None
    meta = _load_field_meta()
    # Snapshot local user-edit values once: a pull must NEVER overwrite a
    # field this device edited locally since its last sync (dirty == local
    # value != sidecar last-known). The same read also feeds the
    # path-separator alignment below.
    _local = adapter.read_sessions()
    local_by_id = {str(s["id"]): s for s in _local}
    local_cwd_map = build_path_map(
        s.get("cwd") for s in _local if isinstance(s.get("cwd"), str))
    while True:
        page_limit = PAGE if limit is None else max(min(PAGE, limit - fetched), 0)
        if page_limit <= 0:
            break
        result = api_call("POST", "/pull", {
            "device_id": DEVICE_ID,
            "client_version": CLIENT_VERSION,
            "agent": AGENT,
            "last_sync_at": last_sync_at, "limit": page_limit, "offset": fetched,
            # Full-pool pull: no agent filter (decision record 2026.08.22.4,
            # see docs/ARCHITECTURE.md "全池拉取契约"). Every client in the
            # workspace pulls ALL sessions (every agent) and pushes only its
            # own; the server deliberately ignores the `agent` field on pull
            # and merges by canonical id. Do NOT rely on it to filter — it is
            # sent for device/version reporting only.
        })
        if "error" in result:
            if fetched == 0:
                _save_field_meta(meta)
                return result
            _save_field_meta(meta)
            return {"imported": imported, "new_messages": new_messages,
                    "total_remote_sessions": total_remote,
                    "error": f"partial pull after {fetched} sessions: {result['error']}"}

        sessions = result.get("sessions", [])
        total_remote = result.get("total_sessions") or result.get("session_count") or 0
        if not sessions:
            break
        # Guard against a server that ignores `offset` and repeats the same
        # page forever (old deployments): stop when the page is unchanged.
        page_ids = tuple(s["id"] for s in sessions)
        if page_ids == prev_page_ids:
            break
        prev_page_ids = page_ids

        # Field-level merge BEFORE writing: never overwrite a user-edit field
        # this device changed locally since its last sync (dirty). The server
        # stays authoritative for both unanchored fields (no sidecar entry)
        # and fields we do not locally differ on -- those we adopt here.
        for s in sessions:
            sid = str(s["id"])
            fr = s.get("field_rev") or {}
            sm = meta.get(sid) or {}
            local = local_by_id.get(sid, {})
            for f in USER_EDIT_FIELDS:
                if f in s and isinstance(s.get(f), str):
                    entry = sm.get(f)
                    if entry is not None and local.get(f) != entry.get("val"):
                        s.pop(f, None)   # locally edited since last sync
            # Align pull-side path fields (cwd/git_repo_root) to the local
            # separator spelling where a local path already exists with only
            # a different separator — keeps local storage consistent and
            # merges instead of splitting the same path.
            for _pk in ("cwd", "git_repo_root"):
                if isinstance(s.get(_pk), str) and s[_pk]:
                    s[_pk] = align_path_to_local(s[_pk], local_cwd_map)
        # Retry the write a few times when the local store is locked by the
        # host agent (Hermes on SQLite < 3.51.3 uses journal_mode=DELETE,
        # where a write contends with concurrent readers). Each attempt
        # fails fast (busy_timeout=5s); short gaps between attempts catch
        # brief idle windows instead of deferring to the next sync cycle.
        stats = None
        for gap in (0, 2, 5, 10):
            if gap:
                log(f"Local store locked during pull write; "
                    f"retrying in {gap}s...")
                time.sleep(gap)
            try:
                stats = adapter.write_sessions(sessions)
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or gap == 10:
                    raise
        imported += stats.get("imported", 0)
        new_messages += stats.get("new_messages", 0)

        # Anchor sidecar base/val for fields adopted from the server (they
        # now read clean), so this device stops treating them as dirty and
        # does not needlessly re-push them.
        for s in sessions:
            sid = str(s["id"])
            fr = s.get("field_rev") or {}
            for f in USER_EDIT_FIELDS:
                if f in s and isinstance(s.get(f), str) and f in fr:
                    meta.setdefault(sid, {})[f] = {"base": fr[f], "val": s[f]}

        fetched += len(sessions)
        if len(sessions) < page_limit:
            break  # last page
        if limit is not None and fetched >= limit:
            break

    if "error" not in result and result.get("sync_at"):
        adapter.save_sync_watermark(result["sync_at"])
    _save_field_meta(meta)
    return {"imported": imported, "new_messages": new_messages,
            "total_remote_sessions": total_remote}

def push_sessions():
    if adapter.discover() is None:
        return {"error": f"Local store not found for agent {AGENT}"}

    sessions_data = adapter.read_sessions()
    if not sessions_data:
        return {"message": "No local sessions to push"}
    # Push EVERYTHING the local store holds, own sessions and foreign ones
    # pulled from the shared pool. A foreign session may have been continued
    # locally (e.g. a hermes session edited in workbuddy gains new messages)
    # and those additions MUST flow back to the server. Tag each session
    # with the agent that OWNS it: with the prefix-free id scheme the owner
    # is the adapter's own agent_type for local sessions and the recorded
    # owner in the foreign-id registry for pulled ones (the server never
    # overwrites agent_type on re-push, so re-pushing pulled content is
    # idempotent and only locally-added messages insert).
    for s in sessions_data:
        sid = str(s["id"])
        agent = adapter.agent_type
        if adapter._is_foreign(sid):
            agent = adapter._foreign_agent(sid) or "hermes"
        s["agent_type"] = agent

    # Batch pushes so each request stays small and fast: the remote server
    # does a per-message dedup SELECT for every row, so one giant request
    # (50 sessions, thousands of messages) can exceed the HTTP timeout as
    # the workspace grows (observed: 31s on a ~6k-message push with a 30s
    # timeout). Push the WHOLE local store (the pool contract: "pushes
    # everything it holds"); _chunk_sessions bounds every request by
    # session count + message count, so large stores never time out and
    # no session starves behind a per-cycle cap.
    totals = {"imported": 0, "updated": 0, "new_messages": 0, "sync_at": None}
    processed = 0
    errors: list[str] = []
    meta = _load_field_meta()
    fp = _load_push_fingerprint()
    try:
        for chunk in _chunk_sessions(sessions_data):
            # Skip sessions whose fingerprint is unchanged since the last
            # successful push (see _session_fingerprint): the server dedupes
            # re-pushes, but a full store re-upload every cycle is wasteful
            # (measured 1.6GB/cycle here). Fingerprints update only on
            # success, so a failed push retries next cycle.
            changed = [s for s in chunk
                       if _session_fingerprint(s)
                       != tuple(fp.get(str(s["id"])) or ())]
            if not changed:
                continue
            # field-level merge: only dirty/first-contact user-edit fields are
            # asserted (others omitted so this device never clobbers a peer)
            outgoing = [_annotate_push_session(s, meta) for s in changed]
            result = api_call("POST", "/push",
                              {"device_id": DEVICE_ID,
                               "client_version": CLIENT_VERSION,
                               "agent": AGENT,
                               "sessions": outgoing})
            if "error" in result:
                # A failing chunk (413 oversized payload, quota, timeout) must
                # NOT starve the rest of the store: record it and keep pushing
                # so sessions behind the failing one still sync this cycle.
                result = explain_quota_error(result)
                err = result.get("error")
                errors.append(f"{len(changed)} sessions: {err}")
                log(f"Push chunk failed ({len(changed)} sessions, "
                    f"{len(errors)} failed so far): {err}")
                continue
            # anchor accepted base/val so these fields stop reading dirty
            _anchor_push_meta(meta, outgoing, result.get("session_revs") or {})
            for k in ("imported", "updated", "new_messages"):
                totals[k] += result.get(k, 0)
            totals["sync_at"] = result.get("sync_at", totals["sync_at"])
            processed += len(changed)
            for s in changed:
                fp[str(s["id"])] = _session_fingerprint(s)
    finally:
        _save_field_meta(meta)
        _save_push_fingerprint(fp)
    if errors:
        return {**totals, "error": f"{len(errors)} chunk(s) failed: "
                                f"{'; '.join(errors[:3])}"
                                + ("..." if len(errors) > 3 else "")}
    return totals

def _chunk_sessions(sessions: list[dict],
                    max_sessions: int = 20,
                    max_messages: int = 3000,
                    max_bytes: int = 8 * 1024 * 1024) -> list[list[dict]]:
    """Split sessions into push batches bounded by session count, total
    message count AND approximate payload bytes. A few huge sessions (e.g. a
    10MB workbuddy session with thousands of events) must not ride along
    with a full batch of average ones -- the per-message dedup on the server
    makes one request cost scale with message count, and a giant request
    times out or trips the proxy body limit (413). A single session larger
    than any bound gets its own batch (a session cannot be split)."""
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_msgs = 0
    cur_bytes = 0
    sizes: dict[str, int] = {}
    def _size(s: dict) -> int:
        sid = str(s["id"])
        if sid not in sizes:
            sizes[sid] = len(json.dumps(s, ensure_ascii=False).encode("utf-8"))
        return sizes[sid]
    for s in sessions:
        msgs = len(s.get("messages") or [])
        sz = _size(s)
        if cur and (len(cur) >= max_sessions or cur_msgs + msgs > max_messages
                    or cur_bytes + sz > max_bytes):
            chunks.append(cur)
            cur, cur_msgs, cur_bytes = [], 0, 0
        cur.append(s)
        cur_msgs += msgs
        cur_bytes += sz
    if cur:
        chunks.append(cur)
    return chunks

def full_sync():
    # pull first, then push: the pull anchors this device's per-field base
    # (and adopts server values) before it pushes only its dirty fields, so
    # a device's own state never clobbers a peer's newer metadata and an
    # un-pushed local edit is never overwritten by the pull. (Field-level
    # optimistic concurrency -- see ARCHITECTURE.md.)
    pull_result = pull_sessions()
    push_result = push_sessions()
    return {"push": push_result, "pull": pull_result}

def push_projects():
    """Push local projects (all profiles) to the server.

    Field-level optimistic merge (Phase 2): only dirty / first-contact scalar
    user-edit project fields (name/primary_path/archived/description) are
    asserted, tagged with their base. Non-dirty fields and folders are omitted
    so this device never overwrites a peer's newer metadata; folders remain
    server-merged by union (multidevice coexist)."""
    if adapter.discover() is None:
        return {"error": f"Local store not found for agent {AGENT}"}
    projects = adapter.read_projects()
    if not projects:
        return {"message": "No local projects to push"}
    meta = _load_project_field_meta()
    outgoing = [_annotate_push_project(p, meta) for p in projects]
    result = api_call("POST", "/api/projects/push",
                      {"device_id": DEVICE_ID,
                       "client_version": CLIENT_VERSION,
                       "agent": AGENT,
                       "projects": outgoing})
    if "error" not in result:
        _anchor_push_project_meta(meta, outgoing,
                                  result.get("project_revs") or {})
    _save_project_field_meta(meta)
    return result

def pull_projects():
    """Pull remote projects + remap records into local projects.db.

    Field-level merge (Phase 2): a locally-dirty scalar user-edit project
    field is kept (never overwritten by the pull) and pushed next cycle;
    fields we do not locally differ on are adopted and their base anchored."""
    if adapter.discover() is None:
        return {"error": f"Local store not found for agent {AGENT}"}
    meta = _load_project_field_meta()
    try:
        # Snapshot local project scalar values once for dirty detection and
        # local separator spellings for path alignment.
        local_projects = [dict(p) for p in (adapter.read_projects() or [])]
        local_proj = {str(p["id"]): p for p in local_projects}
        local_paths = set()
        for _lp in local_projects:
            if isinstance(_lp.get("primary_path"), str) and _lp["primary_path"]:
                local_paths.add(_lp["primary_path"])
            for _f in _lp.get("folders", []) or []:
                if isinstance(_f.get("path"), str) and _f["path"]:
                    local_paths.add(_f["path"])
        # Full-pool pull (decision record 2026.08.22.4): the server returns
        # the workspace's ENTIRE visible project set regardless of `agent` —
        # the field is for device/version reporting only, never a filter.
        result = api_call("POST", "/api/projects/pull",
                          {"device_id": DEVICE_ID,
                           "client_version": CLIENT_VERSION,
                           "agent": AGENT})
        if "error" in result:
            return result
        projects = result.get("projects", []) or []
        for p in projects:
            pid = str(p["id"])
            sm = meta.get(pid) or {}
            local = local_proj.get(pid, {})
            fr = p.get("field_rev") or {}
            # skip locally-dirty scalar fields (keep local; push next cycle)
            for f in PROJECT_USER_EDIT_FIELDS:
                if f in p and isinstance(p.get(f), str):
                    entry = sm.get(f)
                    if entry is not None and local.get(f) != entry.get("val"):
                        p.pop(f, None)
            # Align remaining pull-side paths to the local separator spelling
            # where a local path already exists modulo separators, so a folder
            # path that already exists locally is updated/merged instead of
            # inserted as a duplicate spelling.
            if isinstance(p.get("primary_path"), str) and p["primary_path"]:
                p["primary_path"] = align_path_to_local(
                    p["primary_path"], local_paths)
            for f in p.get("folders", []) or []:
                if isinstance(f.get("path"), str) and f["path"]:
                    f["path"] = align_path_to_local(f["path"], local_paths)
            # anchor adopted fields so they stop reading dirty
            for f in PROJECT_USER_EDIT_FIELDS:
                if f in p and isinstance(p.get(f), str) and f in fr:
                    meta.setdefault(pid, {})[f] = {"base": fr[f], "val": p[f]}
        stats = adapter.write_projects(projects, result.get("remaps", []))
        return {"imported": stats.get("imported", 0),
                "projects": len(projects)}
    finally:
        _save_project_field_meta(meta)

# Tool names: neutral `sync_*` for any agent; `hermes_sync_*` aliases kept
# for existing Hermes registrations.
TOOL_SPECS = [
    ("sync_status", "Show sync status: local store totals and remote server status."),
    ("sync_pull", "Pull latest sessions from remote server into the local store.",
     {"limit": {"type": "integer", "description": "Max sessions to pull (default: 50; a full pull is uncapped unless set)"},
      "full": {"type": "boolean", "description": "Full pull ignoring the sync watermark (default: false)"}}),
    ("sync_push", "Push local sessions to remote server."),
    ("sync_full", "Full sync: push local changes then pull remote changes."),
    ("project_push", "Push local projects (all profiles) to the remote server."),
    ("project_pull", "Pull remote projects into the local projects.db (applies remaps)."),
]

def _build_tools() -> list[Tool]:
    """MCP tool surface: canonical sync_* plus hermes_sync_* aliases."""
    tools = []
    for name, desc, *rest in TOOL_SPECS:
        props = rest[0] if rest else {}
        tools.append(Tool(name=name, description=desc,
                          inputSchema={"type": "object", "properties": props}))
        # aliases (hermes_sync_*)
        tools.append(Tool(name="hermes_" + name, description=desc + " (alias)",
                          inputSchema={"type": "object", "properties": props}))
    return tools


async def _dispatch_tool(name: str, arguments: dict) -> str:
    """Run one tool, returning the human-readable result text."""
    loop = asyncio.get_event_loop()
    base = name[len("hermes_"):] if name.startswith("hermes_") else name
    if base == "sync_status":
        local = adapter.status()
        remote = api_call("GET", f"/status/{DEVICE_ID}")
        result = {"agent": adapter.agent_type, "local": local, "remote": remote}
        text = json.dumps(result, indent=2, ensure_ascii=False)
    elif base == "sync_pull":
        # A FULL pull means "everything": the default limit=50 would
        # silently cap it and drop the oldest sessions. Only cap when the
        # caller explicitly passes `limit`.
        limit = arguments.get("limit")
        full = bool(arguments.get("full", False))
        if limit is None:
            limit = None if full else 50
        result = await loop.run_in_executor(
            None, lambda: pull_sessions(last_sync_at=0 if full else None,
                                        limit=limit))
        text = json.dumps(result, indent=2, ensure_ascii=False)
    elif base == "sync_push":
        result = await loop.run_in_executor(None, push_sessions)
        text = json.dumps(result, indent=2, ensure_ascii=False)
    elif base == "sync_full":
        result = await loop.run_in_executor(None, full_sync)
        text = json.dumps(result, indent=2, ensure_ascii=False)
    elif base == "project_push":
        result = await loop.run_in_executor(None, push_projects)
        text = json.dumps(result, indent=2, ensure_ascii=False)
    elif base == "project_pull":
        result = await loop.run_in_executor(None, pull_projects)
        text = json.dumps(result, indent=2, ensure_ascii=False)
    else:
        raise ValueError(f"Unknown tool: {name}")
    return text


if SDK_V2:
    # v2 handlers: (ctx, params) -> typed result, registered by method name
    # (PaginatedRequestParams / CallToolRequestParams are v2-era wire types).
    from mcp.types import (CallToolRequestParams, CallToolResult,
                           ListToolsResult, PaginatedRequestParams)

    async def list_tools(ctx, params: PaginatedRequestParams) -> ListToolsResult:
        return ListToolsResult(tools=_build_tools())

    async def call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
        text = await _dispatch_tool(params.name, dict(params.arguments or {}))
        return CallToolResult(content=[TextContent(type="text", text=text)])

    server.add_request_handler("tools/list", PaginatedRequestParams, list_tools)
    server.add_request_handler("tools/call", CallToolRequestParams, call_tool)
else:
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return _build_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        text = await _dispatch_tool(name, arguments)
        return [TextContent(type="text", text=text)]

async def periodic_sync():
    if not AUTO_SYNC:
        log("Background periodic sync disabled (HERMES_SYNC_AUTO_SYNC=0)")
        return
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        if not _try_acquire_lock():
            log("Periodic sync skipped: another server process holds the lock")
            continue
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, full_sync)
            imported = result.get("pull", {}).get("imported", 0)
            pushed = result.get("push", {}).get("imported", 0) + result.get("push", {}).get("updated", 0)
            msgs = result.get("pull", {}).get("new_messages", 0)
            log(f"Periodic sync: pulled {imported} sessions, pushed {pushed} sessions")
            if "error" in result.get("pull", {}) or "error" in result.get("push", {}):
                await _notify_host(f"Sync finished with errors: {result}",
                                   level="warning")
            else:
                await _notify_host(
                    f"Sync complete: pulled {imported} session(s), "
                    f"pushed {pushed} session(s), {msgs} new message(s)")
            # projects sync (same cycle, best-effort)
            try:
                pp = await loop.run_in_executor(None, push_projects)
                pl = await loop.run_in_executor(None, pull_projects)
                log(f"Projects sync: push={pp.get('imported', pp.get('updated', 0))}, "
                    f"pull={pl.get('projects', 0)}")
            except Exception as e:
                log(f"Projects sync error: {e}")
        except Exception as e:
            log(f"Periodic sync error: {e}")
        finally:
            _release_lock()

async def background_startup_sync():
    if not AUTO_SYNC:
        log("Background startup sync disabled (HERMES_SYNC_AUTO_SYNC=0)")
        return
    # Delay the first sync so the host agent's own startup/read burst (e.g.
    # Hermes session.resume) has finished before we take SQLite locks.
    await asyncio.sleep(8)
    log(f"Starting, auto-syncing from {SYNC_SERVER} (agent: {adapter.agent_type})...")
    if not _try_acquire_lock():
        log("Initial sync skipped: another server process holds the lock")
        return
    try:
        loop = asyncio.get_event_loop()
        # pull first, then push (matches periodic full_sync). The pull
        # anchors this device's per-field base/adopts server values BEFORE
        # it pushes only its genuinely-dirty fields, so:
        #   * a local move (cwd -> new project) is never overwritten by the
        #     pull (it is locally dirty and excluded), and it is pushed next;
        #   * a stale/unmodified device never clobbers a peer's newer value
        #     (only dirty fields are pushed; the server rejects unknown-base
        #     writes). Call this the field-level optimistic concurrency model
        #     (see docs/ARCHITECTURE.md).
        # An empty remote on a fresh pairing is seeded naturally: pull returns
        # nothing, push inserts this device's sessions (idempotent dedupe).
        result = await loop.run_in_executor(None, pull_sessions)
        push_result = await loop.run_in_executor(None, push_sessions)
        push_err = push_result.get("error") if isinstance(push_result, dict) else None
        log(f"Initial sync: pull={result} | push={push_result}")
        if push_err or "error" in result:
            await _notify_host(
                f"Startup sync failed: pull={result.get('error')} "
                f"push={push_err or 'ok'}", level="warning")
        else:
            pushed = (push_result.get("imported", 0) + push_result.get("updated", 0)) \
                if isinstance(push_result, dict) else 0
            await _notify_host(
                f"Startup sync complete: pulled {result.get('imported', 0)} "
                f"session(s), pushed {pushed} session(s), "
                f"{result.get('new_messages', 0)} new message(s)")
    except Exception as e:
        log(f"Initial sync failed: {e}")
        await _notify_host(f"Startup sync failed: {e}", level="warning")
    finally:
        _release_lock()

def _run_update_check():
    if not AUTO_UPDATE:
        return False
    if not _try_acquire_lock(UPDATE_LOCK_FILE):
        log("Update check skipped: another server process holds the update lock")
        return False
    try:
        # synchronous urllib work; callers run this in an executor
        applied = updater.check_and_update(
            SYNC_SERVER, SYNC_API_KEY, AGENT, MCP_DIR, VERSION_FILE, log)
        if applied:
            log(f"Client updated to {updater.local_version(VERSION_FILE)}; "
                f"restart the agent to activate")
        return applied
    except Exception as e:
        log(f"Update check error: {e}")
        return False
    finally:
        _release_lock(UPDATE_LOCK_FILE)

async def background_update_check():
    """Lazy update check: once 1 minute after startup, then every
    UPDATE_INTERVAL seconds (default 1 hour). Replaced files activate on
    agent restart."""
    if not AUTO_UPDATE:
        log("Client auto-update disabled (HERMES_SYNC_AUTO_UPDATE=0)")
        return
    await asyncio.sleep(60)  # clear of the host agent's startup burst
    applied = await asyncio.get_event_loop().run_in_executor(
        None, _run_update_check)
    if applied:
        await _notify_host(
            f"Client updated to {updater.local_version(VERSION_FILE)} — "
            f"restart the agent to activate", level="notice")
    while True:
        await asyncio.sleep(UPDATE_INTERVAL)
        applied = await asyncio.get_event_loop().run_in_executor(
            None, _run_update_check)
        if applied:
            await _notify_host(
                f"Client updated to {updater.local_version(VERSION_FILE)} — "
                f"restart the agent to activate", level="notice")

async def main():
    asyncio.create_task(background_startup_sync())
    asyncio.create_task(periodic_sync())
    asyncio.create_task(background_update_check())
    log(f"Device: {DEVICE_ID}")
    log(f"Agent: {adapter.agent_type} (local store: {adapter.discover()})")
    log(f"Periodic sync enabled: every {SYNC_INTERVAL}s ({SYNC_INTERVAL//60}min)")
    log(f"Client version: {updater.local_version(VERSION_FILE)} "
        f"(auto-update: {'on' if AUTO_UPDATE else 'off'}, "
        f"check every {UPDATE_INTERVAL//3600}h)")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
