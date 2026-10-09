"""Round-trip tests for the dsh (official DeepSeek Harness) adapter
(mcp/adapters/dsh.py).

Covers read of legacy v0 event logs (session header + user/message +
assistant/message + session/title), write of foreign sessions into
--<cwd-slug>--/session-<uuid>/ in dsh's CURRENT generation (seed head +
turn/step frames, contiguous seq, idmap foreign ids), append/dedupe
idempotency, _no-cwd fallback, cwd-drift relocation, status counts, and
(when the zstandard package is present) the compressed .jsonl.zstd variant.
The workspace domain is owned by dsh's own bootstrap (never written by the
adapter); the projection-cache doc (list titles) IS written, mirroring the
desktop's fold output.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.dsh import (DshAdapter, HAVE_ZSTD, _CURRENT_LOG_GENERATION,  # noqa: E402
                          _extended, _log_filename, _log_generation, _lp,
                          _slug)

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
        """Decoded text of the session's log at the generation dsh opens."""
        from adapters.dsh import HAVE_ZSTD
        proj = next(d for d in (a.sessions_root).iterdir()
                    if d.is_dir() and not d.name.startswith("."))
        path = DshAdapter._log_file(proj / local_id)
        raw = path.read_bytes()
        if HAVE_ZSTD and path.name.endswith(".zstd"):
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

    def test_title_only_update_on_untitled_log(self):
        """A log that never carried a session/title event accepts a
        title-only write (the sidecar reconcile path the client runs after
        pulls): the event lands, seqs stay contiguous, messages are not
        duplicated, and the projection-cache doc starts listing the title."""
        sdir = self.root / _slug(r"E:\OpenCode\agentctxsync") / SID
        sdir.mkdir(parents=True, exist_ok=True)
        lines = [
            {"type": "session", "version": 0, "id": SID,
             "createdAt": TS_MS, "cwd": r"E:\OpenCode\agentctxsync",
             "delegationDepth": 0},
            {"type": "user/message", "seq": 0, "time": TS_MS,
             "surfaceOp": "append",
             "data": {"id": "user-a1", "role": "user",
                      "content": [{"type": "text", "text": "hello dsh"}],
                      "source": {"kind": "user"}}},
        ]
        (sdir / "session.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        a = DshAdapter(sessions_root=self.root)
        s = a.read_sessions()[0]
        self.assertNotIn("title", s)          # the reported broken state
        s["title"] = "Healed title"
        before = (sdir / "session.jsonl").read_bytes()
        st = a.write_sessions([s])
        self.assertEqual(st["updated"], 1)
        self.assertEqual(st["new_messages"], 0)
        read = a.read_sessions()[0]
        self.assertEqual(read["title"], "Healed title")
        self.assertEqual(len(read["messages"]), 1)
        # the legacy v0 artifact is left byte-identical (dsh never mutates a
        # published predecessor) and a current-generation successor carries
        # the update: exactly one title event, seq contiguous from 0
        self.assertEqual((sdir / "session.jsonl").read_bytes(), before)
        seqs, titles = [], []
        for ln in self._session_text(a, SID).splitlines():
            rec = json.loads(ln)
            if "seq" in rec:
                seqs.append(rec["seq"])
            if rec.get("type") == "session/title":
                titles.append(rec["data"]["title"])
        self.assertEqual(seqs, list(range(len(seqs))))
        self.assertEqual(titles, ["Healed title"])
        # cache doc now lists the real title for the desktop list
        doc = json.loads((self.root.parent / "storages"
                          / "session_projcache" / "sessions"
                          / f"{SID}.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["record"]["rows"]["title"]["val"],
                         "Healed title")

    def test_no_cwd_uses_no_cwd_dir(self):
        a = DshAdapter(sessions_root=self.root)
        s = self._session(cwd=None)
        a.write_sessions([s])
        local_id = json.loads(
            (self.root / ".dsh-sync-idmap.json").read_text(encoding="utf-8"))[
                "hermes:20260531_232319_1e131a"]
        # a fresh Session is published in the current generation -- the one
        # dsh's reader opens without running the legacy migration chain
        log = DshAdapter._log_file(self.root / "_no-cwd" / local_id)
        self.assertTrue(log.is_file())
        self.assertEqual(_log_generation(log.name),
                         _CURRENT_LOG_GENERATION)

    def test_no_cwd_session_skips_projcache_doc(self):
        # DSH Desktop 2.0.5's projcache v5 schema requires identity.cwd to
        # be a string; a null-cwd doc is quarantined to .json.bak.* on every
        # boot. Cwd-less sessions therefore get no cache doc at all.
        a = DshAdapter(sessions_root=self.root)
        a.write_sessions([self._session(cwd=None)])
        local_id = json.loads(
            (self.root / ".dsh-sync-idmap.json").read_text(encoding="utf-8"))[
                "hermes:20260531_232319_1e131a"]
        self.assertFalse((self.root.parent / "storages"
                          / "session_projcache" / "sessions"
                          / f"{local_id}.json").exists())

    def test_refresh_removes_stale_no_cwd_projcache_doc(self):
        # Pre-fix writes left null-cwd docs behind; a refresh triggered by a
        # later update must drop the stale doc instead of re-creating it.
        a = DshAdapter(sessions_root=self.root)
        a.write_sessions([self._session(cwd=None)])
        local_id = json.loads(
            (self.root / ".dsh-sync-idmap.json").read_text(encoding="utf-8"))[
                "hermes:20260531_232319_1e131a"]
        doc = (self.root.parent / "storages" / "session_projcache"
               / "sessions" / f"{local_id}.json")
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(json.dumps(
            {"version": 5, "record": {"identity": {"cwd": None}}}),
            encoding="utf-8")
        s = self._session(cwd=None)
        s["messages"].append({"session_id": s["id"], "role": "user",
                              "content": "extra", "timestamp": 9.0})
        st = a.write_sessions([s])  # changed -> cache refresh runs
        self.assertEqual(st["new_messages"], 1)
        self.assertFalse(doc.exists())

    def test_rewrite_renumbers_seq_from_zero(self):
        # dsh's reader requires every event's seq to equal its 0-based index
        # in the file. A whole-file rewrite (append) must renumber from 0 --
        # continuing from the previous max seq shifts the log off zero and
        # dsh rejects it as corrupt on observe.
        a = DshAdapter(sessions_root=self.root)
        a.write_sessions([self._session()])
        s = self._session()
        s["messages"].append({"session_id": s["id"], "role": "user",
                              "content": "more", "timestamp": 3.0})
        st = a.write_sessions([s])  # append triggers a full rewrite
        self.assertEqual(st["new_messages"], 1)
        local_id = json.loads(
            (self.root / ".dsh-sync-idmap.json").read_text(encoding="utf-8"))[
                "hermes:20260531_232319_1e131a"]
        rows = [json.loads(x) for x in
                self._session_text(a, local_id).splitlines()]
        seqs = [r["seq"] for r in rows if r.get("seq") is not None]
        self.assertEqual(seqs, list(range(len(seqs))))

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
        fname = _log_filename(_CURRENT_LOG_GENERATION, HAVE_ZSTD)
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
        # new foreign writes land compressed, in the current generation
        st = a.write_sessions([self._session()])
        self.assertEqual(st["imported"], 1)
        # every line is its own zstd frame, and the first frame decodes to
        # exactly the header line (dsh reader contract)
        import adapters.dsh as m
        local_id = json.loads(
            (self.root / ".dsh-sync-idmap.json").read_text(encoding="utf-8"))[
                "hermes:20260531_232319_1e131a"]
        raw = (self.root / "--E-OpenCode-agentctxsync--" / local_id /
               _log_filename(_CURRENT_LOG_GENERATION, True)).read_bytes()
        text = self._decode_all(raw)
        lines = text.splitlines()
        self.assertEqual(raw.count(b"\x28\xb5\x2f\xfd"), len(lines))
        self.assertEqual(json.loads(lines[0]), {
            "type": "session", "version": _CURRENT_LOG_GENERATION,
            "id": local_id,
            "createdAt": TS_MS,
            "cwd": r"E:\OpenCode\agentctxsync",
            "delegationDepth": 0,
            "isSeeded": False,
            "agentPreset": "standard"})

    def _decode_all(self, raw: bytes) -> str:
        import adapters.dsh as m
        with m._zstd.ZstdDecompressor().stream_reader(raw) as r:
            return r.read().decode("utf-8")


def write_generation_fixture(sdir: Path, sid: str, cwd: str, title: str,
                             messages: list[str], version: int = 3):
    """A dsh generation fixture: header version ``version`` + shaped rows."""
    from adapters.dsh import _compress_zstd
    lines: list[dict] = [{
        "type": "session", "version": version, "id": sid, "createdAt": TS_MS,
        "cwd": cwd, "isSeeded": False, "delegationDepth": 0,
        "agentPreset": "standard"}]
    seq, turn = 0, 0
    lines.append({"type": "session/title", "seq": seq, "time": TS_MS,
                  "data": {"title": title, "messageSeqs": [],
                           "source": {"kind": "user"}}})
    seq += 1
    for i, text in enumerate(messages, start=1):
        if i % 2:                      # user row: data.{content,source,role,id}
            turn += 1
            lines.append({"type": "turn/start", "seq": seq, "time": TS_MS + i,
                          "data": {"turn": turn}})
            seq += 1
            lines.append({"type": "user/message", "seq": seq, "time": TS_MS + i,
                          "surfaceOp": "append",
                          "data": {"content": [{"type": "text", "text": text}],
                                   "source": {"kind": "user"},
                                   "role": "user", "id": f"user-{i}"}})
        else:                          # assistant row: data.message.{role,content}
            lines.append({"type": "assistant/message", "seq": seq,
                          "time": TS_MS + i,
                          "data": {"turn": turn, "step": 1,
                                   "message": {"role": "assistant", "content": [
                                       {"type": "text", "text": text}]}}})
        seq += 1
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / _log_filename(version, True)).write_bytes(
        _compress_zstd(("\n".join(json.dumps(x) for x in lines) + "\n").encode()))
    return sdir


class LogGenerationTest(unittest.TestCase):
    """dsh selects the numerically highest canonical generation for read AND
    write (`session.jsonl[.zstd]` is v0). Preferring the v0 root — the previous
    behaviour — went permanently stale for migrated sessions, whose v0 file is
    frozen history while the successor keeps receiving events."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_generation_of_canonical_names(self):
        cases = {"session.jsonl": 0, "session.jsonl.zstd": 0,
                 "session.v1.jsonl": 1, "session.v3.jsonl.zstd": 3,
                 "session.v12.jsonl": 12}
        for name, gen in cases.items():
            self.assertEqual(_log_generation(name), gen, name)
        for name in ("session.jsonl.bak", "notes.txt", "session.v.jsonl",
                     "session.v3.jsonl.zstd.tmp"):
            self.assertIsNone(_log_generation(name), name)

    def test_filename_round_trip(self):
        for gen in (0, 1, 3, 12):
            for zstd in (True, False):
                name = _log_filename(gen, zstd)
                self.assertEqual(_log_generation(name), gen, name)
                self.assertEqual(name.endswith(".zstd"), zstd, name)

    def test_highest_generation_wins_over_frozen_root(self):
        sdir = self.root / _slug(r"E:\OpenCode\agentctxsync") / SID
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "session.jsonl.zstd").write_bytes(b"{}\n")
        (sdir / "session.v3.jsonl.zstd").write_bytes(b"{}\n")
        self.assertEqual(DshAdapter._log_file(sdir).name,
                         "session.v3.jsonl.zstd")

    def test_falls_back_to_the_only_present_generation(self):
        sdir = self.root / _slug(r"E:\OpenCode\agentctxsync") / SID
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "session.v2.jsonl.zstd").write_bytes(b"{}\n")
        (sdir / "session.jsonl.zstd").write_bytes(b"{}\n")
        self.assertEqual(DshAdapter._log_file(sdir).name, "session.v2.jsonl.zstd")
        (sdir / "session.v2.jsonl.zstd").unlink()
        self.assertEqual(DshAdapter._log_file(sdir).name, "session.jsonl.zstd")

    def test_read_uses_the_live_generation_not_the_root(self):
        sdir = self.root / _slug(r"E:\OpenCode\agentctxsync") / SID
        write_dsh_fixture(self.root)                    # frozen v0: 1 msg
        write_generation_fixture(sdir, SID, r"E:\OpenCode\agentctxsync",
                                 "renamed after migration", ["one", "two"],
                                 version=3)             # live v3
        sessions = DshAdapter(sessions_root=self.root).read_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["title"], "renamed after migration")
        self.assertEqual([m["role"] for m in sessions[0]["messages"]],
                         ["user", "assistant"])
        self.assertEqual(sessions[0]["message_count"], 2)

    def test_write_rewrites_the_current_generation_in_place(self):
        sdir = self.root / _slug(r"E:\OpenCode\agentctxsync") / SID
        fname = _log_filename(_CURRENT_LOG_GENERATION, True)
        write_generation_fixture(sdir, SID, r"E:\OpenCode\agentctxsync",
                                 "current session", ["hello"],
                                 version=_CURRENT_LOG_GENERATION)
        a = DshAdapter(sessions_root=self.root)
        stats = a.write_sessions([{
            "id": SID, "started_at": TS_MS / 1000.0,
            "cwd": r"E:\OpenCode\agentctxsync", "title": "current session",
            "messages": [
                # same instant as the fixture row -> deduped, not re-added
                {"session_id": SID, "role": "user", "content": "hello",
                 "timestamp": (TS_MS + 1) / 1000.0},
                {"session_id": SID, "role": "assistant", "content": "world",
                 "timestamp": (TS_MS + 9) / 1000.0}]}])
        self.assertEqual(stats["new_messages"], 1)
        # rewritten in place: one file, still the current generation, and no
        # predecessor/successor churn beside it
        self.assertEqual([p.name for p in sdir.iterdir()], [fname])
        raw = a._load_log(sdir)
        header = raw["header"]
        self.assertEqual(header["version"], _CURRENT_LOG_GENERATION)
        self.assertEqual(header["id"], SID)
        self.assertEqual(header["isSeeded"], False)          # preserved
        self.assertEqual(header["agentPreset"], "standard")  # preserved
        self.assertEqual(header["cwd"], r"E:\OpenCode\agentctxsync")
        self.assertEqual([m["role"] for m in raw["msgs"]],
                         ["user", "assistant"])
        # and the harness's own generation selection still finds it
        self.assertEqual(DshAdapter._log_file(sdir).name, fname)

    def test_legacy_v3_session_upgrades_to_the_current_generation(self):
        """A store written before this fix carries (v3) logs the desktop's
        v3->v4 edge refuses -- a user session/title with messageSeqs, or any
        earlier release shape. The next sync publishes a current-generation
        successor even without new messages, and the frozen predecessor stays
        byte-identical (dsh never mutates a published predecessor)."""
        sdir = self.root / _slug(r"E:\OpenCode\agentctxsync") / SID
        write_generation_fixture(sdir, SID, r"E:\OpenCode\agentctxsync",
                                 "v3 session", ["hello"], version=3)
        before = (sdir / "session.v3.jsonl.zstd").read_bytes()
        a = DshAdapter(sessions_root=self.root)
        stats = a.write_sessions([{
            "id": SID, "started_at": TS_MS / 1000.0,
            "cwd": r"E:\OpenCode\agentctxsync", "title": "v3 session",
            "messages": [
                {"session_id": SID, "role": "user", "content": "hello",
                 "timestamp": (TS_MS + 1) / 1000.0}]}])
        self.assertEqual(stats["new_messages"], 0)
        self.assertEqual(stats["updated"], 1)          # the upgrade itself
        self.assertEqual((sdir / "session.v3.jsonl.zstd").read_bytes(), before)
        successor = sdir / _log_filename(_CURRENT_LOG_GENERATION, True)
        self.assertTrue(successor.is_file())
        self.assertEqual(DshAdapter._log_file(sdir), successor)


def _read_log_text(path: Path) -> str:
    """Decoded text of a session log file (zstd or raw)."""
    raw = path.read_bytes()
    if path.name.endswith(".zstd"):
        import adapters.dsh as m
        with m._zstd.ZstdDecompressor().stream_reader(raw) as r:
            return r.read().decode("utf-8")
    return raw.decode("utf-8")


class CurrentGenerationWriteTest(unittest.TestCase):
    """New Sessions are published in dsh's CURRENT format generation.

    dsh's reader opens the numerically highest generation and migrates older
    artifacts through v0->v1->v2->v3->v4 — a chain (plus v4 native admission)
    that REFUSES malformed shapes: the message-only v0 logs the adapter used to
    write, a v0 ``sourceEventSeqs``, and a user ``session/title`` carrying
    ``messageSeqs``. Every such synced Session was unopenable in DSH Desktop
    ("历史加载失败"). These tests pin the shape the reader accepts: the current
    generation header, the seed head, one turn/step frame per conversation row,
    the settlement fields an assistant/message must carry, and the empty
    ``messageSeqs`` / ``system-prompt`` source the v4 validator requires.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, **over):
        """Write one two-turn Session (second turn has two model steps)."""
        sess = {"id": "hermes:20260531_232319_1e131a", "started_at": 1.0,
                "cwd": r"E:\OpenCode\agentctxsync", "title": "Pulled",
                "messages": [
                    {"session_id": "x", "role": "user", "content": "q1",
                     "timestamp": 2.0},
                    {"session_id": "x", "role": "assistant", "content": "a1",
                     "timestamp": 3.0},
                    {"session_id": "x", "role": "user", "content": "q2",
                     "timestamp": 4.0},
                    {"session_id": "x", "role": "assistant", "content": "a2",
                     "timestamp": 5.0},
                    {"session_id": "x", "role": "assistant", "content": "a3",
                     "timestamp": 6.0}]}
        sess.update(over)
        a = DshAdapter(sessions_root=self.root)
        a.write_sessions([sess])
        local_id = json.loads(
            (self.root / ".dsh-sync-idmap.json").read_text(encoding="utf-8"))[
                "hermes:20260531_232319_1e131a"]
        sdir = next(d for d in self.root.iterdir()
                    if d.is_dir() and not d.name.startswith(".")) / local_id
        return a, sdir

    def test_new_session_is_written_in_the_current_generation(self):
        a, sdir = self._write()
        log = DshAdapter._log_file(sdir)
        self.assertEqual(_log_generation(log.name),
                         _CURRENT_LOG_GENERATION)
        rows = [json.loads(ln) for ln in _read_log_text(log).splitlines()]
        header = rows[0]
        self.assertEqual(header["type"], "session")
        self.assertEqual(header["version"], _CURRENT_LOG_GENERATION)
        self.assertEqual(header["id"], sdir.name)
        self.assertFalse(header["isSeeded"])
        self.assertEqual(header["agentPreset"], "standard")
        self.assertEqual(header["delegationDepth"], 0)
        self.assertEqual(header["cwd"], r"E:\OpenCode\agentctxsync")
        # dense seq from 0 -- dsh asserts seq == the event's own position
        self.assertEqual([r["seq"] for r in rows[1:]],
                         list(range(len(rows) - 1)))
        # the seed head dsh's migration looks for, then the turn frames
        self.assertEqual([r["type"] for r in rows[1:5]],
                         ["permission/preset", "sandbox/mode",
                          "approval/policy", "turn/start"])
        self.assertEqual(rows[4]["data"], {"turn": 1})
        self.assertEqual([r["type"] for r in rows[5:8]],
                         ["step/start", "system/message", "user/message"])
        self.assertEqual(rows[6]["data"]["turn"], 1)
        self.assertEqual(rows[6]["data"]["step"], 1)
        # a user title carries NO messageSeqs in the current generation: the
        # v4 validator requires them empty exactly when source.kind is "user"
        # (the v3->v4 edge refused the non-empty reference this used to write)
        title = next(r for r in rows if r["type"] == "session/title")
        self.assertEqual(title["data"]["title"], "Pulled")
        self.assertEqual(title["data"]["messageSeqs"], [])
        # the harness-injected system head carries the v4 system-prompt source
        system = next(r for r in rows if r["type"] == "system/message")
        self.assertEqual(system["data"]["message"]["source"],
                         {"kind": "system-prompt"})
        # turn 2 holds both model replies as separate, closed steps
        steps = [(r["type"], r["data"].get("step"))
                 for r in rows if r["type"] in ("step/start", "step/end")]
        self.assertEqual(steps, [("step/start", 1), ("step/end", 1),
                                 ("step/start", 1), ("step/end", 1),
                                 ("step/start", 2), ("step/end", 2)])
        turns = [r["data"] for r in rows if r["type"] == "turn/end"]
        self.assertEqual(turns, [{"turn": 1, "reason": {"kind": "completed"}},
                                 {"turn": 2, "reason": {"kind": "completed"}}])
        # round-trip: both messages of every turn survive
        read = a.read_sessions()[0]
        self.assertEqual([m["content"] for m in read["messages"]],
                         ["q1", "a1", "q2", "a2", "a3"])

    def test_assistant_messages_carry_settlement_fields(self):
        a, sdir = self._write()
        rows = [json.loads(ln) for ln in
                _read_log_text(DshAdapter._log_file(sdir)).splitlines()]
        assistants = [r for r in rows if r["type"] == "assistant/message"]
        self.assertEqual(len(assistants), 3)
        for rec in assistants:
            data = rec["data"]
            self.assertEqual(sorted(data["usage"]),
                             ["cacheReadTokens", "inputTokens",
                              "outputTokens", "reasoningTokens"])
            self.assertIsInstance(data["stream"], list)
            # the v0 codec rejects sourceEventSeqs and dsh never writes it
            self.assertNotIn("sourceEventSeqs", data)
            # the model NAME is never synced (LOCAL_ONLY_SESSION_FIELDS,
            # decision record 2026.10.06.1): the required source stamp is a
            # placeholder, even when the pulled session carried a model
            self.assertEqual(data["message"]["source"],
                             {"kind": "model", "provider": "unknown",
                              "model": "unknown"})

    def test_consecutive_user_rows_close_the_open_turn(self):
        """Two user rows with no model reply between them must not leave a
        turn open: v4 rejects a successor turn/start while a turn is open
        ("turn/start does not open the expected turn"), which is what a
        dropped/absent assistant reply produces. It is framed as an
        ``interrupted`` turn."""
        a, sdir = self._write(messages=[
            {"session_id": "x", "role": "user", "content": "q1",
             "timestamp": 2.0},
            {"session_id": "x", "role": "user", "content": "q2",
             "timestamp": 3.0}])
        rows = [json.loads(ln) for ln in
                _read_log_text(DshAdapter._log_file(sdir)).splitlines()]
        self.assertEqual(
            [r["type"] for r in rows
             if r["type"] in ("turn/start", "turn/end")],
            ["turn/start", "turn/end", "turn/start", "turn/end"])
        self.assertEqual([r["data"] for r in rows if r["type"] == "turn/end"],
                         [{"turn": 1, "reason": {"kind": "interrupted"}},
                          {"turn": 2, "reason": {"kind": "interrupted"}}])
        read = a.read_sessions()[0]
        self.assertEqual([m["content"] for m in read["messages"]],
                         ["q1", "q2"])

    def test_model_first_session_leads_with_the_system_head(self):
        """A Session whose first row is a model reply still leads with the
        protected system head: v4 rejects any surface event before it
        ("system/message requires a protected first surface head")."""
        a, sdir = self._write(messages=[
            {"session_id": "x", "role": "assistant", "content": "a1",
             "timestamp": 2.0}])
        rows = [json.loads(ln) for ln in
                _read_log_text(DshAdapter._log_file(sdir)).splitlines()]
        surface = [r["type"] for r in rows
                   if r["type"] in ("system/message", "user/message",
                                    "assistant/message")]
        self.assertEqual(surface, ["system/message", "assistant/message"])
        self.assertEqual(
            [r["type"] for r in rows
             if r["type"] in ("turn/start", "turn/end")],
            ["turn/start", "turn/end"])
        read = a.read_sessions()[0]
        self.assertEqual([m["content"] for m in read["messages"]], ["a1"])

    def test_legacy_v0_session_gets_a_current_generation_successor(self):
        """A store pulled before the fix (v0 artifacts only) is repaired by
        the next sync: the successor carries the log, the predecessor stays
        byte-identical, and nothing is duplicated."""
        sdir = write_dsh_fixture(self.root)          # legacy v0, 1 turn
        before = (sdir / "session.jsonl").read_bytes()
        a = DshAdapter(sessions_root=self.root)
        sess = {"id": SID, "started_at": TS_MS / 1000.0, "title": "Dsh Chat",
                "cwd": r"E:\OpenCode\agentctxsync",
                "messages": [
                    {"session_id": SID, "role": "user", "content": "hello dsh",
                     "timestamp": TS_MS / 1000.0},
                    {"session_id": SID, "role": "assistant", "content": "hi",
                     "timestamp": (TS_MS + 1000) / 1000.0}]}
        stats = a.write_sessions([sess])
        self.assertEqual(stats["imported"], 0)
        self.assertEqual(stats["new_messages"], 0)
        self.assertEqual(stats["updated"], 1)        # the upgrade itself
        self.assertEqual((sdir / "session.jsonl").read_bytes(), before)
        log = DshAdapter._log_file(sdir)
        self.assertEqual(_log_generation(log.name),
                         _CURRENT_LOG_GENERATION)
        read = a.read_sessions()[0]
        self.assertEqual(read["title"], "Dsh Chat")
        self.assertEqual([m["content"] for m in read["messages"]],
                         ["hello dsh", "hi"])


    def test_text_less_replies_are_not_stored_and_do_not_rewrite(self):
        """An assistant turn without text (tool calls / reasoning only) must
        not become an empty conversation row: the reader drops such a row, so
        it would look new on every pull and rewrite the Session forever."""
        payload = [
            {"session_id": "x", "role": "user", "content": "q", "timestamp": 2.0},
            {"session_id": "x", "role": "assistant", "content": "",
             "timestamp": 3.0},
            {"session_id": "x", "role": "assistant", "content": "a",
             "timestamp": 4.0}]
        a, sdir = self._write(messages=payload)
        log = DshAdapter._log_file(sdir)
        rows = [json.loads(ln) for ln in _read_log_text(log).splitlines()]
        self.assertEqual(
            len([r for r in rows if r["type"] == "assistant/message"]), 1)
        self.assertEqual([m["content"] for m in a.read_sessions()[0]["messages"]],
                         ["q", "a"])
        before = log.read_bytes()
        again = a.write_sessions([{"id": "hermes:20260531_232319_1e131a",
                                   "started_at": 1.0, "title": "Pulled",
                                   "cwd": r"E:\OpenCode\agentctxsync",
                                   "messages": payload}])
        self.assertEqual(again["new_messages"], 0)
        self.assertEqual(again["updated"], 0)      # nothing to rewrite
        self.assertEqual(log.read_bytes(), before)


