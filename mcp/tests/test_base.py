"""Tests for the identity-bound sync watermark (Adapter base class).

The watermark sidecar drives incremental pulls. It is bound to the sync
server identity (HERMES_SYNC_SERVER): switching servers must force a full
resync, otherwise a leftover local watermark keeps every older session below
the server's incremental cutoff (`last_synced_at > watermark`) and they are
silently never pulled again.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.workbuddy import WorkBuddyAdapter  # noqa: E402

SERVER_A = "http://47.95.214.236:8765"
SERVER_B = "http://localhost:8765"


class WatermarkIdentityTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name)
        self.adapter = WorkBuddyAdapter(workbuddy_home=self.home)

    def tearDown(self):
        self._td.cleanup()

    def _write(self, content: str):
        (self.home / ".hermes-sync-watermark").write_text(content,
                                                          encoding="utf-8")

    def test_missing_file_is_full_sync(self):
        self.assertEqual(self.adapter.last_synced_at(), 0.0)

    def test_legacy_format_without_identity_keeps_incremental(self):
        # no HERMES_SYNC_SERVER (standalone use): legacy incremental behavior
        self._write("1786964709.277052")
        with mock.patch.dict(os.environ, {"HERMES_SYNC_SERVER": ""}):
            self.assertEqual(self.adapter.last_synced_at(), 1786964709.277052)

    def test_legacy_format_forces_full_resync_once(self):
        # old-format file + identity available: first pull after upgrade is
        # a full resync; save_sync_watermark then records v2 and pulls go
        # back to incremental
        self._write("1786964709.277052")
        with mock.patch.dict(os.environ, {"HERMES_SYNC_SERVER": SERVER_A}):
            self.assertEqual(self.adapter.last_synced_at(), 0.0)

    def test_matching_identity_is_incremental(self):
        self._write(f"v2 {SERVER_A} 1786964709.277052\n")
        with mock.patch.dict(os.environ, {"HERMES_SYNC_SERVER": SERVER_A}):
            self.assertEqual(self.adapter.last_synced_at(), 1786964709.277052)

    def test_switched_server_forces_full_resync(self):
        self._write(f"v2 {SERVER_A} 1786964709.277052\n")
        with mock.patch.dict(os.environ, {"HERMES_SYNC_SERVER": SERVER_B}):
            self.assertEqual(self.adapter.last_synced_at(), 0.0)

    def test_save_with_identity_writes_v2(self):
        with mock.patch.dict(os.environ, {"HERMES_SYNC_SERVER": SERVER_A}):
            self.adapter.save_sync_watermark(1786964709.277052)
        raw = (self.home / ".hermes-sync-watermark").read_text(encoding="utf-8")
        self.assertTrue(raw.startswith(f"v2 {SERVER_A} "))
        self.assertAlmostEqual(float(raw.split()[2]), 1786964709.277052,
                               places=4)

    def test_save_without_identity_writes_legacy(self):
        with mock.patch.dict(os.environ, {"HERMES_SYNC_SERVER": ""}):
            self.adapter.save_sync_watermark(1786964709.277052)
        raw = (self.home / ".hermes-sync-watermark").read_text(encoding="utf-8")
        self.assertEqual(raw.strip(), "1786964709.277052")

    def test_save_then_read_roundtrip(self):
        with mock.patch.dict(os.environ, {"HERMES_SYNC_SERVER": SERVER_A}):
            self.adapter.save_sync_watermark(1786964709.277052)
            self.assertEqual(self.adapter.last_synced_at(), 1786964709.277052)


if __name__ == "__main__":
    unittest.main()
