"""
dsh (official DeepSeek Harness, deepseek-ai/deepseek-harness) adapter.

Local store (0.1.2-rc.1 layout, as written by DSH Desktop / dsh):

    <dsh home>/sessions/--<cwd-slug>--/<encoded-session-id>/session.jsonl.zstd

    - one session per DIRECTORY named <encoded-session-id> (ids look like
      ``session-<uuid>`` and encode unchanged);
    - one generation file inside: ``session.jsonl`` (v0 name) or
      ``session.vN.jsonl``; compressed with zstd when the store is
      configured for it (the desktop default), suffix ``.zstd``;
    - file = header line ``{"type":"session","version":0,"id":...,"createdAt":ms,
      "cwd":...}`` followed by typed event envelopes
      ``{"type":T,"seq":N,"time":ms,"data":{...}}`` with seq contiguous from 0;
    - conversation text lives in ``user/message`` (data.role='user') and
      ``assistant/message`` (data.message.role='assistant', content = typed
      blocks incl. ``{type:"text",text}``); reasoning/tool/compaction events
      are not conversation text and are skipped on read;
    - titles arrive as log events ``session/title`` (data.title);
    - the DSH Desktop session list additionally reads
      ``<home>/storages/workspace.json`` (+ a projection cache); external
      writers update workspace.json best-effort, the cache is left to the
      harness, so newly written sessions may need a desktop restart to show.

Write constraints: we author whole new session logs (header + events, seq
contiguous) and rewrite existing ones atomically (temp + rename), mirroring
the append-only log semantics; never write into a session while the harness
has it open (Windows file locks surface as PermissionError and are skipped
for the batch like omp).

dsh home discovery: ``DSH_HOME`` env (as resolved by upstream home-paths),
else ``~/.dsh``. ``HERMES_SYNC_AGENT=dsh`` selects this adapter.
"""

import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path

from .base import Adapter, validate_local_id

_IDMAP = ".dsh-sync-idmap.json"
_WATERMARK = ".dsh-sync-watermark"
_FOREIGN = ".dsh-sync-foreign.json"
_NO_CWD = "_no-cwd"

try:
    import zstandard as _zstd  # type: ignore
    HAVE_ZSTD = True
except ImportError:  # pragma: no cover - environment dependent
    _zstd = None
    HAVE_ZSTD = False

_MSG_ID_ALPHABET = "0123456789abcdef"
_SESSION_ID_RE = re.compile(r"^session-[0-9a-fA-F-]{10,}$")
_HEX_ESCAPE_RE = re.compile(r"[^A-Za-z0-9._-]")
_HEX_SEP_RE = re.compile(r"[/\\:]+")

# Windows MAX_PATH (260) applies to the whole file path. The cwd slug escapes
# every non-ASCII byte as ``~XXXX``, so a CJK cwd easily pushes
# ``<home>/sessions/--<slug>--/<id>/session.jsonl.zstd`` past the limit: the
# plain API then fails with FileNotFoundError even though the parent exists,
# and one such session aborted the entire pull. ``\\?\`` lifts the limit
# without the machine-wide LongPathsEnabled policy (UNC roots are left alone —
# they would need the ``\\?\UNC\`` form, and ``DSH_HOME`` is local here).
_LONG_PATH_MIN = 240


def _extended(abspath: str) -> str:
    """Extended-length form of an absolute path (pure, platform-free)."""
    if len(abspath) < _LONG_PATH_MIN or abspath.startswith("\\\\"):
        return abspath
    s = abspath.replace("/", "\\")
    return s if s.startswith("\\\\?\\") else "\\\\?\\" + s


def _lp(path) -> str:
    """Long-path-safe string for open()/os.* on Windows, plain text elsewhere."""
    s = os.path.abspath(str(path))
    return _extended(s) if os.name == "nt" else s


def _msg_id() -> str:
    return "".join(secrets.choice(_MSG_ID_ALPHABET) for _ in range(8))


def _session_uuid() -> str:
    """A fresh dsh-style session id: ``session-<uuid4>``."""
    return f"session-{uuid.uuid4()}"



def _model_parts(model) -> tuple[str, str]:
    """canonical model -> (provider, model) for dsh source stamps."""
    if isinstance(model, dict):
        mid = str(model.get("id") or "")
        pid = str(model.get("providerID") or "")
        if mid:
            return (pid or "unknown", mid)
    s = str(model or "")
    if "/" in s and not s.startswith("http"):
        p, _, m = s.partition("/")
        return (p or "unknown", m or "unknown")
    return ("unknown", s or "unknown")


