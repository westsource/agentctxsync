"""Project sync domain: projects.db push/pull."""
import json
import psycopg2.extras
from datetime import datetime

from fastapi import APIRouter, Depends, Request

from auth import get_workspace_by_api_key
from db import get_conn, normalize_path_sep

router = APIRouter()

# Scalar project fields that participate in field-level optimistic
# concurrency (mirrors sessions, see ARCHITECTURE.md). Folders stay unioned
# by path (append-safe, multi-device coexist) with per-path LWW -- their
# label/is_primary are effectively constant in practice and path-keyed
# versions would be fragile across separator spellings.
PROJECT_USER_EDIT_FIELDS = frozenset(("name", "primary_path", "archived",
                                      "description"))
# Non-editable scalar fields written last-writer (identity/derived).
PROJECT_PLAIN_FIELDS = ("slug", "icon", "color", "board_slug", "created_at")


def _read_clock(c, wid, pid):
    """(cur_rev, field_rev dict) for a project -- baseline 0 / {} on first
    post-upgrade touch (no history reconstruction needed)."""
    c.execute("SELECT rev, field_rev FROM projects "
              "WHERE id = %s AND workspace_id = %s", (pid, wid))
    row = c.fetchone()
    cur_rev = row[0] if row and row[0] is not None else 0
    fr = row[1] if row and row[1] else {}
    if isinstance(fr, str):
        try:
            fr = json.loads(fr)
        except (ValueError, TypeError):
            fr = {}
    return cur_rev, (dict(fr) if isinstance(fr, dict) else {})
@router.post("/api/projects/push")
async def api_projects_push(request: Request, ws: dict = Depends(get_workspace_by_api_key)):
    """Upsert projects + folders. Same (workspace, profile, slug) with a
    different id merges: keep the earliest id, union folders, record remap."""
    body = await request.json()
    wid = ws["workspace_id"]
    projects = body.get("projects", []) or []
    now = datetime.now().timestamp()
    with get_conn() as conn:
        c = conn.cursor()
        imp = upd = merged = 0
        project_revs: dict = {}
        for p in projects:
            pid = p["id"]
            slug = p.get("slug") or p["name"]
            # canonical path: normalize Windows backslashes to '/' before
            # storing primary_path and folder paths.
            if p.get("primary_path"):
                p["primary_path"] = normalize_path_sep(p["primary_path"])
            for f in p.get("folders", []) or []:
                if f.get("path"):
                    f["path"] = normalize_path_sep(f["path"])
            # profile: explicit payload field (new scheme) or the legacy
            # id prefix (<profile>:<p_xxx>; foreign-agent prefixes keep the
            # default profile and are stripped from the stored id)
            profile = p.get("profile") or ""
            if ":" in pid:
                pfx, bare = pid.split(":", 1)
                if pfx not in ("codex", "opencode", "reasonix", "openclaw",
                               "workbuddy") and pfx != "default":
                    profile = profile or pfx
                pid = bare
            # find existing same (workspace, profile, slug); the legacy id
            # prefix branch only matches unmigrated rows
            c.execute("""SELECT id FROM projects
                         WHERE workspace_id = %s AND agent_type = %s
                           AND slug = %s
                           AND (COALESCE(profile,'') = %s
                                OR (COALESCE(profile,'') = '' AND %s = ''
                                    AND CASE WHEN id LIKE '%%:%%' THEN split_part(id,':',1) ELSE '' END = %s))
                         ORDER BY created_at ASC, id ASC LIMIT 1""",
                      (wid, "hermes", slug, profile, profile, profile))
            row = c.fetchone()
            if row and row[0] != pid:
                # merge into the existing (earliest) project
                keep = row[0]
                # union folders from the incoming project
                for f in p.get("folders", []):
                    c.execute("""INSERT INTO project_folders
                                 (workspace_id, project_id, path, label, is_primary, added_at)
                                 VALUES (%s,%s,%s,%s,%s,%s)
                                 ON CONFLICT (workspace_id, project_id, path) DO NOTHING""",
                              (wid, keep, f.get("path"), f.get("label"),
                               f.get("is_primary") or 0, f.get("added_at") or now))
                # record remap (idempotent) and drop the merged row
                c.execute("""INSERT INTO project_remap (workspace_id, old_id, new_id)
                             VALUES (%s,%s,%s)
                             ON CONFLICT (workspace_id, old_id) DO UPDATE SET new_id = EXCLUDED.new_id""",
                          (wid, pid, keep))
                c.execute("DELETE FROM projects WHERE workspace_id = %s AND id = %s", (wid, pid))
                c.execute("DELETE FROM project_folders WHERE workspace_id = %s AND project_id = %s", (wid, pid))
                merged += 1
                continue
            # field_meta: {field -> base_rev|None} for scalar user-edit fields
            # this client asserts. Its PRESENCE marks a "new client" (an
            # unasserted user-edit field keeps the server value); its ABSENCE
            # marks a legacy client -> whole project uses legacy overwrite.
            _fm = p.get("field_meta")
            is_new_client = _fm is not None
            field_meta = _fm if isinstance(_fm, dict) else {}
            if row:
                cur_rev, fr = _read_clock(c, wid, pid)
                new_fr = dict(fr)
                # scalar user-edit fields: accept only asserted (base known);
                # base None / unasserted -> server stays authoritative
                sd = {k: p[k] for k in PROJECT_PLAIN_FIELDS if p.get(k) is not None}
                for k in PROJECT_USER_EDIT_FIELDS:
                    v = p.get(k)
                    if v is None:
                        continue
                    if is_new_client:
                        if k not in field_meta:
                            # not asserting this field -> keep the server value
                            continue
                        if field_meta.get(k) is None:
                            # base unknown: accept only as first new-scheme
                            # write (seed when un-versioned); otherwise this
                            # client is stale -> keep the server value
                            # (avoids the bootstrap deadlock where field_rev
                            # could never leave 0).
                            if new_fr.get(k, 0) > 0:
                                continue
                    sd[k] = v
                    cur_rev += 1
                    new_fr[k] = cur_rev
                sd["rev"] = cur_rev
                sd["field_rev"] = json.dumps(new_fr, ensure_ascii=False)
                set_cl = ", ".join(f"{k} = %s" for k in sd)
                c.execute(f"UPDATE projects SET {set_cl} WHERE id = %s AND workspace_id = %s",
                          list(sd.values()) + [pid, wid])
                upd += 1
                project_revs[pid] = {"rev": cur_rev, "field_rev": new_fr}
            else:
                cols_all = ["id","workspace_id","slug","name","created_at","archived","agent_type","profile"]
                vals = [pid, wid, slug, p["name"], p.get("created_at") or now, p.get("archived") or 0, "hermes", profile]
                for k in ("description","icon","color","board_slug","primary_path"):
                    if p.get(k) is not None:
                        cols_all.append(k); vals.append(p[k])
                # brand-new project: no conflict; seed the logical clock so
                # the creator (and everyone else) can anchor on the next pull.
                sd_present = {k: p.get(k) for k in PROJECT_USER_EDIT_FIELDS if p.get(k) is not None}
                cols_all += ["rev", "field_rev"]
                vals += [1, json.dumps({k: 1 for k in sd_present}, ensure_ascii=False)]
                ph = ", ".join(["%s"] * len(vals))
                c.execute(f"INSERT INTO projects ({', '.join(cols_all)}) VALUES ({ph}) "
                          "ON CONFLICT (workspace_id, id) DO UPDATE SET "
                          + ", ".join(f"{k} = EXCLUDED.{k}" for k in cols_all if k not in ("id","workspace_id")),
                          vals)
                imp += 1
                project_revs[pid] = {"rev": 1, "field_rev": {k: 1 for k in sd_present}}
            # upsert folders incrementally: new paths are added, existing
            # ones updated (is_primary/label) — multi-device edits coexist
            # instead of last-writer-wins replacing the whole set.
            for f in p.get("folders", []):
                c.execute("""INSERT INTO project_folders
                             (workspace_id, project_id, path, label, is_primary, added_at)
                             VALUES (%s,%s,%s,%s,%s,%s)
                             ON CONFLICT (workspace_id, project_id, path) DO UPDATE SET
                               label = EXCLUDED.label,
                               is_primary = EXCLUDED.is_primary""",
                          (wid, pid, f.get("path"), f.get("label"),
                           f.get("is_primary") or 0, f.get("added_at") or now))
        conn.commit()
    return {"imported": imp, "updated": upd, "merged": merged,
            "project_revs": project_revs}

