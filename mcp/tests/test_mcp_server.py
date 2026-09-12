"""Tests for the MCP client's batching logic (mcp/server.py).

Sessions are pushed in small batches bounded by session count AND total
message count, so a giant request cannot exceed the HTTP timeout during a
full sync (a full resync pulls/pushes every session on the server).
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402


def mk(n_msgs: int) -> dict:
    return {"id": f"s{n_msgs}",
            "messages": [{"role": "user"} for _ in range(n_msgs)]}


class ChunkSessionsTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(server._chunk_sessions([]), [])

    def test_under_limits_single_batch(self):
        chunks = server._chunk_sessions([mk(10), mk(20)],
                                        max_sessions=20, max_messages=3000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 2)

    def test_session_count_bound(self):
        chunks = server._chunk_sessions([mk(1)] * 45,
                                        max_sessions=20, max_messages=3000)
        self.assertEqual([len(c) for c in chunks], [20, 20, 5])

    def test_message_count_bound(self):
        chunks = server._chunk_sessions([mk(2000)] * 4,
                                        max_sessions=20, max_messages=3000)
        self.assertEqual([len(c) for c in chunks], [1, 1, 1, 1])
        for c in chunks:
            self.assertLessEqual(sum(len(s["messages"]) for s in c), 3000)

    def test_huge_session_gets_own_batch(self):
        chunks = server._chunk_sessions([mk(100), mk(5000), mk(100)],
                                        max_sessions=20, max_messages=3000)
        self.assertEqual([len(c) for c in chunks], [1, 1, 1])

    def test_mixed_merge_and_split(self):
        # 3 x 1500 msgs: a pair fits (3000), a third would exceed
        chunks = server._chunk_sessions([mk(1500)] * 3,
                                        max_sessions=20, max_messages=3000)
        self.assertEqual([len(c) for c in chunks], [2, 1])


    def test_bytes_bound_splits_big_session_out(self):
        """B4: a session whose payload exceeds max_bytes must not ride along
        with others (regression: a ~148MB chunk tripped the proxy 413 and
        aborted the whole push cycle)."""
        big = {**mk(1), "blob": "x" * (6 * 1024 * 1024)}
        chunks = server._chunk_sessions([mk(10), big, mk(10)],
                                        max_sessions=20, max_messages=3000,
                                        max_bytes=4 * 1024 * 1024)
        self.assertEqual([len(c) for c in chunks], [1, 1, 1])

    def test_single_huge_session_rides_alone(self):
        """B4: a session larger than max_bytes gets its own batch (it cannot
        be split); neighbours stay in their own batches."""
        huge = {**mk(1), "blob": "x" * (20 * 1024 * 1024)}
        chunks = server._chunk_sessions([mk(10), huge, mk(10)],
                                        max_sessions=20, max_messages=3000,
                                        max_bytes=8 * 1024 * 1024)
        self.assertEqual([len(c) for c in chunks], [1, 1, 1])
        self.assertEqual(chunks[1][0]["id"], huge["id"])


class ToolRegistrationTest(unittest.TestCase):
    """The tool surface registers on both mcp SDK eras. Regression: SDK v2
    (mcp>=2.0.0) removed Server.list_tools()/call_tool() decorators, which
    crashed the client at import with AttributeError."""

    EXPECTED = [name for spec in server.TOOL_SPECS
                for name in (spec[0], "hermes_" + spec[0])]

    def test_build_tools_surface(self):
        tools = server._build_tools()
        self.assertEqual([t.name for t in tools], self.EXPECTED)
        self.assertEqual(len(tools), len(self.EXPECTED))
        for t in tools:
            self.assertIsNotNone(t.description)
            # field name differs across SDK eras (inputSchema vs input_schema);
            # the wire alias is stable
            schema = t.model_dump(by_alias=True, mode="json")["inputSchema"]
            self.assertEqual(schema["type"], "object")

    def test_handlers_registered(self):
        # The module import itself already exercises the era branch; assert
        # the handlers actually landed on the server instance.
        if server.SDK_V2:
            handlers = server.server._request_handlers
            self.assertIn("tools/list", handlers)
            self.assertIn("tools/call", handlers)
        else:
            from mcp.types import CallToolRequest, ListToolsRequest
            handlers = server.server.request_handlers
            self.assertIn(ListToolsRequest, handlers)
            self.assertIn(CallToolRequest, handlers)

    def test_dispatch_unknown_tool_raises(self):
        with self.assertRaises(ValueError):
            asyncio.run(server._dispatch_tool("nope", {}))


class PushFingerprintTest(unittest.TestCase):
    """B5: unchanged sessions are skipped by the push loop (no per-cycle
    full-store re-upload); fingerprints update only on success."""

    def test_fingerprint_counts_and_max_ts(self):
        s = {"id": "s1", "messages": [{"timestamp": 1.0}, {"timestamp": 3.5}]}
        self.assertEqual(server._session_fingerprint(s), (2, 3.5))

    def test_fingerprint_includes_mtime_when_adapter_supports(self):
        s = {"id": "s1", "messages": [{"timestamp": 1.0}]}
        with mock.patch.object(server.adapter, "session_mtime",
                               return_value=123.45678):
            self.assertEqual(server._session_fingerprint(s), (1, 1.0, 123.457))

    def test_push_skips_unchanged_sessions(self):
        class FakeAdapter:
            agent_type = "workbuddy"

            def discover(self):
                return "store"

            def read_sessions(self):
                return [{"id": "s1", "cwd": "c:/x", "title": "t",
                         "messages": [{"session_id": "s1", "role": "user",
                                       "content": "hi", "timestamp": 1.0}]}]

            def _is_foreign(self, sid):
                return False

            def _foreign_agent(self, sid):
                return None

        calls = []
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with mock.patch.object(server, "adapter", FakeAdapter()), \
                    mock.patch.object(server, "FIELD_META_PATH",
                                      td / "meta.json"), \
                    mock.patch.object(server, "PUSH_FINGERPRINT_PATH",
                                      td / "fp.json"), \
                    mock.patch.object(
                        server, "api_call",
                        side_effect=lambda *a, **k: calls.append(a) or {
                            "imported": 0, "updated": 1, "new_messages": 0,
                            "sync_at": 1.0, "session_revs": {}}):
                r1 = server.push_sessions()
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][2]["sessions"][0]["id"], "s1")
                calls.clear()
                r2 = server.push_sessions()
                self.assertEqual(calls, [])  # unchanged -> skipped entirely
                self.assertEqual(r2["imported"], 0)
                self.assertEqual(r2["updated"], 0)

    def test_push_retries_after_failure(self):
        """A failed chunk must NOT advance the fingerprint (retried next
        cycle), while successful sessions still anchor."""
        class FakeAdapter:
            agent_type = "workbuddy"

            def discover(self):
                return "store"

            def read_sessions(self):
                return [{"id": "s1", "cwd": "c:/x", "title": "t",
                         "messages": [{"session_id": "s1", "role": "user",
                                       "content": "hi", "timestamp": 1.0}]}]

            def _is_foreign(self, sid):
                return False

            def _foreign_agent(self, sid):
                return None

        results = [{"error": 413}, {"imported": 0, "updated": 1,
                                    "new_messages": 0, "sync_at": 1.0,
                                    "session_revs": {}}]
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with mock.patch.object(server, "adapter", FakeAdapter()), \
                    mock.patch.object(server, "FIELD_META_PATH",
                                      td / "meta.json"), \
                    mock.patch.object(server, "PUSH_FINGERPRINT_PATH",
                                      td / "fp.json"), \
                    mock.patch.object(server, "api_call",
                                      side_effect=lambda *a, **k:
                                      results.pop(0)):
                r1 = server.push_sessions()
                self.assertIn("failed", r1.get("error", ""))
                # fingerprint not anchored for the failed session
                self.assertEqual(server._load_push_fingerprint(), {})
    def test_legacy_flat_fingerprint_is_stale_when_identity_known(self):
        """A fingerprint file from before identity recording must not keep
        suppressing a re-push: once an identity is known, an old flat dict
        is treated empty so the store is re-pushed (mirrors the pull
        watermark's legacy handling)."""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(server, "PUSH_FINGERPRINT_PATH",
                                   Path(td) / "fp.json"), \
                    mock.patch.object(server, "SYNC_SERVER",
                                      "https://www.agentctxsync.com"):
                Path(td, "fp.json").write_text(
                    '{"s1": [2, 3.5]}', encoding="utf-8")
                self.assertEqual(server._load_push_fingerprint(), {})

    def test_fingerprint_invalidated_on_server_switch(self):
        """A fingerprint recorded against a DIFFERENT server must be ignored
        in full (full re-push), exactly like the pull watermark — otherwise
        sessions pushed to an old server/workspace never reach the new one."""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(server, "PUSH_FINGERPRINT_PATH",
                                   Path(td) / "fp.json"), \
                    mock.patch.object(server, "SYNC_SERVER",
                                      "https://www.agentctxsync.com"):
                Path(td, "fp.json").write_text(
                    '{"server": "https://old.invalid", '
                    '"sessions": {"s1": [2, 3.5]}}', encoding="utf-8")
                self.assertEqual(server._load_push_fingerprint(), {})

    def test_fingerprint_roundtrip_new_format(self):
        """Same-server save/load round-trips through the new server-bound
        wrapper (the push loop still sees the flat {sid: fp} dict)."""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(server, "PUSH_FINGERPRINT_PATH",
                                   Path(td) / "fp.json"), \
                    mock.patch.object(server, "SYNC_SERVER",
                                      "https://www.agentctxsync.com"):
                server._save_push_fingerprint({"s1": [2, 3.5]})
                self.assertEqual(server._load_push_fingerprint(),
                                 {"s1": [2, 3.5]})


