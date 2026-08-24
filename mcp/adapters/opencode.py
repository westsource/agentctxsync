"""
opencode (desktop / CLI 1.x) adapter -- SQLite store.

Local store: <XDG_DATA_HOME or %LOCALAPPDATA% or ~/.local/share>/opencode/opencode.db
  opencode CLI and the desktop app share ONE SQLite database (confirmed:
  `opencode db path` == the desktop's opencode.db). Sessions live in the
  ``session`` table, messages in ``message`` (role + time in a data JSON),
  content parts in ``part`` (text/reasoning/tool discrimated-union JSON).

  The previous adapter targeted the OLD open-source JSON layout
  (storage/session/info/*.json) which the 1.x desktop/CLI no longer writes --
  it saw 0 sessions. This adapter reads the shared opencode.db.

We write pulled/synced data back into the SAME opencode.db with the same row
shapes the app produces (id prefixed ses_/msg_/prt_, ms timestamps, project_id
defaulting to the existing 'global' project, unique slug, version header), so
foreign sessions round-trip without a second store. The desktop app may not
render foreign rows (its own UI reads them as best-effort) -- sync/CLI reads
still work, matching the cross-agent round-trip behaviour used elsewhere.
"""

import json
import os
import re
import secrets
import sqlite3
import string
import time
from contextlib import contextmanager
from pathlib import Path

from .base import Adapter, canonical_id, local_id_lenient, validate_local_id

_IDMAP = ".hermes-sync-idmap.json"      # canonical foreign id -> local ses_ id
_VERSION = "1.17.15"                    # version header stamped on written sessions
_ID_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase


def _gen_id(prefix: str) -> str:
    """opencode-style id: <prefix>_ + 12 hex ts + 14 base62 random."""
    ts = int(time.time() * 1000)
    rand = "".join(secrets.choice(_ID_ALPHABET) for _ in range(14))
    return f"{prefix}_{ts:012x}{rand}"


def _candidates_common() -> list[Path]:
    """All plausible <base>/opencode/opencode.db paths, most-specific first."""
    bases = []
    for k in ("XDG_DATA_HOME", "LOCALAPPDATA"):
        v = os.environ.get(k)
        if v:
            bases.append(Path(v))
    bases.append(Path.home() / ".local" / "share")
    return [b / "opencode" / "opencode.db" for b in bases]


def _unique_slug(base: str) -> str:
    """opencode slug: lowercase alnum/dash; caller ensures uniqueness."""
    slug = re.sub(r"[^a-z0-9]+", "-", (base or "session").lower()).strip("-")
    return slug[:64] or "session"


