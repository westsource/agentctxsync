"""requestlog channel/kind classification + access-counting tests.

Pins the observable contract of the admin access-statistics feature:
the Host header decides the channel (hostname -> 'domain', IP literal ->
'ip'), the path decides the kind ('/web/*' and '/' -> 'web', else 'api'),
every counted request triggers one upsert into access_stats, and
static/health traffic is excluded.
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
        rec.assert_called_once_with("www.agentctxsync.com", "/web/login")

    def test_ip_host_passed_through(self):
        rec = self._run("/web/login", host="47.95.214.236:8765")
        rec.assert_called_once_with("47.95.214.236:8765", "/web/login")

    def test_root_landing_counted_as_web(self):
        rec = self._run("/", host="www.agentctxsync.com")
        rec.assert_called_once_with("www.agentctxsync.com", "/")

    def test_sync_post_counted_as_api(self):
        rec = self._run("/push", host="www.agentctxsync.com", method="POST")
        rec.assert_called_once_with("www.agentctxsync.com", "/push")

    def test_static_health_favicon_skipped(self):
        for path in ("/static/app.js", "/static/favicon.svg", "/health", "/favicon.ico"):
            rec = self._run(path)
            rec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
