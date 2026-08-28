r"""
Pi / Oh My Pi adapter (shared JSONL v3 session format).

Pi (earendil-works/pi, formerly badlogic/pi-mono) and Oh My Pi
(can1357/oh-my-pi, a fork) share the same on-disk session store:

    <agent_dir>/sessions/<encoded-cwd>/<timestamp>_<uuidv7>.jsonl

where <encoded-cwd> is ``--`` + cwd with leading slashes stripped and
``/`` ``\\`` ``:`` replaced by ``-`` + ``--`` (pi's getDefaultSessionDirPath;
verified byte-identical against omp 18.0.4 local data). Each file is a
JSONL event stream, version 3:

    {"type":"session","version":3,"id":uuidv7,"timestamp":ISO,"cwd":...}
    {"type":"model_change", ...}
    {"type":"message","id":8-hex,"parentId":<prev-id>,"timestamp":ISO,
     "message":{"role","content":[...blocks...],"timestamp":ms}}
    {"type":"custom","customType":...,"data":...}   (tool execution, etc.)
    {"type":"compaction"|"branch_summary"|"label"|"session_info"|...}

Pi roots:  ~/.pi/agent        (env PI_CODING_AGENT_DIR overrides the agent dir)
Omp roots: ~/.omp/agent       (env OMP_CODING_AGENT_DIR overrides the agent dir)
Windows:   %USERPROFILE%\.pi\agent  /  %USERPROFILE%\.omp\agent  (backslashes)

Read rules
----------
- message entries -> canonical messages; content block lists are normalized
  (text blocks -> content, thinking -> reasoning, tool-ish blocks -> tool
  role + name/call_id); anything else is skipped.
- omp extensions are tolerated: a leading ``title`` record, ``title_change``
  events, header ``title``/``titleSource`` fields, and ``model_change`` with
  ``{model, resolvedModelIsFallback}`` instead of pi's ``{provider, modelId}``.
- title: omp ``title``/``title_change`` and pi ``session_info`` (name) feed
  ``session.title`` (last wins).
- compaction/branch_summary entries become assistant summary messages (like
  the deepseek-harness ``compacted`` handling) so compressed context is not
  lost; they are tagged ``meta.pi:entry_type``.
- branch topology (entry id/parentId/leaf) is NOT preserved: messages are
  emitted in file order with the (role, timestamp) dedupe triple kept unique
  (deterministic +1ms nudge on collisions, same pattern as deepseek-harness
  ``_unique_ts`` -- pi stamps bursts of entries with the same millisecond).

Write rules
-----------
- append-only: existing files get new message entries chained off the last
  entry id (8-hex, pi's own short-id scheme); new sessions get a fresh file
  (header first, pi-compatible, so a pi binary can load it) under the
  encoded-cwd dir of the session's cwd.
- title event follows the target store: pi roots get a ``session_info``
  entry, omp roots a ``title_change`` entry.
- messages dedupe locally on (role, round(ts*1000)) against the file, the
  server stays the dedupe authority for cross-device pushes.
- foreign ids that fail Windows file-name validation are skipped (never
  import an untrusted remote id onto disk); foreign sessions are registered
  in the owner sidecar so push tags the correct agent_type.
"""

import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import JSONLAdapter, validate_file_id

#: entry types that carry conversation content
_MESSAGE_ROLES = ("user", "assistant", "system", "tool", "toolResult")

_TS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z)_(?P<id>.+)\.jsonl$")


