"""Manifest compatibility for the bundled v1 and separate v2 entry helpers."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import scripts.build_file_level_entry as entry


class EntryManifestTests(unittest.TestCase):
    def _reusable_output(self, root, loader):
        output = root / "entry"
        output.mkdir()
        files = {}
        for name in ("shofel2_t124", "intermezzo.bin", "dfu_stage2.bin"):
            content = name.encode()
            (output / name).write_bytes(content)
            files[name] = hashlib.sha256(content).hexdigest()
        (output / "candidate-entry-manifest.json").write_text(json.dumps({
            "loader_sha256": hashlib.sha256(loader.read_bytes()).hexdigest(),
            "loader_size": loader.stat().st_size,
            "shofel_commit": entry.SHOFEL_COMMIT,
            "files_sha256": files,
        }))
        return output

    def test_bundled_v1_manifest_remains_accepted(self):
        loader = entry.ROOT / "assets/loader.bin"
        with tempfile.TemporaryDirectory() as directory:
            output = self._reusable_output(Path(directory), loader)
            with patch("sys.argv", ["build_file_level_entry.py", "--loader", str(loader),
                                    "--out", str(output)]):
                entry.main()

    def test_v2_manifest_must_pin_current_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader = root / "experimental-file-rpc-loader.bin"
            loader.write_bytes(b"candidate")
            manifest = {"kind": "experimental-file-rpc-loader", "protocol": "jibo-file-v2",
                        "size_bytes": loader.stat().st_size,
                        "sha256": hashlib.sha256(loader.read_bytes()).hexdigest(),
                        "source_file_level_patch_sha256": "incorrect"}
            (root / "manifest.json").write_text(json.dumps(manifest))
            output = self._reusable_output(root, loader)
            with patch("sys.argv", ["build_file_level_entry.py", "--loader", str(loader),
                                    "--out", str(output)]):
                with self.assertRaises(SystemExit):
                    entry.main()
            manifest["source_file_level_patch_sha256"] = hashlib.sha256(
                (entry.ROOT / "firmware/file-level.patch").read_bytes()).hexdigest()
            (root / "manifest.json").write_text(json.dumps(manifest))
            with patch("sys.argv", ["build_file_level_entry.py", "--loader", str(loader),
                                    "--out", str(output)]):
                entry.main()


if __name__ == "__main__":
    unittest.main()
