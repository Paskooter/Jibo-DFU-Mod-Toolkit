import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jibo_dfu as j


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = {"schema": 1, "entry": "jibo-ram-dfu-v1", "soc": 124,
                         "load_address": "0x80108000", "persistent_writes_on_entry": False,
                         "hardware_verified": False, "files": {}}
        for name in j.FILES:
            content = bytes(8192) if name == "rcm.bct" else b"synthetic public artifact"
            (self.root / name).write_bytes(content)
            self.manifest["files"][name] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        self.save_manifest()

    def save_manifest(self):
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))

    def test_valid_bundle(self):
        self.assertEqual(j.load_bundle(self.root)["soc"], 124)

    def test_corruption(self):
        (self.root / "rcm.bl").write_bytes(b"corrupted")
        with self.assertRaisesRegex(j.DfuError, "integrity"):
            j.load_bundle(self.root)

    def test_symlink_rejected(self):
        (self.root / "rcm.bl").unlink()
        (self.root / "rcm.bl").symlink_to(self.root / "rcm.qry")
        with self.assertRaisesRegex(j.DfuError, "symlinked"):
            j.load_bundle(self.root)

    def test_persistent_flasher_rejected(self):
        self.manifest["persistent_writes_on_entry"] = True
        self.save_manifest()
        with self.assertRaisesRegex(j.DfuError, "preserves"):
            j.load_bundle(self.root)

    def test_extra_artifact_rejected(self):
        self.manifest["files"]["../private.key"] = {}
        self.save_manifest()
        with self.assertRaises(j.DfuError):
            j.load_bundle(self.root)

    def test_usb_detection(self):
        for port, vid, pid in (("1-2", "0955", "7740"), ("1-3", "0955", "701a"), ("1-4", "1234", "701a")):
            path = self.root / port
            path.mkdir()
            (path / "idVendor").write_text(vid)
            (path / "idProduct").write_text(pid)
        self.assertEqual(j.devices(self.root), [{"port": "1-2", "state": "rcm"}, {"port": "1-3", "state": "dfu"}])

    def test_multiple_robots_require_selection(self):
        found = [{"port": "1-2", "state": "rcm"}, {"port": "1-3", "state": "dfu"}]
        with self.assertRaises(j.DfuError):
            j.select_device(found)
        self.assertEqual(j.select_device(found, "1-3"), found[1])

    @patch.object(j, "devices", return_value=[{"port": "1-2", "state": "dfu"}])
    @patch.object(j, "run", return_value='Found DFU: alt=0, name="var"')
    def test_existing_dfu_needs_no_bundle_or_key(self, run, devices):
        result = j.enter(self.root / "missing", None, "missing-tegrarcm", "dfu-util")
        self.assertTrue(result["already_running"])
        self.assertFalse(result["loader_verified"])
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("-D", run.call_args.args[0])

    @patch.object(j, "devices", return_value=[{"port": "1-2", "state": "rcm"}])
    @patch.object(j, "run")
    def test_bundle_requires_hardware_profile_verification(self, run, devices):
        with self.assertRaisesRegex(j.DfuError, "not verified for its declared hardware profile"):
            j.enter(self.root, None, "tegrarcm", "dfu-util")
        run.assert_not_called()

    def test_rcm_to_dfu(self):
        states = [[{"port": "1-2", "state": "rcm"}], [{"port": "1-2", "state": "dfu"}]]
        with patch.object(j, "devices", side_effect=states), patch.object(j, "run", side_effect=["OK", 'name="jibo-dfu-v1" name="var"']) as run:
            result = j.enter(self.root, None, "tegrarcm", "dfu-util", allow_unverified_profile=True)
            self.assertTrue(result["loader_verified"])
            argv = run.call_args_list[0].args[0]
            self.assertIn("--download-signed-msgs", argv)
            self.assertFalse(any("--pkc" in arg for arg in argv))
            self.assertIn("--usb-port-path=1-2", argv)

    def test_rcm_query_usb_failure_is_not_reported_as_dfu_failure(self):
        failure = j.DfuError("Command failed: tegrarcm\n"
                             "read RCM query version: USB transfer failure\n"
                             "Resource temporarily unavailable")
        with patch.object(j, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(j, "run", side_effect=failure) as run:
            with self.assertRaisesRegex(j.DfuError, "before the recovery loader was sent") as caught:
                j.enter(self.root, None, "tegrarcm", "dfu-util", allow_unverified_profile=True)
        self.assertIn("still in RCM/APX", str(caught.exception))
        self.assertIn("signing profile", str(caught.exception))
        self.assertEqual(run.call_count, 1)

    def test_wrong_loader_marker(self):
        states = [[{"port": "1-2", "state": "rcm"}], [{"port": "1-2", "state": "dfu"}]]
        with patch.object(j, "devices", side_effect=states), patch.object(j, "run", side_effect=["OK", 'name="var"']):
            with self.assertRaisesRegex(j.DfuError, "marker"):
                j.enter(self.root, None, "tegrarcm", "dfu-util", allow_unverified_profile=True)

    @patch.object(j, "devices", return_value=[])
    @patch.object(j, "run")
    def test_no_device_never_transfers(self, run, devices):
        with self.assertRaisesRegex(j.DfuError, "No Jibo"):
            j.enter(self.root, None, "tegrarcm", "dfu-util")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
