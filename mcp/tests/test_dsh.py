"""Round-trip tests for the dsh (official DeepSeek Harness) adapter
(mcp/adapters/dsh.py).

Covers read of the v0 event-log format (session header + user/message +
assistant/message + session/title), write of foreign sessions into
--<cwd-slug>--/session-<uuid>/session.jsonl (contiguous seq, idmap foreign
ids), append/dedupe idempotency, _no-cwd fallback, cwd-drift relocation,
status counts, and (when the zstandard package is present) the compressed
.jsonl.zstd variant. The workspace domain is owned by dsh's own bootstrap
(never written by the adapter); the projection-cache doc (list titles) IS
written, mirroring the desktop's fold output.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.dsh import DshAdapter, HAVE_ZSTD, _slug  # noqa: E402

SID = "session-1f2e3d4c-5b6a-4c7d-8e9f-0a1b2c3d4e5f"
TS_MS = 1787647183000


def write_dsh_fixture(root: Path, sid: str = SID,
                      cwd: str = r"E:\OpenCode\agentctxsync",
                      title: str = "Dsh Chat", plain: bool = True):
    """dsh v0-style store: sessions/<slug>/<sid>/session.jsonl[.zstd]."""
    import adapters.dsh as m
    sdir = root / _slug(cwd) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "session", "version": 0, "id": sid, "createdAt": TS_MS,
         "cwd": cwd, "delegationDepth": 0},
        {"type": "session/title", "seq": 0, "time": TS_MS,
         "data": {"title": title, "messageSeqs": [],
                  "source": {"kind": "user"}}},
        {"type": "user/message", "seq": 1, "time": TS_MS,
         "surfaceOp": "append",
         "data": {"id": "user-a1", "role": "user",
                  "content": [{"type": "text", "text": "hello dsh"}],
                  "source": {"kind": "user"}}},
        {"type": "assistant/chunk", "seq": 2, "time": TS_MS + 500,
         "data": {"turn": 1, "step": 1,
                  "chunk": {"type": "text-delta", "text": "hi"}}},
        {"type": "assistant/message", "seq": 3, "time": TS_MS + 1000,
         "surfaceOp": "append", "sourceEventSeqs": [2],
         "data": {"turn": 1, "step": 1,
                  "message": {"id": "assistant-b1", "role": "assistant",
                              "content": [{"type": "text", "text": "hi"}],
                              "source": {"kind": "user"}}}},
        {"type": "tool/call", "seq": 4, "time": TS_MS + 1500,
         "data": {"turn": 1, "step": 2, "callId": "c1", "name": "sh",
                  "arguments": "{}"}},
    ]
    payload = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
    if plain:
        (sdir / "session.jsonl").write_bytes(payload)
    else:
        from adapters.dsh import _compress_zstd
        (sdir / "session.jsonl.zstd").write_bytes(_compress_zstd(payload))
    return sdir


class DshAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _session(self, **over):
        sess = {"id": "hermes:20260531_232319_1e131a",
                "started_at": TS_MS / 1000.0, "cwd": r"E:\OpenCode\agentctxsync",
                "title": "Pulled dsh session",
                "messages": [
                    {"session_id": "hermes:20260531_232319_1e131a",
                     "role": "user", "content": "hello", "timestamp": 1.0},
                    {"session_id": "hermes:20260531_232319_1e131a",
                     "role": "assistant", "content": "world",
                     "timestamp": 2.0},
                ]}
        sess.update(over)
        return sess

    def test_read_v0_format(self):
        write_dsh_fixture(self.root)
        a = DshAdapter(sessions_root=self.root)
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], SID)
        self.assertEqual(s["title"], "Dsh Chat")
        self.assertEqual(s["cwd"], r"E:\OpenCode\agentctxsync")
        self.assertEqual(s["started_at"], TS_MS / 1000.0)
        self.assertEqual([m["role"] for m in s["messages"]],
                         ["user", "assistant"])
        self.assertEqual(s["messages"][0]["content"], "hello dsh")
        self.assertEqual(s["messages"][0]["session_id"], SID)
        self.assertEqual(s["messages"][1]["content"], "hi")
        # tool/chunk events are not conversation text
        self.assertEqual(s["message_count"], 2)

    def test_write_foreign_roundtrip(self):
        a = DshAdapter(sessions_root=self.root)
        st = a.write_sessions([self._session()])
        self.assertEqual(st["imported"], 1)
        self.assertEqual(st["new_messages"], 2)
        # sidecar idmap mapping is the sync bookkeeping we own
        idmap = json.loads(
            (self.root / ".dsh-sync-idmap.json").read_text(encoding="utf-8"))
        local_id = idmap["hermes:20260531_232319_1e131a"]
        self.assertTrue(local_id.startswith("session-"))
        # workspace domain belongs to dsh's own bootstrap (realpath), but
        # the projection-cache doc IS ours: list titles read it directly
        self.assertFalse((self.root.parent / "storages"
                          / "workspace.json").exists())
        doc = json.loads((self.root.parent / "storages"
                          / "session_projcache" / "sessions"
                          / f"{local_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["record"]["rows"]["title"]["val"],
                         "Pulled dsh session")
        self.assertEqual(doc["record"]["identity"]["cwd"],
                         r"E:\OpenCode\agentctxsync")
        self.assertEqual(doc["record"]["identity"]["isSeeded"], False)
        # written file readable back with identical content
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], "hermes:20260531_232319_1e131a")
        self.assertEqual(s["title"], "Pulled dsh session")
        self.assertEqual([m["content"] for m in s["messages"]],
                         ["hello", "world"])
        # assistant events carry a model source (dsh validator requirement)
        text = self._session_text(a, local_id)
        for line in text.splitlines():
            rec = json.loads(line)
            if rec.get("type") == "assistant/message":
                src = rec["data"]["message"]["source"]
                self.assertEqual(src["kind"], "model")

    def _session_text(self, a, local_id: str) -> str:
        from adapters.dsh import HAVE_ZSTD
        proj = next(d for d in (a.sessions_root).iterdir()
                    if d.is_dir() and not d.name.startswith("."))
        fname = "session.jsonl.zstd" if HAVE_ZSTD else "session.jsonl"
        raw = (proj / local_id / fname).read_bytes()
        if HAVE_ZSTD:
            return self._decode_all(raw)
        return raw.decode("utf-8")

    def test_append_and_dedupe(self):
        a = DshAdapter(sessions_root=self.root)
        a.write_sessions([self._session()])
        # identical re-push: nothing new
        st = a.write_sessions([self._session()])
        self.assertEqual(st["imported"], 0)
        self.assertEqual(st["new_messages"], 0)
        # append one new message
        s = self._session()
        s["messages"].append({"session_id": s["id"], "role": "user",
                              "content": "more", "timestamp": 3.0})
        st = a.write_sessions([s])
        self.assertEqual(st["new_messages"], 1)
        read = a.read_sessions()[0]
        self.assertEqual([m["content"] for m in read["messages"]],
                         ["hello", "world", "more"])

    def test_own_session_id_updates_in_place(self):
        write_dsh_fixture(self.root)  # local session-<uuid> id
        a = DshAdapter(sessions_root=self.root)
        s = {"id": SID, "started_at": TS_MS / 1000.0, "cwd": r"E:\OpenCode\agentctxsync",
             "title": "New title",
             "messages": [
                 {"session_id": SID, "role": "user", "content": "q2",
                  "timestamp": TS_MS / 1000.0 + 2.0},
                 {"session_id": SID, "role": "assistant", "content": "a2",
                  "timestamp": TS_MS / 1000.0 + 3.0},
             ]}
        st = a.write_sessions([s])
        # same local file (session-<uuid> passes through, no new dir)
        self.assertEqual(st["imported"], 0)
        self.assertEqual(st["updated"], 1)
        read = a.read_sessions()[0]
        self.assertEqual(read["id"], SID)
        self.assertEqual([m["content"] for m in read["messages"]],
                         ["hello dsh", "hi", "q2", "a2"])
        self.assertEqual(read["title"], "New title")

    def test_no_cwd_uses_no_cwd_dir(self):
        a = DshAdapter(sessions_root=self.root)
        s = self._session(cwd=None)
        a.write_sessions([s])
        local_id = json.loads(
            (self.root / ".dsh-sync-idmap.json").read_text(encoding="utf-8"))[
                "hermes:20260531_232319_1e131a"]
        from adapters.dsh import HAVE_ZSTD
        fname = "session.jsonl.zstd" if HAVE_ZSTD else "session.jsonl"
        self.assertTrue((self.root / "_no-cwd" / local_id / fname).is_file())

    def test_slug_matches_rc_layout(self):
        # rc observed dir for E:\deepseekharness was --E-deepseekharness--
        self.assertEqual(_slug(r"E:\deepseekharness"), "--E-deepseekharness--")
        self.assertEqual(_slug(r"/work/x"), "--work-x--")
        # trailing separator keeps its dash (drive root E:/ -> --E---)
        self.assertEqual(_slug(r"E:/"), "--E---")

    def test_no_cwd_rewrite_reuses_existing_file(self):
        a = DshAdapter(sessions_root=self.root)
        a.write_sessions([self._session()])  # first write with cwd
        # later push without cwd must update in place, not duplicate
        s = self._session(cwd=None)
        s["messages"].append({"session_id": s["id"], "role": "user",
                              "content": "no-cwd-extra", "timestamp": 9.0})
        st = a.write_sessions([s])
        self.assertEqual(st["imported"], 0)
        self.assertEqual(st["new_messages"], 1)
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)  # no duplicate session

    def test_cwd_drift_relocates_session_dir(self):
        a = DshAdapter(sessions_root=self.root, storages_root=self.root.parent / "storages")
        s1 = {"id": SID, "started_at": 1.0, "cwd": r"E:\OpenCode\agentctxsync",
              "messages": [{"session_id": SID, "role": "user",
                            "content": "q", "timestamp": 2.0}]}
        a.write_sessions([s1])
        from adapters.dsh import HAVE_ZSTD
        fname = "session.jsonl.zstd" if HAVE_ZSTD else "session.jsonl"
        old_dir = self.root / "--E-OpenCode-agentctxsync--" / SID
        self.assertTrue((old_dir / fname).is_file())
        # server-side cwd changed to a different path: no new messages
        s2 = {"id": SID, "started_at": 1.0, "cwd": r"D:\moved-proj",
              "messages": [{"session_id": SID, "role": "user",
                            "content": "q", "timestamp": 2.0}]}
        st = a.write_sessions([s2])
        self.assertEqual(st["new_messages"], 0)
        new_dir = self.root / "--D-moved-proj--" / SID
        self.assertFalse(old_dir.exists())
        self.assertTrue((new_dir / fname).is_file())
        hdr = json.loads(self._session_text(a, SID).splitlines()[0])
        self.assertEqual(hdr["cwd"], r"D:\moved-proj")

    def test_status(self):
        write_dsh_fixture(self.root)
        a = DshAdapter(sessions_root=self.root)
        st = a.status()
        self.assertEqual(st["sessions"], 1)
        self.assertEqual(st["messages"], 2)

    @unittest.skipUnless(HAVE_ZSTD, "zstandard package not installed")
    def test_zstd_roundtrip(self):
        write_dsh_fixture(self.root, plain=False)  # .jsonl.zstd on disk
        a = DshAdapter(sessions_root=self.root)
        s = a.read_sessions()[0]
        self.assertEqual(s["title"], "Dsh Chat")
        self.assertEqual(len(s["messages"]), 2)
        # new foreign writes land compressed too
        st = a.write_sessions([self._session()])
        self.assertEqual(st["imported"], 1)
        # every line is its own zstd frame, and the first frame decodes to
        # exactly the header line (dsh reader contract)
        import adapters.dsh as m
        local_id = json.loads(
            (self.root / ".dsh-sync-idmap.json").read_text(encoding="utf-8"))[
                "hermes:20260531_232319_1e131a"]
        raw = (self.root / "--E-OpenCode-agentctxsync--" / local_id /
               "session.jsonl.zstd").read_bytes()
        text = self._decode_all(raw)
        lines = text.splitlines()
        self.assertEqual(raw.count(b"\x28\xb5\x2f\xfd"), len(lines))
        self.assertEqual(json.loads(lines[0]), {
            "type": "session", "version": 0,
            "id": local_id,
            "createdAt": TS_MS,
            "cwd": r"E:\OpenCode\agentctxsync",
            "delegationDepth": 0})

    def _decode_all(self, raw: bytes) -> str:
        import adapters.dsh as m
        with m._zstd.ZstdDecompressor().stream_reader(raw) as r:
            return r.read().decode("utf-8")


if __name__ == "__main__":
    unittest.main()
