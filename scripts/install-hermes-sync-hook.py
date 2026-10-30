"""Install (or remove) the Hermes shell hook that wakes the sync client.

The MCP client's background loop sleeps ``HERMES_SYNC_INTERVAL`` (5min) and
runs one incremental pull+push per wake. This installer adds a Hermes shell
hook on ``on_session_end`` (every finished turn) that touches the client's
wake file, so the loop syncs right after a conversation move instead of
polling. With the wake file present the client's fallback sweep drops to
30min, which is what makes a big store cheap.

Default is a dry run. ``--apply`` writes the hook through Hermes' own config
loader and records its consent entry, because Hermes refuses to run a shell
hook that was not approved (``~/.hermes/shell-hooks-allowlist.json``).

    python scripts/install-hermes-sync-hook.py            # show what changes
    python scripts/install-hermes-sync-hook.py --apply    # write it
    python scripts/install-hermes-sync-hook.py --remove   # undo

Hermes itself is required for ``--apply`` (its ``hermes_cli`` modules own the
config format). When the current interpreter cannot import them the script
re-runs itself under the Hermes venv/tool Python.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

EVENTS = ("on_session_end",)
HOOK_TIMEOUT_S = 15


def hermes_home() -> Path:
    explicit = os.environ.get("HERMES_HOME")
    if explicit:
        return Path(explicit)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def install_roots(home: Path) -> list[Path]:
    """Data home first, then the platform default install root.

    ``HERMES_HOME`` moves the DATA (config.yaml, state.db); the code checkout
    usually stays under the platform default, so a test/alternate data home
    must still find the interpreter that can import ``hermes_cli``.
    """
    roots = [home]
    local = os.environ.get("LOCALAPPDATA")
    for fallback in ([Path(local) / "hermes"] if local else []) + [
            Path.home() / ".hermes"]:
        if fallback not in roots:
            roots.append(fallback)
    return roots


def candidate_interpreters(home: Path) -> list[tuple[Path, Path]]:
    """``[(python, agent_dir)]`` candidates: the venv first, then the bundled
    tool Python. ``agent_dir`` is the checkout put on ``PYTHONPATH``/``cwd``
    (``hermes_cli`` is a source package, not an installed one)."""
    out: list[tuple[Path, Path]] = []
    for root in install_roots(home):
        agent = root / "hermes-agent"
        if not agent.is_dir():
            continue
        for python in (agent / "venv" / "Scripts" / "python.exe",
                       agent / "venv" / "bin" / "python"):
            if python.exists():
                out.append((python, agent))
        tools = root / "tools"
        if tools.is_dir():
            for python in sorted(tools.glob("python-*/python.exe"), reverse=True):
                out.append((python, agent))
    return out


def have_hermes_modules() -> bool:
    try:
        import hermes_cli.config  # noqa: F401
        return True
    except Exception:
        return False


def wake_script(client_dir: Path) -> Path:
    return client_dir / "mcp" / "wake.py"


def hook_command(python: str, script: Path) -> str:
    parts = [python, str(script)]
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def wake_file_for(agent: str) -> Path:
    """Must match mcp/wake.py + mcp/server.py ``WAKE_FILE`` (per-machine
    coordination dir, not the store and not ``HERMES_HOME``)."""
    explicit = os.environ.get("HERMES_SYNC_WAKE_FILE")
    if explicit:
        return Path(explicit)
    stem = "hermes-sync" if agent == "hermes" else f"hermes-sync-{agent}"
    return Path.home() / "AppData/Local/hermes" / (stem + ".wake")


def plan(events, command: str):
    """Return [(event, entry, action)] for the requested events."""
    from hermes_cli.config import read_raw_config

    cfg = read_raw_config() or {}
    hooks = cfg.get("hooks") if isinstance(cfg.get("hooks"), dict) else {}
    out = []
    for event in events:
        entries = hooks.get(event) if isinstance(hooks.get(event), list) else []
        exists = any(isinstance(e, dict) and e.get("command") == command
                     for e in entries)
        out.append((event, {"command": command, "timeout": HOOK_TIMEOUT_S},
                    "present" if exists else "add"))
    return out


def apply(events, command: str, *, remove: bool = False) -> int:
    from hermes_cli.config import read_raw_config, save_config
    from agent.shell_hooks import _record_approval, revoke

    cfg = read_raw_config() or {}
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        cfg["hooks"] = hooks
    changed = 0
    for event in events:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
        kept = [e for e in entries
                if not (isinstance(e, dict) and e.get("command") == command)]
        if remove:
            if len(kept) != len(entries):
                changed += 1
                revoke(command)
        else:
            if len(kept) == len(entries):
                kept.append({"command": command, "timeout": HOOK_TIMEOUT_S})
                changed += 1
                _record_approval(event, command)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if hooks:
        cfg["hooks"] = hooks
    else:
        cfg.pop("hooks", None)
    if changed:
        # No merge_existing: the raw config was loaded above and every other
        # key is already in ``cfg``. Merging would resurrect the entry a
        # --remove just deleted.
        save_config(cfg)
    return changed


def reexec_under_hermes(argv: list[str], home: Path) -> int:
    for python, agent_dir in candidate_interpreters(home):
        env = os.environ.copy()
        # ``hermes_cli`` is a source checkout, not an installed package: it is
        # importable only with the checkout on the path (the MCP client's own
        # python does not need it, so the venv alone does not provide it).
        env["PYTHONPATH"] = os.pathsep.join(
            [str(agent_dir)] + ([env["PYTHONPATH"]]
                                if env.get("PYTHONPATH") else []))
        result = subprocess.run([str(python), os.path.abspath(__file__),
                                 *argv, "--no-reexec"],
                                env=env, cwd=str(agent_dir))
        return result.returncode
    sys.stderr.write(
        "[hermes-sync] Hermes python not found; run this script with the "
        "Hermes venv/tool interpreter (--python on the hook is separate).\n")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write the hook (default: dry run)")
    parser.add_argument("--remove", action="store_true",
                        help="Remove the hook and its consent entry")
    parser.add_argument("--events", default=",".join(EVENTS),
                        help="Comma-separated hook events (default: on_session_end)")
    parser.add_argument("--client-dir", default=str(Path(__file__).resolve().parent.parent),
                        help="Sync client root holding mcp/wake.py")
    parser.add_argument("--python", default=sys.executable,
                        help="Interpreter the hook uses to run mcp/wake.py")
    parser.add_argument("--agent", default=os.environ.get("HERMES_SYNC_AGENT", "hermes"),
                        help="Agent key (selects the wake file name)")
    parser.add_argument("--no-reexec", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    home = hermes_home()
    events = [e.strip() for e in args.events.split(",") if e.strip()]
    script = wake_script(Path(args.client_dir).resolve())
    if not script.exists():
        sys.stderr.write(f"[hermes-sync] wake script missing: {script}\n")
        return 2
    command = hook_command(args.python, script)
    wake_path = wake_file_for(args.agent)

    if not have_hermes_modules():
        if args.no_reexec:
            sys.stderr.write("[hermes-sync] hermes_cli is not importable here.\n")
            return 2
        return reexec_under_hermes(
            [a for a in (argv if argv is not None else sys.argv[1:])
             if a != "--no-reexec"], home)

    print(f"hermes home : {home}")
    print(f"wake file   : {wake_path}")
    print(f"hook command: {command}")
    for event, entry, action in plan(events, command):
        print(f"hooks.{event}: {action} {json.dumps(entry)}")
    if not args.apply and not args.remove:
        print("\ndry run: pass --apply to write, --remove to undo")
        return 0

    changed = apply(events, command, remove=args.remove)
    print(f"\n{'removed' if args.remove else 'installed'}: {changed} event(s)")
    if not args.remove and not wake_path.exists():
        try:
            wake_path.parent.mkdir(parents=True, exist_ok=True)
            wake_path.touch()
        except OSError as exc:
            sys.stderr.write(f"[hermes-sync] could not create wake file: {exc}\n")
    if not args.remove:
        print("restart Hermes so the hook is registered; the client's fallback "
              "sweep then uses 30min (HERMES_SYNC_INTERVAL still wins).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