def _slug(cwd: str) -> str:
    """Mirror dsh projectKey: '--' + slug + '--'.

    '/', '\\', ':' (and runs) become '-'; keep [A-Za-z0-9._-]; everything
    else escapes as '~XXXX' (uppercase hex); leading '-' trimmed; capped.
    cwd-less sessions live under ``_no-cwd`` (projectDir).
    """
    s = _HEX_SEP_RE.sub("-", cwd)
    s = _HEX_ESCAPE_RE.sub(lambda m: "~%04X" % ord(m.group(0)), s)
    s = s.lstrip("-")  # upstream trims LEADING '-' only; trailing '-' is kept
    if len(s) > 251:
        s = s[:251]
    s = s or "root"
    return f"--{s}--"


def _encode_id(session_id: str) -> str:
    """Mirror dsh encodeSegment: ids we accept (session-<uuid>) are already
    single safe path segments and pass through unchanged."""
    if not session_id:
        return session_id
    if re.fullmatch(r"[A-Za-z0-9._-]+", session_id):
        return session_id
    return _HEX_ESCAPE_RE.sub(lambda m: "~%04X" % ord(m.group(0)), session_id)


def _decompress_zstd(data: bytes) -> bytes:
    if not HAVE_ZSTD:
        raise ImportError("zstandard package required to read .zstd logs")
    with _zstd.ZstdDecompressor().stream_reader(data) as r:
        return r.read()


def _compress_zstd(data: bytes) -> bytes:
    if not HAVE_ZSTD:
        raise ImportError("zstandard package required to write .zstd logs")
    return _zstd.ZstdCompressor().compress(data)


