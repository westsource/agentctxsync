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

import os
from pathlib import Path

from .base import SQLiteAdapter


class HermesAdapter(SQLiteAdapter):
    """Hermes multi-profile state.db adapter (1:1 column mapping)."""

    agent_type = "hermes"

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
        s = dict(local_session)
        lid = str(s["id"])
        prefix = self._id_prefix()
        # NOTE: _is_foreign() is always False for hermes (no foreign-ids
        # sidecar is defined), kept for symmetry with the base contract.
        if self._is_foreign(lid):
            s["id"] = lid
            s["messages"] = [dict(m) for m in s.get("messages", [])]
            for m in s["messages"]:
                m["session_id"] = lid
            return s
        if prefix:
            s["id"] = f"{prefix}{lid}"
            s["messages"] = [dict(m) for m in s.get("messages", [])]
            for m in s["messages"]:
                m["session_id"] = f"{prefix}{m.get('session_id', lid)}"
            return s
        return super().canonicalize(local_session)

    def localize(self, canonical_session: dict, strict: bool = True) -> dict:
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
    def read_sessions(self, limit: int | None = None) -> list[dict]:
        """Push view: sessions from EVERY profile on the machine, merged.

        default sessions keep bare ids; named-profile sessions carry their
        ``<profile>:`` prefix so the server never confuses profiles.
        """
        if not self._aggregate:
            # single-db mode: canonicalize each session (bare -> prefix)
            return [self.canonicalize(s) for s in super().read_sessions(limit=limit)]
        merged: list[dict] = []
        for name, db in self._profile_dbs():
            sub = self._sub_adapter(name, db)
            # sub.read_sessions() already canonicalizes (prefixes) its rows
            merged.extend(sub.read_sessions(limit=limit))
        merged.sort(key=lambda s: s.get("started_at") or 0, reverse=True)
        return merged

    def write_sessions(self, sessions: list[dict]) -> dict:
        """Pull view: route each canonical session to the profile that owns
        its id prefix.  Sessions whose profile does not exist locally (other
        machines' profiles) are skipped; other agents are never touched.
        """
        if not self._aggregate:
            return super().write_sessions(sessions)
        # build prefix -> sub-adapter map
        route: dict[str, HermesAdapter] = {}
        for name, db in self._profile_dbs():
            route[f"{name}:"] = self._sub_adapter(name, db)
        totals = {"imported": 0, "updated": 0, "new_messages": 0, "duplicates": 0}
        skipped = 0
        for s in sessions:
            cid = str(s["id"])
            # default (bare) ids are the legacy hermes namespace
            target = route.get(":")  # default profile: '' prefix -> ':'
            if ":" in cid:
                target = route.get(cid.split(":", 1)[0] + ":")
            if target is None:
                skipped += 1
                continue
            stats = target.write_sessions([s])
            for k in totals:
                totals[k] += stats.get(k, 0)
        if skipped:
            self._log_profile_skip(skipped)
        return totals

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
        """Push view: projects + folders from EVERY profile, prefixed."""
        if not self._aggregate:
            return self._read_projects_from(self._home, "")
        merged: list[dict] = []
        for name, db in self._project_dbs():
            merged.extend(self._read_projects_from(db.parent, name))
        merged.sort(key=lambda p: p.get("created_at") or 0, reverse=True)
        return merged

    def _read_projects_from(self, home: Path, profile: str) -> list[dict]:
        """Read projects.db under ``home``; id prefixed with ``<profile>:``
        when profile is non-empty (default stays bare)."""
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
                d["id"] = f"{prefix}{d['id']}"
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
        """Pull view: route each canonical project to its profile's projects.db.
        Applies server-side remap records (old_id -> new_id) so a project that
        was merged into another on the server converges locally."""
        if not self._aggregate:
            self._write_projects_to(self._home, "", projects, remaps or [])
            return {"imported": len(projects)}
        route: dict[str, Path] = {}
        for name, db in self._project_dbs():
            route[f"{name}:"] = db.parent
        # also allow creating a profile dir if a project for it arrives
        default_home = self._platform_root()
        import sqlite3
        for p in projects:
            cid = str(p["id"])
            profile = cid.split(":", 1)[0] if ":" in cid else ""
            home = route.get(f"{profile}:")
            if home is None:
                # no local projects.db for this profile: create the dir
                home = default_home / self.profiles_dir / profile if profile \
                    else default_home
                home.mkdir(parents=True, exist_ok=True)
                route[f"{profile}:"] = home
        # group by profile
        by_home: dict[Path, list[dict]] = {}
        for p in projects:
            cid = str(p["id"])
            profile = cid.split(":", 1)[0] if ":" in cid else ""
            by_home.setdefault(route[f"{profile}:"], []).append(p)
        total = 0
        for home, plist in by_home.items():
            # derive the profile name from the first project id
            prof = ""
            if plist:
                cid = str(plist[0]["id"])
                prof = cid.split(":", 1)[0] if ":" in cid else ""
            total += self._write_projects_to(home, prof, plist, remaps or [])
        return {"imported": total}

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

        # build remap: old canonical id -> new canonical id (prefix-aware)
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


# registry alias (mcp/adapters/__init__.py looks up ``module.Adapter``)
Adapter = HermesAdapter


if __name__ == "__main__":
    a = HermesAdapter()
    print("root:", a._platform_root())
    print("profiles:", [(n, str(d)) for n, d in a._profile_dbs()])
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions(limit=5)))
