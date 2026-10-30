"""
Hermes desktop app adapter (multi-profile).

Local store layout (Windows):
    <root>/state.db                  -- default profile
    <root>/profiles/<name>/state.db  -- named profile (magic, coder, ...)

The adapter scans ALL profiles under the platform root and syncs every
state.db it finds:

    default profile       -> bare session ids (legacy, backwards compatible)
    named profile "magic" -> "magic:<bare>" canonical ids

This is simpler and more robust than resolving a single "active" profile:
Hermes switches profiles via an in-process ContextVar that is NOT inherited
by the MCP server subprocess, and the desktop app neither writes an
active_profile marker nor passes HERMES_HOME into the child -- so a
single-profile adapter would always read the default store. Scanning
profiles/ means one MCP server (registered under the default config, which
is what Hermes activates on startup) syncs every profile on the machine.

The server distinguishes profiles purely by the id prefix (session key is
(workspace_id, id)); non-hermes agents are never profile-filtered.
"""

import json
import os
import sqlite3
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .base import SQLiteAdapter, AGENT_PREFIXES

try:  # gateway-RPC write path (optional; see mcp/hermes_gateway.py)
    from hermes_gateway import enabled as gateway_writes_enabled
    from hermes_gateway import import_sessions as gateway_import_sessions
except ImportError:  # adapter inspected outside the mcp package
    gateway_writes_enabled = lambda: False  # noqa: E731
    gateway_import_sessions = None