class FieldMergeTest(unittest.TestCase):
    """Field-level optimistic concurrency on the client (see ARCHITECTURE.md):
    push only dirty / first-contact user-edit fields, and anchor the accepted
    base so they stop reading dirty. Mirrors the server-side merge rules."""

    def test_push_omits_non_dirty_fields_keeps_derived(self):
        meta = {"s1": {"cwd": {"base": 4, "val": "D:/old"},
                       "title": {"base": 1, "val": "t"}}}
        s = {"id": "s1", "title": "t", "cwd": "D:/old", "model": "m",
             "messages": []}
        out = server._annotate_push_session(s, meta)
        self.assertEqual(out["model"], "m")        # derived kept
        self.assertNotIn("cwd", out)               # not dirty -> omitted
        self.assertNotIn("title", out)
        self.assertEqual(out["field_meta"], {})

    def test_push_dirty_field_sends_with_known_base(self):
        meta = {"s1": {"cwd": {"base": 4, "val": "D:/old"}}}
        s = {"id": "s1", "cwd": "D:/NEW", "messages": []}
        out = server._annotate_push_session(s, meta)
        self.assertEqual(out["cwd"], "D:/NEW")
        self.assertEqual(out["field_meta"], {"cwd": 4})

    def test_push_first_contact_uses_none_base(self):
        # No sidecar entry -> base unknown -> field asserted with base None
        # so the server stays authoritative for an existing session.
        s = {"id": "s1", "cwd": "D:/x", "messages": []}
        out = server._annotate_push_session(s, {})
        self.assertEqual(out["cwd"], "D:/x")
        self.assertEqual(out["field_meta"], {"cwd": None})

    def test_anchor_records_only_accepted_fields(self):
        meta = {}
        chunk = [{"id": "s1", "cwd": "D:/NEW",
                  "field_meta": {"cwd": 4, "title": None}}]
        revs = {"s1": {"rev": 7, "field_rev": {"cwd": 7, "title": 2}}}
        server._anchor_push_meta(meta, chunk, revs)
        # cwd (known base, accepted) anchored; title (base None, refused) not
        self.assertEqual(meta,
                         {"s1": {"cwd": {"base": 7, "val": "D:/NEW"}}})

    def test_missing_local_value_is_not_dirty(self):
        """A store that never wrote the field is not a local edit.

        A dsh log without a ``session/title`` event (or a column never
        populated) used to read "dirty" against the sidecar anchor, so the
        pull dropped the server's authoritative value and the session stayed
        permanently untitled (desktop list fell back to the workspace name).
        Missing/empty local means "adopt the server", never "user deleted".
        """
        self.assertFalse(server._field_dirty("title", None, "Server title"))
        self.assertFalse(server._field_dirty("title", "", "Server title"))
        self.assertFalse(server._field_dirty("cwd", None, "D:/x"))
        # real local values still compare exactly (paths case-insensitively)
        self.assertTrue(server._field_dirty("title", "Local", "Server"))
        self.assertFalse(server._field_dirty("title", "Same", "Same"))
        self.assertFalse(
            server._field_dirty("cwd", r"D:\Work\X", "D:/work/x"))

    def test_push_missing_local_field_is_omitted_not_asserted(self):
        """Mirror rule on push: a None local value must not be asserted
        against a known base (would clobber the server value)."""
        meta = {"s1": {"title": {"base": 1, "val": "Server title"}}}
        s = {"id": "s1", "title": None, "messages": []}
        out = server._annotate_push_session(s, meta)
        self.assertNotIn("title", out)
        self.assertEqual(out.get("field_meta"), {})


