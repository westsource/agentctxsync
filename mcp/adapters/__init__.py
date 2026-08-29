"""
Adapter registry for Agent Context Sync.

Adding a new agent:
    1. add its prefix to AGENT_PREFIXES in base.py (or reuse existing);
    2. create mcp/adapters/<name>.py implementing the Adapter interface;
    3. add "<name>: <module>" to _ADAPTER_MODULES below.

The registry loads adapters lazily so a missing/partial adapter never
breaks the rest of the server.
"""

import importlib

from .base import (  # noqa: F401  (re-exported for convenience)
    AGENT_PREFIXES,
    CANONICAL_MESSAGE_FIELDS,
    CANONICAL_SESSION_FIELDS,
    Adapter,
    JSONLAdapter,
    SQLiteAdapter,
    canonical_id,
    local_id,
    split_agent_prefix,
)

#: agent key -> adapter module name (lazy-loaded on first use)
_ADAPTER_MODULES = {
    "hermes": "hermes",
    "deepseek-harness": "deepseek_harness",
    "opencode": "opencode",
    "reasonix": "reasonix",
    "openclaw": "openclaw",
    "workbuddy": "workbuddy",
    "omp": "omp",
}

_adapter_cache: dict[str, type[Adapter]] = {}


def available_agents() -> list[str]:
    """Names of all registered agents (stable order)."""
    return list(_ADAPTER_MODULES)


def get_adapter(agent_type: str, **kwargs) -> Adapter:
    """Instantiate the adapter for ``agent_type`` (lazy import)."""
    if agent_type not in _ADAPTER_MODULES:
        raise ValueError(
            f"Unknown agent type: {agent_type!r} "
            f"(registered: {list(_ADAPTER_MODULES)})")
    if agent_type not in _adapter_cache:
        module = importlib.import_module(f".{_ADAPTER_MODULES[agent_type]}",
                                         __name__)
        _adapter_cache[agent_type] = module.Adapter
    return _adapter_cache[agent_type](**kwargs)
