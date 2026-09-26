import io
from contextlib import redirect_stderr, redirect_stdout
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

    def test_usb_detection(self):
        for port, vid, pid in (("1-2", "0955", "7740"), ("1-3", "0955", "701a"),
                               ("1-4", "1234", "701a")):
            path = self.root / port
            path.mkdir()
            (path / "idVendor").write_text(vid)
            (path / "idProduct").write_text(pid)
        self.assertEqual(j.devices(self.root), [
            {"port": "1-2", "state": "rcm"},
            {"port": "1-3", "state": "dfu"},
        ])

    def test_multiple_robots_require_selection(self):
        found = [{"port": "1-2", "state": "rcm"}, {"port": "1-3", "state": "dfu"}]
        with self.assertRaises(j.DfuError):
            j.select_device(found)
        self.assertEqual(j.select_device(found, "1-3"), found[1])

    def test_enter_shofel_dfu_cli_dispatches_to_entry_flow(self):
        result = {"state": "dfu", "entry_transport": "ShofEL"}
        stdout = io.StringIO()
        with patch.object(j, "enter_shofel_dfu", return_value=result) as enter, \
                redirect_stdout(stdout):
            self.assertEqual(j.main([
                "enter-dfu-shofel", "--port", "1-2", "--shofel", "/opt/shofel/shofel2_t124",
                "--loader", "/tmp/loader.bin", "--timeout", "45",
                "--confirm-meerkat-rev02",
            ]), 0)
        enter.assert_called_once_with(
            "1-2", "/opt/shofel/shofel2_t124", Path("/tmp/loader.bin"), None, 45.0, True)
        self.assertIn('"entry_transport": "ShofEL"', stdout.getvalue())

    def test_backup_var_cli_uses_the_dfu_transport(self):
        result = {"status": "backup complete", "transport": "USB DFU upload"}
        stdout = io.StringIO()
        output_dir = self.root / "backup-output"
        with patch.object(j, "tool", return_value="/usr/bin/dfu-util") as resolve, \
                patch.object(j, "backup_var", return_value=result) as backup, \
                redirect_stdout(stdout):
            self.assertEqual(j.main([
                "backup-var", "--port", "1-2", "--out", str(output_dir), "--refresh",
            ]), 0)
        resolve.assert_called_once_with("dfu-util", None)
        backup.assert_called_once_with("1-2", "/usr/bin/dfu-util", output_dir, True)
        self.assertIn('"transport": "USB DFU upload"', stdout.getvalue())

    def test_flash_update_refuses_rcm_without_attempting_recovery_or_writes(self):
        with patch.object(j.updates, "validate_package", return_value=object()), \
                patch.object(j, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(j, "enter_shofel_dfu") as enter, \
                patch.object(j, "run_with_progress") as transfer:
            with self.assertRaisesRegex(j.DfuError, "Enter DFU with ShofEL"):
                j.flash_update(self.root / "package", preserve_var=True, port="1-2",
                               dfu_util="dfu-util")
        enter.assert_not_called()
        transfer.assert_not_called()

    def test_flash_update_cli_no_longer_accepts_signed_recovery_options(self):
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
            j.main(["flash-update", str(self.root / "package"), "--preserve-var", "--bundle",
                    str(self.root / "bundle")])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("unrecognized arguments: --bundle", errors.getvalue())

    def test_flash_update_cli_passes_resume_manifest_to_backend(self):
        manifest = self.root / "update-manifest.json"
        manifest.write_text("{}")
        with patch.object(j, "tool", return_value="/usr/bin/dfu-util"), \
                patch.object(j, "flash_update", return_value={"status": "verified"}) as flash, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(j.main([
                "flash-update", str(self.root / "package.tar.bz2"), "--preserve-var",
                "--port", "1-1", "--resume-from", str(manifest),
                "--yes",
            ]), 0)
        flash.assert_called_once_with(
            Path(self.root / "package.tar.bz2"), True, "1-1", "/usr/bin/dfu-util",
            None, "FLASH UPDATE", False, manifest,
        )


if __name__ == "__main__":
    unittest.main()
