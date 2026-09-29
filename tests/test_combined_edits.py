"""Checks for the single-pass mode and Wi-Fi var edit."""

import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import jibo_images as images


class CombinedEditTests(unittest.TestCase):
    def test_rtm2_startup_is_adjusted_and_compacted_once(self):
        original = (b"# stock instructions\n" * 55 +
                    b"auto wlan0\niface wlan0 inet dhcp\n"
                    b"\tpost-up wpa_supplicant -B -i wlan0 -c /var/etc/wpa_supplicant.conf\n"
                    b"\tpre-down wpa_cli -i wlan0 terminate\n")
        edited, adjusted = images.build_rtm2_wifi_startup(original, compact=True)
        self.assertTrue(adjusted)
        self.assertLess(len(edited), 1024)
        self.assertIn(b"post-up /usr/sbin/iw wlan0 set power_save off || true", edited)
        self.assertIn(b"wlcore/sleep_auth' || true", edited)
        self.assertIn(b"post-up wpa_supplicant -B -i wlan0", edited)
        self.assertNotIn(b"stock instructions", edited)
        self.assertEqual(images.build_rtm2_wifi_startup(edited, compact=True),
                         (edited, False))

    def test_rtm3_and_unknown_startups_are_not_given_rtm2_commands(self):
        rtm3 = (b"# stock instructions\n" * 55 +
                b"auto wlan0\niface wlan0 inet dhcp\n\tpost-up wireless-startup\n")
        compacted, adjusted = images.build_rtm2_wifi_startup(rtm3, compact=True)
        self.assertFalse(adjusted)
        self.assertLess(len(compacted), 1024)
        self.assertIn(b"post-up wireless-startup", compacted)
        self.assertNotIn(b"power_save", compacted)
        custom = b"auto wlan0\niface wlan0 inet static\n\taddress 10.0.0.1\n"
        self.assertEqual(images.build_rtm2_wifi_startup(custom, compact=True),
                         (custom, False))

    def test_partially_adjusted_rtm2_startup_requires_review(self):
        partial = (b"auto wlan0\niface wlan0 inet dhcp\n"
                   b"\tpost-up /usr/sbin/iw wlan0 set power_save off || true\n"
                   b"\tpost-up wpa_supplicant -B -i wlan0 -c /var/etc/wpa_supplicant.conf\n")
        with self.assertRaisesRegex(images.ImageError, "partial power-save"):
            images.build_rtm2_wifi_startup(partial, compact=True)

    def test_saved_ssid_updates_password_without_adding_a_duplicate(self):
        existing = (b"update_config=1\n# Keep this comment\n"
                    b"network={\n    ssid=4a49424f\n    key_mgmt=NONE\n}\n"
                    b"network={\n    ssid=\"Test Wi-Fi\"\n    key_mgmt=WPA-PSK\n"
                    b"    psk=oldpassword\n}\n")
        replacement, count = images.build_wifi_config(
            existing, "Test Wi-Fi", "corrected-password")
        expected_psk = hashlib.pbkdf2_hmac(
            "sha1", b"corrected-password", b"Test Wi-Fi", 4096, dklen=32).hex()
        self.assertEqual(count, 2)
        self.assertEqual(len(images._network_blocks(replacement.decode())), 2)
        self.assertIn(b"# Keep this comment", replacement)
        self.assertIn(b"network={\n    ssid=4a49424f\n    key_mgmt=NONE\n}", replacement)
        self.assertIn(b"ssid=546573742057692d4669", replacement)
        self.assertIn(("psk=" + expected_psk).encode(), replacement)
        self.assertNotIn(b"oldpassword", replacement)
        self.assertNotIn(b"corrected-password", replacement)

    def test_existing_ssid_can_change_between_open_and_protected(self):
        original = b"network={\n    ssid=4a49424f\n    key_mgmt=NONE\n}\n"
        protected, count = images.build_wifi_config(
            original, "JIBO", "newpassword", compact=True)
        self.assertEqual(count, 1)
        self.assertIn(b"key_mgmt=WPA-PSK", protected)
        self.assertIn(b"psk=", protected)
        opened, count = images.build_wifi_config(
            protected, "JIBO", open_network=True, compact=True)
        self.assertEqual(count, 1)
        self.assertIn(b"key_mgmt=NONE", opened)
        self.assertNotIn(b"psk=", opened)

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
            interfaces_file = work / "interfaces"
            interfaces_file.write_bytes(
                b"# stock instructions\n" * 55 +
                b"auto wlan0\niface wlan0 inet dhcp\n"
                b"\tpost-up wpa_supplicant -B -i wlan0 -c /var/etc/wpa_supplicant.conf\n")
            images._run_debugfs(source, "mkdir /etc/network", writable=True)
            images._run_debugfs(source, "write " + str(interfaces_file) + " " +
                                images.VAR_INTERFACES_PATH, writable=True)
            original_hash = images.sha256_file(source)

            result = images.edit_mode_wifi(source, edited, "developer", "Test Wi-Fi",
                                           open_network=True)

            self.assertEqual(result["previous_mode"], "oobe")
            self.assertEqual(result["network_count"], 2)
            self.assertTrue(result["rtm2_wifi_startup_adjusted"])
            self.assertEqual(images.sha256_file(source), original_hash)
            mode = images._extract(edited, images.VAR_MODE_PATH, work / "check-mode")
            wifi = images._extract(edited, images.VAR_WIFI_PATH, work / "check-wifi")
            interfaces = images._extract(edited, images.VAR_INTERFACES_PATH,
                                         work / "check-interfaces")
            self.assertEqual(json.loads(mode)["mode"], "developer")
            self.assertLess(len(wifi), 1024)
            self.assertIn(b"ssid=4a49424f", wifi)
            self.assertIn(b"ssid=546573742057692d4669", wifi)
            self.assertLess(len(interfaces), 1024)
            self.assertIn(b"iw wlan0 set power_save off", interfaces)
