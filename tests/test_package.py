import hashlib
import io
from contextlib import redirect_stderr
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import scripts.check_package as check_package
import scripts.package as package


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.loader = self.root / "loader.raw"
        self.raw_loader = b"raw test image"
        self.loader.write_bytes(self.raw_loader)
        self.padded_loader = self.raw_loader + bytes((-len(self.raw_loader)) % 16)
        self.shofel_dir = self.root / "shofel"
        self.shofel_dir.mkdir()
        self.shofel = self.shofel_dir / "shofel2_t124"
        self.intermezzo = self.shofel_dir / "intermezzo.bin"
        self.dfu_stage = self.shofel_dir / "dfu_stage2.bin"
        self.dfu_util = self.root / "dfu-util"
        loader_hash = hashlib.sha256(self.padded_loader).digest()
        self.shofel.write_bytes(b"#!/bin/sh\nprintf 'dfu-stage-launch=1\\n'\nexit 0\n" +
                                loader_hash)
        self.shofel.chmod(0o755)
        self.intermezzo.write_bytes(b"RCM intermezzo")
        self.dfu_stage.write_bytes(b"DFU stage payload" + loader_hash)
        self.dfu_util.write_bytes(b"dfu-util executable")
        self.output = self.root / "jibo-tool.pyz"

    def argv(self, *extra):
        return ["package.py", "--loader", str(self.loader),
                "--shofel2", str(self.shofel),
                "--intermezzo", str(self.intermezzo),
                "--dfu-stage", str(self.dfu_stage),
                "--dfu-util", str(self.dfu_util),
                "--out", str(self.output), *extra]

    def run_package(self, argv=None):
        with patch.object(package, "PINNED_LOADER_SIZE", len(self.padded_loader)), \
                patch.object(package, "PINNED_LOADER_SHA256",
                             hashlib.sha256(self.padded_loader).hexdigest()), \
                patch("sys.argv", argv or self.argv()):
            package.main()

    def test_package_contains_only_runtime_files_and_padded_pinned_loader(self):
        self.run_package()

        expected = {
            "__main__.py", "loader.bin", "README.md", "jibo_dfu.py",
            "jibo_dfu_bounded.py", "jibo_images.py", "jibo_updates.py", "jibo_tui.py",
            "tools/shofel2_t124", "tools/intermezzo.bin", "tools/dfu_stage2.bin",
            "tools/dfu-util",
        }
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), expected)
            self.assertEqual(archive.read("loader.bin"), self.padded_loader)
            self.assertEqual(archive.read("tools/shofel2_t124"), self.shofel.read_bytes())
            self.assertEqual(archive.read("tools/intermezzo.bin"), b"RCM intermezzo")
            self.assertEqual(archive.read("tools/dfu_stage2.bin"),
                             b"DFU stage payload" + hashlib.sha256(self.padded_loader).digest())
            self.assertEqual(archive.read("tools/dfu-util"), b"dfu-util executable")
            self.assertEqual(archive.read("__main__.py").decode(), package.BOOTSTRAP)
            self.assertFalse(any(name.startswith("bundles/") for name in archive.namelist()))
            for obsolete in ("tools/tegrarcm", "lib/libcryptopp.so", "tools/emmc_server.bin",
                             "tools/dram_probe.bin", "tools/dram_trace.bin"):
                self.assertNotIn(obsolete, archive.namelist())

    def test_bootstrap_runs_the_packaged_cli(self):
        self.run_package()
        result = subprocess.run([sys.executable, str(self.output), "--help"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("enter-dfu-shofel", result.stdout)

    def test_loader_must_match_pinned_padded_size_and_hash(self):
        self.loader.write_bytes(b"different data")
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit):
            self.run_package()
        self.assertIn("SHA-256 does not match", errors.getvalue())
        self.assertFalse(self.output.exists())

        self.loader.write_bytes(b"this is a much longer loader")
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit):
            self.run_package()
        self.assertIn("Padded loader has", errors.getvalue())
        self.assertFalse(self.output.exists())

    def test_launch_disabled_shofel_is_rejected(self):
        self.shofel.write_text("#!/bin/sh\nprintf 'dfu-stage-launch=0\\n'\n")
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit):
            self.run_package()
        self.assertIn("without DFU launch support", errors.getvalue())
        self.assertFalse(self.output.exists())

    def test_each_cli_input_is_required(self):
        baseline = self.argv()
        for option in ("--loader", "--shofel2", "--intermezzo", "--dfu-stage",
                       "--dfu-util", "--out"):
            with self.subTest(option=option):
                index = baseline.index(option)
                incomplete = baseline[:index] + baseline[index + 2:]
                errors = io.StringIO()
                with redirect_stderr(errors), self.assertRaises(SystemExit), \
                        patch("sys.argv", incomplete):
                    package.main()
                self.assertIn(option, errors.getvalue())
                self.assertFalse(self.output.exists())

    def test_missing_empty_and_symlink_inputs_fail(self):
        self.intermezzo.unlink()
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit):
            self.run_package()
        self.assertIn("Could not read tools/intermezzo.bin", errors.getvalue())
        self.intermezzo.write_bytes(b"RCM intermezzo")

        self.dfu_stage.write_bytes(b"")
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit):
            self.run_package()
        self.assertIn("must not be empty", errors.getvalue())
        self.dfu_stage.write_bytes(b"DFU stage payload")

        link = self.root / "linked-dfu-util"
        link.symlink_to(self.dfu_util)
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit):
            self.run_package(self.argv("--dfu-util", str(link)))
        self.assertIn("regular, non-symlink file", errors.getvalue())
        self.assertFalse(self.output.exists())

    def test_private_key_material_is_rejected(self):
        self.dfu_util.write_bytes(b"-----BEGIN PRIVATE KEY-----\nsecret")
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit):
            self.run_package()
        self.assertIn("Private key material detected in tools/dfu-util", errors.getvalue())
        self.assertFalse(self.output.exists())

    def test_stage2_hash_must_match_loader(self):
        self.dfu_stage.write_bytes(b"DFU stage payload")
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit):
            self.run_package()
        self.assertIn("dfu_stage2.bin does not embed exactly", errors.getvalue())
        self.assertFalse(self.output.exists())


    def test_package_check_rejects_stage2_with_stale_loader_hash(self):
        self.run_package()
        loader_hash = hashlib.sha256(self.padded_loader).hexdigest()
        with patch.object(check_package, "PINNED_LOADER_SIZE", len(self.padded_loader)), \
                patch.object(check_package, "PINNED_LOADER_SHA256", loader_hash):
            self.assertEqual(check_package.check_package(self.output), (True, "ready"))
            damaged = self.root / "stale-stage.pyz"
            with zipfile.ZipFile(self.output) as source, \
                    zipfile.ZipFile(damaged, "w") as destination:
                for name in source.namelist():
                    content = source.read(name)
                    if name == "tools/dfu_stage2.bin":
                        content = b"stale stage helper"
                    destination.writestr(name, content)
            result = check_package.check_package(damaged)
        self.assertEqual(result, (False, "tools/dfu_stage2.bin does not embed the pinned RAM loader hash"))

    def test_pinned_file_loader_hash(self):
        self.assertEqual(package.PINNED_LOADER_SIZE, 432_000)
        self.assertEqual(package.PINNED_LOADER_SHA256,
                         "fd5fc5b1759ddbdbb0da88ac89c425ab95eb0bc494917cfe27daf71e55a31095")


if __name__ == "__main__":
    unittest.main()
