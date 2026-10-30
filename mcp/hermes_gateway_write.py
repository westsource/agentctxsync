"""Write pulled sessions through the RUNNING Hermes gateway (single writer).

Hermes' gateway owns the profile's ``state.db`` while it runs, so a standalone
``hermes serve`` is refused for writes ("Exclusive maintenance refused: Gateway
runtime already owns profile"). The gateway itself accepts session edits over
its WebSocket JSON-RPC, exactly like the Desktop app does, so an external
same-user process can be a *client* instead of a second writer.

This module is the protocol half and is meant to run under the Hermes install's
own interpreter: it imports ``hermes_cli.gateway_client`` so the transport,
subprotocol, ticket bootstrap and param contract always match that install.
``mcp/hermes_gateway.py`` is the stdlib-only dispatcher that decides whether to
use it (in-process or as a child process).

    python hermes_gateway_write.py --stdin   # {"home", "profile_home", "sessions": [...]}
    python hermes_gateway_write.py --probe   # diagnostics only, writes nothing

Result JSON (stdout, one object):
    {"ok": true, "supported": true, "imported": 2, "failed": [], "instance_id": ...}
    {"ok": false, "supported": false, "reason": "gateway_absent"}
    {"ok": false, "supported": false, "reason": "unsupported:unknown_method"}

Nothing here is a correctness dependency: the caller writes anything not
imported with its local SQLite writer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

#: A read-only probe target: renaming a session that does not exist returns
#: ``not_found`` when the method + param contract are accepted, and writes
#: nothing. ``unknown_method``/``invalid_params`` mean this Hermes version does
#: not speak the shape we send.
PROBE_SESSION_ID = "__hermes-sync-probe__"


#: A refusal that means our request shape is not this Hermes version's contract.
_CONTRACT_REASONS = frozenset({"invalid_params", "unknown_method", "permission_denied"})


class GatewayUnavailable(Exception):
    """Discovery says no ready owner, or it is not dialable."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _discover_endpoint(home: Path):
    """Discovery ONLY. ``discover_gateway_endpoint`` never starts a gateway."""
    from hermes_cli.gateway_runtime import discover_gateway_endpoint

    discovery = discover_gateway_endpoint(home, timeout=2.0)
    state = getattr(discovery, "state", "") or "unknown"
    endpoint = getattr(discovery, "endpoint", None)
    if state != "ready" or endpoint is None:
        detail = getattr(discovery, "reason_code", None)
        raise GatewayUnavailable(f"gateway_{state}" + (f":{detail}" if detail else ""))
    return endpoint


def default_connect(home: Path):
    """Async context manager yielding a connected gateway client for ``home``."""
    from hermes_cli.gateway_client import connect_gateway

    endpoint = _discover_endpoint(home)
    return connect_gateway(endpoint=endpoint)


def _reason(exc: BaseException) -> str:
    """Bounded, machine-readable failure reason (clamped, no payloads)."""
    if isinstance(exc, GatewayUnavailable):
        return exc.reason
    text = (str(exc) or type(exc).__name__).strip()
    if getattr(exc, "code", None) is not None:
        return text[:200]
    return f"{type(exc).__name__}:{text}"[:200]


async def _probe(gw) -> tuple[str, str | None]:
    """``("ok"|"unsupported", reason)`` without mutating anything."""
    try:
        await gw.rpc("session.mutate", session_id=PROBE_SESSION_ID,
                     request_id=f"probe-{uuid.uuid4().hex}",
                     expected_revision=0, operation="rename",
                     payload={"title": "hermes-sync probe"})
    except Exception as exc:  # GatewayRPCError and transport errors alike
        reason = _reason(exc)
        if reason == "not_found":
            return "ok", None
        if reason in _CONTRACT_REASONS:
            return "unsupported", reason
        # Transport/timeout: not a version verdict, so do not cache it as one.
        return "error", reason
    # A rename of a nonexistent id committed? Treat as unsupported rather than
    # writing through a path whose semantics we did not predict.
    return "unsupported", "probe_wrote"


async def _run_async(payload: dict, connect) -> dict:
    home = Path(payload["home"])
    profile_home = Path(payload.get("profile_home") or home)
    sessions = list(payload.get("sessions") or [])
    async with connect(profile_home) as gw:
        info = await gw.rpc("runtime.describe")
        verdict, detail = await _probe(gw)
        base = {"instance_id": info.get("instance_id"),
                "profile_id": info.get("profile_id")}
        if verdict == "error":
            return {"ok": False, "supported": False,
                    "reason": f"probe_error:{detail}", **base}
        if verdict != "ok":
            return {"ok": False, "supported": False,
                    "reason": f"unsupported:{detail}", **base}
        imported = 0
        failed: list[str] = []

        def _fail(sid: str) -> None:
            if sid and sid not in failed:
                failed.append(sid)

        for index, session in enumerate(sessions):
            sid = str(session.get("id"))
            try:
                result = await gw.rpc(
                    "session.mutate", session_id=sid,
                    request_id=f"import-{uuid.uuid4().hex}",
                    expected_revision=0, operation="import",
                    payload={"sessions": [session]}, _timeout=120)
            except Exception as exc:  # one bad session must not drop the batch
                reason = _reason(exc)
                _fail(sid)
                if reason in _CONTRACT_REASONS:
                    # The contract changed under us: trust none of this path's
                    # outcomes, so the whole batch goes to the local writer
                    # (idempotent upsert) instead of retrying row by row.
                    for row in sessions:
                        _fail(str(row.get("id")))
                    return {"ok": False, "supported": False,
                            "reason": f"unsupported:{reason}",
                            "failed": failed, **base}
                continue
            if int(result.get("imported") or 0) >= 1:
                imported += 1
            else:
                # Already present (raced with Hermes itself) -> the local
                # writer reconciles it.
                _fail(sid)
        return {"ok": True, "supported": True, "imported": imported,
                "failed": failed, **base}


def run(payload: dict, *, connect=None) -> dict:
    """Synchronous entry: import ``payload["sessions"]`` via the gateway."""
    connect = connect or default_connect
    try:
        return asyncio.run(_run_async(payload, connect))
    except Exception as exc:
        return {"ok": False, "supported": False, "reason": _reason(exc)}


def probe(home: Path) -> dict:
    """Diagnostics: can this install write through its gateway right now?"""
    result = run({"home": str(home), "sessions": []})
    out = {"home": str(home), **result}
    try:
        from hermes_cli import __version__
        out["hermes_version"] = __version__
    except Exception:
        out["hermes_version"] = None
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdin", action="store_true", help="Read the JSON payload from stdin")
    parser.add_argument("--probe", action="store_true", help="Probe only, no writes")
    parser.add_argument("--home", default=None, help="Hermes home (default: platform root)")
    args = parser.parse_args(argv)
    if not args.stdin and not args.probe:
        parser.error("pass --stdin or --probe")
    if args.probe:
        home = Path(args.home) if args.home else _default_home()
        result = probe(home)
    else:
        try:
            payload = json.load(sys.stdin)
        except (ValueError, OSError) as exc:
            print(json.dumps({"ok": False, "supported": False,
                              "reason": f"bad_payload:{type(exc).__name__}"}))
            return 0
        result = run(payload)
    print(json.dumps(result))
    return 0


def _default_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


if __name__ == "__main__":
    raise SystemExit(main())