class OpencodeAdapter(Adapter):
    """opencode SQLite (opencode.db) adapter."""

    agent_type = "opencode"

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else self.discover()

    # ------------------------------------------------------------------
    def discover(self) -> Path | None:
        for p in _candidates_common():
            if p.is_file():
                return p
        return None

    # ------------------------------------------------------------------
    # connections
    # ------------------------------------------------------------------
    def _conn(self, ro: bool = False) -> sqlite3.Connection:
        if not self.db_path:
            raise FileNotFoundError("opencode.db not found")
        if ro:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True,
                                   timeout=5)
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=5,
                                   isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connect(self, ro: bool = False):
        """Context manager that reliably CLOSES the connection (sqlite3's
        ``with conn`` only commits/rolls back -- on Windows an unclosed
        handle locks the db file and blocks cleanup/tests)."""
        conn = self._conn(ro)
        try:
            yield conn
        finally:
            conn.close()

    def _watermark_file(self) -> Path | None:
        if self.db_path:
            return self.db_path.with_name(
                self.db_path.name + ".hermes-sync-watermark")
        return None

    # ------------------------------------------------------------------
    # foreign-id / owner registry (keyed by CANONICAL id, mirrors base)
    # ------------------------------------------------------------------
    def _registry_files(self) -> tuple[Path, Path]:
        base = self.db_path.with_name(self.db_path.name)
        return base.with_name(".hermes-sync-idmap.json"), \
            base.with_name(".hermes-sync-foreign.json")

    def _idmap(self) -> dict[str, str]:
        p, _ = self._registry_files()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_idmap(self, m: dict[str, str]):
        p, _ = self._registry_files()
        p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    def _foreign_ids(self) -> dict[str, str]:
        _, p = self._registry_files()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember_foreign(self, canonical_id: str, agent: str = ""):
        f = self._foreign_ids()
        if f.get(canonical_id) != agent:
            f[canonical_id] = agent
            self._registry_files()[1].write_text(
                json.dumps(f, ensure_ascii=False), encoding="utf-8")

    def _local_id_for(self, canonical: str) -> str:
        """Canonical id -> local opencode ses_ id (own pass-through, foreign
        via idmap with a fresh id)."""
        local = local_id_lenient(self.agent_type, canonical)
        if local.startswith("ses_") and len(local) > 4 and validate_local_id(local):
            return local
        m = self._idmap()
        if canonical in m:
            return m[canonical]
        fresh = _gen_id("ses")
        m[canonical] = fresh
        self._save_idmap(m)
        return fresh

    # ------------------------------------------------------------------
    # reading: opencode.db -> canonical
    # ------------------------------------------------------------------
    def read_sessions(self, limit: int | None = None) -> list[dict]:
        if not self.db_path or not self.db_path.is_file():
            return []
        own_to_canon = {v: k for k, v in self._idmap().items()}  # ses_ -> foreign canon
        with self._connect(ro=True) as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM session ORDER BY time_created DESC").fetchall()
            if limit:
                rows = rows[:limit]
            out = []
            for row in rows:
                s = self._session_canonical(cur, row, own_to_canon)
                if s is not None:
                    out.append(s)
        return out

    def _session_canonical(self, cur, row, own_to_canon: dict) -> dict | None:
        sid = row["id"]
        canonical = own_to_canon.get(sid, canonical_id(self.agent_type, sid))
        s = {
            "id": canonical,
            "started_at": (row["time_created"] or 0) / 1000.0,
            "messages": [],
        }
        if row["time_updated"]:
            s["ended_at"] = row["time_updated"] / 1000.0
        if row["title"]:
            s["title"] = row["title"]
        if row["directory"]:
            s["cwd"] = row["directory"]
        if row["model"]:
            s["model"] = row["model"]
        meta = {}
        if row["project_id"] and row["project_id"] != "global":
            meta["opencode:projectID"] = row["project_id"]
        if row["tokens_input"] or row["tokens_output"] or row["tokens_reasoning"]:
            meta["opencode:tokens"] = {
                "input": row["tokens_input"], "output": row["tokens_output"],
                "reasoning": row["tokens_reasoning"],
            }
        if meta:
            s["meta"] = meta
        # messages (chronological). Materialize ALL rows first: running a new
        # query on the same cursor would otherwise clobber the open resultset.
        msgs_rows = cur.execute(
            "SELECT * FROM message WHERE session_id=? ORDER BY time_created",
            (sid,)).fetchall()
        msgs = []
        for mrow in msgs_rows:
            cmsg = self._message_canonical(cur, mrow, canonical)
            if cmsg:
                msgs.append(cmsg)
        s["messages"] = msgs
        s["message_count"] = len(msgs)
        if not s["started_at"]:
            s["started_at"] = row["time_created"] or time.time()
        return self.canonicalize(s)

    def _message_canonical(self, cur, mrow, session_canonical: str) -> dict | None:
        try:
            data = json.loads(mrow["data"]) if mrow["data"] else {}
        except (ValueError, TypeError):
            data = {}
        role = data.get("role") or data.get("type") or "assistant"
        if role in ("agent-switched", "model-switched", "compaction", "step"):
            return None
        if role == "shell":
            role = "tool"
        text, reasoning, tool_refs = [], [], []
        for prow in cur.execute(
                "SELECT * FROM part WHERE message_id=? ORDER BY time_created",
                (mrow["id"],)):
            try:
                part = json.loads(prow["data"]) if prow["data"] else {}
            except (ValueError, TypeError):
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text", "output_text") and part.get("text"):
                text.append(str(part["text"]))
            elif ptype == "reasoning" and part.get("text"):
                reasoning.append(str(part["text"]))
            elif ptype == "tool":
                tname = part.get("tool")
                state = part.get("state") or {}
                inp = state.get("input") if isinstance(state, dict) else None
                if tname:
                    tool_refs.append(f"[tool:{tname}] {inp or ''}".rstrip())
        content = "\n".join(text)
        if tool_refs:
            content = (content + "\n" if content else "") + "\n".join(tool_refs)
        if not content and not reasoning:
            return None
        out = {"session_id": session_canonical, "role": role or "assistant",
               "content": content,
               "timestamp": (mrow["time_created"] or time.time()) / 1000.0}
        if reasoning:
            out["reasoning"] = "\n".join(reasoning)
        model = data.get("model")
        if isinstance(model, dict):
            m = model.get("modelID")
            if m:
                out["model"] = str(m)
        return out

    # ------------------------------------------------------------------
    # writing: canonical -> opencode.db rows
    # ------------------------------------------------------------------
    def write_sessions(self, sessions: list[dict]) -> dict:
        if not self.db_path:
            return {"error": "opencode.db not found"}
        (self.db_path.parent / "session").mkdir(parents=True, exist_ok=True)
        stats = {"imported": 0, "updated": 0, "new_messages": 0, "duplicates": 0}
        with self._connect() as conn:
            cur = conn.cursor()
            taken = {r[0] for r in cur.execute("SELECT id FROM session")}
            for session in sessions:
                s = dict(session)
                msgs = s.pop("messages", [])
                canonical = str(s.get("id", ""))
                sid = self._local_id_for(canonical)
                exists = sid in taken
                now_s = time.time()
                title = (s.get("title") or "untitled")
                slug = _unique_slug(title)
                # ensure slug uniqueness like the desktop
                n = 2
                while slug in {r[0] for r in cur.execute(
                        "SELECT slug FROM session WHERE id != ?", (sid,))}:
                    slug = f"{_unique_slug(title)[:60]}-{n}"; n += 1
                # project_id is NOT NULL with an FK to project; default to the
                # always-present 'global' project unless the session names one.
                project_id = "global"
                if isinstance(s.get("meta"), dict):
                    project_id = s.get("meta", {}).get("opencode:projectID") or "global"
                if exists:
                    cur.execute(
                        "UPDATE session SET title=?, directory=?, model=?, "
                        "time_updated=?, parent_id=? WHERE id=?",
                        (title, s.get("cwd") or "", s.get("model"),
                         int((s.get("ended_at") or now_s) * 1000),
                         s.get("parent_session_id"), sid))
                    stats["updated"] += 1
                else:
                    cur.execute(
                        "INSERT INTO session (id, project_id, parent_id, slug, "
                        "directory, title, version, time_created, time_updated, "
                        "cost, tokens_input, tokens_output, tokens_reasoning, "
                        "tokens_cache_read, tokens_cache_write, agent, model) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, project_id, s.get("parent_session_id"), slug,
                         s.get("cwd") or "", title, _VERSION,
                         int((s.get("started_at") or now_s) * 1000),
                         int((s.get("ended_at") or now_s) * 1000),
                         0.0, 0, 0, 0, 0, 0, "opencode", s.get("model")))
                    taken.add(sid)
                    stats["imported"] += 1
                    # own opencode ids are bare ses_...; anything pushed under a
                    # different canonical id is a foreign session -- register its
                    # owner so push_sessions tags agent_type correctly.
                    if not local_id_lenient(self.agent_type, canonical).startswith("ses_"):
                        self._remember_foreign(canonical, s.get("agent_type") or "")
                # messages
                msg_dir_ids = {r[0] for r in cur.execute(
                    "SELECT id FROM part WHERE session_id=?", (sid,))}
                existing_triples = set()
                for mrow in cur.execute(
                        "SELECT id, time_created, data FROM message WHERE session_id=?",
                        (sid,)):
                    try:
                        role = (json.loads(mrow["data"] or "{}") or {}).get("role")
                    except (ValueError, TypeError):
                        role = None
                    existing_triples.add((role, mrow["time_created"]))
                for m in msgs:
                    ms = int((m.get("timestamp") or now_s) * 1000)
                    key = (m.get("role"), ms)
                    if key in existing_triples:
                        stats["duplicates"] += 1
                        continue
                    mid = _gen_id("msg")
                    data = {"role": m.get("role", "assistant"),
                            "time": {"created": ms}}
                    if m.get("model"):
                        data["model"] = {"modelID": m["model"]}
                    cur.execute(
                        "INSERT INTO message (id, session_id, time_created, "
                        "time_updated, data) VALUES (?,?,?,?,?)",
                        (mid, sid, ms, ms, json.dumps(data, ensure_ascii=False)))
                    existing_triples.add(key)
                    parts = []
                    if m.get("content"):
                        parts.append({"type": "text", "text": m["content"]})
                    if m.get("reasoning"):
                        parts.append({"type": "reasoning", "text": m["reasoning"]})
                    if m.get("tool_name"):
                        parts.append({"type": "tool", "tool": m["tool_name"],
                                      "callID": m.get("tool_call_id"),
                                      "state": {"input": m.get("content", ""),
                                                "status": "completed"}})
                    for pt in parts or [{"type": "text", "text": ""}]:
                        pid = _gen_id("prt")
                        cur.execute(
                            "INSERT INTO part (id, message_id, session_id, "
                            "time_created, time_updated, data) VALUES (?,?,?,?,?,?)",
                            (pid, mid, sid, ms, ms,
                             json.dumps(pt, ensure_ascii=False)))
                    stats["new_messages"] += 1
        return stats

    # ------------------------------------------------------------------
    def status(self) -> dict:
        if not self.db_path or not self.db_path.is_file():
            return {"store": str(self.db_path), "sessions": 0, "messages": 0}
        with self._connect(ro=True) as conn:
            cur = conn.cursor()
            s = cur.execute("SELECT COUNT(*) FROM session").fetchone()[0]
            m = cur.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        return {"store": str(self.db_path), "sessions": s, "messages": m}


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = OpencodeAdapter


if __name__ == "__main__":
    a = OpencodeAdapter()
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions()))
