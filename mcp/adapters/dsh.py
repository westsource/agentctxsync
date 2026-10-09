"""
dsh (official DeepSeek Harness, deepseek-ai/deepseek-harness) adapter.

Local store (0.2.0-rc.2 layout, as written by DSH Desktop / dsh):

    <dsh home>/sessions/--<cwd-slug>--/<encoded-session-id>/session.v4.jsonl.zstd

    - one session per DIRECTORY named <encoded-session-id> (ids look like
      ``session-<uuid>`` and encode unchanged);
    - one file per generation inside: ``session.jsonl`` (v0 name) or
      ``session.vN.jsonl``; compressed with zstd when the store is configured
      for it (the desktop default), suffix ``.zstd``. The adapter publishes the
      CURRENT generation (v4) and leaves an older predecessor byte-identical
      beside it -- the convention dsh's own write path follows;
    - file = header line ``{"type":"session","version":4,"id":...,"createdAt":ms,
      "cwd":...,"isSeeded":false,"delegationDepth":0,"agentPreset":"standard"}``
      followed by typed event envelopes ``{"type":T,"seq":N,"time":ms,"data":{...}}``
      with seq contiguous from 0. A written log opens with the seed head
      (``permission/preset``, ``sandbox/mode``, ``approval/policy``) and one
      ``turn/start`` + ``step/start`` frame per conversation row: dsh's reader
      opens the highest generation and its v0->v1->v2->v3->v4 migration chain
      plus the v4 native admission REFUSE malformed shapes (message-only v0
      logs, ``sourceEventSeqs`` on v0, a user ``session/title`` carrying
      ``messageSeqs``), which surfaced every synced Session in DSH Desktop as
      "历史加载失败";
    - conversation text lives in ``user/message`` (data.role='user') and
      ``assistant/message`` (data.message.role='assistant', content = typed
      blocks incl. ``{type:"text",text}``) -- the latter also carries the
      ``usage``/``stream`` settlement block dsh's validator requires; the
      harness-injected ``system/message`` head of each turn carries
      ``source.kind='system-prompt'`` (the v4 form; v3 used a plugin name);
      reasoning/tool/compaction events are not conversation text and are
      skipped on read;
    - titles arrive as log events ``session/title`` (data.title); a user title
      carries an EMPTY ``messageSeqs`` -- the v4 validator requires it empty
      exactly when ``source.kind='user'``;
    - the DSH Desktop session sidebar groups Sessions by Workspace: the
      ``workspace`` storage domain (``<home>/storages/workspace.json``) keeps one
      record per project directory whose ``sessionIds`` are its members, plus a
      projection cache for list titles. dsh only groups from session headers on
      the ONE-TIME first boot, so this adapter ATTACHES its Sessions to the
      matching record itself (grouping by ``fs.realpath(cwd)``, additively --
      existing records, titles and the durable order survive) and folds the
      projection cache; both are read at desktop boot, so a freshly grouped
      store may need a desktop restart to show.

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

# The ``workspace`` storage domain (dsh-workspace): one record per project
# directory; ``sessionIds`` is the ordered membership account. The adapter only
# touches an initialized, structurally-consistent unit of this exact identity.
_WORKSPACE_UNIT = "workspace"
_WORKSPACE_UNIT_VERSION = 2

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

# Canonical log generations (dsh-session-persistence-jsonl): the unreleased
# ``session.jsonl[.zstd]`` root is v0 and ``session.vN.jsonl[.zstd]`` is the
# vN generation, whose physical header carries that same version. dsh selects
# the NUMERICALLY HIGHEST generation for both read and write, and a write open
# publishes a successor while leaving the source byte-identical — so a migrated
# session's v0 file is frozen history and only its newest-generation file keeps
# growing.
_LOG_FILENAME_RE = re.compile(r"^session(?:\.v(\d+))?\.jsonl(?:\.zstd)?$")

# The generation dsh currently releases (mirrors the toVersion of
# ``@deepseek-ai/dsh-session-format-v3-to-v4``, the last edge DSH Desktop
# 0.2.0-rc.2 mounts). New Sessions are published in it: dsh's reader opens the
# numerically highest generation and migrates older artifacts along the
# v0->v1->v2->v3->v4 chain, whose validators REFUSE malformed predecessors --
# a v0 artifact rejects ``sourceEventSeqs`` and the message-only v3 shape this
# adapter used to write for new Sessions, while the v3->v4 edge rejects a user
# ``session/title`` that carries ``messageSeqs`` ("session/title messageSeqs
# must be empty exactly for a user title"). Every such Session surfaced in DSH
# Desktop as "历史加载失败" and could not be opened.
_CURRENT_LOG_GENERATION = 4

# Seed-head events a current-generation Session opens with, and the permission
# triple they (and the projection-cache ``permissions`` row) carry. Values
# mirror what DSH Desktop itself writes for an unseeded Session.
_PERMISSION_PRESET = "workspace-write"
_SANDBOX_MODE = "workspace-write"
_APPROVAL_POLICY = "ask"
_SEED_HEAD_EVENTS = (
    ("permission/preset", {"preset": _PERMISSION_PRESET}),
    ("sandbox/mode", {"mode": _SANDBOX_MODE}),
    ("approval/policy", {"policy": _APPROVAL_POLICY}),
)
# The system-message source the harness-injected head of a current-generation
# (v4) turn carries. The released v3 shape used a plugin name instead
# (``@deepseek-ai/dsh-system-prompt``), which the v3->v4 migration rewrites to
# this kind and the v4 validator (``dsh-session``) requires; ``_event_message``
# ignores role ``system`` either way.
_SYSTEM_MESSAGE_SOURCE = {"kind": "system-prompt"}
# dsh's agentPreset default for a Session that was not seeded from a preset.
_DEFAULT_AGENT_PRESET = "standard"
# assistant/message settlement block. Token counts are not part of the synced
# payload, so the block is zeroed rather than invented.
_ZERO_USAGE = {"inputTokens": 0, "outputTokens": 0,
               "cacheReadTokens": 0, "reasoningTokens": 0}


def _log_generation(name: str) -> int | None:
    """Generation of a canonical log filename (``session.jsonl`` -> 0)."""
    m = _LOG_FILENAME_RE.fullmatch(name)
    if m is None:
        return None
    return int(m.group(1)) if m.group(1) is not None else 0


def _log_filename(generation: int, zstd: bool) -> str:
    """Canonical filename for one generation (header version == generation)."""
    stem = "session" if generation == 0 else f"session.v{generation}"
    return stem + (".jsonl.zstd" if zstd else ".jsonl")


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


def _isdir(path) -> bool:
    """os.path.isdir through the long-path-safe form (Path.is_dir fails past
    MAX_PATH and turns a real store into "no sessions")."""
    try:
        return os.path.isdir(_lp(path))
    except OSError:
        return False


def _scan(path) -> list[tuple[str, bool, bool, float]]:
    """``[(name, is_dir, is_file, mtime)]`` for one directory, read through the
    long-path-safe form so a deep store root cannot silently hide sessions."""
    out: list[tuple[str, bool, bool, float]] = []
    with os.scandir(_lp(path)) as it:
        for e in it:
            try:
                mtime = e.stat().st_mtime
            except OSError:
                mtime = 0.0
            out.append((e.name, e.is_dir(), e.is_file(), mtime))
    return out


def _mtime(path) -> float:
    """Modification time through the long-path-safe form (0.0 when unreadable)."""
    try:
        return os.stat(_lp(path)).st_mtime
    except OSError:
        return 0.0


def _read_header_line(path: Path) -> dict | None:
    """The session log's header line, decoding only the first zstd frame (the
    log is one frame per line, so the header is cheap to reach)."""
    try:
        with open(_lp(path), "rb") as f:
            if path.name.endswith(".zstd"):
                if not HAVE_ZSTD:
                    return None
                with _zstd.ZstdDecompressor().stream_reader(f) as r:
                    # the reader exposes read() only; a bounded chunk covers
                    # the header line without decoding the whole log
                    line = r.read(1 << 16).split(b"\n", 1)[0]
            else:
                line = f.readline()
    except (OSError, ImportError, ValueError):
        return None
    try:
        rec = json.loads(line.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None
    return rec if isinstance(rec, dict) else None


def _iso_from_ms(ms: int) -> str:
    """``Date(ms).toISOString()`` in UTC, the timestamp form the workspace
    domain stores."""
    t = time.gmtime(ms / 1000.0)
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (
        t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec,
        int(ms) % 1000)


def _norm_path(path) -> str:
    """Case/separator-insensitive comparison key for two path spellings."""
    if not isinstance(path, str):
        return ""
    return os.path.normcase(os.path.normpath(path))


def _canon_dir(cwd) -> str | None:
    """``fs.realpath`` canon of a cwd that exists as a directory, else None.

    Mirrors dsh's ``realpathNormalize``: the workspace registry stamps this
    canon as ``record.path`` and compares it to the same canon of each session
    header's cwd, so only an existing directory has a place in the grouping.
    """
    if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
        return None
    try:
        canon = os.path.realpath(cwd)
    except OSError:
        return None
    return canon if os.path.isdir(canon) else None


def _default_title(path: str) -> str:
    """dsh's ``defaultWorkspaceTitle``: final segment, else the root spelling."""
    base = os.path.basename(path)
    if base:
        return base
    drive, _rest = os.path.splitdrive(path)
    return (drive + os.sep) if drive else path


