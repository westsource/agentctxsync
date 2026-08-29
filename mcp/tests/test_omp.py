"""Round-trip tests for the omp (Oh My Pi) adapter (mcp/adapters/omp.py).

Covers read (omp title-slot + header + message stream), write round-trip
(title slot first, header, parentId chain), idempotency, append, foreign-id
mapping to a fresh UUIDv7 via the idmap sidecar, and status counts.
"""
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.omp import OmpAdapter  # noqa: E402

TS = "2026-08-28T01-04-14.489Z"
SID = "01a045e5-5c99-75ad-abae-91b14594f6ad"


def _iso(ms: int) -> str:
    return (datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z")


def write_omp_fixture(root: Path, sid: str = SID):
    """omp-style store: title record first + omp header + omp model_change."""
    d = root / "--E--OpenCode-agentctxsync--"
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


class OmpAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _session(self, **over):
        sess = {"id": SID, "started_at": 1787647183.0,
                "cwd": "E:\\OpenCode\\agentctxsync", "title": "Omp Chat",
                "messages": [
                    {"session_id": SID, "role": "user",
                     "content": "hello omp", "timestamp": 1787647183.0},
                    {"session_id": SID, "role": "assistant",
                     "content": "hey", "timestamp": 1787647185.0},
                ]}
        sess.update(over)
        return sess

    def test_read_omp_format(self):
        write_omp_fixture(self.root)
        a = OmpAdapter(sessions_root=self.root)
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], SID)
        self.assertEqual(s["cwd"], "E:\\OpenCode\\agentctxsync")
        self.assertEqual(s["title"], "Omp Chat")
        self.assertEqual(len(s["messages"]), 2)
        self.assertEqual(s["messages"][0]["role"], "user")
        self.assertEqual(s["messages"][0]["content"], "hello omp")
        self.assertEqual(s["messages"][1]["role"], "assistant")
        self.assertEqual(s["messages"][1]["content"], "hey")

    def test_write_new_session_roundtrip(self):
        a = OmpAdapter(sessions_root=self.root)
        stats = a.write_sessions([self._session()])
        self.assertEqual(stats["imported"], 1)
        self.assertEqual(stats["new_messages"], 2)
        d = self.root / "--E--OpenCode-agentctxsync--"
        files = list(d.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        lines = [json.loads(l) for l in
                 files[0].read_text(encoding="utf-8").splitlines()]
        # line 1: fixed-width title slot, then header, then messages
        self.assertEqual(lines[0]["type"], "title")
        self.assertEqual(lines[0]["title"], "Omp Chat")
        self.assertEqual(lines[1]["type"], "session")
        self.assertEqual(lines[1]["id"], SID)
        self.assertEqual(lines[1]["version"], 3)
        msgs = [l for l in lines if l["type"] == "message"]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]["parentId"], msgs[0]["id"])  # chain
        # round-trip read preserves title + reasoning
        back = a.read_sessions()[0]
        self.assertEqual(back["title"], "Omp Chat")
        self.assertEqual(back["messages"][0]["content"], "hello omp")

    def test_write_idempotent_no_duplicate_messages(self):
        a = OmpAdapter(sessions_root=self.root)
        a.write_sessions([self._session()])
        stats2 = a.write_sessions([self._session()])
        self.assertEqual(stats2["new_messages"], 0)
        self.assertGreaterEqual(stats2["duplicates"], 1)
        back = a.read_sessions()[0]
        self.assertEqual(len(back["messages"]), 2)

    def test_write_appends_new_message(self):
        a = OmpAdapter(sessions_root=self.root)
        base = self._session(messages=[
            {"session_id": SID, "role": "user",
             "content": "hello omp", "timestamp": 1787647183.0}])
        a.write_sessions([base])
        base["messages"].append(
            {"session_id": SID, "role": "assistant",
             "content": "world", "timestamp": 1787647190.0})
        stats = a.write_sessions([base])
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["new_messages"], 1)
        back = a.read_sessions()[0]
        self.assertEqual(len(back["messages"]), 2)

    def test_foreign_id_maps_to_fresh_local_uuid(self):
        a = OmpAdapter(sessions_root=self.root)
        foreign = "20260801_201638_8ab26b"  # non-uuid (foreign agent)
        sess = self._session(id=foreign, cwd="C:\\tmp", started_at=1.0,
                             messages=[{"session_id": foreign, "role": "user",
                                        "content": "z", "timestamp": 2.0}])
        stats = a.write_sessions([sess])
        self.assertEqual(stats["imported"], 1)
        idmap = json.loads((self.root / ".omp-sync-idmap.json")
                           .read_text(encoding="utf-8"))
        self.assertIn(foreign, idmap)
        # round-trip read returns the canonical (foreign) id
        back = [s for s in a.read_sessions() if s["id"] == foreign]
        self.assertEqual(len(back), 1)
        # the on-disk header uses the fresh local uuid, never the foreign id
        for f in (self.root / "--C--tmp--").glob("*.jsonl"):
            header = json.loads(f.read_text(encoding="utf-8").splitlines()[1])
            self.assertEqual(header["id"], idmap[foreign])

    def test_status_counts(self):
        write_omp_fixture(self.root)
        a = OmpAdapter(sessions_root=self.root)
        st = a.status()
        self.assertEqual(st["sessions"], 1)
        self.assertGreaterEqual(st["messages"], 2)


if __name__ == "__main__":
    unittest.main()
