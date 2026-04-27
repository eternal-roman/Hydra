"""Tests for WS token-file writing, including the dist/ sentinel gate
that keeps dev-only checkouts from materializing a stray dist/ dir.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_ws_server import DashboardBroadcaster


class TokenFileWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        # Best-effort cleanup; don't fail tests on Windows file locks.
        try:
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass

    def _make_broadcaster(self):
        # Avoid binding ports in the constructor — just exercise the
        # token-file writing branch directly. The constructor calls
        # _write_token_files() at line 92.
        with patch.object(DashboardBroadcaster, "register_handler"):
            return DashboardBroadcaster(host="127.0.0.1", port=0)

    def test_writes_root_and_public(self):
        b = self._make_broadcaster()
        for p in ("hydra_ws_token.json", "dashboard/public/hydra_ws_token.json"):
            self.assertTrue(Path(p).exists(), f"{p} should be written")
            data = json.loads(Path(p).read_text())
            self.assertEqual(data["token"], b.auth_token)

    def test_dist_skipped_without_sentinel(self):
        # No dashboard/dist/index.html → dist/hydra_ws_token.json should NOT be
        # created (and the dist/ directory should not be materialized).
        self._make_broadcaster()
        self.assertFalse(Path("dashboard/dist/hydra_ws_token.json").exists())
        self.assertFalse(Path("dashboard/dist").exists())

    def test_dist_written_when_sentinel_present(self):
        # Simulate a built dashboard by creating dist/index.html first.
        Path("dashboard/dist").mkdir(parents=True, exist_ok=True)
        Path("dashboard/dist/index.html").write_text("<html></html>")
        b = self._make_broadcaster()
        token_path = Path("dashboard/dist/hydra_ws_token.json")
        self.assertTrue(token_path.exists())
        data = json.loads(token_path.read_text())
        self.assertEqual(data["token"], b.auth_token)

    def test_dist_token_matches_root_token(self):
        # When sentinel is present, all three files must carry the same token.
        Path("dashboard/dist").mkdir(parents=True, exist_ok=True)
        Path("dashboard/dist/index.html").write_text("<html></html>")
        b = self._make_broadcaster()
        for p in ("hydra_ws_token.json",
                  "dashboard/public/hydra_ws_token.json",
                  "dashboard/dist/hydra_ws_token.json"):
            data = json.loads(Path(p).read_text())
            self.assertEqual(
                data["token"], b.auth_token,
                f"{p} drifted from auth_token",
            )


if __name__ == "__main__":
    unittest.main()
