#!/usr/bin/env python3
"""Hide server sessions that are sub-agent children (parent_session_id set).

Client (mcp/adapters/hermes.py) now folds sub-agent sessions into their
parent at read time, so those child sessions are no longer pushed. This
migration soft-hides (hidden=1) the orphan children already synced to the
server — their messages now live in the parent session (merged), so nothing
is lost. Reversible: unhide the rows if ever needed.

Usage:
    python scripts/migrate-fold-subagents.py [--dsn postgresql://...] [--workspace N] [--apply]

Dry-run by default (reports what would change). Pass --apply to write.
"""
import argparse
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("HERMES_SYNC_PG_DSN"),
                    help="PostgreSQL DSN (default: $HERMES_SYNC_PG_DSN)")
    ap.add_argument("--workspace", type=int, default=None,
                    help="migrate only this workspace (default: all)")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry-run)")
    args = ap.parse_args()
    if not args.dsn:
        sys.exit("missing --dsn (or set HERMES_SYNC_PG_DSN)")

    import psycopg2
    pg = psycopg2.connect(args.dsn)
    cur = pg.cursor()
    wid_clause = "AND workspace_id = %s" if args.workspace else ""
    wid_params = (args.workspace,) if args.workspace else ()

    cur.execute(
        f"SELECT workspace_id, id, title FROM sessions "
        f"WHERE parent_session_id IS NOT NULL "
        f"AND COALESCE(hidden, 0) = 0 {wid_clause} "
        f"ORDER BY workspace_id, id", wid_params)
    rows = cur.fetchall()
    print(f"sub-agent child sessions to hide: {len(rows)}")
    for wid, sid, title in rows[:50]:
        print(f"  ws={wid} id={sid} title={title!r}")
    if len(rows) > 50:
        print(f"  ... and {len(rows) - 50} more")

    if args.apply and rows:
        cur.execute(
            f"UPDATE sessions SET hidden = 1 "
            f"WHERE parent_session_id IS NOT NULL "
            f"AND COALESCE(hidden, 0) = 0 {wid_clause}", wid_params)
        pg.commit()
        print(f"applied: hidden={cur.rowcount}")
    pg.close()
    if not args.apply:
        print("dry-run (no changes written); pass --apply to hide")


if __name__ == "__main__":
    main()
