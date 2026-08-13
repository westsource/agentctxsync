"""
OpenAI Codex CLI adapter.

Local store: <CODEX_HOME or ~/.codex>/sessions/
  - one session per file:  rollout-<ts>-<uuid>.jsonl
      (ts format %Y-%m-%dT%H-%M-%S; archived copies may be .jsonl.zst)
  - session id = the UUID in the file name
  - first line is a SessionMetaLine: {"meta": {...}, "git": {...}}
  - conversation lines are tagged RolloutItems, mostly
    {"type": "response_item", "payload": {...}} (OpenAI Responses API items)
  - titles live in ~/.codex/session_index.jsonl (append-only,
    {"id": <thread_id>, "thread_name": ..., "updated_at": ...}); codex
    backfills its SQLite index from the jsonl files, so new sessions become
    visible after codex re-scans.

Write constraints: files are append-only; never rewrite existing lines.
Compressed .zst files are skipped on read (decompression would need the
zstandard package) and never written.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import JSONLAdapter, validate_local_id

# ts matches codex's fixed %Y-%m-%dT%H-%M-%S (19 chars); the id after the
# last "-" is free-form so foreign ids (hermes bare ids) round-trip.
ROLLOUT_RE = re.compile(
    r"^rollout-(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(?P<id>.+)\.jsonl(?:\.zst)?$")


def _parse_ts(text: str) -> float | None:
    """Parse a codex file-name or RFC3339 timestamp into epoch seconds."""
    if not text:
        return None
    text = text.strip()
    for fmt in ("%Y-%m-%dT%H-%M-%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _item_role_content(payload: dict) -> tuple[str | None, str | None]:
    """Map an OpenAI Responses API item to (role, content). Defensive."""
    role = payload.get("role")
    content = None
    if isinstance(payload.get("content"), list):
        texts = []
        for part in payload["content"]:
            if isinstance(part, dict):
                if part.get("type") in ("output_text", "input_text", "text"):
                    texts.append(str(part.get("text", "")))
                elif part.get("text") is not None:
                    texts.append(str(part["text"]))
        content = "\n".join(t for t in texts if t) or None
    if role and content is None and payload.get("text") is not None:
        content = str(payload["text"])
    return role, content


class CodexAdapter(JSONLAdapter):
    """Codex rollout jsonl adapter (canonical ids prefixed ``codex:``)."""

    agent_type = "codex"

    def __init__(self, codex_home: Path | str | None = None):
        self.codex_home = Path(codex_home) if codex_home else self.discover()

    def _foreign_ids_file(self) -> Path | None:
        if self.codex_home:
            return self.codex_home / ".hermes-sync-foreign-ids.json"
        return None

    def _watermark_file(self) -> Path | None:
        if self.codex_home:
            return self.codex_home / ".hermes-sync-watermark"
        return None

    def discover(self) -> Path | None:
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        return home if home.is_dir() else None

    # ------------------------------------------------------------------
    # session index (titles)
    # ------------------------------------------------------------------
    def _titles(self) -> dict[str, str]:
        """id -> thread_name from session_index.jsonl (last wins)."""
        titles: dict[str, str] = {}
        if not self.codex_home:
            return titles
        idx = self.codex_home / "session_index.jsonl"
        if not idx.exists():
            return titles
        for line in idx.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("id") and row.get("thread_name"):
                titles[str(row["id"])] = str(row["thread_name"])
        return titles

    def _append_title(self, local_id: str, title: str | None):
        if not title or not self.codex_home:
            return
        idx = self.codex_home / "session_index.jsonl"
        line = json.dumps({
            "id": local_id,
            "thread_name": title,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False)
        with open(idx, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ------------------------------------------------------------------
    # session file listing
    # ------------------------------------------------------------------
    def _session_paths(self) -> list[tuple[Path, str]]:
        if not self.codex_home:
            return []
        sess_dir = self.codex_home / "sessions"
        if not sess_dir.is_dir():
            return []
        out = []
        for p in sorted(sess_dir.iterdir(), reverse=True):
            m = ROLLOUT_RE.match(p.name)
            if m:
                out.append((p, m.group("id")))
        return out

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def _read_session_file(self, path: Path, local_id: str) -> dict | None:
        if path.name.endswith(".zst"):
            return None  # would need the zstandard package to decompress
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        session: dict = {"id": local_id, "started_at": 0.0, "messages": []}
        meta_ts = None
        meta_model = None
        fallback_ts = None
        for i, line in enumerate(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            # SessionMetaLine
            if "meta" in row and isinstance(row["meta"], dict):
                m = row["meta"]
                meta_ts = _parse_ts(m.get("timestamp") or m.get("ts"))
                meta_model = m.get("model_provider") or m.get("model")
                if m.get("id") and not session["started_at"]:
                    session["id"] = str(m["id"])
                if meta_ts and not session["started_at"]:
                    session["started_at"] = meta_ts
                if m.get("cwd"):
                    session["cwd"] = m["cwd"]
                if m.get("thread_name") and not session.get("title"):
                    session["title"] = m["thread_name"]
                continue
            # conversation items
            payload = row.get("payload") if row.get("type") == "response_item" else row
            if not isinstance(payload, dict):
                continue
            role, content = _item_role_content(payload)
            ts = _parse_ts(payload.get("timestamp") or row.get("ts"))
            if ts is None and isinstance(row.get("ts_nanos"), (int, float)):
                ts = row["ts_nanos"] / 1e9
            if ts is None:
                # stable monotonic fallback (unique per line)
                base = session["started_at"] or meta_ts or (time.time() - len(lines))
                ts = base + (i / 10.0)
            msg: dict = {"session_id": local_id, "role": role or "assistant",
                         "content": content or "", "timestamp": ts}
            if payload.get("name"):
                msg["tool_name"] = payload["name"]
            if payload.get("call_id"):
                msg["tool_call_id"] = payload["call_id"]
            if payload.get("id"):
                msg["meta"] = {"codex:item_id": payload["id"]}
            session["messages"].append(msg)
        if not session["started_at"]:
            m = ROLLOUT_RE.match(path.name)
            session["started_at"] = _parse_ts(m.group("ts")) if m else 0.0
        if session["started_at"] == 0.0:
            session["started_at"] = path.stat().st_mtime
        if meta_model and not session.get("model"):
            session["model"] = meta_model
        if not session.get("title"):
            titles = self._titles()
            if local_id in titles:
                session["title"] = titles[local_id]
        session["message_count"] = len(session["messages"])
        return session

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    def _existing_path(self, local_id: str) -> Path | None:
        """Locate the session file for ``local_id``.

        Codex names files ``rollout-<ts>-<uuid>.jsonl``, so an existing
        session must be found by its id, not by a freshly-generated ts.
        """
        if not self.codex_home:
            return None
        sess = self.codex_home / "sessions"
        if not sess.is_dir():
            return None
        for p in sess.iterdir():
            m = ROLLOUT_RE.match(p.name)
            if m and m.group("id") == local_id and not p.name.endswith(".zst"):
                return p
        return None

    def write_sessions(self, sessions: list[dict]) -> dict:
        if not self.codex_home:
            return {"error": "codex home not found"}
        sess_dir = self.codex_home / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        titles = self._titles()
        imported = updated = new_messages = duplicates = 0
        for session in sessions:
            s = self.localize(session, strict=False)
            local_id = str(s["id"])
            if not session["id"].startswith("codex:"):
                self._remember_foreign(local_id)
            if not validate_local_id(local_id):
                continue  # untrusted remote id: skip
            msgs = s.pop("messages", [])
            path = self._existing_path(local_id) or (
                sess_dir / f"rollout-{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}-{local_id}.jsonl")
            if path.exists():
                existing = self._read_session_file(path, local_id) or {"messages": []}
                old_ts = {(m["role"], m["timestamp"]) for m in existing["messages"]}
                new_lines = [m for m in msgs
                             if (m.get("role"), m.get("timestamp")) not in old_ts]
                if new_lines:
                    with open(path, "a", encoding="utf-8") as f:
                        for m in new_lines:
                            f.write(self._to_rollout_line(m) + "\n")
                    new_messages += len(new_lines)
                else:
                    duplicates += len(msgs)
                updated += 1
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self._meta_line(local_id, s) + "\n")
                    for m in msgs:
                        f.write(self._to_rollout_line(m) + "\n")
                imported += 1
                new_messages += len(msgs)
            if s.get("title") and titles.get(local_id) != s["title"]:
                self._append_title(local_id, s["title"])
        return {"imported": imported, "updated": updated,
                "new_messages": new_messages, "duplicates": duplicates}

    @staticmethod
    def _meta_line(local_id: str, s: dict) -> str:
        meta = {
            "id": local_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model_provider": s.get("model") or "unknown",
        }
        if s.get("cwd"):
            meta["cwd"] = s["cwd"]
        return json.dumps({"meta": meta, "git": {}}, ensure_ascii=False)

    @staticmethod
    def _to_rollout_line(m: dict) -> str:
        ts = m.get("timestamp")
        if isinstance(ts, (int, float)):
            ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        else:
            ts_iso = ts or datetime.now(timezone.utc).isoformat()
        content = [{"type": "output_text", "text": m.get("content", "")}]
        payload = {"type": "message", "role": m.get("role", "assistant"),
                   "content": content}
        if m.get("tool_call_id"):
            payload["call_id"] = m["tool_call_id"]
        return json.dumps(
            {"type": "response_item", "ts": ts_iso, "payload": payload},
            ensure_ascii=False)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        paths = self._session_paths()
        total_msgs = 0
        zst = 0
        for p, _ in paths:
            if p.name.endswith(".zst"):
                zst += 1
                continue
            try:
                total_msgs += sum(1 for _ in p.open(encoding="utf-8",
                                                     errors="replace"))
            except OSError:
                pass
        return {"store": str(self.codex_home / "sessions") if self.codex_home else None,
                "sessions": len(paths), "messages": total_msgs,
                "compressed_skipped": zst}


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = CodexAdapter


if __name__ == "__main__":
    a = CodexAdapter()
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions(limit=5)))
