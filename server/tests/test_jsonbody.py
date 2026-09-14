"""JSON body hardening: a body that is not a JSON object is a 400, not a 500.

Regression for the live 500s: handlers called `await request.json()` and let
`json.JSONDecodeError` escape (unparseable body), or crashed on `.get()` when
the body parsed but was not an object (e.g. `[1]`). Both are client mistakes
and must fail before any DB work.
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

from fastapi import HTTPException

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auth  # noqa: E402
import jsonbody  # noqa: E402
import sync  # noqa: E402


class FakeRequest:
    """Minimal Request stand-in: `json()` mirrors Starlette's contract."""

    def __init__(self, raw):
        self._raw = raw

    async def json(self):
        return json.loads(self._raw)


def parse(raw):
    """(value, status) -- status is None when json_object() returned."""
    try:
        return asyncio.run(jsonbody.json_object(FakeRequest(raw))), None
    except HTTPException as e:
        return None, e.status_code


class JsonObjectTest(unittest.TestCase):
    def test_valid_object_passes_through(self):
        value, status = parse('{"name": "ws", "n": 1}')
        self.assertEqual(value, {"name": "ws", "n": 1})
        self.assertIsNone(status)

    def test_malformed_body_is_400(self):
        for raw in ("not json", "", '{"name": ', "username=x&password=y"):
            self.assertEqual(parse(raw)[1], 400, f"body={raw!r}")

    def test_non_object_json_is_400(self):
        for raw in ("[1, 2]", '"text"', "42", "null", "true"):
            self.assertEqual(parse(raw)[1], 400, f"body={raw!r}")


class RouteContractTest(unittest.TestCase):
    """What the client sees: the parse rejects before quota/DB work, so the
    request fails fast with 400 instead of blowing up as an unhandled 500."""

    def test_api_login_rejects_malformed_body(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(auth.api_login(FakeRequest("username=probe&password=x")))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_api_login_rejects_non_object_body(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(auth.api_login(FakeRequest('["probe"]')))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_sync_pull_rejects_malformed_body(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(sync.pull(FakeRequest("<xml/>"),
                                  ws={"workspace_id": 1, "user_id": 1}))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_sync_push_rejects_malformed_body(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(sync.push(FakeRequest(""),
                                  ws={"workspace_id": 1, "user_id": 1}))
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
