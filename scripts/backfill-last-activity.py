#!/usr/bin/env python3
"""Backfill ``sessions.last_activity_at`` from the stored messages.

``last_activity_at`` (server release 2026.09.13.3) is DERIVED by the server on
every push -- the newest message in the payload wins, and clients only consume
it (agents disagree on ``ended_at``: measured 2026-09-13 over 40 pulled
sessions, missing in 5 and equal to the newest message time in only 3). Rows
touched by any client after the release therefore fill in by themselves; this
one-off fills the ones that are never re-pushed, from the messages the server
already holds. Without it those rows stay NULL and the clients that display
"last activity" (Hermes, WorkBuddy, omp, OpenClaw) show nothing for them.

Semantics match the Web, which computes ``MAX(m.timestamp)`` over VISIBLE
messages for display: sessions whose messages are all hidden, or which have no
messages, keep NULL (nothing to show).

Usage:
    python scripts/backfill-last-activity.py [--dsn postgresql://...] \\
        [--workspace N] [--apply]

Dry-run by default (reports what would change). Idempotent: a second run
changes nothing.
"""
import argparse
import os
import sys

# the derived value: newest VISIBLE message per session
_SUBQ = """
    (SELECT workspace_id, session_id, MAX(timestamp) AS mx
       FROM messages
      WHERE COALESCE(hidden, 0) = 0 AND timestamp IS NOT NULL
      GROUP BY workspace_id, session_id)"""

# rows are only backfilled when the derived value actually differs, so a
# re-run is a no-op and a client-pushed value that already matches stays put.
# SELECT form joins the subquery onto the aliased sessions table; the UPDATE
# form must NOT repeat `sessions` in FROM (PostgreSQL: "table name specified
# more than once"), so the target is constrained in WHERE instead.
_SELECT_FROM = f"""
    FROM sessions s
    JOIN {_SUBQ} x
      ON x.workspace_id = s.workspace_id AND x.session_id = s.id
   WHERE s.last_activity_at IS DISTINCT FROM x.mx
"""
_UPDATE_TAIL = f"""
    FROM {_SUBQ} x
   WHERE x.workspace_id = s.workspace_id AND x.session_id = s.id
     AND s.last_activity_at IS DISTINCT FROM x.mx
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("HERMES_SYNC_PG_DSN"),
                    help="PostgreSQL DSN (default: $HERMES_SYNC_PG_DSN)")
    ap.add_argument("--workspace", type=int, default=None,
                    help="only this workspace (default: all)")
    ap.add_argument("--apply", action="store_true",
                    help="write the backfill (default: dry-run)")
    args = ap.parse_args()
    if not args.dsn:
        sys.exit("missing --dsn (or set HERMES_SYNC_PG_DSN)")

    import psycopg2
    pg = psycopg2.connect(args.dsn)
    cur = pg.cursor()
    wid_clause = "AND s.workspace_id = %s" if args.workspace else ""
    wid_params = (args.workspace,) if args.workspace else ()

    cur.execute(f"""SELECT COUNT(*) {_SELECT_FROM} {wid_clause}""", wid_params)
    todo = cur.fetchone()[0]
    cur.execute("""SELECT COUNT(*) FROM sessions s
                   WHERE s.last_activity_at IS NULL""")
    missing = cur.fetchone()[0]
    print(f"sessions with NULL last_activity_at: {missing}")
    print(f"rows this backfill would change: {todo}")

    if not todo:
        print("nothing to do")
        pg.close()
        return

    cur.execute(f"""SELECT s.workspace_id, s.id, s.last_activity_at, x.mx {_SELECT_FROM}
                    {wid_clause} ORDER BY x.mx DESC LIMIT 5""", wid_params)
    print("newest rows:")
    for wid, sid, cur_val, mx in cur.fetchall():
        print(f"  ws={wid} {sid} {cur_val} -> {mx}")

    if not args.apply:
        print("\nDRY RUN (no writes). Add --apply to backfill.")
        pg.close()
        return

    cur.execute(f"""UPDATE sessions s SET last_activity_at = x.mx
                    {_UPDATE_TAIL} {wid_clause}""", wid_params)
    changed = cur.rowcount
    pg.commit()
    cur.execute("""SELECT COUNT(*) FROM sessions s
                   WHERE s.last_activity_at IS NOT NULL""")
    print(f"\nbackfilled {changed} row(s); "
          f"{cur.fetchone()[0]} session(s) now carry a last-activity time")
    pg.close()


if __name__ == "__main__":
    main()