class PullTitleAdoptTest(unittest.TestCase):
    """Pull must adopt the server title for a session whose local log never
    got one (title-loss loop), and reconcile idle sessions from the sidecar
    anchor when the server no longer re-serves them (see CHANGELOG
    2026.09.08.1)."""

    def _fake_adapter(self, received):
        class FakeAdapter:
            agent_type = "dsh"

            def discover(self):
                return "store"

            def last_synced_at(self):
                return 0.0

            def save_sync_watermark(self, ts):
                pass

            def read_sessions(self):
                # local store: session exists but has NO title yet
                return [{"id": "s1", "cwd": "D:/x",
                         "messages": [{"role": "user", "content": "hi",
                                       "timestamp": 1.0}]}]

            def write_sessions(self, sessions):
                received.extend(sessions)
                return {"imported": 0, "updated": len(sessions),
                        "new_messages": 0}
        return FakeAdapter()

    def test_pull_keeps_server_title_when_local_untitled(self):
        """Sidecar anchored + local missing title: the incoming title must
        survive the merge (previously popped as 'dirty') and reach the
        adapter write."""
        received = []
        meta = {"s1": {"title": {"base": 1, "val": "Server title"}}}
        page = {"sessions": [{
            "id": "s1", "cwd": "D:/x", "title": "Server title",
            "field_rev": {"title": 1},
            "messages": [{"role": "user", "content": "hi",
                          "timestamp": 1.0}]}],
            "sync_at": 5.0, "total_sessions": 1}
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "meta.json"
            mp.write_text(json.dumps(meta), encoding="utf-8")
            with mock.patch.object(server, "adapter",
                                   self._fake_adapter(received)), \
                    mock.patch.object(server, "FIELD_META_PATH", mp), \
                    mock.patch.object(server, "PUSH_FINGERPRINT_PATH",
                                      td / "fp.json"), \
                    mock.patch.object(server, "api_call",
                                      return_value=page):
                r = server.pull_sessions()
        self.assertTrue(received, "adapter must have been written")
        for s in received:
            self.assertEqual(s["title"], "Server title",
                             "pull must not drop the server title")
        self.assertGreaterEqual(r.get("titles_healed", 0), 0)

    def test_reconcile_heals_idle_session_from_sidecar(self):
        """A session the server no longer re-serves recovers its title from
        the sidecar anchor (last server-accepted value); titled sessions and
        unknowns are untouched; second run is a no-op."""
        received = []
        meta = {"s1": {"title": {"base": 3, "val": "Server title"}},
                "s2": {"title": {"base": 4, "val": "Other title"}}}
        local_by_id = {
            "s1": {"id": "s1", "cwd": "D:/x",
                   "messages": [{"role": "user", "content": "hi",
                                 "timestamp": 1.0}]},
            "s2": {"id": "s2", "cwd": "D:/y", "title": "Has one",
                   "messages": []},
        }
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "meta.json"
            mp.write_text(json.dumps(meta), encoding="utf-8")
            with mock.patch.object(server, "adapter",
                                   self._fake_adapter(received)), \
                    mock.patch.object(server, "FIELD_META_PATH", mp):
                self.assertEqual(
                    server._reconcile_sidecar_titles(local_by_id), 1)
                # the adapter write landed the title: a fresh local read
                # would now see it, so the next run is a no-op
                local_by_id["s1"]["title"] = "Server title"
                self.assertEqual(
                    server._reconcile_sidecar_titles(local_by_id), 0)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["id"], "s1")
        self.assertEqual(received[0]["title"], "Server title")


