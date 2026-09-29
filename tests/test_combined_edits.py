"""Checks for the single-pass mode and Wi-Fi var edit."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import jibo_images as images


class CombinedEditTests(unittest.TestCase):
    def test_compaction_keeps_active_wifi_settings_and_existing_network(self):
        existing = (b"ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
                    b"update_config=1\n"
                    + b"# stock documentation\n" * 150
                    + b"network={\n    ssid=4a49424f\n    key_mgmt=NONE\n}\n")
        replacement, count = images.build_wifi_config(
            existing, "Test Wi-Fi", open_network=True, compact=True)
        self.assertGreater(len(existing), 1024)
        self.assertLess(len(replacement), 1024)
        self.assertEqual(count, 2)
        self.assertIn(b"ctrl_interface=", replacement)
        self.assertIn(b"ssid=4a49424f", replacement)
        self.assertIn(b"ssid=546573742057692d4669", replacement)
        self.assertNotIn(b"stock documentation", replacement)

    @unittest.skipUnless(all(shutil.which(tool) for tool in
                             ("mke2fs", "debugfs", "e2fsck")),
                         "ext4 tools are required")
    def test_combined_edit_changes_both_files_in_one_var_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source.ext4"
            edited = work / "edited.ext4"
            with source.open("wb") as stream:
                stream.truncate(16 * 1024 * 1024)
            subprocess.run(("mke2fs", "-F", "-q", "-t", "ext4", "-b", "1024",
                            str(source)), check=True, capture_output=True)
            images._run_debugfs(source, "mkdir /jibo", writable=True)
            images._run_debugfs(source, "mkdir /etc", writable=True)
            mode_file = work / "mode.json"
            mode_file.write_text('{"mode":"oobe"}\n')
            wifi_file = work / "wifi.conf"
            wifi_file.write_bytes(
                b"update_config=1\n" + b"# stock documentation\n" * 150 +
                b"network={\n    ssid=4a49424f\n    key_mgmt=NONE\n}\n")
            images._run_debugfs(source, "write " + str(mode_file) + " " +
                                images.VAR_MODE_PATH, writable=True)
            images._run_debugfs(source, "write " + str(wifi_file) + " " +
                                images.VAR_WIFI_PATH, writable=True)
            original_hash = images.sha256_file(source)

            result = images.edit_mode_wifi(source, edited, "developer", "Test Wi-Fi",
                                           open_network=True)

            self.assertEqual(result["previous_mode"], "oobe")
            self.assertEqual(result["network_count"], 2)
            self.assertEqual(images.sha256_file(source), original_hash)
            mode = images._extract(edited, images.VAR_MODE_PATH, work / "check-mode")
            wifi = images._extract(edited, images.VAR_WIFI_PATH, work / "check-wifi")
            self.assertEqual(json.loads(mode)["mode"], "developer")
            self.assertLess(len(wifi), 1024)
            self.assertIn(b"ssid=4a49424f", wifi)
            self.assertIn(b"ssid=546573742057692d4669", wifi)
