"""Help-page agent registry & deploy-script download tests.

Pins two contracts added for the onboarding help page:
1. Every agent card announces where its MCP config lives (``config`` field,
   zh + en) so the "配置文件" line is never blank.
2. The deploy-script download endpoint only serves the two whitelisted
   installers (deploy-local-mcp.ps1 / .sh) and rejects any other filename
   (no path traversal).
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agents  # noqa: E402
from web_help import _DEPLOY_SCRIPTS  # noqa: E402


class AgentConfigTest(unittest.TestCase):
    def test_every_agent_has_config_location_in_both_langs(self):
        # The card renders {{ t.help_config }} + the field value, so the
        # field must be a bare location (no embedded label), never blank.
        for key, a in agents.AGENTS.items():
            cfg = a.get("config", {})
            with self.subTest(agent=key):
                self.assertIn("zh", cfg)
                self.assertIn("en", cfg)
                self.assertTrue(cfg["zh"].strip())
                self.assertTrue(cfg["en"].strip())
                # bare location: must not carry the rendered label prefix
                self.assertFalse(cfg["zh"].startswith("配置文件"))
                self.assertFalse(cfg["en"].startswith("Config file"))


class DeployScriptTest(unittest.TestCase):
    def test_whitelist_contains_only_known_installers(self):
        self.assertIn("deploy-local-mcp.ps1", _DEPLOY_SCRIPTS)
        self.assertIn("deploy-local-mcp.sh", _DEPLOY_SCRIPTS)
        for name in _DEPLOY_SCRIPTS:
            src = Path(__file__).resolve().parents[2] / "scripts" / name
            self.assertTrue(src.is_file(), f"{name} missing under scripts/")

    def test_whitelist_rejects_traversal(self):
        self.assertNotIn("../mcp/server.py", _DEPLOY_SCRIPTS)
        self.assertNotIn("deploy-local-mcp.ps1/../../evil", _DEPLOY_SCRIPTS)
