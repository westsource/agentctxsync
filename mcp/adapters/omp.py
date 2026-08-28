"""
omp (oh-my-pi) adapter: one JSONL file per session under
``~/.omp/agent/sessions/<cwd-encoded>/<timestamp>_<uuid>.jsonl``.

Format mirrors what omp itself writes (pi-coding-agent session manager):

    line 1: fixed-width title slot (256 UTF-8 bytes incl. newline):
            {"type":"title","v":1,"title":...,"source":"auto","updatedAt":...,"pad":"..."}
    line 2: session header:
            {"type":"session","version":3,"id":"<uuid>","timestamp":"<iso>","cwd":"<abs>"}
    line 3: model_change root entry (optional but always written here)
    then:   one record per message:
            {"type":"message","id":"<8hex>","parentId":"<prev>","timestamp":"<iso>",
             "message":{"role":"user|assistant","content":[...],"timestamp":<ms>}}

Directory name: cwd is classified as home-relative / tmp-relative / absolute
exactly like omp's computeDefaultSessionDir (see session-paths.ts):

    home/tmp: "-<encoded-relative>" / "-tmp-<encoded-relative>"
    absolute: "--<abspath with separators and ':' replaced by '-'>--"

Sessions pushed from the shared pool get a fresh UUIDv7 local id (idmap
sidecar) so they never collide with omp's own sessions and never overwrite a
live session file. Upserts re-read the existing file (which omp may have
appended to) and merge by (role, timestamp-ms) before rewriting.
"""

import json
import os
import re
import secrets
import tempfile
import time
import uuid
from pathlib import Path

from .base import Adapter, validate_local_id

