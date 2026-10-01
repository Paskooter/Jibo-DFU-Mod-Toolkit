"""Verification of the reusable ARM entry-payload sources."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_file_level_entry.py"
spec = importlib.util.spec_from_file_location("build_file_level_entry", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "entry-payloads"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PayloadSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def current(self):
        manifest = json.loads((ASSETS / "manifest.json").read_text())
        return (manifest["loader_sha256"], manifest["patch_sha256"],
                manifest["shofel_commit"], manifest["files_sha256"])

    def copy_assets(self):
        for name in ("intermezzo.bin", "dfu_stage2.bin", "manifest.json"):
            (self.root / name).write_bytes((ASSETS / name).read_bytes())

    def test_pinned_assets_match_the_shipped_manifest(self):
        sha, patch, commit, files = self.current()
        for name in builder.ARM_PAYLOADS:
            self.assertEqual(digest(ASSETS / name), files[name])
        self.assertEqual(
            builder.validate_payload_source(ASSETS, sha, patch, commit), ASSETS)

    def test_stale_patch_or_loader_or_commit_is_rejected(self):
        sha, patch, commit, _ = self.current()
        for field, value in (("patch_sha256", "0" * 64),
                            ("loader_sha256", "1" * 64),
                            ("shofel_commit", "2" * 40)):
            self.copy_assets()
            manifest = json.loads((self.root / "manifest.json").read_text())
            manifest[field] = value
            (self.root / "manifest.json").write_text(json.dumps(manifest))
            with self.subTest(field=field):
                with self.assertRaisesRegex(RuntimeError, "different"):
                    builder.validate_payload_source(self.root, sha, patch, commit)

    def test_tampered_or_missing_payload_is_rejected(self):
        sha, patch, commit, _ = self.current()
        self.copy_assets()
        (self.root / "intermezzo.bin").write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "does not match its manifest digest"):
            builder.validate_payload_source(self.root, sha, patch, commit)

        self.copy_assets()
        (self.root / "dfu_stage2.bin").unlink()
        with self.assertRaisesRegex(RuntimeError, "is missing dfu_stage2.bin"):
            builder.validate_payload_source(self.root, sha, patch, commit)

    def test_entry_build_manifest_is_also_an_accepted_payload_source(self):
        sha, patch, commit, _ = self.current()
        self.copy_assets()
        entry_manifest = {
            "shofel_commit": commit, "patch_sha256": patch, "loader_sha256": sha,
            "host_platform": "linux-x86_64",
            "files_sha256": {
                "shofel2_t124": "0" * 64,
                **json.loads((self.root / "manifest.json").read_text())["files_sha256"],
            },
        }
        (self.root / "manifest.json").unlink()
        (self.root / "candidate-entry-manifest.json").write_text(
            json.dumps(entry_manifest))
        self.assertEqual(
            builder.validate_payload_source(self.root, sha, patch, commit), self.root)

    def test_a_directory_without_a_payload_manifest_is_rejected(self):
        sha, patch, commit, _ = self.current()
        self.copy_assets()
        (self.root / "manifest.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "no payload manifest"):
            builder.validate_payload_source(self.root, sha, patch, commit)


if __name__ == "__main__":
    unittest.main()