@router.post("/api/projects/pull")
async def api_projects_pull(request: Request, ws: dict = Depends(get_workspace_by_api_key)):
    """Return visible projects + folders and pending remap records."""
    wid = ws["workspace_id"]
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("""SELECT * FROM projects
                     WHERE workspace_id = %s AND COALESCE(hidden,0) = 0
                     ORDER BY created_at DESC""", (wid,))
        projects = []
        for row in c.fetchall():
            p = dict(row)
            # field-level concurrency: hand the client the per-field logical
            # clock so it can anchor base on pull (JSONB comes back as text).
            _fr = p.get("field_rev")
            if isinstance(_fr, str):
                try:
                    p["field_rev"] = json.loads(_fr)
                except (ValueError, TypeError):
                    p["field_rev"] = {}
            elif not isinstance(_fr, dict):
                p["field_rev"] = {}
            c.execute("""SELECT path, label, is_primary, added_at FROM project_folders
                         WHERE workspace_id = %s AND project_id = %s""", (wid, p["id"]))
            p["folders"] = [dict(r) for r in c.fetchall()]
            # canonical path: serve '/' (Windows backslashes migrated away).
            if p.get("primary_path"):
                p["primary_path"] = normalize_path_sep(p["primary_path"])
            for f in p.get("folders", []) or []:
                if f.get("path"):
                    f["path"] = normalize_path_sep(f["path"])
            projects.append(p)
        c.execute("""SELECT old_id, new_id FROM project_remap
                     WHERE workspace_id = %s""", (wid,))
        remaps = [dict(r) for r in c.fetchall()]
    return {"projects": projects, "remaps": remaps}