_IDMAP = ".omp-sync-idmap.json"          # canonical id -> local uuid
_VERSION = 3                             # CURRENT_SESSION_VERSION in omp
_MSG_ID_ALPHABET = "0123456789abcdef"

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _uuid7() -> str:
    """UUIDv7: 48-bit ms timestamp + version 7 + variant 10 + randomness."""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (ts << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    return str(uuid.UUID(int=value))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + \
        f".{int(time.time() * 1000) % 1000:03d}Z"


def _file_safe_ts(iso: str) -> str:
    return iso.replace(":", "-").replace(".", "-")


def _resolve(cwd: str) -> str:
    try:
        return os.path.realpath(os.path.abspath(cwd))
    except (OSError, ValueError):
        return os.path.abspath(cwd)


def _encode_dir_name(cwd: str) -> str:
    """Mirror omp computeDefaultSessionDir / encode*SessionDirName."""
    resolved = _resolve(cwd)
    home = _resolve(os.path.expanduser("~"))
    tmp = _resolve(tempfile.gettempdir())

    def _rel(base: str) -> str | None:
        try:
            rel = os.path.relpath(resolved, base)
        except ValueError:
            return None
        if rel == "" or (not rel.startswith("..") and not os.path.isabs(rel)):
            return rel
        return None

    home_rel = _rel(home)
    if home_rel is not None:
        return _encode_relative("-", home_rel)
    tmp_rel = _rel(tmp)
    if tmp_rel is not None:
        return _encode_relative("-tmp", tmp_rel)
    stripped = re.sub(r"^[/\\]", "", resolved)
    return f"--{re.sub(r'[/\\:]', '-', stripped)}--"


def _encode_relative(prefix: str, relative: str) -> str:
    encoded = re.sub(r"[/\\:]", "-", relative)
    if not encoded:
        return prefix
    return f"{prefix}{encoded}" if prefix.endswith("-") else f"{prefix}-{encoded}"


def _msg_id() -> str:
    return "".join(secrets.choice(_MSG_ID_ALPHABET) for _ in range(8))


def _model_str(value) -> str | None:
    """canonical model -> omp 'provider/id' string (unknown provider -> id)."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        mid = str(value.get("id") or "")
        pid = str(value.get("providerID") or "")
        if not mid:
            return None
        return f"{pid}/{mid}" if pid and pid != "unknown" else mid
    s = str(value)
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return _model_str(parsed)
    except (ValueError, TypeError):
        pass
    return s or None


class OmpAdapter(Adapter):
    """omp (oh-my-pi) file-based session store adapter."""

    agent_type = "omp"

    def __init__(self, sessions_root: Path | str | None = None):
        self.sessions_root = Path(sessions_root) if sessions_root else self.discover()

    # ------------------------------------------------------------------
    def discover(self) -> Path | None:
        candidates = []
        pi_cfg = os.environ.get("PI_CONFIG_DIR", "")
        if pi_cfg:
            candidates.append(Path(pi_cfg) / "agent")
        candidates.extend([
            Path.home() / ".omp" / "agent",
            Path(os.environ.get("LOCALAPPDATA", "")) / "omp" / "agent",
        ])
        for base in candidates:
            p = base / "sessions"
            if p.is_dir():
                return p
        return None

    def _watermark_file(self) -> Path | None:
        if self.sessions_root:
            return self.sessions_root / ".omp-sync-watermark"
        return None

    def _foreign_ids_file(self) -> Path | None:
        if self.sessions_root:
            return self.sessions_root / ".omp-sync-foreign.json"
        return None

    def _idmap_file(self) -> Path | None:
        if self.sessions_root:
            return self.sessions_root / _IDMAP
        return None

    def _idmap(self) -> dict[str, str]:
        f = self._idmap_file()
        if f is None or not f.exists():
            return {}
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_idmap(self, m: dict[str, str]):
        f = self._idmap_file()
        if f is not None:
            f.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    def _local_id_for(self, canonical: str) -> str:
        """Canonical id -> local uuid. Own (uuid-shaped) ids pass through;
        foreign ids map through the idmap with a fresh UUIDv7."""
        if _UUID_RE.match(str(canonical)) and validate_local_id(str(canonical)):
            return str(canonical)
        m = self._idmap()
        if canonical in m:
            return m[canonical]
        fresh = _uuid7()
        m[canonical] = fresh
        self._save_idmap(m)
        return fresh

    # ------------------------------------------------------------------
    # session dir / file discovery
    # ------------------------------------------------------------------
    def _session_dirs(self) -> list[Path]:
        if not self.sessions_root or not self.sessions_root.is_dir():
            return []
        return sorted(
            p for p in self.sessions_root.iterdir()
            if p.is_dir() and not p.name.startswith("."))

    def _session_paths(self) -> list[tuple[Path, str]]:
        """[(path, local_id)] for every *.jsonl session file, newest-first."""
        out = []
        for d in self._session_dirs():
            for f in d.iterdir():
                if f.is_file() and f.suffix == ".jsonl":
                    # filename: <fileSafeTs>_<id>.jsonl
                    stem = f.stem
                    i = stem.find("_")
                    if i > 0 and _UUID_RE.match(stem[i + 1:]):
                        out.append((f, stem[i + 1:]))
        out.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)
        return out

    # ------------------------------------------------------------------
    # reading: files -> canonical
    # ------------------------------------------------------------------
    def read_sessions(self, limit: int | None = None) -> list[dict]:
        paths = self._session_paths()
        if limit:
            paths = paths[:limit]
        idmap = self._idmap()
        own_to_canon = {v: k for k, v in idmap.items()}
        out = []
        for path, local_id in paths:
            s = self._read_session_file(path, local_id, own_to_canon)
            if s is not None:
                out.append(self.canonicalize(s))
        return out

    def _read_session_file(self, path: Path, local_id: str,
                           own_to_canon: dict) -> dict | None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        header = None
        title = None
        msgs = []
        started = None
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict):
                continue
            t = rec.get("type")
            if t == "title" and isinstance(rec.get("title"), str):
                title = rec["title"]
            elif t == "title_change" and isinstance(rec.get("title"), str) \
                    and rec["title"]:
                title = rec["title"]
            elif t == "session":
                header = rec
                if isinstance(rec.get("timestamp"), str):
                    started = _iso_to_epoch(rec["timestamp"])
            elif t == "message":
                m = rec.get("message")
                if isinstance(m, dict):
                    cmsg = self._message_canonical(m, str(rec.get("id", "")))
                    if cmsg is not None:
                        msgs.append(cmsg)
        if header is None:
            return None
        sid = str(header.get("id") or local_id)
        canonical = own_to_canon.get(sid, sid)
        s = {"id": canonical, "started_at": started or time.time(),
             "messages": msgs, "message_count": len(msgs)}
        if title:
            s["title"] = title
        cwd = header.get("cwd")
        if isinstance(cwd, str) and cwd:
            s["cwd"] = cwd
        return s

    def _message_canonical(self, m: dict, entry_id: str) -> dict | None:
        role = m.get("role")
        if role not in ("user", "assistant"):
            return None
        content = m.get("content")
        texts, thinkings = [], []
        if isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                bt = blk.get("type")
                if bt == "text" and blk.get("text"):
                    texts.append(str(blk["text"]))
                elif bt == "thinking" and blk.get("thinking"):
                    thinkings.append(str(blk["thinking"]))
        body = "\n".join(texts)
        if not body and not thinkings:
            return None
        ts = m.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 1e12:
            ts = ts / 1000.0
        elif not isinstance(ts, (int, float)):
            ts = time.time()
        out = {"session_id": str(m.get("session_id") or ""),
               "role": role, "content": body, "timestamp": float(ts)}
        if thinkings:
            out["reasoning"] = "\n".join(thinkings)
        return out

    # ------------------------------------------------------------------
    # writing: canonical -> files
    # ------------------------------------------------------------------
    def write_sessions(self, sessions: list[dict]) -> dict:
        if not sessions:
            return {"imported": 0, "updated": 0, "new_messages": 0,
                    "duplicates": 0}
        if not self.sessions_root:
            return {"error": "omp sessions dir not found"}
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        idmap = self._idmap()
        stats = {"imported": 0, "updated": 0, "new_messages": 0,
                 "duplicates": 0}
        for session in sessions:
            s = dict(session)
            msgs = s.pop("messages", [])
            canonical = str(s.get("id", ""))
            sid = self._local_id_for(canonical)
            cwd = s.get("cwd")
            resolved_cwd = _resolve(cwd) if cwd else None
            sdir = (self.sessions_root / _encode_dir_name(resolved_cwd)
                    ) if resolved_cwd else None
            if sdir is not None:
                sdir.mkdir(parents=True, exist_ok=True)
            path = self._find_file(sdir, sid) if sdir is not None else None
            if path is None:
                # Never invent a location: reuse the file this session
                # already has (wherever it lives) so a missing or
                # mismatched cwd cannot create a duplicate in the wrong
                # directory; without a cwd AND an existing file, skip.
                path = self._find_any(sid)
                if path is not None:
                    sdir = path.parent
                    hcwd = self._header_cwd(path)
                    resolved_cwd = hcwd if hcwd is not None else ""
                elif sdir is None:
                    continue
            existing_msgs = []
            if path is not None:
                existing_msgs = self._load_messages(path)
            _before_new = stats["new_messages"]
            merged = self._merge_messages(existing_msgs, msgs, stats)
            if not merged:
                continue
            if path is None:
                path = sdir / f"{_file_safe_ts(_now_iso())}_{sid}.jsonl"
                stats["imported"] += 1
            else:
                stats["updated"] += 1
                if stats["new_messages"] == _before_new and \
                        self._title_equals(path, s.get("title")):
                    # No new messages and the title is unchanged: leave the
                    # file untouched. A rewrite would bump mtime/content and
                    # can make the push fingerprint look changed (or start a
                    # format-migration full re-push); skipping keeps the
                    # pull a no-op for already-synced sessions.
                    continue
            try:
                self._write_file(path, sid, resolved_cwd, s, merged)
            except PermissionError:
                # The running harness holds this session's file open
                # (Windows: no FILE_SHARE_DELETE), so the atomic replace
                # cannot proceed. The local copy is already authoritative;
                # skip and reconcile on a later pull instead of aborting
                # the whole batch.
                try:
                    path.with_name(path.name + ".tmp").unlink()
                except OSError:
                    pass
                stats.setdefault("skipped", 0)
                stats["skipped"] += 1
                continue
            if canonical not in idmap and sid != canonical:
                idmap[canonical] = sid
        self._save_idmap(idmap)
        return stats

    def _find_file(self, sdir: Path, local_id: str) -> Path | None:
        try:
            for f in sdir.iterdir():
                if f.is_file() and f.suffix == ".jsonl" and f.stem.endswith(f"_{local_id}"):
                    return f
        except OSError:
            pass
        return None
    def _find_any(self, local_id: str) -> Path | None:
        """Locate a session file by id across every cwd dir."""
        for d in self._session_dirs():
            for f in d.iterdir():
                if f.is_file() and f.suffix == ".jsonl" \
                        and f.stem.endswith(f"_{local_id}"):
                    return f
        return None

    def _header_cwd(self, path: Path) -> str | None:
        """The cwd recorded in the session header of ``path``."""
        try:
            for raw in path.read_text(encoding="utf-8",
                                      errors="replace").splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(rec, dict) and rec.get("type") == "session" \
                        and isinstance(rec.get("cwd"), str):
                    return rec["cwd"]
        except OSError:
            return None
        return None
    def _title_equals(self, path: Path, title: str | None) -> bool:
        """True when the file's title slot already matches ``title``."""
        try:
            first = path.read_text(encoding="utf-8",
                                  errors="replace").splitlines()[0]
        except (OSError, IndexError):
            return False
        try:
            rec = json.loads(first)
        except (ValueError, TypeError):
            return False
        return isinstance(rec, dict) and rec.get("type") == "title" \
            and (rec.get("title") or "") == (title or "")

    def _load_messages(self, path: Path) -> list[dict]:
        """Existing message rows (role, ts ms, content, reasoning) from a
        file, in file order."""
        rows = []
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "message":
                    continue
                m = rec.get("message")
                if not isinstance(m, dict):
                    continue
                ts = m.get("timestamp")
                content = m.get("content")
                texts, thinkings = [], []
                if isinstance(content, list):
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        if blk.get("type") == "text" and blk.get("text"):
                            texts.append(str(blk["text"]))
                        elif blk.get("type") == "thinking" and blk.get("thinking"):
                            thinkings.append(str(blk["thinking"]))
                rows.append({
                    "role": m.get("role"),
                    "ts": int(ts) if isinstance(ts, (int, float)) else None,
                    "content": "\n".join(texts),
                    "reasoning": "\n".join(thinkings),
                })
        except OSError:
            pass
        return rows

    def _merge_messages(self, existing: list[dict], msgs: list[dict],
                        stats: dict) -> list[dict]:
        """existing (in file order) + new canonical messages, deduped by
        (role, timestamp-ms). Existing rows keep full content."""
        seen = {(k["role"], k["ts"]) for k in existing if k["ts"] is not None}
        merged = list(existing)
        for m in msgs:
            role = m.get("role")
            ts = m.get("timestamp")
            try:
                ms = int(float(ts) * 1000)
            except (TypeError, ValueError):
                ms = int(time.time() * 1000)
            if role in ("user", "assistant") and (role, ms) not in seen:
                seen.add((role, ms))
                merged.append({"role": role, "ts": ms,
                               "content": m.get("content") or "",
                               "reasoning": m.get("reasoning") or ""})
                stats["new_messages"] += 1
            elif role in ("user", "assistant"):
                stats["duplicates"] += 1
        return merged

    def _write_file(self, path: Path, sid: str, cwd: str,
                    session: dict, msgs: list[dict]):
        title = session.get("title") or ""
        updated_at = _now_iso()
        started = session.get("started_at")
        try:
            started_iso = _epoch_to_iso(float(started)) if started else updated_at
        except (TypeError, ValueError):
            started_iso = updated_at
        out = _title_slot(title, updated_at)   # fixed 256 bytes, incl. \n
        header = {"type": "session", "version": _VERSION, "id": sid,
                  "timestamp": started_iso, "cwd": cwd}
        out += json.dumps(header, ensure_ascii=False) + "\n"
        model = _model_str(session.get("model"))
        prev = None
        if model:
            mc = {"type": "model_change", "id": _msg_id(), "parentId": None,
                  "timestamp": started_iso, "model": model,
                  "resolvedModelIsFallback": False}
            out += json.dumps(mc, ensure_ascii=False) + "\n"
            prev = mc["id"]
        by_ts = {}
        for m in msgs:
            ts = m.get("ts")
            if ts is not None:
                by_ts.setdefault(ts, m)
        for ts in sorted(by_ts):
            entry = _message_entry(by_ts[ts], prev)
            out += json.dumps(entry, ensure_ascii=False) + "\n"
            prev = entry["id"]
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(out, encoding="utf-8")
        os.replace(tmp, path)

    def status(self) -> dict:
        if not self.sessions_root or not self.sessions_root.is_dir():
            return {"store": str(self.sessions_root), "sessions": 0,
                    "messages": 0}
        n = len(self._session_paths())
        m = 0
        for d in self._session_dirs():
            for f in d.iterdir():
                if f.is_file() and f.suffix == ".jsonl":
                    try:
                        m += sum(
                            1 for line in
                            f.read_text(encoding="utf-8").splitlines()
                            if '"type": "message"' in line
                            or '"type":"message"' in line)
                    except OSError:
                        pass
        return {"store": str(self.sessions_root), "sessions": n,
                "messages": m}


def _iso_to_epoch(iso: str) -> float:
    try:
        from datetime import datetime, timezone
        s = iso
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return time.time()


def _epoch_to_iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S") + f".{int(ts * 1000) % 1000:03d}Z"


def _title_slot(title: str, updated_at: str) -> str:
    """Fixed-width 256-byte (UTF-8, incl. newline) title slot, mirroring
    omp's serializeTitleSlot: JSON + pad spaces, then newline."""
    slot_bytes = 256
    line = _title_slot_line(title, updated_at, "")
    pad = slot_bytes - len(line.encode("utf-8")) - 1  # -1 for the newline
    if pad < 0:
        # truncate title (by code points) until it fits
        best = ""
        for cp in title:
            cand = best + cp
            if len(_title_slot_line(cand, updated_at, "").encode("utf-8")) + 1 <= slot_bytes:
                best = cand
            else:
                break
        line = _title_slot_line(best, updated_at, "")
        pad = slot_bytes - len(line.encode("utf-8")) - 1
    return line + " " * pad + "\n"


def _title_slot_line(title: str, updated_at: str, pad: str) -> str:
    slot = {"type": "title", "v": 1, "title": title, "source": "auto",
            "updatedAt": updated_at, "pad": pad}
    return json.dumps(slot, ensure_ascii=False)


def _message_entry(m: dict, parent_id: str | None) -> dict:
    role = m.get("role", "user")
    ts = m.get("ts") or int(time.time() * 1000)
    content = []
    if m.get("content"):
        content.append({"type": "text", "text": m["content"]})
    if m.get("reasoning"):
        content.append({"type": "thinking", "thinking": m["reasoning"]})
    if not content:
        content.append({"type": "text", "text": ""})
    return {
        "type": "message",
        "id": _msg_id(),
        "parentId": parent_id,
        "timestamp": _epoch_to_iso(ts / 1000.0),
        "message": {"role": role, "content": content, "timestamp": ts},
    }


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = OmpAdapter


if __name__ == "__main__":
    a = OmpAdapter()
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions()))
