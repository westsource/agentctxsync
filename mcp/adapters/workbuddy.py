"""
WorkBuddy desktop adapter (bidirectional).

Local store: ~/.workbuddy/  (Windows: %USERPROFILE%\\.workbuddy)
  - session messages : projects/<slug>/<conversationId>.jsonl  (JSONL,
    one event per line; slug is the cwd with drive/separators flattened,
    e.g. F:\\OpenCode\\agentctxsync -> f-OpenCode-agentctxsync)
  - session metadata : workbuddy.db (SQLite, ``sessions`` table: id, cwd,
    user_id, title, status, created_at/updated_at in epoch MILLISECONDS,
    is_playground, mode, model, ...)
  - sync mapping     : edge-sync-mapping-v2.db (WorkBuddy maintains this;
    we must NOT touch it -- WorkBuddy's startup MIGRATE creates the
    convmsg:<userId> mapping for every local session, including sessions
    written by this adapter)

Event types in the JSONL (verified against WorkBuddy 5.3.13):
  - ai-title              : {"timestamp": ms, "type": "ai-title", "aiTitle": ...,
                             "sessionId": ..., "cwd": ...}
  - message               : {"id", "timestamp": ms, "type": "message",
                             "role": "user"|"assistant", "status", "content":
                             [{"type": "input_text"|"output_text", "text": ...}],
                             "providerData", "sessionId", "cwd"}
  - reasoning             : {"id", "timestamp": ms, "type": "reasoning",
                             "rawContent": [{"type": "reasoning_text", "text"}], ...}
  - function_call         : {"id", "timestamp": ms, "type": "function_call",
                             "callId", "name", "arguments", "providerData"}
  - function_call_result  : {"id", "timestamp": ms, "type": "function_call_result",
                             "callId", "name", "status",
                             "output": {"type": "text", "text"}}
  - file-history-snapshot : internal snapshots; skipped on read, never written

Timestamps in the JSONL/db are epoch MILLISECONDS; the canonical model uses
epoch seconds. Convert with /1000 on read and *1000 on write so
(session_id, role, timestamp) round-trips exactly for dedupe.

IMPORTANT write constraint (verified 2026-08-16): sessions written to disk
while WorkBuddy is running are NOT visible in the UI until WorkBuddy is
restarted -- on startup its MIGRATE pass scans workbuddy.db + projects/,
registers the session in edge-sync-mapping-v2.db (cloud channel
convmsg:<userId>) and shows it in the session list. Only the session
metadata/title is synced to the cloud; message CONTENT is not uploaded
(cloud sync only streams messages WorkBuddy itself generates). The local
desktop app CAN open and continue sessions written by this adapter.
Also: the cwd directory of a written session MUST exist or WorkBuddy fails
to open it ("工作目录可能已被重命名或删除") -- this adapter creates it.
"""

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from .base import Adapter, split_agent_prefix, validate_local_id

