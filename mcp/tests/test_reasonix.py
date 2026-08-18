"""Round-trip tests for the reasonix adapter (jsonl transcripts + locks)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.reasonix import ReasonixAdapter  # noqa: E402


def make_fixture(sessions: Path):
    sessions.mkdir(parents=True)
    p = sessions / "rx-001.jsonl"
    p.write_text("\n".join([
        json.dumps({"role": "user", "content": "hello reasonix", "timestamp": 100.0}),
        json.dumps({"role": "assistant", "content": "hi", "timestamp": 101.0,
                    "tool_calls": [{"id": "t1", "name": "read"}]}),
    ]) + "\n", encoding="utf-8")


class ReasonixAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = Path(self.tmp.name) / "sessions"
        make_fixture(self.sessions)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read(self):
        a = ReasonixAdapter(sessions_dir=self.sessions)
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], "rx-001")
        self.assertEqual(len(s["messages"]), 2)
        self.assertEqual(s["messages"][1]["role"], "assistant")
        self.assertEqual(s["messages"][1]["tool_calls"][0]["name"], "read")
        self.assertEqual(s["messages"][0]["session_id"], "rx-001")

    def test_write_new_and_dedupe(self):
        a = ReasonixAdapter(sessions_dir=self.sessions)
        foreign = [{
            "id": "rx-002", "started_at": 200.0, "title": "rx-002",
            "messages": [
                {"session_id": "rx-002", "role": "user",
                 "content": "pushed", "timestamp": 200.5}]}]
        first = a.write_sessions(foreign)
        self.assertEqual(first["imported"], 1)
        second = a.write_sessions(foreign)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(second["new_messages"], 0)
        ids = [s["id"] for s in a.read_sessions()]
        self.assertIn("rx-002", ids)

    def test_write_normalizes_string_tool_calls(self):
        """Pulled hermes sessions carry tool_calls as a JSON string (the
        server stores it as text, OpenAI style). Written lines must be a
        native {id, name, arguments} array so reasonix can open them."""
        a = ReasonixAdapter(sessions_dir=self.sessions)
        raw = [{"id": "call_1", "type": "function",
                "function": {"name": "read_file",
                             "arguments": "{\"path\": \"a\"}"}}]
        foreign = [{
            "id": "rx-str", "started_at": 300.0, "title": "rx-str",
            "messages": [
                {"session_id": "rx-str", "role": "assistant",
                 "content": "calling", "timestamp": 300.5,
                 "tool_calls": json.dumps(raw)}]}]
        a.write_sessions(foreign)
        line = json.loads((self.sessions / "rx-str.jsonl")
                          .read_text(encoding="utf-8").strip().splitlines()[0])
        self.assertEqual(line["tool_calls"],
                         [{"id": "call_1", "name": "read_file",
                           "arguments": "{\"path\": \"a\"}"}])

    def test_write_drops_unparseable_tool_calls(self):
        """A tool_calls value that cannot be parsed must be dropped rather
        than written verbatim (which would corrupt the whole session)."""
        a = ReasonixAdapter(sessions_dir=self.sessions)
        foreign = [{
            "id": "rx-bad", "started_at": 400.0, "title": "rx-bad",
            "messages": [
                {"session_id": "rx-bad", "role": "assistant",
                 "content": "c", "timestamp": 400.5,
                 "tool_calls": "not-json"}]}]
        a.write_sessions(foreign)
        line = json.loads((self.sessions / "rx-bad.jsonl")
                          .read_text(encoding="utf-8").strip().splitlines()[0])
        self.assertNotIn("tool_calls", line)

    def test_write_dedupes_normalized_file_by_content(self):
        """The reasonix desktop rewrites scanned transcripts (timestamps
        stripped, system prompt prepended), so a re-pull of the same
        messages carries fresh timestamps. Identical (role, content) must
        not be appended again or the file grows forever."""
        a = ReasonixAdapter(sessions_dir=self.sessions)
        p = self.sessions / "rx-norm.jsonl"
        # reasonix-normalized view: no timestamps
        p.write_text("\n".join([
            json.dumps({"role": "system", "content": "You are Reasonix"}),
            json.dumps({"role": "user", "content": "hello"}),
            json.dumps({"role": "assistant", "content": "hi"}),
        ]) + "\n", encoding="utf-8")
        a.write_sessions([{
            "id": "rx-norm", "started_at": 500.0, "title": "rx-norm",
            "messages": [
                {"session_id": "rx-norm", "role": "system",
                 "content": "You are Reasonix", "timestamp": 500.0},
                {"session_id": "rx-norm", "role": "user",
                 "content": "hello", "timestamp": 501.0},
                {"session_id": "rx-norm", "role": "assistant",
                 "content": "hi", "timestamp": 502.0}]}])
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 3, "identical content must not be re-appended")
        # a genuinely new message still lands
        a.write_sessions([{
            "id": "rx-norm", "started_at": 500.0, "title": "rx-norm",
            "messages": [
                {"session_id": "rx-norm", "role": "user",
                 "content": "new turn", "timestamp": 600.0}]}])
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("new turn", lines[-1])

    def test_registry_legacy_list_upgraded(self):
        """The foreign-id registry used to be a plain id list; the upgrade
        makes it {id: agent}. A legacy list must still answer _is_foreign
        and upgrade in place when the agent is recorded."""
        p = self.sessions / ".hermes-sync-foreign-ids.json"
        p.write_text(json.dumps(["legacy-hermes-1"]), encoding="utf-8")
        a = ReasonixAdapter(sessions_dir=self.sessions)
        self.assertTrue(a._is_foreign("legacy-hermes-1"))
        self.assertEqual(a._foreign_agent("legacy-hermes-1"), "")
        a._remember_foreign("legacy-hermes-1", "codex")
        self.assertEqual(a._foreign_agent("legacy-hermes-1"), "codex")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertEqual(data["legacy-hermes-1"], "codex")

    def test_foreign_bare_uuid_file_written(self):
        """Post-migration foreign ids are bare (workbuddy/magic uuid-style):
        valid Windows file names, so the session is written instead of being
        dropped by the colon check."""
        a = ReasonixAdapter(sessions_dir=self.sessions)
        stats = a.write_sessions([{
            "id": "3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06", "started_at": 1.0,
            "agent_type": "workbuddy",
            "messages": [{"session_id": "3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06",
                          "role": "user", "content": "wb", "timestamp": 1.0}]}])
        self.assertEqual(stats["imported"], 1)
        self.assertTrue((self.sessions / "3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06.jsonl").exists())
        # the owner agent is recorded for push tagging
        reg = json.loads((self.sessions / ".hermes-sync-foreign-ids.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(reg.get("3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06"), "workbuddy")

    def test_locked_session_skipped(self):
        # simulate a running reasonix holding the lock
        (self.sessions / "rx-001.jsonl.lock").write_text("12345")
        a = ReasonixAdapter(sessions_dir=self.sessions)
        stats = a.write_sessions([{
            "id": "rx-001", "started_at": 100.0,
            "messages": [
                {"session_id": "rx-001", "role": "user",
                 "content": "while running", "timestamp": 999.0}]}])
        self.assertEqual(stats["imported"], 0)
        self.assertEqual(stats["new_messages"], 0)
        # no lines appended
        content = (self.sessions / "rx-001.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("while running", content)

    def test_foreign_sessions_read_back(self):
        """Foreign (pulled) sessions stay in the push view: they may have
        been continued locally and their new messages must flow back;
        push_sessions tags them by owner and the server dedupes."""
        # A session pulled from the remote (bare id, no reasonix: prefix)
        # lands in the local dir but must NOT be read back for push.
        self.tmp2 = tempfile.TemporaryDirectory()
        try:
            sessions = Path(self.tmp2.name) / "sessions"
            sessions.mkdir(parents=True)
            # reasonix's own session (prefixed) + one pulled from hermes (bare)
            (sessions / "rx-001.jsonl").write_text(
                json.dumps({"role": "user", "content": "mine", "timestamp": 1.0})
                + "\n", encoding="utf-8")
            hermes_file = sessions / "20260801_221942_0be785.jsonl"
            hermes_file.write_text(
                json.dumps({"role": "user", "content": "from hermes",
                            "timestamp": 2.0}) + "\n", encoding="utf-8")
            a = ReasonixAdapter(sessions_dir=sessions)
            # mark the hermes id as foreign (as write_sessions would)
            a.write_sessions([{
                "id": "20260801_221942_0be785", "started_at": 2.0,
                "title": "hermes session",
                "messages": [{"session_id": "20260801_221942_0be785",
                              "role": "user", "content": "from hermes",
                              "timestamp": 2.0}]}])
            ids = [s["id"] for s in a.read_sessions()]
            self.assertIn("rx-001", ids)
            self.assertIn("20260801_221942_0be785", ids)
            # the foreign session must not carry the local-id title
            # fallback: pushing title=<id> would overwrite the server's
            # real title
            foreign = next(s for s in a.read_sessions()
                           if s["id"] == "20260801_221942_0be785")
            self.assertNotIn("title", foreign)
            # reasonix's own session keeps its title
            own = next(s for s in a.read_sessions() if s["id"] == "rx-001")
            self.assertEqual(own["title"], "rx-001")
        finally:
            self.tmp2.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
