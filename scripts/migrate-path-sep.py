#!/usr/bin/env python3
"""Migrate path separators to forward slashes + dedupe separator duplicates.

Windows clients report native backslash paths (``E:\\a\\b``); the server
canonicalizes stored paths to ``/`` (sessions.cwd, sessions.git_repo_root,
projects.primary_path, project_folders.path). Historical rows may also hold
the SAME logical path twice -- once with ``/`` and once with ``\\`` because a
pre-normalization foldere path is a PK component and both spellings were
stored (project_folders pk contains `path`). This pass:

  1. dedupes same-project folder rows that differ only by separator,
     keeping one row (prefers is_primary=1, then earliest added_at) so the
     later REPLACE never hits a PK conflict;
  2. rewrites remaining backslashes to ``/`` (the four path columns);
  3. merges (workspace, profile) same-name visible projects (records a
     project_remap, unions folders) per the server's merge semantics.

Dry-run by default (reports what would change). Pass --apply to write.
Idempotent: a second dry-run reports 0 remaining.

Usage:
    python scripts/migrate-path-sep.py [--dsn postgresql://...] [--apply]
"""
import argparse
import os
import sys

# "contains a backslash" predicate; chr(92) = backslash, avoids SQL
# string-escaping pitfalls with '\' and LIKE wildcard '%' handling.
_BACKSLASH = "chr(92)"


def _dedupe_folders(cur, apply):
    """Merge same-project folder rows that differ only by separator.

    Returns (groups, rows_to_delete). Prefers keeping the row with
    is_primary=1, else the earliest added_at.
    """
    cur.execute(f"""
        SELECT workspace_id, project_id, replace(path, {_BACKSLASH}, '/') AS norm,
               array_agg(path ORDER BY (is_primary=1)::int DESC, added_at, path) AS paths
        FROM project_folders
        GROUP BY workspace_id, project_id, replace(path, {_BACKSLASH}, '/')
        HAVING COUNT(*) > 1""")
    groups = cur.fetchall()
    deleted = 0
    for wid, pid, norm, paths in groups:
        keep = paths[0]  # prefers is_primary=1, then earliest
        for p in paths[1:]:
            if apply:
                cur.execute("""DELETE FROM project_folders
                               WHERE workspace_id=%s AND project_id=%s AND path=%s""",
                            (wid, pid, p))
            deleted += 1
            print(f"    dedupe folder: ws={wid} proj={pid} keep={keep!r} drop={p!r}")
    return len(groups), deleted


def _merge_same_name_projects(cur, apply):
    """Merge (workspace, profile) same-name visible projects: keep earliest,
    union folders (deduped), record remap, delete the merged project."""
    # candidate groups of >=2 same-name, same (workspace, profile) visible projects
    cur.execute("""
        SELECT workspace_id, COALESCE(profile,''), name, COUNT(*) AS cnt
        FROM projects
        WHERE COALESCE(hidden,0)=0 AND name IS NOT NULL AND name<>''
        GROUP BY workspace_id, COALESCE(profile,''), name
        HAVING COUNT(*) > 1""")
    groups = cur.fetchall()
    merged = 0
    for wid, profile, name, _ in groups:
        cur.execute("""SELECT id FROM projects
                       WHERE workspace_id=%s AND COALESCE(profile,'')=%s AND name=%s
                         AND COALESCE(hidden,0)=0
                       ORDER BY COALESCE(created_at,0) ASC, id ASC""",
                    (wid, profile, name))
        ids = [r[0] for r in cur.fetchall()]
        keep = ids[0]
        for drop_id in ids[1:]:
            if apply:
                # union the dropped project's folders into the keep (dedup by path)
                cur.execute("""INSERT INTO project_folders
                               (workspace_id, project_id, path, label, is_primary, added_at)
                               SELECT %s, %s, path, label, is_primary, added_at
                               FROM project_folders
                               WHERE workspace_id=%s AND project_id=%s
                               ON CONFLICT (workspace_id, project_id, path) DO NOTHING""",
                            (wid, keep, wid, drop_id))
                cur.execute("""INSERT INTO project_remap (workspace_id, old_id, new_id)
                               VALUES (%s,%s,%s)
                               ON CONFLICT (workspace_id, old_id)
                               DO UPDATE SET new_id = EXCLUDED.new_id""",
                            (wid, drop_id, keep))
                cur.execute("DELETE FROM projects WHERE workspace_id=%s AND id=%s",
                            (wid, drop_id))
                cur.execute("DELETE FROM project_folders WHERE workspace_id=%s AND project_id=%s",
                            (wid, drop_id))
            merged += 1
            print(f"    merge project: ws={wid} name={name!r} keep={keep!r} drop={drop_id!r}")
    return len(groups), merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("HERMES_SYNC_PG_DSN"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if not args.dsn:
        sys.exit("missing --dsn (or set HERMES_SYNC_PG_DSN)")

    import psycopg2
    pg = psycopg2.connect(args.dsn)
    pg.autocommit = False
    cur = pg.cursor()

    # ---- Step 1: dedupe same-project separator-duplicate folders ----
    print("== Step 1: dedupe separator-duplicate project folders ==")
    g1, del1 = _dedupe_folders(cur, args.apply)
    print(f"  {g1} duplicate group(s), {del1} row(s) to delete "
          f"{'(deleted)' if args.apply else '(in dry-run)'}")

    # ---- Step 2: rewrite backslashes to forward slashes ----
    print("== Step 2: rewrite backslashes to '/' in path columns ==")
    total = 0
    path_cols = [
        ("sessions", "cwd"),
        ("sessions", "git_repo_root"),
        ("projects", "primary_path"),
        ("project_folders", "path"),
    ]
    for table, col in path_cols:
        cur.execute(f"SELECT COUNT(*) FROM {table} "
                    f"WHERE strpos({col}, {_BACKSLASH}) > 0")
        n = cur.fetchone()[0]
        if n:
            cur.execute(f"SELECT DISTINCT {col} FROM {table} "
                        f"WHERE strpos({col}, {_BACKSLASH}) > 0 LIMIT 3")
            samples = [r[0] for r in cur.fetchall()]
            print(f"{table}.{col}: {n} row(s) contain backslash")
            for s in samples:
                print(f"    e.g. {s!r}")
        if args.apply and n:
            cur.execute(f"UPDATE {table} SET {col} = "
                        f"REPLACE({col}, {_BACKSLASH}, '/') "
                        f"WHERE strpos({col}, {_BACKSLASH}) > 0")
        total += n

    # ---- Step 3: merge same-name projects ----
    print("== Step 3: merge same-name projects (per workspace+profile) ==")
    g3, m3 = _merge_same_name_projects(cur, args.apply)
    print(f"  {g3} same-name group(s), {m3} project merge(s) "
          f"{'(merged)' if args.apply else '(in dry-run)'}")

    if args.apply:
        pg.commit()
        print(f"\ndone: {total} path row(s) migrated, {del1} folder dup(s) "
              f"deleted, {m3} project(s) merged")
    else:
        pg.rollback()
        print(f"\ndone (dry-run): {total} path row(s), {del1} folder dup(s), "
              f"{m3} project merge(s) WOULD change")
    pg.close()


if __name__ == "__main__":
    main()
