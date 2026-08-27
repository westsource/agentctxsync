"""
OpenClaw adapter.

Local store (OpenClaw 2026.7.x): ~/.openclaw/agents/<agentId>/sessions/
  - sessions.json            -- session index: {session_key: {sessionId,
                                sessionFile, sessionStartedAt, updatedAt, ...}}
  - <sessionId>.jsonl        -- per-session transcript (JSONL v3):
                                session header, model_change / custom events,
                                and message lines {"type":"message",
                                "message":{"role","content","timestamp",...}}

The gateway owns this store and reloads the index on change (mtime-based
cache), so sessions written here appear in the TUI and `sessions.list`
without a gateway restart. Writes are shaped exactly like the gateway's
own persistence: one index entry plus a transcript file with a chained
parentId message graph.

Write caveat: a RUNNING gateway may overwrite sessions.json when it
persists its own in-memory state, clobbering index entries this adapter
added while the gateway was up. Pulls are safe to repeat (dedupe), so
prefer syncing when OpenClaw is closed, or re-pull after a gateway
session write.

id scheme: the session key (e.g. "agent:main:main") is the local id;
canonical ids are bare (no prefix, new scheme).
"""

import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .base import Adapter, validate_local_id

_INDEX_NAME = "sessions.json"
_WATERMARK_NAME = ".hermes-sync-watermark"
_FOREIGN_NAME = ".hermes-sync-foreign-ids.json"
_MAX_TITLE_LEN = 80
_MS_EPOCH = 1e12


def _content_to_text(content) -> str:
    """Normalize OpenClaw message content (str or block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "text"
                    and block.get("text")):
                parts.append(block["text"])
        return "\n".join(parts)
    return str(content) if content is not None else ""


def _iso_ts(ts: float) -> str:
    """ISO-8601 UTC with 'Z' suffix and ms precision (gateway format)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (dt.strftime("%Y-%m-%dT%H:%M:%S.") +
            f"{int((ts - int(ts)) * 1000):03d}Z")



_SURROGATE_RE = re.compile("[\ud800-\udfff]")
#: session-key <rest> shapes that indicate a pooled (server-originated)
#: session rather than a locally-created OpenClaw session: hermes timestamp
#: ids (20260801_201638_8ab26b), reasonix rx-* ids, and foreign uuids.
#: Locally-created keys are short slugs ("main", "test1", "tui-<uuid>").
_POOL_ID_RE = re.compile(
    r"^(\d{8}_\d{6}_\w+|rx-\S+|"
    r"(?<!tui-)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})$"
)


def _sanitize_text(text) -> str:
    """Strip lone surrogates / invalid code points from server content.

    Tool output on the server can carry binary-ish bytes that arrive here
    as surrogate code points; json.dumps(ensure_ascii=False) passes them
    through and the UTF-8 file write then fails or produces a corrupt
    line, which the reader skips -- so the message is never deduped and
    every pull re-appends it. Replacing invalid bytes keeps writes safe.
    """
    if isinstance(text, str) and _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub("\ufffd", text)
    return text