class HermesAdapter(SQLiteAdapter):
    """Hermes multi-profile state.db adapter (1:1 column mapping)."""

    agent_type = "hermes"

    #: per-profile projects.db (see read_projects/write_projects below)
    supports_projects = True

    table_sessions = "sessions"
    table_messages = "messages"

    #: relative dir holding named profiles under the platform root
    profiles_dir = "profiles"

    def __init__(self, db_path: Path | str | None = None):
        if db_path is not None:
            # explicit single-db mode (tests / diagnostics): behave like a
            # plain SQLite adapter bound to that file.
            self._home = Path(db_path).resolve().parent
            self._aggregate = False
            super().__init__(db_path)
            return
        self._home = self._platform_root()
        self._aggregate = True
        super().__init__(self._home / "state.db")

    # ------------------------------------------------------------------
    # path resolution
    # ------------------------------------------------------------------
    def _platform_root(self) -> Path:
        """Platform-native Hermes home root (pre-profile)."""
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            return Path(local) / "hermes"
        if os.name == "nt":
            return Path.home() / "AppData" / "Local" / "hermes"
        return Path.home() / ".hermes"

    def discover(self) -> Path | None:
        db = self._home / "state.db"
        return db if db.exists() else None

    # ------------------------------------------------------------------
    # multi-profile discovery
    # ------------------------------------------------------------------
    def _profile_dbs(self) -> list[tuple[str, Path]]:
        """[(profile_name, state.db path)] for every profile on the machine.

        ``profile_name`` is '' for the default profile.  Only existing db
        files are returned.  Default is always first, named profiles sorted
        by name.
        """
        root = self._platform_root()
        out: list[tuple[str, Path]] = []
        default_db = root / "state.db"
        if default_db.exists():
            out.append(("", default_db))
        pdir = root / self.profiles_dir
        if pdir.is_dir():
            for sub in sorted(pdir.iterdir()):
                if sub.is_dir():
                    db = sub / "state.db"
                    if db.exists():
                        out.append((sub.name, db))
        return out

    def _sub_adapter(self, name: str, db_path: Path) -> "HermesAdapter":
        """A non-aggregating adapter bound to one profile's state.db."""
        a = HermesAdapter.__new__(HermesAdapter)
        a._home = db_path.parent
        a._aggregate = False
        a.db_path = db_path
        a.table_sessions = self.table_sessions
        a.table_messages = self.table_messages
        return a

    @property
    def profile_name(self) -> str:
        """Profile name of this adapter's store, '' for default."""
        home = self._home
        root = self._platform_root()
        if home == root:
            return ""
        return home.name if home.parent == root / self.profiles_dir else ""

    # ------------------------------------------------------------------
    # profile-scoped session identity
    # ------------------------------------------------------------------
    def _id_prefix(self) -> str:
        """Canonical id prefix for this adapter's profile.

        default profile keeps bare ids (legacy, backwards compatible);
        named profiles namespace ids as ``<profile>:<bare>`` so sessions
        from different profiles never collide on the shared server.
        """
        name = self.profile_name
        return f"{name}:" if name else ""

    def canonicalize(self, local_session: dict) -> dict:
        """Canonicalize a session from this adapter's profile.

        id-scheme: canonical ids are bare (no ``<profile>:`` prefix); the
        hermes profile travels in the ``profile_name`` field. Legacy
        prefixed local rows (old pulls stored ``magic:...`` pass-through
        ids) are left untouched — the server's inbound shim normalizes
        them.
        """
        s = dict(local_session)
        lid = str(s["id"])
        # NOTE: _is_foreign() is always False for hermes (no foreign-ids
        # sidecar is defined), kept for symmetry with the base contract.
        if self._is_foreign(lid):
            return super().canonicalize(local_session)
        name = self.profile_name
        s["id"] = lid
        if name:
            s["profile_name"] = name
        else:
            # hermes' own schema has a `profile_name` COLUMN whose literal
            # default is "default", and canonicalize() starts from the local
            # row -- so that literal used to travel to the server. It is not
            # the canonical spelling of the default profile (""/NULL is), and
            # no machine's routing table has a "default" key, so such a row
            # was silently skipped on every pull (see write_sessions).
            s.pop("profile_name", None)
        s["messages"] = [dict(m) for m in s.get("messages", [])]
        for m in s["messages"]:
            m["session_id"] = m.get("session_id") or lid
        return s

    def localize(self, canonical_session: dict, strict: bool = True) -> dict:
        """Map a canonical session to this adapter's profile's local form.

        ids are bare; a legacy ``<profile>:``-prefixed id is stripped so
        pre-migration payloads still land in the right profile store.
        """
        s = dict(canonical_session)
        prefix = self._id_prefix()
        cid = str(s["id"])
        if prefix and cid.startswith(prefix):
            lid = cid[len(prefix):]
            s["id"] = lid
            s["messages"] = [dict(m) for m in s.get("messages", [])]
            for m in s["messages"]:
                m_sid = m.get("session_id", cid)
                m["session_id"] = m_sid[len(prefix):] \
                    if isinstance(m_sid, str) and m_sid.startswith(prefix) else m_sid
            return s
        return super().localize(canonical_session, strict=strict)

    # ------------------------------------------------------------------
    # aggregate read / routed write
    # ------------------------------------------------------------------
    def read_sessions(self, limit: int | None = None, *, ids=None,
                      with_messages: bool = True) -> list[dict]:
        """Push view: sessions from EVERY profile on the machine, merged.

        default sessions keep bare ids; named-profile sessions carry their
        ``<profile>:`` prefix so the server never confuses profiles.
        Sub-agent children (``parent_session_id``) are folded into their
        parent so the main agent and its sub-agents surface as one session.

        ``ids`` restricts the read to those sessions plus everything folding
        needs to reproduce them (the ancestor chain and the root's whole
        subtree), and ``with_messages=False`` returns header rows only.
        """
        if not self._aggregate:
            # single-db mode: canonicalize each session, then fold children
            sessions = [self.canonicalize(s)
                        for s in super().read_sessions(
                            limit=limit, ids=ids, with_messages=with_messages)]
            if not with_messages:
                # canonicalize() re-attaches an empty message list; a header
                # row must not carry one (callers fingerprint off the store).
                for s in sessions:
                    s.pop("messages", None)
                return sessions
            return self._fold_subagent_sessions(sessions)
        merged: list[dict] = []
        for name, db in self._profile_dbs():
            sub = self._sub_adapter(name, db)
            wanted = None
            if ids is not None:
                wanted = self._expand_fold_ids(db, ids)
                if not wanted:
                    continue
            # sub.read_sessions() already canonicalizes its rows; parent and
            # child always live in the same profile's state.db, so fold per
            # profile (parent_session_id is a local id).
            sessions = sub.read_sessions(limit=limit, ids=wanted,
                                         with_messages=with_messages)
            merged.extend(self._fold_subagent_sessions(sessions)
                          if with_messages else sessions)
        merged.sort(key=lambda s: s.get("started_at") or 0, reverse=True)
        return merged

    def read_session_headers(self, limit: int | None = None) -> list[dict]:
        """Session rows (no messages) for the pull-side inventory."""
        return self.read_sessions(limit=limit, with_messages=False)

    def session_fingerprints(self) -> dict[str, tuple]:
        """``{root session id: (message_count, max_timestamp)}`` per profile.

        Sub-agent messages are folded into their root before the push, so a
        child's new messages must change the ROOT's fingerprint -- otherwise
        a continuing sub-agent conversation would never be re-pushed.
        """
        out: dict[str, tuple] = {}
        if not self._aggregate:
            return self._fingerprints_for(self.db_path)
        for name, db in self._profile_dbs():
            out.update(self._fingerprints_for(db))
        return out

    @staticmethod
    def _fingerprints_for(db_path) -> dict[str, tuple]:
        import sqlite3
        try:
            conn = sqlite3.connect(SQLiteAdapter._ro_uri(db_path),
                                   uri=True)
        except sqlite3.Error:
            return {}
        try:
            parent = {str(r[0]): (str(r[1]) if r[1] else "")
                      for r in conn.execute(
                          "SELECT id, parent_session_id FROM sessions")}
            agg = {str(r[0]): (int(r[1]), float(r[2] or 0))
                   for r in conn.execute(
                       "SELECT session_id, COUNT(*), MAX(timestamp) "
                       "FROM messages GROUP BY session_id")}
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        roots: dict[str, str] = {}
        for sid in parent:
            cur, seen = sid, set()
            while parent.get(cur) and cur not in seen:
                seen.add(cur)
                cur = parent[cur]
            roots[sid] = cur if cur in parent else sid
        out: dict[str, tuple] = {}
        for sid, root in roots.items():
            count, latest = agg.get(sid, (0, 0.0))
            prev_count, prev_latest = out.get(root, (0, 0.0))
            out[root] = (prev_count + count, max(prev_latest, latest))
        # Message rows whose session row is gone still belong to a push: the
        # base reader only walks session rows, so this mirrors its output.
        for sid, (count, latest) in agg.items():
            if sid not in roots:
                prev_count, prev_latest = out.get(sid, (0, 0.0))
                out[sid] = (prev_count + count, max(prev_latest, latest))
        return out

    @classmethod
    def _expand_fold_ids(cls, db_path, ids) -> set[str]:
        """Expand requested ids to what folding needs in this profile.

        Reading a root without its children would drop the folded sub-agent
        messages (and report a smaller session than the store holds), while
        reading a child without its root cannot fold it at all. Both sides of
        every ``parent_session_id`` edge are therefore included.
        """
        import sqlite3
        wanted = {str(i) for i in ids}
        try:
            conn = sqlite3.connect(cls._ro_uri(db_path), uri=True)
        except sqlite3.Error:
            return wanted
        try:
            parent = {str(r[0]): (str(r[1]) if r[1] else "")
                      for r in conn.execute(
                          "SELECT id, parent_session_id FROM sessions")}
        except sqlite3.Error:
            return wanted
        finally:
            conn.close()
        children: dict[str, list[str]] = {}
        for sid, pid in parent.items():
            if pid:
                children.setdefault(pid, []).append(sid)
        roots: set[str] = set()
        for sid in wanted:
            cur, seen = sid, set()
            while parent.get(cur) and cur not in seen:
                seen.add(cur)
                cur = parent[cur]
            roots.add(cur if cur in parent else sid)
        out: set[str] = {i for i in wanted if i not in parent}
        stack = list(roots)
        while stack:
            sid = stack.pop()
            if sid in out:
                continue
            out.add(sid)
            stack.extend(children.get(sid, ()))
        return out

    @staticmethod
    def _fold_subagent_sessions(sessions: list[dict]) -> list[dict]:
        """Fold sub-agent children into their parent conversation.

        Hermes stores each sub-agent turn as its own session row linked via
        ``parent_session_id`` (children usually carry no title). Folding =
        re-attribute the child's messages to the parent (``session_id`` ->
        parent id), tag them ``meta.subagent`` so the viewer can badge them,
        sort the merged thread by timestamp, and drop the child from the
        push output. The parent's ``message_count`` is recomputed from the
        merged list. Sessions whose parent is not in this batch are kept
        untouched so no data is ever lost.
        """
        by_id = {str(s["id"]): s for s in sessions}
        roots: list[dict] = []
        for s in sessions:
            pid = str(s.get("parent_session_id") or "")
            parent = by_id.get(pid)
            if parent is None or parent is s:
                roots.append(s)
                continue
            # Follow the chain to the ultimate root (sub-agent of sub-agent).
            root = parent
            seen = {str(s["id"])}
            while True:
                rpid = str(root.get("parent_session_id") or "")
                nxt = by_id.get(rpid)
                if nxt is None or nxt is root or str(nxt["id"]) in seen:
                    break
                seen.add(str(nxt["id"]))
                root = nxt
            pmsgs = root.setdefault("messages", [])
            for m in s.get("messages", []):
                m = dict(m)
                m["session_id"] = root["id"]
                meta = m.get("meta")
                meta = dict(meta) if isinstance(meta, dict) else {}
                meta["subagent"] = True
                m["meta"] = meta
                pmsgs.append(m)
        for s in roots:
            msgs = s.get("messages", [])
            if len(msgs) > 1:
                msgs.sort(key=lambda m: m.get("timestamp") or 0)
            s["message_count"] = len(msgs)
        return roots

    def write_sessions(self, sessions: list[dict]) -> dict:
        """Pull view: route each canonical session to the profile that owns
        it (``profile_name`` field; legacy id-prefix fallback). Sessions
        whose profile does not exist locally (other machines' profiles) are
        skipped.  Foreign-agent sessions land in the default profile with
        their canonical id intact — hermes' pass-through id handling
        round-trips them verbatim, and push_sessions never re-pushes them
        (owner filter).

        With ``HERMES_SYNC_HERMES_API`` pointing at a running Hermes backend
        (``hermes serve``), sessions that do not exist locally yet are
        imported through Hermes' own HTTP API, which runs the write inside
        the Hermes process (single writer, revision/epoch fencing). Hermes'
        import refuses existing ids (it is an import, not an upsert), so
        message appends to an existing session still go through the local
        SQLite writer below.
        """
        if not self._aggregate:
            return super().write_sessions(sessions)
        # build profile -> sub-adapter map ('' = default profile)
        route: dict[str, HermesAdapter] = {}
        for name, db in self._profile_dbs():
            route[name] = self._sub_adapter(name, db)
        totals = {"imported": 0, "updated": 0, "new_messages": 0, "duplicates": 0}
        skipped = 0
        new_by_profile: dict[str, list[dict]] = {}
        via_sqlite: dict[str, list[dict]] = {}
        for s in sessions:
            cid = str(s["id"])
            profile = str(s.get("profile_name") or "")
            if not profile and ":" in cid:
                # legacy prefixed id (<profile>:<bare> / <agent>:<bare>):
                # hermes-profile prefix -> profile; known agent prefix ->
                # foreign session in the default profile
                pfx, _ = cid.split(":", 1)
                if pfx + ":" in AGENT_PREFIXES.values():
                    profile = ""
                elif pfx != "default":
                    profile = pfx
            if profile == "default" and profile not in route:
                # Alias for the default profile: hermes' local column literal
                # ("default") instead of ""/NULL was pushed by older clients
                # and is still stored server-side. Without this the row
                # matches no route entry and is silently dropped. An exact
                # local profile actually named "default" still wins.
                profile = ""
            target = route.get(profile)  # '' -> default profile
            if target is None:
                skipped += 1
                continue
            # Remember the owner agent of foreign-agent sessions so
            # push_sessions can tag them correctly. hermes' state.db has no
            # agent_type column, so without this registry a pulled
            # workbuddy/codex session would be re-pushed tagged as hermes
            # and every message would be mis-attributed on the server.
            owner = s.get("agent_type") or ""
            if owner and owner != "hermes":
                target._remember_foreign(cid, owner)
            if target._has_session(target.localize(s)["id"]):
                via_sqlite.setdefault(profile, []).append(s)
            else:
                new_by_profile.setdefault(profile, []).append(s)
        for profile, batch in new_by_profile.items():
            remaining = self._write_new_sessions(route[profile], profile,
                                                 batch, totals)
            if remaining:
                via_sqlite.setdefault(profile, []).extend(remaining)
        for profile, batch in via_sqlite.items():
            target = route[profile]
            stats = target.write_sessions(batch)
            for k in totals:
                totals[k] += stats.get(k, 0)
        if skipped:
            self._log_profile_skip(skipped)
        return totals

    # ------------------------------------------------------------------
    # Remote write paths for sessions that are new to this store
    # ------------------------------------------------------------------
    def _write_new_sessions(self, target: "HermesAdapter", profile: str,
                            batch: list[dict], totals: dict) -> list[dict]:
        """Try the single-writer paths in order; return what still needs SQLite.

        ``HERMES_SYNC_HERMES_API`` (a ``hermes serve`` backend) first, then the
        running gateway's JSON-RPC (the Desktop case, where a second ``serve``
        is refused), then the caller's local writer. Each step is advisory: a
        refusal only ever costs the optimisation, never the pull.
        """
        originals = batch
        prepared = [self._remote_payload(target, s) for s in originals]
        api_base = self._hermes_api_base()
        if api_base:
            prepared = self._import_via_api(api_base, profile, prepared, totals)
            if not prepared:
                return []
        if gateway_import_sessions is not None and gateway_writes_enabled():
            result = gateway_import_sessions(self._platform_root(),
                                             target._home, prepared)
            if result.get("ok"):
                totals["imported"] += int(result.get("imported") or 0)
                failed = {str(i) for i in (result.get("failed") or [])}
                # Hand back the ORIGINAL payload: the local writer applies its
                # own title rule, and a remote-only suffix must not stick.
                return [s for s in originals if str(s["id"]) in failed]
            self._log_api_once(
                f"gateway write unavailable: {result.get('reason')}")
        return originals

    def _remote_payload(self, target: "HermesAdapter", session: dict) -> dict:
        """Payload for a remote import: titles disambiguated, no local rows.

        Hermes enforces a partial ``UNIQUE INDEX`` on ``sessions.title``; an
        import carrying a title this profile already holds fails the whole
        statement (measured: ``UNIQUE constraint failed: sessions.title``),
        so the same ``" (N)"`` rule the local writer applies is applied here.
        """
        s = dict(session)
        title = s.get("title")
        if isinstance(title, str) and title.strip():
            local_id = str(target.localize(s)["id"])
            s["title"] = target._unique_title(title, local_id)
        s["messages"] = [dict(m) for m in (s.get("messages") or [])]
        return s

    def _unique_title(self, title: str, exclude_id: str | None = None) -> str:
        """First ``title``/``title (N)`` this profile's store does not hold."""
        conn = self._conn(readonly=True)
        try:
            candidate, n = title, 2
            while conn.execute(
                    "SELECT 1 FROM %s WHERE title = ? AND id <> ?"
                    % self.table_sessions,
                    (candidate, exclude_id or "")).fetchone():
                candidate = f"{title} ({n})"
                n += 1
            return candidate
        except sqlite3.Error:
            return title
        finally:
            conn.close()

    _logged_api_reasons: set = set()

    @classmethod
    def _log_api_once(cls, message: str):
        """Log each gateway/API unavailability reason once per process."""
        if message in cls._logged_api_reasons:
            return
        cls._logged_api_reasons.add(message)
        cls._log_api(message)

    # ------------------------------------------------------------------
    # Hermes backend API (optional write path, see write_sessions)
    # ------------------------------------------------------------------
    @staticmethod
    def _hermes_api_base() -> str:
        """Base URL of a running Hermes backend, '' when the API path is off."""
        return (os.environ.get("HERMES_SYNC_HERMES_API") or "").strip().rstrip("/")

    @staticmethod
    def _hermes_api_token() -> str:
        """Optional ``X-Hermes-Session-Token`` (Desktop-owned loopback backends
        require the per-spawn token; a self-started ``hermes serve`` bound to
        loopback does not)."""
        return (os.environ.get("HERMES_SYNC_HERMES_API_TOKEN") or "").strip()

    def _has_session(self, session_id) -> bool:
        """True when this profile's store already holds the (local) id."""
        conn = self._conn(readonly=True)
        try:
            return conn.execute(
                "SELECT 1 FROM %s WHERE id = ?" % self.table_sessions,
                (str(session_id),)).fetchone() is not None
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _import_via_api(self, base: str, profile: str, batch: list[dict],
                        totals: dict) -> list[dict]:
        """Import sessions new to this store through Hermes' own backend.

        Returns the sessions the API did not take (HTTP/validation failure);
        write_sessions falls back to the local SQLite writer for those, so a
        stopped backend or an older Hermes never blocks the pull.
        """
        failed: list[dict] = []
        for chunk in self._import_chunks(batch):
            payload = {
                "request_id": uuid.uuid4().hex,
                "expected_revision": 0,
                "profile": profile or None,
                "sessions": chunk,
            }
            result = self._post_json(base + "/api/sessions/import", payload,
                                     timeout=60)
            if result is None:
                failed.extend(chunk)
                continue
            imported = int(result.get("imported") or 0)
            totals["imported"] += imported
            skipped_ids = result.get("skipped_ids") or []
            failed.extend([s for s in chunk if str(s["id"]) in
                           {str(i) for i in skipped_ids}])
            if result.get("errors"):
                self._log_api(f"import reported errors: {result['errors'][:3]}")
        return failed

    @staticmethod
    def _import_chunks(batch: list[dict]):
        """Bound each import request by session count and payload size."""
        chunk: list[dict] = []
        size = 0
        for s in batch:
            try:
                n = len(json.dumps(s, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError):
                n = 0
            if chunk and (len(chunk) >= 20 or size + n > 4 * 1024 * 1024):
                yield chunk
                chunk, size = [], 0
            chunk.append(s)
            size += n
        if chunk:
            yield chunk

    def _post_json(self, url: str, payload: dict, timeout: float):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json"})
        token = self._hermes_api_token()
        if token:
            req.add_header("X-Hermes-Session-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except OSError:
                pass
            self._log_api(f"POST {url} failed: HTTP {e.code} {detail}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._log_api(f"POST {url} failed: {e}")
        return None

    @staticmethod
    def _log_api(message: str):
        # stderr only: stdout is the MCP JSON-RPC channel.
        try:
            import sys
            sys.stderr.write(f"[hermes-sync] hermes api: {message}\n")
            sys.stderr.flush()
        except Exception:
            pass

    def _log_profile_skip(self, n: int):
        # stderr only: stdout is the MCP JSON-RPC channel and must never
        # carry free-form text (would corrupt the protocol stream).
        try:
            import sys
            sys.stderr.write(f"[hermes-sync] skipped {n} session(s) from "
                             f"profiles/agents not present on this machine\n")
            sys.stderr.flush()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # projects (per-profile projects.db)
    # ------------------------------------------------------------------
    def _project_dbs(self) -> list[tuple[str, Path]]:
        """[(profile_name, projects.db path)] for every profile on the machine."""
        out: list[tuple[str, Path]] = []
        root = self._platform_root()
        default_db = root / "projects.db"
        if default_db.exists():
            out.append(("", default_db))
        pdir = root / self.profiles_dir
        if pdir.is_dir():
            for sub in sorted(pdir.iterdir()):
                if sub.is_dir():
                    db = sub / "projects.db"
                    if db.exists():
                        out.append((sub.name, db))
        return out

    def read_projects(self) -> list[dict]:
        """Push view: projects + folders from EVERY profile, merged.

        ids are bare; the hermes profile travels in the ``profile`` field.
        """
        if not self._aggregate:
            return self._read_projects_from(self._home, "")
        merged: list[dict] = []
        for name, db in self._project_dbs():
            merged.extend(self._read_projects_from(db.parent, name))
        merged.sort(key=lambda p: p.get("created_at") or 0, reverse=True)
        return merged

    def _read_projects_from(self, home: Path, profile: str) -> list[dict]:
        """Read projects.db under ``home``; ids stay bare, profile is set
        as a field (legacy prefixed ids are stripped so pre-migration rows
        round-trip under the new scheme)."""
        import sqlite3
        db = home / "projects.db"
        if not db.exists():
            return []
        prefix = f"{profile}:" if profile else ""
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""SELECT * FROM projects ORDER BY created_at""")
            out = []
            for row in c.fetchall():
                d = dict(row)
                pid = str(d["id"])
                d["id"] = pid[len(prefix):] if prefix and pid.startswith(prefix) else pid
                d["profile"] = profile
                c.execute("""SELECT path, label, is_primary, added_at
                             FROM project_folders WHERE project_id = ?""",
                          (row["id"],))
                d["folders"] = [dict(r) for r in c.fetchall()]
                out.append(d)
            conn.close()
            return out
        except sqlite3.Error:
            return []

    def write_projects(self, projects: list[dict], remaps: list[dict] | None = None) -> dict:
        """Pull view: route each canonical project to its profile's projects.db
        (``profile`` field; legacy id-prefix fallback).  Applies server-side
        remap records (old_id -> new_id) so a project that was merged into
        another on the server converges locally."""
        if not self._aggregate:
            self._write_projects_to(self._home, "", projects, remaps or [])
            return {"imported": len(projects)}
        route: dict[str, Path] = {}
        for name, db in self._project_dbs():
            route[name] = db.parent
        # also allow creating a profile dir if a project for it arrives
        default_home = self._platform_root()
        import sqlite3
        for p in projects:
            profile = self._project_profile(p)
            home = route.get(profile)
            if home is None:
                # no local projects.db for this profile: create the dir
                home = default_home / self.profiles_dir / profile if profile \
                    else default_home
                home.mkdir(parents=True, exist_ok=True)
                route[profile] = home
        # group by profile
        by_home: dict[Path, list[dict]] = {}
        for p in projects:
            profile = self._project_profile(p)
            by_home.setdefault(route[profile], []).append(p)
        total = 0
        for home, plist in by_home.items():
            total += self._write_projects_to(home, self._project_profile(plist[0]),
                                             plist, remaps or [])
        return {"imported": total}

    @staticmethod
    def _project_profile(p: dict) -> str:
        """Profile for a canonical project: the explicit ``profile`` field,
        else the legacy id prefix ('' for bare/default ids)."""
        profile = p.get("profile") or ""
        if profile:
            return profile
        cid = str(p.get("id", ""))
        if ":" in cid:
            pfx = cid.split(":", 1)[0]
            if pfx != "default":
                return pfx
        return ""

    def _write_projects_to(self, home: Path, profile: str, projects: list[dict],
                           remaps: list[dict]) -> int:
        """Write canonical projects (ids possibly prefixed) into ``home``'s
        projects.db. Strips the prefix, applies remaps, upserts rows."""
        import sqlite3
        db = home / "projects.db"
        prefix = f"{profile}:" if profile else ""
        conn = sqlite3.connect(str(db))
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            description TEXT, icon TEXT, color TEXT, board_slug TEXT,
            primary_path TEXT, created_at INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS project_folders (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            path TEXT NOT NULL, label TEXT, is_primary INTEGER NOT NULL DEFAULT 0,
            added_at INTEGER NOT NULL, PRIMARY KEY (project_id, path))""")
        # slug uniqueness helper (mirror hermes _unique_slug)
        def unique_slug(base: str) -> str:
            cand, n = base, 1
            while c.execute("SELECT 1 FROM projects WHERE slug = ?", (cand,)).fetchone():
                n += 1
                suffix = f"-{n}"
                cand = base[: 64 - len(suffix)].rstrip("-_") + suffix
            return cand

        # build remap: old canonical id -> new canonical id
        remap = {}
        for r in remaps or []:
            old, new = str(r.get("old_id", "")), str(r.get("new_id", ""))
            if old and new:
                remap[old] = new
        imported = 0
        try:
            for p in projects:
                raw_id = str(p["id"])
                if raw_id in remap:
                    raw_id = remap[raw_id]
                # ids are bare under the new scheme; strip a legacy
                # ``<profile>:`` prefix if the payload still carries one
                local_id = raw_id[len(prefix):] if prefix and raw_id.startswith(prefix) else raw_id
                slug = p.get("slug") or p["name"]
                # if target slug exists under a DIFFERENT id, rename this one
                row = c.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
                if row and row[0] != local_id:
                    slug = unique_slug(slug)
                c.execute("""INSERT INTO projects
                             (id, slug, name, description, icon, color, board_slug,
                              primary_path, created_at, archived)
                             VALUES (?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(id) DO UPDATE SET
                               slug=excluded.slug, name=excluded.name,
                               description=excluded.description, icon=excluded.icon,
                               color=excluded.color, board_slug=excluded.board_slug,
                               primary_path=excluded.primary_path,
                               archived=excluded.archived""",
                          (local_id, slug, p["name"], p.get("description"),
                           p.get("icon"), p.get("color"), p.get("board_slug"),
                           p.get("primary_path"), int(p.get("created_at") or 0),
                           int(p.get("archived") or 0)))
                # folders: incremental merge (add new, update existing)
                for f in p.get("folders", []):
                    c.execute("""INSERT INTO project_folders
                                 (project_id, path, label, is_primary, added_at)
                                 VALUES (?,?,?,?,?)
                                 ON CONFLICT(project_id, path) DO UPDATE SET
                                   label=excluded.label, is_primary=excluded.is_primary""",
                              (local_id, f.get("path"), f.get("label"),
                               int(f.get("is_primary") or 0), int(f.get("added_at") or 0)))
                imported += 1
            conn.commit()
        except sqlite3.Error as e:
            conn.rollback()
            try:
                import sys
                sys.stderr.write(f"[hermes-sync] projects write error: {e}\n")
                sys.stderr.flush()
            except Exception:
                pass
        finally:
            conn.close()
        return imported

    def last_synced_at(self) -> float:
        """Sync watermark from a sidecar next to state.db (Hermes 0.20's
        sessions table has no last_synced_at column, so we cannot store the
        watermark in the table itself)."""
        return super().last_synced_at()

    def _watermark_file(self) -> Path | None:
        return self._home / ".hermes-sync-watermark"

    def _foreign_ids_file(self) -> Path | None:
        """Sidecar remembering owner agents of pulled foreign sessions.

        hermes' state.db has no agent_type column, so sessions pulled from
        the shared pool (workbuddy/codex/...) would lose their owner and be
        re-pushed tagged as hermes. The registry lives next to the default
        profile's state.db (foreign sessions always land in the default
        profile), so both the aggregating adapter and the default-profile
        sub-adapter see the same file.
        """
        return self._home / ".hermes-sync-foreign.json"


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = HermesAdapter


if __name__ == "__main__":
    a = HermesAdapter()
    print("root:", a._platform_root())
    print("profiles:", [(n, str(d)) for n, d in a._profile_dbs()])
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions(limit=5)))
