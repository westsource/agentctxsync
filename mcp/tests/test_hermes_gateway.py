"""Protocol half of the gateway-RPC write path, tested without Hermes.

``hermes_gateway_write.run`` takes an injectable ``connect`` factory, so the
whole decision surface — probe verdict, per-session import, contract drift,
discovery failure — is exercised here against a fake gateway. The real client
(``hermes_cli.gateway_client``) is only used by ``default_connect``, which the
live smoke test covers.
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hermes_gateway_write as hgw  # noqa: E402


class RpcError(Exception):
    """Stand-in for GatewayRPCError: bounded reason + numeric code."""

    def __init__(self, reason, code=4001):
        super().__init__(reason)
        self.code = code


class FakeGateway:
    def __init__(self, *, probe_reason="not_found", import_reasons=None,
                 skipped=()):
        self.probe_reason = probe_reason
        self.import_reasons = dict(import_reasons or {})
        self.skipped = set(skipped)
        self.calls = []

    async def rpc(self, method, **params):
        if method == "runtime.describe":
            return {"instance_id": "inst-1", "profile_id": "default"}
        if method != "session.mutate":
            raise RpcError("unknown_method", -32601)
        self.calls.append(params)
        sid = params["session_id"]
        if params["operation"] == "rename":
            if self.probe_reason:
                raise RpcError(self.probe_reason)
            return {"session_id": sid, "operation": "rename", "revision": 1}
        if sid in self.import_reasons:
            raise RpcError(self.import_reasons[sid])
        if sid in self.skipped:
            return {"ok": True, "imported": 0, "skipped": 1,
                    "skipped_ids": [sid], "imported_ids": []}
        return {"ok": True, "imported": 1, "skipped": 0,
                "imported_ids": [sid], "skipped_ids": []}


class _Conn:
    def __init__(self, gateway):
        self.gateway = gateway

    async def __aenter__(self):
        return self.gateway

    async def __aexit__(self, *exc):
        return False


def connect_for(gateway):
    return lambda home: _Conn(gateway)


def payload(*ids):
    return {"home": "C:/hermes", "profile_home": "C:/hermes",
            "sessions": [{"id": sid, "title": sid,
                          "messages": [{"role": "user", "content": "hi"}]}
                         for sid in ids]}


class GatewayWriteHelperTest(unittest.TestCase):
    def test_probe_then_import_per_session(self):
        gw = FakeGateway()
        out = hgw.run(payload("a", "b"), connect=connect_for(gw))
        self.assertTrue(out["ok"])
        self.assertTrue(out["supported"])
        self.assertEqual(out["imported"], 2)
        self.assertEqual(out["failed"], [])
        # Probe is a rename of a nonexistent session; imports are 1:1.
        self.assertEqual(gw.calls[0]["operation"], "rename")
        self.assertEqual(gw.calls[0]["session_id"], hgw.PROBE_SESSION_ID)
        self.assertEqual([c["session_id"] for c in gw.calls[1:]], ["a", "b"])
        for call in gw.calls[1:]:
            self.assertEqual(call["operation"], "import")
            self.assertEqual(call["expected_revision"], 0)
            self.assertEqual(len(call["payload"]["sessions"]), 1)

    def test_unexpected_probe_verdict_marks_the_path_unsupported(self):
        gw = FakeGateway(probe_reason="unknown_method")
        out = hgw.run(payload("a"), connect=connect_for(gw))
        self.assertFalse(out["ok"])
        self.assertFalse(out["supported"])
        self.assertEqual(out["reason"], "unsupported:unknown_method")
        self.assertEqual(len(gw.calls), 1)  # probe only, no import attempt

    def test_contract_drift_mid_batch_hands_the_rest_to_the_local_writer(self):
        gw = FakeGateway(import_reasons={"b": "invalid_params"})
        out = hgw.run(payload("a", "b", "c"), connect=connect_for(gw))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "unsupported:invalid_params")
        self.assertEqual(set(out["failed"]), {"a", "b", "c"})

    def test_one_busy_session_does_not_drop_the_batch(self):
        gw = FakeGateway(import_reasons={"a": "session_busy"})
        out = hgw.run(payload("a", "b"), connect=connect_for(gw))
        self.assertTrue(out["ok"])
        self.assertEqual(out["imported"], 1)
        self.assertEqual(out["failed"], ["a"])

    def test_already_present_session_is_reported_for_local_reconciliation(self):
        gw = FakeGateway(skipped={"a"})
        out = hgw.run(payload("a"), connect=connect_for(gw))
        self.assertTrue(out["ok"])
        self.assertEqual(out["imported"], 0)
        self.assertEqual(out["failed"], ["a"])

    def test_absent_gateway_is_not_an_error_path(self):
        def connect(home):
            raise hgw.GatewayUnavailable("gateway_absent")

        out = hgw.run(payload("a"), connect=connect)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "gateway_absent")

    def test_probe_timeout_reports_the_transport_not_a_write(self):
        class TimeoutGateway(FakeGateway):
            async def rpc(self, method, **params):
                if method == "runtime.describe":
                    return {"instance_id": "i", "profile_id": "default"}
                raise TimeoutError("rpc timeout")

        out = hgw.run(payload("a"), connect=connect_for(TimeoutGateway()))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "probe_error:TimeoutError:rpc timeout")


class DispatcherTest(unittest.TestCase):
    def test_off_switch_short_circuits(self):
        import hermes_gateway
        from unittest import mock
        with mock.patch.dict("os.environ", {"HERMES_SYNC_HERMES_RPC": "off"}):
            out = hermes_gateway.import_sessions(Path("C:/hermes"), None,
                                                 [{"id": "a"}])
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "disabled")

    def test_probe_without_a_hermes_install_reports_it(self):
        import hermes_gateway
        from unittest import mock
        with mock.patch.object(hermes_gateway, "enabled", return_value=True), \
                mock.patch.object(hermes_gateway, "in_process", return_value=False), \
                mock.patch.object(hermes_gateway, "hermes_agent_dir",
                                 return_value=None):
            out = hermes_gateway.probe(Path("C:/nowhere"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "hermes_not_found")

    def test_empty_batch_is_a_noop(self):
        import hermes_gateway
        out = hermes_gateway.import_sessions(Path("C:/hermes"), None, [])
        self.assertTrue(out["ok"])
        self.assertEqual(out["imported"], 0)


if __name__ == "__main__":
    unittest.main()