class PullCompletenessRepairTest(unittest.TestCase):
    """A session deleted locally comes back on the next pull while the server
    still holds it visible (docs/ARCHITECTURE.md "本地删除不是删除信号"): the
    first page carries this device's inventory, the server answers with the
    visible ids we lack, and those are fetched by id and written like any
    other pulled page."""

    def _fake_adapter(self, received, local, stores_pulled=True,
                      watermark=1700000000.0):
        class FakeAdapter:
            agent_type = "dsh"
            stores_pulled_sessions = stores_pulled

            def discover(self):
                return "store"

            def last_synced_at(self):
                return watermark

            def save_sync_watermark(self, ts):
                pass

            def read_sessions(self):
                return [dict(s) for s in local]

            def write_sessions(self, sessions):
                received.extend(sessions)
                return {"imported": len(sessions), "updated": 0,
                        "new_messages": 0}
        return FakeAdapter()

    def _run_pull(self, adapter, responses, limit=None):
        calls = []

        def fake_api(method, path, data=None):
            calls.append(data)
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(server, "adapter", adapter), \
                    mock.patch.object(server, "FIELD_META_PATH",
                                      Path(td) / "meta.json"), \
                    mock.patch.object(server, "api_call", side_effect=fake_api):
                return server.pull_sessions(limit=limit), calls

    def test_deleted_session_is_restored_on_next_pull(self):
        received = []
        adapter = self._fake_adapter(received, [{"id": "kept", "messages": []}])
        page = {"sessions": [], "sync_at": 9.0, "total_sessions": 2,
                "missing_ids": ["gone"]}
        restore = {"sessions": [{"id": "gone", "title": "G",
                                 "messages": [{"role": "user",
                                               "content": "old",
                                               "timestamp": 1.0}]}],
                   "sync_at": 9.0, "total_sessions": 1}
        result, calls = self._run_pull(adapter, [page, restore])
        self.assertEqual(calls[0]["known_ids"], ["kept"])
        self.assertNotIn("known_ids", calls[1])
        self.assertEqual(calls[1]["ids"], ["gone"])
        self.assertEqual([s["id"] for s in received], ["gone"])
        self.assertEqual(result["restored"], 1)
        self.assertEqual(result["imported"], 1)

    def test_session_delivered_by_the_page_is_not_fetched_twice(self):
        received = []
        adapter = self._fake_adapter(received, [{"id": "kept", "messages": []}])
        page = {"sessions": [{"id": "gone", "messages": []}], "sync_at": 9.0,
                "total_sessions": 2, "missing_ids": ["gone"]}
        result, calls = self._run_pull(adapter, [page])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["restored"], 0)
        self.assertEqual([s["id"] for s in received], ["gone"])

    def test_unfiled_session_is_reported_not_counted_as_restored(self):
        # an adapter that cannot file the row (unknown profile, read-only
        # store) returns all-zero stats while the session stays absent:
        # the request must not be reported as a restoration
        received = []
        adapter = self._fake_adapter(received, [{"id": "kept", "messages": []}])
        adapter.write_sessions = lambda sessions: {"imported": 0, "updated": 0,
                                                  "new_messages": 0}
        page = {"sessions": [], "sync_at": 9.0, "total_sessions": 2,
                "missing_ids": ["gone"]}
        restore = {"sessions": [{"id": "gone", "title": "G", "messages": []}],
                   "sync_at": 9.0, "total_sessions": 1}
        result, _ = self._run_pull(adapter, [page, restore])
        self.assertEqual(result["restored"], 0)
        self.assertEqual(result["unfiled"], 1)
        self.assertEqual(result["imported"], 0)

    def test_read_only_adapter_never_asks_for_missing_sessions(self):
        # a read-only uploader has no local target for pulled sessions
        received = []
        adapter = self._fake_adapter(received, [{"id": "kept", "messages": []}],
                                     stores_pulled=False)
        page = {"sessions": [], "sync_at": 9.0, "total_sessions": 2,
                "missing_ids": ["gone"]}
        result, calls = self._run_pull(adapter, [page])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("known_ids", calls[0])
        self.assertEqual(result["restored"], 0)

    def test_full_pull_does_not_report_its_inventory(self):
        # a full pull already delivers every visible session; reporting the
        # pre-pull inventory as missing would download the store twice
        received = []
        adapter = self._fake_adapter(received, [{"id": "kept", "messages": []}],
                                     watermark=0.0)
        page = {"sessions": [], "sync_at": 9.0, "total_sessions": 2,
                "missing_ids": ["gone"]}
        result, calls = self._run_pull(adapter, [page])
        self.assertNotIn("known_ids", calls[0])
        self.assertEqual(calls[0]["last_sync_at"], 0)
        self.assertEqual(result["restored"], 0)

    def test_capped_pull_spends_its_limit_before_restoring(self):
        # `limit` stays authoritative: a capped pull never pulls more
        # sessions than the caller asked for
        received = []
        adapter = self._fake_adapter(received, [{"id": "kept", "messages": []}])
        page = {"sessions": [{"id": "new", "messages": []}], "sync_at": 9.0,
                "total_sessions": 2, "missing_ids": ["gone"]}
        result, calls = self._run_pull(adapter, [page], limit=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["restored"], 0)


