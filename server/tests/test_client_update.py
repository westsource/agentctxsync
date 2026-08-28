"""client_update manifest/archive round-trip tests.

Pins the contract that the client-update manifest lists only the mcp/ files
the client updater actually verifies and installs (each looked up as
``mcp/<path>`` inside the zip, see mcp/updater.py verify_archive). The
one-shot deploy scripts (scripts/*) still ship inside the zip for fresh
installs but must NOT appear in the manifest -- otherwise verify_archive
looks for ``mcp/scripts/...`` (absent) and every auto-update fails with a
hash mismatch.
"""
import io
import json
import os
import sys
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import client_update as cu  # noqa: E402

MCP_DIR = Path(__file__).resolve().parents[2] / "mcp"
sys.path.insert(0, str(MCP_DIR))
import updater  # noqa: E402

_MCP_ROOTS = ("server.py", "updater.py", "run.sh", "run.bat",
              "auto-sync.py", "adapters/")


class ManifestRoundTripTest(unittest.TestCase):
    def test_manifest_lists_only_client_managed_mcp_files(self):
        manifest = cu._client_manifest_files("https://example.test", "hermes")
        self.assertTrue(manifest)
        for entry in manifest:
            self.assertTrue(entry["path"].startswith(_MCP_ROOTS),
                            entry["path"])

    def test_zip_verifies_against_its_embedded_manifest(self):
        # Every client-managed mcp/ file hashes to its manifest sha256, so
        # the client updater accepts its own package (regression: scripts/*
        # listed in the manifest made verification fail for every client).
        z = cu._build_client_zip("hermes", "https://example.test")
        zf = zipfile.ZipFile(io.BytesIO(z))
        manifest = json.loads(zf.read("manifest.json"))
        self.assertFalse(any(e["path"].startswith("scripts/")
                             for e in manifest["files"]))
        self.assertIsNotNone(updater.verify_archive(z, manifest["files"]))
