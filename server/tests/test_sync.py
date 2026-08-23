"""/push and /pull behavioral tests with a scripted fake DB.

These are the regression net for the sync hot path: they pin down the
observable contract (counters, dedup semantics, agent_type/hidden
protection, id allocation, pull filtering/merging). The fake DB routes
by SQL shape only, so the batch-optimization rewrite keeps the same
assertions.
"""
import asyncio
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import projects  # noqa: E402
import sync  # noqa: E402


class ScriptedCursor:
    """Routes execute() by SQL regex to scripted results; records everything."""

    def __init__(self):
        self.executed = []
        self.rowcount = 0
        self._scripts = []
        self._next = iter(())

    def add(self, pattern, rows=None, rowcount=None, key=None):
        """Route by regex; `key` (callable(params)->lookup key) selects from
        `rows` when given as a {lookup_key: rows} dict."""
        self._scripts.append((re.compile(pattern), rows, rowcount, key))
        return self

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.rowcount = 0
        for pat, rows, rc, key in self._scripts:
            if pat.search(sql):
                if rc is not None:
                    self.rowcount = rc
                if rows is None:
                    self._next = iter(())
                elif key is not None:
                    self._next = iter(rows.get(key(params), ()))
                else:
                    self._next = iter(rows or ())
                return
        self._next = iter(())

    def fetchone(self):
        return next(self._next, None)

    def fetchall(self):
        return list(self._next)


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **k):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass


