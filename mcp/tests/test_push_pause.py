"""Client contract for sessions the user paused in the Web UI.

/push answers with `paused_ids`: the sessions the server refused to write
because the user paused them (docs/ARCHITECTURE.md "暂停/恢复同步"). The
client must NOT record a push fingerprint for those — a fingerprint means
"the server already holds this content", so recording one would leave the
server frozen forever after the resume: nothing changes locally, the
fingerprint keeps matching, and the session is never re-sent. No fingerprint
= re-sent every cycle, so the resume lands the full content on the next one.
"""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402


def session(sid: str, content: str = "hi", ts: float = 1.0) -> dict:
    return {"id": sid, "cwd": "c:/x", "title": sid,
            "messages": [{"session_id": sid, "role": "user",
                          "content": content, "timestamp": ts}]}


class FakeAdapter:
    agent_type = "workbuddy"

    def __init__(self, sessions):
        self._sessions = sessions

    def discover(self):
        return "store"

    def read_sessions(self):
        return [dict(s) for s in self._sessions]

    def _is_foreign(self, sid):
        return False

    def _foreign_agent(self, sid):
        return None


def payload_ids(calls):
    """Ids in the order they were sent, across every chunk."""
    return [s["id"] for call in calls for s in call[2]["sessions"]]


def push_cycle(sessions, responses, fingerprint_path, meta_path):
    """One push_sessions() run against scripted /push responses.

    Returns (result, sent_ids, fingerprints). ``fingerprints`` is read back
    from the PATCHED path inside the patch scope -- reading it outside would
    hit this machine's real client sidecar."""
    calls = []

    def api(method, path, body=None):
        calls.append((method, path, body))
        return responses.pop(0)

    with mock.patch.object(server, "adapter", FakeAdapter(sessions)), \
            mock.patch.object(server, "FIELD_META_PATH", meta_path), \
            mock.patch.object(server, "PUSH_FINGERPRINT_PATH", fingerprint_path), \
            mock.patch.object(server, "api_call", side_effect=api):
        result = server.push_sessions()
        fingerprints = sorted(server._load_push_fingerprint())
    return result, payload_ids(calls), fingerprints


def accepted(paused_ids=None, **over):
    """A /push response body (the new server's shape)."""
    body = {"imported": 0, "updated": 1, "new_messages": 0, "sync_at": 1.0,
            "session_revs": {}}
    if paused_ids is not None:
        body["paused_ids"] = list(paused_ids)
    body.update(over)
    return body


class PushPauseTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self.fp = td / "fp.json"
        self.meta = td / "meta.json"
        self.addCleanup(self._td.cleanup)

    def test_paused_session_is_not_fingerprinted_and_is_retried(self):
        sessions = [session("s1"), session("s2")]
        result, sent, fp = push_cycle(sessions, [accepted(paused_ids=["s1"])],
                                      self.fp, self.meta)
        self.assertEqual(result["paused"], 1)
        self.assertEqual(result["paused_ids"], ["s1"])
        self.assertEqual(sorted(sent), ["s1", "s2"])
        # only the accepted session anchored: s1 stays un-fingerprinted so the
        # next cycle still considers it changed
        self.assertEqual(fp, ["s2"])

        # ... which is exactly what happens: the next cycle sends s1 again
        # (and only s1), and once the server accepts it, it anchors too
        result2, sent2, fp2 = push_cycle(sessions, [accepted()], self.fp,
                                         self.meta)
        self.assertEqual(sent2, ["s1"])
        self.assertNotIn("paused", result2)
        self.assertEqual(fp2, ["s1", "s2"])

        # converged: nothing left to send
        _, sent3, _ = push_cycle(sessions, [], self.fp, self.meta)
        self.assertEqual(sent3, [])

    def test_pause_is_reported_per_cycle_not_counted_as_synced(self):
        """The paused session must not be reported as pushed (it never
        reached the server), and the cycle stays a success — a pause is a
        user decision, not a failure."""
        sessions = [session("s1")]
        result, _, _ = push_cycle(sessions, [accepted(paused_ids=["s1"])],
                                  self.fp, self.meta)
        self.assertNotIn("error", result)
        self.assertEqual((result["imported"], result["updated"]), (0, 1))

    def test_legacy_server_without_paused_ids_keeps_the_old_contract(self):
        """Mixed-version window: an older server answers without the field,
        and the client behaves exactly as before (fingerprints everything)."""
        sessions = [session("s1")]
        result, sent, fp = push_cycle(sessions, [accepted()], self.fp, self.meta)
        self.assertEqual(sent, ["s1"])
        self.assertNotIn("paused", result)
        self.assertEqual(fp, ["s1"])

    def test_paused_ids_from_every_chunk_are_merged(self):
        """A big store is pushed in chunks; pauses reported by any chunk are
        counted once in the cycle result."""
        sessions = [session(f"s{i}") for i in range(3)]
        with mock.patch.object(server, "_chunk_sessions",
                               side_effect=lambda s, **k: [[s[0]], [s[1]], [s[2]]]):
            result, sent, fp = push_cycle(sessions,
                                          [accepted(paused_ids=["s0"]),
                                           accepted(),
                                           accepted(paused_ids=["s2"])],
                                          self.fp, self.meta)
        self.assertEqual(result["paused"], 2)
        self.assertEqual(result["paused_ids"], ["s0", "s2"])
        self.assertEqual(fp, ["s1"])
        self.assertEqual(sorted(sent), ["s0", "s1", "s2"])


if __name__ == "__main__":
    unittest.main()
