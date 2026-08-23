"""
Multi-agent adapter contract for Agent Context Sync.

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

# User-editable session fields that participate in field-level optimistic
# concurrency (see docs/ARCHITECTURE.md "字段级乐观并发"). A device only
# pushes these when `local value != sidecar last-known` (dirty), and a pull
# never overwrites a locally-dirty field. Everything else is derived/append
# and keeps legacy last-writer semantics (not dirtiness-tracked). Must match
# USER_EDIT_FIELDS on the server (server/sync.py).
USER_EDIT_FIELDS = frozenset((
    "cwd", "git_branch", "git_repo_root", "title", "pinned", "archived",
    "display_name",
))

# Scalar project fields that participate in the same field-level optimistic
# concurrency (Phase 2, see ARCHITECTURE.md). Project folder paths remain
# unioned by path (append-safe, multi-device coexist); their label/is_primary
# are effectively constant in practice and path-keyed versions would be
# fragile across separator spellings.
PROJECT_USER_EDIT_FIELDS = frozenset((
    "name", "primary_path", "archived", "description",
))

CANONICAL_MESSAGE_FIELDS = (
    "session_id", "role", "content", "timestamp", "id", "token_count",
    "finish_reason", "reasoning", "tool_call_id", "tool_name", "tool_calls",
    "display_kind", "display_metadata", "observed", "active", "compacted",
)


def canonical_id(agent_type: str, local_id) -> str:
    """Build the canonical session id for ``local_id`` in ``agent_type``.

    id-scheme: every agent keeps its bare local id (no prefix); agent
    attribution travels in the ``agent_type`` session field and hermes
    profiles in ``profile_name``. AGENT_PREFIXES remains only for
    recognizing legacy prefixed ids (old servers/clients).
    """
    return str(local_id)


def local_id(agent_type: str, canonical: str) -> str:
    """Map a canonical id to the local form.

    New scheme: identity. A legacy prefixed canonical id (``codex:...``,
    the agent's own prefix) is stripped so pre-migration local data still
    round-trips.
    """
    prefix = AGENT_PREFIXES.get(agent_type)
    if prefix and canonical.startswith(prefix):
        return canonical[len(prefix):]
    return canonical


def local_id_lenient(agent_type: str, canonical: str) -> str:
    """Compatibility alias: same as ``local_id`` (ids are bare now)."""
    return local_id(agent_type, canonical)


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


def validate_file_id(local_id: str) -> bool:
    """``validate_local_id`` plus Windows file-name safety.

    ':' is reserved on Windows: NTFS treats "name:id" as an alternate data
    stream, so ``open("rollout-<ts>-workbuddy:abc.jsonl", "w")`` succeeds
    but writes into a hidden ``:abc.jsonl`` stream — the visible file is a
    0-byte stub the adapter's listing regex never matches. One-file-per-
    session adapters (codex, reasonix) MUST use this; adapters that store
    ids in table columns (hermes, workbuddy, openclaw) keep using
    ``validate_local_id`` (their own ``magic:``/``openclaw:`` ids are
    valid there on every platform).
    """
    if not validate_local_id(local_id):
        return False
    if os.name == "nt" and ":" in local_id:
        return False
    return True


def _path_key(p):
    """Canonical comparison key for a local path: separators normalized to
    '/' and, on Windows, case-folded. `E:\\a\\b` and `E:/a/b` share a key."""
    if not isinstance(p, str):
        return None
    k = p.replace("\\", "/")
    return k.lower() if os.name == "nt" else k


def build_path_map(local_paths):
    """{canonical key -> local actual string} for existing local paths.
    Keys collapse separator (and on Windows case) differences, so a pull
    path that equals an existing local path modulo separator maps back to
    the local spelling."""
    out = {}
    for p in local_paths:
        if not isinstance(p, str) or not p:
            continue
        k = _path_key(p)
        if k is not None and k not in out:
            out[k] = p
    return out


def align_path_to_local(pull_path, local_paths):
    """Rewrite a pull-side path to match an existing local path it equals
    (modulo separator, and on Windows case). Returns the existing local
    path when one matches, else ``pull_path`` untouched."""
    if not isinstance(pull_path, str) or not pull_path:
        return pull_path
    target = build_path_map(local_paths).get(_path_key(pull_path))
    return target if target is not None else pull_path


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

    def _sync_identity(self) -> str | None:
        """Identity of the sync server, used to detect server switches.

        Reads ``HERMES_SYNC_SERVER`` -- the env var the MCP server passes
        down (mcp/server.py SYNC_SERVER). Standalone adapter use (tests,
        scripts) typically has no such env: no identity is then recorded and
        the legacy incremental behavior is kept, so callers that do not
        switch servers are unaffected.
        """
        return os.environ.get("HERMES_SYNC_SERVER") or None

    def _watermark_parts(self) -> tuple[str | None, float | None]:
        """Parse the watermark sidecar -> (server_identity, timestamp).

        v2 format:      "v2 <identity> <timestamp>" (server-bound)
        legacy format:  bare float (predates identity recording)
        Missing/empty/corrupt file yields (None, None).
        """
        f = self._watermark_file()
        if f is None or not f.exists():
            return None, None
        try:
            raw = f.read_text(encoding="utf-8").strip()
        except OSError:
            return None, None
        if not raw:
            return None, None
        parts = raw.split()
        if parts[0] == "v2" and len(parts) >= 3:
            try:
                return parts[1], float(parts[2])
            except ValueError:
                return None, None
        try:
            return None, float(raw)
        except ValueError:
            return None, None

    def last_synced_at(self) -> float:
        """Latest sync watermark (0 = never / full resync).

        Used to make startup/periodic pulls incremental instead of full
        rescans. The watermark is bound to the sync server identity: when
        the recorded identity differs from the current one -- or the file
        predates identity recording (legacy format) -- 0 is returned so the
        next pull is a full resync. Without this, a leftover local watermark
        from a previous server kept every older session below the
        incremental cutoff and they were silently never pulled again.
        """
        ident = self._sync_identity()
        f_ident, ts = self._watermark_parts()
        if ts is None:
            return 0.0
        if ident is None:
            return ts  # no identity available: legacy incremental behavior
        if f_ident is None or f_ident != ident:
            return 0.0  # legacy file or server switched: full resync
        return ts

    def save_sync_watermark(self, ts: float):
        """Persist the last successful pull's server-side sync_at, recording
        the sync server identity so a future server switch is detected."""
        f = self._watermark_file()
        if f is not None:
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                ident = self._sync_identity()
                content = f"v2 {ident} {ts:.6f}\n" if ident else f"{ts:.6f}"
                f.write_text(content, encoding="utf-8")
            except OSError:
                pass

    def _watermark_file(self) -> Path | None:
        """Sidecar file for the sync watermark (None = not supported)."""
        return None

    def field_meta_path(self) -> Path | None:
        """Sidecar file for field-level sync metadata ({sid: {field:
        {"base": rev, "val": value}}}). Lives next to the watermark; None =
        agent does not participate in field-level optimistic merge (falls
        back to legacy full-store push/pull, which the server still accepts).
        """
        wf = self._watermark_file()
        if wf is None:
            return None
        return wf.parent / f".{self.agent_type}-sync-field-meta.json"

    def project_field_meta_path(self) -> Path | None:
        """Sidecar for project field-level sync metadata (mirrors
        field_meta_path but for the project store)."""
        wf = self._watermark_file()
        if wf is None:
            return None
        return wf.parent / f".{self.agent_type}-projects-field-meta.json"

    # ------------------------------------------------------------------
    # Foreign id memory (free-form local id agents)
    # ------------------------------------------------------------------
    # Sessions pushed by other agents keep their bare id in free-form
    # stores (codex file stems, reasonix file names). Remembering those
    # ids lets push_sessions tag the correct owner agent: with the
    # prefix-free id scheme the agent can no longer be derived from the
    # id, so the registry stores {id: agent_type} (legacy plain-id-list
    # files are read as unknown-agent entries and upgraded on write).
    def _foreign_ids_file(self) -> Path | None:
        """Sidecar file remembering foreign local ids (None = not supported)."""
        return None

    def _foreign_ids(self) -> dict[str, str]:
        """{local_id: owner agent or ''} for sessions pulled from the server."""
        f = self._foreign_ids_file()
        if f is None or not f.exists():
            return {}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        if isinstance(data, list):  # legacy plain-id list
            return {str(i): "" for i in data}
        if isinstance(data, dict):
            return {str(k): str(v or "") for k, v in data.items()}
        return {}

    def _is_foreign(self, local_id: str) -> bool:
        return local_id in self._foreign_ids()

    def _foreign_agent(self, local_id: str) -> str:
        """Owner agent of a foreign local id ('' when unknown)."""
        return self._foreign_ids().get(local_id, "")

    def _remember_foreign(self, local_id: str, agent: str = ""):
        f = self._foreign_ids_file()
        if f is None:
            return
        ids = self._foreign_ids()
        if local_id not in ids or ids[local_id] != agent:
            ids[local_id] = agent
            f.write_text(json.dumps(ids, ensure_ascii=False, sort_keys=True),
                         encoding="utf-8")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def canonicalize(self, local_session: dict) -> dict:
        """Convert a local session row to canonical form.

        id-scheme: canonical ids are bare for every agent (no prefix);
        attribution lives in the ``agent_type``/``profile_name`` fields.
        Legacy prefixed local ids (old data) pass through unchanged — the
        server's inbound shim normalizes them. Returns a deep-enough copy:
        caller-owned message dicts are never mutated.
        """
        s = dict(local_session)
        lid = str(s["id"])
        s["id"] = lid
        s["messages"] = [dict(m) for m in s.get("messages", [])]
        for m in s["messages"]:
            m["session_id"] = m.get("session_id") or lid
        return s

    def localize(self, canonical_session: dict, strict: bool = True) -> dict:
        """Convert a canonical session back to local form (id stripping).

        ids are bare; a legacy agent-prefixed canonical id is stripped so
        pre-migration payloads still land in the local store.
        """
        s = dict(canonical_session)
        s["id"] = local_id(self.agent_type, str(s["id"]))
        s["messages"] = [dict(m) for m in s.get("messages", [])]
        for m in s["messages"]:
            m["session_id"] = local_id(self.agent_type,
                                       str(m.get("session_id", s["id"])))
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
        # Hermes 0.20+ enforces a partial UNIQUE index on sessions.title
        # (WHERE title IS NOT NULL). Auto-generated titles repeat across
        # sessions, so a pulled session whose title collides with an
        # existing local row would fail the INSERT and abort the whole
        # batch — repeatedly, since the batch rolls back as one
        # transaction. Detect the constraint and disambiguate with a
        # " (N)" suffix (mirroring the desktop app) only when it exists;
        # other stores without the constraint keep titles untouched.
        title_unique = False
        for _seq, _name, _unique, _origin, _partial in c.execute(
                f"PRAGMA index_list({self.table_sessions})"):
            if not _unique:
                continue
            cols = [r[2] for r in c.execute(f"PRAGMA index_info({_name})")]
            if "title" in cols:
                title_unique = True
                break

        def _disambiguate_title(title, exclude_id=None):
            n = 2
            while True:
                cand = f"{title} ({n})"
                args = [cand]
                q = (f"SELECT 1 FROM {self.table_sessions} "
                     f"WHERE title = ?")
                if exclude_id is not None:
                    q += " AND id <> ?"
                    args.append(exclude_id)
                c.execute(q, args)
                if not c.fetchone():
                    return cand
                n += 1
        for session in sessions:
            s = self.localize(session)
            sid = s["id"]
            msgs = s.pop("messages", [])
            s_data = {k: v for k, v in s.items()
                      if k in s_cols and v is not None}
            # Foreign sessions (pulled from the shared pool) may lack
            # columns the local schema declares NOT NULL (e.g. hermes'
            # `source`). Backfill them so inserts never violate constraints:
            # REAL/time columns get `now`, anything else gets "".
            for _name, _typ, _notnull, _dflt in (
                    (r[1], r[2], r[3], r[4])
                    for r in c.execute(
                        f"PRAGMA table_info({self.table_sessions})")):
                if _notnull and _dflt is None and _name not in s_data:
                    s_data[_name] = now if (
                        "time" in _name.lower() or _typ == "REAL") else ""
            c.execute(f"SELECT id FROM {self.table_sessions} WHERE id = ?",
                      (sid,))
            if "last_synced_at" in s_cols:
                s_data.setdefault("last_synced_at", now)
            if c.fetchone():
                if title_unique and s_data.get("title"):
                    c.execute(
                        f"SELECT 1 FROM {self.table_sessions} "
                        f"WHERE title = ? AND id <> ?",
                        (s_data["title"], sid))
                    if c.fetchone():
                        s_data["title"] = _disambiguate_title(
                            s_data["title"], exclude_id=sid)
                s_data.pop("id", None)
                if s_data:
                    set_cl = ", ".join(f"{k} = ?" for k in s_data)
                    c.execute(
                        f"UPDATE {self.table_sessions} SET {set_cl} "
                        f"WHERE id = ?", list(s_data.values()) + [sid])
                updated += 1
            else:
                if title_unique and s_data.get("title"):
                    c.execute(
                        f"SELECT 1 FROM {self.table_sessions} "
                        f"WHERE title = ?", (s_data["title"],))
                    if c.fetchone():
                        s_data["title"] = _disambiguate_title(
                            s_data["title"])
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
