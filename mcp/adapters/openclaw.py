"""
OpenClaw adapter (EXPERIMENTAL -- needs validation against a real store).

Local store: ~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite
(SQLite, runtime sessions) with JSONL transcripts archived under
~/.openclaw/agents/<agentId>/sessions/.

OpenClaw's exact SQLite schema is not publicly documented; this adapter
probes the schema at init time (sqlite_master) and maps the first table
whose columns look like sessions/messages. If probing fails, pass explicit
overrides (table_sessions, table_messages, col_map) to the constructor.
OpenClaw also ships an official MCP bridge (`openclaw mcp serve`) which may
be a better integration point in the future.
"""

import os
import re
import sqlite3
import time
from pathlib import Path

from .base import Adapter, local_id_lenient, validate_local_id

_SESSION_TABLE_RE = re.compile(r"(session|thread|conversation)", re.I)
_MESSAGE_TABLE_RE = re.compile(r"(message|transcript|part)", re.I)
_SESSION_COL_RE = re.compile(r"(title|model|started_at|created_at|updated_at|id)", re.I)
_MESSAGE_COL_RE = re.compile(r"(content|role|timestamp|created_at|time|id)", re.I)


class OpenClawAdapter(Adapter):
    """OpenClaw sqlite adapter (canonical ids prefixed ``openclaw:``)."""

    agent_type = "openclaw"

    def __init__(self, db_path: Path | str | None = None,
                 table_sessions: str | None = None,
                 table_messages: str | None = None,
                 col_map: dict[str, str | None] | None = None):
        self.db_path = Path(db_path) if db_path else self.discover()
        self.table_sessions = table_sessions
        self.table_messages = table_messages
        self.col_map = col_map
        if self.db_path and self.db_path.exists():
            self._probe()

    def _foreign_ids_file(self) -> Path | None:
        if self.db_path:
            return self.db_path.with_name(
                self.db_path.name + ".hermes-sync-foreign-ids.json")
        return None

    def _watermark_file(self) -> Path | None:
        if self.db_path:
            return self.db_path.with_name(
                self.db_path.name + ".hermes-sync-watermark")
        return None

    def discover(self) -> Path | None:
        root = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw"))
        agents_dir = root / "agents"
        if not agents_dir.is_dir():
            return None
        candidates = sorted(agents_dir.glob("*/agent/openclaw-agent.sqlite"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0] if candidates else None

    # ------------------------------------------------------------------
    # schema probing
    # ------------------------------------------------------------------
    def _probe(self):
        conn = sqlite3.connect(str(self.db_path))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        sessions = [t for t in tables if _SESSION_TABLE_RE.search(t)]
        messages = [t for t in tables if _MESSAGE_TABLE_RE.search(t)]
        if self.table_sessions is None and sessions:
            self.table_sessions = sessions[0]
        if self.table_messages is None and messages:
            self.table_messages = messages[0]
        if self.col_map is None:
            self.col_map = {}
        if not self.table_sessions or not self.table_messages:
            raise ValueError(
                "Could not probe OpenClaw schema; pass table_sessions/"
                "table_messages/col_map explicitly. Tables found: "
                + ", ".join(tables))

    def _conn(self):
        if not self.db_path or not self.db_path.exists():
            raise FileNotFoundError(f"Local DB not found: {self.db_path}")
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _cols(self, c, table: str) -> list[str]:
        return [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]

    @staticmethod
    def _map(row: dict, col_map: dict) -> dict:
        out = {}
        for k, v in row.items():
            canon = col_map.get(k, k)
            if v is not None:
                out[canon] = v
        return out

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def read_sessions(self, limit: int | None = None) -> list[dict]:
        conn = self._conn()
        c = conn.cursor()
        s_cols = self._cols(c, self.table_sessions)
        m_cols = self._cols(c, self.table_messages)
        sid_col = "id" if "id" in s_cols else s_cols[0]
        ts_col = next((x for x in ("started_at", "created_at", "updated_at")
                       if x in s_cols), None)
        mid_col = "session_id" if "session_id" in m_cols else \
            next((x for x in m_cols if "session" in x.lower()), None)
        sessions = []
        sql = f"SELECT * FROM {self.table_sessions} ORDER BY {ts_col or sid_col} DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        for row in c.execute(sql):
            s = dict(row)
            sid = str(s[sid_col])
            m_sql = f"SELECT * FROM {self.table_messages}"
            params: tuple = ()
            if mid_col:
                m_sql += f" WHERE {mid_col} = ?"
                params = (sid,)
            m_sql += " ORDER BY " + next(
                (x for x in ("timestamp", "created_at", "time") if x in m_cols),
                m_cols[0])
            msgs = []
            for mrow in c.execute(m_sql, params):
                m = self._map(dict(mrow), self.col_map)
                m["session_id"] = sid
                if not isinstance(m.get("timestamp"), (int, float)):
                    m["timestamp"] = time.time()
                msgs.append(m)
            s = self._map(s, self.col_map)
            s["id"] = sid
            if ts_col and s.get(ts_col) is not None:
                s["started_at"] = s.pop(ts_col)
            s.setdefault("started_at", time.time())
            s["messages"] = msgs
            s["message_count"] = len(msgs)
            sessions.append(self.canonicalize(s))
        conn.close()
        return sessions

    # ------------------------------------------------------------------
    # writing (best-effort INSERT; fails loudly on schema mismatch)
    # ------------------------------------------------------------------
    def write_sessions(self, sessions: list[dict]) -> dict:
        conn = self._conn()
        try:
            return self._write(conn, sessions)
        finally:
            conn.close()

    def _write(self, conn, sessions: list[dict]) -> dict:
        c = conn.cursor()
        s_cols = self._cols(c, self.table_sessions)
        m_cols = self._cols(c, self.table_messages)
        imported = updated = new_messages = duplicates = 0
        for session in sessions:
            s = dict(session)
            s["id"] = local_id_lenient(self.agent_type, s["id"])
            if session.get("agent_type") != "openclaw":
                self._remember_foreign(s["id"], session.get("agent_type"))
            sid = str(s["id"])
            if not validate_local_id(sid):
                continue  # untrusted remote id: skip
            msgs = s.pop("messages", [])
            s_data = {k: v for k, v in s.items()
                      if k in s_cols and v is not None}
            c.execute(f"SELECT id FROM {self.table_sessions} WHERE id = ?",
                      (sid,))
            if c.fetchone():
                updated += 1
            else:
                cols = ", ".join(s_data)
                ph = ", ".join(["?"] * len(s_data))
                c.execute(
                    f"INSERT INTO {self.table_sessions} ({cols}) "
                    f"VALUES ({ph})", list(s_data.values()))
                imported += 1
            mid_col = "session_id" if "session_id" in m_cols else \
                next((x for x in m_cols if "session" in x.lower()), None)
            for m in msgs:
                m_data = {k: v for k, v in m.items()
                          if k in m_cols and v is not None}
                if mid_col:
                    m_data[mid_col] = sid
                role = m_data.get("role")
                ts = m_data.get("timestamp")
                if role is not None and ts is not None:
                    q = f"SELECT 1 FROM {self.table_messages} WHERE role = ? AND timestamp = ?"
                    params = (role, ts)
                    if mid_col:
                        q = f"SELECT 1 FROM {self.table_messages} WHERE {mid_col} = ? AND role = ? AND timestamp = ?"
                        params = (sid, role, ts)
                    c.execute(q, params)
                    if c.fetchone():
                        duplicates += 1
                        continue
                if not m_data:
                    continue
                cols = ", ".join(m_data)
                ph = ", ".join(["?"] * len(m_data))
                c.execute(
                    f"INSERT INTO {self.table_messages} ({cols}) "
                    f"VALUES ({ph})", list(m_data.values()))
                new_messages += 1
        conn.commit()
        return {"imported": imported, "updated": updated,
                "new_messages": new_messages, "duplicates": duplicates}

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        if not self.db_path or not self.db_path.exists():
            return {"store": str(self.db_path), "error": "not found"}
        conn = self._conn()
        c = conn.cursor()
        try:
            c.execute(f"SELECT COUNT(*) FROM {self.table_sessions}")
            sessions = c.fetchone()[0]
            c.execute(f"SELECT COUNT(*) FROM {self.table_messages}")
            messages = c.fetchone()[0]
        except sqlite3.OperationalError:
            sessions = messages = -1
        conn.close()
        return {"store": str(self.db_path), "sessions": sessions,
                "messages": messages}


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = OpenClawAdapter


if __name__ == "__main__":
    a = OpenClawAdapter()
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions(limit=5)))