class WorkspaceIndexTest(unittest.TestCase):
    """A synced Session must land under its cwd's Workspace: the Desktop
    sidebar groups by ``workspace.sessionIds`` and dsh only fills those in on
    its one-time first boot, so an externally written Session stays
    "Ungrouped" forever. The adapter attaches Sessions itself, additively
    (mirroring dsh's header bootstrap), leaving the domain's invariants and
    every existing record, title and order intact."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sessions = root / "sessions"
        self.sessions.mkdir()
        self.storages = root / "storages"
        self.storages.mkdir()
        self.project = root / "proj"
        self.project.mkdir()
        self.canon = os.path.realpath(str(self.project))

    def tearDown(self):
        self.tmp.cleanup()

    def _session(self, **over):
        sess = {"id": "hermes:20260531_232319_1e131a",
                "started_at": TS_MS / 1000.0, "cwd": str(self.project),
                "title": "Pulled",
                "messages": [{"session_id": "x", "role": "user",
                              "content": "hi", "timestamp": 1.0}]}
        sess.update(over)
        return sess

    def _unit(self, records=None, order=None, **over):
        state = {"initialized": True, "workspaceIds": list(order or []),
                 "archivedSessionIds": []}
        state.update(over)
        return {"unit": {"name": "workspace", "version": 2}, "global": state,
                "tables": {"workspaces": records or {}}}

    def _put(self, doc):
        (self.storages / "workspace.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    def _get(self):
        return json.loads((self.storages / "workspace.json").read_text(
            encoding="utf-8"))

    def _adapter(self):
        return DshAdapter(sessions_root=self.sessions,
                          storages_root=self.storages)

    def test_new_session_creates_its_workspaces_record(self):
        self._put(self._unit())
        self._adapter().write_sessions([self._session()])
        doc = self._get()
        self.assertEqual(len(doc["tables"]["workspaces"]), 1)
        wid, rec = next(iter(doc["tables"]["workspaces"].items()))
        self.assertEqual(rec["path"], self.canon)
        self.assertEqual(rec["title"], self.project.name)
        self.assertEqual(doc["global"]["workspaceIds"], [wid])
        self.assertEqual(len(rec["sessionIds"]), 1)
        self.assertTrue(rec["sessionIds"][0].startswith("session-"))

    def test_existing_record_keeps_its_title_order_and_members(self):
        self._put(self._unit({"w1": {
            "path": self.canon, "title": "Renamed", "sessionIds": ["session-keep"],
            "createdAt": "2020-01-01T00:00:00.000Z",
            "updatedAt": "2020-01-01T00:00:00.000Z"}}, ["w1"]))
        self._adapter().write_sessions([self._session()])
        doc = self._get()
        self.assertEqual(list(doc["tables"]["workspaces"]), ["w1"])
        rec = doc["tables"]["workspaces"]["w1"]
        self.assertEqual(rec["title"], "Renamed")
        self.assertEqual(doc["global"]["workspaceIds"], ["w1"])
        # the new member is prepended, the existing one is not lost
        self.assertEqual(len(rec["sessionIds"]), 2)
        self.assertEqual(rec["sessionIds"][-1], "session-keep")
        self.assertNotEqual(rec["updatedAt"], "2020-01-01T00:00:00.000Z")

    def test_grouping_is_idempotent(self):
        self._put(self._unit())
        a = self._adapter()
        a.write_sessions([self._session()])
        first = self._get()
        a.write_sessions([])          # second pass with nothing to write
        self.assertEqual(self._get(), first)

    def test_cwd_that_does_not_resolve_is_not_grouped(self):
        # dsh skips a session whose cwd cannot be canonicalized to a directory
        self._put(self._unit())
        self._adapter().write_sessions(
            [self._session(cwd=str(self.project / "gone"))])
        self.assertEqual(self._get()["tables"]["workspaces"], {})

    def test_cwd_less_session_is_not_grouped(self):
        self._put(self._unit())
        self._adapter().write_sessions([self._session(cwd=None)])
        self.assertEqual(self._get()["tables"]["workspaces"], {})

    def test_absent_or_uninitialized_units_are_left_alone(self):
        # no unit file: dsh creates and bootstraps it itself
        self._adapter().write_sessions([self._session()])
        self.assertFalse((self.storages / "workspace.json").exists())
        # uninitialized: the one-time bootstrap is dsh's, not ours
        self._put(self._unit(initialized=False))
        self._adapter().write_sessions([self._session()])
        doc = self._get()
        self.assertEqual(doc["tables"]["workspaces"], {})
        self.assertEqual(doc["global"]["workspaceIds"], [])

    def test_inconsistent_domain_is_not_touched(self):
        # a durable order referencing a missing record is corruption; adding
        # to it would deepen the inconsistency
        self._put(self._unit({}, ["ghost"]))
        self._adapter().write_sessions([self._session()])
        doc = self._get()
        self.assertEqual(doc["tables"]["workspaces"], {})
        self.assertEqual(doc["global"]["workspaceIds"], ["ghost"])


class LongPathHelperTest(unittest.TestCase):
    """Windows MAX_PATH (260): one CJK cwd slug pushed the atomic write's
    ``session.jsonl.zstd.tmp`` to exactly 260 chars, the open raised
    FileNotFoundError, and the whole pull aborted (9 of 275 sessions
    imported). ``_extended`` supplies the ``\\?\`` form that lifts the limit
    without the machine-wide LongPathsEnabled policy."""

    def test_short_path_untouched(self):
        p = r"C:\Users\me\.dsh\sessions\--a--\session-1\session.jsonl"
        self.assertLess(len(p), 240)
        self.assertEqual(_extended(p), p)

    def test_long_path_gets_extended_prefix(self):
        # the observed failure shape: 237-char dir + 23-char temp name
        p = "C:\\Users\\me\\.dsh\\sessions\\--" + "x" * 250 + "\\session.jsonl.zstd.tmp"
        self.assertGreaterEqual(len(p), 260)
        self.assertEqual(_extended(p), "\\\\?\\" + p)

    def test_separators_normalized_before_prefix(self):
        p = "C:/Users/me/.dsh/sessions/--" + "x" * 250 + "/session.jsonl"
        self.assertEqual(_extended(p), "\\\\?\\" + p.replace("/", "\\"))

    def test_unc_and_already_prefixed_paths_left_alone(self):
        unc = "\\\\server\\share\\" + "x" * 300
        self.assertEqual(_extended(unc), unc)
        already = "\\\\?\\C:\\" + "x" * 300
        self.assertEqual(_extended(already), already)

    def test_lp_applies_extended_form_only_on_windows(self):
        # A real absolute temp path, long enough to cross the threshold, so the
        # assertion reflects the host's actual contract rather than POSIX.
        p = os.path.join(tempfile.gettempdir(), "y" * 300)
        plain = os.path.abspath(p)
        out = _lp(p)
        if os.name == "nt":
            self.assertTrue(out.startswith("\\\\?\\"))
            self.assertEqual(out[4:], plain.replace("/", "\\"))
        else:
            self.assertEqual(out, plain)

    def test_log_file_finds_a_session_dir_past_max_path(self):
        # The generation walk used to swallow the iterdir() failure on a store
        # root deep enough to push a session dir past MAX_PATH, silently
        # dropping those sessions from read / write / status.
        import shutil
        base = os.path.join(tempfile.gettempdir(), "dsh-lp-probe")
        deep = os.path.join(base, "s" * 130, "p" * 130, "session-x")
        self.addCleanup(shutil.rmtree, _lp(base), ignore_errors=True)
        os.makedirs(_lp(deep), exist_ok=True)
        if len(os.path.abspath(deep)) < 260:
            self.skipTest("host path space did not reach MAX_PATH")
        with open(_lp(os.path.join(deep, "session.v3.jsonl.zstd")), "wb") as f:
            f.write(b"{}\n")
        found = DshAdapter._log_file(Path(deep))
        if os.name == "nt":
            self.assertIsNotNone(found)          # was None before the fix
            self.assertEqual(found.name, "session.v3.jsonl.zstd")
        else:
            self.skipTest("MAX_PATH is Windows-only")


if __name__ == "__main__":
    unittest.main()