class FakeCtx:
    """Stand-in for the get_conn() context manager."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class JsonRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


SESS_COLS = ("id", "workspace_id", "title", "agent_type", "meta", "hidden",
             "pinned", "profile_name", "last_synced_at", "archived",
             "cwd", "git_repo_root", "rev", "field_rev")
MSG_COLS = ("id", "session_id", "workspace_id", "role", "content", "timestamp",
            "agent_type", "meta", "hidden")


def run(coro):
    return asyncio.run(coro)


def insert_rows(cur, table):
    """INSERT INTO <table> (cols) VALUES ... -> list of {col: value} dicts.

    Only works for single-row statements (sessions); multi-row batched
    inserts embed their values in the SQL text (execute_values).
    """
    rows = []
    for sql, params in cur.executed:
        if not sql.startswith(f"INSERT INTO {table}") or params is None:
            continue
        m = re.search(r"\(([^)]+)\) VALUES", sql)
        cols = [c.strip() for c in m.group(1).split(",")]
        rows.append(dict(zip(cols, params)))
    return rows


def update_set_cols(cur, table):
    """Column names in `UPDATE <table> SET a = %s, b = %s WHERE ...`."""
    cols = []
    for sql, _ in cur.executed:
        if sql.startswith(f"UPDATE {table}"):
            m = re.search(r"SET (.+?) WHERE", sql)
            cols = [c.split("=")[0].strip() for c in m.group(1).split(",")]
    return cols


def last_update_map(cur, table):
    """{col: value} from the LAST `UPDATE <table> SET ...` statement run."""
    for sql, params in reversed(cur.executed):
        if sql.startswith(f"UPDATE {table}") and params is not None:
            m = re.search(r"SET (.+?) WHERE", sql)
            cols = [c.split("=")[0].strip() for c in m.group(1).split(",")]
            return dict(zip(cols, params))
    return {}


def fake_execute_values(cur, sql, argslist, template=None, page_size=100, fetch=False):
    """Stand-in for psycopg2.extras.execute_values: record the batched rows
    and emit the statement with an empty RETURNING (so the push's conflict
    retry path lands each row via the scripted cursor)."""
    cur.executed.append((sql, None))
    cur.rowcount = len(argslist)
    cur.batch_rows = list(argslist)
    cur._next = iter(())


class PushTest(unittest.TestCase):
    def _push(self, sessions, existing_ids=(), msg_keys=(), msg_contents=(),
              next_id=5, ws=None, field_revs=None):
        """msg_keys: existing (sid, role, timestamp) triples on the server.
        msg_contents: existing (sid, role, content) triples on the server.
        field_revs: {sid: {"rev": R, "field_rev": {f: rev}}} = the logical
        clock the server currently holds for that session (UPDATE path)."""
        content_by_sid = {}
        for sid, role, content in msg_contents:
            content_by_sid.setdefault(sid, []).append((role, content, None))
        cur = (ScriptedCursor()
               .add(r"SELECT id FROM sessions", [(i,) for i in existing_ids])
               .add(r"SELECT rev, field_rev FROM sessions",
                    {sid: [(v["rev"], json.dumps(v["field_rev"]))]
                     for sid, v in (field_revs or {}).items()},
                    key=lambda p: p[0])   # WHERE id = %s -> sid is params[0]
               .add(r"information_schema\.columns.*sessions",
                    [(c,) for c in SESS_COLS])
               .add(r"information_schema\.columns.*messages",
                    [(c,) for c in MSG_COLS])
               # batched dedup-key prefetch (one query for the whole push)
               .add(r"SELECT session_id, role, timestamp, content FROM messages",
                    [(sid, role, ts, None) for sid, role, ts in msg_keys])
               # batched next-id prefetch per session (GROUP BY)
               .add(r"GROUP BY session_id",
                    [(s["id"], next_id) for s in sessions])
               # lazy content fallback map, fetched per session
               .add(r"SELECT role, content, meta FROM messages", content_by_sid,
                    key=lambda p: p[1])
               # legacy id-based dedup check (role/timestamp missing)
               .add(r"SELECT 1 FROM messages", [])
               # conflict retry (single-row, %s placeholders) always lands
               .add(r"INSERT INTO messages.*VALUES \(%s", rows=[("s1", 0)])
               # batched multi-row insert: nothing pre-exists (RETURNING empty)
               .add(r"INSERT INTO messages", rows=[]))
        conn = FakeConn(cur)
        with mock.patch.object(sync, "get_conn", return_value=FakeCtx(conn)), \
             mock.patch("psycopg2.extras.execute_values",
                        side_effect=fake_execute_values):
            resp = run(sync.push(JsonRequest({"device_id": "dev1",
                                             "sessions": sessions}),
                                ws or {"workspace_id": 1, "user_id": None}))
        return resp, cur

    def test_new_session_and_messages_inserted(self):
        sessions = [{"id": "s1", "title": "hi",
                     "messages": [{"role": "user", "content": "a", "timestamp": 1.0},
                                  {"role": "assistant", "content": "b", "timestamp": 2.0}]}]
        resp, cur = self._push(sessions)
        self.assertEqual(resp["imported"], 1)
        self.assertEqual(resp["updated"], 0)
        self.assertEqual(resp["new_messages"], 2)
        self.assertEqual(resp["duplicates"], 0)
        # batched multi-row insert with RETURNING happened (conflict retries
        # are single-row "VALUES (%s" statements and are excluded)
        batch = [s for s, _ in cur.executed
                 if s.startswith("INSERT INTO messages")
                 and "RETURNING session_id, id" in s
                 and "VALUES (%s" not in s]
        self.assertEqual(len(batch), 1)
        # both messages went into the single batch (not per-message inserts)
        self.assertEqual(len(cur.batch_rows), 2)
        # auto_id path: the id column is allocated client-side (in the batch)
        m = re.search(r"INSERT INTO messages \(([^)]+)\)", batch[0])
        self.assertIn("id", [c.strip() for c in m.group(1).split(",")])
        # session row carries agent_type + workspace_id
        sess_rows = insert_rows(cur, "sessions")
        self.assertEqual(len(sess_rows), 1)
        self.assertEqual(sess_rows[0]["agent_type"], "hermes")
        self.assertEqual(sess_rows[0]["workspace_id"], 1)
        # sync_state upsert always happens
        self.assertTrue(any(s.startswith("INSERT INTO sync_state") for s, _ in cur.executed))

    def test_existing_session_updates_never_touch_agent_type_or_hidden(self):
        sessions = [{"id": "s1", "title": "new title", "agent_type": "codex",
                     "hidden": 1, "messages": []}]
        resp, cur = self._push(sessions, existing_ids=("s1",))
        self.assertEqual(resp["updated"], 1)
        self.assertEqual(resp["imported"], 0)
        cols = update_set_cols(cur, "sessions")
        self.assertNotIn("agent_type", cols)
        self.assertNotIn("hidden", cols)
        self.assertIn("last_synced_at", cols)

    def test_duplicate_message_deduped_by_triple(self):
        sessions = [{"id": "s1", "title": "t",
                     "messages": [{"role": "user", "content": "dup", "timestamp": 1.0},
                                  {"role": "user", "content": "dup", "timestamp": 1.0}]}]
        # both messages collide with the already-present (session, role, timestamp)
        resp, cur = self._push(sessions, msg_keys={("s1", "user", 1.0)})
        self.assertEqual(resp["new_messages"], 0)
        self.assertEqual(resp["duplicates"], 2)
        self.assertFalse(any(s.startswith("INSERT INTO messages") for s, _ in cur.executed))

    def test_content_fallback_dedupes_rebuilt_session(self):
        # same role+content but a regenerated timestamp -> still a duplicate
        sessions = [{"id": "s1", "title": "t",
                     "messages": [{"role": "user", "content": "same", "timestamp": 99.0}]}]
        resp, cur = self._push(sessions, msg_keys={("s1", "user", 1.0)},
                               msg_contents={("s1", "user", "same")})
        self.assertEqual(resp["new_messages"], 0)
        self.assertEqual(resp["duplicates"], 1)
        self.assertFalse(any(s.startswith("INSERT INTO messages") for s, _ in cur.executed))

    def test_empty_content_ms_precision_duplicate_deduped(self):
        # hermes tool-call messages carry empty content; a rebuilt copy
        # re-serializes the timestamp at sub-ms precision (.979 vs
        # .9798274), so the exact triple AND the content fallback both miss
        # it. The ms-truncated key must collide with the stored row.
        sessions = [{"id": "s1", "title": "t",
                     "messages": [{"role": "assistant", "content": "",
                                   "timestamp": 1.0008274}]}]
        resp, cur = self._push(sessions, msg_keys={("s1", "assistant", 1.0)})
        self.assertEqual(resp["new_messages"], 0)
        self.assertEqual(resp["duplicates"], 1)
        self.assertFalse(any(s.startswith("INSERT INTO messages") for s, _ in cur.executed))

    def test_empty_content_ms_duplicate_within_same_push(self):
        # one push can carry both the original and the rebuilt copy (the
        # client's local store still holds both); the second must be
        # deduped against the first, not inserted.
        sessions = [{"id": "s1", "title": "t",
                     "messages": [
                         {"role": "assistant", "content": "", "timestamp": 1.0},
                         {"role": "assistant", "content": "", "timestamp": 1.0008274}]}]
        resp, cur = self._push(sessions)
        self.assertEqual(resp["new_messages"], 1)
        self.assertEqual(resp["duplicates"], 1)
        batch = [s for s, _ in cur.executed
                 if s.startswith("INSERT INTO messages")
                 and "RETURNING session_id, id" in s
                 and "VALUES (%s" not in s]
        self.assertEqual(len(batch), 1)
        self.assertEqual(len(cur.batch_rows), 1)

    def test_nonempty_same_ms_distinct_messages_not_deduped(self):
        # the ms fallback is scoped to EMPTY content only: two distinct
        # non-empty messages sharing a millisecond (codex tool bursts) must
        # both insert -- identical content is handled by the content
        # fallback, different content at the same ms is legit.
        sessions = [{"id": "s1", "title": "t",
                     "messages": [
                         {"role": "tool", "content": "out-a", "timestamp": 1.0001},
                         {"role": "tool", "content": "out-b", "timestamp": 1.0002}]}]
        resp, cur = self._push(sessions)
        self.assertEqual(resp["new_messages"], 2)
        self.assertEqual(resp["duplicates"], 0)

    def test_push_normalizes_path_separators_to_forward_slash(self):
        # Windows local paths (backslashes) must be stored as '/' on the
        # server so all devices share a canonical separator.
        sessions = [{"id": "s1", "title": "t", "cwd": r"E:\OpenCode\agentctxsync",
                     "git_repo_root": r"E:\repo\ssl", "messages": []}]
        resp, cur = self._push(sessions)
        self.assertEqual(resp["imported"], 1)
        sess_rows = insert_rows(cur, "sessions")
        self.assertEqual(sess_rows[0]["cwd"], "E:/OpenCode/agentctxsync")
        self.assertEqual(sess_rows[0]["git_repo_root"], "E:/repo/ssl")


    def test_content_fallback_dedupes_reasonix_normalized_file(self):
        # reasonix rewrites local transcripts (fresh timestamps + system
        # prompt), so its re-push carries identical content under new
        # timestamps. The content fallback must dedupe it like hermes'.
        sessions = [{"id": "r1", "agent_type": "reasonix",
                     "messages": [{"role": "system", "content": "sys",
                                   "timestamp": 200.0},
                                  {"role": "user", "content": "same",
                                   "timestamp": 201.0}]}]
        resp, cur = self._push(
            sessions, msg_keys={("r1", "user", 100.0)},
            msg_contents={("r1", "system", "sys"), ("r1", "user", "same")})
        self.assertEqual(resp["new_messages"], 0)
        self.assertEqual(resp["duplicates"], 2)
        self.assertFalse(any(s.startswith("INSERT INTO messages") for s, _ in cur.executed))

    def test_content_fallback_not_applied_to_codex_repeated_output(self):
        # codex legitimately repeats identical content (tool outputs from
        # distinct calls) — the triple-only dedup must keep those
        sessions = [{"id": "c1", "agent_type": "codex",
                     "messages": [{"role": "tool", "content": "same output",
                                   "timestamp": 2.0}]}]
        resp, cur = self._push(sessions, msg_keys={("c1", "tool", 1.0)},
                               msg_contents={("c1", "tool", "same output")})
        self.assertEqual(resp["new_messages"], 1)
        self.assertEqual(resp["duplicates"], 0)
        self.assertTrue(any(s.startswith("INSERT INTO messages") for s, _ in cur.executed))

    def test_content_fallback_dedupes_foreign_session_repush(self):
        # A workbuddy session was pulled by the hermes client and pushed
        # back with a floating-point-shifted timestamp (cross-client
        # re-push). user/assistant rows with identical content must be
        # deduped for EVERY agent, not just hermes/reasonix.
        sessions = [{"id": "w1", "agent_type": "workbuddy",
                     "messages": [{"role": "user", "content": "same",
                                   "timestamp": 99.0005},
                                  {"role": "assistant", "content": "reply",
                                   "timestamp": 100.0005}]}]
        resp, cur = self._push(sessions, msg_keys={("w1", "user", 99.0),
                                                   ("w1", "assistant", 100.0)},
                               msg_contents={("w1", "user", "same"),
                                             ("w1", "assistant", "reply")})
        self.assertEqual(resp["new_messages"], 0)
        self.assertEqual(resp["duplicates"], 2)
        self.assertFalse(any(s.startswith("INSERT INTO messages") for s, _ in cur.executed))

    def test_content_fallback_keeps_workbuddy_tool_repeats(self):
        # non-hermes/reasonix agents keep triple-only dedup for tool rows:
        # identical tool outputs from distinct calls must survive
        sessions = [{"id": "w1", "agent_type": "workbuddy",
                     "messages": [{"role": "tool", "content": "same output",
                                   "timestamp": 2.0}]}]
        resp, cur = self._push(sessions, msg_keys={("w1", "tool", 1.0)},
                               msg_contents={("w1", "tool", "same output")})
        self.assertEqual(resp["new_messages"], 1)
        self.assertEqual(resp["duplicates"], 0)
        self.assertTrue(any(s.startswith("INSERT INTO messages") for s, _ in cur.executed))

    def test_meta_bare_string_fallback_dedupes_lost_content(self):
        # a client whose foreign-store round-trip lost message content may
        # push the text as a bare meta string (JSONB string, not object).
        # The dedup key falls back to that text so the re-push is caught.
        sessions = [{"id": "w1", "agent_type": "workbuddy",
                     "messages": [{"role": "user", "content": None,
                                   "meta": "same", "timestamp": 99.0005}]}]
        resp, cur = self._push(sessions, msg_keys={("w1", "user", 99.0)},
                               msg_contents={("w1", "user", "same")})
        self.assertEqual(resp["new_messages"], 0)
        self.assertEqual(resp["duplicates"], 1)
        self.assertFalse(any(s.startswith("INSERT INTO messages") for s, _ in cur.executed))

    def test_concurrent_triple_race_does_not_duplicate(self):
        # A concurrent push committed the (session, role, timestamp) triple
        # after this push's msg_keys snapshot was taken (the snapshot is a
        # SELECT at push start, so a racing push lands in between). The batch
        # insert must skip it via ON CONFLICT DO NOTHING + the per-row triple
        # check — NOT retry it under a fresh id (that is what used to
        # duplicate the row).
        sessions = [{"id": "s1", "title": "t",
                     "messages": [{"role": "user", "content": "raced",
                                   "timestamp": 1.0}]}]
        cur = (ScriptedCursor()
               .add(r"SELECT id FROM sessions", [])
               .add(r"information_schema\.columns.*sessions",
                    [(c,) for c in SESS_COLS])
               .add(r"information_schema\.columns.*messages",
                    [(c,) for c in MSG_COLS])
               # dedup-key snapshot: EMPTY — the racing push committed later
               .add(r"SELECT session_id, role, timestamp FROM messages", [])
               .add(r"GROUP BY session_id", [("s1", 5)])
               .add(r"SELECT role, content, meta FROM messages", {})
               # the racing push's row is now visible to the per-row check
               .add(r"SELECT 1 FROM messages WHERE workspace_id=%s AND session_id=%s AND role=%s AND timestamp=%s",
                    {("s1", "user", 1.0): [("s1",)]}, key=lambda p: p[1:])
               .add(r"SELECT 1 FROM messages", [])
               .add(r"INSERT INTO messages.*VALUES \(%s", rows=[("s1", 0)])
               .add(r"INSERT INTO messages", rows=[]))
        conn = FakeConn(cur)
        with mock.patch.object(sync, "get_conn", return_value=FakeCtx(conn)), \
             mock.patch("psycopg2.extras.execute_values",
                        side_effect=fake_execute_values):
            resp = run(sync.push(JsonRequest({"device_id": "dev1",
                                              "sessions": sessions}),
                                 {"workspace_id": 1, "user_id": None}))
        self.assertEqual(resp["imported"], 1)
        self.assertEqual(resp["new_messages"], 0)   # raced row not re-inserted
        self.assertEqual(resp["duplicates"], 1)
        # no fresh-id retry happened for the raced message
        self.assertFalse(any(s.startswith("INSERT INTO messages") and "VALUES (%s" in s
                             for s, _ in cur.executed))

    def test_inbound_legacy_prefixed_ids_normalized(self):
        # id-scheme upgrade inbound compat: old clients still push
        # codex:<uuid> / magic:<bare> ids. The shim maps them to bare ids
        # with agent_type/profile_name columns.
        sessions = [
            {"id": "codex:019fc071-fab4-7661-9a0b-2afaa65cbb31", "title": "c",
             "messages": [{"session_id": "codex:019fc071-fab4-7661-9a0b-2afaa65cbb31",
                           "role": "user", "content": "x", "timestamp": 1.0}]},
            {"id": "magic:20260808_205157_c272fe", "title": "m", "messages": []},
            {"id": "default:20260808_180012_0c275f", "title": "d", "messages": []},
        ]
        resp, cur = self._push(sessions)
        self.assertEqual(resp["imported"], 3)
        rows = {r["id"]: r for r in insert_rows(cur, "sessions")}
        self.assertIn("019fc071-fab4-7661-9a0b-2afaa65cbb31", rows)
        self.assertEqual(rows["019fc071-fab4-7661-9a0b-2afaa65cbb31"]["agent_type"], "deepseek-harness")
        self.assertEqual(rows["20260808_205157_c272fe"]["agent_type"], "hermes")
        self.assertEqual(rows["20260808_205157_c272fe"]["profile_name"], "magic")
        self.assertEqual(rows["20260808_180012_0c275f"]["agent_type"], "hermes")
        self.assertEqual(rows["20260808_180012_0c275f"]["profile_name"], "")
        # message session_id normalized to the bare id
        msg_rows = insert_rows(cur, "messages")
        self.assertTrue(all(m["session_id"] == "019fc071-fab4-7661-9a0b-2afaa65cbb31"
                            for m in msg_rows))

    def test_new_scheme_profile_name_field_written(self):
        # new clients push bare ids with explicit fields; profile_name and
        # agent_type pass through to the columns
        sessions = [{"id": "20260808_180013_0c275e", "agent_type": "hermes",
                     "profile_name": "magic", "title": "m", "messages": []}]
        resp, cur = self._push(sessions)
        self.assertEqual(resp["imported"], 1)
        rows = {r["id"]: r for r in insert_rows(cur, "sessions")}
        self.assertEqual(rows["20260808_180013_0c275e"]["profile_name"], "magic")
        self.assertEqual(rows["20260808_180013_0c275e"]["agent_type"], "hermes")

    def test_quota_gate_skipped_for_master_key(self):
        # user_id None (master key) -> no quota queries at all
        sessions = [{"id": "s1", "messages": []}]
        resp, cur = self._push(sessions)
        self.assertEqual(resp["imported"], 1)

    # ---- field-level optimistic merge (decision: 字段级乐观并发) ----

    def test_field_merge_none_base_is_server_authoritative(self):
        # New-client push of a user-edit field whose base it doesn't know
        # (base=None): server must NOT write it (trust the server value) and
        # must not bump that field's version.
        sessions = [{"id": "s1", "title": "t", "cwd": "D:/LOCAL_MOVE",
                     "field_meta": {"cwd": None}, "messages": []}]
        resp, cur = self._push(
            sessions, existing_ids=("s1",),
            field_revs={"s1": {"rev": 3, "field_rev": {"cwd": 3, "title": 1}}})
        upd = last_update_map(cur, "sessions")
        self.assertNotIn("cwd", upd)          # server authority: not written
        self.assertEqual(upd["rev"], 3)       # nothing accepted -> no bump
        self.assertEqual(resp["session_revs"]["s1"]["field_rev"]["cwd"], 3)

    def test_field_merge_none_base_seeds_unversioned_field(self):
        # Migration baseline: the field has never been written under the new
        # scheme (field_rev empty). A base=None push MUST seed it (accept +
        # allocate rev 1) -- otherwise field_rev stays 0, every later push is
        # again base=None and refused, and the field could never change
        # (bootstrap deadlock; observed: 266 sessions stuck at field_rev={}).
        sessions = [{"id": "s1", "title": "t", "cwd": "D:/work/X",
                     "field_meta": {"cwd": None}, "messages": []}]
        resp, cur = self._push(
            sessions, existing_ids=("s1",),
            field_revs={"s1": {"rev": 0, "field_rev": {}}})
        upd = last_update_map(cur, "sessions")
        self.assertEqual(upd["cwd"], "D:/work/X")
        self.assertEqual(upd["rev"], 1)
        self.assertEqual(json.loads(upd["field_rev"]), {"cwd": 1})

    def test_field_merge_known_base_accepts_and_bumps(self):
        # Dirty user-edit field with a known base -> accept + bump version.
        sessions = [{"id": "s1", "title": "t",
                     "cwd": "D:/work/2026新疆公路数字底座",
                     "field_meta": {"cwd": 3}, "messages": []}]
        resp, cur = self._push(
            sessions, existing_ids=("s1",),
            field_revs={"s1": {"rev": 3, "field_rev": {"cwd": 3, "title": 1}}})
        upd = last_update_map(cur, "sessions")
        self.assertEqual(upd["cwd"], "D:/work/2026新疆公路数字底座")
        self.assertEqual(upd["rev"], 4)
        self.assertEqual(json.loads(upd["field_rev"]), {"cwd": 4, "title": 1})

    def test_field_merge_unasserted_user_field_not_written(self):
        # New client sends a user-edit field in the payload but does NOT
        # assert it in field_meta -> server keeps its value (a stale/non-dirty
        # device must not clobber a peer's change).
        sessions = [{"id": "s1", "title": "t", "cwd": "D:/STALE",
                     "field_meta": {"title": 2}, "messages": []}]
        resp, cur = self._push(
            sessions, existing_ids=("s1",),
            field_revs={"s1": {"rev": 5, "field_rev": {"cwd": 3, "title": 2}}})
        upd = last_update_map(cur, "sessions")
        self.assertNotIn("cwd", upd)          # unasserted -> not written
        self.assertEqual(upd["rev"], 6)       # title bumped

    def test_legacy_client_without_field_meta_full_overwrite(self):
        # No field_meta key -> legacy client: user-edit fields ARE written
        # (documented mixed-version window), no protection.
        sessions = [{"id": "s1", "title": "t", "cwd": "D:/legacy",
                     "messages": []}]
        resp, cur = self._push(
            sessions, existing_ids=("s1",),
            field_revs={"s1": {"rev": 2, "field_rev": {"cwd": 2}}})
        upd = last_update_map(cur, "sessions")
        self.assertEqual(upd["cwd"], "D:/legacy")

    def test_field_merge_concurrent_same_field_arrival_lww(self):
        # Two devices edit the same field off the same base; the later push
        # arrives with a stale base but is a genuine dirty edit -> accept
        # (arrival LWW), bump past the first device's version.
        sessions = [{"id": "s1", "title": "t", "cwd": "D:/B_SIDE",
                     "field_meta": {"cwd": 3}, "messages": []}]
        resp, cur = self._push(
            sessions, existing_ids=("s1",),
            field_revs={"s1": {"rev": 7, "field_rev": {"cwd": 7}}})
        upd = last_update_map(cur, "sessions")
        self.assertEqual(upd["cwd"], "D:/B_SIDE")
        self.assertEqual(upd["rev"], 8)

    def test_new_session_seeds_logical_clock(self):
        # Brand-new session: no conflict possible; seed rev=1 and field_rev
        # for every present user-edit field so the creator can anchor on pull.
        sessions = [{"id": "s1", "title": "t", "cwd": "D:/x",
                     "field_meta": {"cwd": None, "title": None},
                     "messages": []}]
        resp, cur = self._push(sessions)
        r = insert_rows(cur, "sessions")[0]
        self.assertEqual(r["rev"], 1)
        self.assertEqual(json.loads(r["field_rev"]), {"cwd": 1, "title": 1})
        self.assertEqual(resp["session_revs"]["s1"]["rev"], 1)
        self.assertFalse(any("quota_config" in s for s, _ in cur.executed))
        self.assertFalse(any("FROM users" in s for s, _ in cur.executed))


class PullTest(unittest.TestCase):
    def _pull(self, body, sessions, messages_by_sid):
        cur = (ScriptedCursor()
               .add(r"SELECT COUNT\(\*\) AS cnt", [{"cnt": 2}])
               .add(r"FROM sessions", sessions)
               # single batched message query for the whole page (ANY)
               .add(r"FROM messages",
                    {"all": [dict(m, session_id=sid)
                             for sid, ms in messages_by_sid.items() for m in ms]},
                    key=lambda p: "all"))
        conn = FakeConn(cur)
        with mock.patch.object(sync, "get_conn", return_value=FakeCtx(conn)):
            return run(sync.pull(JsonRequest(body), {"workspace_id": 1, "user_id": None})), cur

    def test_incremental_pull_filters_and_merges(self):
        sessions = [{"id": "a", "title": "A", "started_at": 10.0},
                    {"id": "b", "title": "B", "started_at": 9.0}]
        msgs = {"a": [{"role": "user", "content": "a1", "timestamp": 1.0},
                      {"role": "assistant", "content": "a2", "timestamp": 2.0}],
                "b": [{"role": "user", "content": "b1", "timestamp": 1.5}]}
        resp, cur = self._pull({"device_id": "d", "last_sync_at": 5.0}, sessions, msgs)
        self.assertEqual(resp["session_count"], 2)
        self.assertEqual(resp["total_sessions"], 2)
        self.assertEqual(resp["message_count"], 3)
        by_id = {s["id"]: s for s in resp["sessions"]}
        self.assertEqual([m["content"] for m in by_id["a"]["messages"]], ["a1", "a2"])
        self.assertEqual([m["content"] for m in by_id["b"]["messages"]], ["b1"])
        # incremental cutoff is applied to the session query
        sess_sql = [s for s, _ in cur.executed if "FROM sessions" in s and "ORDER BY" in s][0]
        self.assertIn("last_synced_at >", sess_sql)

    def test_incremental_pull_serves_full_message_sets(self):
        # a session returned by the incremental cutoff (recent
        # last_synced_at, e.g. a peer device re-pushed it) must come with
        # its FULL message set -- the message query has no timestamp filter,
        # or the client would upsert a ghost session (stale message_count,
        # zero messages). Decision record 2026.08.22.6.
        sessions = [{"id": "a", "title": "A", "started_at": 10.0}]
        msgs = {"a": [{"role": "user", "content": "old-msg", "timestamp": 1.0}]}
        resp, cur = self._pull({"device_id": "d", "last_sync_at": 5.0}, sessions, msgs)
        msg_sql = [s for s, _ in cur.executed if "FROM messages" in s][0]
        self.assertNotIn("timestamp >", msg_sql)
        self.assertEqual(len(resp["sessions"][0]["messages"]), 1)
        self.assertEqual(resp["sessions"][0]["messages"][0]["timestamp"], 1.0)

    def test_pull_returns_agent_and_profile_columns(self):
        # the /pull payload carries agent_type/profile_name so clients can
        # route and tag sessions under the column-based id scheme
        sessions = [{"id": "20260808_180013_0c275e", "title": "m",
                     "agent_type": "hermes", "profile_name": "magic"},
                    {"id": "3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06", "title": "w",
                     "agent_type": "workbuddy", "profile_name": None}]
        resp, _ = self._pull({"device_id": "d", "last_sync_at": 5.0}, sessions, {})
        by_id = {s["id"]: s for s in resp["sessions"]}
        self.assertEqual(by_id["20260808_180013_0c275e"]["agent_type"], "hermes")
        self.assertEqual(by_id["20260808_180013_0c275e"]["profile_name"], "magic")
        self.assertEqual(by_id["3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06"]["agent_type"],
                         "workbuddy")

    def test_pull_returns_field_rev_parsed_as_dict(self):
        # /pull carries each session's per-field logical clock so clients can
        # anchor base_rev. PG returns JSONB as text; the server must normalize
        # it to a dict before handing it to the client.
        sessions = [{"id": "a", "title": "A", "started_at": 10.0,
                     "field_rev": '{"cwd": 3, "title": 7}'}]
        resp, _ = self._pull({"device_id": "d", "last_sync_at": 5.0}, sessions, {})
        s = resp["sessions"][0]
        self.assertIsInstance(s["field_rev"], dict)
        self.assertEqual(s["field_rev"], {"cwd": 3, "title": 7})

    def test_agent_param_ignored_full_pool(self):
        # Full-pool pull: the workspace's whole visible session set is served
        # regardless of the requesting client's agent. The body's ``agent``
        # field must not filter the query — a hermes client has to see the
        # workbuddy/codex sessions another device pushed (cross-agent sync).
        sessions = [{"id": "a", "title": "A", "agent_type": "hermes"},
                    {"id": "b", "title": "B", "agent_type": "workbuddy"}]
        resp, cur = self._pull({"device_id": "d", "agent": "codex", "last_sync_at": 5.0},
                               sessions, {})
        sess_sql = [s for s, _ in cur.executed if "FROM sessions" in s and "ORDER BY" in s][0]
        self.assertNotIn("agent_type", sess_sql)
        self.assertEqual(resp["total_sessions"], 2)
        self.assertEqual({s["id"] for s in resp["sessions"]}, {"a", "b"})


class ProjectsPullTest(unittest.TestCase):
    def test_projects_pull_full_pool_ignores_agent(self):
        # /api/projects/pull serves every visible project (all agents) no
        # matter what the requesting client passes — the query must never
        # gain an agent_type filter.
        cur = (ScriptedCursor()
               .add(r"FROM projects", [
                   {"id": "p1", "slug": "hermes-p", "agent_type": "hermes"},
                   {"id": "p2", "slug": "wb-p", "agent_type": "workbuddy"}])
               .add(r"FROM project_folders", [])
               .add(r"FROM project_remap", []))
        conn = FakeConn(cur)
        with mock.patch.object(projects, "get_conn", return_value=FakeCtx(conn)):
            resp = run(projects.api_projects_pull(
                JsonRequest({"device_id": "d", "agent": "codex"}),
                {"workspace_id": 1, "user_id": None}))
        proj_sql = [s for s, _ in cur.executed if "FROM projects" in s][0]
        self.assertNotIn("agent_type", proj_sql)
        self.assertEqual({p["id"] for p in resp["projects"]}, {"p1", "p2"})
        self.assertEqual(resp["remaps"], [])

    def test_projects_pull_returns_field_rev_parsed(self):
        # /api/projects/pull carries each project's per-field logical clock
        # as a dict (JSONB comes back text from PG).
        cur = (ScriptedCursor()
               .add(r"FROM projects", [
                   {"id": "p1", "slug": "p1", "name": "P1",
                    "field_rev": '{"name": 2, "primary_path": 1}'}])
               .add(r"FROM project_folders", [])
               .add(r"FROM project_remap", []))
        conn = FakeConn(cur)
        with mock.patch.object(projects, "get_conn", return_value=FakeCtx(conn)):
            resp = run(projects.api_projects_pull(
                JsonRequest({"device_id": "d", "agent": "codex"}),
                {"workspace_id": 1, "user_id": None}))
        p = resp["projects"][0]
        self.assertIsInstance(p["field_rev"], dict)
        self.assertEqual(p["field_rev"], {"name": 2, "primary_path": 1})


class ProjectsPushMergeTest(unittest.TestCase):
    """Field-level optimistic merge for project scalar fields (Phase 2)."""

    def _push(self, project, existing_id=None, clock=None):
        cur = (ScriptedCursor()
               .add(r"SELECT id FROM projects", [(existing_id,)] if existing_id else [])
               .add(r"SELECT rev, field_rev FROM projects",
                    [(clock["rev"],
                      json.dumps(clock["field_rev"]))] if clock else []))
        conn = FakeConn(cur)
        with mock.patch.object(projects, "get_conn", return_value=FakeCtx(conn)):
            resp = run(projects.api_projects_push(
                JsonRequest({"device_id": "d", "projects": [project]}),
                {"workspace_id": 1, "user_id": None}))
        return resp, cur

    def test_project_field_merge_known_base_accepts_and_bumps(self):
        project = {"id": "p1", "name": "新名字", "primary_path": "D:/x",
                   "field_meta": {"name": 3}}
        resp, cur = self._push(project, existing_id="p1",
                               clock={"rev": 3, "field_rev": {"name": 3}})
        upd = last_update_map(cur, "projects")
        self.assertEqual(upd["name"], "新名字")
        self.assertNotIn("primary_path", upd)   # unasserted -> kept server-side
        self.assertEqual(upd["rev"], 4)
        self.assertEqual(json.loads(upd["field_rev"]), {"name": 4})

    def test_project_field_merge_none_base_server_authoritative(self):
        project = {"id": "p1", "name": "LOCAL", "field_meta": {"name": None}}
        resp, cur = self._push(project, existing_id="p1",
                               clock={"rev": 5, "field_rev": {"name": 5}})
        upd = last_update_map(cur, "projects")
        self.assertNotIn("name", upd)
        self.assertEqual(upd["rev"], 5)

    def test_project_field_merge_none_base_seeds_unversioned(self):
        # Bootstrap deadlock fix (mirrors sessions): a base=None field this
        # server has never versioned is accepted as the first new-scheme seed.
        project = {"id": "p1", "name": "P1", "primary_path": "D:/x",
                   "field_meta": {"primary_path": None}}
        resp, cur = self._push(project, existing_id="p1",
                               clock={"rev": 0, "field_rev": {}})
        upd = last_update_map(cur, "projects")
        self.assertEqual(upd["primary_path"], "D:/x")
        self.assertEqual(upd["rev"], 1)
        self.assertEqual(json.loads(upd["field_rev"]), {"primary_path": 1})

    def test_new_project_seeds_logical_clock(self):
        project = {"id": "p2", "name": "New", "primary_path": "D:/n",
                   "field_meta": {"name": None, "primary_path": None}}
        resp, cur = self._push(project)
        r = insert_rows(cur, "projects")[0]
        self.assertEqual(r["rev"], 1)
        self.assertEqual(json.loads(r["field_rev"]), {"name": 1, "primary_path": 1})

    def test_legacy_client_full_overwrite(self):
        # No field_meta key -> legacy client writes user-edit fields outright
        # (documented mixed-version window).
        project = {"id": "p1", "name": "legacy"}
        resp, cur = self._push(project, existing_id="p1",
                               clock={"rev": 2, "field_rev": {"name": 2}})
        upd = last_update_map(cur, "projects")
        self.assertEqual(upd["name"], "legacy")


if __name__ == "__main__":
    unittest.main()
