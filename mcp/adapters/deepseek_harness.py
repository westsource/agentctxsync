"""
DeepSeek Harness adapter (codex rollout-format store).

Local store: <CODEX_HOME or ~/.codex>/sessions/  (the harness writes the
same rollout jsonl layout as the codex CLI it descends from)
  - one session per file:  rollout-<ts>-<uuid>.jsonl
      (ts format %Y-%m-%dT%H-%M-%S; archived copies may be .jsonl.zst)
  - session id = the UUID in the file name
  - recent versions partition files by year/month/day:
      sessions/2026/06/29/rollout-2026-06-29T22-05-24-<uuid>.jsonl
    older versions kept them flat under sessions/; both layouts are
    scanned recursively (new writes go into the year/month/day partition
    matching the file timestamp, mirroring what the harness itself does)
  - first line is a SessionMetaLine: legacy CLI writes {"meta": {...},
    "git": {}}; newer versions write {"type": "session_meta",
    "payload": {...}}
  - conversation lines are tagged RolloutItems, mostly
    {"type": "response_item", "payload": {...}}; non-conversation lines
    (event_msg / turn_context / compacted) are handled explicitly:
    lifecycle events are skipped, compaction summaries are kept as
    assistant messages
  - titles live in ~/.codex/session_index.jsonl (append-only,
    {"id": <thread_id>, "thread_name": ..., "updated_at": ...}); the
    harness backfills its SQLite index from the jsonl files, so new
    sessions become visible after it re-scans.

Write constraints: files are append-only; never rewrite existing lines.
Compressed .zst files are skipped on read (decompression would need the
zstandard package) and never written.
"""

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .base import JSONLAdapter, validate_file_id

# ts matches codex's fixed %Y-%m-%dT%H-%M-%S (19 chars); the id after the
# last "-" is free-form so foreign ids (hermes bare ids) round-trip.
ROLLOUT_RE = re.compile(
    r"^rollout-(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(?P<id>.+)\.jsonl(?:\.zst)?$")

# UUID-shaped session ids (the harness's own format AND workbuddy's) pass through
# the harness desktop backfill; timestamp-style ids (hermes) do not. Foreign
# non-UUID ids get a mapped UUID local id (see _local_id_for).
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: canonical id -> local UUID, persisted so re-pulls reuse the same local id
_IDMAP = ".hermes-sync-idmap.json"


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


