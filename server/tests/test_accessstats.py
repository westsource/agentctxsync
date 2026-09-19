"""requestlog channel/kind classification + access-counting tests.

Pins the observable contract of the admin access-statistics feature:
the Host header decides the channel (hostname -> 'domain', IP literal ->
'ip'), the path decides the kind ('/web/*' and '/' -> 'web', else 'api'),
every counted request triggers one upsert into access_stats, requests
carrying a sync client's device_id (POST body or /status/<device> path)
also bump the device's access_device row, and static/health traffic is
excluded.
"""
import asyncio
import contextlib
import io
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requestlog  # noqa: E402

from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402


class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **k):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass


class FakeCtx:
    """Stand-in for the get_conn() context manager."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class ClassifyChannelTest(unittest.TestCase):
    def test_hostnames_are_domain(self):
        for h in ("www.agentctxsync.com", "agentctxsync.com:8765",
                  "WEB.agentctxsync.com", "sub.example.org"):
            self.assertEqual(requestlog.classify_channel(h), "domain", h)

    def test_ip_literals_are_ip(self):
        for h in ("203.0.113.7:8765", "203.0.113.7", "127.0.0.1:8765",
                  "localhost:8765", "[::1]:8765", "2001:db8::1"):
            self.assertEqual(requestlog.classify_channel(h), "ip", h)

    def test_empty_host_is_ip(self):
        self.assertEqual(requestlog.classify_channel(""), "ip")
        self.assertEqual(requestlog.classify_channel(None), "ip")


class ClassifyKindTest(unittest.TestCase):
    def test_web_paths(self):
        for p in ("/", "/web/", "/web/login", "/web/admin/access"):
            self.assertEqual(requestlog.classify_kind(p), "web", p)

    def test_api_paths(self):
        for p in ("/push", "/pull", "/api/projects/push", "/api/projects/pull",
                  "/api/client/download", "/status/dev1"):
            self.assertEqual(requestlog.classify_kind(p), "api", p)


class RecordAccessTest(unittest.TestCase):
    def _record(self, host, path):
        cur = FakeCursor()
        with mock.patch.object(requestlog, "get_conn",
                               return_value=FakeCtx(FakeConn(cur))):
            requestlog._record_access(host, path)
        return cur.executed[0]

    def test_upsert_domain_web(self):
        sql, params = self._record("www.agentctxsync.com", "/web/login")
        self.assertIn("INSERT INTO access_stats", sql)
        self.assertIn("ON CONFLICT", sql)
        self.assertEqual(params[0], date.today())
        self.assertEqual(params[1], "domain")
        self.assertEqual(params[2], "web")

    def test_upsert_ip_api(self):
        _, params = self._record("203.0.113.7:8765", "/push")
        self.assertEqual(params[1], "ip")
        self.assertEqual(params[2], "api")

    def test_db_failure_swallowed(self):
        with mock.patch.object(requestlog, "get_conn", side_effect=RuntimeError("pg down")):
            requestlog._record_access("www.agentctxsync.com", "/web/login")  # must not raise


class RecordDeviceTest(unittest.TestCase):
    """Per-device access rows: sync clients with a device_id get a daily
    (device, channel) counter so the admin drill-down can tell which
    machines use the domain vs direct IP."""

    def _record(self, host, path, device_id="", client_version="", agent="",
                user_id=0):
        cur = FakeCursor()
        with mock.patch.object(requestlog, "get_conn",
                               return_value=FakeCtx(FakeConn(cur))):
            requestlog._record_access(host, path, device_id, client_version,
                                      agent, user_id)
        return cur.executed

    def test_domain_device_upsert(self):
        executed = self._record("www.agentctxsync.com", "/push", "my-pc")
        self.assertEqual(len(executed), 2)
        sql, params = executed[1]
        self.assertIn("INSERT INTO access_device", sql)
        self.assertIn("ON CONFLICT", sql)
        self.assertEqual(params[0], date.today())
        self.assertEqual(params[1], "my-pc")
        self.assertEqual(params[2], "unknown")  # legacy: no agent reported
        self.assertEqual(params[3], "domain")
        self.assertIsInstance(params[4], float)  # last_seen epoch

    def test_client_version_stored(self):
        # sync requests report the MCP version; it lands in that agent's row
        executed = self._record("www.agentctxsync.com", "/push", "my-pc",
                                "2026.08.21.1", "hermes")
        sql, params = executed[1]
        self.assertIn("client_version", sql)
        self.assertIn("COALESCE(EXCLUDED.client_version", sql)
        self.assertEqual(params[2], "hermes")
        self.assertEqual(params[5], "2026.08.21.1")

    def test_owning_user_stored(self):
        # the workspace owner resolved from the client's API key lands in the
        # row, and is part of the upsert key (a device_id is not unique per
        # user: one box can sync two accounts)
        executed = self._record("www.agentctxsync.com", "/push", "my-pc",
                                "2026.08.21.1", "hermes", user_id=7)
        sql, params = executed[1]
        self.assertIn("user_id", sql)
        self.assertIn("ON CONFLICT (stat_date, device_id, agent, channel, user_id)",
                      sql)
        self.assertEqual(params[6], 7)

    def test_unattributed_requests_store_zero(self):
        # no resolved user (invalid/master key, or a non-sync route): the row
        # is written as 0 rather than dropped, so the counter is not lost
        for kwargs in ({}, {"user_id": None}):
            with self.subTest(kwargs=kwargs):
                _, params = self._record("www.agentctxsync.com", "/push",
                                         "my-pc", **kwargs)[1]
                self.assertEqual(params[6], 0)

    def test_agent_reported_not_unknown(self):
        # a client that reports its agent stores that agent's own row/version
        executed = self._record("www.agentctxsync.com", "/push", "box-1",
                                "2026.08.21.1", "reasonix")
        sql, params = executed[1]
        self.assertIn("agent", sql)  # the INSERT carries an agent column
        self.assertEqual(params[2], "reasonix")

    def test_empty_client_version_stays_null(self):
        # requests without a version must not wipe the recorded one
        executed = self._record("www.agentctxsync.com", "/push", "my-pc")
        self.assertIsNone(executed[1][1][5])

    def test_ip_device_upsert(self):
        for host, expected_channel in (("203.0.113.7:8765", "ip"),):
            _, params = self._record(host, "/pull", "box-2")[1]
            self.assertEqual(params[1], "box-2")
            self.assertEqual(params[2], "unknown")
            self.assertEqual(params[3], expected_channel)

    def test_no_device_no_extra_row(self):
        executed = self._record("www.agentctxsync.com", "/web/login")
        self.assertEqual(len(executed), 1)
        self.assertIn("INSERT INTO access_stats", executed[0][0])

    def test_status_path_device_extracted(self):
        # /status/<device_id> carries the device in the path, not the body
        rec = mock.Mock()
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": "/status/my-pc", "raw_path": b"/status/my-pc",
            "query_string": b"", "root_path": "",
            "headers": [(b"host", b"www.agentctxsync.com")],
            "client": ("1.2.3.4", 1234), "server": ("127.0.0.1", 8765),
        }
        from fastapi import Request

        async def call_next(_req):
            return SimpleNamespace(status_code=200)

        with mock.patch.object(requestlog, "_record_access", rec):
            asyncio.run(requestlog.request_log_middleware(Request(scope), call_next))
        rec.assert_called_once_with("www.agentctxsync.com", "/status/my-pc", "my-pc", "", "", 0)

    def test_sync_post_body_device_extracted(self):
        # /push /pull carry device_id + client_version + agent in the POST body
        body = (b'{"device_id": "my-pc", "client_version": "2026.08.21.1", '
                b'"agent": "hermes", "sessions": []}')
        calls = {"n": 0}

        async def receive():
            calls["n"] += 1
            if calls["n"] == 1:
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        scope = {
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": "/push", "raw_path": b"/push",
            "query_string": b"", "root_path": "",
            "headers": [(b"host", b"203.0.113.7:8765")],
            "client": ("1.2.3.4", 1234), "server": ("127.0.0.1", 8765),
            "receive": receive,
        }
        from fastapi import Request

        async def call_next(_req):
            return SimpleNamespace(status_code=200)

        rec = mock.Mock()
        with mock.patch.object(requestlog, "_record_access", rec):
            asyncio.run(requestlog.request_log_middleware(
                Request(scope, receive=receive), call_next))
        rec.assert_called_once_with("203.0.113.7:8765", "/push", "my-pc",
                                    "2026.08.21.1", "hermes", 0)


class RequestLogSourceTest(unittest.TestCase):
    """The REQ line's src is the resolved peer address. X-Forwarded-For's
    leftmost entry is caller-supplied, so reading it as fact let anyone forge
    the audit trail (the limiter was never fooled — it uses client.host)."""

    def _line(self, headers, client=("198.51.100.5", 4444)):
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": "/web/login", "raw_path": b"/web/login",
            "query_string": b"", "root_path": "",
            "headers": headers,
            "client": client, "server": ("127.0.0.1", 8765),
        }
        from fastapi import Request

        async def call_next(_req):
            return SimpleNamespace(status_code=200)

        buf = io.StringIO()
        with mock.patch.object(requestlog, "_record_access"), \
                contextlib.redirect_stdout(buf):
            asyncio.run(requestlog.request_log_middleware(Request(scope), call_next))
        return buf.getvalue()

    def test_spoofed_forwarded_header_cannot_become_src(self):
        line = self._line([(b"host", b"www.agentctxsync.com"),
                           (b"x-forwarded-for", b"203.0.113.9")])
        self.assertIn("src=198.51.100.5", line)
        self.assertIn("xff=203.0.113.9", line)   # kept, but never as the source

    def test_no_forwarded_header_omits_the_field(self):
        line = self._line([(b"host", b"www.agentctxsync.com")])
        self.assertIn("src=198.51.100.5", line)
        self.assertNotIn("xff=", line)


class MiddlewareCountingTest(unittest.TestCase):
    def _run(self, path, host="www.agentctxsync.com", method="GET", state=None):
        scope = {
            "type": "http", "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "",
            "headers": [(b"host", host.encode()), (b"user-agent", b"test")],
            "client": ("1.2.3.4", 1234), "server": ("127.0.0.1", 8765),
        }
        if state is not None:
            scope["state"] = dict(state)
        from fastapi import Request

        async def call_next(_req):
            return SimpleNamespace(status_code=200)

        rec = mock.Mock()
        with mock.patch.object(requestlog, "_record_access", rec):
            asyncio.run(requestlog.request_log_middleware(Request(scope), call_next))
        return rec

    def test_web_request_counted_with_host(self):
        rec = self._run("/web/login", host="www.agentctxsync.com")
        rec.assert_called_once_with("www.agentctxsync.com", "/web/login", "", "", "", 0)

    def test_ip_host_passed_through(self):
        rec = self._run("/web/login", host="203.0.113.7:8765")
        rec.assert_called_once_with("203.0.113.7:8765", "/web/login", "", "", "", 0)

    def test_root_landing_counted_as_web(self):
        rec = self._run("/", host="www.agentctxsync.com")
        rec.assert_called_once_with("www.agentctxsync.com", "/", "", "", "", 0)

    def test_sync_post_counted_as_api(self):
        rec = self._run("/push", host="www.agentctxsync.com", method="POST")
        rec.assert_called_once_with("www.agentctxsync.com", "/push", "", "", "", 0)

    def test_static_health_favicon_skipped(self):
        for path in ("/static/app.js", "/static/favicon.svg", "/health", "/favicon.ico"):
            rec = self._run(path)
            rec.assert_not_called()

    def test_authenticated_user_attributed(self):
        # get_workspace_by_api_key leaves the owner on request.state; the
        # middleware must pick it up and attribute the access row
        rec = self._run("/push", method="POST", state={"ws_user_id": 7})
        rec.assert_called_once_with("www.agentctxsync.com", "/push", "", "", "", 7)

    def test_unset_state_stays_unattributed(self):
        # invalid / master key: state carries 0 (or nothing at all)
        for state in ({}, {"ws_user_id": 0}, {"ws_user_id": None}):
            with self.subTest(state=state):
                rec = self._run("/push", method="POST", state=state)
                rec.assert_called_once_with("www.agentctxsync.com", "/push",
                                            "", "", "", 0)


class UserAttributionPropagationTest(unittest.TestCase):
    """The owner is published on request.state by a FastAPI dependency, i.e.
    INSIDE the downstream app, while the counter is written by an outer
    BaseHTTPMiddleware. That only works because both share the ASGI scope's
    state dict -- pin it, otherwise attribution would silently degrade to 0
    for every request (no test would otherwise notice)."""

    def _call(self, state_writer):
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": "/push", "raw_path": b"/push",
            "query_string": b"", "root_path": "",
            "headers": [(b"host", b"www.agentctxsync.com")],
            "client": ("1.2.3.4", 1234), "server": ("127.0.0.1", 8765),
        }
        sent = []

        async def inner(scope, receive, send):
            state_writer(scope)
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)

        app = BaseHTTPMiddleware(inner, dispatch=requestlog.request_log_middleware)
        rec = mock.Mock()
        with mock.patch.object(requestlog, "_record_access", rec):
            asyncio.run(app(scope, receive, send))
        return rec, sent

    def test_scope_state_reaches_the_outer_middleware(self):
        # mimics the dependency: mutate the scope's state dict downstream
        rec, sent = self._call(
            lambda scope: scope.setdefault("state", {}).update({"ws_user_id": 7}))
        self.assertEqual(sent[0]["status"], 200)
        rec.assert_called_once_with("www.agentctxsync.com", "/push", "", "", "", 7)

    def test_response_without_state_is_unattributed(self):
        rec, sent = self._call(lambda scope: None)
        self.assertEqual(sent[0]["status"], 200)
        rec.assert_called_once_with("www.agentctxsync.com", "/push", "", "", "", 0)


if __name__ == "__main__":
    unittest.main()