#: user id fallback order: existing record in db -> env -> placeholder
_ENV_USER_ID = "WORKBUDDY_USER_ID"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _text_of(content) -> str:
    """Extract text from a WorkBuddy content part list (input_text/output_text)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for p in content:
        if isinstance(p, dict):
            t = p.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts)


class WorkBuddyAdapter(Adapter):
    """WorkBuddy desktop adapter (canonical ids prefixed ``workbuddy:``)."""

    agent_type = "workbuddy"

    def __init__(self, workbuddy_home: Path | str | None = None):
        self.home = Path(workbuddy_home) if workbuddy_home else self.discover()

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    def discover(self) -> Path | None:
        home = Path(os.environ.get("WORKBUDDY_HOME",
                                   Path.home() / ".workbuddy"))
        return home if home.is_dir() else None

    def _foreign_ids_file(self) -> Path | None:
        if self.home:
            return self.home / ".hermes-sync-foreign-ids.json"
        return None

    def _watermark_file(self) -> Path | None:
        if self.home:
            return self.home / ".hermes-sync-watermark"
        return None

    def _db(self) -> Path:
        return self.home / "workbuddy.db"

    def _projects_dir(self) -> Path:
        return self.home / "projects"

    @staticmethod
    def _conn(db: Path, timeout: int = 5) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db), timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def slugify(cwd: str) -> str:
        """cwd -> projects subdir slug (matches WorkBuddy's own scheme).

        F:\\OpenCode\\agentctxsync     -> f-OpenCode-agentctxsync
        C:\\Users\\rong\\HermesSyncTest -> c-Users-rong-HermesSyncTest
        /home/user/proj               -> home-user-proj
        """
        cwd = (cwd or "").replace("/", "\\")
        m = re.match(r"^([a-zA-Z]):[\\]?(.*)$", cwd)
        if m:
            drive, rest = m.group(1).lower(), m.group(2)
        else:
            drive, rest = "", cwd.lstrip("\\")
        slug = re.sub(r"[\\]+", "-", rest)
        return (drive + "-" + slug) if drive else slug

    def _session_path(self, cwd: str, local_id: str) -> Path:
        return self._projects_dir() / self.slugify(cwd) / f"{local_id}.jsonl"

    def _session_rows(self, limit: int | None = None) -> list[dict]:
        """All non-deleted sessions from workbuddy.db, newest first."""
        db = self._db()
        if not db.exists():
            return []
        conn = self._conn(db)
        try:
            sql = ("SELECT * FROM sessions WHERE deleted_at IS NULL "
                   "ORDER BY COALESCE(updated_at, created_at, 0) DESC")
            if limit:
                sql += f" LIMIT {int(limit)}"
            rows = [dict(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()
        return rows

    def _user_id(self) -> str | None:
        """Real WorkBuddy user id (first existing session) or env fallback."""
        for row in self._session_rows(limit=1):
            if row.get("user_id"):
                return str(row["user_id"])
        return os.environ.get(_ENV_USER_ID)

    # ------------------------------------------------------------------
    # reading: jsonl events -> canonical messages
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_jsonl(path: Path, local_id: str) -> tuple[list[dict], str | None]:
        """Parse one session file -> (canonical messages, title_from_events).

        Timestamps converted ms -> seconds. Every meaningful event maps to a
        canonical message so nothing is lost:
          message             -> role user/assistant, content = joined text
          reasoning           -> role assistant, reasoning = rawContent text
          function_call       -> role assistant, tool_name/call_id/args
          function_call_result-> role tool, tool_name/call_id, content = output
          ai-title            -> session title (returned separately)
          file-history-snapshot -> skipped
        """
        messages: list[dict] = []
        title = None
        try:
            lines = path.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
        except OSError:
            return messages, title
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            etype = e.get("type")
            ts = e.get("timestamp")
            if not isinstance(ts, (int, float)):
                continue
            ts_s = ts / 1000.0
            if etype == "ai-title":
                if e.get("aiTitle") and not title:
                    title = str(e["aiTitle"])
                continue
            if etype == "file-history-snapshot":
                continue
            msg: dict = {"session_id": local_id, "timestamp": ts_s}
            if etype == "message":
                role = e.get("role")
                if role not in ("user", "assistant", "system"):
                    continue
                msg["role"] = role
                msg["content"] = _text_of(e.get("content"))
            elif etype == "reasoning":
                raw = e.get("rawContent") or e.get("content") or []
                text = _text_of(raw)
                msg["role"] = "assistant"
                msg["content"] = ""
                msg["reasoning"] = text
            elif etype == "function_call":
                msg["role"] = "assistant"
                msg["content"] = _text_of(e.get("arguments")) or \
                    str(e.get("argumentsDisplayText", ""))
                msg["tool_name"] = str(e.get("name", ""))
                msg["tool_call_id"] = str(e.get("callId", ""))
            elif etype == "function_call_result":
                out = e.get("output")
                if isinstance(out, dict):
                    content = str(out.get("text") or out.get("content") or "")
                else:
                    content = _text_of(out)
                msg["role"] = "tool"
                msg["content"] = content
                msg["tool_name"] = str(e.get("name", ""))
                msg["tool_call_id"] = str(e.get("callId", ""))
            else:
                continue
            if e.get("id"):
                msg["meta"] = {"workbuddy:event_id": str(e["id"])}
            messages.append(msg)
        return messages, title

    def read_sessions(self, limit: int | None = None) -> list[dict]:
        if not self.home or not self.home.is_dir():
            return []
        sessions = []
        for row in self._session_rows(limit=limit):
            sid = str(row.get("id") or "")
            if not sid:
                continue
            cwd = row.get("cwd") or str(Path.home())
            title = row.get("title") or row.get("custom_title")
            path = self._session_path(str(cwd), sid)
            msgs, title_from_events = self._parse_jsonl(path, sid)
            if not title and title_from_events:
                title = title_from_events
            created_ms = row.get("created_at") or row.get("updated_at") or 0
            s = {
                "id": sid,
                "started_at": (created_ms or 0) / 1000.0,
                "cwd": str(cwd),
                "messages": msgs,
            }
            if title:
                s["title"] = title
            if row.get("model"):
                s["model"] = str(row["model"])
            if row.get("status"):
                s["end_reason"] = str(row["status"])
            if row.get("mode"):
                s["meta"] = {"workbuddy:mode": str(row["mode"])}
            if row.get("updated_at"):
                s["ended_at"] = float(row["updated_at"]) / 1000.0
            s["message_count"] = len(msgs)
            sessions.append(self.canonicalize(s))
        return sessions

    # ------------------------------------------------------------------
    # writing: canonical -> workbuddy store
    # ------------------------------------------------------------------
    def write_sessions(self, sessions: list[dict]) -> dict:
        if not self.home:
            return {"error": "workbuddy home not found"}
        if not sessions:
            return {"imported": 0, "updated": 0, "new_messages": 0,
                    "duplicates": 0}
        self._projects_dir().mkdir(parents=True, exist_ok=True)
        user_id = self._user_id() or "hermes-sync"
        imported = updated = new_messages = duplicates = 0
        now_ms = _now_ms()
        for session in sessions:
            s = self.localize(session, strict=False)
            local_id = str(s["id"])
            # Strip a foreign agent prefix (codex:/opencode:/...) so the id is
            # a legal Windows file name; remember it so read round-trips the
            # bare id unchanged (same semantics as hermes bare ids).
            if ":" in local_id:
                _, bare = split_agent_prefix(local_id)
                if bare:
                    local_id = bare
            if not session["id"].startswith("workbuddy:"):
                self._remember_foreign(local_id)
            if not validate_local_id(local_id):
                continue  # untrusted remote id: skip
            msgs = s.pop("messages", []) or []
            cwd = str(s.get("cwd") or Path.home())
            # WorkBuddy refuses to open sessions whose cwd is missing.
            # Foreign sessions may carry paths that don't exist on this
            # machine (another device's cwd) — fall back to a local dir
            # instead of aborting the whole pull.
            try:
                Path(cwd).mkdir(parents=True, exist_ok=True)
            except OSError:
                cwd = str(Path.home() / "hermes-sync-foreign")
                Path(cwd).mkdir(parents=True, exist_ok=True)
            path = self._session_path(cwd, local_id)
            stats = self._write_jsonl(path, cwd, local_id, msgs,
                                      s.get("title"))
            new_messages += stats[0]
            duplicates += stats[1]
            created_ms = int((s.get("started_at") or now_ms / 1000) * 1000)
            updated_ms = int((s.get("ended_at") or now_ms / 1000) * 1000)
            updated_ms = max(updated_ms, created_ms, now_ms)
            title = s.get("title")
            model = s.get("model")
            mode = (s.get("meta") or {}).get("workbuddy:mode")
            was_new = self._upsert_session(local_id, cwd, user_id, title,
                                           model, mode, created_ms,
                                           updated_ms, now_ms)
            if was_new:
                imported += 1
            else:
                updated += 1
        return {"imported": imported, "updated": updated,
                "new_messages": new_messages, "duplicates": duplicates}

    def _write_jsonl(self, path: Path, cwd: str, local_id: str,
                     msgs: list[dict], title: str | None = None
                     ) -> tuple[int, int]:
        """Append canonical messages to a session file. Returns
        (new_messages, duplicates). A fresh file starts with an ai-title
        event (placeholder title so the file is never empty)."""
        new_count = dup_count = 0
        existing_ts: set[tuple] = set()
        existing_title = None
        if path.exists():
            old, existing_title = self._parse_jsonl(path, local_id)
            existing_ts = {(m.get("role"), m.get("timestamp"))
                           for m in old if m.get("timestamp") is not None}
        lines: list[str] = []
        if not path.exists():
            lines.append(json.dumps({
                "timestamp": _now_ms(), "type": "ai-title",
                "aiTitle": title or "Imported session",
                "sessionId": local_id,
                "cwd": cwd.replace("\\", "\\\\"),
            }, ensure_ascii=False))
        for m in msgs:
            role = m.get("role")
            ts = m.get("timestamp")
            if not isinstance(ts, (int, float)) or role is None:
                continue
            key = (role, float(ts))
            if key in existing_ts:
                dup_count += 1
                continue
            existing_ts.add(key)
            lines.append(json.dumps(self._to_event(local_id, m),
                                    ensure_ascii=False))
            new_count += 1
        if new_count:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        return new_count, dup_count

    @staticmethod
    def _to_event(local_id: str, m: dict) -> dict:
        """One canonical message -> one WorkBuddy JSONL event."""
        ts_ms = int(float(m["timestamp"]) * 1000)
        role = m.get("role")
        content_text = m.get("content") or ""
        if not isinstance(content_text, str):
            content_text = str(content_text)
        event: dict = {"id": str(uuid.uuid4()), "timestamp": ts_ms,
                       "sessionId": local_id}
        if role == "user":
            event.update({"type": "message", "role": "user",
                          "status": "completed",
                          "content": [{"type": "input_text",
                                       "text": content_text}]})
        elif role == "assistant":
            event.update({"type": "message", "role": "assistant",
                          "status": "completed",
                          "content": [{"type": "output_text",
                                       "text": content_text}]})
        elif role == "tool":
            event.update({"type": "function_call_result",
                          "name": m.get("tool_name") or "tool",
                          "callId": m.get("tool_call_id")
                          or str(uuid.uuid4()),
                          "status": "completed",
                          "output": {"type": "text", "text": content_text}})
        else:  # system and anything else: keep as a user-visible message
            event.update({"type": "message", "role": "user",
                          "status": "completed",
                          "content": [{"type": "input_text",
                                       "text": content_text}]})
        return event

    def _ensure_schema(self, conn: sqlite3.Connection):
        """Create the sessions table if missing (fresh store).

        Mirrors the real WorkBuddy 5.3.13 schema so a db written by this
        adapter is recognised by WorkBuddy on next startup (its drizzle
        migrations are skipped when the current table already exists).
        """
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            " id TEXT PRIMARY KEY, cwd TEXT NOT NULL, user_id TEXT NOT NULL,"
            " title TEXT, custom_title TEXT, status TEXT NOT NULL,"
            " created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,"
            " deleted_at INTEGER, is_playground INTEGER NOT NULL,"
            " source_mode TEXT, is_background_automation INTEGER, mode TEXT,"
            " model TEXT, expert_id TEXT, expert_locale TEXT,"
            " expert_runtime_identity TEXT, expert_marketplace TEXT,"
            " permission_mode TEXT, last_activity_at INTEGER,"
            " use_sandbox_cli INTEGER, project_id TEXT,"
            " plugin_context_json TEXT,"
            " last_user_prompt_expert_selection TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS __workbuddy_drizzle_migrations ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT NOT NULL,"
            " created_at INTEGER)")

    def _upsert_session(self, local_id: str, cwd: str, user_id: str | None,
                        title, model, mode, created_ms: int, updated_ms: int,
                        now_ms: int) -> bool:
        """Upsert one row in workbuddy.db sessions. Returns True if new."""
        db = self._db()
        conn = self._conn(db)
        try:
            cur = conn.cursor()
            self._ensure_schema(conn)
            exists = cur.execute("SELECT 1 FROM sessions WHERE id = ?",
                                 (local_id,)).fetchone()
            if exists:
                sets = ["cwd = ?", "updated_at = ?", "last_activity_at = ?"]
                vals = [cwd, updated_ms, now_ms]
                if title:
                    sets.append("title = ?")
                    vals.append(title)
                if model:
                    sets.append("model = ?")
                    vals.append(model)
                if mode:
                    sets.append("mode = ?")
                    vals.append(mode)
                if user_id:
                    sets.append("user_id = ?")
                    vals.append(user_id)
                vals.append(local_id)
                cur.execute(f"UPDATE sessions SET {', '.join(sets)} "
                            "WHERE id = ?", vals)
                conn.commit()
                return False
            cur.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, cwd, user_id, title, status, created_at, updated_at, "
                " is_playground, mode, model, last_activity_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (local_id, cwd, user_id, title or "Imported session",
                 "completed", created_ms, updated_ms, 0,
                 mode or "craft", model, now_ms))
            conn.commit()
            return True
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        rows = self._session_rows()
        messages = 0
        for row in rows:
            path = self._session_path(str(row.get("cwd") or ""),
                                      str(row.get("id") or ""))
            if path.exists():
                try:
                    messages += sum(1 for _ in path.open(
                        encoding="utf-8", errors="replace"))
                except OSError:
                    pass
        return {"store": str(self.home / "projects") if self.home else None,
                "sessions": len(rows), "messages": messages}


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = WorkBuddyAdapter


if __name__ == "__main__":
    a = WorkBuddyAdapter()
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions(limit=5)))
