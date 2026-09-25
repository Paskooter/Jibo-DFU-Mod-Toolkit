"""Checks for stale or unusable cached packages; no USB access is needed."""

from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts import check_package


class CachedPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jibo-package-check-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "tool.pyz"
        self.source = self.root / "source"
        self.source.mkdir()
        for name in check_package.SOURCES:
            (self.source / name).write_bytes(("content of " + name).encode())

    def create_package(self, capability="1"):
        with zipfile.ZipFile(self.package, "w") as archive:
            for name in check_package.SOURCES:
                archive.write(self.source / name, name)
            archive.write(check_package.ROOT / "assets/loader.bin", "loader.bin")
            archive.writestr("tools/shofel2_t124",
                             "#!/bin/sh\nprintf 'dfu-stage-launch={}\\n'\n".format(capability))
            archive.writestr("tools/intermezzo.bin", b"intermezzo")
            archive.writestr("tools/dfu_stage2.bin", b"stage")
            archive.writestr("tools/dfu-util", b"dfu-util")

    def test_valid_package_is_accepted(self):
        self.create_package()
        self.assertEqual(check_package.check_package(self.package, self.source), (True, "ready"))

    def test_disabled_shofel_and_changed_source_require_rebuild(self):
        self.create_package(capability="0")
        ready, reason = check_package.check_package(self.package, self.source)
        self.assertFalse(ready)
        self.assertIn("cannot launch DFU", reason)

        self.create_package()
        (self.source / "jibo_tui.py").write_bytes(b"new menu")
        ready, reason = check_package.check_package(self.package, self.source)
        self.assertFalse(ready)
        self.assertIn("jibo_tui.py", reason)


if __name__ == "__main__":
    unittest.main()
