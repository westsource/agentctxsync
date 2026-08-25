"""Tests for the MCP client's batching logic (mcp/server.py).

Sessions are pushed in small batches bounded by session count AND total
message count, so a giant request cannot exceed the HTTP timeout during a
full sync (a full resync pulls/pushes every session on the server).
"""

import asyncio
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
                r2 = server.push_sessions()
                self.assertEqual(len(results), 0)  # retried and consumed
                self.assertEqual(r2["updated"], 1)


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


if __name__ == "__main__":
    unittest.main()
