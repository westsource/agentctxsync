"""Round-trip tests for the pi/omp adapter (shared JSONL v3 session store)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.pi import OmpAdapter, PiAdapter, _encode_cwd  # noqa: E402

TS = "2026-08-28T01-04-14.489Z"


def _iso(ms: int) -> str:
    import datetime
    return (datetime.datetime.fromtimestamp(ms / 1000,
                                            datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z")


def write_pi_fixture(home: Path, sid: str = "01a03812-55d3-7000-a26f-b682260018f6"):
    """pi-style store: header + pi model_change + messages + session_info."""
    d = home / "sessions" / "--E--OpenCode-agentctxsync--"
    d.mkdir(parents=True, exist_ok=True)
    ms = 1787647183000
    lines = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": TS, "cwd": "E:\\OpenCode\\agentctxsync"},
        {"type": "model_change", "id": "aaaa0001", "parentId": None,
         "timestamp": _iso(ms), "provider": "openai", "modelId": "gpt-4o"},
        {"type": "message", "id": "aaaa0002", "parentId": "aaaa0001",
         "timestamp": _iso(ms),
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "hello pi"}],
                     "attribution": "user", "timestamp": ms}},
        {"type": "message", "id": "aaaa0003", "parentId": "aaaa0002",
         "timestamp": _iso(ms + 1000),
         "message": {"role": "assistant",
                     "content": [{"type": "thinking",
                                  "thinking": "let me think"},
                                 {"type": "text", "text": "hi there"}],
                     "timestamp": ms + 1000}},
        {"type": "custom", "customType": "tool_execution_start",
         "id": "aaaa0004", "parentId": "aaaa0003",
         "timestamp": _iso(ms + 2000),
         "data": {"toolName": "read", "args": {"path": "x"}}},
        {"type": "session_info", "id": "aaaa0005", "parentId": "aaaa0003",
         "timestamp": _iso(ms + 3000), "name": "Pi Chat"},
    ]
    p = d / f"2026-08-28T01-04-14-489Z_{sid}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def write_omp_fixture(home: Path, sid: str = "01a045e5-5c99-75ad-abae-91b14594f6ad"):
    """omp-style store: title record first + omp header + omp model_change."""
    d = home / "sessions" / "--E--OpenCode-agentctxsync--"
    d.mkdir(parents=True, exist_ok=True)
    ms = 1787647183000
    lines = [
        {"type": "title", "v": 1, "title": "Omp Chat", "source": "auto",
         "updatedAt": _iso(ms)},
        {"type": "session", "version": 3, "id": sid,
         "timestamp": TS, "cwd": "E:\\OpenCode\\agentctxsync",
         "title": "Omp Chat", "titleSource": "auto"},
        {"type": "model_change", "id": "bbbb0001", "parentId": None,
         "timestamp": _iso(ms), "model": "opencode-go/deepseek-v4-flash",
         "resolvedModelIsFallback": False},
        {"type": "message", "id": "bbbb0002", "parentId": "bbbb0001",
         "timestamp": _iso(ms),
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "hello omp"}],
                     "timestamp": ms}},
        {"type": "title_change", "id": "bbbb0003", "parentId": "bbbb0002",
         "timestamp": _iso(ms + 1000), "title": "Omp Chat", "source": "auto"},
        {"type": "message", "id": "bbbb0004", "parentId": "bbbb0003",
         "timestamp": _iso(ms + 2000),
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "hey"}],
                     "timestamp": ms + 2000}},
    ]
    p = d / f"2026-08-28T01-04-14-489Z_{sid}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


class PiAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_encode_cwd(self):
        self.assertEqual(_encode_cwd(r"E:\OpenCode\agentctxsync"),
                         "--E--OpenCode-agentctxsync--")
        self.assertEqual(_encode_cwd("E:\\"), "--E----")
        self.assertEqual(_encode_cwd("/home/user/proj"), "--home-user-proj--")

    def test_read_pi_format(self):
        write_pi_fixture(self.home)
        a = PiAdapter(store_dir=self.home / "sessions")
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], "01a03812-55d3-7000-a26f-b682260018f6")
        self.assertEqual(s["cwd"], "E:\\OpenCode\\agentctxsync")
        self.assertEqual(s["model"], "gpt-4o")
        self.assertEqual(s["title"], "Pi Chat")  # from session_info
        self.assertGreater(s["started_at"], 0)
        msgs = s["messages"]
        # custom entry skipped; 2 real messages
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "hello pi")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["content"], "hi there")
        self.assertEqual(msgs[1]["reasoning"], "let me think")
        for m in msgs:
            self.assertEqual(m["session_id"], s["id"])

    def test_read_omp_format(self):
        write_omp_fixture(self.home)
        a = OmpAdapter(store_dir=self.home / "sessions")
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], "01a045e5-5c99-75ad-abae-91b14594f6ad")
        self.assertEqual(s["model"], "opencode-go/deepseek-v4-flash")
        self.assertEqual(s["title"], "Omp Chat")
        self.assertEqual(len(s["messages"]), 2)

    def test_write_new_session_roundtrip(self):
        home = self.home
        a = PiAdapter(store_dir=home / "sessions")
        sid = "01a03812-55d3-7000-a26f-b682260018f6"
        sess = {"id": sid, "started_at": 1787647183.0,
                "cwd": "E:\\OpenCode\\agentctxsync", "title": "Round Trip",
                "messages": [
                    {"session_id": sid, "role": "user",
                     "content": "hello", "timestamp": 1787647183.0},
                    {"session_id": sid, "role": "assistant",
                     "content": "world", "reasoning": "think",
                     "timestamp": 1787647184.0},
                ]}
        stats = a.write_sessions([sess])
        self.assertEqual(stats["imported"], 1)
        self.assertEqual(stats["new_messages"], 2)
        # file lives under the encoded cwd dir, header first
        p = home / "sessions" / "--E--OpenCode-agentctxsync--" / f"{sid}.jsonl"
        files = list((home / "sessions" / "--E--OpenCode-agentctxsync--").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        first = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["type"], "session")
        self.assertEqual(first["id"], sid)
        self.assertEqual(first["version"], 3)
        # title event is a pi session_info entry
        tail = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(tail["type"], "session_info")
        self.assertEqual(tail["name"], "Round Trip")
        # round-trip read
        back = a.read_sessions()
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["title"], "Round Trip")
        self.assertEqual(back[0]["messages"][0]["content"], "hello")
        self.assertEqual(back[0]["messages"][1]["reasoning"], "think")
        # parentId chain: message 2's parent is message 1's id
        lines = [json.loads(l) for l in
                 files[0].read_text(encoding="utf-8").splitlines()]
        msgs = [l for l in lines if l["type"] == "message"]
        self.assertEqual(msgs[1]["parentId"], msgs[0]["id"])

    def test_omp_title_event_type(self):
        home = self.home
        a = OmpAdapter(store_dir=home / "sessions")
        sid = "01a045e5-5c99-75ad-abae-91b14594f6ad"
        sess = {"id": sid, "started_at": 1787647183.0,
                "cwd": "E:\\OpenCode\\agentctxsync", "title": "Omp T",
                "messages": [
                    {"session_id": sid, "role": "user",
                     "content": "hi", "timestamp": 1787647183.0}]}
        a.write_sessions([sess])
        files = list((home / "sessions" / "--E--OpenCode-agentctxsync--").glob("*.jsonl"))
        tail = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(tail["type"], "title_change")
        self.assertEqual(tail["title"], "Omp T")

    def test_write_idempotent(self):
        home = self.home
        a = PiAdapter(store_dir=home / "sessions")
        sid = "01a03812-55d3-7000-a26f-b682260018f6"
        sess = {"id": sid, "started_at": 1787647183.0,
                "cwd": "E:\\OpenCode\\agentctxsync", "title": "Idem",
                "messages": [
                    {"session_id": sid, "role": "user",
                     "content": "hello", "timestamp": 1787647183.0}]}
        a.write_sessions([sess])
        stats2 = a.write_sessions([sess])
        self.assertEqual(stats2["imported"], 0)
        self.assertEqual(stats2["new_messages"], 0)
        self.assertEqual(stats2["duplicates"], 1)
        back = a.read_sessions()
        self.assertEqual(len(back[0]["messages"]), 1)

    def test_write_append_new_message(self):
        home = self.home
        a = PiAdapter(store_dir=home / "sessions")
        sid = "01a03812-55d3-7000-a26f-b682260018f6"
        base = {"id": sid, "started_at": 1787647183.0,
                "cwd": "E:\\OpenCode\\agentctxsync", "title": "Append",
                "messages": [
                    {"session_id": sid, "role": "user",
                     "content": "hello", "timestamp": 1787647183.0}]}
        a.write_sessions([base])
        base["messages"].append(
            {"session_id": sid, "role": "assistant",
             "content": "world", "timestamp": 1787647184.0})
        stats = a.write_sessions([base])
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["new_messages"], 1)
        self.assertEqual(stats["duplicates"], 1)
        back = a.read_sessions()
        self.assertEqual(len(back[0]["messages"]), 2)

    def test_foreign_session_registered_and_skipped(self):
        home = self.home
        a = PiAdapter(store_dir=home / "sessions")
        foreign = "20260801_201638_8ab26b"  # hermes-style id
        sess = {"id": foreign, "started_at": 1787647183.0,
                "agent_type": "hermes",
                "messages": [
                    {"session_id": foreign, "role": "user",
                     "content": "from hermes", "timestamp": 1787647183.0}]}
        a.write_sessions([sess])
        # foreign registry records the owner
        reg = json.loads((home / "sessions" / ".hermes-sync-foreign-ids.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(reg[foreign], "hermes")
        # round-trip read keeps the bare id
        back = a.read_sessions()
        self.assertEqual(back[0]["id"], foreign)
        # hostile id with a path separator is refused
        bad = {"id": "..\\evil", "started_at": 1.0,
               "messages": [{"session_id": "..\\evil", "role": "user",
                             "content": "x", "timestamp": 1.0}]}
        stats = a.write_sessions([bad])
        self.assertEqual(stats["imported"], 0)
        self.assertEqual(stats["new_messages"], 0)

    def test_same_ms_messages_get_unique_triples(self):
        home = self.home
        d = home / "sessions" / "--C--tmp--"
        d.mkdir(parents=True, exist_ok=True)
        sid = "01a00001-5464-7000-bd46-a6506a4e7081"
        ms = 1787647183000
        lines = [
            {"type": "session", "version": 3, "id": sid,
             "timestamp": TS, "cwd": "C:\\tmp"},
            {"type": "message", "id": "cccc0001", "parentId": None,
             "timestamp": _iso(ms),
             "message": {"role": "user", "content": [{"type": "text",
                          "text": "ask one"}], "timestamp": ms}},
            {"type": "message", "id": "cccc0002", "parentId": "cccc0001",
             "timestamp": _iso(ms),
             "message": {"role": "user", "content": [{"type": "text",
                          "text": "ask two (rewind)"}], "timestamp": ms}},
        ]
        (d / f"2026-08-28T01-04-14-489Z_{sid}.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        a = PiAdapter(store_dir=home / "sessions")
        msgs = a.read_sessions()[0]["messages"]
        self.assertEqual(len(msgs), 2)
        triples = {(m["role"], round(m["timestamp"], 3)) for m in msgs}
        self.assertEqual(len(triples), 2)  # deterministically disambiguated

    def test_compaction_becomes_summary_message(self):
        home = self.home
        d = home / "sessions" / "--C--tmp--"
        d.mkdir(parents=True, exist_ok=True)
        sid = "01a00001-5464-7000-bd46-a6506a4e7081"
        lines = [
            {"type": "session", "version": 3, "id": sid,
             "timestamp": TS, "cwd": "C:\\tmp"},
            {"type": "message", "id": "dddd0001", "parentId": None,
             "timestamp": _iso(1787647183000),
             "message": {"role": "user", "content": [{"type": "text",
                          "text": "old"}], "timestamp": 1787647183000}},
            {"type": "compaction", "id": "dddd0002", "parentId": "dddd0001",
             "timestamp": _iso(1787647184000),
             "summary": "previous turns summarized",
             "firstKeptEntryId": "dddd0001", "tokensBefore": 123},
        ]
        (d / f"2026-08-28T01-04-14-489Z_{sid}.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        a = PiAdapter(store_dir=home / "sessions")
        msgs = a.read_sessions()[0]["messages"]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]["content"], "previous turns summarized")
        self.assertEqual(msgs[1]["meta"]["pi:entry_type"], "compaction")

    def test_status_counts(self):
        write_pi_fixture(self.home)
        write_omp_fixture(self.home)
        a = PiAdapter(store_dir=self.home / "sessions")
        st = a.status()
        self.assertEqual(st["sessions"], 2)
        self.assertGreaterEqual(st["messages"], 4)


if __name__ == "__main__":
    unittest.main()