def _iso_ts(ts: float) -> str:
    """ISO-8601 UTC with 'Z' and ms precision (pi header/timestamp format)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (dt.strftime("%Y-%m-%dT%H:%M:%S.") +
            f"{int((ts - int(ts)) * 1000):03d}Z")


def _parse_ts(value) -> float | None:
    """Parse an RFC3339/ISO timestamp or epoch (seconds or ms) to seconds."""
    if isinstance(value, (int, float)):
        if value > 1e12:  # milliseconds
            return value / 1000.0
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _blocks_to_text(content) -> tuple[str, str, list[dict]]:
    """Normalize pi content (str or block list) to
    (text, reasoning, tool_refs). tool_refs is a list of
    {"tool_name","tool_call_id","content"} for tool-ish blocks."""
    if isinstance(content, str):
        return content, "", []
    if not isinstance(content, list):
        return str(content) if content is not None else "", "", []
    text: list[str] = []
    reasoning: list[str] = []
    tools: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = (block.get("type") or "").lower()
        if btype in ("text", "input_text", "output_text"):
            if block.get("text"):
                text.append(str(block["text"]))
        elif btype in ("thinking", "reasoning"):
            val = block.get("thinking")
            if val is None:
                val = block.get("text") or block.get("reasoning")
            if val:
                reasoning.append(str(val))
        elif "tool" in btype:
            name = (block.get("name") or block.get("tool")
                    or block.get("toolName") or "")
            cid = (block.get("call_id") or block.get("callId")
                   or block.get("id") or "")
            arg = block.get("arguments")
            if arg is None:
                arg = block.get("input") or block.get("content")
            if isinstance(arg, (dict, list)):
                arg = json.dumps(arg, ensure_ascii=False)
            tools.append({"tool_name": str(name or ""),
                          "tool_call_id": str(cid or ""),
                          "content": str(arg or "")})
    return "\n".join(t for t in text if t), "\n".join(reasoning), tools


def _content_blocks(text: str, reasoning: str) -> list[dict]:
    """Canonical content + reasoning -> pi content block list."""
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    if reasoning:
        blocks.append({"type": "thinking", "thinking": reasoning})
    return blocks


def _encode_cwd(cwd: str) -> str:
    """cwd -> session dir name (pi getDefaultSessionDirPath encoding)."""
    c = (cwd or "").replace("/", "\\")
    c = re.sub(r"^[\\/]", "", c)
    c = re.sub(r"[\\/:]", "-", c)
    return f"--{c}--"


class PiAdapter(JSONLAdapter):
    """Pi session-store adapter (canonical ids bare, uuidv7 native)."""

    agent_type = "pi"
    #: env var overriding the agent dir (pi's PI_CODING_AGENT_DIR pattern)
    _env_dir = "PI_CODING_AGENT_DIR"
    #: per-platform agent dir name under the home dir
    _home_dir = ".pi"

    def __init__(self, store_dir: Path | str | None = None):
        self.sessions_dir = Path(store_dir) if store_dir else self.discover()

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    def _agent_dir(self) -> Path | None:
        env = os.environ.get(self._env_dir)
        if env:
            return Path(env)
        return Path.home() / self._home_dir / "agent"

    def discover(self) -> Path | None:
        root = self._agent_dir()
        d = root / "sessions" if root else None
        return d if d and d.is_dir() else None

    def _foreign_ids_file(self) -> Path | None:
        if self.sessions_dir:
            return self.sessions_dir / ".hermes-sync-foreign-ids.json"
        return None

    def _watermark_file(self) -> Path | None:
        if self.sessions_dir:
            return self.sessions_dir / ".hermes-sync-watermark"
        return None

    # ------------------------------------------------------------------
    # session file listing
    # ------------------------------------------------------------------
    def _session_paths(self) -> list[tuple[Path, str]]:
        if not self.sessions_dir or not self.sessions_dir.is_dir():
            return []
        out = []
        for p in sorted(self.sessions_dir.rglob("*.jsonl"), reverse=True):
            if p.name.startswith(".hermes-sync-"):
                continue
            m = _TS_RE.match(p.name)
            if m:  # <ts>_<id>.jsonl: local id is the suffix after the ts
                out.append((p, m.group("id")))
        return out

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
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
        try:
            lines = path.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
        except OSError:
            return None
        session: dict = {"id": local_id, "started_at": 0.0, "messages": []}
        used_ts: set = set()  # (role, timestamp) triples already emitted
        model = None
        title = None
        # v1 files have no per-entry ids; synthesize them deterministically.
        next_id = 0

        def gen_id() -> str:
            nonlocal next_id
            next_id += 1
            return f"v1-{next_id:04x}"

        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            etype = row.get("type")
            if etype == "session":
                if not session["started_at"]:
                    ts = _parse_ts(row.get("timestamp"))
                    if ts:
                        session["started_at"] = ts
                if row.get("cwd") and not session.get("cwd"):
                    session["cwd"] = row["cwd"]
                if row.get("id"):
                    session["id"] = str(row["id"])  # header id is authoritative
                if row.get("title") and not title:  # omp header title
                    title = str(row["title"])
                continue
            if etype in ("title", "title_change"):  # omp title records
                t = row.get("title")
                if t:
                    title = str(t)
                continue
            if etype == "session_info":  # pi display-name entry
                t = row.get("name")
                if t is not None:
                    title = str(t).strip() or None
                continue
            if etype == "model_change":
                m = (row.get("model")  # omp: {model, ...}
                     or row.get("modelId") or row.get("provider"))  # pi
                if m and m not in ("unknown", "custom"):
                    model = str(m)
                continue
            if etype in ("custom", "label", "thinking_level_change"):
                continue  # extension/tool state, bookmarks: not conversation
            if etype == "compaction" or etype == "branch_summary":
                # summarized context -> assistant summary message (lossless)
                summary = row.get("summary")
                ts = _parse_ts(row.get("timestamp"))
                if summary and ts is not None:
                    ts = self._unique_ts(used_ts, "assistant", ts)
                    session["messages"].append({
                        "session_id": session["id"], "role": "assistant",
                        "content": str(summary), "timestamp": ts,
                        "meta": {"pi:entry_type": etype}})
                continue
            if etype != "message":
                continue
            msg = row.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role not in _MESSAGE_ROLES:
                continue
            text, reasoning, tools = _blocks_to_text(msg.get("content"))
            ts = _parse_ts(msg.get("timestamp") or row.get("timestamp"))
            if ts is None:
                # no reliable timestamp: stable monotonic fallback keeps the
                # dedupe triple unique (same pattern as reasonix)
                base = session["started_at"] or path.stat().st_mtime
                ts = base + (len(session["messages"]) / 10.0)
            if tools:
                for t in tools:
                    tts = self._unique_ts(used_ts, "tool", ts)
                    m = {"session_id": session["id"], "role": "tool",
                         "content": t["content"], "timestamp": tts,
                         "tool_name": t["tool_name"]}
                    if t["tool_call_id"]:
                        m["tool_call_id"] = t["tool_call_id"]
                    session["messages"].append(m)
                ts = self._unique_ts(used_ts, role, ts + 0.001)
            else:
                ts = self._unique_ts(used_ts, role, ts)
            if not text and not reasoning:
                continue
            m = {"session_id": session["id"], "role": role,
                 "content": text, "timestamp": ts}
            if reasoning:
                m["reasoning"] = reasoning
            session["messages"].append(m)
        if not session["started_at"]:
            m = _TS_RE.match(path.name)
            if m:
                session["started_at"] = _parse_ts(m.group("ts")) or 0.0
        if not session["started_at"]:
            session["started_at"] = path.stat().st_mtime
        if model and not session.get("model"):
            session["model"] = model
        if title:
            session["title"] = title
        session["message_count"] = len(session["messages"])
        return session

    @staticmethod
    def _unique_ts(used: set, role: str, ts: float) -> float:
        """Make the (role, timestamp) dedupe triple unique per session.

        pi stamps bursts of entries with the same millisecond; the pool
        dedupes on (session_id, role, timestamp), so two distinct messages
        sharing a triple would silently collapse on pull/push round-trips.
        Nudge colliding timestamps +1ms until free (deterministic).
        """
        while (role, ts) in used:
            ts += 0.001
        used.add((role, ts))
        return ts

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    def _existing_path(self, local_id: str) -> Path | None:
        """Find the session file for ``local_id`` (suffix of the file name)."""
        if not self.sessions_dir or not self.sessions_dir.is_dir():
            return None
        for p in sorted(self.sessions_dir.rglob("*.jsonl")):
            if p.name.startswith(".hermes-sync-"):
                continue
            m = _TS_RE.match(p.name)
            if m and m.group("id") == local_id:
                return p
        return None

    def write_sessions(self, sessions: list[dict]) -> dict:
        stats = {"imported": 0, "updated": 0,
                 "new_messages": 0, "duplicates": 0}
        if not sessions or not self.sessions_dir:
            return stats
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        for session in sessions:
            s = self.localize(session, strict=False)
            local_id = str(s["id"])
            if session.get("agent_type") not in (None, self.agent_type):
                self._remember_foreign(local_id, session.get("agent_type"))
            if not validate_file_id(local_id):
                continue  # untrusted remote id: skip
            msgs = s.pop("messages", []) or []
            cwd = str(s.get("cwd") or Path.home())
            cwd_dir = self.sessions_dir / _encode_cwd(cwd)
            path = self._existing_path(local_id)
            title = s.get("title")
            if path is None:
                cwd_dir.mkdir(parents=True, exist_ok=True)
                path = cwd_dir / f"{_iso_ts(s.get('started_at') or time.time()).replace(':', '-').replace('.', '-')}_{local_id}.jsonl"
                written, _parent = self._write_new_transcript(
                    path, local_id, cwd, s, msgs)
                stats["imported"] += 1
                stats["new_messages"] += written
            else:
                parent = self._tail_id(path)
                written = self._append_messages(path, parent, msgs, stats)
                stats["updated"] += 1
                stats["new_messages"] += written
            if title and self._title_changed(path, title):
                self._append_title(path, title)
        return stats

    def _write_new_transcript(self, path: Path, local_id: str, cwd: str,
                              session: dict, msgs: list) -> tuple[int, str]:
        """Create a fresh session file (header first, pi-compatible).

        The header stays pure pi format (no title field); the title event is
        appended separately by ``_append_title`` so pi/omp each get their own
        title entry type.
        """
        now = time.time()
        started = session.get("started_at") or now
        lines = [{
            "type": "session", "version": 3, "id": local_id,
            "timestamp": _iso_ts(started), "cwd": cwd,
        }]
        parent = None
        for m in msgs:
            parent = self._message_line(lines, parent, m, m.get("timestamp") or now)
        path.write_text("".join(json.dumps(x) + "\n" for x in lines),
                        encoding="utf-8")
        return len(msgs), parent or ""

    def _append_messages(self, path: Path, parent: str | None,
                         msgs: list, stats: dict) -> int:
        """Append non-duplicate messages; returns count written."""
        known = set()
        try:
            for line in path.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("type") != "message":
                    continue
                m = row.get("message")
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                ts = _parse_ts(m.get("timestamp") or row.get("timestamp"))
                if role is not None and ts is not None:
                    known.add((role, int(round(ts * 1000))))
        except OSError:
            return 0
        lines: list[dict] = []
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
            parent = self._message_line(lines, parent, m, ts)
        if not lines:
            return 0
        with path.open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))
        return len(lines)

    def _append_title(self, path: Path, title: str):
        """Title event matching the target store:
        pi roots get ``session_info``, omp roots get ``title_change``."""
        if self.agent_type == "omp":
            entry = {"type": "title_change", "id": secrets.token_hex(4),
                     "parentId": self._tail_id(path),
                     "timestamp": _iso_ts(time.time()), "title": title[:80],
                     "source": "sync"}
        else:
            entry = {"type": "session_info", "id": secrets.token_hex(4),
                     "parentId": self._tail_id(path),
                     "timestamp": _iso_ts(time.time()),
                     "name": title[:80]}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    def _title_changed(self, path: Path, title: str) -> bool:
        """True when the last recorded title event differs from ``title``.

        Prevents the pull from appending an identical session_info /
        title_change record on every sync cycle (unbounded file growth).
        Compares against the 80-char truncation used on write.
        """
        want = title[:80]
        try:
            lines = path.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
        except OSError:
            return True
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            t = row.get("type")
            if t == "session_info":
                return row.get("name") != want
            if t == "title_change":
                return row.get("title") != want
        return True

    def _tail_id(self, path: Path | None) -> str | None:
        """Last entry id in the transcript (parent for appended lines)."""
        if path is None:
            return None
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("id"):
                return str(row["id"])
        return None

    @staticmethod
    def _message_line(lines: list, parent: str | None, m: dict,
                      ts: float) -> str:
        """Append one message event; returns its id (new parent)."""
        mid = secrets.token_hex(4)
        role = m.get("role") or "assistant"
        content = str(m.get("content") or "")
        reasoning = m.get("reasoning") or ""
        blocks = _content_blocks(content, reasoning)
        lines.append({
            "type": "message", "id": mid, "parentId": parent,
            "timestamp": _iso_ts(float(ts)),
            "message": {
                "role": role, "content": blocks,
                "timestamp": int(round(float(ts) * 1000)),
            },
        })
        return mid

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        if not self.sessions_dir or not self.sessions_dir.is_dir():
            return {"store": str(self.sessions_dir), "error": "not found"}
        sessions = 0
        messages = 0
        last = None
        for path, _lid in self._session_paths():
            sessions += 1
            try:
                for line in path.read_text(encoding="utf-8",
                                           errors="replace").splitlines():
                    if '"type": "message"' in line or '"type":"message"' in line:
                        messages += 1
            except OSError:
                continue
            try:
                t = path.stat().st_mtime
                last = max(last, t) if last is not None else t
            except OSError:
                continue
        return {"store": str(self.sessions_dir), "sessions": sessions,
                "messages": messages, "last_started_at": last}


class OmpAdapter(PiAdapter):
    """Oh My Pi adapter: identical store, different root + title events."""

    agent_type = "omp"
    _env_dir = "OMP_CODING_AGENT_DIR"
    _home_dir = ".omp"


def Adapter(**kwargs):
    """Registry factory: the shared module serves both agents; the instance
    type is selected by HERMES_SYNC_AGENT (set before server import)."""
    if os.environ.get("HERMES_SYNC_AGENT") == "omp":
        return OmpAdapter(**kwargs)
    return PiAdapter(**kwargs)


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = Adapter


if __name__ == "__main__":
    import sys
    a = Adapter()
    print("agent:", a.agent_type, "discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions(limit=5)))
    sys.exit(0)