class ProjectFieldMergeTest(unittest.TestCase):
    """Field-level optimistic merge for project scalar fields (Phase 2)."""

    def test_push_omits_non_dirty_keeps_id_and_folders(self):
        meta = {"p1": {"name": {"base": 3, "val": "P1"},
                       "primary_path": {"base": 1, "val": "D:/x"}}}
        p = {"id": "p1", "name": "P1", "primary_path": "D:/x", "slug": "s1",
             "folders": [{"path": "D:/a"}]}
        out = server._annotate_push_project(p, meta)
        self.assertEqual(out["slug"], "s1")            # non-edit kept
        self.assertEqual(out["folders"], [{"path": "D:/a"}])
        self.assertNotIn("name", out)                  # not dirty -> omitted
        self.assertNotIn("primary_path", out)
        self.assertEqual(out["field_meta"], {})

    def test_push_dirty_project_field_sends_with_base(self):
        meta = {"p1": {"name": {"base": 3, "val": "P1"},
                       "primary_path": {"base": 1, "val": "D:/x"}}}
        p = {"id": "p1", "name": "新名", "primary_path": "D:/x", "folders": []}
        out = server._annotate_push_project(p, meta)
        self.assertEqual(out["name"], "新名")           # dirty -> asserted
        self.assertNotIn("primary_path", out)          # not dirty -> omitted
        self.assertEqual(out["field_meta"], {"name": 3})

    def test_push_first_contact_project_uses_none_base(self):
        p = {"id": "p1", "name": "New", "folders": []}
        out = server._annotate_push_project(p, {})
        self.assertEqual(out["name"], "New")
        self.assertEqual(out["field_meta"], {"name": None})

    def test_anchor_project_records_only_accepted_fields(self):
        meta = {}
        chunk = [{"id": "p1", "name": "新名",
                  "field_meta": {"name": 3, "primary_path": None}}]
        revs = {"p1": {"rev": 7, "field_rev": {"name": 7, "primary_path": 2}}}
        server._anchor_push_project_meta(meta, chunk, revs)
        self.assertEqual(meta,
                         {"p1": {"name": {"base": 7, "val": "新名"}}})


