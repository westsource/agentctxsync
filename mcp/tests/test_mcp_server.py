"""Tests for the MCP client's batching logic (mcp/server.py).

Sessions are pushed in small batches bounded by session count AND total
message count, so a giant request cannot exceed the HTTP timeout during a
full sync (a full resync pulls/pushes every session on the server).
"""

import asyncio
import sys
import unittest
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

    def test_default_limits(self):
        chunks = server._chunk_sessions([mk(100)] * 25)
        self.assertEqual([len(c) for c in chunks], [20, 5])


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


if __name__ == "__main__":
    unittest.main()