class DeepseekHarnessAdapter(JSONLAdapter):
    """DeepSeek Harness rollout jsonl adapter (canonical ids bare)."""

    agent_type = "deepseek-harness"

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
    # idmap (canonical id -> local UUID) for foreign sessions
    # ------------------------------------------------------------------
    def _idmap(self) -> dict[str, str]:
        if not self.codex_home:
            return {}
        p = self.codex_home / _IDMAP
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _save_idmap(self, m: dict):
        p = self.codex_home / _IDMAP
        p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")

    def _local_id_for(self, canonical_id: str) -> str:
        """Map a canonical session id to a local codex rollout id.

        The harness desktop's session backfill only indexes rollouts whose id is
        a UUID -- timestamp-style ids (hermes ``20260608_103351_a671c4``)
        are silently skipped and never appear in the UI. Foreign ids that
        are not UUID-shaped therefore get a fresh UUID local id, persisted
        in the idmap so later pulls reuse it (dedupe stays stable). UUID-
        shaped foreign ids (workbuddy) pass through unchanged, exactly like
        the harness's own ids.
        """
        if _UUID_RE.match(canonical_id):
            return canonical_id
        # Filename-unsafe ids (':' on Windows -> NTFS ADS, etc.) are left
        # for the write path's validate_file_id skip -- mapping them would
        # silently import a legacy/untrusted id that must not land on disk.
        if not validate_file_id(canonical_id):
            return canonical_id
        m = self._idmap()
        if canonical_id in m:
            return m[canonical_id]
        fresh = str(uuid.uuid4())
        m[canonical_id] = fresh
        self._save_idmap(m)
        return fresh

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

    @staticmethod
    def _unique_ts(used: set, role: str, ts: float) -> float:
        """Make the (role, timestamp) dedup triple unique per session.

        The harness stamps bursts of items with the same millisecond timestamp
        (observed: hundreds of distinct tool items sharing one ms), and the
        pool dedups messages by (session_id, role, timestamp) — two distinct
        messages with the same triple would silently collapse on pull/push
        round-trips. Nudge colliding timestamps upward by 1ms until free.
        Deterministic: the same file always maps to the same timestamps.
        """
        while (role, ts) in used:
            ts += 0.001
        used.add((role, ts))
        return ts

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
        # 0.142+ partitions by year/month/day (sessions/2026/06/29/...);
        # older CLI versions kept files flat under sessions/. Recursive
        # scan covers both. Sorting the full path reverse orders by the
        # fixed-width ts in the file name, i.e. newest-first.
        for p in sorted(sess_dir.rglob("rollout-*.jsonl*"), reverse=True):
            m = ROLLOUT_RE.match(p.name)
            if m:
                out.append((p, m.group("id")))
        return out

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def read_sessions(self, limit: int | None = None) -> list[dict]:
        """Push view: local sessions with CANONICAL ids.

        Foreign sessions were written under a mapped UUID local id (see
        _local_id_for); map them back so the pool sees the canonical id and
        round-trips dedupe on the server.
        """
        rev = {v: k for k, v in self._idmap().items()}
        sessions = super().read_sessions(limit=limit)
        for s in sessions:
            cid = rev.get(str(s["id"]), str(s["id"]))
            s["id"] = cid
            for m in s.get("messages", []):
                m["session_id"] = cid
        return sessions

    def _read_session_file(self, path: Path, local_id: str) -> dict | None:
        if path.name.endswith(".zst"):
            return None  # would need the zstandard package to decompress
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        session: dict = {"id": local_id, "started_at": 0.0, "messages": []}
        used_ts: set = set()  # (role, timestamp) triples already emitted
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
            ltype = row.get("type")
            # SessionMetaLine, legacy CLI format: {"meta": {...}, "git": {}}
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
            # session_meta (0.142+): metadata only, never a message
            if ltype == "session_meta":
                p = row.get("payload")
                if isinstance(p, dict):
                    mid = p.get("id") or p.get("session_id")
                    ts = _parse_ts(p.get("timestamp") or row.get("timestamp"))
                    if mid and not session["started_at"]:
                        session["id"] = str(mid)
                    if ts and not session["started_at"]:
                        session["started_at"] = ts
                    if p.get("cwd") and not session.get("cwd"):
                        session["cwd"] = p["cwd"]
                    if not meta_model:
                        mp = p.get("model_provider")
                        if mp and mp not in ("custom", "unknown"):
                            meta_model = mp
                continue
            # event_msg / turn_context: internal lifecycle events, not
            # conversation; turn_context carries the per-turn model/cwd
            if ltype in ("event_msg", "turn_context"):
                if ltype == "turn_context":
                    p = row.get("payload")
                    if isinstance(p, dict):
                        if not meta_model and p.get("model"):
                            meta_model = p["model"]
                        if p.get("cwd") and not session.get("cwd"):
                            session["cwd"] = p["cwd"]
                continue
            # compacted: handoff summary another model produced, injected
            # as assistant context after compaction — keep it as a message
            if ltype == "compacted":
                p = row.get("payload")
                text = p.get("message") if isinstance(p, dict) else None
                ts = _parse_ts(row.get("timestamp")) if row.get("timestamp") else None
                if text and ts is not None:
                    session["messages"].append({
                        "session_id": local_id, "role": "assistant",
                        "content": text,
                        "timestamp": self._unique_ts(used_ts, "assistant", ts)})
                continue
            # conversation items: response_item, or bare payload dicts in
            # the legacy line format; anything else is not conversation
            if ltype == "response_item":
                payload = row.get("payload")
            elif ltype is None:
                payload = row
            else:
                continue
            if not isinstance(payload, dict):
                continue
            ts = _parse_ts(payload.get("timestamp") or row.get("ts")
                           or row.get("timestamp"))
            if ts is None and isinstance(row.get("ts_nanos"), (int, float)):
                ts = row["ts_nanos"] / 1e9
            ptype = payload.get("type")
            # tool round-trips map to the shared pool's "tool" role (the
            # Web viewer renders them as collapsible cards)
            if ptype in ("function_call", "custom_tool_call"):
                arg = payload.get("arguments") if ptype == "function_call" \
                    else payload.get("input")
                if isinstance(arg, (dict, list)):
                    arg = json.dumps(arg, ensure_ascii=False)
                session["messages"].append({
                    "session_id": local_id, "role": "tool",
                    "content": str(arg or ""),
                    "timestamp": self._unique_ts(used_ts, "tool", ts or 0.0),
                    "tool_name": payload.get("name"),
                    "tool_call_id": payload.get("call_id"),
                })
                continue
            if ptype in ("function_call_output", "custom_tool_call_output"):
                session["messages"].append({
                    "session_id": local_id, "role": "tool",
                    "content": str(payload.get("output") or ""),
                    "timestamp": self._unique_ts(used_ts, "tool", ts or 0.0),
                    "tool_call_id": payload.get("call_id"),
                })
                continue
            # reasoning: internal thinking, not part of the conversation
            if ptype == "reasoning":
                continue
            role, content = _item_role_content(payload)
            if role == "developer":
                role = "system"  # system prompt; shared pool has no developer
            if ts is None:
                # stable monotonic fallback (unique per line)
                base = session["started_at"] or meta_ts or (time.time() - len(lines))
                ts = base + (i / 10.0)
            if not content and not payload.get("name") and not payload.get("call_id"):
                continue  # empty item: nothing to sync
            msg: dict = {"session_id": local_id, "role": role or "assistant",
                         "content": content or "",
                         "timestamp": self._unique_ts(used_ts, role or "assistant", ts)}
            if payload.get("name"):
                msg["tool_name"] = payload["name"]
            if payload.get("call_id"):
                msg["tool_call_id"] = payload["call_id"]
            if payload.get("id"):
                msg["meta"] = {"deepseek-harness:item_id": payload["id"]}
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

        The harness names files ``rollout-<ts>-<uuid>.jsonl``, so an existing
        session must be found by its id, not by a freshly-generated ts.
        """
        if not self.codex_home:
            return None
        sess = self.codex_home / "sessions"
        if not sess.is_dir():
            return None
        for p in sorted(sess.rglob("rollout-*.jsonl*")):
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
            # Foreign ids that aren't UUID-shaped get a mapped UUID local id
            # so the harness desktop backfill indexes them (see _local_id_for).
            canonical_id = str(session.get("id") or s["id"])
            local_id = self._local_id_for(canonical_id)
            if session.get("agent_type") != "deepseek-harness":
                # registry is keyed by CANONICAL id: read_sessions() maps
                # the local UUID back, so push tags the owner correctly.
                self._remember_foreign(canonical_id, session.get("agent_type"))
            if not validate_file_id(local_id):
                continue  # untrusted remote id: skip
            msgs = s.pop("messages", [])
            # New files go into the year/month/day partition matching the
            # timestamp (0.142+ layout); updates append to the existing file
            # wherever it lives (flat or partitioned).
            now = datetime.now()
            new_path = (sess_dir / now.strftime("%Y/%m/%d")
                        / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{local_id}.jsonl")
            path = self._existing_path(local_id) or new_path
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
                path.parent.mkdir(parents=True, exist_ok=True)
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
        ts = datetime.now(timezone.utc).isoformat()
        payload = {"id": local_id, "session_id": local_id, "timestamp": ts,
                   "model_provider": s.get("model") or "unknown"}
        if s.get("cwd"):
            payload["cwd"] = s["cwd"]
        return json.dumps({"timestamp": ts, "type": "session_meta",
                           "payload": payload}, ensure_ascii=False)

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
            {"type": "response_item", "ts": ts_iso, "timestamp": ts_iso,
             "payload": payload}, ensure_ascii=False)

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
Adapter = DeepseekHarnessAdapter


if __name__ == "__main__":
    a = DeepseekHarnessAdapter()
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions(limit=5)))
