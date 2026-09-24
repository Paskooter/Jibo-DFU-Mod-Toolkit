import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import jibo_dfu as toolkit
from jibo_dfu import FILES
import scripts.package as package


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        records = {}
        for name in FILES:
            content = bytes(8192) if name == "rcm.bct" else b"public test artifact"
            (self.bundle / name).write_bytes(content)
            records[name] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        (self.bundle / "manifest.json").write_text(json.dumps({
            "schema": 1, "entry": "jibo-ram-dfu-v1", "soc": 124,
            "load_address": "0x80108000", "persistent_writes_on_entry": False,
            "hardware_verified": False, "files": records,
        }))
        self.tegrarcm = self.root / "tegrarcm"
        self.dfu_util = self.root / "dfu-util"
        self.crypto = self.root / "libcryptopp.so"
        for path in (self.tegrarcm, self.dfu_util, self.crypto):
            path.write_bytes(b"test tool")
        self.shofel_dir = self.root / "shofel"
        self.shofel_dir.mkdir()
        self.shofel = self.shofel_dir / "shofel2_t124"
        self.payload = self.shofel_dir / "emmc_server.bin"
        self.intermezzo = self.shofel_dir / "intermezzo.bin"
        self.dram_probe = self.shofel_dir / "dram_probe.bin"
        self.dram_trace = self.shofel_dir / "dram_trace.bin"
        self.dfu_stage = self.shofel_dir / "dfu_stage2.bin"
        self.shofel.write_bytes(b"ShofEL executable")
        self.payload.write_bytes(b"eMMC read payload")
        self.intermezzo.write_bytes(b"RCM intermezzo")
        self.dram_probe.write_bytes(b"DRAM diagnostic payload")
        self.dram_trace.write_bytes(b"DRAM trace payload")
        self.dfu_stage.write_bytes(b"Staged DFU receiver")
        self.output = self.root / "jibo-tool.pyz"

    def argv(self, *extra):
        return ["package.py", "--bundle", str(self.bundle),
                "--tegrarcm", str(self.tegrarcm), "--dfu-util", str(self.dfu_util),
                "--libcryptopp", str(self.crypto), "--out", str(self.output), *extra]

    def test_optional_shofel_pair_is_bundled_at_tool_discovery_paths(self):
        args = self.argv("--shofel2", str(self.shofel), "--emmc-server", str(self.payload),
                         "--intermezzo", str(self.intermezzo), "--dram-probe", str(self.dram_probe),
                         "--dram-trace", str(self.dram_trace),
                         "--dfu-stage", str(self.dfu_stage))
        with patch("sys.argv", args):
            package.main()
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read("tools/shofel2_t124"), b"ShofEL executable")
            self.assertEqual(archive.read("tools/emmc_server.bin"), b"eMMC read payload")
            self.assertEqual(archive.read("tools/intermezzo.bin"), b"RCM intermezzo")
            self.assertEqual(archive.read("tools/dram_probe.bin"), b"DRAM diagnostic payload")
            self.assertEqual(archive.read("tools/dram_trace.bin"), b"DRAM trace payload")
            self.assertEqual(archive.read("tools/dfu_stage2.bin"), b"Staged DFU receiver")
            with tempfile.TemporaryDirectory() as extracted:
                archive.extractall(extracted)
                for executable in (Path(extracted) / "tools").iterdir():
                    executable.chmod(0o755)
                with patch.object(toolkit, "ROOT", Path(extracted)):
                    self.assertTrue(toolkit.shofel_available())
                    self.assertEqual(toolkit._shofel_tool(),
                                     str((Path(extracted) / "tools/shofel2_t124").resolve()))

    def test_shofel_host_and_payload_must_be_supplied_together(self):
        with patch("sys.argv", self.argv("--shofel2", str(self.shofel))), \
                self.assertRaises(SystemExit):
            package.main()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
