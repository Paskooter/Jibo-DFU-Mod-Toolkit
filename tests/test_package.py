import hashlib
import io
import json
from contextlib import redirect_stderr
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from textwrap import dedent
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
            "jibo_hosted_updates.py", "jibo_flash_policy.py", "jibo_flash_contract.py", "jibo_flash_schema.py",
            "jibo_calibration_client.py", "jibo_calibration.py",
            "jibo_calibration_flow.py", "jibo_calibration_restore.py",
            "assets/flash-images/contract-v1.json", "assets/flash-images/trust.json",
            "assets/flash-images/flash-release-public.pem",
            "tools/shofel2_t124", "tools/intermezzo.bin", "tools/dfu_stage2.bin",
            "tools/dfu-util",
            "assets/boot-network-status/startup-view.js",
            "assets/boot-network-status/startup-view-x11-early.js",
            "assets/boot-network-status/startup-view-dom.js",
            "assets/boot-network-status/boot-network-status.sh",
            "assets/boot-network-status/ota-safe-overlay.js",
            "assets/boot-network-status/manifest.json",
            "assets/help/rcm-buttons.png",
        }
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), expected)
            self.assertEqual(hashlib.sha256(archive.read("assets/help/rcm-buttons.png")).hexdigest(),
                             package.RCM_HELP_IMAGE_SHA256)
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

    def test_packaged_stdlib_host_openssl_verifier_and_offline_cli(self):
        from fake_flash_server import FakeFlashServer
        self.run_package()
        extracted = self.root / 'extracted'
        with zipfile.ZipFile(self.output) as archive:
            archive.extractall(extracted)
        with FakeFlashServer() as fixture:
            raw = fixture.envelope({'synthetic': 'packaged proof'})
            envelope_path = self.root / 'signed.json'
            envelope_path.write_bytes(raw)
            public_path = self.root / 'public.pem'
            public_path.write_bytes(fixture.public_key.read_bytes())
            command = (
                'from pathlib import Path; import jibo_flash_contract as c; '
                'assert c._python_verifier() is None; '
                'c.trusted_keys=lambda: {"phoenix-flash-2026-10":Path(' + repr(str(public_path)) + ').read_bytes()}; '
                'assert c.verify_envelope(Path(' + repr(str(envelope_path)) + ').read_bytes())["synthetic"] == "packaged proof"; '
                'print("packaged OpenSSL Ed25519 verified")')
            result = subprocess.run([sys.executable, '-S', '-c', command], cwd=extracted,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Ed25519 verified', result.stdout)
        result = subprocess.run([sys.executable, str(self.output), 'list-hosted-updates',
                                '--offline', '--directory', str(self.root / 'empty-updates')],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['images'], [])

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('mke2fs', 'debugfs', 'e2fsck')),
                         'ext4 tools required')
    def test_extracted_package_keeps_calibration_without_source_imports(self):
        from calibration_support import make_image, FILES, GENERIC_CAMERA, PATHS, IDENTITY
        self.run_package()
        extracted = self.root / 'extracted'
        with zipfile.ZipFile(self.output) as archive:
            archive.extractall(extracted)
        camera_keys = ('left', 'right', 'inter_camera')
        stock = make_image(self.root, 'stock.img', {PATHS[k]: GENERIC_CAMERA[k] for k in camera_keys})
        saved = {k: FILES[k] for k in (*camera_keys, 'imu', 'audio')}
        # Every camera item, including alignment, must be robot-specific. An
        # unchanged alignment is now correctly refused by the per-item guard.
        for key in camera_keys:
            self.assertNotEqual(json.loads(saved[key]), json.loads(GENERIC_CAMERA[key]), key)
        entries = {PATHS[k]: raw for k, raw in saved.items()}
        entries['/jibo/identity.json'] = IDENTITY
        backup = make_image(self.root, 'backup.img', entries)
        command = dedent('''\
            import hashlib, json, pathlib, sys
            root = pathlib.Path(sys.argv[1]).resolve()
            sys.path.insert(0, str(root))
            # -I -S removes cwd, PYTHONPATH and site packages. Refuse all sockets
            # and USB helpers even if an unexpected branch tries to use them.
            def offline_only(event, args):
                if event.startswith('socket.'):
                    raise AssertionError('network access attempted')
                if event == 'subprocess.Popen' and pathlib.Path(args[0]).name not in ('debugfs', 'e2fsck'):
                    raise AssertionError('unexpected helper: ' + str(args[0]))
            sys.addaudithook(offline_only)
            import jibo_dfu as dfu
            backup, stock = map(pathlib.Path, sys.argv[2:4])
            digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            before, stock_before = digest(backup), digest(stock)
            tag = 'serial-sha256:' + 'a' * 64
            manifest = backup.parent / 'backup-manifest.json'
            manifest.write_text(json.dumps({'kind': 'jibo-var-backup', 'device_tag': tag, 'sha256': before}))
            preferred = {'image': str(backup), 'sha256': before, 'manifest': str(manifest)}
            dfu.EXPECTED_VAR_SIZE = backup.stat().st_size
            dfu.BACKUP_ROOT = backup.parent / 'unused-backups'
            destination = backup.parent / 'fresh.img'
            selected, result, remote = dfu._resolve_factory_fresh_var(
                tag, preferred, stock, destination, 'synthetic-port', 'forbidden-dfu-util')
            assert selected == preferred and remote is None
            assert result['identity_restored'] is True and result['mode'] == 'oobe'
            for key in ('left', 'right', 'inter_camera', 'imu', 'audio'):
                assert result['calibration_status'][key] == 'restored', key
            import jibo_calibration_flow as flow
            import jibo_calibration_restore as restore
            import jibo_calibration_client as client
            core = client.load_core()
            assert core.__name__ == 'jibo_calibration'
            session = object()
            with flow.operation(session):
                assert flow.active() is session
            assert flow.active() is None
            assert client.CALIBRATION_SHARING_AVAILABLE is False
            assert restore.local_identity(destination) == restore.local_identity(backup)
            for path in result['calibration_files']:
                assert dfu.images._extract(destination, path, backup.parent / 'readback') == dfu.images._extract(
                    backup, path, backup.parent / 'original')
            assert digest(backup) == before and digest(stock) == stock_before
            modules = {name: str(pathlib.Path(module.__file__).resolve())
                       for name, module in sys.modules.items() if name.startswith('jibo_')}
            assert all(pathlib.Path(path).parent == root for path in modules.values()), modules
            print(json.dumps({'status': 'keep-calibration selected and restored', 'modules': sorted(modules)}))
            ''')
        result = subprocess.run([sys.executable, '-I', '-S', '-c', command,
                                 str(extracted), str(backup), str(stock)], cwd=self.root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['status'], 'keep-calibration selected and restored')
        self.assertTrue({'jibo_calibration', 'jibo_calibration_flow', 'jibo_calibration_restore'}
                        .issubset(receipt['modules']))

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

    def test_altered_rcm_help_photo_is_rejected(self):
        with patch.object(package, "RCM_HELP_IMAGE_SHA256", "0" * 64), \
                redirect_stderr(io.StringIO()) as stderr, \
                self.assertRaises(SystemExit):
            self.run_package()
        self.assertIn("RCM help photo", stderr.getvalue())
        self.assertFalse(self.output.exists())

    def test_package_check_rejects_changed_rcm_help_photo(self):
        self.run_package()
        loader_hash = hashlib.sha256(self.padded_loader).hexdigest()
        damaged = self.root / "photo.pyz"
        with zipfile.ZipFile(self.output) as source, zipfile.ZipFile(damaged, "w") as destination:
            for member in source.namelist():
                content = source.read(member)
                destination.writestr(member, content + b"x" if member == "assets/help/rcm-buttons.png"
                                     else content)
        with patch.object(check_package, "PINNED_LOADER_SIZE", len(self.padded_loader)), \
                patch.object(check_package, "PINNED_LOADER_SHA256", loader_hash):
            self.assertEqual(check_package.check_package(self.output), (True, "ready"))
            ready, reason = check_package.check_package(damaged)
        self.assertFalse(ready)
        self.assertIn("rcm-buttons.png", reason)

    def test_pinned_file_loader_hash(self):
        self.assertEqual(package.PINNED_LOADER_SIZE, 432_064)
        self.assertEqual(package.PINNED_LOADER_SHA256,
                         "7105edf6d7d9b68e32aef3f2c331a348e0612210cc9bd1c06bb26bba33b8e8c0")


if __name__ == "__main__":
    unittest.main()
