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
        for h in ("47.95.214.236:8765", "47.95.214.236", "127.0.0.1:8765",
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
        _, params = self._record("47.95.214.236:8765", "/push")
        self.assertEqual(params[1], "ip")
        self.assertEqual(params[2], "api")

    def test_db_failure_swallowed(self):
        with mock.patch.object(requestlog, "get_conn", side_effect=RuntimeError("pg down")):
            requestlog._record_access("www.agentctxsync.com", "/web/login")  # must not raise


class RecordDeviceTest(unittest.TestCase):
    """Per-device access rows: sync clients with a device_id get a daily
    (device, channel) counter so the admin drill-down can tell which
    machines use the domain vs direct IP."""

    def _record(self, host, path, device_id="", client_version=""):
        cur = FakeCursor()
        with mock.patch.object(requestlog, "get_conn",
                               return_value=FakeCtx(FakeConn(cur))):
            requestlog._record_access(host, path, device_id, client_version)
        return cur.executed

    def test_domain_device_upsert(self):
        executed = self._record("www.agentctxsync.com", "/push", "my-pc")
        self.assertEqual(len(executed), 2)
        sql, params = executed[1]
        self.assertIn("INSERT INTO access_device", sql)
        self.assertIn("ON CONFLICT", sql)
        self.assertEqual(params[0], date.today())
        self.assertEqual(params[1], "my-pc")
        self.assertEqual(params[2], "domain")
        self.assertIsInstance(params[3], float)  # last_seen epoch

    def test_client_version_stored(self):
        # sync requests report the MCP version; it lands in the device row
        executed = self._record("www.agentctxsync.com", "/push", "my-pc",
                                "2026.08.21.1")
        sql, params = executed[1]
        self.assertIn("client_version", sql)
        self.assertIn("COALESCE(EXCLUDED.client_version", sql)
        self.assertEqual(params[4], "2026.08.21.1")

    def test_empty_client_version_stays_null(self):
        # requests without a version must not wipe the recorded one
        executed = self._record("www.agentctxsync.com", "/push", "my-pc")
        self.assertIsNone(executed[1][1][4])

    def test_ip_device_upsert(self):
        _, params = self._record("47.95.214.236:8765", "/pull", "box-2")[1]
        self.assertEqual(params[1], "box-2")
        self.assertEqual(params[2], "ip")

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
        rec.assert_called_once_with("www.agentctxsync.com", "/status/my-pc", "my-pc", "")

    def test_sync_post_body_device_extracted(self):
        # /push /pull carry device_id + client_version in the POST body
        body = b'{"device_id": "my-pc", "client_version": "2026.08.21.1", "sessions": []}'
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
            "headers": [(b"host", b"47.95.214.236:8765")],
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
        rec.assert_called_once_with("47.95.214.236:8765", "/push", "my-pc", "2026.08.21.1")


class MiddlewareCountingTest(unittest.TestCase):
    def _run(self, path, host="www.agentctxsync.com", method="GET"):
        scope = {
            "type": "http", "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "",
            "headers": [(b"host", host.encode()), (b"user-agent", b"test")],
            "client": ("1.2.3.4", 1234), "server": ("127.0.0.1", 8765),
        }
        from fastapi import Request

        async def call_next(_req):
            return SimpleNamespace(status_code=200)

        rec = mock.Mock()
        with mock.patch.object(requestlog, "_record_access", rec):
            asyncio.run(requestlog.request_log_middleware(Request(scope), call_next))
        return rec

    def test_web_request_counted_with_host(self):
        rec = self._run("/web/login", host="www.agentctxsync.com")
        rec.assert_called_once_with("www.agentctxsync.com", "/web/login", "", "")

    def test_ip_host_passed_through(self):
        rec = self._run("/web/login", host="47.95.214.236:8765")
        rec.assert_called_once_with("47.95.214.236:8765", "/web/login", "", "")

    def test_root_landing_counted_as_web(self):
        rec = self._run("/", host="www.agentctxsync.com")
        rec.assert_called_once_with("www.agentctxsync.com", "/", "", "")

    def test_sync_post_counted_as_api(self):
        rec = self._run("/push", host="www.agentctxsync.com", method="POST")
        rec.assert_called_once_with("www.agentctxsync.com", "/push", "", "")

    def test_static_health_favicon_skipped(self):
        for path in ("/static/app.js", "/static/favicon.svg", "/health", "/favicon.ico"):
            rec = self._run(path)
            rec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
