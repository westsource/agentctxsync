"""Round-trip tests for the openclaw adapter (gateway jsonl store)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.openclaw import OpenClawAdapter  # noqa: E402


def make_store(dir_path: Path):
    """Fixture OpenClaw store: one populated session + one empty transcript."""
    d = Path(dir_path)
    d.mkdir(parents=True, exist_ok=True)
    s1 = "11111111-1111-1111-1111-111111111111"
    s2 = "22222222-2222-2222-2222-222222222222"
    lines = [
        {"type": "session", "version": 3, "id": s1,
         "timestamp": "2026-08-27T02:00:00.000Z", "cwd": "C:\\ws"},
        {"type": "model_change", "id": "a0000001", "parentId": None,
         "timestamp": "2026-08-27T02:00:00.000Z", "provider": "opencode-go",
         "modelId": "kimi-k2.6"},
        {"type": "message", "id": "a0000002", "parentId": "a0000001",
         "timestamp": "2026-08-27T02:00:01.000Z",
         "message": {"role": "user", "content": "你好",
                     "timestamp": 1787709601000}},
        {"type": "message", "id": "a0000003", "parentId": "a0000002",
         "timestamp": "2026-08-27T02:00:02.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "嗨！"}],
                     "timestamp": 1787709602000}},
    ]
    (d / f"{s1}.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    (d / f"{s2}.jsonl").write_text(
        json.dumps({"type": "session", "version": 3, "id": s2,
                    "timestamp": "2026-08-27T01:00:00.000Z", "cwd": "C:\\ws"})
        + "\n", encoding="utf-8")
    index = {
        "agent:main:main": {
            "sessionId": s1, "sessionFile": str(d / f"{s1}.jsonl"),
            "sessionStartedAt": 1787709600000, "updatedAt": 1787709602000,
            "lastInteractionAt": 1787709602000,
            "lastActivityAt": 1787709602000, "agentHarnessId": "openclaw"},
        "agent:main:empty": {
            "sessionId": s2, "sessionFile": str(d / f"{s2}.jsonl"),
            "sessionStartedAt": 1787706000000, "updatedAt": 1787706000000,
            "lastInteractionAt": 1787706000000,
            "lastActivityAt": 1787706000000, "agentHarnessId": "openclaw"},
    }
    (d / "sessions.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")


class OpenClawAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "sessions"
        make_store(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read(self):
        a = OpenClawAdapter(store_dir=self.store)
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)  # empty transcript skipped
        s = sessions[0]
        # own sessions carry the transcript UUID (server-safe id)
        self.assertEqual(s["id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(s["meta"]["openclaw:session_key"], "agent:main:main")
        self.assertEqual(s["started_at"], 1787709600.0)
        self.assertEqual(len(s["messages"]), 2)
        self.assertEqual(s["messages"][0]["content"], "你好")
        self.assertEqual(s["messages"][1]["content"], "嗨！")
        self.assertEqual(s["agent_type"], "openclaw")
        self.assertEqual(s["title"], "你好")

    def test_status(self):
        a = OpenClawAdapter(store_dir=self.store)
        st = a.status()
        self.assertEqual(st["sessions"], 2)
        self.assertEqual(st["messages"], 2)

    def test_write_and_dedupe(self):
        a = OpenClawAdapter(store_dir=self.store)
        foreign = [{
            "id": "codex:conv-9", "started_at": 1787709600.0,
            "title": "From codex", "agent_type": "codex",
            "messages": [
                {"session_id": "codex:conv-9", "role": "user",
                 "content": "from codex", "timestamp": 1787709600.5}]}]
        first = a.write_sessions(foreign)
        self.assertEqual(first["imported"], 1)
        self.assertEqual(first["new_messages"], 1)
        second = a.write_sessions(foreign)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(second["new_messages"], 0)
        a2 = OpenClawAdapter(store_dir=self.store)
        ids = [s["id"] for s in a2.read_sessions()]
        self.assertIn("codex:conv-9", ids)
        self.assertTrue(a2._is_foreign("codex:conv-9"))
        self.assertEqual(a2._foreign_agent("codex:conv-9"), "codex")

    def test_append_to_existing(self):
        a = OpenClawAdapter(store_dir=self.store)
        r = a.write_sessions([{
            "id": "agent:main:main", "started_at": 1787709600.0,
            "messages": [
                {"session_id": "agent:main:main", "role": "user",
                 "content": "新消息", "timestamp": 1787709700.0}]}])
        self.assertEqual(r["updated"], 1)
        self.assertEqual(r["new_messages"], 1)
        s = OpenClawAdapter(store_dir=self.store).read_sessions()[0]
        self.assertEqual(len(s["messages"]), 3)
        self.assertEqual(s["messages"][-1]["content"], "新消息")

    def test_invalid_id_skipped(self):
        a = OpenClawAdapter(store_dir=self.store)
        r = a.write_sessions([{
            "id": "../escape", "started_at": 1.0,
            "messages": [{"role": "user", "content": "x", "timestamp": 1.0}]}])
        self.assertEqual(r["imported"], 0)

    def test_timestamp_precision_dedupe(self):
        """Server doubles near ms boundaries must not re-append."""
        a = OpenClawAdapter(store_dir=self.store)
        ts = 1787709600.0529997
        foreign = [{
            "id": "codex:prec-1", "started_at": ts,
            "agent_type": "codex",
            "messages": [
                {"session_id": "codex:prec-1", "role": "user",
                 "content": "prec", "timestamp": ts}]}]
        first = a.write_sessions(foreign)
        self.assertEqual(first["new_messages"], 1)
        second = a.write_sessions(foreign)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(second["new_messages"], 0)

    def test_surrogate_content_written_safely(self):
        """Lone surrogates in server content must not corrupt the jsonl."""
        a = OpenClawAdapter(store_dir=self.store)
        r = a.write_sessions([{
            "id": "codex:bin-1", "started_at": 1787709600.0,
            "agent_type": "codex",
            "messages": [{"session_id": "codex:bin-1", "role": "tool",
                          "content": "binary\uD800text",
                          "timestamp": 1787709601.0}]}])
        self.assertEqual(r["new_messages"], 1)
        index = json.loads(
            (self.store / "sessions.json").read_text(encoding="utf-8"))
        f = Path(index["codex:bin-1"]["sessionFile"])
        for line in f.read_text(encoding="utf-8").splitlines():
            json.loads(line)  # every line must parse
        s = [x for x in OpenClawAdapter(store_dir=self.store).read_sessions()
             if x["id"] == "codex:bin-1"][0]
        self.assertEqual(s["messages"][0]["content"], "binary\uFFFDtext")

    def test_line_separator_content_safe(self):
        """U+2028/U+2029 in content must not split the JSONL line."""
        a = OpenClawAdapter(store_dir=self.store)
        r = a.write_sessions([{
            "id": "codex:sep-1", "started_at": 1787709600.0,
            "agent_type": "codex",
            "messages": [{"session_id": "codex:sep-1", "role": "tool",
                          "content": "a\u2028b\u2029c",
                          "timestamp": 1787709601.0}]}])
        self.assertEqual(r["new_messages"], 1)
        index = json.loads(
            (self.store / "sessions.json").read_text(encoding="utf-8"))
        f = Path(index["codex:sep-1"]["sessionFile"])
        lines = f.read_text(encoding="utf-8").splitlines()
        for line in lines:
            json.loads(line)  # every line parses as one JSON value
        self.assertEqual(len(lines), 4)  # header + model + thinking + message
        s = [x for x in OpenClawAdapter(store_dir=self.store).read_sessions()
             if x["id"] == "codex:sep-1"][0]
        self.assertEqual(s["messages"][0]["content"], "a\u2028b\u2029c")

    def test_pooled_session_roundtrips_server_id(self):
        """A pooled session keeps its server id so push updates the row."""
        a = OpenClawAdapter(store_dir=self.store)
        pooled = [{
            "id": "20260801_201638_8ab26b", "started_at": 1785586599.0,
            "agent_type": "hermes",
            "messages": [{"session_id": "20260801_201638_8ab26b",
                          "role": "user",
                          "content": "帮我规划学习道家针灸的路径",
                          "timestamp": 1785586599.0}]}]
        r = a.write_sessions(pooled)
        self.assertEqual(r["imported"], 1)
        a2 = OpenClawAdapter(store_dir=self.store)
        out = [x for x in a2.read_sessions()
               if x["id"] == "20260801_201638_8ab26b"]
        self.assertEqual(len(out), 1)  # server id, never a uuid
        self.assertEqual(out[0]["agent_type"], "hermes")
        self.assertTrue(a2._is_foreign("20260801_201638_8ab26b"))
        self.assertEqual(a2._foreign_agent("20260801_201638_8ab26b"),
                         "hermes")
        # continuing the session and re-pushing updates the same row
        r2 = a2.write_sessions([{
            "id": "20260801_201638_8ab26b", "started_at": 1785586599.0,
            "agent_type": "hermes",
            "messages": [{"session_id": "20260801_201638_8ab26b",
                          "role": "user", "content": "有没有更快的办法",
                          "timestamp": 1785638410.0}]}])
        self.assertEqual(r2["updated"], 1)
        self.assertEqual(r2["new_messages"], 1)
        out2 = [x for x in OpenClawAdapter(
            store_dir=self.store).read_sessions()
            if x["id"] == "20260801_201638_8ab26b"][0]
        self.assertEqual(len(out2["messages"]), 2)

    def test_gateway_rewrite_key_derived_id(self):
        """A running gateway may strip meta and prefix keys with agent:main:;
        the adapter must still derive the server id from the key alone."""
        index = json.loads(
            (self.store / "sessions.json").read_text(encoding="utf-8"))
        entry = index.pop("agent:main:main")
        entry.pop("meta", None)  # gateway stripped adapter meta
        index["agent:main:20260801_201638_8ab26b"] = entry
        (self.store / "sessions.json").write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8")
        (self.store / ".hermes-sync-foreign-ids.json").write_text(
            json.dumps({"20260801_201638_8ab26b": "hermes"}),
            encoding="utf-8")
        a = OpenClawAdapter(store_dir=self.store)
        out = [x for x in a.read_sessions()
               if x["id"] == "20260801_201638_8ab26b"]
        self.assertEqual(len(out), 1)  # pooled id, not uuid
        self.assertEqual(out[0]["agent_type"], "hermes")
        # local-native slug still uses the transcript uuid
        index["agent:main:test1"] = {
            "sessionId": "33333333-3333-3333-3333-333333333333",
            "sessionFile": str(self.store / "33333333-3333-3333-3333-333333333333.jsonl"),
            "sessionStartedAt": 1787709600000, "updatedAt": 1787709602000,
            "agentHarnessId": "openclaw",
        }
        (self.store / "33333333-3333-3333-3333-333333333333.jsonl").write_text(
            json.dumps({"type": "session", "version": 3,
                        "id": "33333333-3333-3333-3333-333333333333",
                        "timestamp": "2026-08-27T02:00:00.000Z",
                        "cwd": "C:\\ws"}) + "\n" +
            json.dumps({"type": "message", "id": "b0000001", "parentId": None,
                        "timestamp": "2026-08-27T02:00:01.000Z",
                        "message": {"role": "user", "content": "本地会话",
                                    "timestamp": 1787709601000}}) + "\n",
            encoding="utf-8")
        (self.store / "sessions.json").write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8")
        out2 = [x for x in OpenClawAdapter(
            store_dir=self.store).read_sessions()
            if x["meta"].get("openclaw:session_key") == "agent:main:test1"]
        self.assertEqual(len(out2), 1)
        self.assertEqual(out2[0]["id"], "33333333-3333-3333-3333-333333333333")
        self.assertEqual(out2[0]["agent_type"], "openclaw")

    def test_pull_maps_gateway_prefixed_key(self):
        """A bare server id must update the agent:main:-prefixed local key
        the gateway created, not import a duplicate."""
        index = json.loads(
            (self.store / "sessions.json").read_text(encoding="utf-8"))
        entry = index.pop("agent:main:main")
        index["agent:main:20260612_160041_24849d"] = entry
        (self.store / "sessions.json").write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8")
        a = OpenClawAdapter(store_dir=self.store)
        r = a.write_sessions([{
            "id": "20260612_160041_24849d", "started_at": 1785586599.0,
            "agent_type": "hermes",
            "messages": []}])
        self.assertEqual(r["updated"], 1)
        self.assertEqual(r["imported"], 0)
        index2 = json.loads(
            (self.store / "sessions.json").read_text(encoding="utf-8"))
        self.assertIn("agent:main:20260612_160041_24849d", index2)
        self.assertNotIn("20260612_160041_24849d", index2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
