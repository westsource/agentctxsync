"""
opencode CLI adapter (anomalyco/opencode).

Local store: <XDG_DATA_HOME or %LOCALAPPDATA%>/opencode/storage/
  0.4.x layout (and 1.x, per-project):
    session/info/<sessionID>.json              -- session metadata
    session/message/<sessionID>/<messageID>.json -- one file per message
    session/part/<sessionID>/<messageID>/<partID>.json -- content parts
  ids are prefixed: ses_/msg_/prt_ + 26 chars (12 hex timestamp + 14
  base62 random). Message files are discriminated unions
  (type: user|assistant|system|shell|synthetic|...); assistant content is
  an inline array of text|reasoning|tool parts.

Cross-agent sessions: opencode ids are restricted to its own format, so a
session coming from another agent gets a fresh ``ses_`` id on first write;
the mapping canonical-id -> local-id is persisted in
storage/.hermes-sync-idmap.json so later pulls reuse the same local id
(dedupe stays stable).
"""

import json
import os
import secrets
import string
import time
from pathlib import Path

from .base import Adapter, canonical_id, local_id_lenient, validate_local_id

_IDMAP = ".hermes-sync-idmap.json"
_ID_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase


def _gen_id(prefix: str) -> str:
    """opencode-style id: <prefix>_ + 12 hex ts + 14 base62 random."""
    ts = int(time.time() * 1000)
    # opencode writes timestamps bit-flipped for reverse-sortable ids; a
    # plain hex ts is still schema-valid (only the prefix is validated).
    rand = "".join(secrets.choice(_ID_ALPHABET) for _ in range(14))
    return f"{prefix}_{ts:012x}{rand}"


