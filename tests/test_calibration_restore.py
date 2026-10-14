"""Database-assisted fresh candidate: synthetic ext4 only, no device writes."""
import base64
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).parent / "fakes"))
import calibration_core_fake as core
import jibo_images as images

@unittest.skipUnless(shutil.which("mke2fs") and shutil.which("debugfs"), "requires e2fsprogs")
class RemoteRestoreTests(unittest.TestCase):
    def setUp(self):
        self.restore = importlib.import_module("jibo_calibration_restore")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stock = self.image("stock", core.FILES, True)
        self.identity = self.image("identity", {}, False)
    def image(self, name, files, caches):
        tree = self.root / (name + "-tree")
        (tree / "jibo/lps").mkdir(parents=True)
        (tree / "etc").mkdir()
        (tree / "jibo/identity.json").write_text(json.dumps({"name": "Example-Unit-Robot", "serial_number": "synthetic", "cpuid": "synthetic", "wifi_mac": "synthetic"}))
        (tree / "jibo/mode.json").write_text('{"mode":"normal"}')
        if caches:
            for suffix in ("left_eye_1", "right_eye_0", "other_variant"):
                (tree / "jibo/lps" / ("distortionPrecomputeFile_" + suffix)).write_bytes(b"synthetic cache")
        for spec in core.ITEMS:
            if spec.key in files and spec.partition == "var":
                path = tree / spec.image_path.lstrip("/")
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(files[spec.key])
        output = self.root / (name + ".img")
        subprocess.run(["mke2fs", "-q", "-t", "ext4", "-F", "-d", str(tree), str(output), "8192"], check=True, capture_output=True)
        return output
    def test_remote_exact_files_identity_local_modes_cache_removal_source_unchanged(self):
        before = images.sha256_file(self.identity)
        output = self.root / "fresh.img"
        result = self.restore.build_fresh_var(self.stock, self.identity, output, core.FILES, core=core)
        self.assertEqual(result["identity_restored"], True)
        for spec in core.ITEMS:
            if spec.key in core.FILES:
                self.assertEqual(images._extract(output, spec.image_path, self.root / "readback"), core.FILES[spec.key])
                mode = "0100600" if spec.key == "audio" else "0100644"
                self.assertEqual(images._file_metadata(output, spec.image_path), {"mode": mode, "uid": "0", "gid": "0"})
        self.assertNotIn("distortionPrecomputeFile_", images._run_debugfs(output, "ls -p /jibo/lps"))
        self.assertEqual(images.sha256_file(self.identity), before)
        images._check_ext4(output)
    def test_missing_or_unknown_identity_refuses_before_candidate_exists(self):
        for identity in (b"{}", b'{"name":"Example-Unit-Robot","serial_number":"s","cpuid":"c","wifi_mac":"w","owner":"synthetic"}'):
            images._replace_file(self.identity, "/jibo/identity.json", identity, self.root)
            output = self.root / "refused.img"
            with self.assertRaises(Exception):
                self.restore.build_fresh_var(self.stock, self.identity, output, core.FILES, core=core)
            self.assertFalse(output.exists())
