"""
Multi-agent adapter contract for Agent Contexts Sync.

Canonical (wire) model
======================
All adapters convert between an agent's native local storage and the
canonical session/message dicts below. The canonical model is the single
format stored on the server (PostgreSQL) and exchanged via /push and /pull.

Canonical session dict
----------------------
Required:
    id          str   -- canonical id; see AGENT_PREFIXES below.
    started_at  float -- epoch seconds.

Common (optional) fields -- shared by most agents, stored as first-class
columns on the server:
    title, model, ended_at, end_reason, message_count,
    parent_session_id, user_id, source, cwd, git_branch, git_repo_root,
    input_tokens, output_tokens, reasoning_tokens, cache_read_tokens,
    cache_write_tokens, estimated_cost_usd, actual_cost_usd,
    display_name, session_key, chat_id, chat_type, thread_id,
    profile_name, pinned, archived

meta (dict) -- anything else the agent stores that has no canonical slot.
Put agent-specific fields here (e.g. codex history_mode, opencode tokens
detail). meta keys MUST be namespaced with the agent name ("codex:foo") to
avoid collisions between agents on the shared server.

Canonical message dict
----------------------
Required:
    session_id  str   -- canonical session id (with prefix).
    role        str   -- "user" | "assistant" | "system" | "tool" | ...
    content     str
    timestamp   float -- epoch seconds.

Optional:
    id, token_count, finish_reason, reasoning, tool_call_id, tool_name,
    tool_calls, display_kind, display_metadata, observed, active, compacted
    meta (dict, namespaced as above)

Reasoning: adapters MUST map their native reasoning text to the unified
`reasoning` field (omit when the agent does not persist reasoning).

Identity & dedupe semantics
===========================
* Session upsert key: the LOCAL id (canonical id minus prefix).
* Message dedupe key: (session_id, role, timestamp) triple -- the same rule
  the server applies, so cross-agent pushes are idempotent.
* Message ids are per-store autoincrement and are NOT globally unique; the
  server and adapters must never rely on them for identity.
"""

import abc
import json
import os
import sqlite3
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Agent prefix registry
# ---------------------------------------------------------------------------
# Every agent gets a stable namespace prefix so sessions from different
# agents can never collide in the shared workspace. ``hermes`` keeps bare
# ids for backwards compatibility with existing deployments (its ids are
# UUIDs that cannot collide with prefixed ids).
#
# When adding a new agent:
#   1. pick a prefix that is NOT already in this dict;
#   2. implement an adapter in mcp/adapters/<name>.py;
#   3. register it in mcp/adapters/__init__.py ADAPTERS.
AGENT_PREFIXES = {
    "hermes": None,        # bare ids (legacy)
    "codex": "codex:",
    "opencode": "opencode:",
    "reasonix": "reasonix:",
    "openclaw": "openclaw:",
    "workbuddy": "workbuddy:",
}

CANONICAL_SESSION_FIELDS = (
    "id", "started_at", "title", "model", "ended_at", "end_reason",
    "message_count", "parent_session_id", "user_id", "source", "cwd",
    "git_branch", "git_repo_root", "input_tokens", "output_tokens",
    "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
    "estimated_cost_usd", "actual_cost_usd", "display_name", "session_key",
    "chat_id", "chat_type", "thread_id", "profile_name", "pinned",
    "archived",
)

CANONICAL_MESSAGE_FIELDS = (
    "session_id", "role", "content", "timestamp", "id", "token_count",
    "finish_reason", "reasoning", "tool_call_id", "tool_name", "tool_calls",
    "display_kind", "display_metadata", "observed", "active", "compacted",
)


def canonical_id(agent_type: str, local_id) -> str:
    """Build the canonical session id for ``local_id`` in ``agent_type``.

    ``hermes`` returns the bare id (legacy); every other agent is prefixed.
    """
    prefix = AGENT_PREFIXES.get(agent_type)
    if prefix is None:
        return str(local_id)
    return f"{prefix}{local_id}"


def local_id(agent_type: str, canonical: str) -> str:
    """Strip the agent prefix from a canonical id, validating it matches.

    Raises ValueError when the canonical id belongs to a different agent.
    """
    prefix = AGENT_PREFIXES.get(agent_type)
    if prefix is None:
        return canonical
    if not canonical.startswith(prefix):
        raise ValueError(
            f"canonical id {canonical!r} does not match agent {agent_type!r} "
            f"(expected prefix {prefix!r})")
    return canonical[len(prefix):]