class OpencodeAdapter(Adapter):
    """opencode JSON-file store adapter (canonical ids prefixed ``opencode:``)."""

    agent_type = "opencode"

    def __init__(self, storage_dir: Path | str | None = None):
        self.storage = Path(storage_dir) if storage_dir else self.discover()

    def discover(self) -> Path | None:
        base = (os.environ.get("XDG_DATA_HOME")
                or os.environ.get("LOCALAPPDATA")
                or str(Path.home() / ".local" / "share"))
        d = Path(base) / "opencode" / "storage"
        return d if d.is_dir() else None

    def _watermark_file(self) -> Path | None:
        if self.storage:
            return self.storage / ".hermes-sync-watermark"
        return None

    # ------------------------------------------------------------------
    # idmap (canonical id -> local id) for foreign sessions
    # ------------------------------------------------------------------
    def _idmap(self) -> dict[str, str]:
        if not self.storage:
            return {}
        p = self.storage / _IDMAP
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _save_idmap(self, m: dict):
        p = self.storage / _IDMAP
        p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")

    def _local_id_for(self, canonical_id: str) -> str:
        """Map a canonical session id to a local opencode id."""
        local = local_id_lenient(self.agent_type, canonical_id)
        if local.startswith("ses_") and len(local) > 4 and validate_local_id(local):
            return local
        m = self._idmap()
        if canonical_id in m:
            return m[canonical_id]
        fresh = _gen_id("ses")
        m[canonical_id] = fresh
        self._save_idmap(m)
        return fresh

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def _session_ids(self) -> list[str]:
        if not self.storage:
            return []
        d = self.storage / "session" / "info"
        if not d.is_dir():
            return []
        return sorted((p.stem for p in d.glob("*.json")), reverse=True)

    def read_sessions(self, limit: int | None = None) -> list[dict]:
        ids = self._session_ids()
        if limit:
            ids = ids[:limit]
        # reverse idmap: local opencode id -> foreign canonical id
        rev = {v: k for k, v in self._idmap().items()}
        sessions = []
        for sid in ids:
            s = self._read_session(sid)
            if s is None:
                continue
            canonical_sid = rev.get(sid, canonical_id(self.agent_type, sid))
            s["id"] = canonical_sid
            for m in s.get("messages", []):
                m["session_id"] = canonical_sid
            sessions.append(s)
        return sessions

    def _read_session(self, sid: str) -> dict | None:
        info_p = self.storage / "session" / "info" / f"{sid}.json"
        try:
            info = json.loads(info_p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        session = {"id": sid, "started_at": 0.0, "messages": []}
        if isinstance(info.get("time"), dict):
            t = info["time"]
            if t.get("created") is not None:
                session["started_at"] = t["created"]
            if t.get("updated") is not None:
                session["ended_at"] = t["updated"]
        if info.get("title"):
            session["title"] = info["title"]
        if info.get("model"):
            session["model"] = info["model"]
        if info.get("projectID"):
            session["meta"] = {"opencode:projectID": info["projectID"]}
        # messages
        msg_dir = self.storage / "session" / "message" / sid
        if msg_dir.is_dir():
            for mp in sorted(msg_dir.glob("*.json")):
                try:
                    msg = json.loads(mp.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                m = self._msg_to_canonical(sid, msg)
                if m:
                    session["messages"].append(m)
        session["message_count"] = len(session["messages"])
        if not session["started_at"]:
            session["started_at"] = info_p.stat().st_mtime
        return session

    @staticmethod
    def _msg_to_canonical(sid: str, msg: dict) -> dict | None:
        role = msg.get("role") or msg.get("type")
        if role in ("agent-switched", "model-switched", "compaction"):
            return None
        if role == "shell":
            role = "tool"
        content_parts = []
        reasoning_parts = []
        raw = msg.get("content")
        if isinstance(raw, list):
            for part in raw:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype in ("text", "input_text", "output_text") and part.get("text"):
                    content_parts.append(str(part["text"]))
                elif ptype == "reasoning" and part.get("text"):
                    reasoning_parts.append(str(part["text"]))
                elif ptype == "tool" and part.get("tool"):
                    tool = part["tool"]
                    if isinstance(tool, dict) and tool.get("name"):
                        content_parts.append(
                            f"[tool:{tool['name']}] {tool.get('input') or ''}")
        elif isinstance(raw, str):
            content_parts.append(raw)
        if not content_parts and not reasoning_parts:
            return None
        out = {"session_id": sid, "role": role or "assistant",
               "content": "\n".join(content_parts),
               "timestamp": (msg.get("time")
                             if isinstance(msg.get("time"), (int, float))
                             else time.time())}
        if reasoning_parts:
            out["reasoning"] = "\n".join(reasoning_parts)
        if msg.get("model"):
            out["model"] = msg["model"]
        if msg.get("id"):
            out["meta"] = {"opencode:message_id": msg["id"]}
        return out

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    def write_sessions(self, sessions: list[dict]) -> dict:
        if not self.storage:
            return {"error": "opencode storage not found"}
        (self.storage / "session" / "info").mkdir(parents=True, exist_ok=True)
        imported = updated = new_messages = duplicates = 0
        for session in sessions:
            s = dict(session)
            msgs = s.pop("messages", [])
            sid = self._local_id_for(session["id"])
            s["id"] = sid
            info_p = self.storage / "session" / "info" / f"{sid}.json"
            now = time.time()
            info = {
                "id": sid,
                "title": s.get("title") or "untitled",
                "time": {"created": s.get("started_at") or now,
                         "updated": s.get("ended_at") or now},
            }
            for k in ("model", "agent", "projectID", "location", "subpath"):
                if s.get(k) is not None:
                    info[k] = s[k]
            if info_p.exists():
                updated += 1
            else:
                imported += 1
            self._atomic_write(info_p, json.dumps(info, ensure_ascii=False))
            # messages: dedupe on (role, timestamp) against existing files
            msg_dir = self.storage / "session" / "message" / sid
            msg_dir.mkdir(parents=True, exist_ok=True)
            existing = set()
            for mp in msg_dir.glob("*.json"):
                try:
                    m = json.loads(mp.read_text(encoding="utf-8"))
                    existing.add((m.get("role"), m.get("time")))
                except (ValueError, OSError):
                    continue
            for m in msgs:
                key = (m.get("role"), m.get("timestamp"))
                if key in existing:
                    duplicates += 1
                    continue
                mid = _gen_id("msg")
                mfile = {
                    "id": mid,
                    "sessionID": sid,
                    "role": m.get("role", "assistant"),
                    "time": m.get("timestamp") or now,
                    "content": [{"type": "text",
                                 "text": m.get("content", "")}],
                }
                if m.get("reasoning"):
                    mfile["content"].append(
                        {"type": "reasoning", "text": m["reasoning"]})
                self._atomic_write(
                    msg_dir / f"{mid}.json", json.dumps(mfile, ensure_ascii=False))
                new_messages += 1
        return {"imported": imported, "updated": updated,
                "new_messages": new_messages, "duplicates": duplicates}

    @staticmethod
    def _atomic_write(path: Path, text: str):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        ids = self._session_ids()
        total_msgs = 0
        for sid in ids:
            d = self.storage / "session" / "message" / sid
            if d.is_dir():
                total_msgs += len(list(d.glob("*.json")))
        return {"store": str(self.storage), "sessions": len(ids),
                "messages": total_msgs}


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = OpencodeAdapter


if __name__ == "__main__":
    a = OpencodeAdapter()
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions(limit=5)))