class SyncLockAndRoleTest(unittest.TestCase):
    """Cross-process sync lock extended to mutating tool calls, and the
    startup-loser standby role (see the helpers above _SyncServer)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        lock = Path(self.tmp.name) / "hermes-sync.lock"
        self._orig = (server.LOCK_FILE, server.TOOL_LOCK_WAIT_S,
                      server.TOOL_LOCK_POLL_S, server._role,
                      server.AUTO_SYNC)
        server.LOCK_FILE = lock
        server.TOOL_LOCK_WAIT_S = 0.3
        server.TOOL_LOCK_POLL_S = 0.02
        server._role = "starting"
        server.AUTO_SYNC = True

    def tearDown(self):
        (server.LOCK_FILE, server.TOOL_LOCK_WAIT_S, server.TOOL_LOCK_POLL_S,
         server._role, server.AUTO_SYNC) = self._orig

    @staticmethod
    def _run(coro):
        return asyncio.run(coro)

    def test_acquire_release_roundtrip(self):
        self.assertTrue(self._run(server._acquire_tool_lock()))
        self.assertEqual(server._lock_holder_pid(), os.getpid())
        server._release_lock()
        self.assertIsNone(server._lock_holder_pid())

    def test_stale_lock_stolen(self):
        # os.kill(pid, 0) liveness checks are platform-dependent; patch the
        # probe so the test asserts the steal logic, not Windows semantics.
        server.LOCK_FILE.write_text("424242")
        with mock.patch.object(server, "_pid_alive", return_value=False):
            self.assertTrue(self._run(server._acquire_tool_lock()))
        self.assertEqual(server._lock_holder_pid(), os.getpid())
        server._release_lock()

    def test_foreign_live_holder_busy_after_wait(self):
        async def go():
            loop = asyncio.get_event_loop()
            server.LOCK_FILE.write_text(str(os.getppid()))
            out = await server._locked_tool(loop, lambda: "ran")
            return out

        out = self._run(go())
        self.assertEqual(out.get("error"), "sync_busy")
        self.assertNotEqual(out.get("detail"), "ran")
        # foreign holder untouched; nothing leaked into the file
        self.assertEqual(server._lock_holder_pid(), os.getppid())

    def test_locked_tool_runs_and_releases(self):
        async def go():
            loop = asyncio.get_event_loop()
            out = await server._locked_tool(loop, lambda: {"done": 1})
            return out, server._lock_holder_pid()

        out, holder_after = self._run(go())
        self.assertEqual(out, {"done": 1})
        self.assertIsNone(holder_after)

    def test_own_cycle_waits_then_runs(self):
        # Lock file holds OUR pid (background cycle in progress): the tool
        # must wait for the cycle to release, then acquire and run.
        async def go():
            loop = asyncio.get_event_loop()
            server.LOCK_FILE.write_text(str(os.getpid()))

            async def release_later():
                await asyncio.sleep(0.05)
                server._release_lock()

            asyncio.get_event_loop().create_task(release_later())
            out = await server._locked_tool(loop, lambda: {"done": 2})
            return out

        self.assertEqual(self._run(go()), {"done": 2})

    def test_periodic_sync_standby_returns_immediately(self):
        server._role = "standby"
        # would loop forever as primary; standby must return at once
        self._run(server.periodic_sync())

    def test_periodic_sync_disabled_flag_returns_immediately(self):
        server.AUTO_SYNC = False
        self._run(server.periodic_sync())


class PushIsolationTest(unittest.TestCase):
    """A session whose payload cannot be JSON-encoded (raw bytes leaked from
    a SQLite BLOB column, ...) must not abort the whole push cycle. Both the
    chunker and the request encoder serialize every session, so one bad value
    used to kill the cycle before anything was sent -- an entire device
    stopped syncing (CHANGELOG 2026.09.12.4). The offender is skipped and
    named; every other session still syncs."""

    @staticmethod
    def _session(sid: str, blob: bool = False) -> dict:
        msg = {"session_id": sid, "role": "user", "content": "hi",
               "timestamp": 1.0}
        if blob:
            msg["display_identity"] = bytes(range(32))
        return {"id": sid, "cwd": "c:/x", "title": "t", "messages": [msg]}

    @staticmethod
    def _adapter(sessions: list[dict]):
        class FakeAdapter:
            agent_type = "workbuddy"

            def discover(self):
                return "store"

            def read_sessions(self):
                return sessions

            def _is_foreign(self, sid):
                return False

            def _foreign_agent(self, sid):
                return None
        return FakeAdapter()

    def test_unsendable_session_is_skipped_while_the_rest_syncs(self):
        sessions = [self._session("bad", blob=True), self._session("good")]
        calls = []
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with mock.patch.object(server, "adapter", self._adapter(sessions)), \
                    mock.patch.object(server, "FIELD_META_PATH",
                                      td / "meta.json"), \
                    mock.patch.object(server, "PUSH_FINGERPRINT_PATH",
                                      td / "fp.json"), \
                    mock.patch.object(
                        server, "api_call",
                        side_effect=lambda *a, **k: calls.append(a) or {
                            "imported": 0, "updated": 1, "new_messages": 0,
                            "sync_at": 1.0, "session_revs": {}}):
                result = server.push_sessions()
                # the encodable session after the bad one still went out
                sent = [s["id"] for call in calls
                        for s in call[2]["sessions"]]
                self.assertEqual(sent, ["good"])
                # a skipped session is not a failed cycle
                self.assertNotIn("error", result)
                self.assertEqual(result["unsendable"], ["bad"])
                # and the good one anchored (not re-sent next cycle)
                self.assertEqual(list(server._load_push_fingerprint()),
                                 ["good"])

    def test_reason_names_the_offending_field(self):
        sendable, reasons = server._partition_encodable(
            [self._session("bad", blob=True), self._session("good")])
        self.assertEqual([s["id"] for s in sendable], ["good"])
        self.assertEqual(reasons,
                         ["bad (messages[0].display_identity (bytes))"])

    def test_api_call_reports_an_unencodable_payload_instead_of_raising(self):
        result = server.api_call("POST", "/push",
                                 {"sessions": [{"b": bytes(4)}]})
        self.assertIn("error", result)
        self.assertIn("bytes", str(result["error"]))


if __name__ == "__main__":
    unittest.main()
