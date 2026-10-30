"""Dispatcher for the optional gateway-RPC write path.

Hermes' gateway owns ``state.db`` while it runs, so the pull cannot write
through a second ``hermes serve`` there. The gateway accepts session edits over
its own WebSocket JSON-RPC; ``hermes_gateway_write.py`` speaks that protocol and
runs under the Hermes install's interpreter (in-process when our MCP child
already uses it, as a child process otherwise).

This module only decides *whether* to use it:

* ``HERMES_SYNC_HERMES_RPC=off`` disables it;
* discovery never starts a gateway (``discover_gateway_endpoint``); an absent
  owner means "use the local writer";
* the helper probes the RPC contract (a rename of a nonexistent id) before the
  batch, so an older/newer/unknown Hermes degrades to the local writer instead
  of failing the pull.

The result is always advisory: callers write whatever comes back as ``failed``
with their local SQLite writer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

#: The protocol half, executed under the Hermes interpreter.
HELPER = Path(__file__).with_name("hermes_gateway_write.py")
#: Upper bound for one helper run (probe + a small batch of sessions).
TIMEOUT_S = float(os.environ.get("HERMES_SYNC_HERMES_RPC_TIMEOUT_S", "180"))


def enabled() -> bool:
    return (os.environ.get("HERMES_SYNC_HERMES_RPC", "auto").strip().lower()
            not in {"0", "off", "false", "no", "disabled"})


def hermes_home() -> Path:
    """Platform Hermes home (the data root that holds ``state.db``)."""
    explicit = os.environ.get("HERMES_HOME")
    if explicit:
        return Path(explicit)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes"
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "hermes"
    return Path.home() / ".hermes"


def hermes_agent_dir(home: Path | None = None) -> Path | None:
    home = home or hermes_home()
    for candidate in (home / "hermes-agent", home):
        if (candidate / "hermes_cli" / "gateway_client.py").is_file():
            return candidate
    return None


def hermes_python(agent_dir: Path) -> Path | None:
    for python in (agent_dir / "venv" / "Scripts" / "python.exe",
                   agent_dir / "venv" / "bin" / "python"):
        if python.exists():
            return python
    tools = agent_dir.parent / "tools"
    if tools.is_dir():
        for python in sorted(tools.glob("python-*/python.exe"), reverse=True):
            if python.exists():
                return python
    return None


def in_process() -> bool:
    """True when THIS interpreter can import the Hermes gateway client."""
    try:
        import importlib.util
        return importlib.util.find_spec("hermes_cli.gateway_client") is not None
    except (ImportError, ValueError):
        return False


def _payload(home: Path, profile_home: Path | None, sessions: list[dict]) -> dict:
    return {"home": str(home),
            "profile_home": str(profile_home or home),
            "sessions": sessions}


def _run_in_process(payload: dict) -> dict:
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import hermes_gateway_write  # local helper; imports hermes only on demand
    return hermes_gateway_write.run(payload)


def _child_env(agent_dir: Path) -> dict:
    """The helper imports ``hermes_cli`` from the checkout, not from the venv:
    put the agent dir on ``PYTHONPATH`` (the venv alone does not expose it)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(agent_dir)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


def _run_child(python: Path, agent_dir: Path, payload: dict) -> dict:
    try:
        proc = subprocess.run(
            [str(python), str(HELPER), "--stdin"],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8",
            timeout=TIMEOUT_S, cwd=str(agent_dir), env=_child_env(agent_dir))
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "supported": False,
                "reason": f"helper_{type(exc).__name__}"}
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"ok": False, "supported": False,
            "reason": f"helper_exit_{proc.returncode}"}


def probe(home: Path) -> dict:
    """Diagnostics: would a write use the gateway right now, and why not?

    Same helper, same discovery: never starts a gateway, writes nothing.
    """
    if not enabled():
        return {"ok": False, "supported": False, "reason": "disabled"}
    inproc_error = None
    if in_process():
        try:
            here = str(Path(__file__).resolve().parent)
            if here not in sys.path:
                sys.path.insert(0, here)
            import hermes_gateway_write
            return hermes_gateway_write.probe(home)
        except Exception as exc:
            inproc_error = f"inproc_{type(exc).__name__}"
    agent_dir = hermes_agent_dir(home)
    if agent_dir is None:
        return {"ok": False, "supported": False,
                "reason": inproc_error or "hermes_not_found"}
    python = hermes_python(agent_dir)
    if python is None:
        return {"ok": False, "supported": False,
                "reason": inproc_error or "hermes_python_missing"}
    try:
        proc = subprocess.run([str(python), str(HELPER), "--probe",
                               "--home", str(home)],
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=TIMEOUT_S, cwd=str(agent_dir),
                              env=_child_env(agent_dir))
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "supported": False,
                "reason": f"helper_{type(exc).__name__}"}
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"ok": False, "supported": False,
            "reason": f"helper_exit_{proc.returncode}"}


def import_sessions(home: Path, profile_home: Path | None,
                    sessions: list[dict]) -> dict:
    """Import new sessions through the running gateway; advisory result."""
    if not sessions:
        return {"ok": True, "supported": True, "imported": 0, "failed": []}
    if not enabled():
        return {"ok": False, "supported": False, "reason": "disabled"}
    payload = _payload(home, profile_home, sessions)
    inproc_error = None
    if in_process():
        try:
            return _run_in_process(payload)
        except Exception as exc:  # e.g. this interpreter lacks hermes' deps
            inproc_error = f"inproc_{type(exc).__name__}"
    agent_dir = hermes_agent_dir(home)
    if agent_dir is None:
        return {"ok": False, "supported": False,
                "reason": inproc_error or "hermes_not_found"}
    python = hermes_python(agent_dir)
    if python is None:
        return {"ok": False, "supported": False,
                "reason": inproc_error or "hermes_python_missing"}
    return _run_child(python, agent_dir, payload)
