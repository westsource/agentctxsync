"""Project sync domain: projects.db push/pull."""
import psycopg2.extras
from datetime import datetime

from fastapi import APIRouter, Depends, Request

from auth import get_workspace_by_api_key
from db import get_conn

router = APIRouter()
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
        for p in projects:
            pid = p["id"]
            slug = p.get("slug") or p["name"]
            # profile = prefix before ':' ('' for default)
            profile = pid.split(":", 1)[0] if ":" in pid else ""
            # find existing same (workspace, profile, slug)
            c.execute("""SELECT id FROM projects
                         WHERE workspace_id = %s AND agent_type = %s
                           AND slug = %s AND (CASE WHEN id LIKE '%%:%%' THEN split_part(id,':',1) ELSE '' END) = %s
                         ORDER BY created_at ASC, id ASC LIMIT 1""",
                      (wid, "hermes", slug, profile))
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
            # upsert project
            cols = [k for k in ("slug","name","description","icon","color","board_slug",
                                "primary_path","created_at","archived") if p.get(k) is not None]
            if row:
                sets = ", ".join(f"{k} = %s" for k in cols)
                vals = [p[k] for k in cols] + [pid, wid]
                c.execute(f"UPDATE projects SET {sets} WHERE id = %s AND workspace_id = %s", vals)
                upd += 1
            else:
                cols_all = ["id","workspace_id","slug","name","created_at","archived","agent_type"]
                vals = [pid, wid, slug, p["name"], p.get("created_at") or now, p.get("archived") or 0, "hermes"]
                for k in ("description","icon","color","board_slug","primary_path"):
                    if p.get(k) is not None:
                        cols_all.append(k); vals.append(p[k])
                ph = ", ".join(["%s"] * len(vals))
                c.execute(f"INSERT INTO projects ({', '.join(cols_all)}) VALUES ({ph}) "
                          "ON CONFLICT (workspace_id, id) DO UPDATE SET "
                          + ", ".join(f"{k} = EXCLUDED.{k}" for k in cols_all if k not in ("id","workspace_id")),
                          vals)
                imp += 1
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
    return {"imported": imp, "updated": upd, "merged": merged}

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
            c.execute("""SELECT path, label, is_primary, added_at FROM project_folders
                         WHERE workspace_id = %s AND project_id = %s""", (wid, p["id"]))
            p["folders"] = [dict(r) for r in c.fetchall()]
            projects.append(p)
        c.execute("""SELECT old_id, new_id FROM project_remap
                     WHERE workspace_id = %s""", (wid,))
        remaps = [dict(r) for r in c.fetchall()]
    return {"projects": projects, "remaps": remaps}

