"""Offline safety checks for the stock-13 first-OTA bridge."""

import hashlib
import io
import json
import unittest
from unittest.mock import patch

import jibo_dfu as dfu
import jibo_repoint as repoint


class RepointTests(unittest.TestCase):
    def test_manifest_covers_both_slots_and_separate_services(self):
        files = repoint.patch_manifest()
        self.assertEqual(len(files), 28)
        self.assertEqual(len({(part, path) for part, path, _ in files}), len(files))
        for part in ("rootfsA", "rootfsB"):
            self.assertIn((part, repoint.CA_PATH, "ca"), files)
            self.assertIn((part, "/usr/lib/node_modules/@jibo/jibo-ota-updater/src/download-update.js",
                           "downloader"), files)
        self.assertIn(("services", repoint.SERVICE_CLIENT + "/lib/http/node.js", "client"), files)
        self.assertIn(("skills", repoint.OOBE_CLIENT + "/lib/region_config.json", "region"), files)

    def test_manifest_probes_later_nested_rootfs_clients(self):
        files = repoint.patch_manifest()
        self.assertEqual(len(files), 28)
        self.assertIn(("rootfsA", repoint.ROOT_CLIENTS[1] + "/lib/http/node.js", "client"), files)
        self.assertEqual(repoint._pinned_output("downloader", repoint.STOCK_54_SHA256["downloader"]),
                         repoint.PATCHED_54_SHA256["downloader"])

    def test_rootfs_profile_accepts_verified_early_client_downloader_pair(self):
        with patch.object(dfu, "_read_partition_file_rpc",
                          side_effect=(b"archive downloader", b"early client")) as read, \
                patch.object(repoint, "_sha", side_effect=(
                    repoint.STOCK_54_SHA256["downloader"],
                    repoint.STOCK_33_SHA256["client"],
                )):
            profile, source, client = repoint._rootfs_profile(
                "dfu-util", "1-2", "rootfsA", "/tmp/test-repoint"
            )
        self.assertEqual(profile, "early client + 5.4 downloader")
        self.assertEqual(source, b"archive downloader")
        self.assertEqual(client, b"early client")
        self.assertEqual(read.call_count, 2)

    def test_public_root_is_pinned(self):
        self.assertEqual(hashlib.sha256(repoint.CA_ASSET.read_bytes()).hexdigest(),
                         repoint.CA_SHA256)

    def test_transforms_keep_tls_verification_enabled(self):
        region = b"jibo.com/" * 5
        self.assertEqual(repoint.patched_bytes("region", region), b"jibo.io/" * 5)
        client = b"options.agent = this.sslAgent();"
        result = repoint.patched_bytes("client", client)
        self.assertIn(b"rejectUnauthorized: true", result)
        self.assertIn(b'require("fs").readFileSync', result)
        self.assertIn(repoint.CA_PATH.encode(), result)
        self.assertIn(b'jibo\\.io$', result)
        self.assertIn(b': this.sslAgent();', result)
        self.assertNotIn(b"rejectUnauthorized: false", result)
        downloader = b"let req = http.get(argv.url, function(res) {"
        result = repoint.patched_bytes("downloader", downloader)
        self.assertIn(b"fs.readFileSync", result)
        self.assertIn(b"argv.url.startsWith", result)
        self.assertIn(b"jibo\\.io", result)

    def test_v1_cannot_repoint_stock_or_write_multiblock_file(self):
        with self.assertRaisesRegex(dfu.DfuError, "jibo-file-v2"):
            repoint.plan("dfu-util", "1-2", [dfu.FILE_LEVEL_MARKER])
        transaction = dfu.FileTransaction(("rootfsA",), port="1-2", dfu_util="dfu-util")
        transaction.replace("rootfsA", "/a", b"x" * 5000)
        with patch.object(dfu, "_file_loader_context", return_value=(
                "1-2", [dfu.FILE_LEVEL_MARKER], "serial-sha256:robot", "")):
            with self.assertRaisesRegex(dfu.DfuError, "v1 loader"):
                transaction.commit(True)

    def test_unknown_stock_file_aborts_before_stat_or_write(self):
        with patch.object(repoint, "_rootfs_profile", return_value=("13.0", b"known", b"known")), \
                patch.object(repoint, "patch_manifest", return_value=(("rootfsA", "/ca", "ca"),)), \
                patch.object(dfu, "_read_partition_file_rpc", return_value=b"unknown"), \
                patch.object(dfu, "_stat_partition_file_rpc") as stat, \
                patch.object(dfu, "_file_request_transfer") as transfer:
            with self.assertRaisesRegex(dfu.DfuError, "Unsupported ca file"):
                repoint.plan("dfu-util", "1-2", [dfu.FILE_LEVEL_MARKER_V2])
        stat.assert_not_called()
        transfer.assert_not_called()

    def test_transform_must_match_pinned_output_before_write(self):
        stock = b"jibo.com/" * 5
        with patch.object(repoint, "_rootfs_profile", return_value=("13.0", b"known", b"known")), \
                patch.object(repoint, "patch_manifest", return_value=(("rootfsA", "/region", "region"),)), \
                patch.dict(repoint.STOCK_SHA256, {"region": hashlib.sha256(stock).hexdigest()}), \
                patch.object(dfu, "_read_partition_file_rpc", return_value=stock), \
                patch.object(dfu, "_stat_partition_file_rpc") as stat:
            with self.assertRaisesRegex(dfu.DfuError, "pinned output"):
                repoint.plan("dfu-util", "1-2", [dfu.FILE_LEVEL_MARKER_V2])
        stat.assert_not_called()

    def test_partial_optional_client_aborts_before_write(self):
        base = repoint.EARLY_SKILL_CLIENTS[0]
        paths = (("skills", base + "/lib/region_config.json", "region"),
                 ("skills", base + "/lib/http/node.js", "client"))
        with patch.object(repoint, "_rootfs_profile", return_value=("13.0", b"known", b"known")), \
                patch.object(repoint, "patch_manifest", return_value=paths), \
                patch.object(dfu, "_read_partition_file_rpc",
                             side_effect=(b"patched", dfu.FileRpcStatusError(2))), \
                patch.object(repoint, "_pinned_output", return_value=None), \
                patch.object(repoint, "_already_patched", return_value=True), \
                patch.object(dfu, "_file_request_transfer") as transfer:
            with self.assertRaisesRegex(dfu.DfuError, "only partly present"):
                repoint.plan("dfu-util", "1-2", [dfu.FILE_LEVEL_MARKER_V2])
        transfer.assert_not_called()

    def test_existing_credentials_are_validated_and_adopted_only_to_jibo_io(self):
        credentials = {"accessKeyId": "A" * 20, "secretAccessKey": "s" * 40,
                       "region": "stg-entrypoint", "friendlyId": "Moth-123"}
        with patch.object(dfu, "_read_partition_file_rpc",
                          return_value=json.dumps(credentials).encode()):
            parsed = repoint._existing_credentials("dfu-util", "1-2")
        self.assertEqual(parsed, {"accessKeyId": "A" * 20, "secretAccessKey": "s" * 40,
                                  "friendlyId": "Moth-123"})
        response = io.BytesIO(b'{"adopted":true,"linked":true}')
        with patch.object(repoint.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value = response
            message = repoint._adopt_existing(parsed, "a" * 43)
            request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.jibo.io/api/adopt-robot")
        self.assertIn(b'"claimCode": "', request.data)
        self.assertIn(b'"friendlyId": "Moth-123"', request.data)
        self.assertIn("linked", message)
        with patch.object(dfu, "_read_partition_file_rpc",
                          return_value=json.dumps({**credentials, "region": "evil.example"}).encode()):
            self.assertEqual(repoint._existing_credentials("dfu-util", "1-2"), parsed)

    def test_repoint_does_not_access_credentials_without_adoption_request(self):
        with patch.object(dfu, "_file_loader_context", return_value=(
                "1-2", [dfu.FILE_LEVEL_MARKER_V2], "serial-sha256:robot", "")), \
                patch.object(repoint, "plan", return_value={"changes": [], "rootfs_profiles": {}}), \
                patch.object(repoint, "_existing_credentials") as credentials, \
                patch.object(repoint, "_adopt_existing") as adopt:
            result = repoint.repoint_jibo_io(port="1-2", dfu_util="dfu-util")
        self.assertEqual(result["status"], "repointed-for-ota")
        self.assertFalse(result["adoption_requested"])
        credentials.assert_not_called()
        adopt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
