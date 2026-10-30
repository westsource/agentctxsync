"""Test-environment guard rails for the MCP client suite.

The gateway-RPC write path talks to the DEVELOPER'S running Hermes gateway and
would write into the real ``state.db`` if a test ever exercised it by accident.
The product default is ``auto``; the suite runs with it off, and the tests that
cover the path patch the adapter's ``gateway_import_sessions`` symbol instead.
"""

import os

os.environ["HERMES_SYNC_HERMES_RPC"] = "off"
