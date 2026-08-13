"""Unit tests for the client auto-update logic (mcp/updater.py)."""

import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import updater  # noqa: E402


def make_archive(version: str, files: dict, tamper_hash: bool = False) -> bytes:
    """Build a client-style zip with an embedded manifest.json."""
    entries = []
    for rel, payload in files.items():
        entries.append({"path": rel, "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload)})
    if tamper_hash:
        entries[0]["sha256"] = "0" * 64  # wrong hash -> verification must fail
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, payload in files.items():
            zf.writestr(f"mcp/{rel}", payload)
        zf.writestr("manifest.json", json.dumps(
            {"version": version, "files": entries}))
    return buf.getvalue()


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mcp = Path(self.tmp.name) / "mcp"
        self.mcp.mkdir()
        (self.mcp / "adapters").mkdir()
        self.version_file = self.mcp / updater.VERSION_FILE_NAME
        # old install
        (self.mcp / "server.py").write_text("old server", encoding="utf-8")
        (self.mcp / "adapters" / "base.py").write_text("old base", encoding="utf-8")
        (self.mcp / "adapters" / "stale.py").write_text("should be removed",
                                                       encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_version(self):
        self.assertEqual(updater.local_version(self.version_file), updater.CLIENT_VERSION)
        self.version_file.write_text("9.9.9", encoding="utf-8")
        self.assertEqual(updater.local_version(self.version_file), "9.9.9")

    def test_verify_ok(self):
        new_files = {"server.py": b"hello v2", "adapters/base.py": b"base v2"}
        archive = make_archive("2.0.0", new_files)
        manifest = json.loads(
            zipfile.ZipFile(io.BytesIO(archive)).read("manifest.json"))["files"]
        out = updater.verify_archive(archive, manifest)
        self.assertIsNotNone(out)
        self.assertEqual(out["server.py"], b"hello v2")
        self.assertEqual(out["adapters/base.py"], b"base v2")

    def test_verify_tampered_rejected(self):
        new_files = {"server.py": b"hello v2"}
        archive = make_archive("2.0.0", new_files, tamper_hash=True)
        manifest = json.loads(
            zipfile.ZipFile(io.BytesIO(archive)).read("manifest.json"))["files"]
        self.assertIsNone(updater.verify_archive(archive, manifest))

    def test_verify_missing_file_rejected(self):
        new_files = {"server.py": b"hello v2", "adapters/base.py": b"b"}
        archive = make_archive("2.0.0", new_files)
        manifest = json.loads(
            zipfile.ZipFile(io.BytesIO(archive)).read("manifest.json"))["files"]
        # manifest lists base.py but the zip lacks it -> reject
        archive2 = make_archive("2.0.0", {"server.py": b"hello v2"})
        manifest2 = json.loads(
            zipfile.ZipFile(io.BytesIO(archive2)).read("manifest.json"))["files"]
        manifest2.append(manifest[1])
        self.assertIsNone(updater.verify_archive(archive2, manifest2))

    def test_apply_replaces_backs_up_and_cleans(self):
        logs = []
        new_files = {"server.py": b"hello v2", "adapters/base.py": b"base v2"}
        ok = updater.apply_update(new_files, self.mcp, self.version_file,
                                  "2.0.0", logs.append)
        self.assertTrue(ok)
        self.assertEqual((self.mcp / "server.py").read_bytes(), b"hello v2")
        self.assertEqual((self.mcp / "adapters" / "base.py").read_bytes(), b"base v2")
        self.assertFalse((self.mcp / "adapters" / "stale.py").exists())
        self.assertEqual(self.version_file.read_text(), "2.0.0")
        # backup of the old install exists
        baks = list(self.mcp.glob(".bak-*"))
        self.assertEqual(len(baks), 1)
        self.assertEqual((baks[0] / "server.py").read_text(), "old server")

    def test_check_and_update_flow(self):
        logs = []
        new_files = {"server.py": b"hello v2", "adapters/base.py": b"base v2"}
        archive = make_archive("2.0.0", new_files)
        manifest = json.loads(
            zipfile.ZipFile(io.BytesIO(archive)).read("manifest.json"))
        orig_fetch_m, orig_fetch_a = updater.fetch_manifest, updater.fetch_archive
        updater.fetch_manifest = lambda *a, **k: {
            "version": manifest["version"], "update_available": True,
            "files": manifest["files"]}
        updater.fetch_archive = lambda *a, **k: archive
        try:
            applied = updater.check_and_update(
                "http://x", "key", "hermes", self.mcp, self.version_file, logs.append)
        finally:
            updater.fetch_manifest, updater.fetch_archive = orig_fetch_m, orig_fetch_a
        self.assertTrue(applied)
        self.assertEqual((self.mcp / "server.py").read_bytes(), b"hello v2")
        self.assertEqual(self.version_file.read_text(), "2.0.0")
        # second run: same version -> no update
        updater.fetch_manifest = lambda *a, **k: {**manifest, "update_available": False}
        try:
            applied2 = updater.check_and_update(
                "http://x", "key", "hermes", self.mcp, self.version_file, logs.append)
        finally:
            updater.fetch_manifest = orig_fetch_m
        self.assertFalse(applied2)
        self.assertEqual((self.mcp / "server.py").read_bytes(), b"hello v2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
