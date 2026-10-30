"""Report which write path this machine's Hermes sync client will use.

Answers "will pulled sessions be written by the gateway (single writer) or by
the local SQLite writer?" for the Hermes install that is actually present, and
never starts a gateway or writes anything.

    python scripts/probe-hermes-write-path.py
    python scripts/probe-hermes-write-path.py --home D:/hermes
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))

import hermes_gateway as hg  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=None, help="Hermes home (default: platform root)")
    args = parser.parse_args(argv)

    home = Path(args.home) if args.home else hg.hermes_home()
    report = {
        "hermes_home": str(home),
        "agent_dir": str(hg.hermes_agent_dir(home) or ""),
        "gateway_in_process": hg.in_process(),
        "rpc_enabled": hg.enabled(),
        "api_setting": os.environ.get("HERMES_SYNC_HERMES_API") or "(unset)",
    }
    agent_dir = hg.hermes_agent_dir(home)
    report["hermes_python"] = str(hg.hermes_python(agent_dir)) if agent_dir and hg.hermes_python(agent_dir) else ""
    report["gateway_probe"] = hg.probe(home)

    probe = report["gateway_probe"]
    if report["api_setting"] != "(unset)":
        report["write_path"] = "hermes-http (HERMES_SYNC_HERMES_API)"
    elif probe.get("ok") and probe.get("supported"):
        report["write_path"] = "hermes-gateway-rpc (single writer)"
    else:
        report["write_path"] = "local-sqlite (fallback)"
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