def local_id_lenient(agent_type: str, canonical: str) -> str:
    """Like local_id but accepts foreign/legacy ids unchanged.

    Used by adapters whose local id format is free-form (codex UUIDs,
    reasonix file stems): a session pushed by hermes arrives as a bare id
    (hermes has no prefix) and must be usable as a local id as-is.
    """
    prefix = AGENT_PREFIXES.get(agent_type)
    if prefix and canonical.startswith(prefix):
        return canonical[len(prefix):]
    return canonical


def validate_local_id(local_id: str) -> bool:
    """Reject ids that could escape the store directory (path traversal).

    Remote ids are untrusted input (other agents' sessions); adapters that
    build file names or keys from them MUST call this before writing.
    """
    if not local_id or local_id in (".", ".."):
        return False
    if any(sep in local_id for sep in ("/", "\\", os.sep, os.altsep or "")):
        return False
    return True


def split_agent_prefix(canonical: str):
    """Server-side helper: map a canonical id to (agent_type, local_id).

    ``hermes`` ids are bare and return (None, id).
    """
    for agent, prefix in AGENT_PREFIXES.items():
        if prefix and canonical.startswith(prefix):
            return agent, canonical[len(prefix):]
    return None, canonical


class Adapter(abc.ABC):
    """Interface every local agent store adapter must implement.

    All methods run inside the MCP server process; read/write are expected
    to be synchronous and reasonably fast (they run in an executor).
    """

    #: agent key, must match an entry in AGENT_PREFIXES and ADAPTERS.
    agent_type: str = ""

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    @abc.abstractmethod
    def discover(self) -> Path | None:
        """Locate this agent's local store. Return None when not installed."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Reading (local -> canonical)
    # ------------------------------------------------------------------
    @abc.abstractmethod
    def read_sessions(self, limit: int | None = None) -> list[dict]:
        """Read local sessions as canonical dicts (each with ``messages``).

        Must return sessions newest-first and include at least id/started_at
        plus as many canonical fields as the local format provides.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Writing (canonical -> local)
    # ------------------------------------------------------------------
    @abc.abstractmethod
    def write_sessions(self, sessions: list[dict]) -> dict:
        """Upsert canonical sessions into the local store.

        Returns a stats dict like:
            {"imported": int, "updated": int, "new_messages": int,
             "duplicates": int}
        Session upsert key is the local id; messages dedupe on
        (session_id, role, timestamp). Implementations MUST never write
        messages under a remote message id -- assign fresh local ids.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    @abc.abstractmethod
    def status(self) -> dict:
        """Return local-store status: total sessions/messages, last update."""
        raise NotImplementedError

    def last_synced_at(self) -> float:
        """Latest sync watermark (0 = never).

        Used to make startup/periodic pulls incremental instead of full
        rescans. Default implementation reads a sidecar file next to the
        store (see save_sync_watermark); subclasses may override.
        """
        f = self._watermark_file()
        if f is not None and f.exists():
            try:
                return float(f.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                return 0.0
        return 0.0

    def save_sync_watermark(self, ts: float):
        """Persist the last successful pull's server-side sync_at."""
        f = self._watermark_file()
        if f is not None:
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(f"{ts:.6f}", encoding="utf-8")
            except OSError:
                pass

    def _watermark_file(self) -> Path | None:
        """Sidecar file for the sync watermark (None = not supported)."""
        return None

    # ------------------------------------------------------------------
    # Foreign id memory (free-form local id agents)
    # ------------------------------------------------------------------
    # Sessions pushed by other agents (hermes bare ids, other prefixes) keep
    # their id as-is in free-form stores (codex file stems, reasonix file
    # names). Remembering those ids lets canonicalize() round-trip them
    # without re-prefixing (which would change identity on the next push).
    def _foreign_ids_file(self) -> Path | None:
        """Sidecar file remembering foreign local ids (None = not supported)."""
        return None

    def _is_foreign(self, local_id: str) -> bool:
        f = self._foreign_ids_file()
        if f is None or not f.exists():
            return False
        try:
            return local_id in json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return False

    def _remember_foreign(self, local_id: str):
        f = self._foreign_ids_file()
        if f is None:
            return
        ids = set()
        if f.exists():
            try:
                ids = set(json.loads(f.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                ids = set()
        if local_id not in ids:
            ids.add(local_id)
            f.write_text(json.dumps(sorted(ids)), encoding="utf-8")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def canonicalize(self, local_session: dict) -> dict:
        """Convert a local session row to canonical form.

        Foreign ids (sessions written into this store by other agents,
        tracked via ``_is_foreign``) keep their id untouched; native ids
        get the agent prefix. Returns a deep-enough copy: caller-owned
        message dicts are never mutated.
        """
        s = dict(local_session)
        lid = str(s["id"])
        if self._is_foreign(lid):
            s["id"] = lid
            s["messages"] = [dict(m) for m in s.get("messages", [])]
            for m in s["messages"]:
                m["session_id"] = lid
            return s
        s["id"] = canonical_id(self.agent_type, s["id"])
        s["messages"] = [dict(m) for m in s.get("messages", [])]
        for m in s["messages"]:
            m["session_id"] = canonical_id(self.agent_type, m.get("session_id", s["id"]))
        return s

    def localize(self, canonical_session: dict, strict: bool = True) -> dict:
        """Convert a canonical session back to local form (id stripping).

        ``strict=False`` (free-form local id agents such as codex/reasonix)
        accepts foreign or legacy ids unchanged instead of raising.
        """
        s = dict(canonical_session)
        resolver = local_id if strict else local_id_lenient
        s["id"] = resolver(self.agent_type, s["id"])
        s["messages"] = [dict(m) for m in s.get("messages", [])]
        for m in s["messages"]:
            m["session_id"] = resolver(self.agent_type, m.get("session_id", s["id"]))
        return s

    @staticmethod
    def _clean(session: dict, fields: tuple) -> dict:
        return {k: v for k, v in session.items()
                if k in fields and v is not None}


class SQLiteAdapter(Adapter):
    """Base class for agents whose local store is a SQLite database.

    Subclasses define ``table_sessions`` / ``table_messages`` and a column
    mapping (local column -> canonical field). Uses the same pragma-based
    column filtering and (session_id, role, timestamp) dedupe as the
    original Hermes implementation.
    """

    table_sessions = "sessions"
    table_messages = "messages"
    #: local col name -> canonical field name (None keeps the same name)
    col_map: dict[str, str | None] = {}

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else self.discover()

    def _conn(self):
        if not self.db_path or not Path(self.db_path).exists():
            raise FileNotFoundError(f"Local DB not found: {self.db_path}")
        # Short busy timeout: the Hermes desktop app opens state.db with
        # journal_mode=DELETE (SQLite 3.50.4 WAL-reset workaround), where a
        # write blocks concurrent readers/writers. Fail fast and let the next
        # sync cycle retry instead of holding/blocking locks for 30s (which
        # stalls Hermes' own session.resume reads).
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _map_cols(row: dict, col_map: dict) -> dict:
        out = {}
        for k, v in row.items():
            canon = col_map.get(k)
            if canon is None:
                canon = k
            if canon and v is not None:
                out[canon] = v
        return out

    def _local_cols(self, c, table: str) -> set:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}

    def read_sessions(self, limit: int | None = None) -> list[dict]:
        conn = self._conn()
        c = conn.cursor()
        sql = "SELECT * FROM %s ORDER BY started_at DESC" % self.table_sessions
        if limit:
            sql += f" LIMIT {int(limit)}"
        sessions = []
        # Materialize rows before issuing nested queries: sqlite3 cursors
        # drop the pending result set when a new query runs on them.
        for row in c.execute(sql).fetchall():
            s = dict(row)
            sid = s["id"]
            c.execute(
                "SELECT * FROM %s WHERE session_id = ? ORDER BY timestamp"
                % self.table_messages, (sid,))
            msgs = [self._map_cols(dict(m), self.col_map)
                    for m in c.fetchall()]
            s = self._map_cols(s, self.col_map)
            s["messages"] = msgs
            sessions.append(s)
        conn.close()
        return sessions

    def write_sessions(self, sessions: list[dict]) -> dict:
        if not sessions:
            return {"imported": 0, "updated": 0, "new_messages": 0,
                    "duplicates": 0}
        conn = self._conn()
        c = conn.cursor()
        s_cols = self._local_cols(c, self.table_sessions)
        m_cols = self._local_cols(c, self.table_messages)
        imported = updated = new_messages = duplicates = 0
        now = time.time()
        for session in sessions:
            s = self.localize(session)
            sid = s["id"]
            msgs = s.pop("messages", [])
            s_data = {k: v for k, v in s.items()
                      if k in s_cols and v is not None}
            c.execute(f"SELECT id FROM {self.table_sessions} WHERE id = ?",
                      (sid,))
            if "last_synced_at" in s_cols:
                s_data.setdefault("last_synced_at", now)
            if c.fetchone():
                s_data.pop("id", None)
                if s_data:
                    set_cl = ", ".join(f"{k} = ?" for k in s_data)
                    c.execute(
                        f"UPDATE {self.table_sessions} SET {set_cl} "
                        f"WHERE id = ?", list(s_data.values()) + [sid])
                updated += 1
            else:
                cols = ", ".join(s_data)
                ph = ", ".join(["?"] * len(s_data))
                c.execute(
                    f"INSERT INTO {self.table_sessions} ({cols}) "
                    f"VALUES ({ph})", list(s_data.values()))
                imported += 1

            for msg in msgs:
                m_data = {k: v for k, v in msg.items()
                          if k in m_cols and v is not None}
                m_sid = m_data.get("session_id", sid)
                role = m_data.get("role")
                ts = m_data.get("timestamp")
                if role is not None and ts is not None:
                    c.execute(
                        f"SELECT 1 FROM {self.table_messages} WHERE "
                        f"session_id = ? AND role = ? AND timestamp = ?",
                        (m_sid, role, ts))
                    if c.fetchone():
                        duplicates += 1
                        continue
                # Content-level fallback, mirroring the server: a session that
                # was rebuilt locally (hermes message-alternation repair after
                # an interrupted turn) re-generates timestamps with time.time(),
                # so identical content arrives under a fresh timestamp. Skip it
                # instead of duplicating the row.
                content = m_data.get("content")
                if role is not None and isinstance(content, str) and content:
                    c.execute(
                        f"SELECT 1 FROM {self.table_messages} WHERE "
                        f"session_id = ? AND role = ? AND content = ?",
                        (m_sid, role, content))
                    if c.fetchone():
                        duplicates += 1
                        continue
                m_data.pop("id", None)
                m_data["session_id"] = sid
                if not m_data:
                    continue
                cols = ", ".join(m_data)
                ph = ", ".join(["?"] * len(m_data))
                c.execute(
                    f"INSERT INTO {self.table_messages} ({cols}) "
                    f"VALUES ({ph})", list(m_data.values()))
                new_messages += 1
        conn.commit()
        conn.close()
        return {"imported": imported, "updated": updated,
                "new_messages": new_messages, "duplicates": duplicates}

    def status(self) -> dict:
        conn = self._conn()
        c = conn.cursor()
        c.execute(f"SELECT COUNT(*) FROM {self.table_sessions}")
        sessions = c.fetchone()[0]
        c.execute(f"SELECT COUNT(*) FROM {self.table_messages}")
        messages = c.fetchone()[0]
        c.execute(f"SELECT MAX(started_at) FROM {self.table_sessions}")
        last = c.fetchone()[0]
        conn.close()
        return {"store": str(self.db_path), "sessions": sessions,
                "messages": messages, "last_started_at": last}


class JSONLAdapter(Adapter):
    """Base class for agents whose local store is one JSONL file per
    session (codex, reasonix). Subclasses implement line-level mapping.
    """

    #: directory holding the per-session files
    sessions_dir: str = ""

    def discover(self) -> Path | None:
        raise NotImplementedError

    def _session_paths(self) -> list[tuple[Path, str]]:
        """Return [(path, local_id)] for every local session file."""
        raise NotImplementedError

    def read_sessions(self, limit: int | None = None) -> list[dict]:
        paths = self._session_paths()
        if limit:
            paths = paths[:limit]
        sessions = []
        for path, local_id in paths:
            s = self._read_session_file(path, local_id)
            if s is not None:
                sessions.append(self.canonicalize(s))
        return sessions

    def _read_session_file(self, path: Path, local_id: str) -> dict | None:
        """Parse one session file into a LOCAL session dict (no prefix)."""
        raise NotImplementedError

    def write_sessions(self, sessions: list[dict]) -> dict:
        raise NotImplementedError
