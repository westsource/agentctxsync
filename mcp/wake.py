"""Touch the sync wake file (called by the host agent's shell hook).

The MCP client's background loop watches this file: when it changes, the loop
runs its sync cycle immediately instead of waiting out ``HERMES_SYNC_INTERVAL``.
Installed by ``scripts/install-hermes-sync-hook.py`` into the host agent's
``hooks:`` config; the hook payload arrives on stdin and is intentionally
ignored.

    python mcp/wake.py [--agent hermes]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def wake_file(agent: str) -> Path:
    """Same path the MCP server watches (mcp/server.py ``WAKE_FILE``).

    Coordination files live under the per-machine hermes home, not next to
    the store; ``HERMES_SYNC_WAKE_FILE`` overrides both sides.
    """
    explicit = os.environ.get("HERMES_SYNC_WAKE_FILE")
    if explicit:
        return Path(explicit)
    stem = "hermes-sync" if agent == "hermes" else f"hermes-sync-{agent}"
    return Path.home() / "AppData/Local/hermes" / (stem + ".wake")


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    # Directory mtime is not portable; rewriting the mtime explicitly keeps
    # the watcher's "> baseline" test true even on a coarse filesystem clock.
    stamp = max(time.time(), os.path.getmtime(path))
    os.utime(path, (stamp, stamp))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default=os.environ.get("HERMES_SYNC_AGENT", "hermes"))
    args = parser.parse_args(argv)
    target = wake_file(args.agent)
    try:
        touch(target)
    except OSError as exc:
        # A hook must never fail the host agent's turn: report and exit 0.
        sys.stderr.write(f"[hermes-sync] wake failed: {exc}\n")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
