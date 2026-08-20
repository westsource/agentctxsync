"""Global sidebar quota injection (render._sidebar_quota).

The quota moved from the dashboard to the shared sidebar: every logged-in
page gets plan + usage via render.py's global context, and it must stay
invisible for guests and deployments without a limited plan.
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auth  # noqa: E402
import invites  # noqa: E402
import render  # noqa: E402


class FakeCursor:
    def __init__(self, fetchone_results=()):
        self.rows = list(fetchone_results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **k):
        return self._cursor


class FakeCtx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class FakeRequest:
    def __init__(self, token="tok"):
        self._token = token

    @property
    def cookies(self):
        return {"hsync_token": self._token}

    @property
    def query_params(self):
        return {}


class SidebarQuotaTest(unittest.TestCase):
    def _call(self, request, ui_active=True, plan="free"):
        cursor = FakeCursor([(plan,), (29,)])
        conn = FakeCtx(FakeConn(cursor))
        patchers = [
            mock.patch.object(render, "_current_request_var",
                              mock.MagicMock(get=lambda: request)),
            mock.patch.object(auth, "verify_jwt",
                              return_value={"sub": "7", "lang": "zh-CN"}),
            mock.patch.object(invites, "quota_ui_active", return_value=ui_active),
            mock.patch.object(render, "get_conn", return_value=conn),
            mock.patch.object(render, "plan_limits", return_value=(300, None)),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        return render._sidebar_quota(), cursor

    def test_guest_no_cookie_returns_none(self):
        quota, _ = self._call(FakeRequest(token=None))
        self.assertIsNone(quota)
        auth.verify_jwt.assert_not_called()

    def test_no_request_context_returns_none(self):
        quota, _ = self._call(None)
        self.assertIsNone(quota)

    def test_ui_inactive_returns_none(self):
        quota, cursor = self._call(FakeRequest(), ui_active=False)
        self.assertIsNone(quota)
        # No plan/usage queries ran.
        self.assertEqual(cursor.executed, [])

    def test_active_returns_plan_and_usage(self):
        quota, cursor = self._call(FakeRequest())
        self.assertEqual(quota, {"plan": "free", "max_sessions": 300, "active_count": 29})
        self.assertEqual(len(cursor.executed), 2)  # plan lookup + active count

    def test_unlimited_plan_shown(self):
        quota, _ = self._call(FakeRequest(), plan="unlimited")
        self.assertEqual(quota["plan"], "unlimited")

    def test_render_injects_quota_into_context(self):
        # render() sets ctx['quota'] via setdefault; a router-provided value
        # wins. Patch the pieces render() touches.
        cursor = FakeCursor([("free",), (29,)])
        captured = {}
        tmpl_mock = mock.MagicMock()
        tmpl_mock.render.side_effect = lambda ctx: captured.update(ctx) or "html"
        patchers = [
            mock.patch.object(render, "_current_request_var",
                              mock.MagicMock(get=lambda: FakeRequest())),
            mock.patch.object(auth, "verify_jwt",
                              return_value={"sub": "7"}),
            mock.patch.object(invites, "quota_ui_active", return_value=True),
            mock.patch.object(render, "get_conn", return_value=FakeCtx(FakeConn(cursor))),
            mock.patch.object(render, "plan_limits", return_value=(300, None)),
            mock.patch.object(render, "get_lang", return_value="zh-CN"),
            mock.patch.object(render, "get_translations", return_value={}),
            mock.patch.object(render.jinja_env, "get_template", return_value=tmpl_mock),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        resp = render.render("base.html")
        self.assertEqual(resp.body, b"html")
        # The template received the injected quota.
        self.assertEqual(captured["quota"]["max_sessions"], 300)
        self.assertEqual(captured["quota"]["plan"], "free")


if __name__ == "__main__":
    unittest.main()
