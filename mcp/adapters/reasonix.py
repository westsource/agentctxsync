"""
Reasonix (DeepSeek-Reasonix) adapter.

Local store: <state root>/sessions/   (Windows default %APPDATA%\\reasonix)
  - one transcript per session:  <id>.jsonl  (id = file stem)
  - sidecars: <id>.events.jsonl (authoritative event log),
    <id>.jsonl.meta, <id>.goal-state.json, <id>.ckpt/,
    <id>.jsonl.lock / <id>.jsonl.lease.json (locks)
  - message lines: {"role", "content", "tool_calls", "tool_call_id", "name"}

Write constraints: transcripts are append-only and reasonix holds lock
files while running. This adapter skips sessions whose lock file is
present (reasonix is likely running) -- sync those when the agent is
closed. New sessions from other agents use the canonical local id as the
file stem (reasonix ids are free-form file names).
"""

import json
import os
import time
from pathlib import Path

from .base import JSONLAdapter, validate_file_id


class ReasonixAdapter(JSONLAdapter):
    """Reasonix jsonl adapter (canonical ids prefixed ``reasonix:``)."""

    agent_type = "reasonix"

    def __init__(self, sessions_dir: Path | str | None = None):
        self.sessions_dir = Path(sessions_dir) if sessions_dir else self.discover()

    def _foreign_ids_file(self) -> Path | None:
        if self.sessions_dir:
            return self.sessions_dir / ".hermes-sync-foreign-ids.json"
        return None

    def _watermark_file(self) -> Path | None:
        if self.sessions_dir:
            return self.sessions_dir / ".hermes-sync-watermark"
        return None

    def discover(self) -> Path | None:
        base = os.environ.get("REASONIX_HOME")
        if base:
            d = Path(base) / "sessions"
        elif os.environ.get("APPDATA"):
            # Windows: state root is %APPDATA%\reasonix, sessions under it
            d = Path(os.environ["APPDATA"]) / "reasonix" / "sessions"
        else:
            d = Path.home() / ".reasonix" / "sessions"
        return d if d.is_dir() else None

    def read_sessions(self, limit: int | None = None) -> list[dict]:
        """Push view: all local sessions, foreign ones included (they may
        have been continued locally; push_sessions tags each by its owner
        and the server dedupes, so only locally-added messages flow up).
        Foreign sessions never carry the local-id title fallback: they
        were pulled without title data, and sending title=<id> would
        overwrite the server's real title on push."""
        paths = self._session_paths()
        if limit:
            paths = paths[:limit]
        sessions = []
        for path, local_id in paths:
            s = self._read_session_file(path, local_id)
            if s is None:
                continue
            if self._is_foreign(local_id) and s.get("title") == local_id:
                s.pop("title", None)
            sessions.append(self.canonicalize(s))
        return sessions

    def _session_paths(self) -> list[tuple[Path, str]]:
        if not self.sessions_dir or not self.sessions_dir.is_dir():
            return []
        out = []
        for p in sorted(self.sessions_dir.iterdir(), reverse=True):
            if p.name.endswith(".jsonl") and not p.name.endswith(".events.jsonl"):
                out.append((p, p.stem))
        return out

    def _read_session_file(self, path: Path, local_id: str) -> dict | None:
        session: dict = {"id": local_id, "started_at": 0.0, "messages": []}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for i, line in enumerate(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            role = row.get("role")
            content = row.get("content")
            ts = row.get("timestamp") or row.get("ts")
            if not isinstance(ts, (int, float)):
                # transcripts have no guaranteed timestamp; use a stable
                # monotonic fallback so dedupe keys stay unique
                base = session["started_at"] or path.stat().st_mtime
                ts = base + (i / 10.0)
            msg = {"session_id": local_id,
                   "role": role if role else "assistant",
                   "content": content if content is not None else "",
                   "timestamp": ts}
            if row.get("tool_call_id"):
                msg["tool_call_id"] = row["tool_call_id"]
            if row.get("name"):
                msg["tool_name"] = row["name"]
            if row.get("tool_calls") is not None:
                msg["tool_calls"] = row["tool_calls"]
            if not session["started_at"]:
                session["started_at"] = ts
            session["messages"].append(msg)
        if not session["started_at"]:
            session["started_at"] = path.stat().st_mtime
        session["message_count"] = len(session["messages"])
        if not session.get("title") and local_id:
            session["title"] = local_id
        return session

    def write_sessions(self, sessions: list[dict]) -> dict:
        if not self.sessions_dir:
            return {"error": "reasonix sessions dir not found"}
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        imported = updated = new_messages = duplicates = 0
        for session in sessions:
            s = self.localize(session, strict=False)
            local_id = str(s["id"])
            if session.get("agent_type") != "reasonix":
                self._remember_foreign(local_id, session.get("agent_type"))
            if not validate_file_id(local_id):
                continue  # untrusted remote id: skip
            path = self.sessions_dir / f"{local_id}.jsonl"
            lock = self.sessions_dir / f"{local_id}.jsonl.lock"
            if lock.exists():
                continue  # reasonix is running with this session; skip
            msgs = s.pop("messages", [])
            if path.exists():
                existing = self._read_session_file(path, local_id) or {"messages": []}
                old_ts = {(m["role"], m["timestamp"]) for m in existing["messages"]}
                # The reasonix desktop normalizes transcripts it scans:
                # timestamps are stripped and a system prompt prepended, so
                # re-reads get mtime-derived fallback timestamps and the
                # (role, timestamp) triple no longer matches the server's
                # real timestamps. Without a content fallback every pull
                # would re-append the same messages and the file grows
                # forever. Identical (role, content) = the same message
                # re-delivered (the server is the dedupe authority);
                # empty content is exempt (consecutive empty tool results
                # are legitimately distinct).
                old_content = {(m["role"], m["content"]) for m in existing["messages"]
                               if m.get("content")}
                new_lines = [m for m in msgs
                             if (m.get("role"), m.get("timestamp")) not in old_ts
                             and (not m.get("content")
                                  or (m.get("role"), m.get("content")) not in old_content)]
                if new_lines:
                    with open(path, "a", encoding="utf-8") as f:
                        for m in new_lines:
                            f.write(self._to_line(m) + "\n")
                    new_messages += len(new_lines)
                else:
                    duplicates += len(msgs)
                updated += 1
            else:
                with open(path, "w", encoding="utf-8") as f:
                    for m in msgs:
                        f.write(self._to_line(m) + "\n")
                imported += 1
                new_messages += len(msgs)
        return {"imported": imported, "updated": updated,
                "new_messages": new_messages, "duplicates": duplicates}

    @staticmethod
    def _normalize_tool_calls(tc):
        """Reasonix expects ``tool_calls`` as a list of
        ``{"id", "name", "arguments"}``. The sync pipeline can deliver
        OpenAI-style objects (``{"type": "function", "function": {name,
        arguments}}``) or a JSON-encoded string of either shape (hermes
        stores tool_calls as text in state.db and the server keeps it as
        text). Normalize so the transcript stays parseable by the reasonix
        desktop app; unparseable input is dropped rather than corrupting
        the whole session file."""
        if tc is None:
            return None
        if isinstance(tc, str):
            try:
                tc = json.loads(tc)
            except (ValueError, TypeError):
                return None
        if not isinstance(tc, list):
            return None
        out = []
        for item in tc:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("function"), dict):
                # OpenAI style: {id, type: "function", function: {name,
                # arguments}} -> {id, name, arguments}
                fn = item["function"]
                item = {**fn, "id": item.get("id", fn.get("id", ""))}
            call = {"id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "")}
            if any(call.values()):
                out.append(call)
        return out or None

    @staticmethod
    def _to_line(m: dict) -> str:
        out: dict = {"role": m.get("role", "assistant"),
                     "content": m.get("content", "")}
        if m.get("tool_call_id"):
            out["tool_call_id"] = m["tool_call_id"]
        if m.get("tool_name"):
            out["name"] = m["tool_name"]
        if m.get("tool_calls") is not None:
            tc = ReasonixAdapter._normalize_tool_calls(m["tool_calls"])
            if tc is not None:
                out["tool_calls"] = tc
        if m.get("timestamp") is not None:
            out["timestamp"] = m["timestamp"]
        return json.dumps(out, ensure_ascii=False)

    def status(self) -> dict:
        paths = self._session_paths()
        total_msgs = 0
        for p, _ in paths:
            try:
                total_msgs += sum(1 for _ in p.open(encoding="utf-8",
                                                    errors="replace"))
            except OSError:
                pass
        return {"store": str(self.sessions_dir) if self.sessions_dir else None,
                "sessions": len(paths), "messages": total_msgs}


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = ReasonixAdapter


if __name__ == "__main__":
    a = ReasonixAdapter()
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions(limit=5)))
