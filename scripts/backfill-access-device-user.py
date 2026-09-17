#!/usr/bin/env python3
"""Backfill ``access_device.user_id`` from the device → workspace → user map.

``access_device`` rows record which sync client (device_id) talked through the
domain vs direct IP; user attribution (release 2026.09.17.2) is written live by
the requestlog middleware from the workspace API key the client authenticated
with. Rows written before that column existed carry ``user_id = 0`` and show as
"未归属 / Unattributed" on /web/admin/access/devices.

The server already knows which workspaces synced from each device_id
(``sync_state``), so historical rows can be attributed without touching the
clients. A device_id is a client-declared string, so it can map to several
users (one box running two accounts); those rows are left at 0 rather than
guessed -- a wrong owner is worse than "unattributed". Devices with no
sync_state row (probes, deleted workspaces) stay 0 as well. A device that maps
to several workspaces of the SAME user is unambiguous and is filled.

Usage:
    python scripts/backfill-access-device-user.py [--dsn postgresql://...] \\
        [--device DEVICE_ID] [--apply]

Dry-run by default (reports what would change). Idempotent: a second run
changes nothing, and it never overwrites a user_id that is already set.
"""
import argparse
import os
import sys

# device_id -> the single owning user, or no row at all when the device is
# ambiguous (several users) or unmapped. The HAVING keeps only devices whose
# workspaces all belong to one user.
_MAP = """
    (SELECT ss.device_id, MIN(w.user_id) AS user_id
       FROM sync_state ss
       JOIN workspaces w ON w.id = ss.workspace_id
      WHERE w.user_id IS NOT NULL
      GROUP BY ss.device_id
     HAVING COUNT(DISTINCT w.user_id) = 1)"""

# Only un-attributed rows are touched, so re-runs are no-ops and live-written
# values are never clobbered.
_SELECT_FROM = f"""
    FROM access_device ad
    JOIN {_MAP} m ON m.device_id = ad.device_id
   WHERE ad.user_id = 0 AND m.user_id IS NOT NULL
"""
# The UPDATE form must NOT repeat the target table in FROM (PostgreSQL:
# "table name specified more than once"), so the target is only constrained in
# WHERE -- same trap the last-activity backfill documents.
_UPDATE = f"""
    UPDATE access_device ad SET user_id = m.user_id
      FROM {_MAP} m
     WHERE m.device_id = ad.device_id
       AND ad.user_id = 0 AND m.user_id IS NOT NULL
"""
# Everything still unattributed after the run: ambiguous devices and devices
# with no sync_state row. LEFT JOIN, or the unmapped ones would silently drop
# out of the count.
_STUCK = f"""
    SELECT COUNT(*)
      FROM access_device ad
      LEFT JOIN {_MAP} m ON m.device_id = ad.device_id
     WHERE ad.user_id = 0 AND m.user_id IS NULL
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("HERMES_SYNC_PG_DSN"),
                    help="PostgreSQL DSN (default: $HERMES_SYNC_PG_DSN)")
    ap.add_argument("--device", default=None,
                    help="only this device_id (default: all)")
    ap.add_argument("--apply", action="store_true",
                    help="write the backfill (default: dry-run)")
    args = ap.parse_args()
    if not args.dsn:
        sys.exit("missing --dsn (or set HERMES_SYNC_PG_DSN)")

    import psycopg2
    pg = psycopg2.connect(args.dsn)
    cur = pg.cursor()
    dev_clause = "AND ad.device_id = %s" if args.device else ""
    params = (args.device,) if args.device else ()

    cur.execute(f"SELECT COUNT(*) FROM access_device WHERE user_id = 0")
    unset = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) {_SELECT_FROM} {dev_clause}", params)
    todo = cur.fetchone()[0]
    cur.execute(f"{_STUCK} {dev_clause}", params)
    stuck = cur.fetchone()[0]
    print(f"access_device rows with user_id = 0: {unset}")
    print(f"rows this backfill would change:     {todo}")
    print(f"rows left unattributed (ambiguous or no sync_state row): {stuck}")

    if not todo:
        print("nothing to do")
        pg.close()
        return

    cur.execute(f"""SELECT ad.device_id, COUNT(*) AS rows, MIN(m.user_id) AS user_id
                      {_SELECT_FROM} {dev_clause}
                     GROUP BY ad.device_id, m.user_id
                     ORDER BY rows DESC, ad.device_id LIMIT 10""", params)
    print("devices to attribute (top 10 by rows):")
    for device, rows, uid in cur.fetchall():
        cur2 = pg.cursor()
        cur2.execute("SELECT COALESCE(display_name, username) FROM users WHERE id = %s",
                     (uid,))
        name = (cur2.fetchone() or ["?"])[0]
        print(f"  {device:32} {rows:3} row(s) -> user {uid} ({name})")

    if not args.apply:
        print("\nDRY RUN (no writes). Add --apply to backfill.")
        pg.close()
        return

    cur.execute(f"{_UPDATE} {dev_clause}", params)
    changed = cur.rowcount
    pg.commit()
    cur.execute(f"SELECT COUNT(*) {_SELECT_FROM} {dev_clause}", params)
    print(f"\nbackfilled {changed} row(s); {cur.fetchone()[0]} still pending")
    cur.execute("SELECT COUNT(*) FROM access_device WHERE user_id = 0")
    print(f"access_device rows still unattributed: {cur.fetchone()[0]}")
    pg.close()


if __name__ == "__main__":
    main()
