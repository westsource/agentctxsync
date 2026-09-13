#!/usr/bin/env python3
"""Retire root-shaped projects from the shared project pool.

A project is a set of folders the Web prefix-matches session ``cwd`` against
(``server/workspace.py::_session_for_project_match``). A root-shaped path --
a drive/filesystem root, the user home or an ancestor of it -- is an ancestor
of every session, so such a card swallows its whole subtree and means a
different directory on every machine. Clients no longer push or pull them
(``mcp/adapters/base.py::is_root_project_path``), but rows pushed before that
rule still sit in ``projects`` and keep grouping sessions on the Web.

Retirement is a soft hide (``projects.hidden = 1``): /api/projects/pull and
the Web workspace page both filter ``COALESCE(hidden,0) = 0``, so the card
disappears for every client, while rev/field_rev/remap history is kept and a
client re-pushing the same id cannot resurrect it (the push update path never
writes ``hidden``). ``--undo`` restores the rows.

Usage:
    python scripts/hide-root-projects.py [--dsn postgresql://...] \\
        [--workspace N] [--apply | --undo]

Dry-run by default (reports what would change). Run it where the deployment's
HERMES_SYNC_PG_DSN is available (or pass --dsn).
"""
import argparse
import os
import re
import sys
from datetime import datetime

# Mirror of mcp/adapters/base.py::is_root_project_path. The client package and
# the server deployment do not share an importable module (mcp/ is a namespace
# directory that collides with the installed mcp SDK), so the predicate is
# duplicated here; mcp/tests/test_project_roots.py::ScriptPredicateTest fails
# if the two ever disagree.
_HOME_ANCESTOR_RE = re.compile(r"^(?:[a-z]:)?/(?:users|home)(?:/[^/]+)?$",
                               re.IGNORECASE)
_ROOT_PATH_RE = re.compile(r"^[a-z]:$", re.IGNORECASE)


def is_root_project_path(path: str) -> bool:
    if not isinstance(path, str):
        return False
    k = path.replace("\\", "/").lower().rstrip("/")
    if k == "" or k == "/root":
        return True
    if _ROOT_PATH_RE.match(k):
        return True
    return bool(_HOME_ANCESTOR_RE.match(k))


def _path_key(p: str) -> str:
    return p.replace("\\", "/").lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("HERMES_SYNC_PG_DSN"),
                    help="PostgreSQL DSN (default: $HERMES_SYNC_PG_DSN)")
    ap.add_argument("--workspace", type=int, default=None,
                    help="only this workspace (default: all)")
    ap.add_argument("--apply", action="store_true",
                    help="hide the matching projects (default: dry-run)")
    ap.add_argument("--undo", action="store_true",
                    help="restore previously hidden projects (hidden = 0)")
    args = ap.parse_args()
    if not args.dsn:
        sys.exit("missing --dsn (or set HERMES_SYNC_PG_DSN)")
    if args.apply and args.undo:
        sys.exit("--apply and --undo are mutually exclusive")

    import psycopg2
    pg = psycopg2.connect(args.dsn)
    cur = pg.cursor()
    wid_clause = "AND p.workspace_id = %s" if args.workspace else ""
    wid_params = (args.workspace,) if args.workspace else ()

    cur.execute(f"""SELECT p.workspace_id, p.id, p.name, p.primary_path,
                           COALESCE(p.hidden, 0), p.rev
                    FROM projects p
                    WHERE TRUE {wid_clause}
                    ORDER BY p.workspace_id, p.created_at""", wid_params)
    rows = cur.fetchall()
    folders: dict[tuple, list] = {}
    cur.execute("""SELECT workspace_id, project_id, path FROM project_folders""")
    for wid, pid, path in cur.fetchall():
        folders.setdefault((wid, pid), []).append(path)

    candidates = []
    for wid, pid, name, primary, hidden, rev in rows:
        paths = [x for x in ([primary] + folders.get((wid, pid), [])) if x]
        if paths and all(is_root_project_path(x) for x in paths):
            candidates.append((wid, pid, name, sorted(set(paths)), hidden, rev))

    if args.undo:
        targets = [c for c in candidates if c[4]]
        verb = "restore"
    else:
        targets = [c for c in candidates if not c[4]]
        verb = "hide"

    print(f"projects scanned: {len(rows)} | root-shaped: {len(candidates)} "
          f"| to {verb}: {len(targets)}")
    for wid, pid, name, paths, hidden, rev in candidates:
        mark = "hidden" if hidden else "visible"
        print(f"  ws={wid} {pid:16s} rev={rev} [{mark}] name={name!r} "
              f"paths={paths}")
    if not targets:
        print(f"nothing to {verb}")
        pg.close()
        return

    for wid, pid, name, paths, hidden, rev in targets:
        matched = 0
        for path in paths:
            base = _path_key(path).rstrip("/")
            cur.execute("""SELECT COUNT(*) FROM sessions
                           WHERE workspace_id = %s AND COALESCE(hidden,0) = 0
                             AND cwd IS NOT NULL AND cwd <> ''
                             AND (LOWER(cwd) = %s OR LOWER(cwd) = %s
                                  OR LEFT(LOWER(cwd), %s) = %s
                                  OR LEFT(LOWER(cwd), %s) = %s)""",
                        (wid, path.lower(), base, len(base) + 1, base + "/",
                         len(base) + 1, base + "\\"))
            matched += cur.fetchone()[0]
        print(f"  {verb} ws={wid} {pid} ({name!r}): sessions in its subtree "
              f"= {matched}")

    if not args.apply:
        print("\nDRY RUN (no writes). Add --apply to hide, --undo to restore.")
        pg.close()
        return

    for wid, pid, name, paths, hidden, rev in targets:
        if args.undo:
            cur.execute("""UPDATE projects SET hidden = 0, hidden_at = NULL
                           WHERE workspace_id = %s AND id = %s""", (wid, pid))
        else:
            cur.execute("""UPDATE projects
                           SET hidden = 1, hidden_at = %s
                           WHERE workspace_id = %s AND id = %s""",
                        (datetime.now().timestamp(), wid, pid))
    pg.commit()
    verb_past = "restored" if args.undo else "hidden"
    print(f"\n{verb_past} {len(targets)} project(s). "
          f"/api/projects/pull and the Web project list exclude them; "
          f"re-run with --undo to revert.")
    pg.close()


if __name__ == "__main__":
    main()