class OpenClawAdapter(Adapter):
    """OpenClaw gateway session-store adapter (jsonl transcripts + index)."""

    agent_type = "openclaw"

    def __init__(self, store_dir: Path | str | None = None):
        self.sessions_dir = Path(store_dir) if store_dir else self.discover()

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    def discover(self) -> Path | None:
        root = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw"))
        agents_dir = root / "agents"
        if not agents_dir.is_dir():
            return None
        candidates = sorted(
            agents_dir.glob("*/sessions/sessions.json"),
            key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0].parent if candidates else None

    @property
    def _index_path(self) -> Path:
        return self.sessions_dir / _INDEX_NAME

    # ------------------------------------------------------------------
    # sidecars
    # ------------------------------------------------------------------
    def _watermark_file(self) -> Path | None:
        if self.sessions_dir:
            return self.sessions_dir / _WATERMARK_NAME
        return None

    def _foreign_ids_file(self) -> Path | None:
        if self.sessions_dir:
            return self.sessions_dir / _FOREIGN_NAME
        return None

    # ------------------------------------------------------------------
    # index
    # ------------------------------------------------------------------
    def _load_index(self) -> dict:
        if not self.sessions_dir or not self._index_path.exists():
            return {}
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_index(self, index: dict):
        tmp = self._index_path.with_name(
            self._index_path.name + ".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._index_path)

    # ------------------------------------------------------------------
    # transcripts
    # ------------------------------------------------------------------
    def _entry_file(self, entry: dict | None) -> Path | None:
        if not entry:
            return None
        sf = entry.get("sessionFile")
        if sf:
            p = Path(sf)
            if not p.is_absolute():
                p = self.sessions_dir / p
            if p.exists():
                return p
        sid = entry.get("sessionId")
        if sid:
            p = self.sessions_dir / f"{sid}.jsonl"
            if p.exists():
                return p
        return None

    def _iter_messages(self, path: Path):
        """Yield (line_id, role, content, timestamp_seconds) per message."""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "message":
                continue
            m = obj.get("message")
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            ts = m.get("timestamp")
            if not isinstance(ts, (int, float)):
                continue
            if ts > _MS_EPOCH:  # ms -> seconds
                ts = ts / 1000.0
            yield obj.get("id"), role, _content_to_text(content), ts

    def _tail_id(self, path: Path | None) -> str | None:
        """Last event id in a transcript (parent for appended lines)."""
        if path is None:
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("id"):
                return obj["id"]
        return None

    def _workspace_cwd(self) -> str:
        root = Path(os.environ.get("OPENCLAW_HOME",
                                   Path.home() / ".openclaw"))
        ws = root / "workspace"
        return str(ws) if ws.is_dir() else ""

    # ------------------------------------------------------------------
    # reading (local -> canonical)
    # ------------------------------------------------------------------
    def read_sessions(self, limit: int | None = None) -> list[dict]:
        index = self._load_index()
        ordered = sorted(index.items(),
                         key=lambda kv: kv[1].get("updatedAt") or 0,
                         reverse=True)
        if limit:
            ordered = ordered[:limit]
        foreign = self._foreign_ids()
        cwd = self._workspace_cwd()
        sessions = []
        for key, entry in ordered:
            path = self._entry_file(entry)
            if path is None:
                continue
            msgs = list(self._iter_messages(path))
            if not msgs:
                continue
            started = (entry.get("sessionStartedAt") or 0) / 1000.0
            if started <= 0:
                started = min((m[3] for m in msgs if m[3] is not None),
                              default=0.0) or time.time()
            # Canonical id priority:
            #  1. openclaw:server_id recorded in meta (when the gateway has
            #     not rewritten the index since the pull);
            #  2. key-derived server id: a RUNNING gateway normalizes keys
            #     back to "agent:<agentId>:<rest>" and strips adapter-written
            #     meta, so meta alone cannot be trusted. A native key whose
            #     <rest> looks like a pool id (hermes timestamps, reasonix
            #     rx-*, foreign uuids) round-trips to that server id; a
            #     locally-created session (main/testN/tui-*) keeps its
            #     transcript UUID;
            #  3. the local key when the foreign registry owns it;
            #  4. transcript UUID as the final fallback.
            meta = entry.get("meta") or {}
            server_id = meta.get("openclaw:server_id")
            sid = None
            if server_id:
                sid = server_id
            elif key.startswith("agent:"):
                rest = key.split(":", 2)[2]
                if _POOL_ID_RE.match(rest):
                    sid = rest
            if sid is None:
                if key in foreign:
                    sid = key
                else:
                    sid = entry.get("sessionId") or key
            owner = foreign.get(sid)
            s = {
                "id": sid,
                "started_at": started,
                "model": entry.get("model"),
                "cwd": cwd or None,
                "agent_type": owner or "openclaw",
                "meta": {"openclaw:session_key": key,
                         "openclaw:session_id": entry.get("sessionId"),
                         **({"openclaw:server_id": server_id}
                            if server_id else {})},
                "messages": [
                    {"session_id": sid, "role": role,
                     "content": content, "timestamp": ts}
                    for _lid, role, content, ts in msgs],
            }
            title = next((m[2] for m in msgs
                          if m[1] == "user" and m[2]), None)
            if title:
                s["title"] = title.strip().replace("\n", " ")[:_MAX_TITLE_LEN]
            s["message_count"] = len(s["messages"])
            sessions.append(self.canonicalize(s))
        return sessions

    # ------------------------------------------------------------------
    # writing (canonical -> local)
    # ------------------------------------------------------------------
    def write_sessions(self, sessions: list[dict]) -> dict:
        stats = {"imported": 0, "updated": 0,
                 "new_messages": 0, "duplicates": 0}
        if not sessions or not self.sessions_dir:
            return stats
        index = self._load_index()
        cwd = self._workspace_cwd()
        for session in sessions:
            server_id = str(session.get("id") or "")
            s = self.localize(session)
            key = str(s["id"])
            if not validate_local_id(key):
                continue
            # Remember the server-side id so read_sessions can round-trip
            # back to the SAME server row. Without it, a session pulled
            # from the pool (id like "20260801_...") would be re-pushed
            # under its transcript UUID and fork into a duplicate on the
            # server.
            meta = dict(s.get("meta") or {})
            if server_id and server_id != key:
                meta["openclaw:server_id"] = server_id
            s["meta"] = meta
            # OpenClaw's own sessions round-trip under the transcript UUID,
            # but they live locally under their session key ("agent:main:x").
            # Map back to the existing key so re-pulls update the original
            # entry instead of importing a duplicate. A running gateway also
            # normalizes pooled keys to the "agent:<agentId>:<id>" form, so
            # a bare id from the server must map to that prefixed key too.
            meta_key = meta.get("openclaw:session_key")
            if meta_key in index:
                key = meta_key
            elif key not in index and ("agent:main:" + key) in index:
                key = "agent:main:" + key
            agent = s.get("agent_type")
            if agent not in (None, "openclaw"):
                self._remember_foreign(server_id or key, agent)
            msgs = s.get("messages") or []
            entry = index.get(key)
            if entry is None:
                stats["imported"] += 1
                sid = str(uuid.uuid4())
                entry = {
                    "sessionId": sid,
                    "sessionFile": str(self.sessions_dir / f"{sid}.jsonl"),
                    "sessionStartedAt": int((s.get("started_at") or
                                             time.time()) * 1000),
                    "agentHarnessId": "openclaw",
                }
                path = self.sessions_dir / f"{sid}.jsonl"
                written, parent = self._write_new_transcript(
                    path, sid, cwd, s, msgs)
                stats["new_messages"] += written
            else:
                stats["updated"] += 1
                path = self._entry_file(entry)
                parent = self._tail_id(path)
                if path is None:  # index entry but transcript vanished
                    sid = str(uuid.uuid4())
                    path = self.sessions_dir / f"{sid}.jsonl"
                    entry["sessionId"] = sid
                    entry["sessionFile"] = str(path)
                    written, parent = self._write_new_transcript(
                        path, sid, cwd, s, msgs)
                    stats["new_messages"] += written
                else:
                    parent = self._append_messages(path, parent, msgs,
                                                   stats)
            now_ms = int(time.time() * 1000)
            entry["updatedAt"] = now_ms
            entry["lastInteractionAt"] = now_ms
            entry["lastActivityAt"] = now_ms
            entry.setdefault("sessionStartedAt",
                             int((s.get("started_at") or
                                  time.time()) * 1000))
            if s.get("model"):
                entry["model"] = s["model"]
            if meta:
                entry["meta"] = meta
            index[key] = entry
        self._save_index(index)
        return stats
    def _write_new_transcript(self, path: Path, sid: str, cwd: str,
                              session: dict, msgs: list) -> tuple[int, str]:
        """Create a fresh transcript file; returns (written, last id)."""
        now = time.time()
        started = session.get("started_at") or now
        model = session.get("model") or "unknown"
        mc_id = secrets.token_hex(4)
        lines = [
            {"type": "session", "version": 3, "id": sid,
             "timestamp": _iso_ts(started), "cwd": cwd},
            {"type": "model_change", "id": mc_id,
             "parentId": None, "timestamp": _iso_ts(started),
             "provider": "openclaw", "modelId": model},
            {"type": "thinking_level_change",
             "id": secrets.token_hex(4), "parentId": mc_id,
             "timestamp": _iso_ts(started), "thinkingLevel": "off"},
        ]
        parent = lines[-1]["id"]
        for m in msgs:
            parent = self._message_line(lines, parent, m,
                                        m.get("timestamp") or now)
        path.write_text("".join(json.dumps(x) + "\n" for x in lines),
                        encoding="utf-8")
        return len(msgs), parent

    def _append_messages(self, path: Path, parent: str | None,
                         msgs: list, stats: dict) -> str | None:
        """Append non-duplicate messages to an existing transcript."""
        known = set()
        for _lid, role, content, ts in self._iter_messages(path):
            if role is not None and ts is not None:
                known.add((role, int(round(ts * 1000))))
        lines = []
        for m in msgs:
            role = m.get("role")
            content = m.get("content")
            ts = m.get("timestamp")
            if ts is None:
                ts = time.time()
            if role is None or content is None:
                continue
            if (role, int(round(float(ts) * 1000))) in known:
                stats["duplicates"] += 1
                continue
            known.add((role, int(round(float(ts) * 1000))))
            lines.append(m)
        if not lines:
            return parent
        tail = parent
        payload = []
        for m in lines:
            tail = self._message_line(payload, tail, m, m["timestamp"])
        with path.open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(x) + "\n" for x in payload))
        stats["new_messages"] += len(lines)
        return tail

    @staticmethod
    def _message_line(lines: list, parent: str | None, m: dict,
                      ts: float) -> str:
        """Append one message event; returns its id (new parent)."""
        mid = secrets.token_hex(4)
        content = _sanitize_text(m.get("content"))
        ts_ms = int(round(float(ts) * 1000))
        lines.append({
            "type": "message", "id": mid, "parentId": parent,
            "timestamp": _iso_ts(float(ts)),
            "message": {
                "role": m.get("role"), "content": content,
                "timestamp": ts_ms,
            },
        })
        return mid

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        if not self.sessions_dir or not self._index_path.exists():
            return {"store": str(self.sessions_dir), "error": "not found"}
        index = self._load_index()
        messages = 0
        last = None
        for entry in index.values():
            path = self._entry_file(entry)
            if path is None:
                continue
            for _lid, _role, _content, ts in self._iter_messages(path):
                messages += 1
                if ts is not None:
                    last = max(last, ts) if last is not None else ts
        return {"store": str(self.sessions_dir),
                "sessions": len(index), "messages": messages,
                "last_started_at": last}

    def session_mtime(self, local_id: str) -> float | None:
        entry = self._load_index().get(local_id)
        path = self._entry_file(entry)
        if path is None:
            return None
        try:
            return path.stat().st_mtime
        except OSError:
            return None


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = OpenClawAdapter

if __name__ == "__main__":
    a = OpenClawAdapter()
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions(limit=5)))
