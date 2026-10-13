import hashlib
import io
import json
from contextlib import redirect_stderr
from pathlib import Path
import shutil
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
        patch_sha = hashlib.sha256(
            (package.ROOT / "patches/shofel2-dfu-entry.patch").read_bytes()).hexdigest()
        (self.shofel_dir / "candidate-entry-manifest.json").write_text(json.dumps({
            "patch_sha256": patch_sha,
            "loader_sha256": hashlib.sha256(self.padded_loader).hexdigest(),
            "loader_size": len(self.padded_loader),
            "files_sha256": {
                name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in (("shofel2_t124", self.shofel),
                                   ("intermezzo.bin", self.intermezzo),
                                   ("dfu_stage2.bin", self.dfu_stage))
            },
        }))

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
            "__main__.py", "loader.bin", "entry-patch.sha256", "README.md", "LICENSE", "NOTICE.md",
            "jibo_dfu.py",
            "jibo_dfu_bounded.py", "jibo_dfu_pipeline.py", "jibo_images.py",
            "jibo_updates.py", "jibo_tui.py", "jibo_gui.py", "jibo_gui_core.py",
            "tools/shofel2_t124", "tools/intermezzo.bin", "tools/dfu_stage2.bin",
            "tools/dfu-util",
            "assets/boot-network-status/startup-view.js",
            "assets/boot-network-status/startup-view-x11-early.js",
            "assets/boot-network-status/startup-view-dom.js",
            "assets/boot-network-status/boot-network-status.sh",
            "assets/boot-network-status/ota-safe-overlay.js",
            "assets/boot-network-status/manifest.json",
        }
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), expected)
            self.assertEqual(archive.read("loader.bin"), self.padded_loader)
            self.assertEqual(archive.read("tools/shofel2_t124"), self.shofel.read_bytes())
            self.assertEqual(archive.read("tools/intermezzo.bin"), b"RCM intermezzo")
            self.assertEqual(archive.read("tools/dfu_stage2.bin"),
                             b"DFU stage payload" + hashlib.sha256(self.padded_loader).digest())
            self.assertEqual(archive.read("tools/dfu-util"), b"dfu-util executable")
            self.assertEqual(archive.read("LICENSE"), (package.ROOT / "LICENSE").read_bytes())
            self.assertEqual(archive.read("NOTICE.md"), (package.ROOT / "NOTICE.md").read_bytes())
            self.assertEqual(archive.read("__main__.py").decode(), package.BOOTSTRAP)
            self.assertEqual(archive.read("entry-patch.sha256").decode().strip(),
                             hashlib.sha256((package.ROOT / "patches/shofel2-dfu-entry.patch")
                                            .read_bytes()).hexdigest())
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

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('mke2fs', 'debugfs', 'e2fsck')),
                         'ext4 tools required')
    def test_packaged_offline_install_uses_the_bundled_assets_after_extraction(self):
        from test_boot_network_status import ImageInstallTests
        import jibo_images
        fixture = ImageInstallTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.run_package()
        candidate = fixture.work / "packaged-installed.ext4"
        developer_source = fixture.work / "developer-source.ext4"
        jibo_images.edit_mode(fixture.source, developer_source, "int-developer")
        result = subprocess.run([sys.executable, str(self.output), "edit-boot-network-status",
                                 str(developer_source), "install", "--out", str(candidate)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["boot_network_status"], "install")
        for name, content in jibo_images.boot_network_status_assets().items():
            self.assertEqual(jibo_images._extract(candidate, jibo_images.BOOT_NETWORK_STATUS_DIR + "/" + name,
                                                 fixture.work / "packaged-readback"), content)
        jibo_images._check_ext4(candidate)

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

    def test_stale_entry_manifest_is_rejected(self):
        manifest_path = self.shofel_dir / "candidate-entry-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["patch_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit):
            self.run_package()
        self.assertIn("does not match the current patch", errors.getvalue())
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

    def test_package_check_rejects_missing_or_changed_license(self):
        self.run_package()
        loader_hash = hashlib.sha256(self.padded_loader).hexdigest()
        with patch.object(check_package, "PINNED_LOADER_SIZE", len(self.padded_loader)), \
                patch.object(check_package, "PINNED_LOADER_SHA256", loader_hash):
            for name, change in (("LICENSE", b"All rights reserved."), ("NOTICE.md", b"")):
                damaged = self.root / ("license-" + name + ".pyz")
                with zipfile.ZipFile(self.output) as source, \
                        zipfile.ZipFile(damaged, "w") as destination:
                    for member in source.namelist():
                        if member == name and not change:
                            continue
                        destination.writestr(member, change if member == name else source.read(member))
                ready, reason = check_package.check_package(damaged)
                self.assertFalse(ready, name)
                self.assertIn(name, reason)

    def test_pinned_file_loader_hash(self):
        self.assertEqual(package.PINNED_LOADER_SIZE, 432_064)
        self.assertEqual(package.PINNED_LOADER_SHA256,
                         "7105edf6d7d9b68e32aef3f2c331a348e0612210cc9bd1c06bb26bba33b8e8c0")


if __name__ == "__main__":
    unittest.main()
