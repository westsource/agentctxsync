"""Web-side sync pause: the batch endpoints and the import gate.

Covers POST /web/sync/pause|resume (multi-select form values, ownership
guard, same-origin return path, empty selection) and the Web import's skip
of paused sessions. The push-side freeze itself lives in test_sync.py
(PushTest's sync-pause block).
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workspace  # noqa: E402
from translations import TRANSLATIONS, get_translations  # noqa: E402


class Form(dict):
    def getlist(self, key):
        value = self.get(key)
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]


class FormRequest:
    """Request stub: `.form()` (awaitable) + query params."""

    def __init__(self, form=None, query=None):
        self._form = Form(form or {})
        self.query_params = query or {}

    async def form(self):
        return self._form


class PauseCursor:
    """Answers the two statements the endpoint issues and records the rest.

    ``owned`` = the workspace ids the ownership query may return;
    ``rowcounts`` = the UPDATE rowcount per owned workspace, in order."""

    def __init__(self, owned=(), rowcounts=()):
        self.owned = set(owned)
        self.executed = []
        self.rowcount = 0
        self._rowcounts = list(rowcounts)
        self._next = iter(())

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.startswith("SELECT id FROM workspaces"):
            self._next = iter([(w,) for w in (params[1] or ()) if w in self.owned])
        elif sql.startswith("UPDATE sessions"):
            self.rowcount = self._rowcounts.pop(0) if self._rowcounts else 0

    def fetchall(self):
        return list(self._next)


class PauseConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **k):
        return self._cursor

    def commit(self):
        pass


class PauseCtx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


def call_pause_endpoint(route, form=None, query=None, owned=(), rowcounts=(),
                        user=("7", True)):
    """Run one pause/resume route with a stubbed DB; returns (resp, cursor, flash)."""
    cursor = PauseCursor(owned=owned, rowcounts=rowcounts)
    captured = {}
    patchers = [
        mock.patch.object(workspace, "get_current_user",
                          return_value={"sub": user[0]} if user[1] else None,
                          side_effect=None if user[1] else Exception("no user")),
        mock.patch.object(workspace, "get_conn",
                          return_value=PauseCtx(PauseConn(cursor))),
        mock.patch.object(workspace, "get_lang", return_value="zh-CN"),
        mock.patch.object(workspace, "get_translations",
                          return_value={"sync_pause_ok": "已暂停 %s 个会话的同步",
                                        "sync_resume_ok": "已恢复 %s 个会话的同步",
                                        "sync_select_none": "未选择任何会话"}),
        mock.patch.object(workspace, "make_flash",
                          side_effect=lambda resp, msg, cat="success":
                          captured.update(message=msg, category=cat)),
    ]
    for p in patchers:
        p.start()
    try:
        resp = asyncio.run(route(FormRequest(form, query)))
    finally:
        for p in patchers:
            p.stop()
    return resp, cursor, captured


def updates(cursor):
    return [params for sql, params in cursor.executed
            if sql.startswith("UPDATE sessions")]


class SelPairsTest(unittest.TestCase):
    """`sel` values are "<ws_id>:<session_id>" -- only the FIRST colon is the
    separator, because session ids carry profile prefixes of their own."""

    def test_groups_by_workspace(self):
        sel = Form({"sel": ["3:a", "3:b", "4:c"]})
        self.assertEqual(workspace._sel_pairs(sel), {3: {"a", "b"}, 4: {"c"}})

    def test_session_id_keeps_its_own_colons(self):
        sel = Form({"sel": ["7:default:abc", "7:work:xyz"]})
        self.assertEqual(workspace._sel_pairs(sel),
                         {7: {"default:abc", "work:xyz"}})

    def test_malformed_values_are_dropped(self):
        sel = Form({"sel": ["no-colon", "abc:s1", "3:", ":s1", "", "3:s1"]})
        self.assertEqual(workspace._sel_pairs(sel), {3: {"s1"}})

    def test_missing_field_is_an_empty_selection(self):
        self.assertEqual(workspace._sel_pairs(Form()), {})


class SyncPauseRouteTest(unittest.TestCase):
    def test_single_selection_pauses_one_session(self):
        resp, cursor, flash = call_pause_endpoint(
            workspace.web_sync_pause,
            form={"sel": "3:s1", "next": "/web/workspace/3"},
            owned=(3,), rowcounts=(1,))
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/workspace/3")
        self.assertEqual(len(updates(cursor)), 1)
        paused, paused_at, ws_id, ids = updates(cursor)[0]
        self.assertEqual((paused, ws_id, ids), (1, 3, ["s1"]))
        self.assertIsInstance(paused_at, float)
        self.assertEqual(flash["message"], "已暂停 1 个会话的同步")

    def test_batch_across_workspaces_updates_each_owned_one(self):
        resp, cursor, flash = call_pause_endpoint(
            workspace.web_sync_pause,
            form={"sel": ["3:a", "3:b", "4:c"], "next": "/web/all-sessions"},
            owned=(3, 4), rowcounts=(2, 1))
        self.assertEqual([params[2] for params in updates(cursor)], [3, 4])
        self.assertEqual(updates(cursor)[0][3], ["a", "b"])
        self.assertEqual(updates(cursor)[1][3], ["c"])
        self.assertEqual(flash["message"], "已暂停 3 个会话的同步")

    def test_unowned_workspace_is_never_touched(self):
        # A crafted form naming someone else's workspace: the ownership query
        # is the only thing that runs, and no session row is updated.
        resp, cursor, flash = call_pause_endpoint(
            workspace.web_sync_pause, form={"sel": "9:s1"}, owned=())
        self.assertEqual(updates(cursor), [])
        self.assertTrue(any(sql.startswith("SELECT id FROM workspaces")
                            for sql, _ in cursor.executed))
        self.assertEqual(flash["message"], "已暂停 0 个会话的同步")

    def test_resume_clears_the_pause_timestamp(self):
        resp, cursor, flash = call_pause_endpoint(
            workspace.web_sync_resume,
            form={"sel": "3:s1", "next": "/web/workspace/3/session/s1"},
            owned=(3,), rowcounts=(1,))
        paused, paused_at, ws_id, ids = updates(cursor)[0]
        self.assertEqual((paused, paused_at, ws_id, ids), (0, None, 3, ["s1"]))
        self.assertEqual(flash["message"], "已恢复 1 个会话的同步")

    def test_empty_selection_reports_an_error_without_querying(self):
        resp, cursor, flash = call_pause_endpoint(
            workspace.web_sync_pause, form={"next": "/web/"}, owned=(3,))
        self.assertEqual(updates(cursor), [])
        self.assertEqual(cursor.executed, [])
        self.assertEqual(flash, {"message": "未选择任何会话", "category": "error"})

    def test_return_path_stays_same_origin(self):
        _, _, _ = call_pause_endpoint(
            workspace.web_sync_pause, form={"sel": "3:s1"})
        for target, expected in (("//evil.example/x", "/web/"),
                                 ("https://evil.example/x", "/web/"),
                                 ("/web/workspace/3", "/web/workspace/3")):
            resp, _, _ = call_pause_endpoint(
                workspace.web_sync_pause, form={"sel": "3:s1", "next": target},
                owned=(3,), rowcounts=(1,))
            self.assertEqual(resp.headers["location"], expected)

    def test_anonymous_request_goes_to_login(self):
        resp, cursor, _ = call_pause_endpoint(
            workspace.web_sync_pause, form={"sel": "3:s1"}, user=("7", False))
        self.assertEqual(resp.headers["location"], "/web/login")
        self.assertEqual(cursor.executed, [])


class ImportPauseTest(unittest.TestCase):
    """The Web import merges like /push, so it must skip paused sessions too
    (counted in the flash, never silently dropped)."""

    class Upload:
        def __init__(self, payload):
            self._payload = payload

        async def read(self):
            return self._payload

        async def close(self):
            pass

    class ImportCursor:
        def __init__(self, paused=()):
            self.paused = set(paused)
            self.executed = []
            self.rowcount = 0
            self._next = iter(())

        def execute(self, sql, params=None):
            self.executed.append((sql, params))
            if sql.startswith("SELECT id FROM workspaces"):
                self._next = iter([(1,)])
            elif "information_schema.columns" in sql:
                self._next = iter([("id",), ("title",), ("last_synced_at",),
                                   ("sync_paused",)] if "sessions" in sql
                                  else [("id",), ("session_id",), ("role",),
                                        ("timestamp",), ("content",)])
            elif "COALESCE(sync_paused,0) = 1" in sql:
                self._next = iter([(sid,) for sid in sorted(self.paused)])
            else:
                self._next = iter(())

        def fetchone(self):
            return next(self._next, None)

        def fetchall(self):
            return list(self._next)

    def _import(self, sessions, paused=()):
        cursor = self.ImportCursor(paused=paused)
        captured = {}
        payload = json.dumps({"format": "hermes-sync-sessions", "version": 1,
                              "sessions": sessions}).encode("utf-8")

        class Request:
            query_params: dict = {}

            async def form(self):
                return {"file": self.upload}

        request = Request()
        request.upload = self.Upload(payload)
        patchers = [
            mock.patch.object(workspace, "get_current_user",
                              return_value={"sub": "7"}),
            mock.patch.object(workspace, "get_conn",
                              return_value=PauseCtx(PauseConn(cursor))),
            mock.patch.object(workspace, "get_lang", return_value="zh-CN"),
            mock.patch.object(workspace, "get_translations",
                              return_value={"ws_import_invalid": "invalid",
                                            "ws_import_version": "version",
                                            "ws_import_ok":
                                                "导入完成：新增 %s 会话、更新 %s 会话、"
                                                "新增 %s 条消息、跳过 %s 条重复",
                                            "ws_import_paused":
                                                "（跳过 %s 个已暂停同步的会话）"}),
            mock.patch.object(workspace, "make_flash",
                              side_effect=lambda resp, msg, cat="success":
                              captured.update(message=msg, category=cat)),
        ]
        for p in patchers:
            p.start()
        try:
            resp = asyncio.run(workspace.web_workspace_import(1, request))
        finally:
            for p in patchers:
                p.stop()
        return resp, cursor, captured

    def test_paused_session_is_skipped_and_reported(self):
        sessions = [
            {"id": "s1", "title": "frozen",
             "messages": [{"role": "user", "content": "keep out",
                           "timestamp": 1.0}]},
            {"id": "s2", "title": "imported", "messages": []},
        ]
        resp, cursor, flash = self._import(sessions, paused=("s1",))
        self.assertEqual(resp.status_code, 303)
        # not one statement carries the paused session: no session upsert, no
        # message insert (the pause query itself only carries the workspace id)
        self.assertEqual([(sql, params) for sql, params in cursor.executed
                          if params and "s1" in str(params)], [])
        self.assertTrue(any(sql.startswith("INSERT INTO sessions") and "s2" in str(params)
                            for sql, params in cursor.executed))
        self.assertIn("跳过 1 个已暂停同步的会话", flash["message"])

    def test_unpaused_import_flash_has_no_pause_note(self):
        sessions = [{"id": "s2", "title": "imported", "messages": []}]
        _, _, flash = self._import(sessions)
        self.assertNotIn("已暂停", flash["message"])


class PauseTranslationsTest(unittest.TestCase):
    def test_both_languages_carry_the_pause_terms(self):
        keys = ("sync_pause_badge", "sync_pause_badge_hint", "sync_pause_btn",
                "sync_resume_btn", "sync_pause_btn_hint", "sync_resume_btn_hint",
                "sync_pause_confirm", "sync_resume_confirm", "sync_pause_ok",
                "sync_resume_ok", "sync_select_none", "sync_bulk_hint",
                "sync_bulk_pause", "sync_bulk_resume", "sync_bulk_select_all",
                "ws_import_paused")
        for lang in TRANSLATIONS:
            t = get_translations(lang)
            for key in keys:
                self.assertIn(key, t, f"{key} missing in {lang}")
            self.assertIn("%s", t["sync_pause_ok"])
            self.assertIn("%s", t["sync_resume_ok"])
            self.assertIn("%s", t["ws_import_paused"])


if __name__ == "__main__":
    unittest.main()