def _msg_id() -> str:
    return "".join(secrets.choice(_MSG_ID_ALPHABET) for _ in range(8))


def _session_uuid() -> str:
    """A fresh dsh-style session id: ``session-<uuid4>``."""
    return f"session-{uuid.uuid4()}"


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
            if _isdir(p):
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
            d = json.loads(Path(_lp(f)).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_idmap(self, m: dict[str, str]):
        f = self._idmap_file()
        if f is not None:
            Path(_lp(f)).write_text(json.dumps(m, ensure_ascii=False),
                                    encoding="utf-8")

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
        if not self.sessions_root or not _isdir(self.sessions_root):
            return out
        try:
            projects = _scan(self.sessions_root)
        except OSError:
            return out
        for proj_name, is_dir, _, _ in projects:
            if not is_dir or proj_name.startswith("."):
                continue
            proj = self.sessions_root / proj_name
            try:
                entries = _scan(proj)
            except OSError:
                continue
            for sdir_name, sdir_is_dir, _, _ in entries:
                if not sdir_is_dir:
                    continue
                fid = self._log_file(proj / sdir_name)
                if fid is None:
                    continue
                # local id = the session dir name, decoded if it was escaped
                out.append((fid, _decode_id(sdir_name)))
        out.sort(key=lambda t: _mtime(t[0]), reverse=True)
        return out

    @staticmethod
    def _log_file(sdir: Path) -> Path | None:
        """The log dsh itself would open: the highest canonical generation.

        Mirrors ``dsh-session-persistence-jsonl`` ("runtime operations select
        the numerically highest canonical generation"). A migrated session
        keeps its predecessors byte-identical while the successor keeps
        growing, so picking the legacy ``session.jsonl[.zstd]`` reader-first
        (the previous behaviour) went permanently stale. Within one generation
        the compressed root wins over the raw one; mtime breaks any remaining
        tie.
        """
        try:
            entries = _scan(sdir)
        except OSError:
            return None
        best: tuple[tuple[int, int, float], Path] | None = None
        for name, _is_dir, is_file, mtime in entries:
            if not is_file:
                continue
            generation = _log_generation(name)
            if generation is None:
                continue
            key = (generation, 1 if name.endswith(".zstd") else 0, mtime)
            if best is None or key > best[0]:
                best = (key, sdir / name)
        return best[1] if best is not None else None

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
        if not self.sessions_root:
            return {"error": "dsh sessions dir not found"}
        if not sessions:
            # nothing to write, but the grouping pass still heals a store whose
            # Sessions were never attached to a Workspace record
            self._refresh_workspace_index()
            return {"imported": 0, "updated": 0, "new_messages": 0,
                    "duplicates": 0}
        Path(_lp(self.sessions_root)).mkdir(parents=True, exist_ok=True)
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
                # A log from an older generation is upgraded even when nothing
                # else changed: dsh's reader refuses to migrate the legacy
                # message-only artifacts this adapter used to write, so those
                # Sessions stay unopenable until a current-generation successor
                # is published (see _write_log).
                legacy = (_log_generation(existing["path"].name) or 0) < \
                    _CURRENT_LOG_GENERATION
                if not added and not moved and not legacy and (
                        s.get("title") is None or
                        existing["title"] == s.get("title")):
                    continue
                stats["updated"] += 1
                any_changed = True
            stats["new_messages"] += added
            try:
                self._write_log(sdir, sid, cwd, s, existing, merged)
            except PermissionError:
                # the atomic write's temp sibling, whose name follows the
                # session's generation (session[.vN].jsonl[.zstd].tmp)
                try:
                    stale = [p for p in sdir.iterdir()
                             if p.name.endswith(".tmp")
                             and _log_generation(p.name[:-4]) is not None]
                except OSError:
                    stale = []
                for p in stale:
                    try:
                        Path(_lp(p)).unlink()
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
        self._refresh_workspace_index()
        return stats

    def _find_any_log(self, local_id: str) -> Path | None:
        """Locate an existing session log by local id across every project
        dir (encoded id = dir name)."""
        if not self.sessions_root or not _isdir(self.sessions_root):
            return None
        enc = _encode_id(local_id)
        try:
            projects = _scan(self.sessions_root)
        except OSError:
            return None
        for proj_name, is_dir, _, _ in projects:
            if not is_dir or proj_name.startswith("."):
                continue
            sdir = self.sessions_root / proj_name / enc
            f = self._log_file(sdir)
            if f is not None:
                return f
        return None

    def _load_log(self, sdir: Path) -> dict:
        """Parse an existing session log (if any): header/title/message
        (role, ts-ms), event counters, file path."""
        path = self._log_file(sdir)
        out = {"path": path, "header": None, "title": None, "cwd": None,
               "msgs": [], "seq": -1, "turn": -1, "step": -1}
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
            if t == "session" and out["header"] is None:
                # kept verbatim so a rewrite preserves generation-specific
                # fields (version, isSeeded, agentPreset, …) we do not model
                out["header"] = rec
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
        (role, timestamp-ms); returns (rows in file order, added count).

        Text-less rows are skipped: an assistant turn that carried only tool
        calls / reasoning canonicalizes to an empty string, which the log
        cannot represent as a conversation event (``_event_message`` drops it).
        Storing it would make the row invisible on read and therefore "new"
        again on every pull -- every synced Session would be rewritten on every
        sync cycle forever.
        """
        seen = {(k["role"], k["ts"]) for k in existing}
        rows = list(existing)
        added = 0
        for m in msgs:
            role = m.get("role")
            if role in ("user", "assistant") \
                    and not str(m.get("content") or "").strip():
                continue
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
        """(Re)write one session log atomically in dsh's CURRENT generation:
        the seed head (permission/preset, sandbox/mode, approval/policy), then
        one turn/step frame per conversation row, with the settlement fields
        (``usage`` / ``stream``) an assistant/message must carry.

        dsh's reader opens the numerically highest generation and migrates
        older artifacts through v0->v1->v2->v3->v4 — but those edges and the
        v4 native admission REFUSE the shapes this adapter used to publish
        (message-only v0 logs; a v0 ``sourceEventSeqs``; and, on the v3->v4
        edge, a user ``session/title`` carrying ``messageSeqs``), which made
        every synced Session unopenable in DSH Desktop ("历史加载失败").

        An existing log of the current generation is rewritten in place; a
        predecessor of an older generation is left byte-identical with a
        successor published next to it, mirroring dsh's own write path (the
        reader always takes the highest generation, published predecessors are
        never mutated). Header fields of a foreign generation are not carried
        over — ``version`` / ``isSeeded`` / ``agentPreset`` are
        generation-specific; only identity fields survive.
        """
        src = existing.get("path")
        generation = _CURRENT_LOG_GENERATION
        if src is not None:
            prev = _log_generation(src.name)
            if prev is not None and prev > generation:
                generation = prev  # a newer artifact exists: never downgrade
        # Compressed unless the current generation's own log is raw; a fresh
        # successor follows the platform default (zstd when available).
        use_zstd = HAVE_ZSTD and (src is None or src.name.endswith(".zstd")
                                  or (_log_generation(src.name) or 0)
                                  < generation)
        header = dict(existing.get("header") or {})
        if header.get("version") != generation:
            # foreign generation: keep the identity fields only
            header = {k: header[k] for k in ("createdAt",) if k in header}
        header.update({"type": "session", "version": generation, "id": sid})
        try:
            created = int(float(session.get("started_at") or time.time()) * 1000)
        except (TypeError, ValueError):
            created = int(time.time() * 1000)
        header.setdefault("createdAt", created)
        if not isinstance(header.get("createdAt"), (int, float)):
            header["createdAt"] = created
        header["createdAt"] = int(header["createdAt"])
        header.pop("cwd", None)
        if isinstance(cwd, str) and cwd:
            header["cwd"] = cwd
        header.setdefault("delegationDepth", 0)
        if generation >= 1:
            # The current format records isSeeded/agentPreset in the physical
            # header (dsh's v2->v3 header check requires isSeeded). Preserved
            # headers already carry them; this only covers a synthesized one.
            header.setdefault("isSeeded", False)
            header.setdefault("agentPreset", _DEFAULT_AGENT_PRESET)

        lines = [json.dumps(header, ensure_ascii=False)]
        # seq is the 0-based event index of THIS file, renumbered on every
        # rewrite: dsh's reader asserts event.seq === its 0-based position in
        # the expanded event stream, so continuing a predecessor's numbering
        # would shift the whole file off zero ("complete frame contains a torn
        # JSONL record").
        seq = 0

        def emit(rec: dict, ts: int):
            """Append one event, assigning the next dense seq."""
            nonlocal seq
            rec["seq"] = seq
            rec.setdefault("time", ts)
            lines.append(json.dumps(rec, ensure_ascii=False))
            seq += 1

        base = header["createdAt"]
        for etype, data in _SEED_HEAD_EVENTS:
            emit({"type": etype, "data": dict(data)}, base)

        title = session.get("title")
        if not (isinstance(title, str) and title):
            # no title in the payload (never titled, or a local edit the pull
            # deliberately withheld): keep the one the log already carries --
            # the published successor is the file dsh reads, so dropping the
            # event would lose the title from the desktop list
            title = existing["title"]
        want_title = bool(isinstance(title, str) and title)
        # One turn/step frame per conversation row, mirroring the chronology
        # dsh writes: a user row opens a turn and its first step (the harness
        # system head lands in it), the model reply settles inside that step,
        # and any further reply in the same turn opens the next step.
        turn = step = 0
        settled = False        # the open step already carries a model reply
        last_ts = base

        def system_head(ts: int):
            """The protected first surface head of the open turn. v4 requires
            the very first surface event to be this system message; a turn
            that opens on a model reply (no preceding user row) gets one too.
            """
            emit({"type": "system/message", "surfaceOp": "append",
                  "data": {"turn": turn, "step": step,
                           "message": {
                               "id": f"system-{_msg_id()}",
                               "role": "system",
                               "source": dict(_SYSTEM_MESSAGE_SOURCE),
                               "content": []}}}, ts)

        def close_turn(ts: int):
            """Close the open turn. A turn whose step never got a model reply
            is `interrupted`: v4 forbids a successor ``turn/start`` while a
            turn is still open, which is exactly what consecutive user rows
            (a dropped/absent assistant reply) produce.
            """
            nonlocal settled
            if not turn:
                return
            emit({"type": "step/end",
                  "data": {"turn": turn, "step": step}}, ts)
            emit({"type": "turn/end",
                  "data": {"turn": turn,
                           "reason": {"kind": "completed" if settled
                                      else "interrupted"}}}, ts)
            settled = False

        def open_turn(ts: int):
            """Open the next dense turn with its first step and system head."""
            nonlocal turn, step, settled
            turn += 1
            step = 1
            settled = False
            emit({"type": "turn/start", "data": {"turn": turn}}, ts)
            emit({"type": "step/start",
                  "data": {"turn": turn, "step": step}}, ts)
            system_head(ts)

        for r in sorted(rows, key=lambda k: (k["ts"], k["role"] != "user")):
            role, ts, content = r["role"], r["ts"], r["content"]
            last_ts = ts
            if role == "user":
                close_turn(ts)
                open_turn(ts)
                emit({"type": "user/message", "surfaceOp": "append",
                      "data": {"id": f"user-{_msg_id()}", "role": "user",
                               "content": [{"type": "text", "text": content}],
                               "source": {"kind": "user"}}}, ts)
                if want_title:
                    want_title = False
                    # A user title carries NO messageSeqs in the current
                    # generation: the v4 validator (and the v3->v4 edge)
                    # requires them empty exactly when source.kind is "user".
                    emit({"type": "session/title",
                          "data": {"title": title, "messageSeqs": [],
                                   "source": {"kind": "user"}}}, ts)
                continue
            if settled:
                # a further model step in the same turn: close the open step
                emit({"type": "step/end",
                      "data": {"turn": turn, "step": step}}, ts)
                step += 1
                emit({"type": "step/start",
                      "data": {"turn": turn, "step": step}}, ts)
                settled = False
            elif not turn:
                # a model reply without a preceding user row (defensive):
                # open the first turn so the log still leads with the
                # protected system head v4 requires
                open_turn(ts)
            # dsh's validator requires source.kind=model with provider/model
            # strings; the model NAME is never synced
            # (LOCAL_ONLY_SESSION_FIELDS, decision record 2026.10.06.1), so the
            # placeholder this already used for model-less sessions is used
            # for every pulled session.
            model_s = ("unknown", "unknown")
            emit({"type": "assistant/message", "surfaceOp": "append",
                  "data": {"turn": turn, "step": step,
                           "message": {"id": f"assistant-{_msg_id()}",
                                       "role": "assistant",
                                       "content": [{"type": "text",
                                                    "text": content}],
                                       "source": {"kind": "model",
                                                  "provider": model_s[0],
                                                  "model": model_s[1]}},
                           "usage": dict(_ZERO_USAGE),
                           "stream": []}}, ts)
            settled = True
        close_turn(last_ts)
        if want_title:
            emit({"type": "session/title",
                  "data": {"title": title, "messageSeqs": [],
                           "source": {"kind": "user"}}}, last_ts)

        payload = ("\n".join(lines) + "\n").encode("utf-8")
        fname = _log_filename(generation, use_zstd)
        if use_zstd:
            # dsh's own reader asserts the FIRST zstd frame decompresses to
            # exactly the one header line; its append writer emits a frame
            # per write batch. Mirror the strictest compatible shape: one
            # independent frame per line (concatenated), which also keeps
            # every frame's content a whole number of lines.
            payload = b"".join(
                _compress_zstd(line) for line in payload.splitlines(keepends=True))
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
    # The ``workspace`` domain (storages/workspace.json) is reconciled
    # additively by ``_refresh_workspace_index`` further down.
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
            Path(_lp(cdir)).mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        for path, local_id in self._session_files():
            doc_path = cdir / f"{local_id}.json"
            meta = self._log_meta(path)
            if meta is None or not meta["cwd"]:
                # no foldable identity: drop any stale doc instead of
                # churning quarantined .bak files on every desktop boot.
                try:
                    Path(_lp(doc_path)).unlink()
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
                Path(_lp(doc_path)).write_text(
                    json.dumps(doc, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except OSError:
                pass

    # ------------------------------------------------------------------
    # workspace domain (storages/workspace.json): the Desktop sidebar groups
    # Sessions by Workspace, and a Workspace's ``sessionIds`` are its members.
    # dsh groups from session headers only on its ONE-TIME first boot
    # (``initialized: true`` never re-scans), so an externally written Session
    # stays "Ungrouped" forever. Reconcile additively, mirroring dsh's own
    # header bootstrap (group by ``fs.realpath(cwd)``): existing records,
    # titles and the durable order are preserved; only membership and newly
    # seen directories are added, so the domain's invariants (one owner per
    # session, one record per canonical path, order == table keys) hold.
    # ------------------------------------------------------------------
    def _workspace_file(self) -> Path | None:
        if self.storages_root:
            return self.storages_root / "workspace.json"
        return None

    def _refresh_workspace_index(self):
        """Attach this store's Sessions to their cwd's Workspace record.

        Fail-soft by design: an absent, foreign, uninitialized or already
        inconsistent unit is left untouched (dsh owns it then), and any parse
        or I/O fault aborts the pass without failing the sync. Runs after every
        write so a store healed by a newer client groups its whole history,
        not only the sessions of the current page."""
        wf = self._workspace_file()
        if wf is None:
            return
        try:
            doc = json.loads(Path(_lp(wf)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        unit = doc.get("unit") if isinstance(doc, dict) else None
        state = doc.get("global") if isinstance(doc, dict) else None
        tables = doc.get("tables") if isinstance(doc, dict) else None
        if not isinstance(unit, dict) or unit.get("name") != _WORKSPACE_UNIT \
                or unit.get("version") != _WORKSPACE_UNIT_VERSION \
                or not isinstance(state, dict) or not isinstance(tables, dict):
            return
        records = tables.get("workspaces")
        order = state.get("workspaceIds")
        if not isinstance(records, dict) or not isinstance(order, list) \
                or state.get("initialized") is not True \
                or state.get("pendingMutation") is not None:
            return
        # An initialized domain must account for exactly its records; anything
        # else is corruption we must not build on.
        if len(order) != len(records) or set(map(str, order)) != set(records):
            return
        by_path: dict[str, str] = {}
        accounted: set[str] = set()
        for wid, rec in records.items():
            if not isinstance(rec, dict) or not isinstance(rec.get("path"), str) \
                    or not isinstance(rec.get("sessionIds"), list):
                return
            by_path[_norm_path(rec["path"])] = wid
            accounted.update(map(str, rec["sessionIds"]))

        groups: dict[str, list[tuple[int, str]]] = {}
        for path, local_id in self._session_files():
            if local_id in accounted:
                continue
            header = _read_header_line(path)
            if header is None:
                continue
            canon = _canon_dir(header.get("cwd"))
            if canon is None:
                continue
            created = header.get("createdAt")
            created = int(created) if isinstance(created, (int, float)) else 0
            groups.setdefault(canon, []).append((created, str(local_id)))

        now = _iso_from_ms(int(time.time() * 1000))
        new_ids: list[str] = []
        changed = False
        for canon, members in sorted(groups.items(),
                                     key=lambda kv: max(m[0] for m in kv[1]),
                                     reverse=True):
            wid = by_path.get(_norm_path(canon))
            if wid is None:
                wid = str(uuid.uuid4())
                created = _iso_from_ms(max(m[0] for m in members))
                records[wid] = {"path": canon, "title": _default_title(canon),
                                "sessionIds": [], "createdAt": created,
                                "updatedAt": created}
                by_path[_norm_path(canon)] = wid
                new_ids.append(wid)
                changed = True
            rec = records[wid]
            have = set(map(str, rec["sessionIds"]))
            add = [sid for _c, sid in sorted(members, reverse=True)
                   if sid not in have]
            if add:
                rec["sessionIds"] = add + list(rec["sessionIds"])
                rec["updatedAt"] = now
                changed = True
        if not changed:
            return
        if new_ids:
            # dsh prepends a newly created Workspace to the durable order.
            state["workspaceIds"] = new_ids + list(order)
        state.setdefault("archivedSessionIds", [])
        state.setdefault("pinnedSessionIds", [])
        try:
            Path(_lp(wf)).write_text(
                json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        except OSError:
            pass

    def _log_meta(self, path: Path) -> dict | None:
        """Parse one session log: header created_at/cwd + title + seq stats
        + last user message time (for list metadata)."""
        try:
            raw = Path(_lp(path)).read_bytes()
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
                            "val": _SANDBOX_MODE},
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
                            "val": {"preset": _PERMISSION_PRESET,
                                    "sandbox": _SANDBOX_MODE,
                                    "approval": _APPROVAL_POLICY,
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
        if not self.sessions_root or not _isdir(self.sessions_root):
            return {"store": str(self.sessions_root), "sessions": 0,
                    "messages": 0}
        files = self._session_files()
        msgs = 0
        for path, _ in files:
            try:
                raw = Path(_lp(path)).read_bytes()
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