class DshAdapter(Adapter):
    """DeepSeek Harness (official dsh) JSONL+zstd session store adapter."""

    agent_type = "dsh"

    def __init__(self, sessions_root: Path | str | None = None,
                 storages_root: Path | str | None = None):
        self.sessions_root = Path(sessions_root) if sessions_root else \
            self.discover()
        if storages_root is None and self.sessions_root is not None:
            self.storages_root = self.sessions_root.parent / "storages"
        else:
            self.storages_root = Path(storages_root) if storages_root else None

    # ------------------------------------------------------------------
    def discover(self) -> Path | None:
        env_home = os.environ.get("DSH_HOME", "").strip()
        candidates = []
        if env_home:
            candidates.append(Path(env_home) / "sessions")
        candidates.extend([
            Path.home() / ".dsh" / "sessions",
            Path(os.environ.get("USERPROFILE", "")) / ".dsh" / "sessions",
        ])
        for p in candidates:
            if p.is_dir():
                return p
        return None

    def _watermark_file(self) -> Path | None:
        if self.sessions_root:
            return self.sessions_root / _WATERMARK
        return None

    def _foreign_ids_file(self) -> Path | None:
        if self.sessions_root:
            return self.sessions_root / _FOREIGN
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
        """Canonical id -> local dsh id. Own (session-<uuid>) ids pass
        through; foreign ids map through the idmap to a fresh session id."""
        cid = str(canonical)
        if _SESSION_ID_RE.match(cid) and validate_local_id(cid):
            return cid
        m = self._idmap()
        if cid in m:
            return m[cid]
        fresh = _session_uuid()
        m[cid] = fresh
        self._save_idmap(m)
        return fresh

    # ------------------------------------------------------------------
    # discovery of session files
    # ------------------------------------------------------------------
    def _session_files(self) -> list[tuple[Path, str]]:
        """[(session file path, local session id)] for every session log,
        newest-first. Walk <root>/<project-slug>/<encoded-id>/session*.jsonl*.
        """
        out: list[tuple[Path, str]] = []
        if not self.sessions_root or not self.sessions_root.is_dir():
            return out
        for proj in self.sessions_root.iterdir():
            if not proj.is_dir() or proj.name.startswith("."):
                continue
            for sdir in proj.iterdir():
                if not sdir.is_dir():
                    continue
                fid = self._log_file(sdir)
                if fid is None:
                    continue
                # local id = the session dir name, decoded if it was escaped
                out.append((fid, _decode_id(sdir.name)))
        out.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)
        return out

    @staticmethod
    def _log_file(sdir: Path) -> Path | None:
        if not sdir.is_dir():
            return None
        for name in ("session.jsonl", "session.jsonl.zstd"):
            p = sdir / name
            if p.is_file():
                return p
        try:
            entries = sorted(sdir.iterdir())
        except OSError:
            return None
        for p in entries:
            if p.is_file() and re.fullmatch(
                    r"session\.v\d+\.jsonl(?:\.zstd)?", p.name):
                return p
        return None

    # ------------------------------------------------------------------
    # reading: files -> canonical
    # ------------------------------------------------------------------
    def read_sessions(self, limit: int | None = None) -> list[dict]:
        paths = self._session_files()
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
            raw = Path(_lp(path)).read_bytes()
        except OSError:
            return None
        if path.name.endswith(".zstd"):
            try:
                raw = _decompress_zstd(raw)
            except ImportError:
                return None
        text = raw.decode("utf-8", "replace")
        header = None
        title = None
        started = None
        msgs = []
        last = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict):
                continue
            t = rec.get("type")
            if t == "session":
                header = rec
                if isinstance(rec.get("createdAt"), (int, float)):
                    started = rec["createdAt"] / 1000.0
            elif t == "session/title" and isinstance(rec.get("data"), dict):
                d = rec["data"]
                if isinstance(d.get("title"), str) and d["title"]:
                    title = d["title"]
            elif t in ("user/message", "assistant/message"):
                m = self._event_message(rec)
                if m is not None:
                    msgs.append(m)
            # tool/call, tool/result, assistant/chunk, compaction/*,
            # permission/*, sandbox/*, request/* are not conversation text.
            last = rec
        if header is None:
            return None
        sid = str(header.get("id") or last.get("id") or local_id)
        canonical = own_to_canon.get(sid, sid)
        s = {"id": canonical, "started_at": started or time.time(),
             "messages": msgs, "message_count": len(msgs)}
        if title:
            s["title"] = title
        cwd = header.get("cwd")
        if isinstance(cwd, str) and cwd:
            s["cwd"] = cwd
        return s

    @staticmethod
    def _event_message(rec: dict) -> dict | None:
        """user/message | assistant/message event -> canonical message."""
        data = rec.get("data")
        if not isinstance(data, dict):
            return None
        role = data.get("role")
        msg = data.get("message")
        if isinstance(msg, dict):
            role = msg.get("role") or role
            content = msg.get("content")
        else:
            content = data.get("content")
        if role not in ("user", "assistant"):
            return None
        texts = []
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text" \
                        and blk.get("text"):
                    texts.append(str(blk["text"]))
        body = "\n".join(texts).strip()
        if not body:
            return None
        ts = rec.get("time")
        if isinstance(ts, (int, float)) and ts > 0:
            ts = float(ts) / 1000.0
        else:
            ts = time.time()
        return {"session_id": "", "role": role, "content": body,
                "timestamp": ts}

    # ------------------------------------------------------------------
    # writing: canonical -> files
    # ------------------------------------------------------------------
    def write_sessions(self, sessions: list[dict]) -> dict:
        if not sessions:
            return {"imported": 0, "updated": 0, "new_messages": 0,
                    "duplicates": 0}
        if not self.sessions_root:
            return {"error": "dsh sessions dir not found"}
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        idmap = self._idmap()
        stats = {"imported": 0, "updated": 0, "new_messages": 0,
                 "duplicates": 0}
        any_changed = False
        for session in sessions:
            s = dict(session)
            msgs = s.pop("messages", [])
            canonical = str(s.get("id", ""))
            if not canonical:
                continue
            sid = self._local_id_for(canonical)
            cwd = s.get("cwd")
            slug = _slug(str(cwd)) if isinstance(cwd, str) and cwd \
                else _NO_CWD
            sdir = self.sessions_root / slug / _encode_id(sid)
            existing = self._load_log(sdir)
            if existing["path"] is None:
                # Never park an update in a second location: a session that
                # already has a log (possibly under another cwd) is updated
                # in place, mirroring how a missing/changed cwd must not
                # create duplicates.
                any_path = self._find_any_log(sid)
                if any_path is not None:
                    sdir = any_path.parent
                    existing = self._load_log(sdir)
                    if not isinstance(cwd, str) or not cwd:
                        cwd = existing.get("cwd")
            moved = False
            if existing["path"] is not None \
                    and isinstance(cwd, str) and cwd \
                    and sdir.parent.name != _slug(cwd):
                # header cwd (server value) no longer matches the session's
                # dir slug (cwd edits / drive-letter case drift): relocate so
                # dsh's identity check (dir == slug(header cwd)) stays true.
                target = self.sessions_root / _slug(cwd) / _encode_id(sid)
                try:
                    Path(_lp(target.parent)).mkdir(parents=True, exist_ok=True)
                    os.replace(_lp(sdir), _lp(target))
                    moved = True
                except OSError:
                    # Windows NTFS is case-insensitive: a case-only drift
                    # cannot move between dirs; rename in place instead so
                    # the on-disk casing matches the slug.
                    try:
                        if str(sdir.parent.name).lower() == \
                                str(_slug(cwd)).lower():
                            os.rename(_lp(sdir), _lp(target))
                            moved = True
                    except OSError:
                        pass  # keep in place; next pull reconciles
                if moved:
                    sdir = target
                existing = self._load_log(sdir)
            merged, added = self._merge_messages(existing["msgs"], msgs)
            if existing["path"] is None:
                Path(_lp(sdir)).mkdir(parents=True, exist_ok=True)
                stats["imported"] += 1
                any_changed = True
            else:
                if not added and not moved and (
                        s.get("title") is None or
                        existing["title"] == s.get("title")):
                    continue
                stats["updated"] += 1
                any_changed = True
            stats["new_messages"] += added
            try:
                self._write_log(sdir, sid, cwd, s, existing, merged)
            except PermissionError:
                for tname in ("session.jsonl.zstd.tmp", "session.jsonl.tmp"):
                    try:
                        Path(_lp(sdir / tname)).unlink()
                    except OSError:
                        pass
                stats.setdefault("skipped", 0)
                stats["skipped"] += 1
                continue
            except ImportError:
                stats.setdefault("error", 0)
                stats["error"] += 1
                continue
            if canonical not in idmap and sid != canonical:
                idmap[canonical] = sid
        self._save_idmap(idmap)
        if any_changed:
            self._refresh_cache_docs()
        return stats

    def _find_any_log(self, local_id: str) -> Path | None:
        """Locate an existing session log by local id across every project
        dir (encoded id = dir name)."""
        if not self.sessions_root or not self.sessions_root.is_dir():
            return None
        enc = _encode_id(local_id)
        for proj in self.sessions_root.iterdir():
            if not proj.is_dir() or proj.name.startswith("."):
                continue
            sdir = proj / enc
            if sdir.is_dir():
                f = self._log_file(sdir)
                if f is not None:
                    return f
        return None

    def _load_log(self, sdir: Path) -> dict:
        """Parse an existing session log (if any): header/title/message
        (role, ts-ms), event counters, file path."""
        path = self._log_file(sdir)
        out = {"path": path, "title": None, "cwd": None, "msgs": [],
               "seq": -1, "turn": -1, "step": -1}
        if path is None:
            return out
        try:
            raw = Path(_lp(path)).read_bytes()
        except OSError:
            return out
        if path.name.endswith(".zstd"):
            try:
                raw = _decompress_zstd(raw)
            except ImportError:
                return out
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict):
                continue
            t = rec.get("type")
            seq = rec.get("seq")
            if isinstance(seq, int) and seq > out["seq"]:
                out["seq"] = seq
            if t == "session" and isinstance(rec.get("cwd"), str):
                out["cwd"] = rec["cwd"]
            data = rec.get("data")
            if isinstance(data, dict):
                turn = data.get("turn")
                step = data.get("step")
                if isinstance(turn, int) and turn > out["turn"]:
                    out["turn"] = turn
                if isinstance(step, int) and step > out["step"]:
                    out["step"] = step
            if t == "session/title" and isinstance(data, dict) \
                    and isinstance(data.get("title"), str):
                out["title"] = data["title"]
            elif t in ("user/message", "assistant/message"):
                m = self._event_message(rec)
                if m is not None:
                    ts = int(m["timestamp"] * 1000)
                    out["msgs"].append({"role": m["role"], "ts": ts,
                                        "content": m["content"]})
        return out

    def _merge_messages(self, existing: list[dict],
                        msgs: list[dict]) -> tuple[list[dict], int]:
        """existing rows + new canonical messages deduped by
        (role, timestamp-ms); returns (rows in file order, added count)."""
        seen = {(k["role"], k["ts"]) for k in existing}
        rows = list(existing)
        added = 0
        for m in msgs:
            role = m.get("role")
            ts = m.get("timestamp")
            try:
                ms = int(float(ts) * 1000)
            except (TypeError, ValueError):
                ms = int(time.time() * 1000)
            if role in ("user", "assistant") and (role, ms) not in seen:
                seen.add((role, ms))
                rows.append({"role": role, "ts": ms,
                             "content": m.get("content") or ""})
                added += 1
            elif role in ("user", "assistant"):
                pass  # duplicate
        return rows, added

    def _write_log(self, sdir: Path, sid: str, cwd, session: dict,
                   existing: dict, rows: list[dict]):
        """(Re)write one session log atomically: v0 header + contiguous-seq
        user/message & assistant/message events (seq renumbered from 0 on
        every rewrite). Compressed .zstd unless the store already uses plain
        .jsonl."""
        lines = []
        # seq is the 0-based event index of THIS file, renumbered on every
        # rewrite. dsh's reader asserts event.seq === its 0-based position in
        # the expanded event stream; continuing from the previous file's max
        # seq would shift every rewritten log off zero and make the whole
        # file unreadable ("complete frame contains a torn JSONL record").
        seq = 0
        header = {"type": "session", "version": 0, "id": sid}
        try:
            header["createdAt"] = int(float(
                session.get("started_at") or time.time()) * 1000)
        except (TypeError, ValueError):
            header["createdAt"] = int(time.time() * 1000)
        if isinstance(cwd, str) and cwd:
            header["cwd"] = cwd
        header["delegationDepth"] = 0
        lines.append(json.dumps(header, ensure_ascii=False))

        title = session.get("title")
        if isinstance(title, str) and title and existing["title"] != title:
            lines.append(json.dumps({
                "type": "session/title", "seq": seq, "time": header["createdAt"],
                "data": {"title": title, "messageSeqs": [],
                         "source": {"kind": "user"}},
            }, ensure_ascii=False))
            seq += 1

        # turn/step replay from the renumbered file's own rows: the file is
        # rewritten whole, so display counters restart with it.
        turn, step = 0, 0
        last_role = None
        for r in sorted(rows, key=lambda k: (k["ts"], k["role"] != "user")):
            role, ts, content = r["role"], r["ts"], r["content"]
            if role == "user":
                turn += 1
                step = 0
            else:
                step += 1
            ev = {"type": "user/message" if role == "user"
                  else "assistant/message",
                  "seq": seq, "time": ts, "surfaceOp": "append"}
            if role == "user":
                ev["data"] = {"id": f"user-{_msg_id()}", "role": "user",
                              "content": [{"type": "text", "text": content}],
                              "source": {"kind": "user"}}
            else:
                model_s = _model_parts(session.get("model"))
                ev["data"] = {
                    "turn": turn, "step": step,
                    "message": {"id": f"assistant-{_msg_id()}",
                                "role": "assistant",
                                "content": [{"type": "text",
                                             "text": content}],
                                "source": {"kind": "model",
                                           "provider": model_s[0],
                                           "model": model_s[1]}},
                    "sourceEventSeqs": [],
                }
            lines.append(json.dumps(ev, ensure_ascii=False))
            seq += 1
            last_role = role

        payload = ("\n".join(lines) + "\n").encode("utf-8")
        use_zstd = HAVE_ZSTD and not (
            existing["path"] is not None
            and not existing["path"].name.endswith(".zstd"))
        if use_zstd:
            # dsh's own reader asserts the FIRST zstd frame decompresses to
            # exactly the one header line; its append writer emits a frame
            # per write batch. Mirror the strictest compatible shape: one
            # independent frame per line (concatenated), which also keeps
            # every frame's content a whole number of lines.
            payload = b"".join(
                _compress_zstd(line) for line in payload.splitlines(keepends=True))
            fname = "session.jsonl.zstd"
        else:
            fname = "session.jsonl"
        tmp = sdir / (fname + ".tmp")
        Path(_lp(tmp)).write_bytes(payload)
        os.replace(_lp(tmp), _lp(sdir / fname))

    # ------------------------------------------------------------------
    # session projection cache (storages/session_projcache): the DSH
    # Desktop session list reads titles/rows from these per-session docs
    # synchronously. Without a matching doc it falls back to the
    # workspace/dir name until the harness folds the log on open. We write
    # the full v5 doc (rows mirroring the desktop's own fold output,
    # identity = header createdAt/cwd) right after each log write so a
    # fresh pull lists real titles immediately.
    #
    # workspace.json is deliberately NOT written: dsh bootstraps that
    # domain from session headers on first init (fs.realpath canonical
    # paths), and externally synthesized rows break its invariants.
    # ------------------------------------------------------------------
    def _projcache_dir(self) -> Path | None:
        if self.storages_root:
            return self.storages_root / "session_projcache" / "sessions"
        return None

    def _refresh_cache_docs(self):
        """Fold every local session log into its projection-cache doc
        (version 5, identity-matched). Cache is fail-soft: a bad doc is
        ignored by dsh, never authoritative (the log is).

        Sessions without a real header cwd get NO doc (and any stale doc
        from an earlier write is removed): DSH Desktop 2.0.5's v5 schema
        requires ``identity.cwd`` to be a string, so a ``null``-cwd doc is
        quarantined to ``.json.bak.*`` on every boot and recreated by the
        next sync -- a churn loop with no list benefit (cwd-less sessions
        belong to no workspace)."""
        cdir = self._projcache_dir()
        if cdir is None:
            return
        try:
            cdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        for path, local_id in self._session_files():
            doc_path = cdir / f"{local_id}.json"
            meta = self._log_meta(path)
            if meta is None or not meta["cwd"]:
                # no foldable identity: drop any stale doc instead of
                # churning quarantined .bak files on every desktop boot.
                try:
                    doc_path.unlink()
                except OSError:
                    pass
                continue
            doc = {
                "version": 5,
                "record": {
                    "identity": {
                        "createdAt": meta["created_at"],
                        "cwd": meta["cwd"],
                        "isSeeded": False,
                        "inheritedEventCount": 0,
                    },
                    "rows": self._cache_rows(meta),
                },
            }
            try:
                doc_path.write_text(
                    json.dumps(doc, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except OSError:
                pass

    def _log_meta(self, path: Path) -> dict | None:
        """Parse one session log: header created_at/cwd + title + seq stats
        + last user message time (for list metadata)."""
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if path.name.endswith(".zstd"):
            try:
                raw = _decompress_zstd(raw)
            except ImportError:
                return None
        meta = {"created_at": None, "cwd": None, "title": None,
                "max_seq": -1, "last_user_ms": None, "users": []}
        for line in raw.decode("utf-8", "replace").splitlines():
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict):
                continue
            t = rec.get("type")
            seq = rec.get("seq")
            if isinstance(seq, int) and seq > meta["max_seq"]:
                meta["max_seq"] = seq
            if t == "session":
                if isinstance(rec.get("createdAt"), (int, float)):
                    meta["created_at"] = int(rec["createdAt"])
                if isinstance(rec.get("cwd"), str):
                    meta["cwd"] = rec["cwd"]
            elif t == "session/title" and isinstance(rec.get("data"), dict) \
                    and isinstance(rec["data"].get("title"), str):
                meta["title"] = rec["data"]["title"]
            elif t == "user/message" and isinstance(rec.get("time"),
                                                    (int, float)):
                data = rec.get("data")
                texts = []
                if isinstance(data, dict):
                    for blk in (data.get("content") or []):
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            texts.append(str(blk.get("text") or ""))
                meta["users"].append(
                    (int(rec["time"]),
                     "\n".join(t for t in texts if t),
                     int(seq) if isinstance(seq, int) else None))
                meta["last_user_ms"] = int(rec["time"])
        if meta["created_at"] is None:
            return None
        return meta

    @staticmethod
    def _cache_rows(meta: dict) -> dict:
        """Rows mirror the desktop's own folded v5 doc exactly (verified
        against DSH Desktop 2.0.5 output). The list view reads these rows
        synchronously -- without the title row it falls back to the
        workspace/dir name until the session is opened."""
        seq = max(meta["max_seq"], 0)
        first_user = meta["users"][0][1] if meta["users"] else None
        first_seq = meta["users"][0][2] if meta["users"] else None
        last_seq = meta["users"][-1][2] if meta["users"] else None
        title_input = {"first": None, "count": 0, "lastSeq": None}
        if first_user:
            title_input = {"first": {"seq": first_seq, "text": first_user},
                           "count": len(meta["users"]),
                           "lastSeq": last_seq}
        return {
            "title": {"ver": 1, "seq": seq, "val": meta.get("title")},
            "titleInput": {"ver": 3, "seq": seq, "val": title_input},
            "llmRetry": {"ver": 1, "seq": seq, "val": {}},
            "sandboxMode": {"ver": 1, "seq": seq,
                            "val": "workspace-write"},
            "goal": {"ver": 6, "seq": seq,
                     "val": {"current": None, "seenGoalIds": [],
                             "failure": None}},
            "tokenUsage": {
                "ver": 2, "seq": seq,
                "val": {"totals": {"uncachedInputTokens": 0,
                                   "outputTokens": 0,
                                   "cacheReadTokens": 0,
                                   "cacheWriteTokens": 0},
                        "last": None}},
            "contextPressure": {"ver": 4, "seq": seq,
                                "val": {"surfaceTokens": 0}},
            "contextBreakdown": {"ver": 2, "seq": seq,
                                 "val": {"systemTokens": 0,
                                         "toolsTokens": 0,
                                         "messageTokens": 0}},
            "turnBoundary": {"ver": 2, "seq": seq,
                             "val": {"openTurnStartSeq": None,
                                     "lastStepStartSeq": None,
                                     "lastStepBoundary": None,
                                     "lastTurn": 0}},
            "sessionStats": {"ver": 1, "seq": seq,
                             "val": {"turns": 0, "steps": 0, "llmMs": 0,
                                     "toolMs": 0, "ttftMs": 0,
                                     "ttftSteps": 0, "decodeMs": 0,
                                     "decodeTokens": 0, "lastTurn": None,
                                     "openStep": None,
                                     "pendingCalls": {}}},
            "turnOutline": {"ver": 2, "seq": seq,
                            "val": {"turns": [], "draft": ""}},
            "agentPreset": {"ver": 1, "seq": seq, "val": None},
            "subagentTiming": {"ver": 2, "seq": seq,
                               "val": {"descriptorSeen": False,
                                       "settledMs": 0}},
            "subagent": {"ver": 2, "seq": seq, "val": {}},
            "permissions": {"ver": 2, "seq": seq,
                            "val": {"preset": "workspace-write",
                                    "sandbox": "workspace-write",
                                    "approval": "ask",
                                    "seeded": True}},
            "modelSelection": {"ver": 2, "seq": seq,
                               "val": {"lastUsed": None,
                                       "pending": None}},
            "sessionListMetadata": {
                "ver": 1, "seq": seq,
                "val": {"blank": not meta["users"],
                        "lastPromptAt": meta.get("last_user_ms")}},
            "imageLimits": {"ver": 1, "seq": seq, "val": None},
            "todos": {"ver": 2, "seq": seq, "val": None},
            "plan": {"ver": 3, "seq": seq,
                     "val": {"active": False, "wanted": None,
                             "running": None,
                             "activeAtLastHeader": None}},
            "subagentModelSelectionPolicy": {"ver": 1, "seq": seq,
                                             "val": None},
        }

    # ------------------------------------------------------------------
    def status(self) -> dict:
        if not self.sessions_root or not self.sessions_root.is_dir():
            return {"store": str(self.sessions_root), "sessions": 0,
                    "messages": 0}
        files = self._session_files()
        msgs = 0
        for path, _ in files:
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if path.name.endswith(".zstd"):
                try:
                    raw = _decompress_zstd(raw)
                except ImportError:
                    continue
            for line in raw.decode("utf-8", "replace").splitlines():
                if '"type": "user/message"' in line \
                        or '"type":"user/message"' in line \
                        or '"type": "assistant/message"' in line \
                        or '"type":"assistant/message"' in line:
                    msgs += 1
        return {"store": str(self.sessions_root), "sessions": len(files),
                "messages": msgs}


def _decode_id(name: str) -> str:
    """Best-effort reverse of encodeSegment for reading back ids."""
    if "~" not in name:
        return name

    def _sub(m):
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return m.group(0)
    return re.sub(r"~([0-9A-Fa-f]{4})", _sub, name)


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = DshAdapter


if __name__ == "__main__":
    a = DshAdapter()
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions()))
