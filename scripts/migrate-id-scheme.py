#!/usr/bin/env python3
"""Migrate the id scheme: prefixed canonical ids -> bare ids + columns.

The id-scheme upgrade stores every session/message id bare and carries
agent attribution in ``sessions.agent_type`` / ``messages.agent_type`` and
hermes profiles in ``sessions.profile_name`` (projects: ``projects.profile``).
Legacy ids (``codex:<uuid>``, ``magic:<bare>``, ``workbuddy:<uuid>``, ...)
are split into columns and the prefix removed.

Collisions (the same bare id already existing in a workspace under a
different namespace) are reported and SKIPPED — the operator resolves them
manually; nothing is silently merged or overwritten.

Usage:
    python scripts/migrate-id-scheme.py [--dsn postgresql://...] [--workspace N] [--apply]

Dry-run by default (reports what would change). Pass --apply to write.
"""
import argparse
import os
import sys

# prefix -> agent (mirrors the server's inbound shim in server/sync.py)
AGENT_ID_PREFIXES = {
    "codex:": "codex", "opencode:": "opencode", "reasonix:": "reasonix",
    "openclaw:": "openclaw", "workbuddy:": "workbuddy",
}


def split_inbound_id(cid: str):
    """-> (agent_type|None, profile_name|None, bare_id)"""
    if ":" not in cid:
        return None, None, cid
    prefix, bare = cid.split(":", 1)
    agent = AGENT_ID_PREFIXES.get(prefix + ":")
    if agent:
        return agent, None, bare
    if prefix == "default":
        return "hermes", "", bare
    return "hermes", prefix, bare


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

    # ---- sessions ----
    cur.execute(f"SELECT workspace_id, id FROM sessions "
                f"WHERE id LIKE '%%:%%' {wid_clause} ORDER BY workspace_id, id",
                wid_params)
    prefixed = cur.fetchall()
    changed = skipped_collision = 0
    problems = []
    for wid, cid in prefixed:
        agent, profile, bare = split_inbound_id(cid)
        # collision check: a DIFFERENT row already holds the bare id
        cur.execute("SELECT id FROM sessions WHERE workspace_id=%s AND id=%s",
                    (wid, bare))
        clash = cur.fetchone()
        if clash and clash[0] != cid:
            skipped_collision += 1
            problems.append(f"  workspace {wid}: {cid!r} collides with existing {bare!r} - SKIPPED")
            continue
        if args.apply:
            cur.execute("""UPDATE sessions SET id=%s,
                           agent_type=COALESCE(NULLIF(%s,''), agent_type),
                           profile_name=%s
                           WHERE workspace_id=%s AND id=%s""",
                        (bare, agent, profile or "", wid, cid))
            cur.execute("UPDATE messages SET session_id=%s "
                        "WHERE workspace_id=%s AND session_id=%s",
                        (bare, wid, cid))
        changed += 1
        print(f"  ws {wid}: {cid!r} -> {bare!r} "
              f"(agent={agent or '?'}, profile={profile or ''!r})")
    print(f"sessions: {changed} to migrate, {skipped_collision} collision(s)")

    # ---- projects ----
    cur.execute(f"SELECT workspace_id, id FROM projects "
                f"WHERE id LIKE '%%:%%' {wid_clause} ORDER BY workspace_id, id",
                wid_params)
    prefixed_p = cur.fetchall()
    p_changed = 0
    for wid, pid in prefixed_p:
        agent, profile, bare = split_inbound_id(pid)
        if args.apply:
            cur.execute("UPDATE projects SET id=%s, profile=%s "
                        "WHERE workspace_id=%s AND id=%s",
                        (bare, profile or "", wid, pid))
            cur.execute("UPDATE project_folders SET project_id=%s "
                        "WHERE workspace_id=%s AND project_id=%s",
                        (bare, wid, pid))
            cur.execute("UPDATE project_remap SET old_id=%s "
                        "WHERE workspace_id=%s AND old_id=%s",
                        (bare, wid, pid))
        p_changed += 1
        print(f"  ws {wid}: project {pid!r} -> {bare!r} (profile={profile or ''!r})")
    print(f"projects: {p_changed} to migrate")

    # ---- project_remap new_id side (old_id handled above when the row
    # matched a prefixed project; normalize any remaining prefixed refs) ----
    cur.execute(f"SELECT workspace_id, old_id, new_id FROM project_remap "
                f"WHERE new_id LIKE '%%:%%' {wid_clause}",
                wid_params)
    remap_new = cur.fetchall()
    for wid, old, new in remap_new:
        _, _, bare = split_inbound_id(new)
        if args.apply:
            cur.execute("UPDATE project_remap SET new_id=%s "
                        "WHERE workspace_id=%s AND old_id=%s", (bare, wid, old))
    print(f"project_remap new_id refs: {len(remap_new)} to migrate")

    # ---- project_remap old_id side: merged projects were DELETED from the
    # projects table, so their remap rows never matched the projects loop ----
    cur.execute(f"SELECT workspace_id, old_id FROM project_remap "
                f"WHERE old_id LIKE '%%:%%' {wid_clause}",
                wid_params)
    remap_old = cur.fetchall()
    for wid, old in remap_old:
        _, _, bare = split_inbound_id(old)
        if args.apply:
            cur.execute("UPDATE project_remap SET old_id=%s "
                        "WHERE workspace_id=%s AND old_id=%s", (bare, wid, old))
    print(f"project_remap old_id refs: {len(remap_old)} to migrate")

    if problems:
        print("COLLISIONS (resolve manually, then re-run):")
        print("\n".join(problems))

    if args.apply:
        pg.commit()
        print("APPLIED.")
    else:
        pg.rollback()
        print("dry-run only - re-run with --apply to write.")
    pg.close()


if __name__ == "__main__":
    main()
